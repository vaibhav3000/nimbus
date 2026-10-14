"""Fake edge cluster for the cloud side (Person 3), answering what P4 asked:
read leases from the outbox, send check-ins and OUTPUT_READY, and show up on
the timeline. Also reports cluster health, answers probes, and can raise an
UNEXPECTED_EVENT, which is how re-planning is exercised without a real edge.

    python scripts/edge_sim.py --api http://127.0.0.1:8000 --cluster east-wing --complete
    python scripts/edge_sim.py --cluster east-wing --block E3 --once

Real edge (Person 2) replaces this; the messages are the contract's.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from urllib import request as urlreq
from urllib.error import HTTPError


class EdgeSim:
    def __init__(self, api: str, cluster: str, *, complete: bool = False, block: str | None = None,
                 robots: list | None = None):
        self.api, self.cluster = api.rstrip("/"), cluster
        self.complete, self.block = complete, block
        self.robots = robots
        self.seen: set[str] = set()   # lease ids already answered
        self.log: list[tuple[str, int, dict]] = []

    def _call(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urlreq.Request(self.api + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            resp = urlreq.urlopen(req)
        except HTTPError as err:
            resp = err
        with resp:
            out = json.loads(resp.read().decode() or "{}")
            self.log.append((f"{method} {path}", resp.status, out))
            return resp.status, out

    @staticmethod
    def _mid(kind: str) -> str:
        return f"sim-{kind}-{uuid.uuid4().hex[:10]}"

    def heartbeat(self):
        body = {"cluster_id": self.cluster}
        if self.robots:
            body["robots"] = self.robots
        return self._call("POST", "/fleet/heartbeat", body)

    def checkin(self, lease: dict):
        body = {"cluster_id": self.cluster, "lease_id": lease["lease_id"], "lease_version": lease["version"],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "message_id": self._mid("ci")}
        if self.robots:
            body["robots"] = self.robots
        return self._call("POST", "/checkins", body)

    def escalate(self, kind: str, lease: dict, payload: dict):
        task_id = lease["lease_id"][len("lease-"):lease["lease_id"].rindex("-v")]
        return self._call("POST", "/escalations", {
            "type": kind, "message_id": self._mid(kind.lower()), "lease_version": lease["version"],
            "task_id": task_id, "payload": payload})

    def step(self) -> int:
        """Process new outbox rows once; returns how many were handled."""
        _, out = self._call("GET", "/outbox")
        handled = 0
        for row in out.get("outbox", []):
            topic, payload = row["topic"], row["payload"]
            if topic == f"nimbus/{self.cluster}/probe":
                self.heartbeat(); handled += 1
            elif topic == f"nimbus/{self.cluster}/lease" and payload["lease_id"] not in self.seen:
                self.seen.add(payload["lease_id"])
                handled += 1
                self.checkin(payload)
                if self.block and payload["version"] == 1:
                    self.escalate("UNEXPECTED_EVENT", payload, {
                        "what_happened": f"corridor {self.block} blocked by a cart", "where": self.block,
                        "robots_saw": [f"{payload['assigned_robots'][0]}: {self.block} blocked"],
                        "segments": [{"segment_id": self.block, "state": "blocked"}]})
                elif self.complete:
                    task = payload["lease_id"][len("lease-"):payload["lease_id"].rindex("-v")]
                    self.escalate("OUTPUT_READY", payload, {"result_ref": f"s3://nimbus-outputs/{task}/delivery-proof.jpg"})
        return handled


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--cluster", default="east-wing")
    ap.add_argument("--complete", action="store_true", help="send OUTPUT_READY for each lease")
    ap.add_argument("--block", help="send UNEXPECTED_EVENT blocking this zone for each first-version lease")
    ap.add_argument("--once", action="store_true", help="process the outbox once and exit")
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()
    sim = EdgeSim(a.api, a.cluster, complete=a.complete, block=a.block)
    sim.heartbeat()
    while True:
        n = sim.step()
        if n:
            print(f"[{a.cluster}] handled {n} message(s)")
        if a.once:
            break
        sim.heartbeat()
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
