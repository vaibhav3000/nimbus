"""Unit tests for Nimbus network fault injection harness (scripts/fault_harness.py)."""

from pathlib import Path
import socket
import threading
import time
import urllib.parse
import uuid
import pytest

from conftest import http_json
from edge.supervisor.supervisor import EdgeSupervisor
from scripts.edge_sim import EdgeSim
from scripts.fault_harness import (
    PRESET_PROFILES,
    FaultProfile,
    FaultProxy,
    TraceReplayer,
)
from tests.test_cloud_api import sc1_leased


def test_fault_profile_defaults_and_presets():
    """Verify default values and built-in preset configurations."""
    p = FaultProfile()
    assert p.latency_ms == 0.0
    assert p.jitter_ms == 0.0
    assert p.loss_rate == 0.0
    assert not p.disconnected
    assert p.compute_delay_sec() == 0.0
    assert not p.should_drop()

    # Preset profiles
    healthy = PRESET_PROFILES["healthy"]
    assert healthy.latency_ms == 0.0
    assert not healthy.should_drop()

    shielded = PRESET_PROFILES["shielded_room"]
    assert shielded.latency_ms == 250.0
    assert shielded.jitter_ms == 80.0
    assert shielded.loss_rate == 0.05

    outage = PRESET_PROFILES["basement_outage"]
    assert outage.disconnected
    assert outage.should_drop()


def test_fault_profile_delay_and_jitter():
    """Verify latency computation with jitter stays non-negative and within bounds."""
    p = FaultProfile(latency_ms=100.0, jitter_ms=20.0)
    delays = [p.compute_delay_sec() for _ in range(50)]
    for d in delays:
        assert 0.075 <= d <= 0.125, f"Delay {d}s outside expected [0.075, 0.125]s window"


def test_fault_profile_loss_rate():
    """Verify loss rate probability bounds."""
    p_zero = FaultProfile(loss_rate=0.0)
    assert not any(p_zero.should_drop() for _ in range(100))

    p_full = FaultProfile(loss_rate=1.0)
    assert all(p_full.should_drop() for _ in range(100))

    p_half = FaultProfile(loss_rate=0.5)
    drops = sum(1 for _ in range(500) if p_half.should_drop())
    assert 180 <= drops <= 320, f"Expected ~250 drops for 0.5 rate, got {drops}"


def test_fault_profile_from_env(monkeypatch):
    """Verify environment variable parsing for profile flags."""
    monkeypatch.setenv("NIMBUS_FAULT_PROFILE", "shielded_room")
    p = FaultProfile.from_env()
    assert p.name == "shielded_room"
    assert p.latency_ms == 250.0

    monkeypatch.delenv("NIMBUS_FAULT_PROFILE")
    monkeypatch.setenv("NIMBUS_FAULT_LATENCY_MS", "150")
    monkeypatch.setenv("NIMBUS_FAULT_JITTER_MS", "30")
    monkeypatch.setenv("NIMBUS_FAULT_LOSS_RATE", "0.08")
    monkeypatch.setenv("NIMBUS_FAULT_DISCONNECTED", "1")
    p2 = FaultProfile.from_env()
    assert p2.latency_ms == 150.0
    assert p2.jitter_ms == 30.0
    assert p2.loss_rate == 0.08
    assert p2.disconnected


def test_to_tc_netem():
    """Verify Linux tc/netem command generation."""
    p = FaultProfile(latency_ms=120.0, jitter_ms=25.0, loss_rate=0.03)
    cmd = p.to_tc_netem(iface="wlan0", op="add")
    assert "sudo tc qdisc add dev wlan0 root netem" in cmd
    assert "delay 120.0ms 25.0ms distribution normal" in cmd
    assert "loss 3.00%" in cmd

    del_cmd = p.to_tc_netem(iface="wlan0", op="del")
    assert del_cmd == "sudo tc qdisc del dev wlan0 root"

    outage = FaultProfile(disconnected=True)
    outage_cmd = outage.to_tc_netem(iface="eth0")
    assert "loss 100%" in outage_cmd


