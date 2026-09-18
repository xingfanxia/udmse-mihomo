"""Focused admin boundary checks; no router, systemd, or engine is touched."""
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("admin_server", REPO / "admin-server.py")
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)
TOKEN = "ab" * 32
ENGINE_SECRET = "cd" * 32


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config.yaml"
        self.config.write_text('mode: rule\nsecret: "' + ENGINE_SECRET + '"\nsubscription: https://private.invalid/secret-token\n')
        self.scope = "SCOPE=devices\nLAN_INTERFACES=(br0)\nDEVICE_IPV4='192.168.1.20'\nDEVICE_MAC='aa:bb:cc:dd:ee:ff'\nSOURCE_CIDRS=()\n"
        (self.root / "routing.env").write_text(self.scope)
        self.controller = admin.Controller(self.root, self.state, self.root / "lifecycle.lock")
        self.engine = self.routing = False
        self.actual_mode = "rule"
        self.global_selection = "DIRECT"
        self.selected = "Hong Kong test"
        self.commands = []
        self.api_calls = []
        self.controller.run = self.run_action
        self.controller.api = self.engine_api

    def tearDown(self):
        self.temp.cleanup()

    def run_action(self, args, timeout=3):
        self.commands.append((args, timeout))
        if args[0] == "systemctl":
            return self.engine
        if args[0].endswith("mihomo-routing.sh"):
            self.assertEqual(args[1:], ["check"])
            return self.routing
        if args[1] == "start":
            self.engine = self.routing = True
            (self.state / "wanted").touch()
            (self.state / "expires_at").write_text(str(int(time.time()) + int(args[2]) * 60))
            self.controller.apply_mode(args[3])
        elif args[1] == "stop":
            self.engine = self.routing = False
            (self.state / "wanted").unlink(missing_ok=True)
        else:
            self.fail("Unexpected command")
        return True

    def engine_api(self, method, path, body=None):
        self.api_calls.append((method, path, body))
        if (method, path) == ("PUT", "/proxies/GLOBAL"):
            self.global_selection = body["name"]
        elif (method, path) == ("PATCH", "/configs"):
            self.actual_mode = body["mode"]
        elif (method, path) == ("GET", "/configs"):
            return {"mode": self.actual_mode, "secret": ENGINE_SECRET}
        elif (method, path) == ("GET", "/proxies/GLOBAL"):
            return {"now": self.global_selection}
        elif (method, path) == ("GET", "/proxies/PROXY"):
            return {"now": self.selected, "connections": [{"password": "do-not-return"}]}
        else:
            self.fail("Unexpected engine endpoint")
        return {}

    def test_start_stop_fixed_arguments_and_global_order(self):
        current = self.controller.control({"enabled": True, "mode": "global"})
        self.assertEqual(current["state"], "active")
        self.assertEqual(self.commands[0], ([str(self.root / "20-mihomo.sh"), "start", "60", "global"], 90))
        self.assertEqual(self.api_calls[0], ("PUT", "/proxies/GLOBAL", {"name": "PROXY"}))
        self.assertEqual(self.api_calls[1], ("GET", "/proxies/GLOBAL", None))
        self.assertEqual(self.api_calls[2], ("PATCH", "/configs", {"mode": "global"}))
        self.assertEqual(admin.scalar(self.config.read_text(), "mode"), "global")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        current = self.controller.control({"enabled": False, "mode": "rule"})
        self.assertEqual(current["state"], "direct")
        self.assertEqual(current["mode"], "global")  # Off retains the preference.
        self.assertIn(([str(self.root / "20-mihomo.sh"), "stop"], 90), self.commands)

    def test_active_mode_switch_preserves_deadline(self):
        self.controller.control({"enabled": True, "mode": "rule", "minutes": 15})
        before = (self.state / "expires_at").read_text()
        self.commands.clear()
        self.controller.control({"enabled": True, "mode": "global", "minutes": 1440})
        self.assertEqual((self.state / "expires_at").read_text(), before)
        self.assertFalse(any("20-mihomo.sh" in call[0][0] for call in self.commands))

    def test_invalid_fields_types_and_injection_never_execute(self):
        invalid = [None, [], {}, {"enabled": True}, {"enabled": 1, "mode": "rule"},
                   {"enabled": True, "mode": "rule; touch /tmp/no"},
                   {"enabled": True, "mode": "direct"},
                   {"enabled": True, "mode": "global", "path": "/etc/passwd"}]
        invalid += [{"enabled": True, "mode": "rule", "minutes": value} for value in (True, 0, 1441, "60", 1.5)]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(admin.AdminError) as caught:
                self.controller.control(body)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.api_calls, [])

    def test_nonblocking_control_lock(self):
        self.controller.control_lock.acquire()
        try:
            with self.assertRaises(admin.AdminError) as caught:
                self.controller.control({"enabled": False, "mode": "rule"})
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(self.commands, [])
        finally:
            self.controller.control_lock.release()

    def test_active_change_obeys_lifecycle_lock(self):
        self.controller.control({"enabled": True, "mode": "rule"})
        with self.controller.lifecycle_lock.open("a") as lock:
            admin.fcntl.flock(lock, admin.fcntl.LOCK_EX | admin.fcntl.LOCK_NB)
            with self.assertRaises(admin.AdminError) as caught:
                self.controller.control({"enabled": True, "mode": "global"})
            self.assertEqual(caught.exception.code, 409)
        self.assertEqual(self.actual_mode, "rule")

    def test_global_selection_failure_does_not_activate_or_persist(self):
        original = self.engine_api
        def wrong_selection(method, path, body=None):
            if (method, path) == ("GET", "/proxies/GLOBAL"):
                return {"now": "DIRECT"}
            return original(method, path, body)
        self.controller.api = wrong_selection
        with self.assertRaises(admin.AdminError):
            self.controller.apply_mode("global")
        self.assertEqual(self.actual_mode, "rule")
        self.assertEqual(admin.scalar(self.config.read_text(), "mode"), "rule")

    def test_persist_failure_never_reports_success(self):
        with mock.patch.object(self.controller, "persist_mode", side_effect=OSError("private secret")):
            with self.assertRaises(admin.AdminError) as caught:
                self.controller.apply_mode("global")
        self.assertNotIn("private secret", caught.exception.message)

    def test_status_prefers_snapshot_and_exposes_only_allowlisted_fields(self):
        self.controller.control({"enabled": True, "mode": "rule"})
        (self.state / "scope").write_text(self.scope.replace("192.168.1.20", "192.168.1.21"))
        status = self.controller.status()
        self.assertEqual(status["scope"]["device_ipv4"], "192.168.1.21")
        self.assertEqual(status["selected_proxy"], self.selected)
        response = json.dumps(status)
        for private in [ENGINE_SECRET, "private.invalid", "do-not-return"]:
            self.assertNotIn(private, response)
        self.selected = "https://private.invalid/profile"
        self.assertIsNone(self.controller.status()["selected_proxy"])

    def test_scope_substitution_is_never_executed(self):
        sentinel = self.root / "executed"
        (self.root / "routing.env").write_text(self.scope.replace("'192.168.1.20'", '"$(touch ' + str(sentinel) + ')"'))
        self.assertIsNotNone(self.controller.status()["message"])
        self.assertFalse(sentinel.exists())

    def test_token_file_permissions_and_symlink(self):
        path = self.root / "admin-token"
        path.write_text(TOKEN)
        path.chmod(0o600)
        self.assertEqual(admin.load_token(path), TOKEN)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            admin.load_token(path)
        link = self.root / "token-link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            admin.load_token(link)


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.assets = Path(self.temp.name)
        for name in ("index.html", "app.js", "style.css"):
            (self.assets / name).write_text("fixture")
        self.controller = mock.Mock()
        self.controller.status.return_value = {"enabled": False, "state": "direct"}
        self.controller.control.return_value = {"enabled": True, "state": "active"}
        self.server = admin.make_server(self.controller, TOKEN, self.assets, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = "127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method="GET", path="/api/status", body=None, headers=None):
        given = {"Host": self.host, "Authorization": "Bearer " + TOKEN}
        if method == "POST":
            given.update({"Origin": "http://" + self.host, "Content-Type": "application/json"})
        for key, value in (headers or {}).items():
            if value is None:
                given.pop(key, None)
            else:
                given[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=given)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_auth_missing_bad_and_url_token_rejected(self):
        for header in (None, "Bearer bad", "Basic " + TOKEN):
            self.assertEqual(self.request(headers={"Authorization": header})[0], 401)
        self.assertEqual(self.request(path="/api/status?token=" + TOKEN, headers={"Authorization": None})[0], 401)
        self.assertEqual(self.request(headers={"Authorization": None, "Cookie": "token=" + TOKEN})[0], 401)
        self.controller.status.assert_not_called()

    def test_host_origin_and_csrf_checks(self):
        for host in ("attacker.invalid", "127.0.0.1:9999", self.host + ".attacker.invalid"):
            self.assertEqual(self.request(headers={"Host": host})[0], 403)
        for origin in (None, "null", "https://" + self.host, "http://attacker.invalid"):
            self.assertEqual(self.request("POST", "/api/control", b"{}", {"Origin": origin})[0], 403)
        self.controller.control.assert_not_called()
        self.assertEqual(self.request(headers={"Host": "localhost:" + str(self.server.server_port)})[0], 200)

    def test_body_limits_json_and_methods(self):
        for body in (b"{", b'{"enabled":true,"enabled":false}', b'{"minutes":NaN}', b"\xff"):
            self.assertEqual(self.request("POST", "/api/control", body)[0], 400)
        self.assertEqual(self.request("POST", "/api/control", b"x" * 2049)[0], 413)
        self.assertEqual(self.request("POST", "/api/control", b"{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/control", b"{}", {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request("PUT", "/api/control", b"{}")[0], 405)
        self.assertEqual(self.request("OPTIONS", "/api/control")[0], 405)
        self.controller.control.assert_not_called()

    def test_control_returns_actual_status_and_fixed_failure(self):
        body = b'{"enabled":true,"mode":"global"}'
        self.assertEqual(self.request("POST", "/api/control", body)[0], 200)
        self.controller.control.assert_called_once_with({"enabled": True, "mode": "global"})
        self.controller.control.side_effect = admin.AdminError(502, "The change could not be confirmed.")
        code, _, raw = self.request("POST", "/api/control", body)
        self.assertEqual(code, 502)
        self.assertEqual(json.loads(raw)["status"], self.controller.status.return_value)
        self.assertNotIn(TOKEN.encode(), raw)

    def test_unexpected_errors_and_failed_status_refresh_are_sanitized(self):
        body = b'{"enabled":true,"mode":"global"}'
        self.controller.control.side_effect = RuntimeError("https://private.invalid/token=" + TOKEN)
        code, _, raw = self.request("POST", "/api/control", body)
        self.assertEqual(code, 502)
        self.assertNotIn(b"private.invalid", raw)
        self.assertNotIn(TOKEN.encode(), raw)
        self.controller.control.side_effect = admin.AdminError(502, "The change could not be confirmed.")
        self.controller.status.side_effect = RuntimeError(ENGINE_SECRET)
        code, _, raw = self.request("POST", "/api/control", body)
        self.assertEqual(code, 502)
        self.assertNotIn(ENGINE_SECRET.encode(), raw)

    def test_unknown_method_still_requires_api_auth(self):
        self.assertEqual(self.request("UNSUPPORTED", headers={"Authorization": None})[0], 401)
        self.assertEqual(self.request("UNSUPPORTED")[0], 405)

    def test_static_allowlist_no_cors_and_csp(self):
        code, headers, raw = self.request(path="/", headers={"Authorization": None})
        self.assertEqual(code, 200)
        self.assertEqual(raw, b"fixture")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ("/../config.yaml", "/%2e%2e/config.yaml", "/admin-token", "/config.yaml", "/app.js?token=private"):
            self.assertEqual(self.request(path=path)[0], 404)
        (self.assets / "favicon.svg").symlink_to(self.assets / "index.html")
        self.assertEqual(self.request(path="/favicon.svg")[0], 404)


if __name__ == "__main__":
    unittest.main()
