"""Notices: what the cloud tells people and clusters besides leases (FR-9, FR-11).

Audience is always chosen by code (who is affected); the model only writes the
text. Notices to a cluster go out on `nimbus/{cluster}/notice` (addendum to
contracts/topics.md); requester and operator notices are read from
`GET /notifications`. The message id is a hash of the content, so a repeated
notice is recognised as a repeat (FR-8).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass

from .clock import Clock, iso

AUDIENCES = ("requester", "operator", "cluster", "place")
TYPES = ("ANSWER", "WARNING", "MAP", "STOP", "BUNDLE")


@dataclass
class Notice:
    audience: str                  # requester | operator | cluster
    text: str
    type: str = "WARNING"          # ANSWER | WARNING | MAP | STOP
    cluster_id: str | None = None  # required for audience "cluster"
    task_id: str | None = None
    lease_version: int | None = None
    zone: str | None = None        # for audience "place": the people working there


class Notifier:
    MAX_KEPT = 300

    def __init__(self, clock: Clock, durable=None):
        self.clock, self.durable = clock, durable
        self._lock = threading.Lock()
        self.feed: list[dict] = sorted(durable.load("notice").values(), key=lambda e: e["ts"]) if durable is not None else []

    def send(self, n: Notice) -> tuple[dict, tuple | None]:
        """Record a notice. Returns (feed entry, (topic, payload) to publish or None)."""
        if (n.audience not in AUDIENCES or n.type not in TYPES or (n.audience == "cluster" and not n.cluster_id)
                or (n.audience == "place" and not n.zone)):
            raise ValueError(f"bad notice {n}")
        mid = "notice-" + hashlib.sha1(json.dumps(asdict(n), sort_keys=True).encode()).hexdigest()[:10]
        with self._lock:
            for e in self.feed:
                if e["message_id"] == mid:
                    return e, None          # a repeat of something already said
            entry = {**asdict(n), "message_id": mid, "ts": iso(self.clock.now())}
            self.feed.append(entry)
            if self.durable is not None:
                self.durable.put("notice", mid, entry, ttl_s=7 * 86400)
                if len(self.feed) > self.MAX_KEPT:          # bounded: the oldest fall off memory and storage together
                    old = self.feed.pop(0)
                    self.durable.delete("notice", old["message_id"])
        if n.audience == "cluster":
            payload = {k: v for k, v in entry.items() if k != "audience" and v is not None}
            return entry, (f"nimbus/{n.cluster_id}/notice", payload)
        return entry, None

    def for_audience(self, audience: str | None = None, task_id: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.feed
                    if (audience is None or e["audience"] == audience) and (task_id is None or e["task_id"] == task_id)]