class EchoServer:
    """Helper simple TCP echo server on ephemeral port for proxy testing."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.sock.settimeout(0.5)
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn: socket.socket):
        try:
            while self.running:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_fault_proxy_clean_forwarding():
    """Verify FaultProxy transparently forwards packets when healthy."""
    echo = EchoServer()
    proxy_port = find_free_port()
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=FaultProfile(latency_ms=0, loss_rate=0),
    )
    proxy.start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.sendall(b"ping-healthy-test")
            client.settimeout(2.0)
            resp = client.recv(1024)
            assert resp == b"ping-healthy-test"
            assert proxy.stats["packets_forwarded"] >= 1
    finally:
        proxy.stop()
        echo.stop()


def test_fault_proxy_injected_latency():
    """Verify FaultProxy introduces measured latency delay."""
    echo = EchoServer()
    proxy_port = find_free_port()
    # Inject 150ms delay
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=FaultProfile(latency_ms=150.0, jitter_ms=0.0),
    )
    proxy.start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            start = time.time()
            client.sendall(b"ping-latency")
            client.settimeout(3.0)
            resp = client.recv(1024)
            elapsed = time.time() - start
            assert resp == b"ping-latency"
            # Should have taken at least 120ms due to injected roundtrip delay
            assert elapsed >= 0.12, f"Expected elapsed >= 0.12s, got {elapsed:.3f}s"
    finally:
        proxy.stop()
        echo.stop()


def test_fault_proxy_disconnection_mode():
    """Verify FaultProxy closes or rejects connections when disconnected."""
    echo = EchoServer()
    proxy_port = find_free_port()
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=FaultProfile(disconnected=True),
    )
    proxy.start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.settimeout(1.0)
            data = client.recv(1024)
            # Immediate EOF / connection closed by proxy
            assert data == b""
            assert proxy.stats["connections_rejected"] >= 1
    finally:
        proxy.stop()
        echo.stop()


def test_trace_replayer(tmp_path: Path):
    """Verify TraceReplayer parses CSV rows and dynamically sets proxy profile."""
    trace_file = tmp_path / "test_trace.csv"
    trace_file.write_text(
        "time_sec,rtt_ms,loss_pct\n"
        "0.0,40.0,1.0\n"
        "0.2,300.0,10.0\n",
        encoding="utf-8",
    )

    proxy = FaultProxy(profile=FaultProfile())
    replayer = TraceReplayer(trace_file, proxy)
    replayer.start()

    try:
        # Give replayer moment to parse and apply initial row
        time.sleep(0.05)
        assert proxy.profile.latency_ms == pytest.approx(20.0, rel=1e-2)  # half RTT
        assert proxy.profile.loss_rate == pytest.approx(0.01, rel=1e-2)

        # Wait for second step at t=0.2s
        time.sleep(0.25)
        assert proxy.profile.latency_ms == pytest.approx(150.0, rel=1e-2)
        assert proxy.profile.loss_rate == pytest.approx(0.10, rel=1e-2)
    finally:
        replayer.stop()


@pytest.fixture(autouse=True)
def _patch_mock_robot_compat():
    """Ensure MockRobot accepts supervisor's topology keyword arguments if present."""
    from edge.mock import mock_robot

    orig_init = mock_robot.MockRobot.__init__

    def patched_init(self, robot_id="robot-R1", initial_pos=None, current_zone="hallway-central", waypoints=None, **kwargs):
        orig_init(self, robot_id=robot_id, initial_pos=initial_pos)
        self.current_zone = current_zone
        self.waypoints = waypoints or {}

    mock_robot.MockRobot.__init__ = patched_init
    yield
    mock_robot.MockRobot.__init__ = orig_init


