"""Nimbus Local Network Fault Injection Harness (Person 4 — Experiments E4–E6).

Provides switchable network degradation (latency, jitter, packet loss, and full
disconnection / partition) on the cloud-edge link using:
1. Pure-Python multi-threaded streaming proxy (works on Windows, macOS, and Linux
   without root or kernel dependencies).
2. Linux tc/netem script generator and runner for container and Linux testbeds.
3. Trace-driven playback for recorded IISc campus routes and 4G/5G mobility traces.

Usage:
    # Run standalone proxy intercepting HTTP (default 8001 -> 8000)
    python scripts/fault_harness.py --listen 8001 --target 8000 --profile shielded_room

    # Run standalone proxy for MQTT (e.g. 1884 -> 1883)
    python scripts/fault_harness.py --listen 1884 --target 1883 --profile severe

    # Custom latency and loss
    python scripts/fault_harness.py --latency 200 --jitter 50 --loss 0.05

    # Print Linux tc/netem commands for a hardware testbed
    python scripts/fault_harness.py --tc-script --profile severe --iface eth0

    # Trace replay mode (Note: IISc traces are short synthetic examples for local testing, not full 10 Hz recordings)
    python scripts/fault_harness.py --trace traces/iisc_lift.csv
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import random
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("nimbus.faults")


@dataclass
class FaultProfile:
    """Configuration for simulated network degradation."""

    name: str = "custom"
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_rate: float = 0.0  # Fraction between 0.0 (0%) and 1.0 (100%)
    disconnected: bool = False
    trace_file: str | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms must be non-negative, got {self.latency_ms}")
        if self.jitter_ms < 0:
            raise ValueError(f"jitter_ms must be non-negative, got {self.jitter_ms}")
        if not (0.0 <= self.loss_rate <= 1.0):
            raise ValueError(f"loss_rate must be between 0.0 and 1.0, got {self.loss_rate}")

    def compute_delay_sec(self) -> float:
        """Calculate artificial delay in seconds including random jitter."""
        if self.latency_ms <= 0 and self.jitter_ms <= 0:
            return 0.0
        jitter = random.uniform(-self.jitter_ms, self.jitter_ms) if self.jitter_ms > 0 else 0.0
        delay_ms = max(0.0, self.latency_ms + jitter)
        return max(0.0, delay_ms / 1000.0)

    def should_drop(self) -> bool:
        """Determine if a connection or packet chunk should be dropped."""
        if self.disconnected:
            return True
        if self.loss_rate <= 0.0:
            return False
        if self.loss_rate >= 1.0:
            return True
        return random.random() < self.loss_rate

    def to_tc_netem(self, iface: str = "eth0", op: str = "add") -> str:
        """Generate equivalent Linux iproute2 / tc netem command string."""
        if op == "del":
            return f"sudo tc qdisc del dev {iface} root"

        parts = [f"sudo tc qdisc {op} dev {iface} root netem"]
        if self.disconnected:
            parts.append("loss 100%")
        else:
            if self.latency_ms > 0:
                parts.append(f"delay {self.latency_ms:.1f}ms")
                if self.jitter_ms > 0:
                    parts.append(f"{self.jitter_ms:.1f}ms distribution normal")
            if self.loss_rate > 0:
                loss_pct = self.loss_rate * 100.0
                parts.append(f"loss {loss_pct:.2f}%")
        return " ".join(parts)

    @classmethod
    def from_env(cls) -> "FaultProfile":
        """Load configuration from standard NIMBUS_FAULT_* environment variables."""
        profile_name = os.environ.get("NIMBUS_FAULT_PROFILE", "").lower().strip()
        if profile_name:
            if profile_name in PRESET_PROFILES:
                base = PRESET_PROFILES[profile_name]
                return cls(
                    name=base.name,
                    latency_ms=base.latency_ms,
                    jitter_ms=base.jitter_ms,
                    loss_rate=base.loss_rate,
                    disconnected=base.disconnected,
                )
            elif profile_name not in ("custom", "healthy", "none", "clear"):
                raise ValueError(
                    f"Unknown NIMBUS_FAULT_PROFILE '{profile_name}'. "
                    f"Must be one of: {list(PRESET_PROFILES.keys())} or 'custom'"
                )

        try:
            latency = float(os.environ.get("NIMBUS_FAULT_LATENCY_MS", "0"))
        except ValueError as e:
            raise ValueError(f"Invalid NIMBUS_FAULT_LATENCY_MS: {e}") from e

        try:
            jitter = float(os.environ.get("NIMBUS_FAULT_JITTER_MS", "0"))
        except ValueError as e:
            raise ValueError(f"Invalid NIMBUS_FAULT_JITTER_MS: {e}") from e

        try:
            loss = float(os.environ.get("NIMBUS_FAULT_LOSS_RATE", "0"))
        except ValueError as e:
            raise ValueError(f"Invalid NIMBUS_FAULT_LOSS_RATE: {e}") from e

        disconnected = os.environ.get("NIMBUS_FAULT_DISCONNECTED", "0").lower() in ("1", "true", "yes")
        trace = os.environ.get("NIMBUS_FAULT_TRACE") or None

        return cls(
            name=profile_name or "env",
            latency_ms=latency,
            jitter_ms=jitter,
            loss_rate=loss,
            disconnected=disconnected,
            trace_file=trace,
        )


PRESET_PROFILES: dict[str, FaultProfile] = {
    "healthy": FaultProfile(name="healthy", latency_ms=0.0, jitter_ms=0.0, loss_rate=0.0),
    "mild_wan": FaultProfile(name="mild_wan", latency_ms=60.0, jitter_ms=15.0, loss_rate=0.01),
    "shielded_room": FaultProfile(name="shielded_room", latency_ms=250.0, jitter_ms=80.0, loss_rate=0.05),
    "severe": FaultProfile(name="severe", latency_ms=600.0, jitter_ms=200.0, loss_rate=0.15),
    "basement_outage": FaultProfile(name="basement_outage", latency_ms=0.0, jitter_ms=0.0, loss_rate=1.0, disconnected=True),
}


def _safe_close(sock: socket.socket | None) -> None:
    """Safely shut down and close a socket, ignoring errors if already closed."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, Exception):
        pass
    try:
        sock.close()
    except (OSError, Exception):
        pass


