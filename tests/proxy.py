"""A minimal reverse proxy for tests: mounts an upstream server under a path
prefix the way a gateway does (doc 03 §3.2, `--public-base`), and can refuse
the QUERY method the way an intermediary that has never heard of it does.

Runs in a thread; nothing here is production code.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}


class Proxy:
    def __init__(self, upstream: str, prefix: str = "/kgf", refuse_query: bool = False) -> None:
        self.upstream = upstream.rstrip("/")
        self.prefix = prefix.rstrip("/")
        self.refuse_query = refuse_query
        self.client = httpx.Client(timeout=30)
        self.methods_seen: list[str] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _forward(self) -> None:
                proxy.methods_seen.append(self.command)
                # consume the body first, or its bytes become the next request
                # line on this keep-alive connection when we refuse early
                length = int(self.headers.get("Content-Length") or 0)
                content = self.rfile.read(length) if length else None
                if not self.path.startswith(proxy.prefix + "/") and self.path != proxy.prefix:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if proxy.refuse_query and self.command == "QUERY":
                    body = (
                        b'{"code":"method_not_allowed","status":405,"detail":"proxy refuses QUERY"}'
                    )
                    self.send_response(405)
                    self.send_header("Allow", "GET,HEAD,POST")
                    self.send_header("Content-Type", "application/problem+json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | {"host"}
                }
                headers["Accept-Encoding"] = "identity"
                upstream_path = self.path[len(proxy.prefix) :] or "/"
                response = proxy.client.request(
                    self.command, proxy.upstream + upstream_path, content=content, headers=headers
                )
                self.send_response(response.status_code)
                for k, v in response.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = do_QUERY = do_HEAD = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.origin = f"http://{host}:{port}{self.prefix}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.client.close()