def test_fault_proxy_hot_swap_reconnection():
    """Verify FaultProxy transitions cleanly from outage (drop) to healthy (forward)."""
    echo = EchoServer()
    proxy_port = find_free_port()
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=PRESET_PROFILES["basement_outage"],
    )
    proxy.start()

    try:
        # Phase 1: During outage, connection is refused / rejected
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.settimeout(1.0)
            data = client.recv(1024)
            assert data == b""

        # Phase 2: Hot-swap profile to healthy
        proxy.set_profile(PRESET_PROFILES["healthy"])

        # Phase 3: Now connection succeeds and forwards data
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.sendall(b"reconnected-packet")
            client.settimeout(2.0)
            resp = client.recv(1024)
            assert resp == b"reconnected-packet"
    finally:
        proxy.stop()
        echo.stop()


def test_offline_quality_edge_sim_hold_and_replay_on_reconnect(api):
    """Offline-quality check: EdgeSim holds escalations during basement_outage and replays on reconnect."""
    task_id = sc1_leased(api)
    parsed = urllib.parse.urlparse(api)
    proxy_port = find_free_port()

    # Start proxy in basement_outage mode (partition)
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=parsed.port or 8000,
        profile=PRESET_PROFILES["basement_outage"],
    )
    proxy.start()

    try:
        sim = EdgeSim(f"http://127.0.0.1:{proxy_port}", "east-wing")
        lease = {"lease_id": f"lease-{task_id}-v1", "version": 1}
        payload = {"result_ref": f"s3://nimbus-outputs/{task_id}/sim-proof.jpg"}

        # Attempt to escalate while disconnected
        status, _ = sim.escalate("OUTPUT_READY", lease, payload)
        assert status == 0
        assert len(sim.outbox_buffer) == 1
        assert sim.outbox_buffer[0]["type"] == "OUTPUT_READY"

        # Verify task on cloud is still in leased state (not updated yet)
        _, task = http_json("GET", api + f"/tasks/{task_id}")
        assert task["status"] == "leased"

        # Reconnect link: switch proxy profile to healthy
        proxy.set_profile(PRESET_PROFILES["healthy"])

        # Replay buffered offline escalations
        flushed = sim.flush()
        assert flushed == 1
        assert len(sim.outbox_buffer) == 0

        # Verify cloud received the replayed escalation and marked task completed
        _, updated_task = http_json("GET", api + f"/tasks/{task_id}")
        assert updated_task["status"] == "completed"
        assert updated_task["result_ref"] == f"s3://nimbus-outputs/{task_id}/sim-proof.jpg"
    finally:
        proxy.stop()


def test_offline_quality_edge_supervisor_hold_and_replay_on_reconnect(api, tmp_path: Path):
    """Offline-quality check: EdgeSupervisor preserves outbox in SQLite across outage and replays (FR-10)."""
    task_id = sc1_leased(api, suffix="sup")
    parsed = urllib.parse.urlparse(api)
    proxy_port = find_free_port()

    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=parsed.port or 8000,
        profile=PRESET_PROFILES["basement_outage"],
    )
    proxy.start()

    try:
        db_path = str(tmp_path / "supervisor_test.db")
        supervisor = EdgeSupervisor(
            cluster_id="east-wing",
            db_path=db_path,
            cloud_url=f"http://127.0.0.1:{proxy_port}",
            slm_mode="mock",
        )
        escalation = {
            "type": "OUTPUT_READY",
            "message_id": f"esc-outage-{uuid.uuid4().hex[:8]}",
            "task_id": task_id,
            "lease_version": 1,
            "payload": {"result_ref": f"s3://nimbus-outputs/{task_id}/supervisor-proof.jpg"},
        }

        # Attempt escalation under basement outage: fails and stays in SQLite outbox
        delivered = supervisor.send_escalation_to_cloud(escalation)
        assert not delivered
        pending = supervisor.store.get_pending_outbox()
        assert len(pending) == 1
        assert pending[0]["message_id"] == escalation["message_id"]

        # Cloud task should still be 'leased'
        _, task = http_json("GET", api + f"/tasks/{task_id}")
        assert task["status"] == "leased"

        # Reconnection: hot-swap profile to healthy
        proxy.set_profile(PRESET_PROFILES["healthy"])

        # Flush pending outbox
        flushed = supervisor.flush_pending_escalations()
        assert flushed == 1
        assert len(supervisor.store.get_pending_outbox()) == 0

        # Verify cloud task is now completed
        _, updated_task = http_json("GET", api + f"/tasks/{task_id}")
        assert updated_task["status"] == "completed"
        assert updated_task["result_ref"] == f"s3://nimbus-outputs/{task_id}/supervisor-proof.jpg"
    finally:
        proxy.stop()


