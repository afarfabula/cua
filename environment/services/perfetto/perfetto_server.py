#!/usr/bin/env python3
"""Perfetto UI server on Unix socket with SO_PEERCRED UID enforcement.

Serves offline Perfetto UI (.wasm) and trace files via a Unix socket
(/run/perfetto.sock). Uses SO_PEERCRED to verify the connecting process's
UID — only root is allowed. Socket file is chmod 600 (agent can't connect).
"""
import http.server
import os
import pathlib
import socket
import socketserver
import struct
import sys

AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))
SOCKET_PATH = os.environ.get("PERFETTO_SOCKET", "/run/perfetto.sock")
UI_DIR = os.environ.get("PERFETTO_UI_DIR", "/opt/perfetto-ui")
TRACE_DIR = pathlib.Path(os.environ.get("TRACE_DIR", "/opt/services/traces"))


class PerfettoHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }

    def setup(self):
        super().setup()
        self._peer_uid = -1
        try:
            cred = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
            self._peer_uid, _, _ = struct.unpack("iII", cred)
        except Exception:
            pass

    def _check_uid(self):
        # Socket file is chmod 600 root-owned — agent can't connect at all.
        # SO_PEERCRED is defense-in-depth: also reject if UID matches agent.
        if self._peer_uid == AGENT_UID:
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"403: Agent terminal blocked")
            return False
        return True

    def do_GET(self):
        if not self._check_uid():
            return
        # Serve trace files
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/traces/"):
            rel = path[len("/traces/"):]
            trace_file = (TRACE_DIR / rel).resolve()
            try:
                data = trace_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (OSError, FileNotFoundError):
                self.send_error(404, "Trace not found")
            return
        # Serve UI files
        clean_path = path.lstrip("/")
        if not clean_path:
            clean_path = "index.html"
        ui_file = pathlib.Path(UI_DIR) / clean_path
        try:
            data = ui_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, FileNotFoundError):
            self.send_error(404, f"Not found: {path}")
        except Exception as e:
            print(f"[perfetto] error serving {path}: {e}", file=sys.stderr, flush=True)
            self.send_error(500, str(e))

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress default logging


class UnixSocketHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        super().server_bind()
        os.chmod(self.server_address, 0o600)


if __name__ == "__main__":
    srv = UnixSocketHTTPServer(SOCKET_PATH, PerfettoHandler)
    print(f"Serving Perfetto on {SOCKET_PATH} (root-only, block UID={AGENT_UID})", flush=True)
    srv.serve_forever()
