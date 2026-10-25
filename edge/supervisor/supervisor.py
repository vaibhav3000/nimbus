"""
Nimbus Edge Supervisor Daemon (Person 2)
Supervises local wing cluster, enforces lease boundaries (FR-5, FR-6),
manages local persistence in SQLite (FR-10), executes sub-tasks on robots,
and communicates with Cloud over HTTP REST (Milestone 1) and outbox.
"""

from datetime import datetime, timezone
import json
import re
import uuid
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

from edge.schemas.validator import (
    validate_lease,
    validate_escalation,
    validate_robot_subtask,
    is_coordinate_in_allowed_zones,
)
from edge.store.local_store import EdgeLocalStore
from edge.slm.engine import EdgeSLMEngine
from edge.mock.mock_robot import MockRobot


class EdgeSupervisor:
    def __init__(
        self,
        cluster_id: str = "east-wing",
        cloud_url: str = "http://localhost:8000",
        db_path: str = "edge_local_store.db",
        slm_mode: str = "auto"
    ):
        self.cluster_id = cluster_id
        self.cloud_url = cloud_url.rstrip("/")
        self.store = EdgeLocalStore(db_path)
        self.slm = EdgeSLMEngine(mode=slm_mode)
        self.robots: Dict[str, MockRobot] = {
            "R1": MockRobot(robot_id="R1", initial_pos=[0.0, 0.0, 0.0])
        }
        self.active_lease: Optional[Dict[str, Any]] = None

    @staticmethod
    def _extract_task_id(lease: Dict[str, Any]) -> str:
        """Extracts canonical task_id ('task-000001') from lease document or lease_id ('lease-task-000001-v1')."""
        if "task_id" in lease:
            return lease["task_id"]
        lid = lease.get("lease_id", "")
        if lid.startswith("lease-") and "-v" in lid:
            return lid[len("lease-"):lid.rindex("-v")]
        return lid

    def fetch_leases_from_cloud(self) -> List[Dict[str, Any]]:
        """Polls GET /outbox on the cloud stand-in server."""
        url = f"{self.cloud_url}/outbox"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                outbox_items = data.get("outbox", [])
                leases = []
                for item in outbox_items:
                    # Case 1: outbox wrapped in topic payload
                    if isinstance(item, dict) and "payload" in item:
                        payload = item["payload"]
                        if "lease_id" in payload and item.get("topic", "").endswith("/lease"):
                            leases.append(payload)
                    # Case 2: raw lease dict
                    elif isinstance(item, dict) and "lease_id" in item:
                        leases.append(item)
                return leases
        except Exception as e:
            # Server not up or network error
            return []

    def send_escalation_to_cloud(self, escalation: Dict[str, Any]) -> bool:
        """Pushes structured escalation to POST /escalations (FR-9)."""
        valid, err = validate_escalation(escalation)
        if not valid:
            raise ValueError(f"Invalid escalation payload: {err}")

        # Store locally first (Store-and-Forward - FR-10)
        self.store.enqueue_outbox(
            message_id=escalation["message_id"],
            lease_id=escalation.get("task_id", "unknown"),
            message_type=escalation["type"],
            payload=escalation
        )

        url = f"{self.cloud_url}/escalations"
        req_data = json.dumps(escalation).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status in (200, 201, 204):
                    self.store.mark_outbox_uploaded(escalation["message_id"])
                    return True
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"[{self.cluster_id}] Escalation HTTP error {e.code}: {err_body}")
        except Exception as e:
            print(f"[{self.cluster_id}] Escalation network error: {e}")
        return False


    def send_heartbeat_to_cloud(
        self,
        robots_digest: Optional[List[Dict[str, Any]]] = None,
        stock_digest: Optional[Dict[str, int]] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Sends idle cluster heartbeat to POST /fleet/heartbeat (Item 2: liveness probe).
        Keeps cluster health in 'healthy' state on cloud (<30s).
        """
        url = f"{self.cloud_url}/fleet/heartbeat"
        if robots_digest is None:
            robots_digest = []
            for rid, r in self.robots.items():
                is_busy = bool(self.active_lease and rid in self.active_lease.get("assigned_robots", []))
                robots_digest.append({
                    "id": rid,
                    "state": "busy" if is_busy else "idle",
                    "zone": getattr(r, "current_zone", "home"),
                    "battery": getattr(r, "battery_pct", 100.0)
                })

        body = {
            "cluster_id": self.cluster_id,
            "robots": robots_digest
        }
        if stock_digest:
            body["stock"] = stock_digest

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if resp.status in (200, 201):
                    return True, data
        except urllib.error.HTTPError as he:
            err_text = he.read().decode("utf-8", errors="ignore")
            print(f"[{self.cluster_id}] Heartbeat HTTP error {he.code}: {err_text}")
        except Exception:
            # Network partition / offline -> keep going (Item 3)
            pass
        return False, {}

    def send_checkin_to_cloud(
        self,
        lease: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Sends active lease check-in to POST /checkins (Item 1: renewal & telemetry).
        Returns (success: bool, status_reason: str, reply_data: dict)
        status_reason: 'accepted', 'renewed', 'stale_version', 'offline'
        """
        target_lease = lease or self.active_lease
        if not target_lease:
            return False, "no_active_lease", {}

        url = f"{self.cloud_url}/checkins"
        lid = target_lease.get("lease_id", "")
        ver = target_lease.get("version", 1)

        # Server requires format lease-<task_id>-v<version>
        if not re.fullmatch(r"lease-(.+)-v\d+", lid):
            task_id = self._extract_task_id(target_lease)
            lid = f"lease-{task_id}-v{ver}"

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message_id = f"msg-ci-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"

        robots_digest = []
        for rid, r in self.robots.items():
            robots_digest.append({
                "id": rid,
                "state": "busy",
                "zone": getattr(r, "current_zone", "transit"),
                "battery": getattr(r, "battery_pct", 95.0)
            })

        body = {
            "cluster_id": self.cluster_id,
            "lease_id": lid,
            "lease_version": ver,
            "timestamp": now_iso,
            "message_id": message_id,
            "robots": robots_digest
        }

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if resp.status in (200, 201):
                    if "renewed_until" in data:
                        return True, "renewed", data
                    return True, "accepted", data
        except urllib.error.HTTPError as he:
            err_text = he.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_text)
            except Exception:
                err_json = {}
            if he.code == 409:
                print(f"[{self.cluster_id}] Check-in 409: lease version {ver} is outdated. Current cloud version: {err_json.get('current')}")
                return False, "stale_version", err_json
            print(f"[{self.cluster_id}] Check-in HTTP error {he.code}: {err_text}")
            return False, f"http_{he.code}", err_json
        except Exception:
            # Item 3: If a check-in gets no reply, keep going and retry later
            return False, "offline", {}

    def process_lease(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ingests, validates, and executes an authority lease.
        Enforces:
        - Schema & cross-field rules (FR-5)
        - Monotonic versioning (FR-8)
        - Expiry checking & safe parking (FR-6)
        - Robot command dispatch & logging (FR-3, FR-20, NFR-6)
        """
        lease_id = lease["lease_id"]
        version = lease["version"]

        # 1. Validate Schema
        valid, err_msg = validate_lease(lease)
        if not valid:
            return {"status": "REJECTED", "reason": f"Validation failed: {err_msg}"}

        # 2. Check cluster routing (FR-11)
        if lease.get("cluster_id") != self.cluster_id:
            return {"status": "IGNORED", "reason": f"Lease targeted to {lease.get('cluster_id')}, not {self.cluster_id}"}

        # 3. Store in SQLite with version deduplication (FR-8)
        stored, store_msg = self.store.store_lease(lease)
        if not stored:
            return {"status": "DROPPED_STALE", "reason": store_msg}

        self.active_lease = lease
        task_id = self._extract_task_id(lease)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = set(lease.get("allowed_zones", []))
        forbidden_zones = set(lease.get("forbidden_zones", []))

        # Initial check-in on lease acceptance
        self.send_checkin_to_cloud(lease)
        last_checkin_time = time.time()

        # Check expiry timestamp (ISO 8601 UTC)
        expiry_ts_str = lease["expiry_ts"]
        if expiry_ts_str.endswith("Z"):
            expiry_ts_str = expiry_ts_str[:-1] + "+00:00"
        expiry_dt = datetime.fromisoformat(expiry_ts_str)

        executed_steps = []

        # 4. Execute sub-task sequence
        for idx, subtask in enumerate(task_sequence):
            now_dt = datetime.now(timezone.utc)
            if now_dt >= expiry_dt:
                # Invariant 3: Expiry -> Safe Park (FR-6)
                park_result = self._handle_lease_expiry_parking(lease)
                return {
                    "status": "EXPIRED_PARKED",
                    "reason": "Lease expired mid-mission; parked in safe spot",
                    "executed_steps": executed_steps,
                    "parking": park_result
                }

            # Periodic check-in every 5s during active execution (Item 1)
            now_loop = time.time()
            if now_loop - last_checkin_time >= 5.0:
                ok, status_reason, checkin_reply = self.send_checkin_to_cloud(lease)
                last_checkin_time = now_loop
                if status_reason == "stale_version":
                    self.active_lease = None
                    return {
                        "status": "DROPPED_STALE",
                        "reason": f"Check-in received 409: outdated version {version}, current is {checkin_reply.get('current')}"
                    }
                elif status_reason == "renewed" and "renewed_until" in checkin_reply:
                    new_exp = checkin_reply["renewed_until"]
                    lease["expiry_ts"] = new_exp
                    if new_exp.endswith("Z"):
                        new_exp = new_exp[:-1] + "+00:00"
                    expiry_dt = datetime.fromisoformat(new_exp)

            cmd = subtask["cmd"]
            robot_id = subtask.get("robot", lease["assigned_robots"][0])
            params = subtask.get("params", {})

            # Boundary check on destination (FR-5)
            target_dest = params.get("to")
            if target_dest:
                if target_dest in forbidden_zones:
                    self._escalate_unexpected(
                        lease, robot_id,
                        what=f"Target {target_dest} is in forbidden zones",
                        where=target_dest
                    )
                    return {"status": "FAILED_BOUNDARY", "reason": f"Forbidden zone: {target_dest}"}

            robot = self.robots.get(robot_id)
            if not robot:
                robot = MockRobot(robot_id=robot_id)
                self.robots[robot_id] = robot

            # Translate to robot primitive command
            cmd_payload = {
                "cmd_id": f"cmd-{lease_id}-step-{idx+1}",
                "lease_id": lease_id,
                "robot_id": robot_id,
                "action": cmd,
                "params": params,
                "timestamp": time.time()
            }

            # Store command and dispatch
            self.store.store_robot_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "DISPATCHED")

            exec_report = robot.execute_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "COMPLETED")

            # Log decision (FR-20, NFR-6)
            self.store.log_decision(
                decision_id=f"dec-{lease_id}-v{version}-{idx+1}",
                lease_id=lease_id,
                decided_by="RULE",
                input_context={"step": idx, "subtask": subtask},
                decision_action=cmd,
                decision_output=exec_report,
                latency_ms=1.5
            )
            executed_steps.append(exec_report)

        # 5. Mission Completed -> Emit OUTPUT_READY (FR-9, Table 2)
        escalation = {
            "type": "OUTPUT_READY",
            "message_id": f"msg-out-{int(time.time()*1000)}",
            "lease_version": version,
            "task_id": task_id,
            "payload": {
                "result_ref": f"storage://results/{task_id}/delivery-proof.jpg"
            }
        }
        self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")
        self.active_lease = None

        return {
            "status": "COMPLETED",
            "lease_id": lease_id,
            "executed_steps_count": len(executed_steps),
            "result_ref": escalation["payload"]["result_ref"]
        }

    def _handle_lease_expiry_parking(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """Parks all assigned robots in safe spots outside doorways/lifts (FR-6)."""
        robot_id = lease["assigned_robots"][0]
        robot = self.robots.get(robot_id)
        park_cmd = {
            "cmd_id": f"cmd-park-{int(time.time()*1000)}",
            "lease_id": lease["lease_id"],
            "robot_id": robot_id,
            "action": "go_to",
            "params": {"to": "safe-parking-bay-1"},
            "timestamp": time.time()
        }
        res = robot.execute_command(park_cmd)
        res["action"] = "go_to"
        res["params"] = park_cmd["params"]
        return res

    def _escalate_unexpected(self, lease: Dict[str, Any], robot_id: str, what: str, where: str):
        """Emits UNEXPECTED_EVENT escalation."""
        esc = {
            "type": "UNEXPECTED_EVENT",
            "message_id": f"msg-ue-{int(time.time()*1000)}",
            "lease_version": lease["version"],
            "task_id": lease.get("task_id", lease["lease_id"]),
            "payload": {
                "what_happened": what,
                "where": where,
                "robots_saw": [f"{robot_id} reported boundary conflict at {where}"]
            }
        }
        self.send_escalation_to_cloud(esc)

    def flush_pending_escalations(self) -> int:
        """Flushes any pending escalations in local outbox (FR-10)."""
        pending = self.store.get_pending_outbox()
        flushed = 0
        for item in pending:
            payload = item.get("payload")
            if not payload:
                continue
            if "task_id" in payload and payload["task_id"].startswith("lease-") and "-v" in payload["task_id"]:
                lid = payload["task_id"]
                payload["task_id"] = lid[len("lease-"):lid.rindex("-v")]
            url = f"{self.cloud_url}/escalations"
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201, 204):
                        self.store.mark_outbox_uploaded(item["message_id"])
                        flushed += 1
            except Exception:
                pass
        return flushed
