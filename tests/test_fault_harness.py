"""Unit tests for Nimbus network fault injection harness (scripts/fault_harness.py)."""

from pathlib import Path
import socket
import threading
import time
import pytest

from scripts.fault_harness import (
    PRESET_PROFILES,
    FaultProfile,
    FaultProxy,
    TraceReplayer,
)


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
