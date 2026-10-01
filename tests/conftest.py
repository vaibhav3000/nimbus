"""Shared fixture: the whole server on an ephemeral port, fresh data dir per test.

The seam under test is the public HTTP API (same surface the app and Person 3
see); tests speak only HTTP plus the published schemas, never private helpers.
"""

import threading
from urllib import request as urlreq

import pytest

import server


@pytest.fixture()
def api(tmp_path):
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def http_json(method: str, url: str, body: dict | None = None):
    data = None if body is None else bytes(__import__("json").dumps(body), "utf-8")
    req = urlreq.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urlreq.urlopen(req) as resp:
        import json as _json

        return resp.status, _json.loads(resp.read().decode("utf-8"))