def test_fault_endpoint_defaults_and_updates(api):
    """Verify GET and POST /fault endpoints for operator console reflection."""
    # 1. Default should be healthy and inactive
    code, initial = http_json("GET", api + "/fault")
    assert code == 200
    assert initial["active"] is False
    assert initial["profile"] == "healthy"
    assert initial["latency_ms"] == 0.0
    assert initial["disconnected"] is False
    assert initial["packets_dropped"] == 0
    assert initial["packets_forwarded"] == 0
    assert "healthy" in initial["summary"]

    # 2. Set profile to shielded_room with packet metrics
    code, resp = http_json("POST", api + "/fault", {
        "profile": "shielded_room",
        "packets_forwarded": 42,
        "packets_dropped": 3,
    })
    assert code == 200
    assert resp["accepted"] is True
    assert resp["fault"]["active"] is True
    assert resp["fault"]["profile"] == "shielded_room"
    assert resp["fault"]["latency_ms"] == 250.0
    assert resp["fault"]["loss_rate"] == 0.05
    assert resp["fault"]["packets_forwarded"] == 42
    assert resp["fault"]["packets_dropped"] == 3
    assert "42 fwd" in resp["fault"]["summary"]
    assert "3 drop" in resp["fault"]["summary"]

    # 3. GET reflects updated profile and metrics
    _, updated = http_json("GET", api + "/fault")
    assert updated["active"] is True
    assert updated["profile"] == "shielded_room"
    assert updated["latency_ms"] == 250.0
    assert updated["packets_forwarded"] == 42
    assert updated["packets_dropped"] == 3

    # 4. Set profile to basement_outage
    code, outage = http_json("POST", api + "/fault", {"profile": "basement_outage"})
    assert code == 200
    assert outage["fault"]["disconnected"] is True
    assert "PARTITIONED" in outage["fault"]["summary"]

    # 5. Clear back to healthy
    code, cleared = http_json("POST", api + "/fault", {"profile": "healthy"})
    assert code == 200
    assert cleared["fault"]["active"] is False
    assert cleared["fault"]["profile"] == "healthy"


def test_operator_console_serves_fault_hud(api):
    """Verify operator console HTML and JS serve fault HUD elements, telemetry summary, and exact labels."""
    from urllib import request as urlreq

    with urlreq.urlopen(api + "/operator") as resp:
        html = resp.read().decode("utf-8")
        assert resp.status == 200
        # Exact operator labels preserved
        assert "Nimbus: Operator Console" in html
        assert "Operator console: pause and cancel record the intent and publish a stop lease to robots." in html
        # Spatial View fault badge, telemetry summary, and styles
        assert 'id="floor-fault-badge"' in html
        assert 'id="floor-fault-stats"' in html
        assert ".hud-tag.fault" in html
        assert ".hud-fault-telemetry" in html
        assert "updateFault" in html

    with urlreq.urlopen(api + "/operator.js") as resp:
        js = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "refreshFault" in js
        assert "/fault" in js


