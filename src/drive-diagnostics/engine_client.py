"""Contract-backed, consumer-only client for the Google Drive diagnostic flow.

The client deliberately owns no OAuth credentials or provider transport.  Engine
is the OAuth and connection authority; this module only calls the generated
Engine route table and keeps the short-lived state/identity bindings needed to
prevent a UI from mixing accounts or collections.
"""

from __future__ import annotations

import json
import hashlib
import http.client
import os
import re
import ssl
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_VERIFIED_READ_BYTES = 64 * 1024 * 1024 * 1024
_VERIFIED_READ_CHUNK_BYTES = 64 * 1024
_VERIFIED_READ_DEADLINE_SECONDS = 600.0
_PROVIDER_ID = "google-drive"
_CONSUMER_HEADER = "X-Linnaeus-Consumer-Id"
_GRAPH_COMPLETED_STATUSES = frozenset(("completed", "succeeded"))
_GRAPH_FAILED_STATUSES = frozenset((
    "completedwithfailures", "failed", "cancelled", "canceled", "queuetimeout",
    "timedout", "lost", "superseded",
))
_GRAPH_ACTIVE_STATUSES = frozenset((
    "planned", "admitted", "queued", "starting", "running", "recovering",
    "waiting", "cancelling", "canceledpending",
))
_ACTIONS = (
    "readiness", "start", "status", "connections", "collections", "assets",
    # These are optional until the corresponding committed Engine route is
    # projected.  Keeping the action names here lets the diagnostic UI remain
    # useful across Engine contract revisions without inventing a fallback
    # endpoint or silently using the legacy import path.
    "browse", "acquire", "inspect", "verifiedRead", "verify", "materialize",
    "release", "delete", "revoke", "graph",
    "graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire",
    "graphStatus", "graphWatch", "graphCancel",
)
_OPTIONAL_ACTIONS = frozenset(_ACTIONS) - frozenset(("readiness", "start", "status", "connections", "collections", "assets"))
_ACTION_ALIASES = {
    "access": ("verifiedRead", "inspect"),
    "release": ("release", "tombstone"),
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SAFE_STATUS_KEYS = {
    "providerId", "state", "status", "isSuccess", "reasonCodes", "expiresAtUtc", "httpStatus",
    "reconciliationRequired", "retryable",
}
_PUBLIC_GRAPH_ADMISSION_REASON_CODES = frozenset((
    "external_media_graph_execute_right_missing",
    "external_media_trust_unavailable",
    "external_media_clock_unavailable",
    "external_media_implementation_unavailable",
    "graph_execution_ticket_invalid_request",
    "graph_execution_ticket_authorization_denied",
    "graph_execution_ticket_evidence_author_unavailable",
    "graph_execution_ticket_plan_not_admitted",
    "external_media_admitted_invocation_conflict",
    "external_media_terminal_replay_unavailable",
    "external_media_reconciliation_required",
))


def _project_http_error(data: bytes, status: int) -> dict[str, Any]:
    """Expose only bounded transport evidence and fixed public graph errors.

    Engine problem details can contain internal messages, identities, and
    extensions.  Only the numeric response status and the exact admission
    reason codes published by the external-media route may cross into this
    diagnostic UI.  The response body can never turn an HTTP failure into a
    success.
    """
    output: dict[str, Any] = {
        "isSuccess": False,
        "reasonCodes": ["engine_http_error"],
    }
    if type(status) is int and 400 <= status <= 599:
        output["httpStatus"] = status
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return output
    if not isinstance(parsed, dict):
        return output
    reason_code = parsed.get("reasonCode")
    if type(reason_code) is str and reason_code in _PUBLIC_GRAPH_ADMISSION_REASON_CODES:
        output["reasonCodes"] = [reason_code]
        if reason_code == "external_media_reconciliation_required":
            # This route's recovery contract is fixed: callers must reconcile
            # before retrying, regardless of untrusted body extensions.
            output["reconciliationRequired"] = True
            output["retryable"] = False
    return output


class DriveClientError(ValueError):
    """A redacted client failure safe to show in a diagnostic UI."""


@dataclass(frozen=True)
class Config:
    base_url: str
    bearer_token: str
    product_id: str
    environment_id: str
    tenant_id: str
    workspace_id: str
    consumer_id: str
    user_id: str | None = None
    team_id: str | None = None
    production_id: str | None = None
    certificate_sha256: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise DriveClientError(f"missing_engine_configuration:{name}")
            return value

        return cls(
            base_url=required("ENGINE_BASE_URL"),
            bearer_token=(values.get("ENGINE_ACCESS_TOKEN", "").strip() or required("ENGINE_BEARER_TOKEN")),
            product_id=required("ENGINE_PRODUCT_ID"),
            environment_id=required("ENGINE_ENVIRONMENT_ID"),
            tenant_id=required("ENGINE_TENANT_ID"),
            workspace_id=required("ENGINE_WORKSPACE_ID"),
            consumer_id=required("ENGINE_CONSUMER_ID"),
            user_id=values.get("ENGINE_USER_ID", "").strip() or None,
            team_id=values.get("ENGINE_TEAM_ID", "").strip() or None,
            production_id=values.get("ENGINE_PRODUCTION_ID", "").strip() or None,
            certificate_sha256=values.get("ENGINE_CERTIFICATE_SHA256", "").strip() or None,
        )

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DriveClientError("invalid_engine_base_url")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise DriveClientError("invalid_engine_base_url")
        is_loopback = parsed.hostname.lower().strip("[]") in _LOCAL_HOSTS
        if parsed.scheme == "http" and not is_loopback:
            raise DriveClientError("remote_http_engine_base_forbidden")
        try:
            port = parsed.port
        except ValueError as exc:
            raise DriveClientError("invalid_engine_base_url") from exc
        if port is not None and not 1 <= port <= 65535:
            raise DriveClientError("invalid_engine_base_url")
        if self.certificate_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", self.certificate_sha256):
            raise DriveClientError("invalid_engine_certificate_pin")
        if parsed.scheme == "https" and not is_loopback and self.certificate_sha256 is None:
            raise DriveClientError("engine_certificate_pin_required")
        if any(not value for value in (self.bearer_token, self.product_id, self.environment_id, self.tenant_id, self.workspace_id, self.consumer_id)):
            raise DriveClientError("invalid_engine_configuration")
        if not any((self.user_id, self.team_id, self.production_id)):
            raise DriveClientError("owner_scope_required")

    @property
    def owner_scope(self) -> dict[str, str]:
        result = {"tenantId": self.tenant_id, "workspaceId": self.workspace_id}
        for key, value in (("userId", self.user_id), ("teamId", self.team_id), ("productionId", self.production_id)):
            if value:
                result[key] = value
        return result


class DriveClient:
    """Small consumer for projected Engine Drive routes.

    Optional actions are deliberately capability-gated by ``contracts.json``.
    The tester never synthesizes a route while the Engine source change is
    pending, and it never acquires content as a side effect of browsing.
    """

    def __init__(self, config: Config, contracts_path: str | os.PathLike[str] | None = None, timeout: float = 10.0) -> None:
        if timeout <= 0 or timeout > 60:
            raise DriveClientError("invalid_engine_timeout")
        self.config = config
        self.timeout = timeout
        self._contracts_path = Path(contracts_path) if contracts_path else Path(__file__).with_name("contracts.json")
        self._callback_state: str | None = None
        self._state: str | None = None
        self._connection_ids: set[str] = set()
        self._collection_ids: dict[str, set[str]] = {}
        self._asset_ids: dict[str, set[str]] = {}
        self._selection_refs: dict[str, set[str]] = {}
        self._artifact_ids: dict[str, set[str]] = {}
        self._last_collection_connection: str | None = None
        # A graph ticket is short-lived Engine state.  Keep the canonical
        # request and route binding together so a UI cannot mix an invocation
        # with another account, location, or acquisition request.
        self._graph_bindings: dict[str, dict[str, Any]] = {}

    def execute(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if action == "access":
            # The reusable diagnostic UI is metadata-only. Do not turn an
            # ambiguous access request into a binary content read.
            action = "inspect" if self._has_operation("inspect") else "verifiedRead"
        if action not in _ACTIONS:
            if action in {"import", "download", "open"}:
                return {"action": action, "isSuccess": False, "reasonCodes": ["capability_unavailable"]}
            raise DriveClientError("unsupported_drive_action")
        if action in _OPTIONAL_ACTIONS and not self._has_operation(action):
            return {"action": action, "isSuccess": False, "reasonCodes": ["capability_unavailable"]}
        if action == "start":
            # Starting over may select another Google account. Invalidate the
            # previous attempt before any fallible contract or network work.
            self._state = None
            self._callback_state = None
            self._clear_connection_bindings()
        operation = self._operation_for_action(action)
        body: dict[str, Any] | None = None
        query: dict[str, str] = {}
        if action == "readiness":
            query = {"productId": self.config.product_id, "environmentId": self.config.environment_id, "providerId": _PROVIDER_ID}
        elif action == "start":
            body = {
                "productId": self.config.product_id,
                "environmentId": self.config.environment_id,
                "providerId": _PROVIDER_ID,
                "ownerScope": self.config.owner_scope,
                "requestedByActorId": self.config.user_id,
            }
        elif action == "status":
            if not self._state:
                return {"action": action, "isSuccess": False, "reasonCodes": ["authorization_not_started"]}
            query = {"state": self._state}
        elif action == "connections":
            query = self.config.owner_scope
        elif action in {"collections", "assets", "browse", "acquire", "inspect", "verifiedRead", "verify", "materialize", "release", "delete", "revoke",
                        "graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch", "graphCancel"}:
            body = self._bound_body(action, payload)

        # Artifact access is defined as a GET by the Engine contract. Its
        # purpose/correlation metadata belongs in the query, never a JSON
        # body. Keep the bound artifact in route_body so {artifactId} can be
        # resolved without sending opaque IDs as a request payload.
        route_body = body
        if action in {"inspect", "verifiedRead"}:
            supplied = payload if isinstance(payload, Mapping) else {}
            purpose = supplied.get("purpose")
            if purpose is None:
                purpose = "drive-diagnostics.inspect" if action == "inspect" else "drive-diagnostics.read"
            if not isinstance(purpose, str) or not purpose or len(purpose) > 128:
                raise DriveClientError("artifact_access_purpose_invalid")
            query = {"purpose": purpose}
            for key in ("sessionId", "correlationId", "traceparent"):
                value = supplied.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value or len(value) > 256:
                        raise DriveClientError("artifact_access_context_invalid")
                    query[key] = value
            body = None
        request_body = body

        # Artifact routes bind the opaque artifact identifier in the path.  The
        # POST body contains only the caller-owned access/mutation metadata; the
        # private route binding is never sent over the wire.
        if action in {"verify", "materialize", "release", "delete"} and isinstance(body, Mapping):
            request_body = {key: value for key, value in body.items() if key != "__routeArtifactId"}
        if action in {"graphPrepareBrowse", "graphPrepareAcquire"} and isinstance(body, Mapping):
            request_body = {key: value for key, value in body.items() if not key.startswith("__graph")}
        if action in {"graphSubmitBrowse", "graphSubmitAcquire"} and isinstance(body, Mapping):
            request_body = {key: value for key, value in body.items() if not key.startswith("__graph")}
        if action == "graphStatus":
            request_body = None
        if action == "graphWatch" and isinstance(body, Mapping):
            request_body = body.get("__graphCursor")
        if action == "graphCancel" and isinstance(body, Mapping):
            request_body = {"idempotencyKey": body["idempotencyKey"]}
        # Connection revocation binds the connection only in the route.  The
        # Engine contract has no JSON request DTO for this endpoint; retaining
        # the bound body here would make the diagnostic client depend on an
        # unrecognized provider-facing payload shape.
        if action == "revoke":
            request_body = None

        if action in {"graphSubmitBrowse", "graphSubmitAcquire"}:
            kind = "browse" if action == "graphSubmitBrowse" else "acquire"
            binding = self._graph_bindings.get(kind)
            if binding is not None:
                # Submission is one-shot even when transport fails. Status
                # reconciliation must settle this attempt before another submit.
                binding["submitted"] = True
                binding["statusTerminal"] = False

        if action == "connections":
            # Treat every refresh attempt as a binding boundary.  A failed or
            # malformed refresh must not leave the UI able to use old refs.
            self._clear_connection_bindings()
        selected_connection = route_body.get("connectionId") if isinstance(route_body, Mapping) else None
        result = self._request(
            operation["method"],
            self._path(operation["path"], action, route_body),
            query=query,
            body=request_body,
            expect_binary=action == "verifiedRead",
        )
        result = self._normalize_published_shape(action, result)
        raw_result = result
        result = self._sanitize(action, result, selected_connection=selected_connection)
        if action in {"graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch", "graphCancel"}:
            graph_kind = payload.get("graphKind") if isinstance(payload, Mapping) else None
            result = self._project_graph_result(action, raw_result, result, graph_kind=graph_kind)
        if action == "start" and result.get("isSuccess"):
            state = result.get("state")
            url = result.get("authorizationUrl")
            if not isinstance(state, str) or not state:
                return {"action": action, "isSuccess": False, "reasonCodes": ["engine_response_invalid"]}
            self._validate_authorization_url(url)
            self._state = state
            self._callback_state = state
        elif action == "connections" and result.get("isSuccess"):
            self._remember_connections(result)
        elif action == "revoke" and result.get("isSuccess"):
            # Revocation is a binding boundary.  Do not let a later UI request
            # reuse cached connection, selection, retained-artifact, or graph
            # handles after the provider connection has been revoked.
            self._clear_connection_bindings()
        elif action == "collections" and result.get("isSuccess"):
            result = {**result, "connectionId": self._last_collection_connection}
            self._remember_collections(result, self._last_collection_connection)
        elif action in {"assets", "browse"} and result.get("isSuccess"):
            if action == "browse":
                self._remember_collections(result, selected_connection)
            self._remember_assets(
                result,
                selected_connection,
                # The canonical browse route can expose the same opaque
                # cloud-selection asset handle as graph browse.  The strict
                # prefix check prevents arbitrary provider IDs from being
                # promoted, while the connection binding keeps account
                # switching isolated.
                allow_cloud_selection_asset_id=action == "browse",
            )
        elif action in {"acquire", "materialize"} and result.get("isSuccess"):
            self._remember_artifacts(result, selected_connection)
        elif action in {"graphPrepareBrowse", "graphPrepareAcquire"} and result.get("isSuccess"):
            self._remember_graph_prepare(action, result, body)
        elif action in {"graphSubmitBrowse", "graphSubmitAcquire"}:
            self._remember_graph_submit(action, result)
            if action == "graphSubmitBrowse" and result.get("isSuccess"):
                binding = self._graph_bindings.get("browse")
                if binding is not None:
                    connection_id = binding.get("connectionId")
                    # Graph browse output is the Engine-authorized source for
                    # the canonical cloud-selection asset handle. Keep folder
                    # refs as well so the next bounded folder listing can use
                    # the same account binding.
                    self._remember_collections(result, connection_id)
                    self._remember_assets(result, connection_id, allow_cloud_selection_asset_id=True)
        elif action in {"graphStatus", "graphWatch"}:
            self._remember_graph_status(result)
        result.pop("_graphBindingVerified", None)
        return result

    def complete_callback(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        """Relay one Engine-issued attempt; Engine owns PKCE and token exchange."""
        if (set(parameters) - {"state", "code", "error"}
                or any(not isinstance(value, str) or not value or len(value) > 4096 for value in parameters.values())
                or ("code" in parameters) == ("error" in parameters)):
            raise DriveClientError("callback_parameters_invalid")
        state = parameters.get("state", "")
        if not self._callback_state or not secrets.compare_digest(state, self._callback_state):
            raise DriveClientError("callback_state_invalid")
        # Consume before transport: a timeout is an unknown outcome, never a
        # reason to replay a provider authorization code automatically.
        self._callback_state = None
        operation = self._operation("callback")
        path = operation["path"].replace("{providerId}", _PROVIDER_ID)
        result = self._request(operation["method"], self._path(path, "callback"),
                               query=dict(parameters), body=None, callback_text=True)
        return {"isSuccess": result.get("isSuccess") is True}

    @staticmethod
    def _normalize_published_shape(action: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize published DTOs before their safe consumer projection."""
        normalized = dict(result)
        status = result.get("httpStatus")
        if not isinstance(status, int) or not 200 <= status < 300:
            return normalized
        if action == "browse" and "result" in result:
            nested = result.get("result")
            if type(result.get("isSuccess")) is not bool or not isinstance(nested, Mapping):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            nested_success = nested.get("isSuccess")
            if type(nested_success) is not bool:
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            if result.get("isSuccess") is not True or nested_success is not True:
                reasons = _reason_codes(nested.get("reasonCodes")) or _reason_codes(result.get("reasonCodes"))
                return {
                    "isSuccess": False,
                    "reasonCodes": reasons or ["engine_response_invalid"],
                    "httpStatus": status,
                }
            if (not isinstance(nested.get("collections"), list)
                    or not isinstance(nested.get("assets"), list)):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            outer_connection = result.get("connectionId")
            nested_connection = nested.get("connectionId")
            if (isinstance(outer_connection, str) and isinstance(nested_connection, str)
                    and outer_connection != nested_connection):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            normalized = {key: value for key, value in result.items() if key != "result"}
            normalized.update(nested)
            normalized["isSuccess"] = True
            normalized["reasonCodes"] = _reason_codes(nested.get("reasonCodes"))
            return normalized
        if action == "acquire" and "publication" in result:
            publication = result.get("publication")
            if type(result.get("isSuccess")) is not bool or not isinstance(publication, Mapping):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            if result.get("isSuccess") is not True:
                reasons = _reason_codes(result.get("reasonCodes")) or _reason_codes(publication.get("reasonCodes"))
                return {
                    "isSuccess": False,
                    "reasonCodes": reasons or ["engine_response_invalid"],
                    "httpStatus": status,
                }
            record = publication.get("record")
            if not isinstance(record, Mapping):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            artifact_id = _canonical_artifact_value(record.get("artifactId"))
            if not isinstance(artifact_id, str) or not artifact_id or not _valid_opaque_artifact_id(artifact_id):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            # Keep only the bounded publication fields the diagnostic app can
            # use; the full lifecycle record remains Engine-owned.
            safe_publication: dict[str, Any] = {"artifactId": artifact_id}
            for source, target in (("contentSha256", "contentSha256"), ("contentType", "contentType"),
                                   ("sizeBytes", "byteSize"), ("reasonCode", "reasonCode")):
                value = record.get(source)
                if value is None:
                    value = publication.get(source)
                if isinstance(value, str) and len(value) <= 512:
                    safe_publication[target] = value
                elif source == "sizeBytes" and type(value) is int and value >= 0:
                    safe_publication[target] = value
            normalized = {key: value for key, value in result.items() if key != "publication"}
            normalized["publication"] = safe_publication
            normalized["retainedArtifactId"] = artifact_id
            normalized["isSuccess"] = True
            normalized["reasonCodes"] = _reason_codes(result.get("reasonCodes"))
            return normalized
        # Graph lifecycle DTOs are authoritative even if a legacy/proxy
        # response also carries an optimistic isSuccess field.  Do not let an
        # outer success bit bypass validation of the published watch/status/
        # cancel envelope.
        if "isSuccess" in result and action not in {
            "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch", "graphCancel",
        }:
            return normalized

        if action == "readiness":
            providers = result.get("providers")
            if (
                not isinstance(result.get("productId"), str)
                or not isinstance(result.get("environmentId"), str)
                or not isinstance(result.get("reasonCodes"), list)
                or not isinstance(providers, list)
                or any(not isinstance(item, Mapping) for item in providers)
            ):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            google = [item for item in providers if item.get("providerId") == _PROVIDER_ID]
            if len(google) != 1:
                return {**normalized, "isSuccess": False, "reasonCodes": ["google_drive_provider_missing"]}
            report_reasons = _reason_codes(result.get("reasonCodes"))
            if report_reasons:
                return {**normalized, "isSuccess": False, "reasonCodes": report_reasons}
            provider = google[0]
            if not isinstance(provider.get("isConfigured"), bool) or not isinstance(provider.get("isSignInAvailable"), bool):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            if provider.get("isConfigured") is True and provider.get("isSignInAvailable") is True:
                return {**normalized, "isSuccess": True, "reasonCodes": []}
            provider_reasons = _reason_codes(provider.get("reasonCodes"))
            return {**normalized, "isSuccess": False, "reasonCodes": provider_reasons or ["google_drive_provider_not_ready"]}

        if action == "connections":
            connections = result.get("connections")
            if not isinstance(connections, list) or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("connectionId"), str)
                or not isinstance(item.get("providerId"), str)
                for item in connections
            ) or not isinstance(result.get("capturedAtUtc"), str):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}

        if action in {"graphPrepareBrowse", "graphPrepareAcquire"}:
            required = ("invocationRef", "scopeId", "rootJobId", "graphHash", "expiresUtc")
            if all(isinstance(result.get(key), str) and result.get(key) for key in required):
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
        if action == "graphWatch":
            watch = result.get("watch")
            if (isinstance(result.get("invocationRef"), str)
                    and _validate_published_graph_watch(watch)):
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
            return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
        if action in {"graphSubmitBrowse", "graphSubmitAcquire"}:
            required = ("invocationRef", "scopeId", "rootJobId", "graphHash", "status")
            if not all(isinstance(result.get(key), str) and result.get(key) for key in required[:-1]):
                return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
            execution_status = result.get("status")
            completed = execution_status == "Completed" or type(execution_status) is int and execution_status == 0
            known_terminal = (
                isinstance(execution_status, str)
                and execution_status in {"CompletedWithFailures", "Failed", "Cancelled", "QueueTimedOut"}
            ) or (type(execution_status) is int and execution_status in {1, 2, 3, 4})
            if completed:
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
            if known_terminal:
                reasons = _reason_codes(result.get("reasonCodes")) or ["external_media_graph_not_completed"]
                return {**normalized, "isSuccess": False, "reasonCodes": reasons}
            return {"isSuccess": False, "reasonCodes": ["engine_response_invalid"], "httpStatus": status}
        if action == "graphStatus":
            status_projection = result.get("status")
            if (isinstance(result.get("invocationRef"), str)
                    and _validate_published_graph_status(status_projection)):
                status_value = status_projection.get("status")
                normalized_status = _normalize_graph_status(status_value)
                if normalized_status in _GRAPH_FAILED_STATUSES:
                    return {
                        **normalized,
                        "isSuccess": False,
                        "reasonCodes": _reason_codes(result.get("reasonCodes")) or ["external_media_graph_not_completed"],
                    }
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
        if action == "graphCancel":
            # Some pre-projection local fixtures carry isSuccess/status rather
            # than the published control DTO.  They still must name cancel;
            # published responses additionally require the bounded result
            # envelope and row/tree inventories.
            if (result.get("operation") == "cancel"
                    and ("isSuccess" in result or _validate_published_graph_control(result))):
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}

        return normalized

    def _operation(self, action: str) -> Mapping[str, str]:
        try:
            document = json.loads(self._contracts_path.read_text(encoding="utf-8"))
            operation = document["operations"][action]
            if operation["method"] not in {"GET", "POST"} or not isinstance(operation["path"], str) or not operation["path"].startswith("/"):
                raise ValueError
            return operation
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DriveClientError("engine_contract_unavailable") from exc

    def _has_operation(self, action: str) -> bool:
        try:
            self._operation_for_action(action)
            return True
        except DriveClientError:
            return False

    def _operation_for_action(self, action: str) -> Mapping[str, str]:
        candidates = (action,) + tuple(_ACTION_ALIASES.get(action, ()))
        for candidate in candidates:
            try:
                return self._operation(candidate)
            except DriveClientError:
                continue
        raise DriveClientError("engine_contract_unavailable")

    def _path(self, template: str, action: str, body: Mapping[str, Any] | None = None) -> str:
        if "{" not in template and "}" not in template:
            return template
        if not isinstance(body, Mapping):
            raise DriveClientError("engine_contract_path_unsupported")
        replacements = {
            "connectionId": body.get("connectionId"),
            "artifactId": body.get("__routeArtifactId") or body.get("retainedArtifactId") or body.get("artifactId"),
            "tenantId": body.get("__graphTenantId") or self.config.tenant_id,
            "scopeId": body.get("__graphScopeId"),
            "invocationRef": body.get("__graphInvocationRef"),
        }
        path = template
        for placeholder, value in replacements.items():
            token = "{" + placeholder + "}"
            if token in path:
                if not isinstance(value, str) or not value:
                    raise DriveClientError("engine_contract_path_unsupported")
                path = path.replace(token, urllib.parse.quote(value, safe=""))
        if "{" in path or "}" in path:
            raise DriveClientError("engine_contract_path_unsupported")
        return path

    def _graph_binding_for_payload(self, graph_kind: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Use memory when present, or explicitly recover only an opaque ticket handle."""
        binding = self._graph_bindings.get(graph_kind)
        supplied_scope = payload.get("graphScopeId")
        supplied_invocation = payload.get("graphInvocationRef")
        if supplied_scope is None and supplied_invocation is None:
            return binding
        if not isinstance(supplied_scope, str) or not _valid_graph_scope_id(supplied_scope):
            raise DriveClientError("graph_scope_id_invalid")
        if not isinstance(supplied_invocation, str) or not _valid_graph_invocation_ref(supplied_invocation):
            raise DriveClientError("graph_invocation_ref_invalid")
        if binding is not None and (binding.get("scopeId") != supplied_scope or binding.get("invocationRef") != supplied_invocation):
            raise DriveClientError("graph_binding_mismatch")
        if binding is None:
            binding = {
                "connectionId": None,
                "tenantId": self.config.tenant_id,
                "scopeId": supplied_scope,
                "invocationRef": supplied_invocation,
                "requestBody": {},
                # Recovery is status/watch only until the server reports a
                # terminal observation. It never reconstructs a submit body.
                "submitted": True,
                "statusTerminal": False,
                "recovered": True,
            }
            self._graph_bindings[graph_kind] = binding
        return binding

    def _bound_body(self, action: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise DriveClientError("request_payload_required")
        connection_id = payload.get("connectionId")
        expected_graph_kind = {
            "graphPrepareBrowse": "browse",
            "graphSubmitBrowse": "browse",
            "graphPrepareAcquire": "acquire",
            "graphSubmitAcquire": "acquire",
        }.get(action)
        graph_kind = expected_graph_kind
        if expected_graph_kind is not None and isinstance(payload.get("graphKind"), str) and payload["graphKind"] != expected_graph_kind:
            raise DriveClientError("graph_kind_mismatch")
        if expected_graph_kind is None and isinstance(payload.get("graphKind"), str):
            graph_kind = payload["graphKind"]
        if action in {"graphStatus", "graphWatch", "graphCancel"}:
            if graph_kind not in {"browse", "acquire"}:
                raise DriveClientError("graph_kind_required")
            binding = self._graph_binding_for_payload(graph_kind, payload)
            if not binding:
                raise DriveClientError("graph_not_prepared")
            if action == "graphCancel":
                idempotency_key = payload.get("idempotencyKey")
                if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
                    raise DriveClientError("graph_cancel_idempotency_key_required")
                return {
                    "graphKind": graph_kind,
                    "idempotencyKey": idempotency_key,
                    "__graphTenantId": self.config.tenant_id,
                    "__graphScopeId": binding["scopeId"],
                    "__graphInvocationRef": binding["invocationRef"],
                }
            if action == "graphWatch":
                cursor = payload.get("graphCursor")
                if cursor is None:
                    cursor = binding.get("cursor")
                _validate_graph_cursor(cursor, self.config.tenant_id, binding["scopeId"])
                return {
                    "graphKind": graph_kind,
                    "__graphCursor": cursor,
                    "__graphTenantId": self.config.tenant_id,
                    "__graphScopeId": binding["scopeId"],
                    "__graphInvocationRef": binding["invocationRef"],
                }
            return {
                "graphKind": graph_kind,
                "__graphTenantId": self.config.tenant_id,
                "__graphScopeId": binding["scopeId"],
                "__graphInvocationRef": binding["invocationRef"],
            }
        if action in {"graphSubmitBrowse", "graphSubmitAcquire"}:
            if graph_kind not in {"browse", "acquire"}:
                raise DriveClientError("graph_kind_required")
            binding = self._graph_bindings.get(graph_kind)
            if not binding:
                raise DriveClientError("graph_not_prepared")
            if binding.get("submitted"):
                if not binding.get("statusTerminal"):
                    raise DriveClientError("graph_reconciliation_required")
                # The candidate has no generic retry route.  A new prepare is
                # required after reconciliation, which prevents a blind replay
                # of a durable ticket after an unknown network outcome.
                raise DriveClientError("graph_retry_requires_new_prepare")
            return {
                **dict(binding["requestBody"]),
                "__graphTenantId": self.config.tenant_id,
                "__graphScopeId": binding["scopeId"],
                "__graphInvocationRef": binding["invocationRef"],
            }
        if action in {"graphPrepareBrowse", "graphPrepareAcquire"}:
            kind = "browse" if action == "graphPrepareBrowse" else "acquire"
            existing = self._graph_bindings.get(kind)
            if existing and existing.get("submitted") and not existing.get("statusTerminal"):
                raise DriveClientError("graph_reconciliation_required")
            base_action = "browse" if graph_kind == "browse" else "acquire"
            body = self._bound_body(base_action, payload)
            return {**body, "__graphKind": graph_kind}
        if graph_kind is not None:
            raise DriveClientError("graph_action_unsupported")

        retained_artifact_id = payload.get("retainedArtifactId")
        explicit_artifact_id = payload.get("artifactId") if retained_artifact_id is None else None
        artifact_id = retained_artifact_id or explicit_artifact_id
        if action in {"inspect", "verifiedRead", "verify", "materialize", "release", "delete"}:
            if not isinstance(artifact_id, str) or not _valid_opaque_artifact_id(artifact_id):
                raise DriveClientError("artifact_id_invalid")
            # Explicit artifactId is the reload path and is authorized by the
            # Engine owner scope. Only an in-process retainedArtifactId needs a
            # selected connection binding in this diagnostic client.
            if retained_artifact_id is not None:
                if not isinstance(connection_id, str) or connection_id not in self._connection_ids:
                    raise DriveClientError("unknown_connection_reference")
                if artifact_id not in self._artifact_ids.get(connection_id, set()):
                    raise DriveClientError("unknown_artifact_reference")
            body = {"__routeArtifactId": artifact_id}
            if action in {"verify", "materialize", "release", "delete"}:
                purpose = payload.get("purpose")
                if not isinstance(purpose, str) or not purpose or len(purpose) > 128:
                    raise DriveClientError("artifact_access_purpose_invalid")
                body["purpose"] = purpose
            if action == "materialize":
                output_path = payload.get("relativeOutputPath")
                if not isinstance(output_path, str) or not output_path or len(output_path) > 4096:
                    raise DriveClientError("materialize_output_path_invalid")
                body["relativeOutputPath"] = output_path
            if action in {"release", "delete"}:
                key = payload.get("idempotencyKey")
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise DriveClientError("idempotency_key_invalid")
                body["idempotencyKey"] = key
            for key in ("sessionId", "correlationId", "traceparent"):
                value = payload.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value or len(value) > 256:
                        raise DriveClientError("artifact_access_context_invalid")
                    body[key] = value
            return body

        if not isinstance(connection_id, str) or connection_id not in self._connection_ids:
            raise DriveClientError("unknown_connection_reference")
        body: dict[str, Any] = {"connectionId": connection_id, "ownerScope": self.config.owner_scope}
        if action == "collections":
            self._last_collection_connection = connection_id
            return body
        if action == "browse":
            # The UI may use friendly labels, but the wire contract is the
            # provider-neutral CloudBrowseRequest nested under Location.
            location_kind = payload.get("locationKind")
            if location_kind is None:
                view = payload.get("view", "my-drive")
                location_kind = {
                    "my-drive": "my_drive_root",
                    "shared-with-me": "shared_with_me",
                    "shared-drives": "shared_drives",
                    "shared-drive-folder": "folder",
                }.get(view)
            if not isinstance(location_kind, str) or location_kind not in {
                "my_drive_root", "shared_with_me", "shared_drives", "shared_drive", "folder"
            }:
                raise DriveClientError("browse_location_invalid")
            location: dict[str, Any] = {"locationKind": location_kind}
            for key in ("locationRef", "driveId"):
                value = payload.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value or len(value) > 4096:
                        raise DriveClientError("browse_location_invalid")
                    location[key] = value
            page_size = payload.get("pageSize")
            if page_size is not None:
                try:
                    page_size = int(page_size)
                except (TypeError, ValueError) as exc:
                    raise DriveClientError("browse_page_size_invalid") from exc
                if not 1 <= page_size <= 1000:
                    raise DriveClientError("browse_page_size_invalid")
                location["pageSize"] = page_size
            continuation = payload.get("continuationToken")
            if continuation is not None:
                if not isinstance(continuation, str) or not continuation:
                    raise DriveClientError("continuation_token_invalid")
                location["continuationToken"] = continuation
            if location_kind in {"folder", "shared_drive"} and not location.get("locationRef"):
                raise DriveClientError("browse_location_reference_required")
            body = {"connectionId": connection_id, "location": location}
            return body
        if action == "revoke":
            return {"connectionId": connection_id}
        if action == "acquire":
            selection_ref = payload.get("selectionRef") or payload.get("opaqueSelectionRef")
            # Keep the legacy fixture alias input-compatible, but never emit
            # the provider-facing asset id on the canonical wire contract.
            if selection_ref is None:
                selection_ref = payload.get("assetId")
            if not isinstance(selection_ref, str) or selection_ref not in (
                self._selection_refs.get(connection_id, set()) | self._asset_ids.get(connection_id, set())
            ):
                raise DriveClientError("unknown_asset_reference" if payload.get("assetId") is not None else "unknown_selection_reference")
            maximum_bytes = payload.get("maximumBytes")
            if maximum_bytes is None:
                raise DriveClientError("maximum_bytes_required")
            try:
                maximum_bytes = int(maximum_bytes)
            except (TypeError, ValueError) as exc:
                raise DriveClientError("maximum_bytes_invalid") from exc
            if maximum_bytes <= 0:
                raise DriveClientError("maximum_bytes_invalid")
            maximum_duration = payload.get("maximumDurationSeconds", 3600)
            try:
                maximum_duration = int(maximum_duration)
            except (TypeError, ValueError) as exc:
                raise DriveClientError("maximum_duration_invalid") from exc
            if not 1 <= maximum_duration <= 3600:
                raise DriveClientError("maximum_duration_invalid")
            artifact_id = payload.get("artifactId")
            if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 160:
                raise DriveClientError("artifact_id_required")
            publication = payload.get("publication")
            if not isinstance(publication, Mapping):
                raise DriveClientError("publication_required")
            body = {
                "connectionId": connection_id,
                "selectionRef": selection_ref,
                "maximumBytes": maximum_bytes,
                "artifactId": {"value": artifact_id},
                "publication": _validated_publication(publication),
                "maximumDurationSeconds": maximum_duration,
            }
            return body
        collection_ref = payload.get("collectionRef")
        if not isinstance(collection_ref, str) or collection_ref not in self._collection_ids.get(connection_id, set()):
            raise DriveClientError("unknown_collection_reference")
        body["collectionRef"] = collection_ref
        return body

    def _request(self, method: str, path: str, *, query: Mapping[str, str], body: Mapping[str, Any] | None,
                 callback_text: bool = False, expect_binary: bool = False) -> dict[str, Any]:
        base = self.config.base_url.rstrip("/")
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        parsed = urllib.parse.urlsplit(url)
        connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=self.timeout)
            if parsed.scheme == "https":
                connection.connect()
                peer_certificate = connection.sock.getpeercert(binary_form=True) if connection.sock is not None else None
                if not peer_certificate:
                    raise DriveClientError("engine_certificate_pin_unavailable")
                expected_pin = (self.config.certificate_sha256 or "").lower()
                if expected_pin and hashlib.sha256(peer_certificate).hexdigest() != expected_pin:
                    raise DriveClientError("engine_certificate_pin_mismatch")
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            headers = {
                "Accept": "text/plain" if callback_text else "application/json",
                "Authorization": f"Bearer {self.config.bearer_token}",
                _CONSUMER_HEADER: self.config.consumer_id,
            }
            if encoded is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, target, body=encoded, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                return {"isSuccess": False, "reasonCodes": ["engine_redirect_forbidden"]}
            if expect_binary:
                if response.status >= 400:
                    return {"isSuccess": False, "reasonCodes": ["engine_http_error"], "httpStatus": response.status}
                return self._read_verified_response(connection, response)
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(data) > _MAX_RESPONSE_BYTES:
                return {"isSuccess": False, "reasonCodes": ["engine_response_too_large"]}
            if response.status >= 400:
                return _project_http_error(data, response.status)
            if callback_text:
                # The published callback returns fixed plain text. Never relay
                # provider/Engine response bodies into the browser or logs.
                valid = (response.status == 200
                         and response.getheader("Content-Type", "").split(";", 1)[0] == "text/plain"
                         and data == b"Cloud sign-in complete\n\nYou can return to the app.\n")
                return {"isSuccess": valid}
            return self._parse_response(data, response.status)
        except DriveClientError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise DriveClientError("engine_unreachable") from exc
        finally:
            if connection is not None:
                connection.close()

    def _read_verified_response(self, connection: http.client.HTTPConnection | http.client.HTTPSConnection,
                                response: Any) -> dict[str, Any]:
        content_type = response.getheader("Content-Type", "").split(";", 1)[0]
        if response.status != 200 or content_type != "application/octet-stream":
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_refused"]}
        raw_length = response.getheader("Content-Length")
        if not isinstance(raw_length, str) or not re.fullmatch(r"[0-9]+", raw_length.strip()):
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_content_length_required"]}
        declared_length = int(raw_length.strip())
        if declared_length > _MAX_VERIFIED_READ_BYTES:
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_size_invalid"]}

        deadline = time.monotonic() + _VERIFIED_READ_DEADLINE_SECONDS
        digest = hashlib.sha256()
        remaining = declared_length
        count = 0
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                return {"isSuccess": False, "reasonCodes": ["engine_verified_read_timeout"]}
            socket = getattr(connection, "sock", None)
            if socket is not None and hasattr(socket, "settimeout"):
                socket.settimeout(min(timeout, self.timeout))
            try:
                chunk = response.read(min(_VERIFIED_READ_CHUNK_BYTES, remaining + 1))
            except TimeoutError:
                return {"isSuccess": False, "reasonCodes": ["engine_verified_read_timeout"]}
            if not isinstance(chunk, bytes):
                return {"isSuccess": False, "reasonCodes": ["engine_verified_read_invalid_body"]}
            if not chunk:
                return {"isSuccess": False, "reasonCodes": ["engine_verified_read_incomplete"]}
            if len(chunk) > remaining:
                return {"isSuccess": False, "reasonCodes": ["engine_verified_read_overrun"]}
            digest.update(chunk)
            count += len(chunk)
            remaining -= len(chunk)

        # A compliant HTTPResponse honors Content-Length, while a test or
        # alternate transport may expose an extra byte.  Reject either case.
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_timeout"]}
        socket = getattr(connection, "sock", None)
        if socket is not None and hasattr(socket, "settimeout"):
            socket.settimeout(min(timeout, self.timeout))
        try:
            extra = response.read(1)
        except TimeoutError:
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_timeout"]}
        if not isinstance(extra, bytes):
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_invalid_body"]}
        if extra:
            return {"isSuccess": False, "reasonCodes": ["engine_verified_read_overrun"]}
        # The diagnostic harness proves the bounded read without returning
        # patient bytes to the browser or storing them.
        return {"isSuccess": True, "verifiedBytesRead": count, "contentSha256": digest.hexdigest()}

    @staticmethod
    def _parse_response(data: bytes, status: int) -> dict[str, Any]:
        if not data:
            return {"isSuccess": False, "reasonCodes": ["engine_empty_response"], "httpStatus": status}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"isSuccess": False, "reasonCodes": ["engine_invalid_response"], "httpStatus": status}
        if not isinstance(parsed, dict):
            return {"isSuccess": False, "reasonCodes": ["engine_invalid_response"], "httpStatus": status}
        parsed["httpStatus"] = status
        return parsed

    @staticmethod
    def _sanitize(action: str, result: Mapping[str, Any], *, selected_connection: str | None = None) -> dict[str, Any]:
        output: dict[str, Any] = {
            "action": action,
            "isSuccess": result.get("isSuccess") is True,
            "reasonCodes": _failure_reason_codes(result),
        }
        for key in _SAFE_STATUS_KEYS - {"isSuccess", "reasonCodes"}:
            if key in result and isinstance(result[key], (str, int, float, bool)):
                output[key] = result[key]
        if action == "start" and isinstance(result.get("authorizationUrl"), str):
            output["authorizationUrl"] = result["authorizationUrl"]
        for key in ("connections", "collections", "assets"):
            if isinstance(result.get(key), list):
                records = []
                for item in result[key]:
                    if not isinstance(item, Mapping):
                        continue
                    provider_id = item.get("providerId")
                    connection_id = item.get("connectionId")
                    if provider_id is not None and provider_id != _PROVIDER_ID:
                        continue
                    if key != "connections" and selected_connection is not None and connection_id is not None and connection_id != selected_connection:
                        continue
                    records.append(_safe_record(item))
                output[key] = records
        if isinstance(result.get("providers"), list):
            output["providers"] = [_safe_record(item) for item in result["providers"] if isinstance(item, Mapping)]
        for key in ("nextPageToken", "changeCursor", "continuationToken"):
            if isinstance(result.get(key), str) and len(result[key]) <= 4096:
                output[key] = result[key]
        for key in ("receiptId", "operationRef", "retainedArtifactId", "artifactId", "graphInvocationRef", "hasMore", "incompleteSearch"):
            if key in result and type(result[key]) in (str, bool):
                output[key] = result[key]
        verified_bytes = result.get("verifiedBytesRead")
        if type(verified_bytes) is int and verified_bytes >= 0:
            output["verifiedBytesRead"] = verified_bytes
        content_hash = result.get("contentSha256")
        if isinstance(content_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", content_hash):
            output["contentSha256"] = content_hash.lower()
        for key in ("receipts", "artifacts", "items"):
            if isinstance(result.get(key), list):
                output[key] = [_safe_record(item) for item in result[key] if isinstance(item, Mapping)]
        if isinstance(result.get("publication"), Mapping):
            output["publication"] = _safe_record(result["publication"])
        if isinstance(result.get("connection"), Mapping):
            output["connection"] = _safe_record(result["connection"])
        if action in {"graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch", "graphCancel"}:
            _append_safe_graph_fields(output, result)
        return output

    def _remember_connections(self, result: Mapping[str, Any]) -> None:
        refreshed: set[str] = set()
        for item in result.get("connections", []):
            identifier = item.get("connectionId") if isinstance(item, Mapping) else None
            if isinstance(identifier, str) and item.get("providerId") == _PROVIDER_ID:
                refreshed.add(identifier)
        self._connection_ids = refreshed
        self._clear_collection_bindings()

    def _clear_connection_bindings(self) -> None:
        self._connection_ids.clear()
        self._clear_collection_bindings()

    def _clear_collection_bindings(self) -> None:
        # A refresh is an account/binding boundary even when a provider reuses
        # the same connection ID.  Never let collection refs cross that boundary.
        self._collection_ids = {}
        self._asset_ids = {}
        self._selection_refs = {}
        self._artifact_ids = {}
        self._last_collection_connection = None
        self._graph_bindings = {}

    def _remember_graph_prepare(self, action: str, result: Mapping[str, Any], body: Mapping[str, Any] | None) -> None:
        kind = "browse" if action == "graphPrepareBrowse" else "acquire"
        invocation = result.get("invocationRef")
        scope = result.get("scopeId")
        if not isinstance(invocation, str) or not isinstance(scope, str) or not invocation or not scope:
            return
        if not isinstance(body, Mapping):
            return
        request_body = {key: value for key, value in body.items() if not key.startswith("__graph")}
        self._graph_bindings[kind] = {
            "connectionId": request_body.get("connectionId"),
            "tenantId": self.config.tenant_id,
            "scopeId": scope,
            "invocationRef": invocation,
            "requestBody": request_body,
            "submitted": False,
            "statusTerminal": False,
        }

    def _project_graph_result(self, action: str, raw: Mapping[str, Any], output: dict[str, Any], *, graph_kind: str | None = None) -> dict[str, Any]:
        """Project only output bound to the prepared graph's connection.

        Graph responses contain an intentionally broad JsonElement/status tree. The
        diagnostic app receives only provider-neutral browse records or the
        retained artifact identity, never that tree or provider metadata.
        """
        kind = graph_kind if graph_kind in {"browse", "acquire"} else ("browse" if "Browse" in action else "acquire")
        invocation = raw.get("invocationRef") or raw.get("graphInvocationRef")
        if action == "graphStatus" and isinstance(invocation, str):
            for candidate_kind, candidate in self._graph_bindings.items():
                if candidate.get("invocationRef") == invocation:
                    kind, binding = candidate_kind, candidate
                    break
            else:
                binding = None
        else:
            binding = self._graph_bindings.get(kind)
        if binding is None:
            if action in {"graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch"}:
                return self._graph_binding_failure(output)
            return output
        # A transport-level Engine failure has no invocation binding to echo.
        # Preserve its bounded reason/status instead of misdiagnosing the
        # failed request as a provider/tenant mismatch.  The ticket remains
        # unverified so the caller still has to reconcile before retrying.
        http_status = raw.get("httpStatus")
        if (
            action in {"graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch"}
            and output.get("isSuccess") is not True
            and type(http_status) is int
            and 400 <= http_status <= 599
            and not invocation
        ):
            for key in ("retainedArtifact", "retainedArtifactId", "retainedArtifacts"):
                output.pop(key, None)
            output["_graphBindingVerified"] = False
            return output
        if action in {"graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphWatch"} and (
                not isinstance(invocation, str) or invocation != binding.get("invocationRef")):
            return self._graph_binding_failure(output)
        if isinstance(invocation, str) and invocation and invocation != binding.get("invocationRef"):
            return self._graph_binding_failure(output)
        if action == "graphCancel":
            http_status = raw.get("httpStatus")
            published = type(http_status) is int and 200 <= http_status < 300 and "isSuccess" not in raw
            if raw.get("operation") != "cancel" or (published and not _validate_published_graph_control(raw)):
                output["isSuccess"] = False
                output["reasonCodes"] = ["engine_response_invalid"]
                output["_graphBindingVerified"] = False
                return output
            output["_graphBindingVerified"] = True
            return output
        if action == "graphStatus":
            verified = self._project_graph_status(raw, output, binding)
            output["_graphBindingVerified"] = verified
            if not verified:
                output["isSuccess"] = False
                output["reasonCodes"] = ["graph_binding_mismatch"]
            return output
        if action == "graphWatch":
            watch = raw.get("watch")
            if not isinstance(watch, Mapping):
                return self._graph_binding_failure(output)
            cursor = watch.get("cursor")
            next_cursor = watch.get("nextCursor")
            try:
                _validate_graph_cursor(cursor, self.config.tenant_id, binding.get("scopeId"))
                _validate_graph_cursor(next_cursor, self.config.tenant_id, binding.get("scopeId"))
            except DriveClientError:
                return self._graph_binding_failure(output)
            changed_jobs = watch.get("changedJobs")
            if not _validate_published_graph_watch(watch):
                return self._graph_binding_failure(output)
            output["graphChangedJobCount"] = min(len(changed_jobs), 4096)
            output["graphStatuses"] = _graph_status_values(watch.get("projection"))[:32]
            status_projection = {
                "status": {"projection": watch.get("projection"), "nextCursor": next_cursor},
            }
            verified = self._project_graph_status(status_projection, output, binding)
            output["_graphBindingVerified"] = verified
            if not verified:
                return self._graph_binding_failure(output)
            output["graphNextCursor"] = next_cursor
            return output
        output["_graphBindingVerified"] = True
        if action not in {"graphSubmitBrowse", "graphSubmitAcquire"} or raw.get("isSuccess") is not True:
            return output
        graph_output = raw.get("output")
        if not isinstance(graph_output, Mapping):
            return output
        if kind == "browse":
            if graph_output.get("isSuccess") is not True:
                return output
            connection_id = binding.get("connectionId")
            if not isinstance(connection_id, str) or graph_output.get("connectionId") != connection_id:
                return output
            page = graph_output.get("result")
            if not isinstance(page, Mapping) or page.get("isSuccess") is not True:
                return output
            if (isinstance(page.get("connectionId"), str)
                    and page.get("connectionId") != connection_id):
                return self._graph_binding_failure(output)
            if not isinstance(page.get("collections"), list) or not isinstance(page.get("assets"), list):
                return self._graph_binding_failure(output)
            collections = [_safe_record(item) for item in page["collections"] if isinstance(item, Mapping)]
            assets = [_safe_record(item) for item in page["assets"] if isinstance(item, Mapping)]
            output["graphOutput"] = {"kind": "browse", "connectionId": connection_id,
                                     "collections": collections, "assets": assets}
            output["collections"] = collections
            output["assets"] = assets
            cursor = page.get("continuationToken")
            if isinstance(cursor, str) and cursor and len(cursor) <= 4096:
                output["continuationToken"] = cursor
            return output
        artifact_id = graph_output.get("artifactId")
        if isinstance(artifact_id, str) and artifact_id and len(artifact_id) <= 160:
            output["retainedArtifact"] = {"artifactId": artifact_id}
            output["retainedArtifactId"] = artifact_id
            binding["retainedArtifactId"] = artifact_id
            connection_id = binding.get("connectionId")
            if isinstance(connection_id, str):
                self._artifact_ids.setdefault(connection_id, set()).add(artifact_id)
        return output

    @staticmethod
    def _graph_binding_failure(output: dict[str, Any]) -> dict[str, Any]:
        # A provider/tenant/invocation mismatch is an unsafe response, even if
        # a proxy optimistically marked it successful.  Do not let its opaque
        # artifact identity reach the UI or settle the local graph ticket.
        for key in ("retainedArtifact", "retainedArtifactId", "retainedArtifacts"):
            output.pop(key, None)
        output["isSuccess"] = False
        output["reasonCodes"] = ["graph_binding_mismatch"]
        output["_graphBindingVerified"] = False
        return output

    def _project_graph_status(self, raw: Mapping[str, Any], output: dict[str, Any], binding: Mapping[str, Any]) -> bool:
        """Recover only the bound retained-handle artifact from a status snapshot."""
        status = raw.get("status")
        if not isinstance(status, Mapping):
            return False
        status_value = _normalize_graph_status(status.get("status"))
        cursor = status.get("nextCursor")
        scope = cursor.get("scope") if isinstance(cursor, Mapping) else None
        if not isinstance(scope, Mapping):
            return False
        if scope.get("tenantId") != self.config.tenant_id or scope.get("scopeId") != binding.get("scopeId"):
            return False
        try:
            _validate_graph_cursor(cursor, self.config.tenant_id, binding.get("scopeId"))
        except DriveClientError:
            return False
        projection = status.get("projection")
        if not _validate_published_graph_projection(projection, cursor):
            return False
        # Cursor advancement is a commit point.  Every scope, inventory, and
        # artifact check below must succeed before the durable local binding is
        # allowed to move past its previous observation boundary.
        def commit_cursor() -> None:
            if isinstance(binding, dict):
                binding["cursor"] = cursor
        # The cursor is the primary continuation boundary, but a projected job
        # snapshot also carries its tenant/scope envelope.  Reject a snapshot
        # whose projection crosses that boundary before considering artifacts.
        if (projection.get("tenantId") != self.config.tenant_id
                or projection.get("scopeId") != binding.get("scopeId")):
            return False
        if status_value in _GRAPH_FAILED_STATUSES:
            for key in ("retainedArtifact", "retainedArtifactId", "retainedArtifacts"):
                output.pop(key, None)
            output["isSuccess"] = False
            output["reasonCodes"] = ["external_media_graph_not_completed"]
            # The response is bound to this ticket; the failed terminal state
            # may settle reconciliation only after all scope checks pass.
            commit_cursor()
            return True
        if status_value and status_value not in _GRAPH_COMPLETED_STATUSES | _GRAPH_ACTIVE_STATUSES:
            output["isSuccess"] = False
            output["reasonCodes"] = ["engine_response_invalid"]
            return False
        job_states = []
        for job in projection["jobs"][:256]:
            if not isinstance(job, Mapping):
                continue
            state = job.get("currentState")
            if state is None:
                state = job.get("state")
            if state is None:
                state = job.get("status")
            normalized_state = _normalize_graph_status(state)
            if normalized_state:
                job_states.append(normalized_state)
        if any(state in _GRAPH_FAILED_STATUSES for state in job_states):
            for key in ("retainedArtifact", "retainedArtifactId", "retainedArtifacts"):
                output.pop(key, None)
            output["isSuccess"] = False
            output["reasonCodes"] = ["external_media_graph_not_completed"]
            commit_cursor()
            return True
        if status_value in _GRAPH_ACTIVE_STATUSES or (
                not status_value and any(state in _GRAPH_ACTIVE_STATUSES for state in job_states)):
            # The status request itself succeeded, but an active job is not a
            # valid source of a retained artifact yet.
            commit_cursor()
            return True
        if job_states and not all(state in _GRAPH_COMPLETED_STATUSES for state in job_states):
            output["isSuccess"] = False
            output["reasonCodes"] = ["engine_response_invalid"]
            return False
        recovered: list[dict[str, Any]] = []
        for job in projection["jobs"][:256]:
            if not isinstance(job, Mapping) or not isinstance(job.get("artifacts"), list):
                continue
            for artifact in job["artifacts"][:256]:
                if not isinstance(artifact, Mapping) or artifact.get("payloadSchemaId") != "external-media.retained-handle.v1":
                    continue
                artifact_id = artifact.get("artifactId")
                content_hash = artifact.get("contentHash")
                byte_size = artifact.get("byteSize")
                if (not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 160
                        or not isinstance(content_hash, str) or not content_hash or len(content_hash) > 512
                        or type(byte_size) is not int or byte_size < 0):
                    continue
                recovered.append({"artifactId": artifact_id, "contentHash": content_hash, "byteSize": byte_size})
        if not recovered:
            commit_cursor()
            return True
        output["retainedArtifacts"] = recovered[:32]
        output["retainedArtifact"] = recovered[0]
        output["retainedArtifactId"] = recovered[0]["artifactId"]
        connection_id = binding.get("connectionId")
        if isinstance(connection_id, str):
            self._artifact_ids.setdefault(connection_id, set()).update(item["artifactId"] for item in recovered[:32])
            binding["retainedArtifactId"] = recovered[0]["artifactId"]
        commit_cursor()
        return True

    def _remember_graph_submit(self, action: str, result: Mapping[str, Any]) -> None:
        if result.get("_graphBindingVerified") is False:
            return
        kind = "browse" if action == "graphSubmitBrowse" else "acquire"
        binding = self._graph_bindings.get(kind)
        if binding is not None:
            # A response or transport failure both consume the one-shot submit
            # attempt.  Status reconciliation is required before preparing a
            # fresh explicit retry.
            binding["submitted"] = True
            binding["statusTerminal"] = _graph_result_terminal(result)

    def _remember_graph_status(self, result: Mapping[str, Any]) -> None:
        if result.get("_graphBindingVerified") is False:
            return
        invocation = result.get("graphInvocationRef") or result.get("invocationRef")
        for binding in self._graph_bindings.values():
            if isinstance(invocation, str) and invocation and binding.get("invocationRef") != invocation:
                continue
            if binding.get("submitted"):
                binding["statusTerminal"] = _graph_result_terminal(result)

    def _remember_collections(self, result: Mapping[str, Any], connection_id: str | None = None) -> None:
        if connection_id is None:
            connection_id = result.get("connectionId")
        if not isinstance(connection_id, str):
            return
        refs = self._collection_ids.setdefault(connection_id, set())
        for item in result.get("collections", []):
            if isinstance(item, Mapping):
                ref = item.get("collectionRef") or item.get("collectionId") or item.get("id")
                if isinstance(ref, str):
                    refs.add(ref)

    def _remember_assets(self, result: Mapping[str, Any], connection_id: str | None, *,
                         allow_cloud_selection_asset_id: bool = False) -> None:
        if not isinstance(connection_id, str):
            return
        refs = self._asset_ids.setdefault(connection_id, set())
        selections = self._selection_refs.setdefault(connection_id, set())
        for item in result.get("assets", []) if isinstance(result.get("assets"), list) else result.get("items", []):
            if isinstance(item, Mapping):
                ref = item.get("assetId")
                if isinstance(ref, str):
                    refs.add(ref)
                selection = item.get("selectionRef") or item.get("opaqueSelectionRef")
                if isinstance(selection, str):
                    selections.add(selection)
                elif allow_cloud_selection_asset_id and isinstance(ref, str) and _valid_cloud_selection_ref(ref):
                    # The graph browse projection intentionally exposes only
                    # the opaque cloud-selection assetId. Treating that
                    # Engine-issued handle as a selection ref is safe only for
                    # this successful, connection-bound graph response; do
                    # not infer it from ordinary provider browse records.
                    selections.add(ref)

    def _remember_artifacts(self, result: Mapping[str, Any], connection_id: str | None) -> None:
        if not isinstance(connection_id, str):
            return
        refs = self._artifact_ids.setdefault(connection_id, set())
        for key in ("retainedArtifactId", "artifactId"):
            value = result.get(key)
            if isinstance(value, str):
                refs.add(value)
        for item in result.get("artifacts", []) if isinstance(result.get("artifacts"), list) else []:
            if isinstance(item, Mapping):
                value = item.get("retainedArtifactId") or item.get("artifactId")
                if isinstance(value, str):
                    refs.add(value)
        publication = result.get("publication")
        if isinstance(publication, Mapping):
            value = publication.get("artifactId")
            if isinstance(value, Mapping):
                value = value.get("value")
            if isinstance(value, str):
                refs.add(value)

    @staticmethod
    def _validate_authorization_url(value: Any) -> None:
        if not isinstance(value, str):
            raise DriveClientError("engine_authorization_url_invalid")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "accounts.google.com" or parsed.username or parsed.password or parsed.fragment:
            raise DriveClientError("engine_authorization_url_invalid")
        if parsed.port not in (None, 443):
            raise DriveClientError("engine_authorization_url_invalid")


def _reason_codes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and len(item) <= 128][:32]
    if isinstance(value, str) and value:
        return [value[:128]]
    return []


def _failure_reason_codes(result: Mapping[str, Any]) -> list[str]:
    """Project an operation envelope's published failure reason without its body.

    Cloud operation routes may return a successful HTTP response whose outer
    envelope is unsuccessful while the published operation DTO carries the
    actionable reason under ``result``.  Read only that bounded reason-code
    field; never copy the nested operation payload into the UI projection.
    """
    reason_codes = _reason_codes(result.get("reasonCodes"))
    if reason_codes or result.get("isSuccess") is not False:
        return reason_codes
    nested = result.get("result")
    if isinstance(nested, Mapping) and nested.get("isSuccess") is False:
        return _reason_codes(nested.get("reasonCodes"))
    return []


def _valid_opaque_artifact_id(value: str) -> bool:
    """Keep an explicitly re-entered artifact ref path-safe and bounded."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value))


def _canonical_artifact_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and set(value) == {"value"} and isinstance(value.get("value"), str):
        return value["value"]
    return None


def _valid_cloud_selection_ref(value: str) -> bool:
    """Recognize only bounded Engine-issued cloud-selection handles."""
    return bool(re.fullmatch(r"cloud-selection:[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value))


def _valid_graph_scope_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value))


def _valid_graph_invocation_ref(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _validate_graph_cursor(value: Any, tenant_id: str, scope_id: Any, *, require_jobs: bool = True) -> None:
    """Validate only the bounded route binding before Engine validates full DTO semantics."""
    if not isinstance(value, Mapping) or not isinstance(scope_id, str):
        raise DriveClientError("graph_cursor_required")
    try:
        serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DriveClientError("graph_cursor_invalid") from exc
    if len(serialized) > _MAX_RESPONSE_BYTES:
        raise DriveClientError("graph_cursor_too_large")
    cursor_scope = value.get("scope")
    if (not isinstance(cursor_scope, Mapping)
            or cursor_scope.get("tenantId") != tenant_id
            or cursor_scope.get("scopeId") != scope_id):
        raise DriveClientError("graph_cursor_scope_mismatch")
    if not require_jobs and "contractVersion" not in value and "jobCursors" not in value:
        return
    if value.get("contractVersion") != 1:
        raise DriveClientError("graph_cursor_invalid")
    job_cursors = value.get("jobCursors")
    if not isinstance(job_cursors, list) or not 1 <= len(job_cursors) <= 4096:
        raise DriveClientError("graph_cursor_invalid")
    seen: set[str] = set()
    for item in job_cursors:
        if not isinstance(item, Mapping) or item.get("schemaVersion") != 1:
            raise DriveClientError("graph_cursor_invalid")
        job = item.get("job")
        job_id = job.get("jobId") if isinstance(job, Mapping) else None
        sequence = item.get("afterSequence")
        if (not isinstance(job_id, str) or not job_id or len(job_id) > 256
                or job_id in seen or type(sequence) is not int or sequence < 0):
            raise DriveClientError("graph_cursor_invalid")
        seen.add(job_id)


def _validate_published_graph_cursor(value: Any) -> bool:
    """Check the structural cursor guarantees needed before projecting a DTO.

    The Engine remains authoritative for canonical job references and the full
    observation scope.  The app nevertheless must reject an envelope that is
    missing the versioned cursor inventory, rather than treating a scope-only
    cursor as a successful status/watch response.
    """
    if not isinstance(value, Mapping):
        return False
    scope = value.get("scope")
    if not isinstance(scope, Mapping):
        return False
    tenant_id, scope_id = scope.get("tenantId"), scope.get("scopeId")
    if not isinstance(tenant_id, str) or not isinstance(scope_id, str):
        return False
    try:
        _validate_graph_cursor(value, tenant_id, scope_id)
    except DriveClientError:
        return False
    return True


def _validate_published_graph_projection(value: Any, cursor: Mapping[str, Any]) -> bool:
    """Validate the bounded projection/cursor inventory join.

    This mirrors the consumer-visible part of
    ``DistributedAgentJobProjection.Validate``.  Canonical job-reference
    validation stays in Engine; we only require the fields needed to prevent a
    cross-scope or cursor-inventory projection from advancing local state.
    """
    if not isinstance(value, Mapping) or value.get("contractVersion") != 1 or value.get("advisoryOnly") is not True:
        return False
    scope = cursor.get("scope") if isinstance(cursor, Mapping) else None
    if not isinstance(scope, Mapping):
        return False
    if value.get("tenantId") != scope.get("tenantId") or value.get("scopeId") != scope.get("scopeId"):
        return False
    jobs = value.get("jobs")
    cursors = cursor.get("jobCursors")
    if not isinstance(jobs, list) or not isinstance(cursors, list) or len(jobs) != len(cursors):
        return False
    for row, item in zip(jobs, cursors):
        if not isinstance(row, Mapping) or row.get("contractVersion") != 1:
            return False
        job = row.get("job")
        cursor_job = item.get("job") if isinstance(item, Mapping) else None
        if not isinstance(job, Mapping) or not isinstance(cursor_job, Mapping) or job.get("jobId") != cursor_job.get("jobId"):
            return False
    return True


def _validate_published_graph_status(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    cursor = value.get("nextCursor")
    if not _validate_published_graph_cursor(cursor):
        return False
    return _validate_published_graph_projection(value.get("projection"), cursor)


def _validate_published_graph_watch(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    cursor, next_cursor = value.get("cursor"), value.get("nextCursor")
    if not _validate_published_graph_cursor(cursor) or not _validate_published_graph_cursor(next_cursor):
        return False
    cursor_scope = cursor.get("scope")
    next_scope = next_cursor.get("scope")
    if cursor_scope != next_scope:
        return False
    previous = cursor.get("jobCursors")
    following = next_cursor.get("jobCursors")
    changed = value.get("changedJobs")
    if not isinstance(previous, list) or not isinstance(following, list) or not isinstance(changed, list):
        return False
    if len(previous) != len(following):
        return False
    expected: dict[str, Mapping[str, Any]] = {}
    for before, after in zip(previous, following):
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return False
        before_job = before.get("job")
        after_job = after.get("job")
        if (not isinstance(before_job, Mapping) or not isinstance(after_job, Mapping)
                or before_job.get("jobId") != after_job.get("jobId")
                or type(before.get("afterSequence")) is not int
                or type(after.get("afterSequence")) is not int
                or after["afterSequence"] < before["afterSequence"]):
            return False
        if after["afterSequence"] > before["afterSequence"]:
            expected[after_job["jobId"]] = after_job
    if len(changed) != len(expected):
        return False
    seen: set[str] = set()
    for item in changed:
        if not isinstance(item, Mapping) or not isinstance(item.get("jobId"), str):
            return False
        job_id = item["jobId"]
        if job_id in seen or job_id not in expected or dict(item) != dict(expected[job_id]):
            return False
        seen.add(job_id)
    return _validate_published_graph_projection(value.get("projection"), next_cursor)


def _validate_published_graph_control(value: Mapping[str, Any]) -> bool:
    """Validate the safe, published shape of GraphJobExecutionControlResult."""
    if value.get("schemaVersion") != 1 or value.get("operation") != "cancel":
        return False
    tree, rows = value.get("tree"), value.get("rows")
    if not isinstance(tree, Mapping) or not isinstance(rows, list):
        return False
    if tree.get("schemaVersion") != 1 or not isinstance(tree.get("rootJob"), Mapping):
        return False
    bindings = tree.get("bindings")
    if not isinstance(bindings, list) or not bindings or len(rows) != len(bindings):
        return False
    if any(not isinstance(binding, Mapping) for binding in bindings):
        return False
    # Row identity is checked at the boundary without copying the opaque
    # lineage details into the app projection.
    binding_ids = {
        binding.get("job", {}).get("jobId")
        for binding in bindings
        if isinstance(binding.get("job"), Mapping)
    }
    row_ids = {
        row.get("binding", {}).get("job", {}).get("jobId")
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("binding"), Mapping)
        and isinstance(row["binding"].get("job"), Mapping)
    }
    return len(binding_ids) == len(bindings) and row_ids == binding_ids


def _validated_publication(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the caller-owned retained-publication metadata.

    Tenant, actor, storage and provider fields are intentionally not accepted.
    The Engine remains the authority for those values.  Record-struct IDs are
    sent in their canonical ``{value: ...}`` wire form used by the SDK.
    """
    required = {
        "fileName", "role", "kind", "classification", "subjectIds",
        "allowedPurposes", "consentRequiredPurposes", "retentionPolicyId",
        "retentionClass", "idempotencyKey",
    }
    optional = {"sessionId", "correlationId", "traceparent"}
    if set(value) - required - optional or not required.issubset(value):
        raise DriveClientError("publication_metadata_invalid")

    def bounded_string(key: str, maximum: int = 256) -> str:
        candidate = value.get(key)
        if not isinstance(candidate, str) or not candidate or len(candidate) > maximum:
            raise DriveClientError("publication_metadata_invalid")
        return candidate

    def string_list(key: str) -> list[str]:
        candidate = value.get(key)
        if not isinstance(candidate, list) or len(candidate) > 32:
            raise DriveClientError("publication_metadata_invalid")
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in candidate):
            raise DriveClientError("publication_metadata_invalid")
        return list(candidate)

    subjects = string_list("subjectIds")
    policy = value.get("retentionPolicyId")
    if isinstance(policy, str) and policy:
        policy = {"value": policy}
    if not isinstance(policy, Mapping) or set(policy) != {"value"} or not isinstance(policy.get("value"), str) or not policy["value"]:
        raise DriveClientError("publication_metadata_invalid")

    # MediaArtifactKind is an SDK enum without a JSON string converter on the
    # graph broker route.  Keep this diagnostic consumer intentionally narrow:
    # SourceMedia is the only permitted import kind and its canonical wire
    # value is numeric 1.  Do not guess or expand the provider-facing enum.
    kind = value.get("kind")
    if kind == "SourceMedia":
        kind = 1
    elif type(kind) is not int or kind != 1:
        raise DriveClientError("publication_metadata_invalid")

    output: dict[str, Any] = {
        "fileName": bounded_string("fileName", 512),
        "role": bounded_string("role"),
        "kind": kind,
        "classification": bounded_string("classification", 64),
        "subjectIds": [{"value": subject} for subject in subjects],
        "allowedPurposes": string_list("allowedPurposes"),
        "consentRequiredPurposes": string_list("consentRequiredPurposes"),
        "retentionPolicyId": {"value": policy["value"]},
        "retentionClass": bounded_string("retentionClass", 64),
        "idempotencyKey": bounded_string("idempotencyKey", 256),
    }
    for key in optional:
        candidate = value.get(key)
        if candidate is not None:
            if not isinstance(candidate, str) or not candidate or len(candidate) > 256:
                raise DriveClientError("publication_metadata_invalid")
            output[key] = candidate
    return output


def _safe_record(value: Mapping[str, Any]) -> dict[str, Any]:
    scalar_allowed = {
        "connectionId", "providerId", "signInMode", "credentialState", "state", "collectionRef", "collectionId",
        "assetId", "selectionRef", "opaqueSelectionRef", "displayName", "name", "mimeType", "sizeBytes", "modifiedAtUtc", "kind", "isReady",
        "isConfigured", "isSignInAvailable", "version", "sha256Checksum", "md5Checksum",
        "isShortcut", "shortcutEligible", "driveType", "view", "locationKind", "locationRef", "driveId",
        "retainedArtifactId", "artifactId", "contentSha256", "contentType", "state", "reasonCode",
        "contentHash", "byteSize", "payloadSchemaId",
        "receiptId", "operationRef", "graphInvocationRef", "status", "isSuccess", "verified", "materialized",
    }
    output = {
        key: value[key]
        for key in scalar_allowed
        if key in value and type(value[key]) in (str, int, float, bool)
    }
    capability = value.get("canDownload")
    if not (type(capability) is bool
            or (isinstance(capability, str) and capability.casefold() in {"true", "false"})):
        metadata = value.get("metadata")
        capability = metadata.get("driveCanDownload") if isinstance(metadata, Mapping) else None
    if type(capability) is bool:
        output["canDownload"] = capability
    elif isinstance(capability, str) and capability.casefold() in {"true", "false"}:
        output["canDownload"] = capability.casefold() == "true"
    for key in ("artifactId", "retainedArtifactId"):
        candidate = value.get(key)
        if isinstance(candidate, Mapping) and set(candidate) == {"value"} and isinstance(candidate.get("value"), str):
            output[key] = candidate["value"]
    for key in ("requiredScopes", "reasonCodes"):
        candidate = value.get(key)
        if (
            isinstance(candidate, list)
            and len(candidate) <= 32
            and all(isinstance(item, str) and len(item) <= 128 for item in candidate)
        ):
            output[key] = list(candidate)
    return output


def _append_safe_graph_fields(output: dict[str, Any], result: Mapping[str, Any]) -> None:
    """Project graph lifecycle DTOs without exposing job-tree/provider detail."""
    for key in ("invocationRef", "scopeId", "rootJobId", "graphHash", "expiresUtc", "contractVersion"):
        value = result.get(key)
        if type(value) in (str, int):
            output[key] = value
    status = result.get("status")
    if isinstance(status, Mapping):
        statuses = _graph_status_values(status)
        if statuses:
            output["graphStatuses"] = statuses[:32]
    elif isinstance(status, str) and len(status) <= 64:
        output["graphStatuses"] = [status]
    for key in ("operation", "operationRef"):
        value = result.get(key)
        if isinstance(value, str) and len(value) <= 128:
            output[key] = value
    status = result.get("status")
    watch = result.get("watch")
    for container in (status, watch):
        if not isinstance(container, Mapping):
            continue
        cursor = container.get("nextCursor")
        if isinstance(cursor, Mapping):
            # A scope-only cursor can look route-bound while dropping the job
            # sequence that makes continuation safe.  Expose only the
            # published, versioned cursor inventory to the browser projection.
            if _validate_published_graph_cursor(cursor):
                output["graphNextCursor"] = cursor


def _graph_status_values(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 8:
        return []
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"status", "state", "outcome", "currentState"} and isinstance(item, str) and len(item) <= 64:
                values.append(item)
            elif isinstance(item, (Mapping, list)):
                values.extend(_graph_status_values(item, depth=depth + 1))
    elif isinstance(value, list):
        for item in value[:128]:
            values.extend(_graph_status_values(item, depth=depth + 1))
    return values


def _normalize_graph_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    return value.casefold().replace("_", "").replace("-", "")


def _graph_result_terminal(result: Mapping[str, Any]) -> bool:
    terminal = {
        "completed", "completedwithfailures", "succeeded", "failed", "cancelled",
        "canceled", "queuetimeout", "timedout", "lost", "superseded",
    }
    projected = result.get("graphStatuses")
    values = projected if isinstance(projected, list) else _graph_status_values(result)
    statuses = [normalized for value in values if (normalized := _normalize_graph_status(value))]
    if not statuses:
        return False
    return all(value in terminal for value in statuses)
