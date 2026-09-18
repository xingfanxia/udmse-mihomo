#!/usr/bin/env python3
"""Mac loopback dashboard bridge; the upstream must be an authenticated SSH tunnel."""
import argparse
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys


ASSETS = {
    "/": "text/html; charset=utf-8",
    "/index.html": "text/html; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/telemetry.js": "text/javascript; charset=utf-8",
    "/theme.js": "text/javascript; charset=utf-8",
    "/style.css": "text/css; charset=utf-8",
    "/favicon.svg": "image/svg+xml",
}
API = {"/api/status": "GET", "/api/telemetry": "GET", "/api/control": "POST"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MAX_RESPONSE = 2 * 1024 * 1024


class RequestError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def load_token(path):
    # O_NONBLOCK also prevents a substituted FIFO from hanging startup.
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()):
            raise ValueError("Token must be an owner-only regular file")
        token = stream.read(130).strip()
    if not re.fullmatch(r"[!-~]{10,128}", token):
        raise ValueError("Invalid token")
    return token


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid JSON constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


class Handler(BaseHTTPRequestHandler):
    server_version = "MihomoLocal"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def reply(self, code, body, content_type="application/json"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def guard(self):
        hosts = self.headers.get_all("Host", [])
        port = str(self.server.server_port)
        if len(hosts) != 1 or hosts[0] not in {"127.0.0.1:" + port, "localhost:" + port}:
            raise RequestError(403, "Use the local dashboard address.")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != ["http://" + hosts[0]]:
            raise RequestError(403, "Requests must come from this page.")
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if sites and sites not in (["same-origin"], ["none"]):
            raise RequestError(403, "Requests must come from this page.")
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError(400, "Unsupported request encoding.")
        for name in ("Content-Length", "Content-Type", "Authorization"):
            if len(self.headers.get_all(name, [])) > 1:
                raise RequestError(400, "Ambiguous request headers.")
        lengths = self.headers.get_all("Content-Length", [])
        if lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RequestError(400, "A valid request length is required.")
        if self.command != "POST" and lengths and int(lengths[0]) != 0:
            raise RequestError(400, "Unexpected request body.")
        return origins

    def forward(self, body=None):
        # Fixed loopback destination; http.client ignores proxy environment and
        # does not follow redirects. Never pass browser credentials or cookies.
        # The SSH listener's port differs from the router server's Host check.
        host = "127.0.0.1:" + str(self.server.upstream_host_port)
        headers = {"Host": host, "Connection": "close"}
        if self.path in API:
            headers["Authorization"] = "Bearer " + self.server.upstream_token
            headers["Origin"] = "http://" + host
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.upstream_port,
            timeout=100 if self.command == "POST" else 10)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if (len(raw) > MAX_RESPONSE or not 200 <= response.status <= 599
                    or 300 <= response.status <= 399
                    or self.server.upstream_token.encode() in raw):
                raise RequestError(502, "The dashboard connection is unavailable.")
            # Do not forward cookies, Location, CORS or arbitrary upstream headers.
            content_type = ASSETS.get(self.path, "application/json")
            if response.status != 200:
                content_type = "application/json"
            return self.reply(response.status, raw, content_type)
        finally:
            connection.close()

    def dispatch(self):
        origins = self.guard()
        if self.path.startswith("/api"):
            credentials = self.headers.get_all("Authorization", [])
            expected = "Bearer " + self.server.local_token
            if len(credentials) != 1 or not hmac.compare_digest(credentials[0].encode(), expected.encode()):
                raise RequestError(401, "Reload this page to reconnect.")
        if self.path in API:
            if self.command != API[self.path]:
                raise RequestError(405, "This method is not available.")
            if self.command == "POST":
                if len(origins) != 1:
                    raise RequestError(403, "Requests must come from this page.")
                if self.headers.get_all("Content-Type", []) != ["application/json"]:
                    raise RequestError(415, "Use an application/json request.")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1:
                    raise RequestError(400, "A valid request length is required.")
                size = int(lengths[0])
                if size > 2048:
                    raise RequestError(413, "The request is too large.")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise RequestError(400, "Incomplete request.")
                try:
                    if not isinstance(strict_json(raw), dict):
                        raise ValueError("Expected object")
                except (ValueError, UnicodeError):
                    raise RequestError(400, "Invalid JSON request.") from None
                return self.forward(raw)
            return self.forward()
        if self.path == "/local-session":
            if self.command != "GET":
                raise RequestError(405, "This method is not available.")
            return self.reply(200, {"token": self.server.local_token, "auth": "local"})
        if self.path in ASSETS:
            if self.command != "GET":
                raise RequestError(405, "This method is not available.")
            return self.forward()
        raise RequestError(404, "Page not found.")

    def handle_request(self):
        try:
            self.dispatch()
        except RequestError as error:
            self.reply(error.code, {"error": error.message})
        except (OSError, ValueError, http.client.HTTPException):
            self.reply(502, {"error": "The dashboard connection is unavailable."})
        except Exception:
            self.reply(502, {"error": "The dashboard request could not be completed."})

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(name)


def make_server(token, port=9088, upstream_port=9089, upstream_host_port=9088):
    if (not 0 <= port <= 65535 or not 1 <= upstream_port <= 65535
            or not 1 <= upstream_host_port <= 65535):
        raise ValueError("Invalid port")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.upstream_token = token
    server.local_token = secrets.token_urlsafe(32)
    server.upstream_port = upstream_port
    server.upstream_host_port = upstream_host_port
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9088)
    parser.add_argument("--upstream-port", type=int, default=9089)
    parser.add_argument("--upstream-host-port", type=int, default=9088)
    parser.add_argument("--token-file", type=Path,
                        default=Path.home() / "creds/unifi/udm-admin-token")
    args = parser.parse_args()
    try:
        token = load_token(args.token_file)
        with make_server(token, args.port, args.upstream_port, args.upstream_host_port) as server:
            server.serve_forever()
    except (OSError, ValueError, UnicodeError):
        print("Dashboard startup failed; check the local service and credential permissions.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
