from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MODULE = Path(__file__).parents[2] / "src" / "drive-diagnostics" / "engine_client.py"
SPEC = importlib.util.spec_from_file_location("drive_engine_client", MODULE)
assert SPEC and SPEC.loader
client_module = importlib.util.module_from_spec(SPEC)
sys.modules["drive_engine_client"] = client_module
SPEC.loader.exec_module(client_module)
Config = client_module.Config
DriveClient = client_module.DriveClient
DriveClientError = client_module.DriveClientError


CONTRACTS = {
    "operations": {
        "readiness": {"method": "GET", "path": "/readiness"},
        "start": {"method": "POST", "path": "/start"},
        "status": {"method": "GET", "path": "/status"},
        "connections": {"method": "GET", "path": "/connections"},
        "collections": {"method": "POST", "path": "/collections"},
        "assets": {"method": "POST", "path": "/assets"},
        "browse": {"method": "POST", "path": "/cloud/media:browse"},
        "graphCancel": {"method": "POST", "path": "/graph/cancel"},
        "callback": {"method": "GET", "path": "/callback/{providerId}"},
    }
}


class EngineHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict]] = []
    consumer_ids: list[str | None] = []
    mode = "happy"
    http_error_status = 500
    http_error_payload = {"secret": "must-not-leak"}
    connections_payload = None
    collections_payload = None
    assets_payload = None
    redirect_target = "/redirect-target"

    def log_message(self, *_args):
        return

    def _reply(self, status: int, payload: object, headers: dict[str, str] | None = None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        EngineHandler.consumer_ids.append(self.headers.get("X-Linnaeus-Consumer-Id"))
        EngineHandler.calls.append(("GET", self.path, {}))
        if urlsplit(self.path).path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_target)
            self.end_headers()
            return
        if EngineHandler.mode == "http-error":
            self._reply(EngineHandler.http_error_status, EngineHandler.http_error_payload)
            return
        if EngineHandler.mode == "semantic-fail":
            self._reply(200, {"isSuccess": False, "reasonCodes": ["permission_denied"], "secret": "must-not-leak"})
            return
        if EngineHandler.mode == "string-success":
            self._reply(200, {"isSuccess": "false", "reasonCodes": ["permission_denied"]})
            return
        if EngineHandler.mode == "large":
            self._reply(200, {"isSuccess": True, "padding": "x" * (1024 * 1024 + 4)})
            return
        route = urlsplit(self.path).path
        if route == "/readiness":
            if EngineHandler.mode == "actual-shape":
                self._reply(200, {"productId": "product", "environmentId": "env", "providers": [{"providerId": "google-drive", "isConfigured": True, "isSignInAvailable": True, "reasonCodes": []}], "reasonCodes": []})
            else:
                self._reply(200, {"isSuccess": True, "providers": [{"providerId": "google-drive", "isSignInAvailable": True}]})
        elif route == "/callback/google-drive":
            text = b"Cloud sign-in complete\n\nYou can return to the app.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)
        elif route == "/status":
            self._reply(200, {"isSuccess": True, "providerId": "google-drive", "state": "opaque-state", "status": "connected"})
        elif route == "/connections":
            if EngineHandler.mode == "actual-shape":
                self._reply(200, {"connections": [{"connectionId": "conn-1", "providerId": "google-drive", "credentialState": "available"}], "capturedAtUtc": "2026-11-18T00:00:00Z"})
            else:
                self._reply(200, EngineHandler.connections_payload or {"isSuccess": True, "connections": [{"connectionId": "conn-1", "providerId": "google-drive", "credentialRef": "secret://never"}]})
        else:
            self._reply(404, {"message": "unknown"})

    def do_POST(self):
        EngineHandler.consumer_ids.append(self.headers.get("X-Linnaeus-Consumer-Id"))
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        EngineHandler.calls.append(("POST", self.path, body))
        if EngineHandler.mode == "semantic-fail":
            self._reply(200, {"isSuccess": False, "reasonCodes": ["permission_denied"], "secret": "must-not-leak"})
            return
        if EngineHandler.mode == "wrapped-semantic-fail":
            self._reply(200, {
                "isSuccess": False,
                "connectionId": "conn-1",
                "result": {
                    "isSuccess": False,
                    "collections": [],
                    "assets": [],
                    "reasonCodes": ["cloud_credentials_refresh_failed"],
                    "providerPayload": {"accessToken": "must-not-leak"},
                },
            })
            return
        if self.path == "/start":
            self._reply(200, {"isSuccess": True, "providerId": "google-drive", "state": "opaque-state", "status": "pending", "authorizationUrl": "https://accounts.google.com/o/oauth2/v2/auth?client_id=test"})
        elif self.path == "/collections":
            self._reply(200, EngineHandler.collections_payload or {"isSuccess": True, "collections": [{"collectionId": "folder-1", "displayName": "Shared"}]})
        elif self.path == "/assets":
            self._reply(200, EngineHandler.assets_payload or {"isSuccess": True, "assets": [{"assetId": "asset-1", "displayName": "study.dcm", "mimeType": "application/dicom"}]})
        else:
            self._reply(404, {"message": "unknown"})