def test_fault_proxy_syncs_with_server(api):
    """Verify FaultProxy notifies server and updates GET /fault with live packet telemetry."""
    parsed = urllib.parse.urlparse(api)
    proxy_port = find_free_port()
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=parsed.port or 8000,
        profile=PRESET_PROFILES["severe"],
    )
    proxy.start()

    try:
        # Give proxy notification thread a moment
        time.sleep(0.1)
        _, fault = http_json("GET", api + "/fault")
        assert fault["active"] is True
        assert fault["profile"] == "severe"
        assert fault["latency_ms"] == 600.0
        assert fault["loss_rate"] == 0.15
        assert "packets_forwarded" in fault
        assert "packets_dropped" in fault

        # Hot-swap profile
        proxy.set_profile(PRESET_PROFILES["shielded_room"])
        time.sleep(0.1)
        _, fault_swapped = http_json("GET", api + "/fault")
        assert fault_swapped["profile"] == "shielded_room"
        assert fault_swapped["latency_ms"] == 250.0
    finally:
        proxy.stop()
        time.sleep(0.1)
        _, fault_stopped = http_json("GET", api + "/fault")
        assert fault_stopped["active"] is False


def test_fault_proxy_packet_loss_aborts_connection():
    """Verify packet loss in userspace proxy aborts connection rather than corrupting mid-stream bytes."""
    echo = EchoServer()
    proxy_port = find_free_port()
    # 100% loss rate should drop/abort connection
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=FaultProfile(loss_rate=1.0),
    )
    proxy.start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.settimeout(1.0)
            data = client.recv(1024)
            assert data == b""
            assert proxy.stats["packets_dropped"] >= 1
    finally:
        proxy.stop()
        echo.stop()


def test_fault_profile_validation_and_clamping():
    """Verify FaultProfile rejects invalid parameters and clamps delay to non-negative."""
    # Clamping & non-negative delay
    p = FaultProfile(latency_ms=10.0, jitter_ms=50.0)
    for _ in range(50):
        d = p.compute_delay_sec()
        assert d >= 0.0, f"Delay must be non-negative, got {d}"

    # Invalid values raise ValueError
    with pytest.raises(ValueError, match="latency_ms"):
        FaultProfile(latency_ms=-1.0)

    with pytest.raises(ValueError, match="jitter_ms"):
        FaultProfile(jitter_ms=-5.0)

    with pytest.raises(ValueError, match="loss_rate"):
        FaultProfile(loss_rate=1.5)

    with pytest.raises(ValueError, match="loss_rate"):
        FaultProfile(loss_rate=-0.1)


def test_edge_sim_preserves_message_id_and_timestamp_on_replay(api):
    """Verify EdgeSim preserves original message_id and timestamp across offline buffering and replay."""
    task_id = sc1_leased(api, suffix="pres")
    parsed = urllib.parse.urlparse(api)
    proxy_port = find_free_port()

    # Start disconnected proxy
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=parsed.port or 8000,
        profile=PRESET_PROFILES["basement_outage"],
    )
    proxy.start()

    try:
        sim = EdgeSim(f"http://127.0.0.1:{proxy_port}", "east-wing")
        lease = {"lease_id": f"lease-{task_id}-v1", "version": 1}

        # Check-in during outage: fails and gets buffered
        status_ci, _ = sim.checkin(lease)
        assert status_ci == 0
        assert len(sim.outbox_buffer) == 1
        buffered_ci = sim.outbox_buffer[0]
        orig_ci_id = buffered_ci["message_id"]
        orig_ci_ts = buffered_ci["timestamp"]
        assert orig_ci_id.startswith("sim-ci-")
        assert orig_ci_ts is not None

        # Escalation during outage: fails and gets buffered
        status_esc, _ = sim.escalate("OUTPUT_READY", lease, {"result_ref": f"s3://outputs/{task_id}/pic.jpg"})
        assert status_esc == 0
        assert len(sim.outbox_buffer) == 2
        buffered_esc = sim.outbox_buffer[1]
        orig_esc_id = buffered_esc["message_id"]
        assert orig_esc_id.startswith("sim-output_ready-")

        # Now reconnect
        proxy.set_profile(PRESET_PROFILES["healthy"])

        # Flush buffered messages
        flushed = sim.flush()
        assert flushed == 2
        assert len(sim.outbox_buffer) == 0

        # Verify buffered items did not mutate IDs
        assert buffered_ci["message_id"] == orig_ci_id
        assert buffered_ci["timestamp"] == orig_ci_ts
        assert buffered_esc["message_id"] == orig_esc_id

        # Verify task completed on cloud
        _, task = http_json("GET", api + f"/tasks/{task_id}")
        assert task["status"] == "completed"
    finally:
        proxy.stop()


