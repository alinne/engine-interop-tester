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
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_MAX_RESPONSE_BYTES = 1024 * 1024
_PROVIDER_ID = "google-drive"
_CONSUMER_HEADER = "X-Linnaeus-Consumer-Id"
_ACTIONS = ("readiness", "start", "status", "connections", "collections", "assets")
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
    """Small Engine client for the six currently published Drive diagnostics."""

    def __init__(self, config: Config, contracts_path: str | os.PathLike[str] | None = None, timeout: float = 10.0) -> None:
        if timeout <= 0 or timeout > 60:
            raise DriveClientError("invalid_engine_timeout")
        self.config = config
        self.timeout = timeout
        self._contracts_path = Path(contracts_path) if contracts_path else Path(__file__).with_name("contracts.json")
        self._state: str | None = None
        self._connection_ids: set[str] = set()
        self._collection_ids: dict[str, set[str]] = {}
        self._last_collection_connection: str | None = None

    def execute(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if action not in _ACTIONS:
            if action in {"import", "download", "open", "acquire"}:
                return {"action": action, "isSuccess": False, "reasonCodes": ["capability_unavailable"]}
            raise DriveClientError("unsupported_drive_action")
        if action == "start":
            # Starting over may select another Google account. Invalidate the
            # previous attempt before any fallible contract or network work.
            self._state = None
            self._clear_connection_bindings()
        operation = self._operation(action)
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
        elif action in {"collections", "assets"}:
            body = self._bound_body(action, payload)

        if action == "connections":
            # Treat every refresh attempt as a binding boundary.  A failed or
            # malformed refresh must not leave the UI able to use old refs.
            self._clear_connection_bindings()
        selected_connection = body.get("connectionId") if isinstance(body, Mapping) else None
        result = self._request(operation["method"], self._path(operation["path"], action), query=query, body=body)
        result = self._normalize_published_shape(action, result)
        result = self._sanitize(action, result, selected_connection=selected_connection)
        if action == "start" and result.get("isSuccess"):
            state = result.get("state")
            url = result.get("authorizationUrl")
            if not isinstance(state, str) or not state:
                return {"action": action, "isSuccess": False, "reasonCodes": ["engine_response_invalid"]}
            self._validate_authorization_url(url)
            self._state = state
        elif action == "connections" and result.get("isSuccess"):
            self._remember_connections(result)
        elif action == "collections" and result.get("isSuccess"):
            result = {**result, "connectionId": self._last_collection_connection}
            self._remember_collections(result)
        return result

    @staticmethod
    def _normalize_published_shape(action: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize only the two published 2xx DTOs that omit ``isSuccess``."""
        normalized = dict(result)
        status = result.get("httpStatus")
        if not isinstance(status, int) or not 200 <= status < 300 or "isSuccess" in result:
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

    def _path(self, template: str, action: str) -> str:
        if "{" in template or "}" in template:
            raise DriveClientError("engine_contract_path_unsupported")
        return template

    def _bound_body(self, action: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise DriveClientError("request_payload_required")
        connection_id = payload.get("connectionId")
        if not isinstance(connection_id, str) or connection_id not in self._connection_ids:
            raise DriveClientError("unknown_connection_reference")
        body: dict[str, Any] = {"connectionId": connection_id, "ownerScope": self.config.owner_scope}
        if action == "collections":
            self._last_collection_connection = connection_id
            return body
        collection_ref = payload.get("collectionRef")
        if not isinstance(collection_ref, str) or collection_ref not in self._collection_ids.get(connection_id, set()):
            raise DriveClientError("unknown_collection_reference")
        body["collectionRef"] = collection_ref
        return body

    def _request(self, method: str, path: str, *, query: Mapping[str, str], body: Mapping[str, Any] | None) -> dict[str, Any]:
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
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.bearer_token}",
                _CONSUMER_HEADER: self.config.consumer_id,
            }
            if encoded is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, target, body=encoded, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                return {"isSuccess": False, "reasonCodes": ["engine_redirect_forbidden"]}
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(data) > _MAX_RESPONSE_BYTES:
                return {"isSuccess": False, "reasonCodes": ["engine_response_too_large"]}
            if response.status >= 400:
                return {"isSuccess": False, "reasonCodes": ["engine_http_error"], "httpStatus": response.status}
            return self._parse_response(data, response.status)
        except DriveClientError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise DriveClientError("engine_unreachable") from exc
        finally:
            if connection is not None:
                connection.close()

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
        if isinstance(result.get("connection"), Mapping):
            output["connection"] = _safe_record(result["connection"])
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
        self._last_collection_connection = None

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


def _safe_record(value: Mapping[str, Any]) -> dict[str, Any]:
    scalar_allowed = {
        "connectionId", "providerId", "signInMode", "credentialState", "state", "collectionRef", "collectionId",
        "assetId", "displayName", "name", "mimeType", "sizeBytes", "modifiedAtUtc", "kind", "isReady",
        "isConfigured", "isSignInAvailable",
    }
    output = {
        key: value[key]
        for key in scalar_allowed
        if key in value and type(value[key]) in (str, int, float, bool)
    }
    for key in ("requiredScopes", "reasonCodes"):
        candidate = value.get(key)
        if (
            isinstance(candidate, list)
            and len(candidate) <= 32
            and all(isinstance(item, str) and len(item) <= 128 for item in candidate)
        ):
            output[key] = list(candidate)
    return output
