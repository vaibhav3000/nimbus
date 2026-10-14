"""Nimbus local API and static app server (stdlib only).

One process serves the requester app and the JSON API. This is Person 4's
cloud stand-in boundary: Person 3 replaces the decision-making inside
standin.py (and eventually the storage in store.py with DynamoDB) without
touching these routes. Endpoints are the public seam; see SPEC-P4.md.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import contracts
from cloud.clock import iso
from cloud.orchestrator import InvalidLeaseError, Orchestrator, StaleVersion
from cost import CostBook
from store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Handler(BaseHTTPRequestHandler):
    store: Store  # injected per server instance
    cost: CostBook
    orch: Orchestrator  # Person 3's cloud decision plane (replaces standin.py)

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
            "spec": task.get("spec"),
            "notice": task.get("notice"),
            "reason": task.get("reason"),
        }

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_static("index.html")
        elif path == "/app.js":
            self._send_static("app.js")
        elif path == "/operator":
            self._send_static("operator.html")
        elif path == "/operator.js":
            self._send_static("operator.js")
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
                    "tasks": len(self.store.all_tasks()),
                },
            )
        elif path == "/fleet":
            self._send_json(200, {"fleet": self.orch.fleet.snapshot(), "mode": self.orch.mode,
                                  "llm": getattr(self.orch.llm, "model_id", "?"), "pack": self.orch.pack.id})
        elif path == "/pack":
            pk = self.orch.pack
            self._send_json(200, {"id": pk.id, "name": pk.name, "version": pk.doc["version"],
                                  "description": pk.doc.get("description", ""),
                                  "items": [i["id"] for i in pk.items],
                                  "priorities": [p["id"] for p in pk.priorities],
                                  "rules": [r["id"] for r in pk.rules],
                                  "restrictions": sorted(pk.restrictions),
                                  "tools": {s: self.orch.tools.names(s) for s in pk.doc["steps"]}})
        elif path == "/outbox":
            self._send_json(200, {"outbox": self.store.outbox()})
        elif path.startswith("/schemas/"):
            self._send_schema(path[len("/schemas/"):])
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    def _send_static(self, filename: str) -> None:
        """Serve the requester app (FR-21). Files only from STATIC_DIR; the
        names are fixed here, so no path traversal is possible."""
        allowed = {
            "index.html": "text/html",
            "app.js": "application/javascript",
            "operator.html": "text/html",
            "operator.js": "application/javascript",
        }
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
        elif path == "/fleet/heartbeat":
            self._post_heartbeat(body)
        elif path.startswith("/tasks/") and path.endswith("/replan"):
            self._post_replan(path.split("/")[2], body)
        elif path == "/checkins":
            self._post_checkin(body)
        elif path.startswith("/tasks/") and path.endswith("/control"):
            self._post_control(path.split("/")[2], body)
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
            "timestamp": iso(self.orch.clock.now()),
            "task_id": task_id,
        }


    def _dedupe(self, body: dict) -> tuple[bool, str | None]:
        """FR-8 shape: a repeated message_id is acknowledged, not acted on.

        Check only : the caller records the id once the message is accepted,
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
        self._decide(task, lambda: self.orch.handle_request(
            task, text, history=self.store.timeline(task_id)), created=True)

    def _decide(self, task: dict, run, *, created: bool = False) -> bool:
        """Run one orchestrator decision and answer the caller. Returns False
        after sending an error, in which case nothing more may be sent."""
        try:
            outcome = run()
        except InvalidLeaseError as exc:
            task["status"] = "rejected"
            task.pop("message_id", None)  # faulted delivery must not burn the id
            self.store.put_task(task)
            self._send_json(500, {"error": "orchestrator produced an invalid lease", "detail": exc.errors})
            return False
        self._apply(task, outcome)
        view = {"duplicate": False, "task_id": task["task_id"], "status": task["status"]}
        if created or task["status"] == "needs_clarification":
            view["question"] = task.get("question")
        if not created:
            view["lease"] = task.get("lease")
        for key in ("notice", "reason"):
            if task.get(key):
                view[key] = task[key]
        self._send_json(201, view)
        return True

    def _apply(self, task: dict, outcome) -> None:
        """Persist an orchestrator Outcome: log rows first, then the state they
        explain (FR-20 replay order), then publish and meter."""
        task_id = task["task_id"]
        for r in outcome.rows:
            self.store.append_decision(self._row(r["who"], r["inputs"], r["output"], r["lease_version"], task_id))
        task["notice"] = outcome.notice
        if outcome.spec:
            task["spec"] = outcome.spec
        if outcome.kind == "clarify":
            task["status"], task["question"] = "needs_clarification", outcome.question
        elif outcome.kind == "leased":
            task["status"], task["question"], task["reason"] = "leased", None, None
            task["lease"] = outcome.lease
            if outcome.reservation:
                task["reservation"], task["stock_released"] = outcome.reservation, False
        elif outcome.kind == "queued":
            task["status"], task["reason"] = "queued", outcome.reason
        elif outcome.kind == "rejected":
            task["status"], task["reason"] = "rejected", outcome.reason
        self.store.put_task(task)  # records task["message_id"] as seen
        for topic, payload in outcome.publish:
            self.store.append_outbox({"topic": topic, "payload": payload})
        # T_in/T_out from the model's usage (estimates for the offline stand-in);
        # M: leases published; W: rows + task write; F/K: flat per-decision stand-ins.
        self.cost.record(
            input_tokens=outcome.tokens_in,
            output_tokens=outcome.tokens_out,
            iot_messages=len(outcome.publish),
            dynamodb_ops=len(outcome.rows) + 2,
            fargate_gbs=0.001,
            cloudwatch_metrics=1,
        )

    def _drain(self) -> None:
        """Capacity freed: lease queued tasks in priority/deadline order."""
        for task, outcome in self.orch.drain(self.store.get_task):
            self._apply(task, outcome)


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
        question = task["question"]
        self.store.append_decision(
            self._row(
                "requester",
                {"text": text, "question": question},
                {"event": "clarification_answered"},
                None,
                task_id,
            )
        )
        # The model sees the whole exchange: request, its question, the answer.
        self._decide(task, lambda: self.orch.handle_request(
            task, task["request_text"], question=question, answer=text,
            history=self.store.timeline(task_id)))


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
            self.orch.finish(task)  # robots and zones are free again
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
        replanned = None
        if esc_type == "UNEXPECTED_EVENT" and task.get("spec") and task.get("lease"):
            blocked = _blocked_zones(self.orch.topo, body["payload"])
            if blocked:
                outcome = self.orch.replan(task, blocked_zones=blocked, reason=body["payload"]["what_happened"])
                self._apply(task, outcome)
                replanned = outcome.kind == "leased"
        if esc_type == "OUTPUT_READY":
            self._drain()
        reply = {"duplicate": False, "accepted": True, "status": task["status"]}
        if replanned is not None:
            reply.update(replanned=replanned, lease_version=task["lease"]["version"])
        self._send_json(201, reply)


    def _post_checkin(self, body: dict):
        """Edge check-in ingress (shape in contracts/topics.md): record one
        decision-log row, nothing more: no re-planning, no routing. Person 3
        replaces the reaction with lease renewal."""
        dup, message_id = self._dedupe(body)
        if dup:
            return
        missing = [
            k for k in ("cluster_id", "lease_id", "lease_version", "timestamp")
            if k not in body
        ]
        if missing:
            self._send_json(400, {"error": f"check-in missing {missing}"})
            return
        match = re.fullmatch(r"lease-(.+)-v\d+", body["lease_id"])
        if not match:
            self._send_json(400, {"error": "lease_id must be lease-<task_id>-v<version>"})
            return
        digest = {k: body[k] for k in ("robots", "stock") if k in body}
        self.orch.fleet.heartbeat(body["cluster_id"], digest)  # alive, whatever the lease says
        try:
            renewed = self.orch.renew(match.group(1), body["lease_version"])
        except StaleVersion as exc:
            self._send_json(409, {"error": "outdated lease_version", "current": exc.current})
            return
        row = self._row(
            "edge",
            {"cluster_id": body["cluster_id"], "lease_id": body["lease_id"],
             "checkin_timestamp": body["timestamp"]},
            {"event": "checkin"},
            body["lease_version"],
            match.group(1),
        )
        errors = contracts.validate_document("decision_log_entry", row)
        if errors:
            self._send_json(400, {"error": "check-in violates decision-log contract", "detail": errors})
            return
        self.store.see_message_id(message_id)  # accepted: record for FR-8 dedup
        self.store.append_decision(row)
        self.cost.record(sqs_messages=1, dynamodb_ops=1)  # queue delivery + log row
        reply = {"duplicate": False, "accepted": True}
        if renewed:
            task = self.store.get_task(match.group(1))
            if task and task.get("lease"):
                task["lease"] = renewed
                self.store.put_task(task)
            self.cost.record(iot_messages=1, dynamodb_ops=1)  # renewal message + lease write
            reply["renewed_until"] = renewed["expiry_ts"]
        self._send_json(201, reply)


    def _post_control(self, task_id: str, body: dict):
        """Operator pause/cancel: a RECORDED INTENT only. One decision-log row
        (who=rule) plus the status change; nothing is delivered to a robot.
        Person 3's real console wires delivery through the messaging layer."""
        action = body.get("action")
        if action not in ("pause", "cancel"):
            self._send_json(400, {"error": "action must be pause or cancel"})
            return
        task = self.store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"unknown task {task_id}"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return
        lease_version = task["lease"]["version"] if task.get("lease") else None
        row = self._row(
            "rule",
            {"action": action, "by": "operator"},
            {"decision": f"operator_{action}"},
            lease_version,
            task_id,
        )
        errors = contracts.validate_document("decision_log_entry", row)
        if errors:
            self._send_json(400, {"error": "control intent violates decision-log contract", "detail": errors})
            return
        new_status = {"pause": "paused", "cancel": "cancelled"}[action]
        # Log row first, then the state it explains (FR-20 replay order).
        self.store.append_decision(row)
        task["status"] = new_status
        if action == "cancel":
            self.orch.cancel(task)  # frees robots and zones, returns the item
        self.store.put_task(task)
        self.store.see_message_id(message_id)  # accepted: record for FR-8 dedup
        self.cost.record(dynamodb_ops=2)  # status write + log row
        if action == "cancel":
            self._drain()
        self._send_json(201, {"duplicate": False, "accepted": True, "status": task["status"]})

    def _post_heartbeat(self, body: dict):
        """Cluster-level liveness with no lease attached (an idle cluster has
        nothing to check in about). Same optional robots[]/stock{} digest as a
        check-in; idempotent, so no message_id is required."""
        cluster_id = body.get("cluster_id")
        if not isinstance(cluster_id, str) or not cluster_id:
            self._send_json(400, {"error": "cluster_id is required"})
            return
        known = self.orch.fleet.heartbeat(cluster_id, {k: body[k] for k in ("robots", "stock") if k in body})
        if not known:
            self._send_json(404, {"error": f"unknown cluster {cluster_id}"})
            return
        self.cost.record(iot_messages=1)
        self._send_json(201, {"accepted": True, "health": self.orch.fleet.health(cluster_id)})
        self._drain()  # a cluster coming back may unblock queued work

    def _post_replan(self, task_id: str, body: dict):
        """Issue lease v(n+1) avoiding blocked zones: the explicit trigger for
        what an UNEXPECTED_EVENT does automatically. Lets the edge team and
        P4's stale-version tests drive the v2 path without an edge running."""
        task = self.store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"unknown task {task_id}"})
            return
        blocked = body.get("blocked_zones")
        if not isinstance(blocked, list) or not blocked or not all(isinstance(z, str) for z in blocked):
            self._send_json(400, {"error": "blocked_zones must be a non-empty list of zone ids"})
            return
        if not task.get("lease") or not task.get("spec"):
            self._send_json(409, {"error": f"task {task_id} has no lease to replace"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return
        reason = str(body.get("reason") or "operator re-plan")
        outcome = self.orch.replan(task, blocked_zones=blocked, reason=reason)
        self._apply(task, outcome)
        self.store.see_message_id(message_id)
        if outcome.kind != "leased":
            self._send_json(409, {"error": "no admissible re-plan", "detail": outcome.reason})
            return
        self._send_json(201, {"duplicate": False, "task_id": task_id, "lease": task["lease"]})



def _blocked_zones(topo, payload: dict) -> list[str]:
    """Zones an UNEXPECTED_EVENT says are unusable: structured segments first,
    otherwise any known zone id named in its 'where' text."""
    zones = [s["segment_id"] for s in payload.get("segments", [])
             if isinstance(s, dict) and s.get("state") in ("blocked", "closed", "crowded")
             and s.get("segment_id") in topo.nodes]
    if not zones:
        zones = [n for n in topo.nodes if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", payload.get("where", ""), re.I)]
    return sorted(set(zones))


def make_server(host: str, port: int, data_dir: Path) -> ThreadingHTTPServer:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)  # cost counters live here even when the store is DynamoDB
    if os.environ.get("NIMBUS_BACKEND", "local") == "aws":
        from cloud.aws_store import DynamoStore
        store = DynamoStore.from_env()
    else:
        store = Store(data_dir)

    def publish(topic: str, payload: dict) -> None:
        store.append_outbox({"topic": topic, "payload": payload})

    orch = Orchestrator.from_env(data_dir, publish)
    orch.recover(store.all_tasks())
    handler = type(
        "BoundHandler",
        (Handler,),
        {"store": store, "cost": CostBook(data_dir), "orch": orch},
    )
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8000"))
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    httpd = make_server("0.0.0.0", port, data_dir)
    print(f"Nimbus local API + app on http://127.0.0.1:{port} (data in {data_dir}/)")
    httpd.serve_forever()