class FaultProxy:
    """Pure-Python streaming proxy that injects latency, loss, and disconnection."""

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8001,
        target_host: str = "127.0.0.1",
        target_port: int = 8000,
        profile: FaultProfile | None = None,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.profile = profile or FaultProfile()
        self._lock = threading.RLock()
        self._running = False
        self._server_sock: socket.socket | None = None
        self._worker_thread: threading.Thread | None = None
        self._active_sockets: set[socket.socket] = set()

        # Telemetry metrics for experiment evaluation
        self.stats = {
            "connections_total": 0,
            "connections_rejected": 0,
            "packets_forwarded": 0,
            "packets_dropped": 0,
            "bytes_forwarded": 0,
        }
        self._last_sync_time = 0.0

    def set_profile(self, profile: FaultProfile) -> None:
        """Hot-swap the active fault profile while running."""
        with self._lock:
            self.profile = profile
            self._sync_fault_status()

    def _sync_fault_status(self) -> None:
        """Write current profile to local data/fault_status.json and notify target if local server."""
        with self._lock:
            fwd = self.stats.get("packets_forwarded", 0)
            drop = self.stats.get("packets_dropped", 0)
            bytes_fwd = self.stats.get("bytes_forwarded", 0)
            conn_tot = self.stats.get("connections_total", 0)
            conn_rej = self.stats.get("connections_rejected", 0)
            active = not (self.profile.name == "healthy" and not self.profile.disconnected and self.profile.loss_rate == 0 and self.profile.latency_ms == 0)
            state = {
                "active": active,
                "profile": self.profile.name,
                "latency_ms": self.profile.latency_ms,
                "jitter_ms": self.profile.jitter_ms,
                "loss_rate": self.profile.loss_rate,
                "disconnected": self.profile.disconnected,
                "packets_forwarded": fwd,
                "packets_dropped": drop,
                "bytes_forwarded": bytes_fwd,
                "connections_total": conn_tot,
                "connections_rejected": conn_rej,
            }
        try:
            import json
            status_path = Path("data/fault_status.json")
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            pass

        if self.target_host in ("127.0.0.1", "localhost"):
            def _post():
                try:
                    import json
                    from urllib import request as urlreq
                    req = urlreq.Request(
                        f"http://{self.target_host}:{self.target_port}/fault",
                        data=json.dumps(state).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlreq.urlopen(req, timeout=0.5):
                        pass
                except Exception:
                    pass
            threading.Thread(target=_post, daemon=True).start()

    def _maybe_sync_stats(self) -> None:
        """Throttled sync of live packet forwarding and loss counters."""
        now = time.time()
        if now - self._last_sync_time >= 0.5:
            self._last_sync_time = now
            self._sync_fault_status()

    def _clear_fault_status(self) -> None:
        """Clear local fault status on proxy shutdown synchronously."""
        try:
            status_path = Path("data/fault_status.json")
            if status_path.exists():
                status_path.unlink()
        except Exception:
            pass
        if self.target_host in ("127.0.0.1", "localhost"):
            try:
                import json
                from urllib import request as urlreq
                req = urlreq.Request(
                    f"http://{self.target_host}:{self.target_port}/fault",
                    data=json.dumps({"active": False, "profile": "healthy"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlreq.urlopen(req, timeout=1.0):
                    pass
            except Exception:
                pass

    def start(self) -> None:
        """Start listening on the background daemon thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.listen_host, self.listen_port))
            sock.listen(128)
            sock.settimeout(0.5)
            self._server_sock = sock

            self._worker_thread = threading.Thread(
                target=self._accept_loop,
                name=f"FaultProxy-{self.listen_port}->{self.target_port}",
                daemon=True,
            )
            self._worker_thread.start()
            self._sync_fault_status()

        import atexit
        try:
            atexit.register(self._clear_fault_status)
        except Exception:
            pass

        if threading.current_thread() is threading.main_thread():
            def _sigterm_reset(signum, frame):
                self._clear_fault_status()
                sys.exit(128 + signum)

            try:
                sig = getattr(signal, "SIGTERM", None)
                if sig is not None:
                    self._prev_sigterm = signal.signal(sig, _sigterm_reset)
            except (ValueError, OSError):
                pass

    def stop(self) -> None:
        """Stop listening and cleanly terminate active connections."""
        import atexit
        try:
            atexit.unregister(self._clear_fault_status)
        except Exception:
            pass

        if hasattr(self, "_prev_sigterm"):
            try:
                sig = getattr(signal, "SIGTERM", None)
                if sig is not None:
                    signal.signal(sig, self._prev_sigterm)
            except (ValueError, OSError):
                pass

        with self._lock:
            self._running = False
            if self._server_sock:
                _safe_close(self._server_sock)
                self._server_sock = None

            for s in list(self._active_sockets):
                _safe_close(s)
            self._active_sockets.clear()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        self._clear_fault_status()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                assert self._server_sock is not None
                client_sock, client_addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with self._lock:
                self.stats["connections_total"] += 1
                current_profile = self.profile

            # Check disconnection / outage partition
            if current_profile.disconnected:
                with self._lock:
                    self.stats["connections_rejected"] += 1
                _safe_close(client_sock)
                continue

            # Spawn handler thread for bidirectional pipe
            t = threading.Thread(
                target=self._handle_client,
                args=(client_sock,),
                name=f"FaultPipe-{client_addr[1]}",
                daemon=True,
            )
            t.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        with self._lock:
            profile = self.profile

        # Upfront packet-loss decision: draw once per connection (M3)
        if profile.should_drop():
            with self._lock:
                self.stats["packets_dropped"] += 1
                self.stats["connections_rejected"] += 1
            self._maybe_sync_stats()
            _safe_close(client_sock)
            return

        target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self._lock:
            self._active_sockets.add(client_sock)
            self._active_sockets.add(target_sock)

        try:
            target_sock.connect((self.target_host, self.target_port))
        except Exception:
            with self._lock:
                self.stats["connections_rejected"] += 1
            _safe_close(client_sock)
            _safe_close(target_sock)
            with self._lock:
                self._active_sockets.discard(client_sock)
                self._active_sockets.discard(target_sock)
            return

        # Start two pipe directions
        t1 = threading.Thread(
            target=self._pipe,
            args=(client_sock, target_sock, "c2s"),
            daemon=True,
        )
        t2 = threading.Thread(
            target=self._pipe,
            args=(target_sock, client_sock, "s2c"),
            daemon=True,
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        for s in (client_sock, target_sock):
            _safe_close(s)
            with self._lock:
                self._active_sockets.discard(s)

    def _pipe(self, src: socket.socket, dst: socket.socket, direction: str) -> None:
        try:
            src.settimeout(1.0)
            while self._running:
                with self._lock:
                    profile = self.profile

                if profile.disconnected:
                    break

                try:
                    data = src.recv(65536)
                    if not data:
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break

                # Latency and jitter injection (guaranteed non-negative)
                delay = max(0.0, profile.compute_delay_sec())
                if delay > 0.0:
                    time.sleep(delay)

                try:
                    dst.sendall(data)
                    with self._lock:
                        self.stats["packets_forwarded"] += 1
                        self.stats["bytes_forwarded"] += len(data)
                    self._maybe_sync_stats()
                except OSError:
                    break
        except OSError:
            # Handle sibling thread or OS closing socket concurrently (e.g. EBADF, WSAENOTSOCK, WSAECONNABORTED)
            pass
        except Exception:
            pass
        finally:
            _safe_close(src)
            _safe_close(dst)


class TraceReplayer:
    """Replays time-series RTT and packet loss from a CSV trace file."""

    def __init__(self, trace_file: str | Path, proxy: FaultProxy):
        self.trace_file = Path(trace_file)
        self.proxy = proxy
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.trace_file.exists():
            raise FileNotFoundError(f"Trace file not found: {self.trace_file}")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="TraceReplayer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        rows: list[tuple[float, float, float]] = []
        with open(self.trace_file, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for line_no, row in enumerate(reader, 1):
                if not row or row[0].startswith("#") or row[0].strip().lower() == "time_sec":
                    continue
                try:
                    t_sec = float(row[0])
                    rtt_ms = float(row[1])
                    loss_pct = float(row[2]) if len(row) > 2 else 0.0
                    rows.append((t_sec, rtt_ms, loss_pct))
                except (ValueError, IndexError):
                    continue

        if not rows:
            return

        start_time = time.time()
        for t_sec, rtt_ms, loss_pct in rows:
            if self._stop_event.is_set():
                break

            target_elapsed = t_sec
            current_elapsed = time.time() - start_time
            if target_elapsed > current_elapsed:
                sleep_dur = max(0.0, target_elapsed - current_elapsed)
                if sleep_dur > 0 and self._stop_event.wait(sleep_dur):
                    break

            new_profile = FaultProfile(
                name=f"trace@{t_sec:.1f}s",
                latency_ms=max(0.0, rtt_ms / 2.0),  # One-way delay estimation
                jitter_ms=max(0.0, rtt_ms * 0.1),
                loss_rate=min(1.0, max(0.0, loss_pct / 100.0)),
                disconnected=(loss_pct >= 99.0),
            )
            self.proxy.set_profile(new_profile)


def run_tc_command(cmd: str) -> tuple[int, str]:
    """Execute a Linux tc command via subprocess."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except Exception as e:
        return 1, str(e)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nimbus local network fault injection harness (E4–E6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--listen", type=int, default=8001, help="Proxy local listen port (default: 8001)")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Proxy listen host (default: 127.0.0.1)")
    parser.add_argument("--target", type=int, default=8000, help="Target upstream port (default: 8000)")
    parser.add_argument("--target-host", default="127.0.0.1", help="Target upstream host (default: 127.0.0.1)")

    parser.add_argument(
        "--profile",
        choices=list(PRESET_PROFILES.keys()),
        help=f"Use a preset degradation profile ({', '.join(PRESET_PROFILES.keys())})",
    )
    parser.add_argument("--latency", type=float, default=None, help="Injected latency in milliseconds")
    parser.add_argument("--jitter", type=float, default=None, help="Latency jitter in milliseconds")
    parser.add_argument("--loss", type=float, default=None, help="Packet loss rate (0.0 to 1.0)")
    parser.add_argument("--disconnected", action="store_true", help="Simulate complete network outage")
    parser.add_argument("--trace", type=str, default=None, help="Path to CSV trace file for dynamic playback")

    parser.add_argument("--tc-script", action="store_true", help="Print the Linux tc/netem command and exit")
    parser.add_argument("--tc-apply", action="store_true", help="Execute the Linux tc/netem command via sudo")
    parser.add_argument("--iface", default="eth0", help="Network interface for tc netem (default: eth0)")
    parser.add_argument("--tc-clear", action="store_true", help="Clear tc qdisc rule on the interface and exit")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Resolve profile
    if args.profile:
        profile = PRESET_PROFILES[args.profile]
    else:
        profile = FaultProfile.from_env()

    if args.latency is not None:
        profile.latency_ms = args.latency
    if args.jitter is not None:
        profile.jitter_ms = args.jitter
    if args.loss is not None:
        profile.loss_rate = args.loss
    if args.disconnected:
        profile.disconnected = True
    if args.trace:
        profile.trace_file = args.trace

    # Handle Linux tc/netem commands
    if args.tc_clear:
        cmd = f"sudo tc qdisc del dev {args.iface} root"
        if args.tc_apply:
            code, out = run_tc_command(cmd)
            print(f"Executed: {cmd}\nOutput: {out}")
            return code
        print(cmd)
        return 0

    if args.tc_script or args.tc_apply:
        cmd = profile.to_tc_netem(iface=args.iface)
        if args.tc_apply:
            code, out = run_tc_command(cmd)
            print(f"Executed: {cmd}\nOutput: {out}")
            return code
        print(cmd)
        return 0

    # Run pure-Python streaming proxy
    proxy = FaultProxy(
        listen_host=args.listen_host,
        listen_port=args.listen,
        target_host=args.target_host,
        target_port=args.target,
        profile=profile,
    )

    replayer: TraceReplayer | None = None
    if profile.trace_file:
        replayer = TraceReplayer(profile.trace_file, proxy)

    print(
        f"Starting Nimbus Fault Proxy on {args.listen_host}:{args.listen} -> {args.target_host}:{args.target}\n"
        f"  Profile:      {profile.name}\n"
        f"  Latency:      {profile.latency_ms:.1f}ms (+/- {profile.jitter_ms:.1f}ms)\n"
        f"  Loss Rate:    {profile.loss_rate * 100:.1f}%\n"
        f"  Disconnected: {profile.disconnected}\n"
        f"  Trace File:   {profile.trace_file or 'None'}"
    )

    proxy.start()
    if replayer:
        print(f"Starting trace replay from {profile.trace_file}...")
        replayer.start()

    print("Press Ctrl+C to stop.")
    def _sig_handler(signum, frame):
        print(f"\nReceived signal {signum}, stopping Fault Proxy...")
        raise KeyboardInterrupt()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _sig_handler)
            except (ValueError, OSError):
                pass

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping Fault Proxy...")
    finally:
        if replayer:
            replayer.stop()
        proxy.stop()
        print(
            f"Fault Proxy stopped.\nStats: {proxy.stats['packets_forwarded']} packets forwarded, "
            f"{proxy.stats['packets_dropped']} packets dropped, "
            f"{proxy.stats['connections_total']} total connections ({proxy.stats['connections_rejected']} rejected)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
