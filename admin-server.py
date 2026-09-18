#!/usr/bin/env python3
"""Loopback-only administrator for a scoped Mihomo trial (Python 3.9+)."""
import argparse
import fcntl
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


class AdminError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid JSON constant")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def scalar(text, key):
    values = re.findall(r"^" + re.escape(key) + r":[ \t]*(.*)$", text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError("Missing or duplicate configuration field")
    value = values[0].strip()
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("Configuration field must be a string")
        return parsed
    tokens = shlex.split(value, comments=True, posix=True)
    if len(tokens) != 1:
        raise ValueError("Configuration field must be a scalar")
    return tokens[0]


def load_token(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError("Admin token must be an owner-only regular file")
        token = stream.read(130).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Invalid admin token")
    return token


def read_scope(path):
    fields = {}
    allowed = {"SCOPE", "LAN_INTERFACES", "DEVICE_IPV4", "DEVICE_MAC", "SOURCE_CIDRS"}
    for line in path.read_text().splitlines():
        match = re.match(r"^[ \t]*([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if not match or match[1] not in allowed:
            continue
        key, value = match.groups()
        if key in fields:
            raise ValueError("Duplicate scope field")
        if key in {"LAN_INTERFACES", "SOURCE_CIDRS"}:
            # An array of literal words, optionally followed by a comment.
            array = re.fullmatch(r"\(([^()]*)\)[ \t]*(?:#.*)?", value.strip())
            if not array:
                raise ValueError("Unsupported scope array")
            fields[key] = shlex.split(array[1], comments=True, posix=True)
        else:
            words = shlex.split(value, comments=True, posix=True)
            if len(words) != 1:
                raise ValueError("Unsupported scope scalar")
            fields[key] = words[0]
    scope_type = fields.get("SCOPE", "devices")
    interfaces = fields.get("LAN_INTERFACES", [])
    device_ip = fields.get("DEVICE_IPV4", "")
    device_mac = fields.get("DEVICE_MAC", "")
    networks = fields.get("SOURCE_CIDRS", [])
    if scope_type not in {"devices", "networks"} or not all(re.fullmatch(r"br[0-9]+", value) for value in interfaces):
        raise ValueError("Invalid scope")
    if device_ip:
        ipaddress.IPv4Address(device_ip)
    if device_mac and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", device_mac):
        raise ValueError("Invalid device MAC")
    for network in networks:
        ipaddress.IPv4Network(network, strict=True)
    return {"type": scope_type, "interfaces": interfaces, "device_ipv4": device_ip,
            "device_mac": device_mac, "networks": networks}


class Controller:
    def __init__(self, root=Path("/data/mihomo"), state=Path("/run/mihomo-routing"),
                 lifecycle_lock=Path("/run/lock/udmse-mihomo-install.lock")):
        self.root = Path(root)
        self.state = Path(state)
        self.lifecycle_lock = Path(lifecycle_lock)
        self.control_lock = threading.Lock()

    def run(self, arguments, timeout=3):
        # No shell and no command output is returned or logged. Kill the entire
        # subprocess group on timeout, including children of lifecycle scripts.
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            return process.wait(timeout=timeout) == 0
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return False

    def api(self, method, path, payload=None):
        # Fixed local endpoint: no redirects, proxy environment, or caller URL.
        secret = scalar((self.root / "config.yaml").read_text(), "secret")
        connection = http.client.HTTPConnection("127.0.0.1", 9090, timeout=3)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            connection.request(method, path, body=body, headers={
                "Authorization": "Bearer " + secret, "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status not in (200, 204) or len(raw) > 65536:
                raise ValueError("Engine request failed")
            parsed = strict_json(raw) if raw else {}
            if not isinstance(parsed, dict):
                raise ValueError("Unexpected engine response")
            return parsed
        finally:
            connection.close()

    def persist_mode(self, mode):
        path = self.root / "config.yaml"
        text = path.read_text()
        scalar(text, "mode")  # Require one existing top-level mode.
        text = re.sub(r"^mode:[^\n]*$", "mode: " + mode, text, flags=re.MULTILINE)
        fd, temporary = tempfile.mkstemp(prefix=".admin-config-", dir=str(self.root))
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def apply_mode(self, mode):
        if mode not in {"rule", "global"}:
            raise AdminError(400, "Choose smart or global mode.")
        try:
            if mode == "global":
                self.api("PUT", "/proxies/GLOBAL", {"name": "PROXY"})
                if self.api("GET", "/proxies/GLOBAL").get("now") != "PROXY":
                    raise ValueError("Global proxy selection did not take effect")
            self.api("PATCH", "/configs", {"mode": mode})
            if self.api("GET", "/configs").get("mode") != mode:
                raise ValueError("Mode did not take effect")
            if mode == "global" and self.api("GET", "/proxies/GLOBAL").get("now") != "PROXY":
                raise ValueError("Global proxy selection changed")
            self.persist_mode(mode)
        except (OSError, ValueError, http.client.HTTPException):
            raise AdminError(502, "Could not apply the mode. Check the current status.") from None

    def status(self):
        enabled = (self.state / "wanted").is_file()
        message = None
        mode = "rule"
        try:
            stored = scalar((self.root / "config.yaml").read_text(), "mode")
            if stored not in {"rule", "global"}:
                raise ValueError("Unsupported mode")
            mode = stored
        except (OSError, ValueError):
            message = "The saved mode could not be read."
        try:
            source = self.state / "scope"
            scope = read_scope(source if source.exists() else self.root / "routing.env")
        except (OSError, ValueError):
            scope = {"type": "devices", "interfaces": [], "device_ipv4": "", "device_mac": "", "networks": []}
            message = "The selected devices could not be read."
        engine = routing = False
        try:
            engine = self.run(["systemctl", "is-active", "--quiet", "mihomo.service"])
        except OSError:
            message = "The service status could not be checked."
        try:
            routing = self.run([str(self.root / "mihomo-routing.sh"), "check"])
        except OSError:
            message = "The routing status could not be checked."
        selected = None
        api_ok = not engine
        if engine:
            try:
                actual = self.api("GET", "/configs").get("mode")
                if not isinstance(actual, str) or actual not in {"rule", "global"}:
                    raise ValueError("Unsupported running mode")
                mode = actual
                name = self.api("GET", "/proxies/PROXY").get("now")
                if isinstance(name, str) and 0 < len(name) <= 128 and name.isprintable() and "://" not in name:
                    # Only a group's selected label is exposed, never the full
                    # provider, node definitions, subscription URL or connections.
                    secret = scalar((self.root / "config.yaml").read_text(), "secret")
                    if secret not in name:
                        selected = name
                if mode == "global" and self.api("GET", "/proxies/GLOBAL").get("now") != "PROXY":
                    raise ValueError("Global selection is not the proxy group")
                api_ok = True
            except (OSError, ValueError, http.client.HTTPException):
                message = "The running proxy mode could not be verified."
        remaining = None
        if enabled:
            try:
                deadline = (self.state / "expires_at").read_text().strip()
                if not re.fullmatch(r"[0-9]{1,12}", deadline):
                    raise ValueError("Invalid deadline")
                remaining = max(0, int(deadline) - int(time.time()))
            except (OSError, ValueError):
                message = "The automatic stop time could not be read."
        if enabled and routing and engine and api_ok and remaining is not None and remaining > 0 and message is None:
            state = "active"
        elif not enabled and not routing and not engine and message is None:
            state = "direct"
        else:
            state = "degraded"
            message = message or "Proxy routing is not fully active. Check or stop the trial."
        return {"enabled": enabled, "routing_active": routing, "engine_active": engine,
                "mode": mode, "state": state, "remaining_seconds": remaining,
                "scope": scope, "selected_proxy": selected, "message": message}

    def control(self, body):
        if not isinstance(body, dict) or set(body) - {"enabled", "mode", "minutes"} or not {"enabled", "mode"} <= set(body):
            raise AdminError(400, "Provide enabled, mode, and optional minutes only.")
        enabled, mode, minutes = body["enabled"], body["mode"], body.get("minutes", 60)
        if type(enabled) is not bool or not isinstance(mode, str) or mode not in {"rule", "global"}:
            raise AdminError(400, "Invalid enabled or mode value.")
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise AdminError(400, "Choose a duration from 1 to 1440 minutes.")
        if not self.control_lock.acquire(blocking=False):
            raise AdminError(409, "Another change is in progress.")
        try:
            if not enabled:
                success = self.run([str(self.root / "20-mihomo.sh"), "stop"], timeout=90)
            elif (self.state / "wanted").is_file():
                # Lifecycle scripts hold this same lock themselves. Only an
                # in-place mode change takes it here; never extend the timer.
                with self.lifecycle_lock.open("a") as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise AdminError(409, "Another change is in progress.") from None
                    if not (self.state / "wanted").is_file():
                        raise AdminError(409, "The trial stopped. Refresh before starting again.")
                    self.apply_mode(mode)
                success = True
            else:
                success = self.run([str(self.root / "20-mihomo.sh"), "start", str(minutes), mode], timeout=90)
            current = self.status()
            verified = (current["enabled"] and current["routing_active"] and current["engine_active"] and current["mode"] == mode and current["state"] == "active") if enabled else not any(current[key] for key in ("enabled", "routing_active", "engine_active"))
            if not success or not verified:
                raise AdminError(502, "The change could not be confirmed. Check the current status.")
            return current
        except (OSError, ValueError, http.client.HTTPException):
            raise AdminError(502, "The change could not be completed. Check the current status.") from None
        finally:
            self.control_lock.release()


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class Handler(BaseHTTPRequestHandler):
    server_version = "MihomoAdmin"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass  # Request paths and error details can contain private input.

    def reply(self, code, body, content_type="application/json"):
        data = json.dumps(body, ensure_ascii=False).encode() if content_type == "application/json" else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def dispatch(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}:
            raise AdminError(403, "This page is available only through its local address.")
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != "http://" + hosts[0]):
            raise AdminError(403, "Requests must come from this page.")
        is_api = self.path.startswith("/api")
        if is_api:
            credentials = self.headers.get_all("Authorization", [])
            expected = "Bearer " + self.server.token
            if len(credentials) != 1 or not hmac.compare_digest(credentials[0].encode(), expected.encode()):
                raise AdminError(401, "Enter the administrator token.")
        if self.command == "GET" and self.path == "/api/status":
            return self.reply(200, self.server.controller.status())
        if self.command == "POST" and self.path == "/api/control":
            if len(origins) != 1:
                raise AdminError(403, "Requests must come from this page.")
            if self.headers.get_all("Transfer-Encoding"):
                raise AdminError(400, "Unsupported request encoding.")
            if self.headers.get_all("Content-Type", []) != ["application/json"]:
                raise AdminError(415, "Use an application/json request.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise AdminError(400, "A valid request length is required.")
            size = int(lengths[0])
            if size > 2048:
                raise AdminError(413, "The request is too large.")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise AdminError(400, "Incomplete request.")
            try:
                body = strict_json(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise AdminError(400, "Invalid JSON request.") from None
            return self.reply(200, self.server.controller.control(body))
        if self.path in {"/api/status", "/api/control"} or self.command not in {"GET", "POST"}:
            raise AdminError(405, "This method is not available.")
        if self.command == "GET" and self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            path = self.server.asset_dir / filename
            if path.is_symlink() or not path.is_file():
                raise AdminError(404, "Page not found.")
            return self.reply(200, path.read_bytes(), mime)
        raise AdminError(404, "Page not found.")

    def handle_request(self):
        try:
            try:
                self.dispatch()
            except AdminError as error:
                response = {"error": error.message}
                if error.code in {409, 502}:
                    response["status"] = self.server.controller.status()
                self.reply(error.code, response)
        except Exception:
            # This HTTP boundary never exposes exception text or traceback,
            # including a failed status refresh after a rejected operation.
            self.reply(502, {"error": "The local service could not complete the request."})

    def __getattr__(self, name):
        # Unknown HTTP verbs still pass the same Host/auth checks before 405.
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(name)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_request


def make_server(controller, token, asset_dir, port=9088):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.controller = controller
    server.token = token
    server.asset_dir = Path(asset_dir)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/mihomo"))
    parser.add_argument("--state", type=Path, default=Path("/run/mihomo-routing"))
    parser.add_argument("--port", type=int, default=9088)
    parser.add_argument("--apply-mode", choices=("rule", "global"))
    args = parser.parse_args()
    controller = Controller(args.root, args.state)
    try:
        if args.apply_mode:
            # The bootstrap already owns the lifecycle lock; acquiring again
            # in this subprocess would deadlock its caller.
            controller.apply_mode(args.apply_mode)
            return 0
        token = load_token(args.root / "admin-token")
        server = make_server(controller, token, args.root / "admin", args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except (AdminError, OSError, ValueError):
        print("Admin operation failed; check local installation and permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
