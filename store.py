"""File-backed stores: the task map and the append-only decision log.

State lives under one data directory: tasks.json holds every task plus the
set of message ids already seen (FR-8: a repeated message_id must be ignored
even across restarts), decisions.jsonl is the decision log (FR-20: append
only). In the real deployment DynamoDB replaces this module; its two public
operations (task get/put, decision append) are the same.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()

_SEEN_KEY = "_seen_message_ids"


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_path = self.data_dir / "tasks.json"
        self.decisions_path = self.data_dir / "decisions.jsonl"
        self.tasks: dict = {}
        self.seen_message_ids: set[str] = set()
        if self.tasks_path.exists():
            blob = json.loads(self.tasks_path.read_text(encoding="utf-8"))
            self.seen_message_ids = set(blob.get(_SEEN_KEY, []))
            self.tasks = {k: v for k, v in blob.items() if k != _SEEN_KEY}
        self._task_seq = max(
            (int(t["task_id"].split("-")[1]) for t in self.tasks.values() if t["task_id"].startswith("task-")),
            default=0,
        )

    def _save_tasks(self) -> None:
        blob = dict(self.tasks)
        blob[_SEEN_KEY] = sorted(self.seen_message_ids)
        tmp = self.tasks_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        os.replace(tmp, self.tasks_path)

    def next_task_id(self) -> str:
        """Allocate the next task id from a monotonic counter under the lock,
        so concurrent requests can never mint the same id."""
        with _LOCK:
            self._task_seq += 1
            return f"task-{self._task_seq:06d}"

    def get_task(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)

    def all_tasks(self) -> list[dict]:
        return sorted(self.tasks.values(), key=lambda t: t["task_id"])

    def put_task(self, task: dict) -> None:
        with _LOCK:
            self.tasks[task["task_id"]] = task
            if task.get("message_id"):
                self.seen_message_ids.add(task["message_id"])
            self._save_tasks()

    def see_message_id(self, message_id: str) -> None:
        with _LOCK:
            self.seen_message_ids.add(message_id)
            self._save_tasks()

    def append_decision(self, row: dict) -> None:
        """Append one decision-log row (FR-20); rows are never edited."""
        with _LOCK:
            with self.decisions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def append_outbox(self, entry: dict) -> None:
        """Append one {topic, payload} publication to the outbox file.

        The outbox is the cloud-edge messaging stand-in (PRD Fig 4 component
        6): Person 2 reads data/outbox.jsonl until a real IoT client exists;
        the API exposes the same rows via GET /outbox.
        """
        with _LOCK:
            with (self.data_dir / "outbox.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def outbox(self) -> list[dict]:
        path = self.data_dir / "outbox.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def timeline(self, task_id: str) -> list[dict]:
        """All decision-log rows for one task, in append order (FR-20)."""
        if not self.decisions_path.exists():
            return []
        rows = []
        for line in self.decisions_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("task_id") == task_id:
                    rows.append(row)
        return rows
