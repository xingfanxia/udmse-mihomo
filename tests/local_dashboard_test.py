"""Local bridge trust boundary checks; no router or credential vault is touched."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_dashboard", REPO / "local-dashboard.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
TOKEN = "test-only-upstream-credential"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        self.send_response(self.server.code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "private=never-forward")
        self.send_header("Location", "http://attacker.invalid/")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)

    do_GET = do_POST = handle_request


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.requests = []
        self.upstream.code = 200
        self.upstream.body = b'{"state":"direct"}'
        self.server = bridge.make_server(TOKEN, port=0, upstream_port=self.upstream.server_port)
        self.threads = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.host = "127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, method="GET", path="/api/status", body=None, headers=None, extra=()):
        given = {"Host": self.host, "Authorization": "Bearer " + self.server.local_token}
        if method == "POST":
            given.update({"Origin": "http://" + self.host, "Content-Type": "application/json"})
        if body is not None:
            given["Content-Length"] = str(len(body))
        for name, value in (headers or {}).items():
            if value is None:
                given.pop(name, None)
            else:
                given[name] = value
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in list(given.items()) + list(extra):
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_session_returns_only_ephemeral_local_token(self):
        code, headers, raw = self.request(path="/local-session", headers={"Authorization": None})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"token": self.server.local_token, "auth": "local"})
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertGreaterEqual(len(self.server.local_token), 32)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.upstream.requests, [])
        with bridge.make_server(TOKEN, port=0) as other:
            self.assertNotEqual(other.local_token, self.server.local_token)
            self.assertEqual(other.server_address[0], "127.0.0.1")

    def test_api_needs_local_bearer_not_vault_password_or_cookie(self):
        for authorization in (None, "Bearer bad", "Bearer " + TOKEN):
            with self.subTest(authorization=authorization):
                self.assertEqual(self.request(headers={"Authorization": authorization})[0], 401)
        self.assertEqual(self.request(headers={"Authorization": None,
                                              "Cookie": "token=" + self.server.local_token})[0], 401)
        self.assertEqual(self.request(path="/api/status?token=" + self.server.local_token,
                                      headers={"Authorization": None})[0], 401)
        self.assertEqual(self.upstream.requests, [])

    def test_host_origin_and_fetch_metadata_protect_every_route(self):
        for path in ("/local-session", "/", "/api/status"):
            for host in ("attacker.invalid", self.host + ".evil", "127.0.0.1:1"):
                self.assertEqual(self.request(path=path, headers={"Host": host})[0], 403)
            for origin in ("null", "http://attacker.invalid", "https://" + self.host):
                self.assertEqual(self.request(path=path, headers={"Origin": origin})[0], 403)
            for site in ("cross-site", "same-site", "unknown"):
                self.assertEqual(self.request(path=path, headers={"Sec-Fetch-Site": site})[0], 403)
            for name, value in (("Host", self.host), ("Origin", "http://" + self.host),
                                ("Sec-Fetch-Site", "same-origin")):
                self.assertEqual(self.request(path=path, headers={name: value},
                                              extra=[(name, value)])[0], 403)
        self.assertEqual(self.upstream.requests, [])
        for site in ("same-origin", "none"):
            self.assertEqual(self.request(path="/local-session", headers={"Sec-Fetch-Site": site})[0], 200)
        local = "localhost:" + str(self.server.server_port)
        self.assertEqual(self.request(path="/local-session", headers={"Host": local,
                                     "Origin": "http://" + local})[0], 200)

    def test_forward_swaps_auth_host_origin_and_discards_browser_headers(self):
        body = b'{"enabled":false,"mode":"rule"}'
        code, headers, raw = self.request("POST", "/api/control", body,
                                         {"Cookie": "browser=private", "X-Untrusted": "ignored"})
        self.assertEqual(code, 200)
        method, path, forwarded, sent = self.upstream.requests[0]
        self.assertEqual((method, path, sent), ("POST", "/api/control", body))
        target = "127.0.0.1:9088"
        self.assertNotEqual(self.upstream.server_port, self.server.upstream_host_port)
        self.assertEqual(forwarded["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(forwarded["Host"], target)
        self.assertEqual(forwarded["Origin"], "http://" + target)
        self.assertEqual(forwarded["Content-Type"], "application/json")
        self.assertNotIn(self.server.local_token, str(forwarded))
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("X-Untrusted", forwarded)
        for name in ("Set-Cookie", "Location", "Access-Control-Allow-Origin"):
            self.assertNotIn(name, headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(raw, self.upstream.body)

    def test_post_requires_origin_json_and_bounded_unambiguous_body(self):
        for origin in (None, "http://attacker.invalid"):
            self.assertEqual(self.request("POST", "/api/control", b"{}", {"Origin": origin})[0], 403)
        for content_type in (None, "text/plain", "application/json; charset=utf-8"):
            self.assertEqual(self.request("POST", "/api/control", b"{}",
                                          {"Content-Type": content_type})[0], 415)
        self.assertEqual(self.request("POST", "/api/control", b"x" * 2049)[0], 413)
        self.assertEqual(self.request("POST", "/api/control", b"{}",
                                      {"Content-Length": None})[0], 400)
        for length in ("-1", "+2", "2, 2"):
            self.assertEqual(self.request("POST", "/api/control", b"{}",
                                          {"Content-Length": length})[0], 400)
        for name, value in (("Content-Length", "2"), ("Content-Type", "application/json"),
                            ("Authorization", "Bearer " + self.server.local_token)):
            self.assertEqual(self.request("POST", "/api/control", b"{}", extra=[(name, value)])[0], 400)
        for body in (b"{", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff"):
            self.assertEqual(self.request("POST", "/api/control", body)[0], 400)
        self.assertEqual(self.upstream.requests, [])

    def test_transfer_encoding_or_get_body_never_forwarded(self):
        for path in ("/", "/local-session", "/api/status"):
            self.assertEqual(self.request(path=path, headers={"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(self.request(path=path, body=b"unwanted")[0], 400)
        self.assertEqual(self.upstream.requests, [])

    def test_only_fixed_routes_and_methods_forwarded(self):
        for path in ("/config.yaml", "/admin-token", "/../config.yaml", "/%2e%2e/config.yaml",
                     "/api/connections", "/app.js?x=1", "/local-session?x=1",
                     "http://attacker.invalid/", "//attacker.invalid/"):
            self.assertEqual(self.request(path=path)[0], 404)
        for method, path in (("POST", "/"), ("POST", "/local-session"),
                             ("GET", "/api/control"), ("POST", "/api/status"),
                             ("OPTIONS", "/api/status"), ("PUT", "/api/control")):
            self.assertEqual(self.request(method, path)[0], 405)
        self.assertEqual(self.upstream.requests, [])
        for path in bridge.ASSETS:
            self.assertEqual(self.request(path=path, headers={"Authorization": None})[0], 200)
            self.assertNotIn("Authorization", self.upstream.requests[-1][2])
        self.assertEqual(self.request(path="/api/telemetry")[0], 200)

    def test_redirects_oversized_or_secret_responses_fail_closed(self):
        self.upstream.code = 302
        code, _, raw = self.request()
        self.assertEqual(code, 502)
        self.assertNotIn(b"attacker", raw)
        self.upstream.code = 200
        self.upstream.body = TOKEN.encode()
        code, _, raw = self.request()
        self.assertEqual(code, 502)
        self.assertNotIn(TOKEN.encode(), raw)
        self.upstream.body = b"x" * 100
        with mock.patch.object(bridge, "MAX_RESPONSE", 32):
            self.assertEqual(self.request()[0], 502)

    def test_upstream_connection_failure_is_generic(self):
        with mock.patch.object(bridge.Handler, "forward", side_effect=OSError("private path " + TOKEN)):
            code, _, raw = self.request()
        self.assertEqual(code, 502)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(b"private path", raw)


class TokenTest(unittest.TestCase):
    def test_private_regular_token_file_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN + "\n")
            path.chmod(0o600)
            self.assertEqual(bridge.load_token(path), TOKEN)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                bridge.load_token(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                bridge.load_token(link)
            for invalid in ("short", "with a space", "x" * 129, "x" * 130 + "private"):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    bridge.load_token(path)


if __name__ == "__main__":
    unittest.main()
