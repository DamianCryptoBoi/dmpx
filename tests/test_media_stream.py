"""The real open_media against a local HTTP server (the fake CDN skips it)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dreamina.http_client import UpstreamError
from dreamina.http_client import open_media as real_open_media

BODY = bytes(range(256)) * 1024


class Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}
    encodings: list[str | None] = []

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        Handler.encodings.append(self.headers.get("accept-encoding"))
        if self.path == "/gone":
            self.send_response(403)
            self.end_headers()
            return
        if self.path == "/broken":
            self.send_response(503)
            self.end_headers()
            return
        body, status = BODY, 200
        extra = {}
        if self.headers.get("range"):
            first, last = (int(x) for x in self.headers["range"].removeprefix("bytes=").split("-"))
            body, status = BODY[first : last + 1], 206
            extra["Content-Range"] = f"bytes {first}-{last}/{len(BODY)}"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Set-Cookie", "tracking=1")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    Handler.hits.clear()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


async def test_streams_body_and_passes_only_safe_headers(server):
    stream = await real_open_media(f"{server}/video.mp4")
    data = b"".join([chunk async for chunk in stream.chunks])
    assert data == BODY and stream.status == 200
    assert stream.headers == {"content-length": str(len(BODY)), "accept-ranges": "bytes"}
    assert Handler.encodings[-1] == "identity"
    await stream.close()  # idempotent after the body finished


async def test_forwards_range(server):
    stream = await real_open_media(f"{server}/video.mp4", range_header="bytes=10-19")
    data = b"".join([chunk async for chunk in stream.chunks])
    assert stream.status == 206 and data == BODY[10:20]
    assert stream.headers["content-range"] == f"bytes 10-19/{len(BODY)}"


async def test_refused_url_fails_fast(server):
    with pytest.raises(UpstreamError) as excinfo:
        await real_open_media(f"{server}/gone", attempts=3)
    assert excinfo.value.status == 403 and Handler.hits["/gone"] == 1


async def test_server_errors_are_retried(server):
    with pytest.raises(UpstreamError) as excinfo:
        await real_open_media(f"{server}/broken", attempts=2)
    assert excinfo.value.status == 503 and Handler.hits["/broken"] == 2
