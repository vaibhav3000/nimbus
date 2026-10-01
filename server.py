"""Nimbus local API and static app server (stdlib only).

One process serves the requester app and the JSON API. This is Person 4's
cloud stand-in boundary: Person 3 replaces the decision-making inside
standin.py (and eventually the storage in store.py with DynamoDB) without
touching these routes. Endpoints are the public seam; see SPEC-P4.md.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import contracts
import standin
from cost import CostBook
from store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Handler(BaseHTTPRequestHandler):
    store: Store  # injected per server instance
    cost: CostBook

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep demo output quiet

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        # G: one API Gateway request; D: response bytes crossing the wire.
        self.cost.record(apigw_requests=1, data_transfer_bytes=len(data))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def _task_view(self, task: dict) -> dict:
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "request_text": task["request_text"],
            "question": task.get("question"),
            "lease": task.get("lease"),
            "result_ref": task.get("result_ref"),
        }

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/tasks/"):
            task_id = path.split("/")[2]
            task = self.store.get_task(task_id)
            if task is None:
                self._send_json(404, {"error": f"unknown task {task_id}"})
            elif path.endswith("/timeline"):
                self._send_json(200, {"task_id": task_id, "timeline": self.store.timeline(task_id)})
            else:
                self._send_json(200, self._task_view(task))
        elif path == "/tasks":
            self._send_json(
                200, {"tasks": [self._task_view(t) for t in self.store.all_tasks()]}
            )
        elif path == "/cost":
            self._send_json(
                200,
                {
                    "counters": self.cost.snapshot(),
                    "tasks": len(self.store.tasks),
                },
            )
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if path == "/requests":
            self._post_request(body)
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    # -- request handling (the cloud stand-in's ingress) ------------------

    def _post_request(self, body: dict):
        text = body.get("text")
        message_id = body.get("message_id")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text is required"})
            return
        if not isinstance(message_id, str) or not message_id:
            self._send_json(400, {"error": "message_id is required"})
            return
        if message_id in self.store.seen_message_ids:
            self._send_json(200, {"duplicate": True})
            return
        self.store.see_message_id(message_id)

        task_id = f"task-{len(self.store.tasks) + 1:06d}"
        task = {
            "task_id": task_id,
            "message_id": message_id,
            "request_text": text,
            "status": "received",
            "question": None,
            "lease": None,
            "result_ref": None,
        }
        self.store.append_decision(
            {
                "who": "requester",
                "inputs": {"text": text},
                "output": {"event": "request_received"},
                "lease_version": None,
                "timestamp": standin.now_iso(),
                "task_id": task_id,
            }
        )

        lease = standin.build_lease(task_id, text)
        errors = contracts.validate_document("lease", lease)
        if errors:
            task["status"] = "rejected"
            self.store.put_task(task)
            self._send_json(500, {"error": "stand-in produced an invalid lease", "detail": errors})
            return
        # FR-15 order: the LLM (stand-in) proposes, the rule checker verifies,
        # only then is the lease issued. Three cloud/rule rows plus the
        # requester row give a replayable chain.
        self.store.append_decision(
            {
                "who": "cloud",
                "inputs": {"request_text": text},
                "output": {
                    "decision": "propose_lease",
                    "cluster": lease["cluster_id"],
                    "robots": lease["assigned_robots"],
                    "zones": lease["allowed_zones"],
                },
                "lease_version": None,
                "timestamp": standin.now_iso(),
                "task_id": task_id,
            }
        )
        self.store.append_decision(
            {
                "who": "rule",
                "inputs": {
                    "proposal": {
                        "cluster": lease["cluster_id"],
                        "robots": lease["assigned_robots"],
                        "zones": lease["allowed_zones"],
                    }
                },
                "output": {"decision": "approve", "checks": ["zones", "robots", "route"]},
                "lease_version": lease["version"],
                "timestamp": standin.now_iso(),
                "task_id": task_id,
            }
        )
        task["lease"] = lease
        task["status"] = "leased"
        self.store.put_task(task)
        self.store.append_decision(
            {
                "who": "cloud",
                "inputs": {"lease_id": lease["lease_id"]},
                "output": {"decision": "issue_lease", "lease_id": lease["lease_id"]},
                "lease_version": lease["version"],
                "timestamp": standin.now_iso(),
                "task_id": task_id,
            }
        )
        # T_in/T_out stand-ins (Person 3 reads real usage from Bedrock);
        # M: lease published to the cluster; W: rows written; F/K: stand-ins.
        # ponytail: Fargate GB-s as a flat per-request constant, replace with
        # wall-clock * vCPU/mem if the report needs finer Fargate accounting.
        self.cost.record(
            input_tokens=max(1, len(text) // 4),
            output_tokens=150,  # the PRD's 150-token decision cap
            iot_messages=1,
            dynamodb_ops=5,
            fargate_gbs=0.001,
            cloudwatch_metrics=1,
        )
        self._send_json(
            201,
            {
                "duplicate": False,
                "task_id": task_id,
                "status": task["status"],
                "question": None,
            },
        )


def make_server(host: str, port: int, data_dir: Path) -> ThreadingHTTPServer:
    data_dir = Path(data_dir)
    handler = type(
        "BoundHandler",
        (Handler,),
        {"store": Store(data_dir), "cost": CostBook(data_dir)},
    )
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8000"))
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    httpd = make_server("0.0.0.0", port, data_dir)
    print(f"Nimbus local API + app on http://127.0.0.1:{port} (data in {data_dir}/)")
    httpd.serve_forever()
