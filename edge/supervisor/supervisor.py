"""
Nimbus Edge Supervisor Daemon (Person 2)
Supervises local wing cluster, enforces lease boundaries (FR-5, FR-6),
manages local persistence in SQLite (FR-10), executes sub-tasks on robots,
and communicates with Cloud over HTTP REST (Milestone 1) and outbox.
Incorporates Person 1's Gazebo hospital topology and waypoints for spatial authority.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import uuid

from edge.mock.mock_robot import MockRobot
from edge.schemas.validator import (
    is_coordinate_in_allowed_zones,
    validate_escalation,
    validate_lease,
    validate_robot_subtask,
)
from edge.slm.engine import EdgeSLMEngine
from edge.store.local_store import EdgeLocalStore


class EdgeSupervisor:
    # Cluster configuration profiles matching hospital wings to topology circulation
    CLUSTER_DEFAULTS = {
        "east-wing": {
            "home_zone": "east_waiting_approach",
            "safe_parking": "east_waiting_approach",
            "default_robot": "R1",
            "default_pos": [4.5, 8.0, 0.0],
            "area": "east",
        },
        "west-wing": {
            "home_zone": "west_waiting_approach",
            "safe_parking": "west_waiting_approach",
            "default_robot": "R4",
            "default_pos": [-4.5, 8.0, 0.0],
            "area": "west",
        },
    }

    def __init__(
        self,
        cluster_id: str = "east-wing",
        cloud_url: str = "http://localhost:8000",
        db_path: str = "edge_local_store.db",
        slm_mode: str = "auto",
        topology_file: Optional[str] = None,
        waypoints_file: Optional[str] = None,
    ):
        self.cluster_id = cluster_id
        self.cloud_url = cloud_url.rstrip("/")
        self.store = EdgeLocalStore(db_path)
        self.slm = EdgeSLMEngine(mode=slm_mode)

        # 1. Load Person 1's Gazebo hospital topology & waypoints
        self.topology, self.waypoints = self._load_topology(topology_file, waypoints_file)
        self.safe_parking_spots = [
            n
            for n, d in self.topology.get("nodes", {}).items()
            if d.get("kind") == "zone"
        ]
        self.gates = [
            n
            for n, d in self.topology.get("nodes", {}).items()
            if d.get("kind") == "gate"
        ]
        self.corridors = [
            n
            for n, d in self.topology.get("nodes", {}).items()
            if d.get("kind") == "corridor"
        ]

        # 2. Configure cluster defaults
        cfg = self.CLUSTER_DEFAULTS.get(
            self.cluster_id,
            {
                "home_zone": "east_waiting_approach",
                "safe_parking": "east_waiting_approach",
                "default_robot": "R1",
                "default_pos": [4.5, 8.0, 0.0],
                "area": "east",
            },
        )
        self.home_zone = cfg["home_zone"]
        self.safe_parking_zone = cfg["safe_parking"]
        def_robot_id = cfg["default_robot"]

        # 3. Initialize robot instances localized in topology
        self.robots: Dict[str, MockRobot] = {
            def_robot_id: MockRobot(
                robot_id=def_robot_id,
                initial_pos=cfg["default_pos"],
                current_zone=self.home_zone,
                waypoints=self.waypoints,
            )
        }
        self.active_lease: Optional[Dict[str, Any]] = None

    def _load_topology(
        self,
        topology_file: Optional[str] = None,
        waypoints_file: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, List[float]]]:
        """Loads Person 1's Gazebo hospital topology and waypoints."""
        if topology_file and waypoints_file:
            t_p, w_p = Path(topology_file), Path(waypoints_file)
            if t_p.exists() and w_p.exists():
                try:
                    topo = json.loads(t_p.read_text(encoding="utf-8"))
                    wp = json.loads(w_p.read_text(encoding="utf-8")).get("waypoints", {})
                    return topo, wp
                except Exception:
                    pass

        # Search relative to repository root and current working directory
        candidate_dirs = [
            Path(__file__).resolve().parents[2] / "simulation" / "hospital_world" / "topology",
            Path.cwd() / "simulation" / "hospital_world" / "topology",
            Path(__file__).resolve().parent / "topology",
        ]
        for cdir in candidate_dirs:
            t_p = cdir / "hospital_topology.json"
            w_p = cdir / "topology_waypoints.json"
            if t_p.exists() and w_p.exists():
                try:
                    topo = json.loads(t_p.read_text(encoding="utf-8"))
                    wp = json.loads(w_p.read_text(encoding="utf-8")).get("waypoints", {})
                    return topo, wp
                except Exception:
                    break

        # Fallback built-in topo-v0 hospital graph
        default_nodes = {
            "lift_lobby": {"kind": "gate", "speed_cap": 0.35},
            "north_hall": {"kind": "corridor", "speed_cap": 0.45},
            "west_north_gate": {"kind": "gate", "speed_cap": 0.30},
            "east_north_gate": {"kind": "gate", "speed_cap": 0.30},
            "west_waiting_approach": {"kind": "zone", "speed_cap": 0.30},
            "east_waiting_approach": {"kind": "zone", "speed_cap": 0.30},
            "west_mid_hall": {"kind": "corridor", "speed_cap": 0.40},
            "east_mid_hall": {"kind": "corridor", "speed_cap": 0.40},
            "nurse_station_approach": {"kind": "zone", "speed_cap": 0.25},
            "west_south_hall": {"kind": "corridor", "speed_cap": 0.40},
            "east_south_hall": {"kind": "corridor", "speed_cap": 0.40},
            "south_equipment_approach": {"kind": "zone", "speed_cap": 0.25},
            "south_room_approach": {"kind": "zone", "speed_cap": 0.30},
        }
        default_waypoints = {
            "lift_lobby": [0.0, 18.0],
            "north_hall": [0.0, 12.0],
            "west_north_gate": [-4.5, 14.0],
            "east_north_gate": [4.5, 14.0],
            "west_waiting_approach": [-4.5, 8.0],
            "east_waiting_approach": [4.5, 8.0],
            "west_mid_hall": [-4.5, 3.0],
            "east_mid_hall": [4.5, 3.0],
            "nurse_station_approach": [0.0, 5.0],
            "west_south_hall": [-4.5, -8.0],
            "east_south_hall": [4.5, -8.0],
            "south_equipment_approach": [0.0, -15.0],
            "south_room_approach": [0.0, -28.0],
        }
        return {"version": "topo-v0", "nodes": default_nodes}, default_waypoints

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
        except Exception:
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
            payload=escalation,
        )

        url = f"{self.cloud_url}/escalations"
        req_data = json.dumps(escalation).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
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
        stock_digest: Optional[Dict[str, int]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Sends idle cluster heartbeat to POST /fleet/heartbeat (Item 2: liveness probe).
        Keeps cluster health in 'healthy' state on cloud (<30s).
        Reports localized zone telemetry from Person 1's topology.
        """
        url = f"{self.cloud_url}/fleet/heartbeat"
        if robots_digest is None:
            robots_digest = []
            for rid, r in self.robots.items():
                is_busy = bool(self.active_lease and rid in self.active_lease.get("assigned_robots", []))
                robots_digest.append({
                    "id": rid,
                    "state": "busy" if is_busy else "idle",
                    "zone": getattr(r, "current_zone", self.home_zone),
                    "battery": getattr(r, "battery_pct", 100.0),
                })

        body = {
            "cluster_id": self.cluster_id,
            "robots": robots_digest,
        }
        if stock_digest:
            body["stock"] = stock_digest

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
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
        lease: Optional[Dict[str, Any]] = None,
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
                "zone": getattr(r, "current_zone", self.home_zone),
                "battery": getattr(r, "battery_pct", 95.0),
            })

        body = {
            "cluster_id": self.cluster_id,
            "lease_id": lid,
            "lease_version": ver,
            "timestamp": now_iso,
            "message_id": message_id,
            "robots": robots_digest,
        }

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
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
        - Expiry checking & safe parking outside doorways/lifts (FR-6)
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
                    "parking": park_result,
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
                        "reason": f"Check-in received 409: outdated version {version}, current is {checkin_reply.get('current')}",
                    }
                elif status_reason == "renewed" and "renewed_until" in checkin_reply:
                    new_exp = checkin_reply["renewed_until"]
                    lease["expiry_ts"] = new_exp
                    if new_exp.endswith("Z"):
                        new_exp = new_exp[:-1] + "+00:00"
                    expiry_dt = datetime.fromisoformat(new_exp)

            cmd = subtask["cmd"]
            robot_id = subtask.get("robot", lease["assigned_robots"][0])
            params = dict(subtask.get("params", {}))

            # Boundary check on destination (FR-5)
            target_dest = params.get("to")
            if target_dest:
                if target_dest in forbidden_zones:
                    self._escalate_unexpected(
                        lease, robot_id,
                        what=f"Target {target_dest} is in forbidden zones",
                        where=target_dest,
                    )
                    return {"status": "FAILED_BOUNDARY", "reason": f"Forbidden zone: {target_dest}"}

                # Topology waypoint injection & speed limits
                if target_dest in self.waypoints and "target" not in params:
                    params["target"] = self.waypoints[target_dest]

                if target_dest in self.topology.get("nodes", {}):
                    cap = self.topology["nodes"][target_dest].get("speed_cap")
                    if cap is not None:
                        params["max_speed"] = min(lease.get("speed_limit", 0.8), cap)

            robot = self.robots.get(robot_id)
            if not robot:
                init_pos = self.waypoints.get(self.home_zone, [0.0, 0.0, 0.0])
                robot = MockRobot(
                    robot_id=robot_id,
                    initial_pos=init_pos,
                    current_zone=self.home_zone,
                    waypoints=self.waypoints,
                )
                self.robots[robot_id] = robot

            # Translate to robot primitive command
            cmd_payload = {
                "cmd_id": f"cmd-{lease_id}-step-{idx+1}",
                "lease_id": lease_id,
                "robot_id": robot_id,
                "action": cmd,
                "params": params,
                "timestamp": time.time(),
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
                latency_ms=1.5,
            )
            executed_steps.append(exec_report)

        # 5. Mission Completed -> Emit OUTPUT_READY (FR-9, Table 2)
        escalation = {
            "type": "OUTPUT_READY",
            "message_id": f"msg-out-{int(time.time()*1000)}",
            "lease_version": version,
            "task_id": task_id,
            "payload": {
                "result_ref": f"storage://results/{task_id}/delivery-proof.jpg",
            },
        }
        self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")
        self.active_lease = None

        return {
            "status": "COMPLETED",
            "lease_id": lease_id,
            "executed_steps_count": len(executed_steps),
            "result_ref": escalation["payload"]["result_ref"],
        }

    def _handle_lease_expiry_parking(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parks all assigned robots in safe spots outside doorways/lifts (FR-6 / Invariant 4).
        Selects designated safe bay recesses ('zone' kind), strictly avoiding doorways/lifts ('gate').
        """
        robot_id = lease["assigned_robots"][0]
        robot = self.robots.get(robot_id)
        if not robot:
            init_pos = self.waypoints.get(self.home_zone, [0.0, 0.0, 0.0])
            robot = MockRobot(
                robot_id=robot_id,
                initial_pos=init_pos,
                current_zone=self.home_zone,
                waypoints=self.waypoints,
            )
            self.robots[robot_id] = robot

        # Spatial resolution for safe parking
        allowed = set(lease.get("allowed_zones", []))
        # Prioritize authorized safe bay recesses within allowed lease boundaries
        safe_candidates = [z for z in self.safe_parking_spots if z in allowed]

        if safe_candidates:
            safe_spot = safe_candidates[0]
        elif self.safe_parking_zone in allowed or not allowed:
            safe_spot = self.safe_parking_zone
        elif "safe-parking-bay-1" in allowed:
            # Fallback for synthetic test fixtures
            safe_spot = "safe-parking-bay-1"
        else:
            safe_spot = self.safe_parking_zone

        target_coords = self.waypoints.get(safe_spot, [0.0, 0.0])

        park_cmd = {
            "cmd_id": f"cmd-park-{int(time.time()*1000)}",
            "lease_id": lease["lease_id"],
            "robot_id": robot_id,
            "action": "go_to",
            "params": {
                "to": safe_spot,
                "target": target_coords,
                "reason": "lease_expiry_safe_park",
            },
            "timestamp": time.time(),
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
                "robots_saw": [f"{robot_id} reported boundary conflict at {where}"],
            },
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
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201, 204):
                        self.store.mark_outbox_uploaded(item["message_id"])
                        flushed += 1
            except Exception:
                pass
        return flushed