class BinaryResponse:
    def __init__(self, body: bytes, content_length: str | None, content_type: str = "application/octet-stream",
                 *, overrun: bool = False):
        self.status = 200
        self._body = body
        self._content_length = content_length
        self._content_type = content_type
        self._offset = 0
        self._overrun = overrun

    def getheader(self, name: str, default=None):
        if name.casefold() == "content-length":
            return self._content_length if self._content_length is not None else default
        if name.casefold() == "content-type":
            return self._content_type
        return default

    def read(self, size: int = -1) -> bytes:
        if self._overrun and self._offset == 0:
            self._offset = len(self._body)
            return self._body
        if self._offset >= len(self._body):
            return b""
        if size < 0:
            size = len(self._body) - self._offset
        end = min(self._offset + size, len(self._body))
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk


class BinaryConnection:
    class Socket:
        def settimeout(self, _timeout: float):
            pass

    def __init__(self, response: BinaryResponse):
        self.sock = self.Socket()
        self.response = response

    def request(self, *_args, **_kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


class DriveClientTests(unittest.TestCase):
    def setUp(self):
        EngineHandler.calls = []
        EngineHandler.consumer_ids = []
        EngineHandler.mode = "happy"
        EngineHandler.http_error_status = 500
        EngineHandler.http_error_payload = {"secret": "must-not-leak"}
        EngineHandler.connections_payload = None
        EngineHandler.collections_payload = None
        EngineHandler.assets_payload = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), EngineHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.contracts = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(CONTRACTS, self.contracts)
        self.contracts.close()
        self.config = Config(
            f"http://127.0.0.1:{self.server.server_port}", "access-secret", "product", "env", "tenant", "workspace",
            consumer_id="consumer-test", user_id="user"
        )

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        Path(self.contracts.name).unlink(missing_ok=True)

    def client(self):
        return DriveClient(self.config, self.contracts.name)

    def test_sanitize_preserves_nonnegative_verified_byte_count(self):
        result = DriveClient._sanitize(
            "verifiedRead", {"isSuccess": True, "verifiedBytesRead": 4096}
        )
        self.assertEqual(4096, result["verifiedBytesRead"])
        self.assertNotIn(
            "verifiedBytesRead",
            DriveClient._sanitize("verifiedRead", {"isSuccess": True, "verifiedBytesRead": -1}),
        )

    def _verified_read(self, body: bytes, content_length: str | None,
                       content_type: str = "application/octet-stream", *, overrun: bool = False):
        client = self.client()
        response = BinaryResponse(body, content_length, content_type, overrun=overrun)
        connection = BinaryConnection(response)
        with mock.patch.object(client_module.http.client, "HTTPConnection", return_value=connection):
            return client._request("GET", "/verified", query={}, body=None, expect_binary=True)

    def test_verified_read_streams_bounded_bytes_and_returns_hash_only(self):
        result = self._verified_read(b"verified-content", str(len(b"verified-content")))
        self.assertTrue(result["isSuccess"])
        self.assertEqual(len(b"verified-content"), result["verifiedBytesRead"])
        self.assertEqual(hashlib.sha256(b"verified-content").hexdigest(), result["contentSha256"])

    def test_verified_read_rejects_short_overrun_oversized_and_wrong_type(self):
        self.assertEqual(
            ["engine_verified_read_incomplete"],
            self._verified_read(b"short", "10")["reasonCodes"],
        )
        self.assertEqual(
            ["engine_verified_read_overrun"],
            self._verified_read(b"four", "3", overrun=True)["reasonCodes"],
        )
        self.assertEqual(
            ["engine_verified_read_size_invalid"],
            self._verified_read(b"", str(client_module._MAX_VERIFIED_READ_BYTES + 1))["reasonCodes"],
        )
        self.assertEqual(
            ["engine_verified_read_refused"],
            self._verified_read(b"not-media", "9", "application/json")["reasonCodes"],
        )

    def test_callback_uses_published_plain_text_contract_and_keeps_secrets_private(self):
        client = self.client()
        client.execute("start")
        result = client.complete_callback({"state": "opaque-state", "code": "private-code"})
        self.assertEqual({"isSuccess": True}, result)
        method, path, _ = EngineHandler.calls[-1]
        self.assertEqual("GET", method)
        self.assertEqual("/callback/google-drive", urlsplit(path).path)
        self.assertEqual(["private-code"], parse_qs(urlsplit(path).query)["code"])
        with self.assertRaisesRegex(DriveClientError, "callback_state_invalid"):
            client.complete_callback({"state": "opaque-state", "code": "private-code"})

    def test_callback_requires_current_state_and_is_consumed_before_transport(self):
        client = self.client()
        with self.assertRaisesRegex(DriveClientError, "callback_state_invalid"):
            client.complete_callback({"state": "opaque-state", "code": "private-code"})
        client.execute("start")
        with self.assertRaisesRegex(DriveClientError, "callback_state_invalid"):
            client.complete_callback({"state": "other", "code": "private-code"})
        with mock.patch.object(client, "_request", side_effect=DriveClientError("engine_unreachable")) as request:
            with self.assertRaisesRegex(DriveClientError, "engine_unreachable"):
                client.complete_callback({"state": "opaque-state", "code": "private-code"})
            self.assertEqual("/callback/google-drive", request.call_args.args[1])
            self.assertTrue(request.call_args.kwargs["callback_text"])
        with self.assertRaisesRegex(DriveClientError, "callback_state_invalid"):
            client.complete_callback({"state": "opaque-state", "code": "private-code"})
        # Status remains available after the callback has been consumed.
        self.assertTrue(client.execute("status")["isSuccess"])

    def test_callback_rejects_ambiguous_or_unbounded_parameters(self):
        client = self.client()
        client.execute("start")
        for query in ({"state": "opaque-state"},
                      {"state": "opaque-state", "code": "a", "error": "b"},
                      {"state": "opaque-state", "code": "x" * 4097},
                      {"state": "opaque-state", "code": "a", "url": "https://evil.invalid"}):
            with self.subTest(keys=list(query)):
                with self.assertRaisesRegex(DriveClientError, "callback_parameters_invalid"):
                    client.complete_callback(query)
        with mock.patch.object(client, "_request", return_value={"isSuccess": False}) as request:
            self.assertFalse(client.complete_callback({"state": "opaque-state", "error": "access_denied"})["isSuccess"])
            self.assertEqual("access_denied", request.call_args.kwargs["query"]["error"])

    def test_full_flow_sends_bearer_and_keeps_refs_bound(self):
        client = self.client()
        self.assertTrue(client.execute("readiness")["isSuccess"])
        started = client.execute("start")
        self.assertEqual(started["state"], "opaque-state")
        self.assertTrue(client.execute("status")["isSuccess"])
        connections = client.execute("connections")
        self.assertEqual(connections["connections"][0]["connectionId"], "conn-1")
        self.assertNotIn("credentialRef", connections["connections"][0])
        collections = client.execute("collections", {"connectionId": "conn-1"})
        self.assertEqual(collections["collections"][0]["collectionId"], "folder-1")
        assets = client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-1"})
        self.assertEqual(assets["assets"][0]["assetId"], "asset-1")
        self.assertTrue(all(call[2] == {} or call[2].get("ownerScope") for call in EngineHandler.calls))
        self.assertTrue(EngineHandler.consumer_ids)
        self.assertTrue(all(value == "consumer-test" for value in EngineHandler.consumer_ids))

    def test_new_authorization_attempt_invalidates_previous_account_bindings(self):
        for outcome in ("success", "semantic-failure", "transport-failure"):
            with self.subTest(outcome=outcome):
                EngineHandler.mode = "happy"
                client = self.client()
                client.execute("start")
                client.execute("connections")
                client.execute("collections", {"connectionId": "conn-1"})
                if outcome == "transport-failure":
                    with mock.patch.object(client, "_request", side_effect=DriveClientError("engine_unreachable")):
                        with self.assertRaisesRegex(DriveClientError, "engine_unreachable"):
                            client.execute("start")
                else:
                    EngineHandler.mode = "semantic-fail" if outcome == "semantic-failure" else "happy"
                    self.assertEqual(outcome == "success", client.execute("start")["isSuccess"])
                before = len(EngineHandler.calls)
                if outcome != "success":
                    self.assertEqual(["authorization_not_started"], client.execute("status")["reasonCodes"])
                with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
                    client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-1"})
                self.assertEqual(before, len(EngineHandler.calls))

    def test_actual_engine_readiness_and_connection_dtos_normalize_on_2xx(self):
        EngineHandler.mode = "actual-shape"
        client = self.client()
        readiness = client.execute("readiness")
        self.assertTrue(readiness["isSuccess"])
        self.assertEqual("google-drive", readiness["providers"][0]["providerId"])
        connections = client.execute("connections")
        self.assertTrue(connections["isSuccess"])
        self.assertEqual(["conn-1"], [item["connectionId"] for item in connections["connections"]])

    def test_actual_shape_requires_valid_schema(self):
        EngineHandler.mode = "actual-shape"
        client = self.client()
        original = EngineHandler.do_GET
        def malformed(handler):
            if urlsplit(handler.path).path == "/readiness":
                handler._reply(200, {"providers": "not-a-list", "reasonCodes": []})
            else:
                original(handler)
        EngineHandler.do_GET = malformed
        try:
            result = client.execute("readiness")
            self.assertFalse(result["isSuccess"])
            self.assertEqual(["engine_response_invalid"], result["reasonCodes"])
        finally:
            EngineHandler.do_GET = original

    def test_unknown_connection_or_collection_never_calls_engine(self):
        client = self.client()
        with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
            client.execute("collections", {"connectionId": "other"})
        client.execute("connections")
        with self.assertRaisesRegex(DriveClientError, "unknown_collection_reference"):
            client.execute("assets", {"connectionId": "conn-1", "collectionRef": "other"})
        self.assertEqual(len(EngineHandler.calls), 1)

    def test_semantic_failure_is_not_success_and_body_is_redacted(self):
        EngineHandler.mode = "semantic-fail"
        result = self.client().execute("readiness")
        self.assertFalse(result["isSuccess"])
        self.assertEqual(result["reasonCodes"], ["permission_denied"])
        self.assertNotIn("secret", result)

    def test_cloud_operation_wrapper_preserves_nested_failure_reason_without_payload(self):
        client = self.client()
        client.execute("connections")
        EngineHandler.mode = "wrapped-semantic-fail"
        result = client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["cloud_credentials_refresh_failed"], result["reasonCodes"])
        self.assertNotIn("providerPayload", result)
        self.assertNotIn("accessToken", json.dumps(result))

    def test_cloud_operation_wrappers_only_use_nested_reasons_for_explicit_failure(self):
        failure = {
            "isSuccess": False,
            "result": {"isSuccess": False, "reasonCodes": ["cloud_credentials_refresh_failed"]},
        }
        success = {
            "isSuccess": True,
            "result": {"isSuccess": False, "reasonCodes": ["must_not_become_failure"]},
        }
        for action in ("browse", "acquire", "collections", "assets", "inspect", "verify", "materialize", "release", "delete"):
            with self.subTest(action=action):
                self.assertEqual(["cloud_credentials_refresh_failed"], DriveClient._sanitize(action, failure)["reasonCodes"])
                self.assertEqual([], DriveClient._sanitize(action, success)["reasonCodes"])

    def test_string_success_flag_is_not_truthy(self):
        EngineHandler.mode = "string-success"
        result = self.client().execute("readiness")
        self.assertFalse(result["isSuccess"])

    def test_graph_cancel_idempotency_key_matches_engine_contract_before_network(self):
        client = self.client()
        client._graph_bindings["browse"] = {
            "scopeId": "scope-cancel",
            "invocationRef": "i" * 64,
            "connectionId": "conn-1",
        }
        invalid = ("", "x" * 129, "contains/slash", "contains space", "unicode-\N{SNOWMAN}")
        with mock.patch.object(client, "_request") as request:
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(DriveClientError, "graph_cancel_idempotency_key_required"):
                        client.execute("graphCancel", {"graphKind": "browse", "idempotencyKey": value})
            request.assert_not_called()

        with mock.patch.object(client, "_request", return_value={"isSuccess": True, "operation": "cancel", "status": "Cancelling"}) as request:
            result = client.execute("graphCancel", {"graphKind": "browse", "idempotencyKey": "v" * 128})
        self.assertTrue(result["isSuccess"])
        self.assertEqual({"idempotencyKey": "v" * 128}, request.call_args.kwargs["body"])

    def test_connection_refresh_replaces_and_invalidates_old_bindings(self):
        client = self.client()
        client.execute("connections")
        EngineHandler.connections_payload = {"isSuccess": True, "connections": [{"connectionId": "conn-2", "providerId": "google-drive"}]}
        client.execute("connections")
        with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
            client.execute("collections", {"connectionId": "conn-1"})
        self.assertTrue(client.execute("collections", {"connectionId": "conn-2"})["isSuccess"])

    def test_same_connection_refresh_clears_stale_collection_bindings(self):
        client = self.client()
        client.execute("connections")
        client.execute("collections", {"connectionId": "conn-1"})
        # A refresh can represent a re-authenticated account reusing the same
        # provider connection ID; old collection refs must not survive it.
        client.execute("connections")
        with self.assertRaisesRegex(DriveClientError, "unknown_collection_reference"):
            client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-1"})

    def test_nested_secret_objects_and_invalid_arrays_are_not_projected(self):
        client = self.client()
        EngineHandler.connections_payload = {
            "isSuccess": True,
            "connections": [{
                "connectionId": "conn-1",
                "providerId": "google-drive",
                "requiredScopes": ["drive.readonly"],
                "reasonCodes": ["ready"],
                "credentialState": {"token": "secret"},
                "metadata": {"secret": "must-not-leak"},
            }],
        }
        result = client.execute("connections")
        record = result["connections"][0]
        self.assertEqual(record["requiredScopes"], ["drive.readonly"])
        self.assertEqual(record["reasonCodes"], ["ready"])
        self.assertNotIn("credentialState", record)
        self.assertNotIn("metadata", record)

        EngineHandler.collections_payload = {
            "isSuccess": True,
            "collections": [{
                "collectionId": "folder-1",
                "connectionId": "conn-1",
                "requiredScopes": ["drive.readonly", {"secret": "nested"}],
                "sizeBytes": [123],
                "metadata": {"secret": "must-not-leak"},
            }],
        }
        collection = client.execute("collections", {"connectionId": "conn-1"})["collections"][0]
        self.assertNotIn("requiredScopes", collection)
        self.assertNotIn("sizeBytes", collection)
        self.assertNotIn("metadata", collection)

    def test_mismatched_provider_and_connection_records_are_filtered(self):
        client = self.client()
        EngineHandler.connections_payload = {
            "isSuccess": True,
            "connections": [
                {"connectionId": "conn-1", "providerId": "google-drive"},
                {"connectionId": "evil", "providerId": "dropbox"},
            ],
        }
        self.assertEqual(["conn-1"], [item["connectionId"] for item in client.execute("connections")["connections"]])
        EngineHandler.collections_payload = {
            "isSuccess": True,
            "collections": [
                {"collectionId": "folder-1", "connectionId": "evil", "providerId": "google-drive"},
                {"collectionId": "folder-2", "connectionId": "conn-1", "providerId": "google-drive"},
                {"collectionId": "folder-3", "connectionId": "conn-1", "providerId": "dropbox"},
            ],
        }
        collections = client.execute("collections", {"connectionId": "conn-1"})
        self.assertEqual(["folder-2"], [item["collectionId"] for item in collections["collections"]])
        EngineHandler.assets_payload = {
            "isSuccess": True,
            "assets": [
                {"assetId": "asset-1", "collectionId": "folder-2", "connectionId": "evil", "providerId": "google-drive"},
                {"assetId": "asset-2", "collectionId": "folder-2", "connectionId": "conn-1", "providerId": "google-drive"},
                {"assetId": "asset-3", "collectionId": "folder-2", "connectionId": "conn-1", "providerId": "dropbox"},
            ],
        }
        assets = client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-2"})
        self.assertEqual(["asset-2"], [item["assetId"] for item in assets["assets"]])

    def test_http_failure_and_response_limit_are_safe(self):
        EngineHandler.mode = "http-error"
        failed = self.client().execute("readiness")
        self.assertEqual(failed["reasonCodes"], ["engine_http_error"])
        self.assertEqual(500, failed["httpStatus"])
        self.assertFalse(failed["isSuccess"])
        self.assertNotIn("must-not-leak", json.dumps(failed))
        EngineHandler.mode = "large"
        limited = self.client().execute("readiness")
        self.assertEqual(limited["reasonCodes"], ["engine_response_too_large"])

    def test_http_error_projects_only_allowlisted_admission_reason_codes(self):
        for reason_code in sorted(client_module._PUBLIC_GRAPH_ADMISSION_REASON_CODES):
            with self.subTest(reason_code=reason_code):
                result = client_module._project_http_error(
                    json.dumps({
                        "reasonCode": reason_code,
                        "message": "secret detail",
                        "providerId": "secret-provider",
                        "extensions": {"token": "secret-token"},
                    }).encode("utf-8"),
                    409,
                )
                self.assertFalse(result["isSuccess"])
                self.assertEqual(409, result["httpStatus"])
                self.assertEqual([reason_code], result["reasonCodes"])
                self.assertNotIn("secret", json.dumps(result))
                self.assertNotIn("providerId", result)

    def test_reconciliation_error_has_only_fixed_retry_projection(self):
        result = client_module._project_http_error(
            json.dumps({
                "reasonCode": "external_media_reconciliation_required",
                "reconciliationRequired": False,
                "retryable": True,
                "message": "secret detail",
                "extensions": {"token": "secret-token"},
            }).encode("utf-8"),
            409,
        )
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["external_media_reconciliation_required"], result["reasonCodes"])
        self.assertTrue(result["reconciliationRequired"])
        self.assertFalse(result["retryable"])
        self.assertNotIn("message", result)
        self.assertNotIn("extensions", result)

        EngineHandler.mode = "http-error"
        EngineHandler.http_error_status = 409
        EngineHandler.http_error_payload = {
            "reasonCode": "external_media_reconciliation_required",
            "isSuccess": True,
            "reconciliationRequired": False,
            "retryable": True,
            "unknown": {"secret": "must-not-leak"},
        }
        response = self.client().execute("readiness")
        self.assertFalse(response["isSuccess"])
        self.assertEqual(["external_media_reconciliation_required"], response["reasonCodes"])
        self.assertTrue(response["reconciliationRequired"])
        self.assertFalse(response["retryable"])
        self.assertNotIn("unknown", response)
        self.assertNotIn("secret", json.dumps(response))

    def test_http_error_rejects_unknown_nested_and_body_status_fields(self):
        EngineHandler.mode = "http-error"
        EngineHandler.http_error_status = 409
        EngineHandler.http_error_payload = {
            "reasonCode": {"value": "external_media_trust_unavailable"},
            "status": 200,
            "extensions": {"reasonCode": "external_media_trust_unavailable"},
            "message": "secret detail",
            "providerId": "secret-provider",
        }
        result = self.client().execute("readiness")
        self.assertFalse(result["isSuccess"])
        self.assertEqual(409, result["httpStatus"])
        self.assertEqual(["engine_http_error"], result["reasonCodes"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("providerId", result)

    def test_http_error_malformed_body_stays_generic(self):
        result = client_module._project_http_error(b"{\"reasonCode\":", 503)
        self.assertFalse(result["isSuccess"])
        self.assertEqual(503, result["httpStatus"])
        self.assertEqual(["engine_http_error"], result["reasonCodes"])

    def test_no_redirect_and_unavailable_acquisition(self):
        contracts = {"operations": {"readiness": {"method": "GET", "path": "/redirect"}}}
        Path(self.contracts.name).write_text(json.dumps(contracts), encoding="utf-8")
        self.assertEqual(self.client().execute("readiness")["reasonCodes"], ["engine_redirect_forbidden"])
        result = self.client().execute("download")
        self.assertEqual(result, {"action": "download", "isSuccess": False, "reasonCodes": ["capability_unavailable"]})
        self.assertEqual(self.client().execute("acquire"), {"action": "acquire", "isSuccess": False, "reasonCodes": ["capability_unavailable"]})

    def test_config_rejects_remote_http_and_url_credentials(self):
        with self.assertRaisesRegex(DriveClientError, "remote_http_engine_base_forbidden"):
            Config("http://engine.example", "token", "p", "e", "t", "w", consumer_id="c", user_id="u")
        with self.assertRaisesRegex(DriveClientError, "invalid_engine_base_url"):
            Config("https://user:pass@engine.example", "token", "p", "e", "t", "w", consumer_id="c", user_id="u")
        with self.assertRaisesRegex(DriveClientError, "engine_certificate_pin_required"):
            Config("https://engine.example", "token", "p", "e", "t", "w", consumer_id="c", user_id="u")
        with self.assertRaisesRegex(DriveClientError, "invalid_engine_certificate_pin"):
            Config("https://engine.example", "token", "p", "e", "t", "w", consumer_id="c", user_id="u", certificate_sha256="nope")
        with self.assertRaisesRegex(DriveClientError, "invalid_engine_base_url"):
            Config("https://engine.example/engine", "token", "p", "e", "t", "w", consumer_id="c", user_id="u", certificate_sha256="0" * 64)

    def test_config_requires_consumer_id_environment(self):
        environment = {
            "ENGINE_BASE_URL": "http://127.0.0.1:1234",
            "ENGINE_ACCESS_TOKEN": "token",
            "ENGINE_PRODUCT_ID": "product",
            "ENGINE_ENVIRONMENT_ID": "env",
            "ENGINE_TENANT_ID": "tenant",
            "ENGINE_WORKSPACE_ID": "workspace",
            "ENGINE_USER_ID": "user",
        }
        with self.assertRaisesRegex(DriveClientError, "missing_engine_configuration:ENGINE_CONSUMER_ID"):
            Config.from_env(environment)
        environment["ENGINE_CONSUMER_ID"] = "consumer"
        self.assertEqual("consumer", Config.from_env(environment).consumer_id)

    def test_certificate_pin_mismatch_happens_before_authorized_request(self):
        class FakeSocket:
            def getpeercert(self, binary_form=False):
                self.binary_form = binary_form
                return b"peer-certificate"

        class FakeTlsConnection:
            def __init__(self, *_args, **_kwargs):
                self.sock = FakeSocket()
                self.request_called = False

            def connect(self):
                self.connected = True

            def request(self, *_args, **_kwargs):
                self.request_called = True

            def close(self):
                pass

        fake = FakeTlsConnection()
        config = Config(
            "https://engine.example",
            "access-secret",
            "product",
            "env",
            "tenant",
            "workspace",
            consumer_id="consumer-test",
            user_id="user",
            certificate_sha256="00" * 32,
        )
        client = DriveClient(config, self.contracts.name)
        with mock.patch.object(client_module.http.client, "HTTPSConnection", return_value=fake):
            with self.assertRaisesRegex(DriveClientError, "engine_certificate_pin_mismatch"):
                client.execute("readiness")
        self.assertFalse(fake.request_called)

    def test_start_rejects_untrusted_authorization_host(self):
        EngineHandler.mode = "happy"
        original = EngineHandler.do_POST
        def unsafe(handler):
            if handler.path == "/start":
                handler._reply(200, {"isSuccess": True, "state": "s", "authorizationUrl": "https://evil.example/auth"})
            else:
                original(handler)
        EngineHandler.do_POST = unsafe
        try:
            with self.assertRaisesRegex(DriveClientError, "engine_authorization_url_invalid"):
                self.client().execute("start")
        finally:
            EngineHandler.do_POST = original


if __name__ == "__main__":
    unittest.main()
