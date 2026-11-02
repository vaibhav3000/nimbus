"""Fake edge cluster for the cloud side (Person 3), answering what P4 asked:
read leases from the outbox, send check-ins and OUTPUT_READY, and show up on
the timeline. Also reports cluster health, answers probes, and can raise an
UNEXPECTED_EVENT, which is how re-planning is exercised without a real edge.

    python scripts/edge_sim.py --api http://127.0.0.1:8000 --cluster east-wing --complete
    python scripts/edge_sim.py --cluster east-wing --block E3 --once
    python scripts/edge_sim.py --cluster east-wing --close W0 --robot-zone E4 --once   # SC-2: a gate shuts, robot parked at E4

Real edge (Person 2) replaces this; the messages are the contract's.

Transport Note:
This simulation exercises the cloud via the default HTTP REST endpoints
(GET /outbox, POST /escalations, POST /checkins). The canonical MQTT topic
patterns from contracts/topics.md are referenced here for outbox topic
matching. See docs/integration.md for full specifications.
"""

from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import sys
import time
import urllib.parse
import uuid
from urllib import request as urlreq
from urllib.error import HTTPError

# Ensure repo root is on sys.path for fault_harness import
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class EdgeSim:
    # Milestone 2 Canonical MQTT Topic Patterns (contracts/topics.md)
    TOPIC_LEASE = "nimbus/{cluster}/lease"
    TOPIC_RENEWAL = "nimbus/{cluster}/renewal"
    TOPIC_PROBE = "nimbus/{cluster}/probe"
    TOPIC_ESCALATION = "nimbus/{cluster}/escalation"
    TOPIC_CHECKIN = "nimbus/{cluster}/checkin"

    def __init__(self, api: str, cluster: str, *, complete: bool = False, block: str | None = None,
                 robots: list | None = None, state: str = "blocked", robot_zone: str | None = None):
        self.api, self.cluster = api.rstrip("/"), cluster
        self.complete, self.block, self.state = complete, block, state
        self.robot_zone = robot_zone      # where the robot is parked when it reports; sent as a map node, as the contract says
        self.robot_id: str | None = None  # learned from the first lease
        self.stopped: set[str] = set()    # tasks the cloud told this cluster to stop (a lease whose only step is `stop`)
        self.robots = robots
        self.seen: set[str] = set()   # lease ids already answered
        self.log: list[tuple[str, int, dict]] = []
        self.outbox_buffer: list[dict] = []  # buffered offline messages (FR-10 store-and-forward)

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0):
        data = None if body is None else json.dumps(body).encode()
        req = urlreq.Request(self.api + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urlreq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                out = json.loads(raw or "{}")
                status = resp.status
        except HTTPError as err:
            status = getattr(err, "status", getattr(err, "code", 500))
            try:
                raw = err.read().decode("utf-8")
                out = json.loads(raw or "{}")
            except Exception:
                out = {"error": str(err)}
        except (urlreq.URLError, OSError, ConnectionError, http.client.HTTPException, http.client.IncompleteRead) as err:
            self.log.append((f"{method} {path}", 0, {"error": str(err)}))
            return 0, {"error": str(err)}
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            self.log.append((f"{method} {path}", 0, {"error": str(err)}))
            return 0, {"error": str(err)}

        self.log.append((f"{method} {path}", status, out))
        return status, out

    def flush(self) -> int:
        """Retry sending buffered offline escalations and check-ins; returns count successfully delivered."""
        if not self.outbox_buffer:
            return 0
        flushed = 0
        remaining = []
        for body in self.outbox_buffer:
            # Preserve original message_id and timestamp across replays without regenerating
            endpoint = "/escalations" if "type" in body else "/checkins"
            status, _ = self._call("POST", endpoint, body)
            if status in (200, 201, 204):
                flushed += 1
            else:
                remaining.append(body)
        self.outbox_buffer = remaining
        return flushed

    @staticmethod
    def _mid(kind: str) -> str:
        return f"sim-{kind}-{uuid.uuid4().hex[:10]}"

    def heartbeat(self):
        body = {"cluster_id": self.cluster}
        if self.robot_zone and not self.robots:
            body["robots"] = [{"id": self.robot_id or "R1", "state": "busy", "zone": self.robot_zone}]
        if self.robots:
            body["robots"] = self.robots
        return self._call("POST", "/fleet/heartbeat", body)

    def checkin(self, lease: dict):
        body = {
            "cluster_id": self.cluster,
            "lease_id": lease["lease_id"],
            "lease_version": lease["version"],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "message_id": self._mid("ci"),
        }
        if self.robots:
            body["robots"] = self.robots
        status, resp = self._call("POST", "/checkins", body)
        if status not in (200, 201, 204):
            # Network or server offline: hold check-in in local buffer for replay, preserving message_id and timestamp
            self.outbox_buffer.append(body)
        return status, resp

    def escalate(self, kind: str, lease: dict, payload: dict):
        task_id = lease["lease_id"][len("lease-"):lease["lease_id"].rindex("-v")]
        body = {
            "type": kind,
            "message_id": self._mid(kind.lower()),
            "lease_version": lease["version"],
            "task_id": task_id,
            "payload": payload,
        }
        status, resp = self._call("POST", "/escalations", body)
        if status not in (200, 201, 204):
            # Network or server offline: hold message in local buffer for replay (FR-10), preserving message_id
            self.outbox_buffer.append(body)
        return status, resp

    def step(self) -> int:
        """Process new outbox rows once; returns how many were handled."""
        try:
            self.flush()
            status, out = self._call("GET", "/outbox")
            if status not in (200, 201, 204) or not isinstance(out, dict):
                return 0
            handled = 0
            for row in out.get("outbox", []):
                try:
                    topic, payload = row.get("topic"), row.get("payload")
                    if not topic or not isinstance(payload, dict):
                        continue
                    if topic == f"nimbus/{self.cluster}/probe":
                        self.heartbeat(); handled += 1
                    elif topic == f"nimbus/{self.cluster}/lease" and payload.get("lease_id") not in self.seen:
                        self.seen.add(payload["lease_id"])
                        handled += 1
                        self.robot_id = self.robot_id or payload["assigned_robots"][0]
                        if [s.get("cmd") for s in payload.get("task_sequence", [])] == ["stop"]:
                            self.stopped.add(payload["lease_id"])      # park and stay quiet: the cloud ended this task here
                            continue
                        self.checkin(payload)
                        if self.block and payload.get("version") == 1:
                            self.heartbeat()   # report where the robot is parked before the cloud has to plan around it
                            extra = {"robot_zone": self.robot_zone} if self.robot_zone else {}
                            self.escalate("UNEXPECTED_EVENT", payload, {
                                "what_happened": f"{self.block} is {self.state}", "where": self.block,
                                "robots_saw": [f"{payload.get('assigned_robots', ['R1'])[0]}: {self.block} {self.state}"],
                                "segments": [{"segment_id": self.block, "state": self.state}], **extra})
                        elif self.complete:
                            lid = payload["lease_id"]
                            task = lid[len("lease-"):lid.rindex("-v")]
                            self.escalate("OUTPUT_READY", payload, {"result_ref": f"s3://nimbus-outputs/{task}/delivery-proof.jpg"})
                except Exception as exc:
                    self.log.append(("step_row_error", 0, {"error": str(exc)}))
            return handled
        except Exception as exc:
            self.log.append(("step_error", 0, {"error": str(exc)}))
            return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--cluster", default="east-wing")
    ap.add_argument("--complete", action="store_true", help="send OUTPUT_READY for each lease")
    ap.add_argument("--block", help="send UNEXPECTED_EVENT blocking this zone for each first-version lease")
    ap.add_argument("--close", help="like --block, but the zone is reported closed (a gate or door), not blocked")
    ap.add_argument("--robot-zone", help="map node where the robot is parked when it reports (heartbeat and event)")
    ap.add_argument("--once", action="store_true", help="process the outbox once and exit")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument(
        "--fault-profile",
        choices=["healthy", "mild_wan", "shielded_room", "severe", "basement_outage"],
        help="Run through an in-process fault proxy with this degradation profile (E4-E6)",
    )
    ap.add_argument(
        "--fault-port",
        type=int,
        default=8001,
        help="Local port for the fault proxy when --fault-profile is active (default: 8001)",
    )
    a = ap.parse_args()

    proxy = None
    target_api = a.api
    if a.fault_profile:
        from scripts.fault_harness import FaultProxy, PRESET_PROFILES

        parsed = urllib.parse.urlparse(a.api)
        target_host = parsed.hostname or "127.0.0.1"
        target_port = parsed.port or 8000
        scheme = parsed.scheme or "http"

        profile = PRESET_PROFILES[a.fault_profile]
        proxy = FaultProxy(
            listen_host="127.0.0.1",
            listen_port=a.fault_port,
            target_host=target_host,
            target_port=target_port,
            profile=profile,
        )
        proxy.start()
        target_api = f"{scheme}://127.0.0.1:{a.fault_port}"
        print(f"[{a.cluster}] In-process FaultProxy active on {target_api} -> {target_host}:{target_port} (profile: {a.fault_profile})")

    try:
        sim = EdgeSim(target_api, a.cluster, complete=a.complete, block=a.close or a.block,
                      state="closed" if a.close else "blocked", robot_zone=a.robot_zone)
        try:
            sim.heartbeat()
        except Exception:
            pass
        while True:
            try:
                n = sim.step()
                if n:
                    print(f"[{a.cluster}] handled {n} message(s)")
            except Exception as exc:
                print(f"[{a.cluster}] step warning: {exc}")
            if a.once:
                break
            try:
                sim.heartbeat()
            except Exception:
                pass
            time.sleep(a.interval)
    finally:
        if proxy is not None:
            proxy.stop()


if __name__ == "__main__":
    main()
