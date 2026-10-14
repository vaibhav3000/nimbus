"""The two scripts you run to connect to AWS, executed here against moto and
an emulated Bedrock, so what ships has actually been run."""

import sys
import threading
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")
import boto3  # noqa: E402

from bedrock_emulator import EmulatedBedrockRuntime  # noqa: E402
from cloud.aws_dynamo import ensure_tables  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture()
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "ap-south-1"}.items():
        monkeypatch.setenv(k, v)
    real, emu, lock = boto3.client, EmulatedBedrockRuntime(), threading.RLock()

    class Locked:
        def __init__(self, c):
            self._c, self.exceptions = c, c.exceptions

        def __getattr__(self, n):
            fn = getattr(self._c, n)
            return (lambda *a, **k: _locked(fn, a, k)) if callable(fn) else fn

    def _locked(fn, a, k):
        with lock:
            return fn(*a, **k)

    def fake_client(service, *a, **k):
        return emu if service == "bedrock-runtime" else Locked(real(service, *a, **k))

    with moto.mock_aws():
        monkeypatch.setattr(boto3, "client", fake_client)
        yield emu


def test_bedrock_hello_script_runs_and_reports_tool_use(aws, capsys):
    import bedrock_hello
    bedrock_hello.hello("apac.test-model", "ap-south-1")
    out = capsys.readouterr().out
    assert "tool use works" in out and "ask_clarification" in out
    req = aws.requests[0]
    assert req["toolConfig"]["toolChoice"] == {"any": {}} and req["modelId"] == "apac.test-model"


def test_smoke_live_script_passes_end_to_end_and_tears_down(aws, capsys, monkeypatch):
    import smoke_live
    # The script configures itself through os.environ; register every key it sets
    # with monkeypatch first so they are restored and cannot leak into later tests.
    for key in ("NIMBUS_BACKEND", "NIMBUS_LLM", "NIMBUS_BEDROCK_MODEL", "AWS_REGION", "NIMBUS_DDB_PREFIX",
                "NIMBUS_MODE", "NIMBUS_CASSETTE", "NIMBUS_CASSETTE_MODE"):
        monkeypatch.setenv(key, "placeholder")  # registers the restore-to-original...
        monkeypatch.delenv(key)                  # ...and starts the script from a clean slate
    monkeypatch.setattr(sys, "argv", ["smoke_live.py", "--model", "apac.test-model", "--teardown"])
    assert smoke_live.main() == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "deleted tables nimbus-smoke-" in out
    assert "cloud:ask_clarify" in out and "cloud:replan" in out and "edge:OUTPUT_READY" in out
    left = boto3.client("dynamodb", region_name="ap-south-1").list_tables()["TableNames"]
    assert not [t for t in left if t.startswith("nimbus-smoke-")]


def test_prompt_alone_is_enough_to_allocate_and_busy_robots_are_visible(aws):
    """The emulator never sees Python objects: allocation worked only because the
    prompt carries health, free robots and stock. Busy robots must show up too."""
    from cloud_helpers import make_orch, new_task
    orch = make_orch()
    orch.llm = __import__("cloud.llm", fromlist=["BedrockLLM"]).BedrockLLM("m", orch.tools, client=aws)
    first = orch.handle_request(new_task(1), "Deliver a wheelchair to ward 3")
    second = orch.handle_request(new_task(2), "Deliver a wheelchair to ward 3")
    assert first.rows[0]["output"]["proposed_by"] == "llm" and second.rows[0]["output"]["proposed_by"] == "llm"
    assert first.lease["assigned_robots"] != second.lease["assigned_robots"]  # second prompt marked R1 busy
    prompt = aws.requests[-1]["system"][0]["text"]
    assert "R1=busy" in prompt and "east-wing area=east health=unknown" in prompt and "SOP-ISO-02" not in prompt
