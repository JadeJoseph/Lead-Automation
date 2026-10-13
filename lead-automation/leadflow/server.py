"""Webhook mode: POST /lead with one JSON submission, get back the outcome.

    python -m leadflow serve --port 8080
    python -m leadflow run --via-http http://127.0.0.1:8080/lead

Requests are processed one at a time (a lock), because dedupe depends on the order leads arrive in.
If WEBHOOK_SECRET is set, callers must send it as the X-Webhook-Secret header.
"""
from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock


def make_handler(pipeline, secret: str):
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._send(200, {"status": "ok"}) if self.path == "/health" else self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/lead":
                return self._send(404, {"error": "not found"})
            if secret and not hmac.compare_digest(self.headers.get("X-Webhook-Secret", ""), secret):
                return self._send(401, {"error": "bad or missing X-Webhook-Secret"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 100_000:
                    return self._send(413, {"error": "payload too large"})
                payload = json.loads(self.rfile.read(length) or b"null")
            except (ValueError, json.JSONDecodeError) as exc:
                with lock:
                    pipeline.store.log(pipeline.run_id, "(unknown)", "ingest", "rejected", f"invalid JSON body: {exc}")
                return self._send(400, {"error": f"invalid JSON: {exc}"})
            with lock:
                outcome = pipeline.process(payload)
                pipeline.store.export_review_queue()  # keep it current; a killed server must not leave it stale
            self._send(200, outcome.as_dict())

        def log_message(self, fmt, *args):  # quiet
            pass

    return Handler


def serve(pipeline, secret: str, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pipeline, secret))
    print(f"leadflow webhook listening on http://127.0.0.1:{port}/lead  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pipeline.store.export_review_queue()
