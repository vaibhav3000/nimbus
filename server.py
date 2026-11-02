"""Nimbus local API and static app server (stdlib only).

One process serves the requester app and the JSON API. This is Person 4's
cloud stand-in boundary: Person 3 replaces the decision-making inside
standin.py (and eventually the storage in store.py with DynamoDB) without
touching these routes. Endpoints are the public seam; see SPEC-P4.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable

import contracts
from cloud.clock import iso
from cloud.notify import Notice
from cloud.trace import wire_store
from cloud.orchestrator import InvalidLeaseError, Orchestrator, StaleVersion
from cost import CostBook
from mqtt_transport import is_mqtt_enabled, make_mqtt_transport_from_env
from store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"

_STATE_LOCK = threading.RLock()
TERMINAL_STATUSES = ("completed", "cancelled", "rejected")


def make_decision_row(orch: Orchestrator, who: str, inputs: dict, output: dict, lease_version, task_id: str) -> dict:
    """One decision-log entry (FR-20 contract shape)."""
    return {
        "who": who,
        "inputs": inputs,
        "output": output,
        "lease_version": lease_version,
        "timestamp": iso(orch.clock.now()),
        "task_id": task_id,
    }


def apply_outcome(orch, store, cost, publish, task: dict, outcome) -> None:
    """Persist an orchestrator Outcome: log rows first, then the state they explain (FR-20 replay order),
    then publish and meter. One copy for the HTTP handler and the MQTT ingress."""
    with _STATE_LOCK:
        task_id = task["task_id"]
        for r in outcome.rows:
            store.append_decision(make_decision_row(orch, r["who"], r["inputs"], r["output"], r["lease_version"], task_id))
        task["notice"] = outcome.notice
        if outcome.spec:
            task["spec"] = outcome.spec
        if outcome.legs is not None:
            task["legs"] = outcome.legs
        if outcome.leg_index is not None:
            task["leg_index"] = outcome.leg_index
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
        elif outcome.kind == "stood_down":
            task["lease"] = outcome.lease   # the stop lease is now the highest version the edge may honour
        elif outcome.kind == "held":
            task["status"], task["reason"], task["lease"] = "queued", outcome.reason, outcome.lease
        elif outcome.kind == "statement":
            task["status"], task["reason"] = "completed", outcome.reason   # a rule, not a delivery: nothing left to run
        store.put_task(task)  # records task["message_id"] as seen
        for topic, payload in outcome.publish:
            publish(topic, payload)
        for other, other_outcome in outcome.others:      # tasks this decision stopped or held (a new restriction)
            apply_outcome(orch, store, cost, publish, other, other_outcome)
        spawned = []
        for n, spec in enumerate(outcome.children, 1):   # further tasks the message created
            child_id = store.next_task_id()
            child = {"task_id": child_id, "message_id": f"{task.get('message_id') or task_id}#{n}",
                     "request_text": task["request_text"], "status": "received", "question": None, "lease": None,
                     "result_ref": None, "parent": task_id}
            store.append_decision(make_decision_row(orch, "cloud", {"parent": task_id, "spec": spec},
                                                    {"decision": "spawn_task"}, None, child_id))
            task.setdefault("children", []).append(child_id)
            spawned.append((child, spec))
        if outcome.children:
            store.put_task(task)
        # T_in/T_out from the model's usage (estimates for the offline stand-in);
        # M: leases published; W: rows + task write; F/K: flat per-decision stand-ins.
        cost.record(
            llm_calls=outcome.llm_calls,
            input_tokens=outcome.tokens_in,
            output_tokens=outcome.tokens_out,
            iot_messages=len(outcome.publish),
            dynamodb_ops=len(outcome.rows) + 2,
            fargate_gbs=0.001,
            cloudwatch_metrics=1,
        )
    # Planning can call the model (seconds): never while holding the lock that every check-in and heartbeat needs.
    for child, spec in spawned:
        apply_outcome(orch, store, cost, publish, child, orch.allocate_spec(child, spec))
    if outcome.drain:
        for queued, out in orch.drain(store.get_task):
            apply_outcome(orch, store, cost, publish, queued, out)


class Handler(BaseHTTPRequestHandler):
    store: Store  # injected per server instance
    cost: CostBook
    orch: Orchestrator  # Person 3's cloud decision plane (replaces standin.py)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep demo output quiet

    def publish(self, topic: str, payload: dict) -> None:
        """Publish outgoing messages (outbox mirror + optional MQTT)."""
        with _STATE_LOCK:
            self.store.append_outbox({"topic": topic, "payload": payload})

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, allow_nan=False).encode("utf-8")
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
            "children": task.get("children"),
            "parent": task.get("parent"),
            "legs": task.get("legs"),           # P3, additive: hand-over legs and which one is live
            "leg_index": task.get("leg_index"),
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
        elif path == "/map":
            self._send_static("map.html")
        elif path == "/map.json":
            from cloud import mapview
            self._send_json(200, mapview.build(self.orch, self.store.all_tasks()))
        elif path == "/operator.js":
            self._send_static("operator.js")
        elif path.startswith("/tasks/"):
            task_id = path.split("/")[2]
            task = self.store.get_task(task_id)
            if task is None:
                self._send_json(404, {"error": f"unknown task {task_id}"})
            elif path.endswith("/timeline"):
                self._send_json(200, {"task_id": task_id, "timeline": self.store.timeline(task_id)})
            elif path.endswith("/trace"):   # P3: the full conversation for this task, with the model calls in it
                self._send_json(200, {"task_id": task_id, "events": self.orch.trace.events(task_id=task_id)})
            else:
                self._send_json(200, self._task_view(task))
        elif path == "/tasks":
            self._send_json(
                200, {"tasks": [self._task_view(t) for t in self.store.all_tasks()]}
            )
        elif path == "/trace":
            q = parse_qs(urlparse(self.path).query)
            self._send_json(200, {"events": self.orch.trace.events(after=int((q.get("after") or ["0"])[0]),
                                                                   limit=int((q.get("limit") or ["200"])[0]))})
        elif path == "/cost":
            self._send_json(
                200,
                {
                    "counters": self.cost.snapshot(),
                    "tasks": len(self.store.all_tasks()),
                },
            )
        elif path == "/fleet":
            self._send_json(200, {"fleet": self.orch.fleet.snapshot(), "places": self.orch.fleet.place_snapshot(),
                                  "mode": self.orch.mode,
                                  "llm": getattr(self.orch.llm, "model_id", "?"), "pack": self.orch.pack.id})
        elif path == "/notifications":
            q = parse_qs(urlparse(self.path).query)
            self._send_json(200, {"notifications": self.orch.notifier.for_audience(
                (q.get("audience") or [None])[0], (q.get("task_id") or [None])[0])})
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
        elif path == "/fault":
            self._send_json(200, self._fault_view())
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
            "map.html": "text/html",          # P3: live map of the whole system
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
        elif path == "/fleet/stock":
            self._post_stock(body)
        elif path.startswith("/tasks/") and path.endswith("/replan"):
            self._post_replan(path.split("/")[2], body)
        elif path == "/checkins":
            self._post_checkin(body)
        elif path == "/map/reopen":
            zone = body.get("zone")
            node = self.orch.topo.resolve_node(zone) if isinstance(zone, str) else None
            if node is None:
                self._send_json(400, {"error": "zone must be a place on the map"})
            else:
                with _STATE_LOCK:
                    self.orch.mapstate.clear(node)
                    self._drain()    # work that was waiting on the closure may now be placed
                self._send_json(201, {"reopened": node})
        elif path == "/relay/decide":
            self._post_relay_decide(body)
        elif path.startswith("/tasks/") and path.endswith("/control"):
            self._post_control(path.split("/")[2], body)
        elif path == "/fault":
            self._post_fault(body)
        else:
            self._send_json(404, {"error": f"no route for {path}"})

    # -- request handling (the cloud stand-in's ingress) ------------------

    def _row(self, who: str, inputs: dict, output: dict, lease_version, task_id: str) -> dict:
        """One decision-log entry (FR-20 contract shape)."""
        return make_decision_row(self.orch, who, inputs, output, lease_version, task_id)

    def _dedupe(self, body: dict) -> tuple[bool, str | None]:
        """FR-8 shape: a repeated message_id is acknowledged, not acted on.

        Check only : the caller records the id once the message is accepted,
        so a delivery that fails validation never burns its id and an honest
        retry still goes through.
        """
        with _STATE_LOCK:
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

        with _STATE_LOCK:
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
            history = self.store.timeline(task_id)
        self._decide(task, lambda: self.orch.handle_request(task, text, history=history), created=True)

    def _decide(self, task: dict, run, *, created: bool = False) -> bool:
        """Run one orchestrator decision and answer the caller. Returns False
        after sending an error, in which case nothing more may be sent."""
        try:
            outcome = run()          # may call the model: not under the state lock
        except InvalidLeaseError as exc:
            with _STATE_LOCK:
                task["status"] = "rejected"
                task.pop("message_id", None)  # faulted delivery must not burn the id
                self.store.put_task(task)
            self._send_json(500, {"error": "orchestrator produced an invalid lease", "detail": exc.errors})
            return False
        with _STATE_LOCK:
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
        apply_outcome(self.orch, self.store, self.cost, self.publish, task, outcome)

    def _drain(self) -> None:
        """Capacity freed: lease queued tasks in priority/deadline order."""
        for task, outcome in self.orch.drain(self.store.get_task):    # planning outside the lock; apply_outcome locks
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

        if task.get("edge_question"):   # the edge asked (CLARIFY) and the cloud passed it on: relay the answer back
            self._relay_edge_answer(task, text, message_id)
            return
        with _STATE_LOCK:
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
            history = self.store.timeline(task_id)
        # The model sees the whole exchange: request, its question, the answer.
        self._decide(task, lambda: self.orch.handle_request(
            task, task["request_text"], question=question, answer=text, history=history))

    def _post_escalation(self, body: dict):
        """Ingress for edge escalations (PRD Table 2; Person 3 replaces the
        reaction logic, the route and its guards stay)."""
        code, resp = process_escalation(
            self.store, self.orch, self.cost, body,
            apply_fn=self._apply, drain_fn=self._drain,
        )
        self._send_json(code, resp)

    def _post_checkin(self, body: dict):
        """Edge check-in ingress (shape in contracts/topics.md): record one
        decision-log row, nothing more: no re-planning, no routing. Person 3
        replaces the reaction with lease renewal."""
        code, resp = process_checkin(self.store, self.orch, self.cost, body)
        self._send_json(code, resp)

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
        if task["status"] in TERMINAL_STATUSES:
            self._send_json(409, {"error": f"task is already {task['status']}", "status": task["status"]})
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
        with _STATE_LOCK:
            new_status = {"pause": "paused", "cancel": "cancelled"}[action]
            # Log row first, then the state it explains (FR-20 replay order).
            self.store.append_decision(row)
            if self.orch.repo.current(task_id) is not None:   # FR-22: the stop must reach the robots, not just the record
                self._apply(task, self.orch.stand_down(task, f"operator {action}"))
            task["status"] = new_status
            if action == "cancel":
                self.orch.cancel(task)  # frees robots and zones, returns the item
            self.store.put_task(task)
            self.store.see_message_id(message_id)  # accepted: record for FR-8 dedup
            self.cost.record(dynamodb_ops=2)  # status write + log row
        if action == "cancel":
            self._drain()
        self._send_json(201, {"duplicate": False, "accepted": True, "status": task["status"]})

    def _relay_edge_answer(self, task: dict, text: str, message_id: str) -> None:
        """P3: the requester answered a question the edge asked. Log it, tell the cluster, resume the task."""
        lease = task.get("lease") or {}
        with _STATE_LOCK:
            self.store.append_decision(self._row("requester", {"text": text, "question": task["question"]},
                                                 {"event": "clarification_answered"}, lease.get("version"), task["task_id"]))
            entry, pub = self.orch.notifier.send(Notice("cluster", text, type="ANSWER", cluster_id=lease.get("cluster_id"),
                                                        task_id=task["task_id"], lease_version=lease.get("version")))
            if pub:
                self.publish(*pub)
            task["status"], task["question"], task["edge_question"] = "leased", None, False
            self.store.put_task(task)
            self.store.see_message_id(message_id)
            self.cost.record(iot_messages=1 if pub else 0, dynamodb_ops=2)
        self._send_json(201, {"duplicate": False, "accepted": True, "status": task["status"],
                              "relayed_to": lease.get("cluster_id")})

    def _post_heartbeat(self, body: dict):
        """Cluster-level liveness with no lease attached (an idle cluster has
        nothing to check in about). Same optional robots[]/stock{} digest as a
        check-in; idempotent, so no message_id is required."""
        cluster_id = body.get("cluster_id")
        if not isinstance(cluster_id, str) or not cluster_id:
            self._send_json(400, {"error": "cluster_id is required"})
            return
        with _STATE_LOCK:
            known = self.orch.fleet.heartbeat(cluster_id, {k: body[k] for k in ("robots", "stock") if k in body})
            if not known:
                self._send_json(404, {"error": f"unknown cluster {cluster_id}"})
                return
            self.cost.record(iot_messages=1)
        held = body.get("leases")      # optional (FR-12): the leases the edge still holds after an outage
        reconcile = self.orch.reconcile(cluster_id, held) if isinstance(held, list) and held else None
        self._drain()  # a cluster coming back may unblock queued work (retries only if capacity really changed)
        reply = {"accepted": True, "health": self.orch.fleet.health(cluster_id)}
        if reconcile is not None:
            reply["reconcile"] = reconcile
        self._send_json(201, reply)

    def _post_relay_decide(self, body: dict):
        """C2 (cloud-only): the edge is a thin relay and asks the cloud before each non-reflex
        step. One model round trip per call; the reply says proceed, wait, reroute or abort."""
        if self.orch.mode != "C2":
            self._send_json(409, {"error": f"relay decisions exist only in C2 (this service runs {self.orch.mode})"})
            return
        missing = [k for k in ("cluster_id", "lease_id", "lease_version", "step_index") if k not in body]
        match = re.fullmatch(r"lease-(.+)-v\d+", str(body.get("lease_id", "")))
        if missing or not match:
            self._send_json(400, {"error": f"relay request needs {missing or 'lease_id as lease-<task_id>-v<n>'}"})
            return
        dup, message_id = self._dedupe(body)
        if dup:
            return
        event = {k: body[k] for k in ("step_index", "cmd", "zone", "target", "situation", "blocked_zones", "attempt")
                 if k in body}
        task_id = match.group(1)
        self.orch.fleet.heartbeat(body["cluster_id"], {})
        try:
            res = self.orch.decide_step(task_id, body["lease_version"], body["cluster_id"], event)   # model call: unlocked
        except StaleVersion as exc:
            self._send_json(409, {"error": "outdated lease_version", "current": exc.current})
            return
        if "error" in res:
            self._send_json(res["status"], {"error": res["error"]})
            return
        with _STATE_LOCK:
            for r in res["rows"]:
                self.store.append_decision(self._row(r["who"], r["inputs"], r["output"], r["lease_version"], task_id))
            self.store.see_message_id(message_id)
            self.cost.record(llm_calls=res["calls"], input_tokens=res["tokens_in"], output_tokens=res["tokens_out"],
                             iot_messages=2, dynamodb_ops=len(res["rows"]), fargate_gbs=0.001, cloudwatch_metrics=1)
        self._send_json(201, {"duplicate": False, **res["decision"]})

    def _post_stock(self, body: dict):
        """Operator restock (added by P3): set absolute counts for one cluster,
        then retry the queued tasks that were waiting on stock."""
        cluster_id, counts = body.get("cluster_id"), body.get("stock")
        if cluster_id not in self.orch.fleet.clusters:
            self._send_json(404, {"error": f"unknown cluster {cluster_id!r}"})
            return
        known_items = {i["id"] for i in self.orch.pack.items}
        if (not isinstance(counts, dict) or not counts
                or not all(isinstance(q, int) and not isinstance(q, bool) and q >= 0 for q in counts.values())):
            self._send_json(400, {"error": "stock must be a non-empty object of item -> whole number >= 0"})
            return
        unknown = sorted(set(counts) - known_items)
        if unknown:
            self._send_json(400, {"error": f"unknown items {unknown}"})
            return
        self.cost.record(dynamodb_ops=len(counts))
        new_counts = self.orch.fleet.set_stock(cluster_id, counts)
        self._drain()  # restocked items may unblock queued work; do it before replying so the console is current
        self._send_json(201, {"accepted": True, "cluster_id": cluster_id, "stock": new_counts})

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
        with _STATE_LOCK:
            reason = str(body.get("reason") or "operator re-plan")
            outcome = self.orch.replan(task, blocked_zones=blocked, reason=reason)
            self._apply(task, outcome)
            self.store.see_message_id(message_id)
        if outcome.kind != "leased":
            self._send_json(409, {"error": "no admissible re-plan", "detail": outcome.reason})
            return
        self._send_json(201, {"duplicate": False, "task_id": task_id, "lease": task["lease"]})

    def _fault_view(self) -> dict:
        """Returns active network fault state for operator UI / diagnostics."""
        with _STATE_LOCK:
            state = None
            # 1. Server in-memory state (set via POST /fault or tests)
            if getattr(self.server, "fault_state", None):
                state = dict(self.server.fault_state)
                state.setdefault("active", True)

            # 2. Check data_dir / fault_status.json
            if state is None:
                data_dir = getattr(self, "data_dir", None) or Path("data")
                status_file = data_dir / "fault_status.json"
                if not status_file.exists():
                    status_file = Path("data/fault_status.json")
                if status_file.exists():
                    try:
                        data = json.loads(status_file.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            state = dict(data)
                    except Exception:
                        pass

            # 3. Environment variable fallback (NIMBUS_FAULT_PROFILE)
            if state is None:
                env_profile = os.environ.get("NIMBUS_FAULT_PROFILE", "").lower().strip()
                if env_profile:
                    try:
                        from scripts.fault_harness import PRESET_PROFILES
                        if env_profile in PRESET_PROFILES:
                            p = PRESET_PROFILES[env_profile]
                            state = {
                                "active": env_profile != "healthy",
                                "profile": p.name,
                                "latency_ms": p.latency_ms,
                                "jitter_ms": p.jitter_ms,
                                "loss_rate": p.loss_rate,
                                "disconnected": p.disconnected,
                            }
                    except Exception:
                        pass

            # 4. Default: healthy / no fault active
            if state is None:
                state = {
                    "active": False,
                    "profile": "healthy",
                    "latency_ms": 0.0,
                    "jitter_ms": 0.0,
                    "loss_rate": 0.0,
                    "disconnected": False,
                }

            # Normalize telemetry metrics & strictly ensure finite numbers
            def _clean_float(val, default=0.0):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    f = float(val)
                    if math.isfinite(f):
                        return max(0.0, f)
                return default

            def _clean_int(val, default=0):
                if isinstance(val, int) and not isinstance(val, bool):
                    return max(0, min(val, 2_147_483_647))
                return default

            state["latency_ms"] = _clean_float(state.get("latency_ms"), 0.0)
            state["jitter_ms"] = _clean_float(state.get("jitter_ms"), 0.0)
            state["loss_rate"] = max(0.0, min(1.0, _clean_float(state.get("loss_rate"), 0.0)))
            state["packets_dropped"] = _clean_int(state.get("packets_dropped"), 0)
            state["packets_forwarded"] = _clean_int(state.get("packets_forwarded"), 0)
            state["bytes_forwarded"] = _clean_int(state.get("bytes_forwarded"), 0)
            state["disconnected"] = bool(state.get("disconnected", False))
            state["active"] = bool(state.get("active", False))
            state["profile"] = str(state.get("profile", "healthy"))

            lat = state["latency_ms"]
            loss_pct = round(state["loss_rate"] * 100, 1)
            fwd = state["packets_forwarded"]
            drop = state["packets_dropped"]
            if state.get("disconnected"):
                state["summary"] = f"PARTITIONED (100% loss) | pkts: {fwd} fwd / {drop} drop"
            elif state.get("active"):
                state["summary"] = f"{lat:.1f}ms latency, {loss_pct}% loss | pkts: {fwd} fwd / {drop} drop"
            else:
                state["summary"] = "healthy (0ms, 0% loss)"

            return state

    def _post_fault(self, body: dict) -> None:
        """Set or clear active network fault state (local-only endpoint)."""
        if not isinstance(body, dict):
            self._send_json(400, {"error": "body must be a json object"})
            return
        with _STATE_LOCK:
            data_dir = getattr(self, "data_dir", None) or Path("data")
            status_file = data_dir / "fault_status.json"
            MAX_INT = 2_147_483_647

            # 1. Strict validation of body field types
            if "profile" in body:
                if not isinstance(body["profile"], str):
                    self._send_json(400, {"error": "profile must be a string"})
                    return
                profile_name = body["profile"].lower().strip()
            else:
                profile_name = "custom"

            if "active" in body and not isinstance(body["active"], bool):
                self._send_json(400, {"error": "active must be a boolean"})
                return

            if "disconnected" in body and not isinstance(body["disconnected"], bool):
                self._send_json(400, {"error": "disconnected must be a boolean"})
                return

            # Integer counters: reject bools, non-ints, non-finite, and negatives; cap large integers
            for k in ("packets_forwarded", "packets_dropped", "bytes_forwarded"):
                if k in body:
                    val = body[k]
                    if isinstance(val, bool) or not isinstance(val, int):
                        self._send_json(400, {"error": f"{k} must be an integer (no bools or strings allowed)"})
                        return
                    if val < 0:
                        self._send_json(400, {"error": f"{k} must be non-negative"})
                        return

            # Float parameters: reject bools, non-numbers, non-finite (NaN/Inf), and out-of-range
            for k in ("latency_ms", "jitter_ms"):
                if k in body:
                    val = body[k]
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        self._send_json(400, {"error": f"{k} must be a number (no bools or strings allowed)"})
                        return
                    if not math.isfinite(val):
                        self._send_json(400, {"error": f"{k} must be a finite number (NaN and Infinity not allowed)"})
                        return
                    if val < 0:
                        self._send_json(400, {"error": f"{k} must be non-negative"})
                        return

            if "loss_rate" in body:
                val = body["loss_rate"]
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    self._send_json(400, {"error": "loss_rate must be a number (no bools or strings allowed)"})
                    return
                if not math.isfinite(val):
                    self._send_json(400, {"error": "loss_rate must be a finite number (NaN and Infinity not allowed)"})
                    return
                if not (0.0 <= val <= 1.0):
                    self._send_json(400, {"error": "loss_rate must be between 0.0 and 1.0"})
                    return

            # 2. Handle clear / healthy profile
            if profile_name in ("none", "healthy", "clear") or body.get("active") is False:
                self.server.fault_state = None
                for p in (status_file, Path("data/fault_status.json")):
                    try:
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass
                self._send_json(200, {"accepted": True, "fault": self._fault_view()})
                return

            # 3. Preset profile resolution
            try:
                from scripts.fault_harness import PRESET_PROFILES
                p = PRESET_PROFILES.get(profile_name)
            except Exception:
                p = None
                PRESET_PROFILES = {}

            if p is None and profile_name not in ("custom",):
                valid = list(PRESET_PROFILES.keys()) if PRESET_PROFILES else ["healthy", "mild_wan", "shielded_room", "severe", "basement_outage"]
                self._send_json(400, {"error": f"unknown fault profile: '{profile_name}'. Expected one of {valid} or 'custom'"})
                return

            def_lat = p.latency_ms if p else 0.0
            def_jit = p.jitter_ms if p else 0.0
            def_loss = p.loss_rate if p else 0.0
            def_disc = p.disconnected if p else False

            lat = float(body.get("latency_ms", def_lat))
            jit = float(body.get("jitter_ms", def_jit))
            loss = float(body.get("loss_rate", def_loss))
            disc = bool(body.get("disconnected", def_disc))

            prev = getattr(self.server, "fault_state", {}) or {}
            fwd = min(MAX_INT, int(body.get("packets_forwarded", prev.get("packets_forwarded", 0))))
            drop = min(MAX_INT, int(body.get("packets_dropped", prev.get("packets_dropped", 0))))
            bytes_fwd = min(MAX_INT, int(body.get("bytes_forwarded", prev.get("bytes_forwarded", 0))))

            state = {
                "active": profile_name != "healthy",
                "profile": p.name if p else profile_name,
                "latency_ms": lat,
                "jitter_ms": jit,
                "loss_rate": loss,
                "disconnected": disc,
                "packets_forwarded": fwd,
                "packets_dropped": drop,
                "bytes_forwarded": bytes_fwd,
            }

            loss_pct = round(state["loss_rate"] * 100, 1)
            if state["disconnected"]:
                state["summary"] = f"PARTITIONED (100% loss) | pkts: {fwd} fwd / {drop} drop"
            elif state["active"]:
                state["summary"] = f"{lat:.1f}ms latency, {loss_pct}% loss | pkts: {fwd} fwd / {drop} drop"
            else:
                state["summary"] = "healthy (0ms, 0% loss)"

            self.server.fault_state = state
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                status_file.write_text(json.dumps(state, allow_nan=False), encoding="utf-8")
            except Exception:
                pass

            self._send_json(200, {"accepted": True, "fault": state})


def _blocked_zones(topo, payload: dict) -> list[str]:  # kept for callers; the logic lives in cloud/react.py
    from cloud.react import blocked_zones
    return blocked_zones(topo, payload)


def process_escalation(
    store: Store,
    orch: Orchestrator,
    cost: CostBook,
    body: dict,
    apply_fn: Callable[[dict, Any], None],
    drain_fn: Callable[[], None],
) -> tuple[int, dict]:
    """Ingress for edge escalations (PRD Table 2). Used by both HTTP and MQTT paths."""
    with _STATE_LOCK:
        message_id = body.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return 400, {"error": "message_id is required"}
        if message_id in store.seen_message_ids:
            return 200, {"duplicate": True}

        esc_type = body.get("type")
        task_id = body.get("task_id")
        if esc_type not in ("CLARIFY", "UNEXPECTED_EVENT", "OUTPUT_READY"):
            return 400, {"error": "unknown escalation type"}

        errors = contracts.validate_document("escalation", body)
        if errors:
            return 400, {"error": "escalation violates schema", "detail": errors}

        task = store.get_task(task_id)
        if task is None:
            return 404, {"error": f"unknown task {task_id}"}

        current = task["lease"]["version"] if task.get("lease") else 0
        if body["lease_version"] < current:
            return 409, {"error": "outdated lease_version", "current": current}

        next_leg = esc_type == "OUTPUT_READY" and task["status"] != "cancelled" and orch.has_next_leg(task)
        if next_leg:
            task.setdefault("leg_results", []).append(body["payload"]["result_ref"])
            store.put_task(task)
        elif esc_type == "OUTPUT_READY" and task["status"] != "cancelled":
            task["status"] = "completed"
            task["result_ref"] = body["payload"]["result_ref"]
            orch.finish(task)  # robots and zones are free again
            store.put_task(task)

        store.see_message_id(message_id)  # accepted: record for FR-8 dedup
        store.append_decision(
            make_decision_row(
                orch,
                "edge",
                dict(body["payload"]),
                {"event": esc_type},
                body["lease_version"],
                task_id,
            )
        )

        cost_map = {"sqs_messages": 1, "dynamodb_ops": 1}
        if esc_type == "OUTPUT_READY":
            cost_map["s3_ops"] = 1
        cost.record(**cost_map)
        reacts = esc_type in ("UNEXPECTED_EVENT", "CLARIFY") and bool(task.get("lease"))

    # What follows can call the model (seconds), so it runs without the lock that check-ins and heartbeats need.
    # Anything that moved meanwhile is handled by the lease store's conditional writes, not by holding everyone up.
    leg_outcome = orch.advance_leg(task) if next_leg else None            # a hand-over: the next leg is the next version
    react = None
    if reacts:
        react = (orch.react.on_event(task, body["payload"]) if esc_type == "UNEXPECTED_EVENT"
                 else orch.react.on_clarify(task, body["payload"], body["lease_version"]))

    with _STATE_LOCK:
        task = store.get_task(task_id) or task      # a check-in may have renewed the lease meanwhile: do not overwrite it
        replanned = None
        answer = None
        if leg_outcome is not None:
            apply_fn(task, leg_outcome)
        if react is not None:
            for r in react.rows:
                store.append_decision(make_decision_row(orch, r["who"], r["inputs"], r["output"], r["lease_version"], task_id))
            cost.record(llm_calls=react.llm_calls, input_tokens=react.tokens_in, output_tokens=react.tokens_out,
                        iot_messages=len(react.publish), dynamodb_ops=len(react.rows))
            for topic, payload in react.publish:
                orch.publish(topic, payload)
            if react.outcome is not None:
                apply_fn(task, react.outcome)
                replanned = react.outcome.kind == "leased"
            if react.question:
                task["status"], task["question"], task["edge_question"] = "needs_clarification", react.question, True
                store.put_task(task)
            answer = react.answer
        status, version = task["status"], (task.get("lease") or {}).get("version")

    if esc_type == "OUTPUT_READY":
        drain_fn()

    reply = {"duplicate": False, "accepted": True, "status": status}
    if replanned is not None:
        reply.update(replanned=replanned, lease_version=version)
    if answer is not None:
        reply["answer"] = answer
    return 201, reply


def process_checkin(
    store: Store,
    orch: Orchestrator,
    cost: CostBook,
    body: dict,
) -> tuple[int, dict]:
    """Edge check-in ingress (shape in contracts/topics.md). Used by both HTTP and MQTT paths."""
    with _STATE_LOCK:
        message_id = body.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return 400, {"error": "message_id is required"}
        if message_id in store.seen_message_ids:
            return 200, {"duplicate": True}

        missing = [
            k for k in ("cluster_id", "lease_id", "lease_version", "timestamp")
            if k not in body
        ]
        if missing:
            return 400, {"error": f"check-in missing {missing}"}

        match = re.fullmatch(r"lease-(.+)-v\d+", body["lease_id"])
        if not match:
            return 400, {"error": "lease_id must be lease-<task_id>-v<version>"}

        task_id = match.group(1)
        row = make_decision_row(
            orch,
            "edge",
            {
                "cluster_id": body["cluster_id"],
                "lease_id": body["lease_id"],
                "checkin_timestamp": body["timestamp"],
            },
            {"event": "checkin"},
            body["lease_version"],
            task_id,
        )
        errors = contracts.validate_document("decision_log_entry", row)
        if errors:
            return 400, {"error": "check-in violates decision-log contract", "detail": errors}
        live = orch.repo.current(task_id)
        if live and live["cluster_id"] != body["cluster_id"]:
            return 409, {"error": "lease belongs to another cluster", "cluster_id": live["cluster_id"]}

        digest = {k: body[k] for k in ("robots", "stock") if k in body}
        orch.fleet.heartbeat(body["cluster_id"], digest)  # alive, whatever the lease says
        try:
            renewed = orch.renew(task_id, body["lease_version"])
        except StaleVersion as exc:
            return 409, {"error": "outdated lease_version", "current": exc.current}

        store.see_message_id(message_id)  # accepted: record for FR-8 dedup
        store.append_decision(row)
        cost.record(sqs_messages=1, dynamodb_ops=1)  # queue delivery + log row
        reply = {"duplicate": False, "accepted": True}
        if renewed:
            task = store.get_task(task_id)
            if task and task.get("lease"):
                task["lease"] = renewed
                store.put_task(task)
            cost.record(iot_messages=1, dynamodb_ops=1)  # renewal message + lease write
            reply["renewed_until"] = renewed["expiry_ts"]
        return 201, reply


class NimbusServer(ThreadingHTTPServer):
    transport = None
    fault_state = None
    data_dir = None

    def server_close(self):
        if getattr(self, "ticker_stop", None):
            self.ticker_stop.set()
        if self.transport:
            try:
                self.transport.stop()
            except Exception:
                pass
        super().server_close()


def make_server(host: str, port: int, data_dir: Path) -> NimbusServer:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)  # cost counters live here even when the store is DynamoDB
    if os.environ.get("NIMBUS_BACKEND", "local") == "aws":
        from cloud.aws_store import DynamoStore
        store = DynamoStore.from_env()
    else:
        store = Store(data_dir)

    cost = CostBook(data_dir)
    transport = None
    if is_mqtt_enabled():
        transport = make_mqtt_transport_from_env()

    def publish(topic: str, payload: dict) -> None:
        with _STATE_LOCK:
            store.append_outbox({"topic": topic, "payload": payload})
            if transport:
                transport.publish(topic, payload)

    orch = Orchestrator.from_env(data_dir, publish)
    orch.load_task = store.get_task
    wire_store(orch.trace, store)   # P3: every message and decision becomes a trace event (see /tasks/{id}/trace)
    orch.recover(store.all_tasks())

    def apply_fn(task: dict, outcome) -> None:
        apply_outcome(orch, store, cost, publish, task, outcome)

    def drain_fn() -> None:
        with _STATE_LOCK:
            for task, outcome in orch.drain(store.get_task):
                apply_fn(task, outcome)

    if transport:
        def on_mqtt_escalation(topic: str, data: dict) -> None:
            body = (
                data["payload"]
                if isinstance(data, dict) and "topic" in data and isinstance(data.get("payload"), dict)
                else data
            )
            process_escalation(store, orch, cost, body, apply_fn=apply_fn, drain_fn=drain_fn)

        def on_mqtt_checkin(topic: str, data: dict) -> None:
            body = (
                data["payload"]
                if isinstance(data, dict) and "topic" in data and isinstance(data.get("payload"), dict)
                else data
            )
            parts = topic.split("/")
            if len(parts) >= 3 and isinstance(body, dict) and "cluster_id" not in body:
                body["cluster_id"] = parts[1]
            process_checkin(store, orch, cost, body)

        transport.subscribe("nimbus/+/escalation", on_mqtt_escalation)
        transport.subscribe("nimbus/+/checkin", on_mqtt_checkin)
        transport.start()

    handler = type(
        "BoundHandler",
        (Handler,),
        {"store": store, "cost": cost, "orch": orch, "publish": staticmethod(publish), "data_dir": data_dir},
    )
    server_inst = NimbusServer((host, port), handler)
    server_inst.transport = transport
    server_inst.store = store
    server_inst.cost = cost
    server_inst.orch = orch
    server_inst.data_dir = data_dir

    def tick_once():
        """One pass of the time-driven work (cloud/ticker.py): expiries, deadlines, silent clusters, queue."""
        res = orch.ticker.tick(store.all_tasks())          # may plan (model calls): outside the lock
        for task, outcome in res.apply:
            apply_fn(task, outcome)
        with _STATE_LOCK:
            for task in res.touched:
                store.put_task(task)
        return res

    server_inst.tick_once = tick_once
    tick_s = float(os.environ.get("NIMBUS_TICK_S", "5"))
    if tick_s > 0:
        stop = threading.Event()

        def loop():
            while not stop.wait(tick_s):
                try:
                    tick_once()
                except Exception as exc:       # a failed pass must never take the service down
                    logging.getLogger("nimbus.tick").warning("tick failed: %s", exc)

        threading.Thread(target=loop, name="nimbus-ticker", daemon=True).start()
        server_inst.ticker_stop = stop
    return server_inst


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8000"))
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    httpd = make_server("0.0.0.0", port, data_dir)
    print(f"Nimbus local API + app on http://127.0.0.1:{port} (data in {data_dir}/)")
    if is_mqtt_enabled():
        print("Nimbus MQTT transport enabled on canonical topics (nimbus/{cluster_id}/...)")
    httpd.serve_forever()
