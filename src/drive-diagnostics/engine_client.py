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
    "graphStatus", "graphCancel",
)
_OPTIONAL_ACTIONS = frozenset(_ACTIONS) - frozenset(("readiness", "start", "status", "connections", "collections", "assets"))
_ACTION_ALIASES = {
    "access": ("verifiedRead", "inspect"),
    "release": ("release", "tombstone"),
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SAFE_STATUS_KEYS = {"providerId", "state", "status", "isSuccess", "reasonCodes", "expiresAtUtc"}


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
                        "graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphCancel"}:
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
        if action in {"graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphCancel"}:
            result = self._project_graph_result(action, raw_result, result)
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
        elif action == "collections" and result.get("isSuccess"):
            result = {**result, "connectionId": self._last_collection_connection}
            self._remember_collections(result)
        elif action in {"assets", "browse"} and result.get("isSuccess"):
            self._remember_assets(result, selected_connection)
        elif action in {"acquire", "materialize"} and result.get("isSuccess"):
            self._remember_artifacts(result, selected_connection)
        elif action in {"graphPrepareBrowse", "graphPrepareAcquire"} and result.get("isSuccess"):
            self._remember_graph_prepare(action, result, body)
        elif action in {"graphSubmitBrowse", "graphSubmitAcquire"}:
            self._remember_graph_submit(action, result)
            if action == "graphSubmitBrowse" and result.get("isSuccess"):
                binding = self._graph_bindings.get("browse")
                if binding is not None:
                    self._remember_assets(result, binding.get("connectionId"))
        elif action == "graphStatus":
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
        """Normalize only the two published 2xx DTOs that omit ``isSuccess``."""
        normalized = dict(result)
        status = result.get("httpStatus")
        if not isinstance(status, int) or not 200 <= status < 300:
            return normalized
        # Graph submission status is authoritative even if a legacy/proxy
        # response also carries an optimistic isSuccess field. Other routes
        # retain their explicit published result shape unchanged.
        if "isSuccess" in result and action not in {"graphSubmitBrowse", "graphSubmitAcquire"}:
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
            if isinstance(result.get("invocationRef"), str) and isinstance(result.get("status"), Mapping):
                status_value = result["status"].get("status")
                normalized_status = _normalize_graph_status(status_value)
                if normalized_status in _GRAPH_FAILED_STATUSES:
                    return {
                        **normalized,
                        "isSuccess": False,
                        "reasonCodes": _reason_codes(result.get("reasonCodes")) or ["external_media_graph_not_completed"],
                    }
                return {**normalized, "isSuccess": True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
        if action == "graphCancel":
            if isinstance(result.get("operation"), str) or isinstance(result.get("status"), (str, Mapping)):
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
        if action in {"graphStatus", "graphCancel"}:
            if graph_kind not in {"browse", "acquire"}:
                raise DriveClientError("graph_kind_required")
            binding = self._graph_bindings.get(graph_kind)
            if not binding:
                raise DriveClientError("graph_not_prepared")
            if action == "graphCancel":
                idempotency_key = payload.get("idempotencyKey")
                if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256:
                    raise DriveClientError("graph_cancel_idempotency_key_required")
                return {
                    "graphKind": graph_kind,
                    "idempotencyKey": idempotency_key,
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
        artifact_id = payload.get("retainedArtifactId") or payload.get("artifactId")
        if action in {"inspect", "verifiedRead", "verify", "materialize", "release", "delete"}:
            if not isinstance(artifact_id, str) or artifact_id not in self._artifact_ids.get(connection_id, set()):
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
                return {"isSuccess": False, "reasonCodes": ["engine_http_error"], "httpStatus": response.status}
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
        output: dict[str, Any] = {"action": action, "isSuccess": result.get("isSuccess") is True, "reasonCodes": _reason_codes(result.get("reasonCodes"))}
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
        if action in {"graphPrepareBrowse", "graphPrepareAcquire", "graphSubmitBrowse", "graphSubmitAcquire", "graphStatus", "graphCancel"}:
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

    def _project_graph_result(self, action: str, raw: Mapping[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        """Project only output bound to the prepared graph's connection.

        Graph responses contain an intentionally broad JsonElement/status tree. The
        diagnostic app receives only provider-neutral browse records or the
        retained artifact identity, never that tree or provider metadata.
        """
        kind = "browse" if "Browse" in action else "acquire"
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
            if action in {"graphSubmitBrowse", "graphSubmitAcquire", "graphStatus"}:
                return self._graph_binding_failure(output)
            return output
        if action in {"graphSubmitBrowse", "graphSubmitAcquire", "graphStatus"} and (
                not isinstance(invocation, str) or invocation != binding.get("invocationRef")):
            return self._graph_binding_failure(output)
        if isinstance(invocation, str) and invocation and invocation != binding.get("invocationRef"):
            return self._graph_binding_failure(output)
        if action == "graphStatus":
            verified = self._project_graph_status(raw, output, binding)
            output["_graphBindingVerified"] = verified
            if not verified:
                output["isSuccess"] = False
                output["reasonCodes"] = ["graph_binding_mismatch"]
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
            collections = [_safe_record(item) for item in page.get("collections", []) if isinstance(item, Mapping)] if isinstance(page.get("collections"), list) else []
            assets = [_safe_record(item) for item in page.get("assets", []) if isinstance(item, Mapping)] if isinstance(page.get("assets"), list) else []
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
        projection = status.get("projection")
        if not isinstance(projection, Mapping) or not isinstance(projection.get("jobs"), list):
            return True
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
            return True
        if status_value in _GRAPH_ACTIVE_STATUSES or (
                not status_value and any(state in _GRAPH_ACTIVE_STATUSES for state in job_states)):
            # The status request itself succeeded, but an active job is not a
            # valid source of a retained artifact yet.
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
            return True
        output["retainedArtifacts"] = recovered[:32]
        output["retainedArtifact"] = recovered[0]
        output["retainedArtifactId"] = recovered[0]["artifactId"]
        connection_id = binding.get("connectionId")
        if isinstance(connection_id, str):
            self._artifact_ids.setdefault(connection_id, set()).update(item["artifactId"] for item in recovered[:32])
            binding["retainedArtifactId"] = recovered[0]["artifactId"]
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

    def _remember_collections(self, result: Mapping[str, Any]) -> None:
        connection_id = result.get("connectionId")
        if not isinstance(connection_id, str):
            return
        refs = self._collection_ids.setdefault(connection_id, set())
        for item in result.get("collections", []):
            if isinstance(item, Mapping):
                ref = item.get("collectionRef") or item.get("collectionId") or item.get("id")
                if isinstance(ref, str):
                    refs.add(ref)

    def _remember_assets(self, result: Mapping[str, Any], connection_id: str | None) -> None:
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

    output: dict[str, Any] = {
        "fileName": bounded_string("fileName", 512),
        "role": bounded_string("role"),
        "kind": bounded_string("kind", 64),
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
        "isConfigured", "isSignInAvailable", "version", "sha256Checksum", "md5Checksum", "canDownload",
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
