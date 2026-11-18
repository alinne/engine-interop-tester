import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/drive-diagnostics"))
from server import make_server


class FakeClient:
    def __init__(self):
        self.calls = []

    def execute(self, action, payload):
        self.calls.append((action, payload))
        return {"isSuccess": True, "reasonCodes": [], "action": action}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.server = make_server(self.client, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def bootstrap(self):
        with self.opener.open(self.url + "/") as response:
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        with self.opener.open(self.url + "/session") as response:
            return json.load(response)["csrf"]

    def post(self, payload, origin=None, csrf=None):
        headers = {"Content-Type": "application/json", "Origin": origin or self.url}
        if csrf:
            headers["X-Drive-CSRF"] = csrf
        return self.opener.open(Request(self.url + "/action", data=json.dumps(payload).encode(), headers=headers))

    def test_callback_allows_cross_site_navigation_but_never_reflects_secrets(self):
        calls = []
        def callback(query):
            calls.append(query)
            return {"isSuccess": True, "code": "private-code"}
        self.client.complete_callback = callback
        request = Request(self.url + "/oauth/callback/google-drive?state=s&code=private-code&scope=drive",
                          headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
        with self.opener.open(request) as response:
            body = response.read().decode()
            self.assertIn("return to the test app", body)
            self.assertNotIn("private-code", body)
            self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual([{"state": "s", "code": "private-code"}], calls)

    def test_callback_forwards_denial_without_informational_provider_fields(self):
        calls = []
        self.client.complete_callback = lambda query: calls.append(query) or {"isSuccess": False}
        with self.assertRaises(HTTPError) as error:
            self.opener.open(self.url + "/oauth/callback/google-drive?state=s&error=access_denied&error_description=private-detail&error_uri=https%3A%2F%2Fexample.com")
        self.assertEqual(400, error.exception.code)
        self.assertNotIn("private-detail", error.exception.read().decode())
        error.exception.close()
        self.assertEqual([{"state": "s", "error": "access_denied"}], calls)

    def test_callback_rejects_duplicate_keys_bad_hosts_and_subresource_requests(self):
        calls = []
        self.client.complete_callback = lambda query: calls.append(query) or {"isSuccess": True}
        for path, headers in (("?state=s&state=t&code=c", {}),
                              ("?state=s&code=c", {"Host": "evil.invalid"}),
                              ("?state=s&code=c", {"Sec-Fetch-Mode": "no-cors"}),
                              ("?state=s&code=c&unexpected=x", {}),
                              ("?state=s&code=" + "x" * 17000, {})):
            with self.subTest(headers=headers, length=len(path)):
                with self.assertRaises(HTTPError) as error:
                    self.opener.open(Request(self.url + "/oauth/callback/google-drive" + path, headers=headers))
                error.exception.close()
        self.assertEqual([], calls)

    def test_callback_failure_and_exception_do_not_report_success(self):
        for behavior in (lambda query: {"isSuccess": False}, lambda query: {"isSuccess": "true"}):
            self.client.complete_callback = behavior
            with self.assertRaises(HTTPError) as error:
                self.opener.open(self.url + "/oauth/callback/google-drive?state=s&code=c")
            self.assertEqual(400, error.exception.code)
            self.assertNotIn("c=", error.exception.read().decode())
            error.exception.close()

    def test_real_loopback_request_reaches_client(self):
        csrf = self.bootstrap()
        with self.post({"action": "readiness", "payload": {}}, csrf=csrf) as response:
            self.assertTrue(json.load(response)["isSuccess"])
        self.assertEqual([("readiness", {})], self.client.calls)

    def test_cross_origin_request_never_reaches_engine(self):
        csrf = self.bootstrap()
        with self.assertRaises(HTTPError) as error:
            self.post({"action": "start"}, "https://evil.invalid", csrf)
        self.assertEqual(403, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.client.calls)

    def test_missing_csrf_never_reaches_engine(self):
        self.bootstrap()
        with self.assertRaises(HTTPError) as error:
            self.post({"action": "start"})
        self.assertEqual(403, error.exception.code)
        error.exception.close()

    def test_dns_rebinding_host_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.opener.open(Request(self.url + "/", headers={"Host": "attacker.invalid"}))
        self.assertEqual(403, error.exception.code)
        error.exception.close()

    def test_oversized_request_is_rejected(self):
        csrf = self.bootstrap()
        with self.assertRaises(HTTPError) as error:
            self.post({"action": "assets", "payload": {"collectionRef": "x" * 17000}}, csrf=csrf)
        self.assertEqual(413, error.exception.code)
        error.exception.close()

    def test_unknown_action_is_rejected(self):
        csrf = self.bootstrap()
        with self.assertRaises(HTTPError) as error:
            self.post({"action": "delete"}, csrf=csrf)
        self.assertEqual(400, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.client.calls)

    def test_payload_cannot_override_scope_or_exceed_schema(self):
        csrf = self.bootstrap()
        for payload in ({"tenantId": "other"}, {"assetId": "x" * 4097}, {"connectionId": 3}):
            with self.subTest(payload_type=list(payload)):
                with self.assertRaises(HTTPError) as error:
                    self.post({"action": "assets", "payload": payload}, csrf=csrf)
                self.assertEqual(400, error.exception.code)
                error.exception.close()
        self.assertEqual([], self.client.calls)

    def test_unknown_request_property_is_rejected(self):
        csrf = self.bootstrap()
        with self.assertRaises(HTTPError) as error:
            self.post({"action": "readiness", "baseUrl": "https://evil.invalid"}, csrf=csrf)
        self.assertEqual(400, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.client.calls)

    def test_session_requires_cookie(self):
        with self.assertRaises(HTTPError) as error:
            self.opener.open(self.url + "/session")
        self.assertEqual(403, error.exception.code)
        error.exception.close()

    def test_engine_exception_is_not_exposed(self):
        def fail(*args):
            raise RuntimeError("access_token=private")
        self.client.execute = fail
        csrf = self.bootstrap()
        with self.post({"action": "readiness"}, csrf=csrf) as response:
            body = response.read().decode()
        self.assertNotIn("private", body)
        self.assertIn("diagnostic_request_failed", body)


if __name__ == "__main__":
    unittest.main()
