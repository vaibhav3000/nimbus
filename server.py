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
        if path == "/":
            self._send_static("index.html")
        elif path == "/app.js":
            self._send_static("app.js")
        elif path.startswith("/tasks/"):
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
        elif path.startswith("/schemas/"):
            self._send_schema(path[len("/schemas/"):])
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    def _send_static(self, filename: str) -> None:
        """Serve the requester app (FR-21). Files only from STATIC_DIR; the
        names are fixed here, so no path traversal is possible."""
        allowed = {"index.html": "text/html", "app.js": "application/javascript"}
        if filename not in allowed:
            self._send_json(404, {"error": "not found"})
            return
        data = (STATIC_DIR / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", allowed[filename])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_schema(self, name: str) -> None:
        """Serve the published contracts so Person 3 can fetch them at plug-in
        time instead of reaching into the repo."""
        if not name.endswith(".json"):
            name += ".json"
        try:
            schema = contracts.load_schema(name[: -len(".json")])
        except KeyError:
            self._send_json(404, {"error": f"unknown schema {name}"})
            return
        self._send_json(200, schema)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if path == "/requests":
            self._post_request(body)
        elif path.startswith("/requests/") and path.endswith("/answer"):
            self._post_answer(path.split("/")[2], body)
        elif path == "/escalations":
            self._post_escalation(body)
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    # -- request handling (the cloud stand-in's ingress) ------------------

    def _row(self, who: str, inputs: dict, output: dict, lease_version, task_id: str) -> dict:
        """One decision-log entry (FR-20 contract shape)."""
        return {
            "who": who,
            "inputs": inputs,
            "output": output,
            "lease_version": lease_version,
            "timestamp": standin.now_iso(),
            "task_id": task_id,
        }

    def _dedupe(self, body: dict) -> tuple[bool, str | None]:
        """FR-8 shape: a repeated message_id is acknowledged, not acted on.

        Check only — the caller records the id once the message is accepted,
        so a delivery that fails validation never burns its id and an honest
        retry still goes through.
        """
        message_id = body.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            self._send_json(400, {"error": "message_id is required"})
            return True, None
        if message_id in self.store.seen_message_ids:
            self._send_json(200, {"duplicate": True})
            return True, message_id
        return False, message_id

    def _post_request(self, body: dict):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text is required"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return

        task_id = self.store.next_task_id()
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
            self._row("requester", {"text": text}, {"event": "request_received"}, None, task_id)
        )

        if standin.wants_clarification(text):
            question = standin.clarifying_question()
            task["status"] = "needs_clarification"
            task["question"] = question
            self.store.put_task(task)  # records task["message_id"] as seen
            self.store.append_decision(
                self._row(
                    "cloud",
                    {"request_text": text},
                    {"decision": "ask_clarify", "question": question},
                    None,
                    task_id,
                )
            )
            self._record_cloud_decision(text)
            self._send_json(
                201,
                {
                    "duplicate": False,
                    "task_id": task_id,
                    "status": task["status"],
                    "question": question,
                },
            )
            return

        self._issue_lease(task, text)
        self._send_json(
            201,
            {
                "duplicate": False,
                "task_id": task_id,
                "status": task["status"],
                "question": None,
            },
        )

    def _post_answer(self, task_id: str, body: dict):
        task = self.store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"unknown task {task_id}"})
            return
        if task["status"] != "needs_clarification":
            self._send_json(409, {"error": f"task {task_id} is not awaiting an answer"})
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text is required"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return

        task["message_id"] = message_id
        self.store.append_decision(
            self._row(
                "requester",
                {"text": text, "question": task["question"]},
                {"event": "clarification_answered"},
                None,
                task_id,
            )
        )
        # Urgency and zones come from the original request, so the lease is
        # built from request + answer together.
        self._issue_lease(task, f"{task['request_text']} {text}")
        self._send_json(
            201,
            {
                "duplicate": False,
                "task_id": task_id,
                "status": task["status"],
                "lease": task["lease"],
            },
        )

    def _issue_lease(self, task: dict, text: str) -> None:
        """The stand-in's FR-15 order: propose, verify, then issue (v1)."""
        task_id = task["task_id"]
        lease = standin.build_lease(task_id, text)
        errors = contracts.validate_document("lease", lease)
        if errors:
            task["status"] = "rejected"
            self.store.put_task(task)
            self._send_json(500, {"error": "stand-in produced an invalid lease", "detail": errors})
            return
        proposal = {
            "cluster": lease["cluster_id"],
            "robots": lease["assigned_robots"],
            "zones": lease["allowed_zones"],
        }
        self.store.append_decision(
            self._row(
                "cloud", {"request_text": text}, {"decision": "propose_lease", **proposal},
                None, task_id,
            )
        )
        self.store.append_decision(
            self._row(
                "rule", {"proposal": proposal},
                {"decision": "approve", "checks": ["zones", "robots", "route"]},
                lease["version"], task_id,
            )
        )
        task["lease"] = lease
        task["status"] = "leased"
        task["question"] = None
        self.store.put_task(task)
        self.store.append_decision(
            self._row(
                "cloud", {"lease_id": lease["lease_id"]},
                {"decision": "issue_lease", "lease_id": lease["lease_id"]},
                lease["version"], task_id,
            )
        )
        self._record_cloud_decision(text)

    def _record_cloud_decision(self, text: str) -> None:
        # T_in/T_out stand-ins (Person 3 reads real usage from Bedrock);
        # M: lease published to the cluster; W: rows written; F/K: stand-ins.
        # ponytail: Fargate GB-s as a flat per-decision constant, replace with
        # wall-clock * vCPU/mem if the report needs finer Fargate accounting.
        self.cost.record(
            input_tokens=max(1, len(text) // 4),
            output_tokens=150,  # the PRD's 150-token decision cap
            iot_messages=1,
            dynamodb_ops=5,
            fargate_gbs=0.001,
            cloudwatch_metrics=1,
        )

    def _post_escalation(self, body: dict):
        """Ingress for edge escalations (PRD Table 2; Person 3 replaces the
        reaction logic, the route and its guards stay)."""
        esc_type = body.get("type")
        task_id = body.get("task_id")
        if esc_type not in ("CLARIFY", "UNEXPECTED_EVENT", "OUTPUT_READY"):
            self._send_json(400, {"error": "unknown escalation type"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return
        errors = contracts.validate_document("escalation", body)
        if errors:
            self._send_json(400, {"error": "escalation violates schema", "detail": errors})
            return
        task = self.store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"unknown task {task_id}"})
            return
        current = task["lease"]["version"] if task.get("lease") else 0
        if body["lease_version"] < current:
            # FR-8: outdated lease versions are dropped, never acted on.
            self._send_json(409, {"error": "outdated lease_version", "current": current})
            return
        if esc_type == "OUTPUT_READY":
            task["status"] = "completed"
            task["result_ref"] = body["payload"]["result_ref"]
            self.store.put_task(task)
        self.store.see_message_id(message_id)  # accepted: record for FR-8 dedup
        self.store.append_decision(
            self._row(
                "edge", dict(body["payload"]), {"event": esc_type},
                body["lease_version"], task_id,
            )
        )
        # M: escalation arrived via the messaging queue; Q: queue delivery;
        # W: log row; S: result storage. CLARIFY/UNEXPECTED_EVENT reactions
        # (re-plan, ask the user) are Person 3's orchestrator.
        cost = {"sqs_messages": 1, "dynamodb_ops": 1}
        if esc_type == "OUTPUT_READY":
            cost["s3_ops"] = 1
        self.cost.record(**cost)
        self._send_json(201, {"duplicate": False, "accepted": True, "status": task["status"]})


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
