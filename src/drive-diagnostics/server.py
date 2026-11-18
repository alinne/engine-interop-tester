"""Loopback-only presentation bridge; Engine remains the connection authority."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit, parse_qs

WEB = Path(__file__).with_name("web")
ACTIONS = frozenset(("readiness", "start", "status", "connections", "collections", "assets", "acquire"))


def make_server(client, port=8765):
    cookie = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            # Provider state, file names and account identifiers never enter access logs.
            pass

        def send_body(self, status, body, content_type="application/json", set_cookie=False):
            data = json.dumps(body).encode() if content_type == "application/json" else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header("Connection", "close")
            if set_cookie:
                self.send_header("Set-Cookie", f"drive_session={cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def reject(self, status=403, reason="diagnostic_request_denied"):
            self.send_body(status, {"isSuccess": False, "reasonCodes": [reason]})

        def host_allowed(self):
            return self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]

        def has_cookie(self):
            try:
                parsed = SimpleCookie(self.headers.get("Cookie", ""))
                value = parsed.get("drive_session")
                return value is not None and secrets.compare_digest(value.value, cookie)
            except Exception:
                return False

        def do_GET(self):
            if not self.host_allowed():
                return self.reject()
            # Only the fixed OAuth return accepts a cross-site navigation.
            # Its Engine-issued state replaces the Strict cookie here only.
            if urlsplit(self.path).path == "/oauth/callback/google-drive":
                return self.oauth_callback()
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.reject()
            if self.path == "/session":
                if not self.has_cookie():
                    return self.reject()
                return self.send_body(200, {"csrf": csrf})
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in assets:
                return self.reject(404, "diagnostic_route_not_found")
            filename, media_type = assets[self.path]
            self.send_body(200, (WEB / filename).read_bytes(), media_type, set_cookie=self.path == "/")

        def oauth_callback(self):
            if len(self.path) > 16384 or self.headers.get("Sec-Fetch-Mode") not in (None, "navigate"):
                return self.reject(400)
            try:
                parsed = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=12)
                # Google may append these informational fields; they confer no
                # authority and are discarded rather than forwarded to Engine.
                allowed = {"state", "code", "error", "error_description", "error_uri", "scope", "authuser", "prompt", "hd", "iss"}
                if set(parsed) - allowed or any(len(values) != 1 or len(values[0]) > 4096 for values in parsed.values()):
                    return self.reject(400)
                # Google's optional issuer identifies this fixed provider; it
                # never selects an endpoint or changes Engine's state binding.
                if "iss" in parsed and parsed["iss"] != ["https://accounts.google.com"]:
                    return self.reject(400)
                query = {key: values[0] for key, values in parsed.items() if key in {"state", "code", "error"}}
                result = client.complete_callback(query)
                success = result.get("isSuccess") is True
            except Exception:
                success = False
            text = (b"Google sign-in returned successfully. You can return to the test app and check status.\n"
                    if success else b"Google sign-in did not complete. Return to the test app and start a new sign-in attempt.\n")
            self.send_body(200 if success else 400, text, "text/plain; charset=utf-8")

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.host_allowed() or not self.has_cookie()
                    or self.headers.get_all("Origin") != [expected_origin]
                    or not secrets.compare_digest(self.headers.get("X-Drive-CSRF", ""), csrf)):
                return self.reject()
            if self.path != "/action":
                return self.reject(404, "diagnostic_route_not_found")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                return self.reject(400)
            if length < 0 or length > 16384 or self.headers.get("Transfer-Encoding"):
                return self.reject(413, "diagnostic_request_too_large")
            if self.headers.get_content_type() != "application/json":
                return self.reject(415)
            try:
                request = json.loads(self.rfile.read(length))
                if (not isinstance(request, dict) or set(request) - {"action", "payload"}
                        or not isinstance(request.get("action"), str) or request["action"] not in ACTIONS):
                    return self.reject(400, "diagnostic_action_not_allowed")
                payload = request.get("payload", {})
                if (not isinstance(payload, dict) or set(payload) - {"connectionId", "collectionRef", "assetId"}
                        or any(not isinstance(value, str) or len(value) > 4096 for value in payload.values())):
                    return self.reject(400)
            except (ValueError, TimeoutError):
                return self.reject(400)
            try:
                result = client.execute(request["action"], payload)
            except Exception:
                result = {"isSuccess": False, "reasonCodes": ["diagnostic_request_failed"]}
            self.send_body(200, result)

    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    from engine_client import Config, DriveClient
    class UnconfiguredClient:
        def execute(self, action, payload):
            return {"isSuccess": False, "reasonCodes": ["diagnostic_configuration_required"]}
    try:
        configuration = Config.from_env()
        client = DriveClient(configuration)
    except ValueError:
        client = UnconfiguredClient()
        print("Engine configuration missing/invalid; UI inspection only. See docs/drive-diagnostics.md.")
    try:
        application = make_server(client)
    except OSError:
        raise SystemExit("Diagnostic port unavailable.") from None
    print("Drive diagnostic: http://127.0.0.1:8765 — Ctrl+C to stop. No live-account qualification is claimed.")
    try:
        application.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.server_close()