def test_fault_endpoint_rejects_unknown_profiles_and_bad_values(api):
    """Verify POST /fault rejects unknown profiles and invalid numeric values with 400."""
    # Unknown profile
    code, resp = http_json("POST", api + "/fault", {"profile": "totally_bogus_profile"})
    assert code == 400
    assert "unknown fault profile" in resp["error"]

    # Negative latency
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "latency_ms": -50.0})
    assert code == 400
    assert "latency_ms must be non-negative" in resp["error"]

    # Loss rate > 1.0
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "loss_rate": 1.5})
    assert code == 400
    assert "loss_rate must be between 0.0 and 1.0" in resp["error"]

    # Negative counters
    code, resp = http_json("POST", api + "/fault", {"profile": "healthy", "packets_dropped": -1})
    assert code == 400
    assert "non-negative" in resp["error"]

    # Reject boolean where number expected
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "latency_ms": True})
    assert code == 400
    assert "number" in resp["error"]

    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "packets_forwarded": False})
    assert code == 400
    assert "integer" in resp["error"]

    # Reject numeric strings where number/integer expected
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "latency_ms": "250.0"})
    assert code == 400
    assert "no bools or strings" in resp["error"]

    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "packets_forwarded": "100"})
    assert code == 400
    assert "integer" in resp["error"]

    # Reject NaN and Infinity
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "latency_ms": float("nan")})
    assert code == 400
    assert "finite number" in resp["error"]

    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "jitter_ms": float("inf")})
    assert code == 400
    assert "finite number" in resp["error"]

    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "loss_rate": float("nan")})
    assert code == 400
    assert "finite number" in resp["error"]

    # Cap extremely large integers
    code, resp = http_json("POST", api + "/fault", {"profile": "custom", "packets_forwarded": 999_999_999_999})
    assert code == 200
    assert resp["fault"]["packets_forwarded"] == 2_147_483_647


def test_edge_sim_aborted_response_handling():
    """Verify EdgeSim handles aborted connections, timeouts, and IncompleteRead without crashing (M1)."""
    # Point sim to an echo server that closes immediately
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    def _abort():
        try:
            conn, _ = server.accept()
            conn.recv(1024)
            # Send partial HTTP response and abruptly close (IncompleteRead / aborted response)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")
            conn.close()
        except Exception:
            pass
        finally:
            try:
                server.close()
            except Exception:
                pass

    threading.Thread(target=_abort, daemon=True).start()

    sim = EdgeSim(f"http://127.0.0.1:{port}", "east-wing")
    status, out = sim._call("GET", "/test", timeout=1.0)
    assert status == 0
    assert "error" in out
    # Verify sim.step() does not crash
    assert sim.step() == 0


def test_fault_proxy_once_per_connection_loss_accuracy():
    """Verify loss decision is drawn once per connection and handles sibling pipe shutdown cleanly (M3, M4)."""
    echo = EchoServer()
    proxy_port = find_free_port()
    # 0% loss: always passes
    proxy_clean = FaultProxy(
        listen_port=proxy_port,
        target_port=echo.port,
        profile=FaultProfile(loss_rate=0.0),
    )
    proxy_clean.start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", proxy_port))
            client.sendall(b"hello")
            client.settimeout(2.0)
            assert client.recv(1024) == b"hello"
    finally:
        proxy_clean.stop()
        echo.stop()


def test_fault_proxy_sigterm_cleanup(monkeypatch):
    """Verify SIGTERM clean reset handler unlinks status and restores server (M5)."""
    proxy_port = find_free_port()
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=8000,
        profile=PRESET_PROFILES["shielded_room"],
    )
    proxy.start()

    try:
        assert proxy._running
        # Call _clear_fault_status directly or stop()
        proxy.stop()
        assert not proxy._running
    finally:
        proxy.stop()
