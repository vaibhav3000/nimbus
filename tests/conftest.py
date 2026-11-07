"""Shared fixture: the whole server on an ephemeral port, fresh data dir per test.

The seam under test is the public HTTP API (same surface the app and Person 3
see); tests speak only HTTP plus the published schemas, never private helpers.
"""

import os
from pathlib import Path
import sys
import threading
import uuid
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import server


@pytest.fixture(autouse=True)
def _default_auth_disabled_for_tests(monkeypatch):
    """C1 / Addendum 1: tests default to NIMBUS_AUTH_DISABLED=1 unless explicitly testing auth."""
    if "NIMBUS_AUTH_DISABLED" not in os.environ and "NIMBUS_TEST_AUTH_CHECK" not in os.environ:
        monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "1")


def _no_background_ticker(monkeypatch):
    """Tests drive the ticker by hand (server.tick_once / orch.ticker.tick) so time stays under their control."""
    monkeypatch.setenv("NIMBUS_TICK_S", "0")


@pytest.fixture(autouse=True)
def _pin_hospital_pack(monkeypatch):
    """The shipped default is P1's Gazebo map (hospital_sim). Most tests were written against the hand-made
    map, so they pin it; tests of the Gazebo map set NIMBUS_PACK themselves (it overrides this)."""
    monkeypatch.setenv("NIMBUS_PACK", "hospital")


@pytest.fixture(autouse=True)
def _aws_backend(monkeypatch):
    """NIMBUS_BACKEND=aws runs the whole suite against the DynamoDB adapters
    on moto (an emulator: no account, no network, no cost)."""
    if os.environ.get("NIMBUS_BACKEND") != "aws":
        yield
        return
    moto = pytest.importorskip("moto")
    import boto3

    from cloud.aws_dynamo import ensure_tables, tables_from_env

    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_REGION": "ap-south-1",
                 "AWS_DEFAULT_REGION": "ap-south-1", "NIMBUS_DDB_PREFIX": "t" + uuid.uuid4().hex[:8]}.items():
        monkeypatch.setenv(k, v)
    real_client = boto3.client

    class _Serialized:
        """moto's transact_write_items deep-copies its tables with no lock, so
        concurrent threads crash the emulator (real DynamoDB has no such limit).
        Serialize calls to it; conditional-write semantics are unaffected."""

        _lock = threading.RLock()

        def __init__(self, inner):
            self._inner = inner
            self.exceptions = inner.exceptions

        def __getattr__(self, name):
            fn = getattr(self._inner, name)
            if not callable(fn):
                return fn

            def call(*a, **kw):
                with self._lock:
                    return fn(*a, **kw)
            return call

    with moto.mock_aws():
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: _Serialized(real_client(*a, **kw)))
        ensure_tables(boto3.client("dynamodb"), tables_from_env())
        yield


@pytest.fixture()
def api(tmp_path):
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def http_json(method: str, url: str, body: dict | None = None):
    import json
    from urllib.error import HTTPError

    data = None if body is None else bytes(json.dumps(body), "utf-8")
    req = urlreq.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urlreq.urlopen(req)
    except HTTPError as err:
        resp = err
    with resp:
        return resp.status if hasattr(resp, "status") else resp.code, json.loads(
            resp.read().decode("utf-8")
        )
