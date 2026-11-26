#!/usr/bin/env python3
"""Static file server for offline Perfetto UI (.wasm MIME type) + trace file serving."""
import http.server
import mimetypes
import os
import pathlib
import socketserver


class PerfettoHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }

    def __init__(self, *args, **kwargs):
        ui_dir = os.environ.get("PERFETTO_UI_DIR", "/opt/perfetto-ui")
        super().__init__(*args, directory=ui_dir, **kwargs)

    def translate_path(self, path):
        if path.startswith("/traces/"):
            rel = path[len("/traces/"):].split("?", 1)[0]
            trace_dir = pathlib.Path(os.environ.get("TRACE_DIR", "/opt/services/traces"))
            return str((trace_dir / rel).resolve())
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(os.environ.get("PERFETTO_PORT", "9001"))
    srv = ReusableTCPServer(("127.0.0.1", port), PerfettoHandler)
    print(f"Serving Perfetto UI on 127.0.0.1:{port}")
    srv.serve_forever()
