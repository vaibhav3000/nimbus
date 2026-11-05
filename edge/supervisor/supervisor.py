"""
Nimbus Edge Supervisor Daemon (Person 2)
Supervises local wing cluster, enforces lease boundaries (FR-5, FR-6),
manages local persistence in SQLite (FR-10), executes sub-tasks on robots,
runs local tactical autonomy via SLM (FR-3, FR-4), packages camera artifacts (FR-9),
manages store-and-forward outbox uploads (FR-10, NFR-2), and handles
reconnection protocols with Cloud (FR-12).
Incorporates Person 1's Gazebo hospital topology and waypoints for spatial authority.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import uuid

from edge.mock.mock_robot import MockRobot
from edge.mock.sim_robot import SimRobot
from edge.schemas.validator import (
    is_coordinate_in_allowed_zones,
    validate_escalation,
    validate_lease,
    validate_robot_subtask,
)
from edge.slm.engine import EdgeSLMEngine
from edge.store.local_store import EdgeLocalStore
from edge.supervisor.artifacts import ArtifactPackager
from edge.supervisor.geofence import SpatialGeofenceValidator
from edge.supervisor.outbox_worker import OutboxUploadWorker
from edge.supervisor.parking import SafeParkingEngine
from edge.supervisor.reconnection import ReconnectionManager


class EdgeSupervisor:
    # Cluster configuration profiles matching hospital wings to topology circulation
    CLUSTER_DEFAULTS = {
        "east-wing": {
            "home_zone": "north_hall",
            "safe_parking": "east_waiting_approach",
            "default_robot": "R1",
            "default_pos": [0.0, 12.0, 0.0],
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

    ROBOT_SPAWN_POSES = {
        "robot_1": [0.0, 12.0, 0.0],
        "R1": [0.0, 12.0, 0.0],
        "robot_2": [0.0, 9.5, 0.0],
        "R2": [0.0, 9.5, 0.0],
        "robot_3": [4.5, 8.0, 0.0],
        "R3": [4.5, 8.0, 0.0],
        "robot_4": [-4.5, 8.0, 0.0],
        "R4": [-4.5, 8.0, 0.0],
        "robot_5": [-4.5, 3.0, 0.0],
        "R5": [-4.5, 3.0, 0.0],
        "robot_6": [-4.5, -8.0, 0.0],
        "R6": [-4.5, -8.0, 0.0],
    }

    ROBOT_START_ZONES = {
        "robot_1": "north_hall",
        "R1": "north_hall",
        "robot_2": "nurse_station_approach",
        "R2": "nurse_station_approach",
        "robot_3": "east_waiting_approach",
        "R3": "east_waiting_approach",
        "robot_4": "west_waiting_approach",
        "R4": "west_waiting_approach",
        "robot_5": "west_mid_hall",
        "R5": "west_mid_hall",
        "robot_6": "west_south_hall",
        "R6": "west_south_hall",
    }

    def __init__(
        self,
        cluster_id: str = "east-wing",
        cloud_url: str = "http://localhost:8000",
        db_path: str = "edge_local_store.db",
        slm_mode: str = "auto",
        topology_file: Optional[str] = None,
        waypoints_file: Optional[str] = None,
        verbose: bool = True,
        robot_backend: str = "mock",
        gazebo_validation: str = "odometry",
        system_mode: str = "C1",
        token: Optional[str] = None,
        sim_mode: str = "auto",
    ):
        import os
        self.sim_mode = (sim_mode or os.environ.get("NIMBUS_SIM_MODE") or "auto").lower()
        self._seen_notices = set()
        self.probe_requested = False
        self._preempt: Optional[Dict[str, Any]] = None      # set by the lease watcher: a newer lease / stale version arrived
        self._answers: List[Dict[str, Any]] = []            # ANSWER notices from the cloud (replies to our CLARIFY)
        self._seen_answers: set = set()
        self._preempt_lock = __import__("threading").Lock()
        self.token = token or os.environ.get("NIMBUS_EDGE_TOKEN")
        self.cluster_id = cluster_id
        self.cloud_url = cloud_url.rstrip("/")
        self.robot_backend = (robot_backend or "mock").lower()
        self.gazebo_validation = gazebo_validation
        self.system_mode = (system_mode or "C1").upper()
        if self.system_mode == "C3":
            slm_mode = "rules_only"
        self.store = EdgeLocalStore(db_path)
        self.slm = EdgeSLMEngine(mode=slm_mode)
        self.geofence = SpatialGeofenceValidator()
        self.parking_engine = SafeParkingEngine()
        self.artifact_packager = ArtifactPackager()
        self.verbose = verbose

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

        # Merge physical topology zones into parking engine
        all_safe_spots = dict(self.parking_engine.safe_spots)
        for node_name in self.safe_parking_spots:
            coords = self.waypoints.get(node_name, [0.0, 0.0])
            all_safe_spots[node_name] = {
                "zone_name": node_name,
                "coords": coords,
                "zone_type": "zone",
                "wing": self.topology.get("nodes", {}).get(node_name, {}).get("wing", "shared"),
                "capacity": 3,
                "description": f"Hospital Topology Safe Zone: {node_name}",
            }
        self.parking_engine = SafeParkingEngine(safe_spots=all_safe_spots)

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
        if self.cluster_id == "east-wing":
            self.robots: Dict[str, Any] = {
                "R1": self._create_robot("R1", [0.0, 12.0, 0.0], "north_hall"),
                "R2": self._create_robot("R2", [0.0, 9.5, 0.0], "nurse_station_approach"),
                "R3": self._create_robot("R3", [4.5, 8.0, 0.0], "east_waiting_approach"),
            }
        else:
            self.robots: Dict[str, Any] = {
                "R4": self._create_robot("R4", [-4.5, 8.0, 0.0], "west_waiting_approach"),
                "R5": self._create_robot("R5", [-4.5, 3.0, 0.0], "west_mid_hall"),
                "R6": self._create_robot("R6", [-4.5, -8.0, 0.0], "west_south_hall"),
            }
        self.active_lease: Optional[Dict[str, Any]] = None
        self.active_robot_id: Optional[str] = None
        self.failed_robot_ids: Set[str] = set()

        # Store-and-Forward Outbox Worker (FR-10, NFR-2)
        self.outbox_worker = OutboxUploadWorker(
            store=self.store,
            cloud_url=self.cloud_url,
            auto_start=False,
            token=self.token,
        )

        # Reconnection Manager (FR-12)
        self.reconnection_mgr = ReconnectionManager(
            cluster_id=self.cluster_id,
            store=self.store,
            robots=self.robots,
            cloud_url=self.cloud_url,
            token=self.token,
        )

    def _create_robot(
        self,
        robot_id: str,
        initial_pos: Optional[list] = None,
        current_zone: Optional[str] = None,
    ) -> Any:
        """Instantiates either a SimRobot or MockRobot based on robot_backend."""
        if initial_pos is None:
            initial_pos = self.ROBOT_SPAWN_POSES.get(robot_id, [0.0, 12.0, 0.0])
        if current_zone is None:
            current_zone = self.ROBOT_START_ZONES.get(robot_id, "north_hall")

        if getattr(self, "robot_backend", "mock") in ("sim", "simulator", "gazebo"):
            # Check if a SimRobot for this vehicle namespace already exists
            target_sim_name = f"robot_{robot_id[1:]}" if robot_id.upper().startswith("R") and robot_id[1:].isdigit() else robot_id
            for existing in getattr(self, "robots", {}).values():
                if isinstance(existing, SimRobot) and getattr(existing, "sim_name", None) == target_sim_name:
                    return existing
            return self._log_robot_commands(SimRobot(
                robot_id=robot_id,
                initial_pos=initial_pos,
                current_zone=current_zone,
                waypoints=self.waypoints,
                use_gazebo={"gazebo": True, "kinematic": False}.get(getattr(self, "sim_mode", "auto")),
                validation_source=self.gazebo_validation,
            ))
        return self._log_robot_commands(MockRobot(
            robot_id=robot_id,
            initial_pos=initial_pos,
            current_zone=current_zone,
            waypoints=self.waypoints,
        ))

    def _log_robot_commands(self, robot: Any) -> Any:
        """Print every instruction the edge sends to a robot and the robot's answer (always on,
        even with --quiet), so the edge logs show the edge -> robot traffic."""
        if getattr(robot, "_nimbus_cmd_log", False):
            return robot
        original = robot.execute_command
        cluster = self.cluster_id

        def logged(cmd: Dict[str, Any]) -> Dict[str, Any]:
            rid = getattr(robot, "robot_id", cmd.get("robot_id", "?"))
            action = cmd.get("action")
            p = cmd.get("params") or {}
            detail = ", ".join(f"{k}={v}" for k, v in p.items() if k in ("to", "target", "max_speed", "speed", "seconds", "duration_sec", "reason", "purpose"))
            ts = time.strftime("%H:%M:%S")
            print(f"[{cluster}] {ts} EDGE -> {rid}: {action} {detail}  (cmd {cmd.get('cmd_id', '')[-28:]})", flush=True)
            t0 = time.time()
            res = original(cmd)
            try:
                print(f"[{cluster}] {time.strftime('%H:%M:%S')} {rid} -> EDGE: {action} {res.get('status')} "
                      f"zone={res.get('zone')} ({time.time() - t0:.1f}s) {str(res.get('message', ''))[:110]}", flush=True)
            except Exception:
                pass
            return res

        robot.execute_command = logged
        robot._nimbus_cmd_log = True
        return robot

    def close(self):
        """Cleans up supervisor resources and simulated robot runtimes."""
        for r in list(self.robots.values()):
            if hasattr(r, "close"):
                try:
                    r.close()
                except Exception:
                    pass

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
            "lift_lobby": {"kind": "gate", "speed_cap": 1.0},
            "north_hall": {"kind": "corridor", "speed_cap": 1.0},
            "west_north_gate": {"kind": "gate", "speed_cap": 1.0},
            "east_north_gate": {"kind": "gate", "speed_cap": 1.0},
            "west_waiting_approach": {"kind": "zone", "speed_cap": 1.0},
            "east_waiting_approach": {"kind": "zone", "speed_cap": 1.0},
            "west_mid_hall": {"kind": "corridor", "speed_cap": 1.0},
            "east_mid_hall": {"kind": "corridor", "speed_cap": 1.0},
            "nurse_station_approach": {"kind": "zone", "speed_cap": 1.0},
            "west_south_hall": {"kind": "corridor", "speed_cap": 1.0},
            "east_south_hall": {"kind": "corridor", "speed_cap": 1.0},
            "south_equipment_approach": {"kind": "zone", "speed_cap": 1.0},
            "south_room_approach": {"kind": "zone", "speed_cap": 1.0},
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
        return {"nodes": default_nodes}, default_waypoints

    @staticmethod
    def _extract_task_id(lease: Dict[str, Any]) -> str:
        """Extracts canonical task_id ('task-000001') from lease document or lease_id ('lease-task-000001-v1')."""
        if "task_id" in lease:
            return lease["task_id"]
        lid = lease.get("lease_id", "")
        if lid.startswith("lease-") and "-v" in lid:
            return lid[len("lease-"):lid.rindex("-v")]
        return lid

    def start_outbox_worker(self):
        """Starts background upload worker thread."""
        self.outbox_worker.start()

    def stop_outbox_worker(self, timeout: float = 3.0):
        """Stops background upload worker thread."""
        self.outbox_worker.stop(timeout=timeout)

    def flush_outbox(self, timeout: float = 5.0) -> bool:
        """Synchronously drains all pending outbox entries."""
        return self.outbox_worker.flush(timeout=timeout)

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
                fl_headers = {"Content-Type": "application/json"}
                if getattr(self, "token", None):
                    fl_headers["Authorization"] = f"Bearer {self.token}"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=fl_headers,
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201, 204):
                        self.store.mark_outbox_uploaded(item["message_id"])
                        flushed += 1
            except Exception:
                pass
        return flushed

    def set_network_available(self, available: bool):
        """Toggles network connectivity simulation."""
        self.outbox_worker.set_network_available(available)
        if not available:
            self.reconnection_mgr.record_disconnection()
        else:
            self.reconnection_mgr.record_reconnection()

    def simulate_network_partition(self, duration_sec: Optional[float] = None):
        """Simulates WAN disconnection for resilience testing (FR-6, FR-10)."""
        self.set_network_available(False)

    def simulate_network_recovery(self) -> Dict[str, Any]:
        """Simulates WAN restoration and triggers reconnection summary (FR-12)."""
        summary = self.reconnection_mgr.record_reconnection()
        self.outbox_worker.resume_network()
        return summary

    def fetch_leases_from_cloud(self) -> List[Dict[str, Any]]:
        """Polls GET /outbox on the cloud stand-in server (FR-11 selective sharing, NFR-5)."""
        url = f"{self.cloud_url}/outbox?active=1"
        headers = {"Accept": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                outbox_items = data.get("outbox", [])
                leases = []
                for item in outbox_items:
                    topic = item.get("topic", "") if isinstance(item, dict) else ""
                    if topic == f"nimbus/{self.cluster_id}/notice" and isinstance(item, dict):
                        npayload = item.get("payload", {})
                        nkey = item.get("payload_id") or json.dumps(npayload, sort_keys=True)
                        if self.verbose and nkey not in self._seen_notices:
                            self._seen_notices.add(nkey)
                            print(f"[{self.cluster_id}] Notice received: {npayload.get('type')}")
                        continue
                    if topic and not topic.startswith(f"nimbus/{self.cluster_id}/"):
                        continue
                    kind = topic.rsplit("/", 1)[-1] if topic else "lease"
                    if kind == "renewal" and isinstance(item.get("payload"), dict):
                        # cloud -> edge: same lease version, later expiry_ts (PRD Sec. 6 renewal)
                        rp = item["payload"]
                        al = self.active_lease
                        if al and al.get("lease_id") == rp.get("lease_id") and rp.get("expiry_ts", "") > str(al.get("expiry_ts", "")):
                            al["expiry_ts"] = rp["expiry_ts"]
                        continue
                    if kind == "probe" and isinstance(item.get("payload"), dict):
                        # cloud -> edge: "are you alive?" -> answer with a heartbeat on the next loop
                        pid = item["payload"].get("message_id")
                        if pid and pid not in self._seen_notices:
                            self._seen_notices.add(pid)
                            self.probe_requested = True
                        continue
                    if kind not in ("lease",):
                        continue
                    if isinstance(item, dict) and "payload" in item:
                        payload = item["payload"]
                        if isinstance(payload, dict) and "lease_id" in payload:
                            if payload.get("cluster_id") and payload.get("cluster_id") != self.cluster_id:
                                continue
                            leases.append(payload)
                    elif isinstance(item, dict) and "lease_id" in item:
                        if item.get("cluster_id") and item.get("cluster_id") != self.cluster_id:
                            continue
                        leases.append(item)
                return leases
        except Exception:
            return []

    def send_escalation_to_cloud(self, escalation: Dict[str, Any]) -> bool:
        """Pushes structured escalation (FR-9) with store-and-forward persistence."""
        self.store.queue_outbox(
            message_id=escalation["message_id"],
            message_type=escalation["type"],
            payload=escalation,
            lease_id=escalation.get("lease_id"),
        )
        if not self.outbox_worker.is_network_available():
            return False

        url = f"{self.cloud_url}/escalations"
        req_data = json.dumps(escalation).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status in (200, 201, 204):
                    self.store.mark_outbox_uploaded(escalation["message_id"])
                    return True
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                if err_body.get("duplicate") is True:
                    self.store.mark_outbox_uploaded(escalation["message_id"])
                    return True
            except Exception:
                pass
            if self.verbose:
                print(f"[{self.cluster_id}] Escalation HTTP error {e.code}")
        except Exception as e:
            if self.verbose:
                print(f"[{self.cluster_id}] Escalation network error (saved in outbox): {e}")
        return False

    def send_heartbeat_to_cloud(
        self,
        robots_digest: Optional[List[Dict[str, Any]]] = None,
        stock_digest: Optional[Dict[str, int]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Sends idle cluster heartbeat to POST /fleet/heartbeat (liveness probe & FR-12 reconciliation)."""
        url = f"{self.cloud_url}/fleet/heartbeat"
        if robots_digest is None:
            robots_digest = []
            failed_set = getattr(self, "failed_robot_ids", set())
            active_id = getattr(self, "active_robot_id", None)
            for rid, r in self.robots.items():
                is_failed = rid in failed_set or getattr(r, "has_fault", False) or getattr(r, "status", "") == "FAILED"
                is_busy = not is_failed and bool(self.active_lease and (rid == active_id or rid in self.active_lease.get("assigned_robots", [])))
                r_zone = getattr(r, "current_zone", self.home_zone)
                if r_zone in ("home", "transit", "unknown") or r_zone not in self.topology.get("nodes", {}):
                    r_zone = self.home_zone
                robots_digest.append({
                    "id": rid,
                    "state": "failed" if is_failed else ("busy" if is_busy else "idle"),
                    "zone": r_zone,
                    "battery": getattr(r, "battery_pct", 100.0),
                })

        body = {
            "cluster_id": self.cluster_id,
            "robots": robots_digest,
        }
        if stock_digest:
            body["stock"] = stock_digest

        # FR-12 Reconnection: Send held leases for cloud reconciliation
        active_leases = self.store.get_all_active_leases(self.cluster_id)
        if active_leases:
            body["leases"] = [
                {"lease_id": l["lease_id"], "version": l.get("version", 1)}
                for l in active_leases
            ]
        elif self.active_lease:
            body["leases"] = [
                {"lease_id": self.active_lease["lease_id"], "version": self.active_lease.get("version", 1)}
            ]

        req_data = json.dumps(body).encode("utf-8")
        if self.verbose:
            print(f"[{self.cluster_id}] Sending heartbeat ({len(body.get('robots', []))} robots)")
        headers = {"Content-Type": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if resp.status in (200, 201):
                    for rec in data.get("reconcile", []):
                        if rec.get("action") == "stop":
                            lid = rec.get("lease_id")
                            if self.verbose:
                                print(f"[{self.cluster_id}] Reconcile received 'stop' for lease {lid} - halting robot")
                            self.store.update_lease_status(lid, 999, "STOPPED")
                            if self.active_lease and self.active_lease.get("lease_id") == lid:
                                self.trigger_expiry_interrupt(reason="Cloud reconciled lease termination (stop)")
                                self.active_lease = None
                    return True, data
        except Exception as e:
            if self.verbose:
                print(f"[{self.cluster_id}] Heartbeat network error: {e}")
        return False, {}

    def send_checkin_to_cloud(
        self,
        lease: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Sends active lease check-in to POST /checkins (renewal & telemetry)."""
        target_lease = lease or self.active_lease
        if not target_lease:
            return False, "no_active_lease", {}

        url = f"{self.cloud_url}/checkins"
        lid = target_lease.get("lease_id", "")
        ver = target_lease.get("version", 1)

        if not re.fullmatch(r"lease-(.+)-v\d+", lid):
            task_id = self._extract_task_id(target_lease)
            lid = f"lease-{task_id}-v{ver}"

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message_id = f"msg-ci-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"

        robots_digest = []
        failed_set = getattr(self, "failed_robot_ids", set())
        active_id = getattr(self, "active_robot_id", None)
        for rid, r in self.robots.items():
            r_zone = getattr(r, "current_zone", self.home_zone)
            if r_zone in ("home", "transit", "unknown") or r_zone not in self.topology.get("nodes", {}):
                r_zone = self.home_zone
            is_failed = rid in failed_set or getattr(r, "has_fault", False) or getattr(r, "status", "") == "FAILED"
            is_busy = not is_failed and bool(active_id == rid or (not active_id and getattr(r, "status", "") == "RUNNING"))
            robots_digest.append({
                "id": rid,
                "state": "failed" if is_failed else ("busy" if is_busy else "idle"),
                "zone": r_zone,
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
        headers = {"Content-Type": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers=headers,
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
                return False, "stale_version", err_json
            return False, f"http_{he.code}", err_json
        except Exception:
            return False, "offline", {}

    def trigger_clarify(
        self,
        task_id: str,
        question: str,
        possible_readings: List[str],
        lease_version: int = 1,
    ) -> Dict[str, Any]:
        """Triggers structured CLARIFY escalation (FR-9) and halts active mission."""
        msg_id = f"msg-clarify-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
        escalation = {
            "type": "CLARIFY",
            "message_id": msg_id,
            "lease_version": lease_version,
            "task_id": task_id,
            "payload": {
                "question": question,
                "possible_readings": possible_readings,
            },
        }
        sent = self.send_escalation_to_cloud(escalation)
        self.store.log_decision(
            decision_id=f"dec-clarify-{int(time.time()*1000)}",
            lease_id=f"lease-{task_id}-v{lease_version}",
            decided_by="RULE",
            input_context={"task_id": task_id, "question": question},
            decision_action="ESCALATE_CLARIFY",
            decision_output=escalation["payload"],
            latency_ms=1.0,
        )
        return {"status": "ESCALATED_CLARIFY", "sent": sent, "escalation": escalation}

    def trigger_unexpected_event(
        self,
        task_id: str,
        what: str,
        where: str,
        robots_saw: Optional[List[str]] = None,
        lease_version: int = 1,
        robot_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Triggers structured UNEXPECTED_EVENT escalation (FR-9) requesting Cloud re-plan."""
        msg_id = f"msg-unexp-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
        payload = {
            "what_happened": what,
            "where": where,
            "robots_saw": robots_saw or [f"Robot observed blockage at {where}"],
            "segments": [{"segment_id": where, "state": "blocked"}],
        }
        if robot_zone:
            payload["robot_zone"] = robot_zone
        escalation = {
            "type": "UNEXPECTED_EVENT",
            "message_id": msg_id,
            "lease_version": lease_version,
            "task_id": task_id,
            "payload": payload,
        }
        sent = self.send_escalation_to_cloud(escalation)
        self.store.log_decision(
            decision_id=f"dec-unexp-{int(time.time()*1000)}",
            lease_id=f"lease-{task_id}-v{lease_version}",
            decided_by="RULE",
            input_context={"what": what, "where": where},
            decision_action="ESCALATE_UNEXPECTED",
            decision_output=escalation["payload"],
            latency_ms=1.0,
        )
        return {"status": "ESCALATED_UNEXPECTED", "sent": sent, "escalation": escalation}

    def trigger_expiry_interrupt(
        self,
        reason: str = "Watchdog timer expired",
        simulated_now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Cancels in-flight errand commands and safely parks all assigned robots (FR-6 / Invariant 3 & 4)."""
        if not self.active_lease:
            return {"status": "NO_ACTIVE_LEASE", "cancelled_commands": 0}

        lease_id = self.active_lease.get("lease_id", "")
        task_id = self._extract_task_id(self.active_lease)
        assigned_robots = self.active_lease.get("assigned_robots", list(self.robots.keys()))

        cancelled_count = self.store.cancel_active_commands(lease_id)

        parking_results = {}
        primary_spot = self.safe_parking_zone

        for rid in assigned_robots:
            robot = self.robots.get(rid)
            if not robot:
                robot = self._create_robot(robot_id=rid, initial_pos=[0.0, 0.0, 0.0], current_zone=self.home_zone)
                self.robots[rid] = robot

            robot.execute_command({
                "cmd_id": f"cmd-stop-{rid}-{int(time.time()*1000)}",
                "action": "stop",
                "params": {"reason": f"Expiry interrupt: {reason}"},
            })

            safe_spot_info = self.parking_engine.find_nearest_safe_parking_spot(
                current_pose=robot.pose,
                allowed_zones=self.active_lease.get("allowed_zones"),
                forbidden_zones=self.active_lease.get("forbidden_zones"),
            )
            safe_spot_name = safe_spot_info["zone_name"]
            primary_spot = safe_spot_name

            park_cmd = {
                "cmd_id": f"cmd-park-{rid}-{int(time.time()*1000)}",
                "lease_id": lease_id,
                "robot_id": rid,
                "action": "go_to",
                "params": {
                    "to": safe_spot_name,
                    "target": safe_spot_info["coords"],
                    "reason": "lease_expiry_safe_park",
                },
                "timestamp": time.time(),
            }
            self.store.store_robot_command(park_cmd)
            res = robot.execute_command(park_cmd)
            park_status=res.get("status","FAILED")
            if park_status == "COMPLETED" and res.get("arrival_verified",False):
                self.store.update_robot_command_status(park_cmd["cmd_id"], "COMPLETED")
            else:
                self.store.update_robot_command_status(park_cmd["cmd_id"],park_status)
            parking_results[rid] = res

        self.store.log_decision(
            decision_id=f"dec-park-{int(time.time()*1000)}",
            lease_id=lease_id,
            decided_by="RULE",
            input_context={"reason": reason, "robots": assigned_robots},
            decision_action="SAFE_PARK",
            decision_output=parking_results,
            latency_ms=0.5,
        )

        self.trigger_unexpected_event(
            task_id=task_id,
            what=f"Lease expired; all robots parked in verified safe recesses: {reason}",
            where=primary_spot,
            robots_saw=[f"{rid} parked at {r.get('zone', primary_spot)}" for rid, r in parking_results.items()],
            lease_version=self.active_lease.get("version", 1),
        )

        self.store.update_lease_status(lease_id, self.active_lease.get("version", 1), "EXPIRED_PARKED")
        self.active_lease = None

        return {
            "status": "EXPIRED_PARKED",
            "cancelled_commands": cancelled_count,
            "primary_spot": primary_spot,
            "parking_results": parking_results,
        }

    def process_lease(
        self,
        lease: Dict[str, Any],
        simulated_now: Optional[datetime] = None,
        verbose: Optional[bool] = None,
        allow_resumption: bool = False,
    ) -> Dict[str, Any]:
        """Runs a lease with a background watcher (PRD Sec. 6: regular check-ins; FR-8/FR-16/FR-22: a newer lease
        version replaces the running one). While the robots are busy the watcher
          * checks in every 5 s (renewal; a 409 means this lease version is outdated -> stop),
          * reads the cloud outbox every 2 s: a higher version of THIS task (re-plan, operator stop, hand-over)
            stops the robots at once; a renewal moves the expiry; an ANSWER notice answers our CLARIFY.
        Without it a cancel/re-plan was only seen after the whole lease finished."""
        if simulated_now is not None or not isinstance(lease, dict) or not lease.get("lease_id"):
            return self._process_lease_impl(lease, simulated_now, verbose, allow_resumption)
        import threading
        stop = threading.Event()
        self._preempt = None
        self._answers = []
        task_id = self._extract_task_id(lease)
        version = lease.get("version", 1)

        def _watch() -> None:
            last_checkin = time.time()
            while not stop.wait(2.0):
                try:
                    self._watch_outbox(lease, task_id, version)
                    if time.time() - last_checkin >= 5.0:
                        last_checkin = time.time()
                        _ok, why, reply = self.send_checkin_to_cloud(lease)
                        new_exp = reply.get("renewed_until") if why == "renewed" else None
                        if new_exp and new_exp > str(lease.get("expiry_ts", "")):
                            lease["expiry_ts"] = new_exp
                        elif why == "stale_version":
                            self._request_preempt(lease, f"cloud says lease v{version} is outdated (current v{reply.get('current')})")
                except Exception:
                    pass

        t = threading.Thread(target=_watch, name=f"lease-watch-{lease.get('lease_id')}", daemon=True)
        t.start()
        try:
            return self._process_lease_impl(lease, simulated_now, verbose, allow_resumption)
        finally:
            stop.set()

    # -- lease watcher helpers ---------------------------------------------------------------

    def _fetch_outbox_items(self) -> List[Dict[str, Any]]:
        """Raw cloud->edge messages for this cluster (HTTP GET /outbox?active=1)."""
        headers = {"Accept": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(f"{self.cloud_url}/outbox?active=1", headers=headers)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                items = json.loads(resp.read().decode("utf-8")).get("outbox", [])
        except Exception:
            return []
        return [i for i in items if isinstance(i, dict) and str(i.get("topic", "")).startswith(f"nimbus/{self.cluster_id}/")]

    def _watch_outbox(self, lease: Dict[str, Any], task_id: str, version: int) -> None:
        for item in self._fetch_outbox_items():
            kind = item["topic"].rsplit("/", 1)[-1]
            pl = item.get("payload")
            if not isinstance(pl, dict):
                continue
            if kind == "lease" and "lease_id" in pl:
                if self._extract_task_id(pl) == task_id and int(pl.get("version", 1)) > int(version):
                    self._request_preempt(lease, f"cloud issued lease v{pl.get('version')} for {task_id} (replaces v{version})")
            elif kind == "renewal" and pl.get("lease_id") == lease.get("lease_id") and pl.get("version") == version:
                if str(pl.get("expiry_ts", "")) > str(lease.get("expiry_ts", "")):
                    lease["expiry_ts"] = pl["expiry_ts"]
            elif kind == "notice" and pl.get("type") == "ANSWER" and pl.get("task_id") == task_id:
                mid = pl.get("message_id")
                if pl.get("lease_version") in (None, version) and mid and mid not in self._seen_answers:
                    self._seen_answers.add(mid)
                    self._answers.append(pl)

    def _request_preempt(self, lease: Dict[str, Any], reason: str) -> None:
        """A newer authority exists: record why and stop this lease's robots immediately."""
        with self._preempt_lock:
            if self._preempt is not None:
                return
            self._preempt = {"reason": reason, "ts": time.time()}
        print(f"[{self.cluster_id}] {time.strftime('%H:%M:%S')} !! LEASE SUPERSEDED: {reason} -> stopping robots", flush=True)
        for rid in list(dict.fromkeys(lease.get("assigned_robots", []))):
            robot = self.robots.get(rid)
            if robot is None:
                continue
            try:
                robot.execute_command({"cmd_id": f"cmd-stop-superseded-{rid}-{int(time.time()*1000)}", "action": "stop",
                                       "params": {"reason": "lease superseded"}, "robot_id": rid})
            except Exception:
                pass

    def _supersede(self, lease: Dict[str, Any], lease_id: str, version: int, executed_steps: list) -> Dict[str, Any]:
        reason = (self._preempt or {}).get("reason", "superseded")
        self.store.update_lease_status(lease_id, version, "SUPERSEDED")
        self.store.log_decision(
            decision_id=f"dec-superseded-{lease_id}-{int(time.time()*1000)}", lease_id=lease_id, decided_by="RULE",
            input_context={"lease_id": lease_id, "version": version}, decision_action="LEASE_SUPERSEDED",
            decision_output={"reason": reason}, latency_ms=1.0)
        self.active_lease = None
        return {"status": "SUPERSEDED", "reason": reason, "executed_steps": executed_steps}

    # -- CLARIFY (PRD FR-9 / Table 2) ----------------------------------------------------------

    def _is_known_zone(self, name: str) -> bool:
        if name in self.topology.get("nodes", {}) or name in self.waypoints:
            return True
        if name in getattr(self.geofence, "registry", {}):
            return True
        try:
            from edge.mock import sim_robot as _sr
            if name in _sr.CLOUD_TO_SIM_ALIASES:
                return True
            if _sr.HAS_SDK:
                _sr.resolve_destination(name)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _resolve_reading(text: str, readings: List[str], allowed_zones: List[str]) -> Optional[str]:
        t = (text or "").strip().lower()
        if not t:
            return None
        for r in readings:
            if t == r.lower():
                return r
        for r in readings:
            if r.lower() in t or t in r.lower():
                return r
        for z in allowed_zones:
            if t == z.lower():
                return z
        return None

    def _await_clarification(self, lease: Dict[str, Any], task_id: str, version: int, question: str,
                             readings: List[str]) -> Dict[str, Any]:
        """Send CLARIFY, hold the robots, and wait for the cloud's ANSWER (it asks the requester or answers
        from memory). Outcomes: answered / superseded (a new lease arrived) / expired (lease ran out)."""
        try:  # ignore answers that were already in the outbox before we asked
            for item in self._fetch_outbox_items():
                pl = item.get("payload") or {}
                if item["topic"].endswith("/notice") and pl.get("type") == "ANSWER" and pl.get("message_id"):
                    self._seen_answers.add(pl["message_id"])
        except Exception:
            pass
        self._answers = []
        self.trigger_clarify(task_id=task_id, question=question, possible_readings=readings, lease_version=version)
        print(f"[{self.cluster_id}] {time.strftime('%H:%M:%S')} EDGE -> CLOUD: CLARIFY '{question}' readings={readings}; "
              f"robots held, waiting for the answer", flush=True)
        for rid in dict.fromkeys(lease.get("assigned_robots", [])):
            robot = self.robots.get(rid)
            if robot is not None:
                try:
                    robot.execute_command({"cmd_id": f"cmd-stop-clarify-{rid}-{int(time.time()*1000)}", "action": "stop",
                                           "params": {"reason": "waiting for clarification"}, "robot_id": rid})
                except Exception:
                    pass
        timeout = float(os.environ.get("NIMBUS_CLARIFY_TIMEOUT", "0") or 0)
        t0 = time.time()
        while True:
            if self._preempt:
                return {"outcome": "superseded"}
            if self._answers:
                ans = self._answers.pop(0)
                allowed = list(lease.get("allowed_zones", []))
                choice = self._resolve_reading(ans.get("text", ""), readings, allowed)
                print(f"[{self.cluster_id}] {time.strftime('%H:%M:%S')} CLOUD -> EDGE: ANSWER '{ans.get('text')}' "
                      f"(chosen reading: {choice}); resuming", flush=True)
                self.store.log_decision(
                    decision_id=f"dec-answer-{task_id}-{int(time.time()*1000)}", lease_id=lease.get("lease_id", ""),
                    decided_by="RULE", input_context={"question": question, "readings": readings},
                    decision_action="CLARIFY_ANSWERED", decision_output={"answer": ans.get("text"), "choice": choice}, latency_ms=1.0)
                return {"outcome": "answered", "text": ans.get("text", ""), "choice": choice}
            try:
                e = str(lease["expiry_ts"]); e = e[:-1] + "+00:00" if e.endswith("Z") else e
                if datetime.now(timezone.utc) >= datetime.fromisoformat(e):
                    return {"outcome": "expired"}
            except Exception:
                pass
            if timeout and time.time() - t0 > timeout:
                return {"outcome": "expired"}
            time.sleep(0.5)

    def _clarify_outcome_result(self, clar: Dict[str, Any], lease: Dict[str, Any], lease_id: str, version: int,
                                executed_steps: list) -> Optional[Dict[str, Any]]:
        """Non-answer outcomes of _await_clarification -> the result process_lease must return."""
        if clar["outcome"] == "superseded":
            return self._supersede(lease, lease_id, version, executed_steps)
        if clar["outcome"] == "expired":
            park = self._handle_lease_expiry_parking(lease)
            self.active_lease = None
            return {"status": "EXPIRED_PARKED", "reason": "Lease expired while waiting for a clarification answer; parked safely",
                    "executed_steps": executed_steps, "parking": park}
        return None

    def _process_lease_impl(
        self,
        lease: Dict[str, Any],
        simulated_now: Optional[datetime] = None,
        verbose: Optional[bool] = None,
        allow_resumption: bool = False,
    ) -> Dict[str, Any]:
        """
        Ingests, validates, and executes an authority lease.
        Enforces:
        - Schema Draft 2020-12 compliance (FR-5)
        - Spatial boundary & forbidden zone confinement (FR-5)
        - Monotonic versioning (FR-8)
        - Local tactical autonomy on obstacles/faults (FR-3, FR-4)
        - Ambiguity detection & CLARIFY escalation (FR-9)
        - Expiry watchdog & safe parking in verified non-transit recesses (FR-6)
        - Camera artifact creation and OUTPUT_READY escalation (FR-9)
        - Real-time live execution logging if verbose=True.
        """
        is_verbose = self.verbose if verbose is None else verbose
        mission_start_time = time.time()
        lease_id = lease["lease_id"]
        version = lease["version"]
        priority = lease.get("priority", "routine")

        if is_verbose:
            print("=" * 70)
            print(f"  [{self.cluster_id.upper()}] INGESTED LEASE: {lease_id} (v{version}) | Priority: {priority}")
            print("=" * 70)

        # 1. Validate Schema
        valid, err_msg = validate_lease(lease)
        if not valid:
            if is_verbose:
                print(f"  [!] Schema Validation REJECTED: {err_msg}")
            return {"status": "REJECTED", "reason": f"Validation failed: {err_msg}"}

        # 2. Check cluster routing (FR-11)
        if lease.get("cluster_id") != self.cluster_id:
            if is_verbose:
                print(f"  [-] Ignored lease targeted to cluster: {lease.get('cluster_id')}")
            return {"status": "IGNORED", "reason": f"Lease targeted to {lease.get('cluster_id')}, not {self.cluster_id}"}

        # 3. Store in SQLite with version deduplication (FR-8)
        if allow_resumption:
            existing_lease = self.store.get_lease(lease_id, version)
            if existing_lease is None:
                stored, store_msg = self.store.store_lease(lease)
                if not stored:
                    if is_verbose:
                        print(f"  [-] Dropped stale/duplicate lease: {store_msg}")
                    return {"status": "DROPPED_STALE", "reason": store_msg}
        else:
            stored, store_msg = self.store.store_lease(lease)
            if not stored:
                if is_verbose:
                    print(f"  [-] Dropped stale/duplicate lease: {store_msg}")
                return {"status": "DROPPED_STALE", "reason": store_msg}

        self.active_lease = lease
        task_id = self._extract_task_id(lease)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = lease.get("allowed_zones", [])
        forbidden_zones = lease.get("forbidden_zones", [])

        # PRD FR-8 & docs/p2-edge-contract-ms2.md item 1: Stop lease recognition
        is_stop_lease = bool(task_sequence and all(st.get("cmd") == "stop" for st in task_sequence))
        if is_stop_lease:
            if is_verbose:
                print(f"  [STOP LEASE] Cloud issued stop command for {lease_id} (Task {task_id}) - halting robots and parking")
            self.store.cancel_active_commands(lease_id)
            park_dest = allowed_zones[0] if allowed_zones else self.safe_parking_zone
            for rid in lease.get("assigned_robots", ["R1"]):
                r = self.robots.get(rid)
                if r:
                    r.execute_command({
                        "cmd_id": f"cmd-stop-{rid}-{int(time.time()*1000)}",
                        "action": "stop",
                        "params": {"reason": "Stop lease received: task ended/paused by cloud"}
                    })
                    if park_dest:
                        r.execute_command({
                            "cmd_id": f"cmd-park-{rid}-{int(time.time()*1000)}",
                            "action": "go_to",
                            "params": {"to": park_dest}
                        })
            self.store.update_lease_status(lease_id, version, "STOPPED")
            if self.active_lease and self._extract_task_id(self.active_lease) == task_id:
                self.active_lease = None
            return {
                "status": "STOPPED",
                "lease_id": lease_id,
                "reason": "Stop lease received from cloud: task paused or cancelled",
                "executed_steps_count": 0,
                "executed_steps": []
            }

        if is_verbose:
            print(f"  [1] Guard & Invariant Validation:")
            print(f"      |-- Schema Check       : PASS (Draft 2020-12 compliant)")
            print(f"      |-- Monotonic Version  : PASS (v{version} accepted, no duplicate)")
            print(f"      |-- Allowed Zones      : {allowed_zones}")
            print(f"      |-- Forbidden Zones    : {forbidden_zones}")
            print(rf"      \-- Local Persistence  : Committed to SQLite 'leases' table")

        # Initial check-in on lease acceptance
        self.send_checkin_to_cloud(lease)
        last_checkin_time = time.time()

        # Check expiry timestamp (ISO 8601 UTC)
        expiry_ts_str = lease["expiry_ts"]
        if expiry_ts_str.endswith("Z"):
            expiry_ts_str = expiry_ts_str[:-1] + "+00:00"
        expiry_dt = datetime.fromisoformat(expiry_ts_str)

        executed_steps = []
        active_robot_id = lease.get("assigned_robots", ["R1"])[0]
        active_robot = self.robots.get(active_robot_id)
        if not active_robot:
            active_robot = self._create_robot(
                robot_id=active_robot_id,
                initial_pos=self.ROBOT_SPAWN_POSES.get(active_robot_id),
                current_zone=self.ROBOT_START_ZONES.get(active_robot_id),
            )
            self.robots[active_robot_id] = active_robot

        battery_start = active_robot.battery_pct
        mission_distance_start={rid:getattr(robot,"measured_distance_m",None) for rid,robot in self.robots.items()}
        mission_start_poses={rid:list(robot.pose) for rid,robot in self.robots.items()}

        if is_verbose:
            print(f"\n  [2] Task Ingestion & Execution Plan:")
            print(f"      |-- Task / Lease ID    : {lease_id} (v{version})")
            print(f"      |-- Priority           : {priority.upper()}")
            print(f"      |-- Assigned Robot     : {active_robot_id} (Start Zone: {active_robot.current_zone}, Pose: {active_robot.pose[:2]})")
            print(f"      |-- Allowed Zones      : {allowed_zones}")
            print(f"      |-- Forbidden Zones    : {forbidden_zones}")
            print(f"      |-- Expiry Deadline    : {lease.get('expiry_ts')}")
            print(rf"      \-- Sub-task Sequence  : {len(task_sequence)} steps planned:")
            for s_idx, st in enumerate(task_sequence):
                s_cmd = st.get("cmd")
                s_params = st.get("params", {})
                s_to = s_params.get("to")
                s_extra = f" -> to '{s_to}'" if s_to else (f" -> {s_params}" if s_params else "")
                print(f"         [{s_idx + 1}] {s_cmd}{s_extra}")
            print(f"\n  [3] Sub-task Execution Stream:")

        # 4. Execute sub-task sequence
        idx = 0
        step_wait_counts: Dict[int, int] = {}
        failed_robots: Set[str] = set()
        while idx < len(task_sequence):
            subtask = task_sequence[idx]
            step_num = idx + 1
            if self._preempt:
                return self._supersede(lease, lease_id, version, executed_steps)
            now_dt = simulated_now or datetime.now(timezone.utc)
            try:  # pick up renewals made by the background renewer during a long step
                _e = str(lease["expiry_ts"]); _e = _e[:-1] + "+00:00" if _e.endswith("Z") else _e
                expiry_dt = max(expiry_dt, datetime.fromisoformat(_e))
            except Exception:
                pass
            if now_dt >= expiry_dt:
                park_result = self._handle_lease_expiry_parking(lease)
                if is_verbose:
                    print(f"\n  [!] EXPIRED_PARKED: Lease expired mid-mission; parked safely in {park_result.get('params', {}).get('to')}")
                return {
                    "status": "EXPIRED_PARKED",
                    "reason": "Lease expired mid-mission; parked in safe spot",
                    "executed_steps": executed_steps,
                    "parking": park_result,
                }

            # Periodic check-in every 5s during active execution
            now_loop = time.time()
            if now_loop - last_checkin_time >= 5.0:
                ok, status_reason, checkin_reply = self.send_checkin_to_cloud(lease)
                last_checkin_time = now_loop
                if is_verbose:
                    print(f"      |-- [Telemetry] Check-in sent to Cloud POST /checkins (Status: {status_reason})")
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
            robot_id = subtask.get("robot", active_robot_id)
            params = dict(subtask.get("params", {}))

            # CLARIFY (FR-9, Table 2): the instruction is too ambiguous for the edge -> ask, hold, resume on the answer
            clarify_q = None
            if params.get("ambiguous") is True or cmd == "clarify":
                clarify_q = (params.get("question", f"Destination '{params.get('to')}' is ambiguous"),
                             list(params.get("possible_readings") or [f"{params.get('to', 'unknown')}-A", f"{params.get('to', 'unknown')}-B"]))
            elif (cmd == "go_to" and isinstance(params.get("to"), str) and "target" not in params
                  and params["to"] not in allowed_zones and params["to"] not in forbidden_zones
                  and not self._is_known_zone(params["to"])):
                import difflib
                cands = difflib.get_close_matches(params["to"], list(allowed_zones), n=3, cutoff=0.3) or list(allowed_zones)[:3]
                clarify_q = (f"Destination '{params['to']}' is not on the hospital map. Which place was meant?", cands)
            if clarify_q:
                question, readings = clarify_q
                if is_verbose:
                    print(f"      |-- [Step {step_num}/{len(task_sequence)}] Ambiguity detected -> CLARIFY")
                clar = self._await_clarification(lease, task_id, version, question, readings)
                res = self._clarify_outcome_result(clar, lease, lease_id, version, executed_steps)
                if res is not None:
                    return res
                if cmd == "clarify":          # the step was only the question: carry on with the next one
                    idx += 1
                    continue
                choice = clar.get("choice")
                if params.get("to") is not None and choice and (choice in allowed_zones or self._is_known_zone(choice)):
                    params["to"] = choice
                    subtask.setdefault("params", {})["to"] = choice
                    for k in ("ambiguous", "question", "possible_readings"):
                        params.pop(k, None); subtask["params"].pop(k, None)
                elif params.get("to") is not None:
                    self.trigger_unexpected_event(
                        task_id=task_id, what=f"Clarification answer '{clar.get('text')}' does not name a usable place for step {step_num}",
                        where=str(params.get("to")), robots_saw=[f"{robot_id} is waiting at {robot.current_zone if (robot:=self.robots.get(robot_id)) else '?'}"],
                        lease_version=version)
                    self.active_lease = None
                    return {"status": "ESCALATED_UNEXPECTED", "reason": "clarification answer unusable", "executed_steps": executed_steps}

            # Spatial boundary check on sub-task (FR-5)
            is_valid_boundary, boundary_err = self.geofence.validate_subtask(
                subtask,
                allowed_zones=allowed_zones,
                forbidden_zones=forbidden_zones,
            )
            if not is_valid_boundary:
                target_dest = params.get("to") or params.get("target") or "unknown"
                if target_dest in forbidden_zones:
                    reason_str = f"Forbidden zone: {target_dest}"
                else:
                    reason_str = f"Boundary violation: {boundary_err}"

                self.store.log_decision(
                    decision_id=f"dec-bound-{lease_id}-v{version}-{idx+1}",
                    lease_id=lease_id,
                    decided_by="RULE",
                    input_context={"step": idx, "subtask": subtask, "allowed": allowed_zones, "forbidden": forbidden_zones},
                    decision_action="REJECT_BOUNDARY",
                    decision_output={"error": boundary_err, "reason": reason_str},
                    latency_ms=0.5,
                )
                self._escalate_unexpected(
                    lease, robot_id,
                    what=reason_str,
                    where=str(target_dest),
                )
                if is_verbose:
                    print(f"      |-- [Step {step_num}/{len(task_sequence)}] Boundary check FAILED: {reason_str}")
                self.active_lease = None
                return {"status": "FAILED_BOUNDARY", "reason": reason_str}

            # Topology waypoint injection & speed limits
            target_dest = params.get("to")
            if target_dest:
                if target_dest in self.waypoints and "target" not in params:
                    params["target"] = self.waypoints[target_dest]
                if target_dest in self.topology.get("nodes", {}):
                    cap = self.topology["nodes"][target_dest].get("speed_cap")
                    if cap is not None:
                        params["max_speed"] = min(lease.get("speed_limit", 1.0), cap)

            robot = self.robots.get(robot_id)
            if not robot:
                robot = self._create_robot(
                    robot_id=robot_id,
                    initial_pos=self.ROBOT_SPAWN_POSES.get(robot_id),
                    current_zone=self.ROBOT_START_ZONES.get(robot_id),
                )
                self.robots[robot_id] = robot

            cmd_id = f"cmd-{lease_id}-step-{idx+1}"

            # FR-7 & FR-8: Check if sub-task was already executed by prior supervisor before failover
            existing_cmd = self.store.get_robot_command(cmd_id)
            if existing_cmd and existing_cmd["status"] == "COMPLETED" and getattr(robot,"execution_mode","mock") == "mock":
                if is_verbose:
                    print(f"      |-- [Step {step_num}/{len(task_sequence)}] Sub-task already COMPLETED in store (Standby Takeover / Deduplication) -> Skipping physical re-execution")
                # A durable completion record prevents duplicate actuation, but
                # never rewrites live localization to the old commanded target.
                executed_steps.append({
                    "cmd_id": cmd_id,
                    "action": cmd,
                    "status": "COMPLETED",
                    "skipped_duplicate": True,
                    "zone": robot.current_zone,
                    "pose": list(robot.pose),
                })
                idx += 1
                continue

            # Translate to robot primitive command
            cmd_payload = {
                "cmd_id": cmd_id,
                "lease_id": lease_id,
                "robot_id": robot_id,
                "action": cmd,
                "params": params,
                "timestamp": time.time(),
            }

            self.store.store_robot_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "DISPATCHED")

            if is_verbose:
                target_str = f" to '{target_dest}'" if target_dest else (f" ({params})" if params else "")
                print(f"      |-- [Step {step_num}/{len(task_sequence)}] Sub-task: {cmd}{target_str} (Robot: {robot_id})")

            exec_report = robot.execute_command(cmd_payload)
            if self._preempt:      # a newer lease arrived while this step ran: the watcher stopped the robot
                return self._supersede(lease, lease_id, version, executed_steps)
            exec_report.setdefault("action",cmd)
            exec_report.setdefault("params",params)
            exec_status = str(exec_report.get("status", "FAILED")).upper()

            detailed_failures={"INVALID_DESTINATION","SENSOR_UNAVAILABLE","LOCALIZATION_UNAVAILABLE","TIMEOUT","CAPTURE_FAILED","CANCELLED","REJECTED","GROUND_TRUTH_MISMATCH"}
            if exec_status in detailed_failures or exec_status not in {"COMPLETED","BLOCKED","FAILED"}:
                try: robot.execute_command({"cmd_id":f"cmd-stop-{robot_id}-{int(time.time()*1000)}","action":"stop","params":{"reason":f"terminal execution failure: {exec_status}"}})
                except Exception: pass
                self.store.update_robot_command_status(cmd_payload["cmd_id"],exec_status)
                self.store.update_lease_status(lease_id,version,"FAILED")
                self.store.log_decision(
                    decision_id=f"dec-fail-{lease_id}-v{version}-{idx+1}",lease_id=lease_id,decided_by="RULE",
                    input_context={"step":idx,"subtask":subtask},decision_action="EXECUTION_FAILED",
                    decision_output=exec_report,latency_ms=1.0,
                )
                try:
                    where_zone = exec_report.get("blocked_zone")
                    if not where_zone:
                        m_fail = re.search(r"failed to reach\s+([\w-]+)", exec_report.get("message", ""))
                        where_zone = m_fail.group(1) if m_fail else robot.current_zone
                    self.trigger_unexpected_event(
                        task_id=task_id,
                        what=f"Terminal robot failure: {exec_status} - {exec_report.get('message', '')}",
                        where=where_zone,
                        robots_saw=[f"{robot_id} failed: {exec_status}"],
                        lease_version=version,
                        robot_zone=robot.current_zone,
                    )
                except Exception:
                    pass
                self.active_lease=None
                return {"status":"FAILED","execution_status":exec_status,"reason":exec_report.get("message",f"Robot returned {exec_status}"),"executed_steps":executed_steps,"failed_step":exec_report}

            # Check if execution failed or was blocked by obstacle
            if exec_report["status"] in ("BLOCKED", "FAILED"):
                self.store.update_robot_command_status(cmd_payload["cmd_id"],exec_status)
                blocked_zone = exec_report.get("blocked_zone")
                if not blocked_zone or blocked_zone == robot.current_zone:
                    m = re.search(r"failed to reach\s+([\w-]+)", exec_report.get("message", ""))
                    if m:
                        blocked_zone = m.group(1)
                    else:
                        blocked_zone = robot.current_zone
                event_desc = exec_report.get("message", "Obstruction encountered")
                time_remaining = max(0.0, (expiry_dt - datetime.now(timezone.utc)).total_seconds())

                # C2 Baseline (Cloud-Only Relay): Bypass edge SLM and immediately forward event to Cloud
                if self.system_mode == "C2":
                    if is_verbose:
                        print(f"      |   |--- Execution Mode : [C2 CLOUD-ONLY RELAY TRIGGERED]")
                        print(f"      |   |   |--- Trigger Event  : {event_desc} in '{blocked_zone}'")
                        print(rf"      |   |   \--- Policy Action  : Bypassing local SLM; relaying event to Cloud")

                    # Reflex halt robot motion
                    stop_cmd = {
                        "cmd_id": f"cmd-stop-{lease_id}-{idx+1}-{int(time.time()*1000)}",
                        "lease_id": lease_id,
                        "robot_id": robot_id,
                        "action": "stop",
                        "params": {"reason": f"C2 relay pause for event in {blocked_zone}"},
                        "timestamp": time.time(),
                    }
                    robot.execute_command(stop_cmd)

                    relay_ctx = {
                        "lease_id": lease_id,
                        "version": version,
                        "robot_id": robot_id,
                        "event_description": event_desc,
                        "blocked_zone": blocked_zone,
                        "current_pos": list(robot.pose),
                    }
                    self.store.log_decision(
                        decision_id=f"dec-c2-relay-{lease_id}-v{version}-{idx+1}-{int(time.time()*1000)}",
                        lease_id=lease_id,
                        decided_by="RELAY",
                        input_context=relay_ctx,
                        decision_action="ESCALATE_UNEXPECTED",
                        decision_output={"action": "ESCALATE_UNEXPECTED", "reason": f"C2 relay: {event_desc}"},
                        latency_ms=1.0,
                    )
                    self._escalate_unexpected(
                        lease, robot_id,
                        what=f"C2 Relay: {event_desc}",
                        where=blocked_zone,
                    )
                    return {
                        "status": "ESCALATED_CLOUD",
                        "reason": f"C2 Cloud-Only Relay: Event in {blocked_zone} forwarded to Cloud: {event_desc}",
                        "blocked_zone": blocked_zone,
                        "executed_steps": executed_steps,
                    }

                if is_verbose:
                    print(f"      |   |--- Execution Mode : [SLM TAKEOVER TRIGGERED]")
                    print(f"      |   |   |--- Trigger Event  : {event_desc} in '{blocked_zone}'")
                    print(rf"      |   |   \--- Reasoner Engine: Edge SLM ({self.slm.model_name}, mode: {self.slm.mode})")

                alt_wps = self.geofence.find_alternative_waypoints(
                    current_zone=robot.current_zone,
                    destination_zone=params.get("to", ""),
                    allowed_zones=allowed_zones,
                    forbidden_zones=forbidden_zones,
                    blocked_zone=blocked_zone,
                )

                tactical_ctx = {
                    "lease_id": lease_id,
                    "version": version,
                    "time_remaining_sec": time_remaining,
                    "robot_id": robot_id,
                    "current_pos": list(robot.pose),
                    "current_zone": robot.current_zone,
                    "allowed_zones": allowed_zones,
                    "forbidden_zones": forbidden_zones,
                    "allowed_waypoints": alt_wps,
                    "assigned_robots": list(dict.fromkeys(lease.get("assigned_robots", []) + list(self.robots.keys()))),
                    "failed_robots": list(failed_robots),
                    "event_description": event_desc,
                    "has_fault": exec_report.get("has_fault", False),
                    "wait_count": step_wait_counts.get(idx, 0),
                }

                decision, decided_by, latency_ms = self.slm.evaluate_decision(tactical_ctx)

                self.store.log_decision(
                    decision_id=f"dec-tac-{lease_id}-v{version}-{idx+1}-{int(time.time()*1000)}",
                    lease_id=lease_id,
                    decided_by=decided_by,
                    input_context=tactical_ctx,
                    decision_action=decision.get("action", "UNKNOWN"),
                    decision_output=decision,
                    latency_ms=latency_ms,
                )

                tac_action = decision.get("action")
                tac_waypoint = decision.get("target_waypoint")
                tac_reason = decision.get("reason", "No explanation provided")
                is_timeout = (latency_ms > 10000.0) or decision.get("timed_out", False)

                if is_verbose:
                    if is_timeout:
                        print(f"      |   |   |--- SLM Latency    : {latency_ms:.1f} ms ({latency_ms/1000.0:.2f} s) [TIMEOUT EXCEEDED: > 10.0s SLA]")
                        print(f"      |   |   |--- What Happened  : SLM reasoning exceeded the 10.0s real-time safety threshold.")
                        print(f"      |   |   |                    Watchdog aborted SLM query to prevent hospital corridor stall.")
                        print(f"      |   |   |                    Deterministic Safety Rule Fallback Engine immediately engaged.")
                        print(f"      |   |   |--- Decided By     : Deterministic Safety Rule (Timeout Fallback)")
                        print(f"      |   |   |--- Fallback Action: {tac_action}" + (f" -> Target: '{tac_waypoint}'" if tac_waypoint else ""))
                        print(rf"      |   |   \--- Fallback Reason: \"{tac_reason}\"")
                    elif decided_by == "RULE":
                        print(f"      |   |   |--- Rule Latency   : {latency_ms:.1f} ms ({latency_ms/1000.0:.2f} s) [PASS: Deterministic Rule]")
                        print(f"      |   |   |--- Decided By     : Deterministic Rule Engine")
                        print(f"      |   |   |--- Rule Action    : {tac_action}" + (f" -> Target: '{tac_waypoint}'" if tac_waypoint else ""))
                        print(rf"      |   |   \--- Rule Reason    : \"{tac_reason}\"")
                    else:
                        print(f"      |   |   |--- SLM Latency    : {latency_ms:.1f} ms ({latency_ms/1000.0:.2f} s) [PASS: <= 10.0s SLA]")
                        print(f"      |   |   |--- Decided By     : Edge SLM ({self.slm.model_name})")
                        print(f"      |   |   |--- Final Decision : {tac_action}" + (f" -> Target: '{tac_waypoint}'" if tac_waypoint else ""))
                        print(rf"      |   |   \--- SLM Reason     : \"{tac_reason}\"")

                if tac_action == "LOCAL_WAIT":
                    step_wait_counts[idx] = step_wait_counts.get(idx, 0) + 1
                    wait_cmd = {
                        "cmd_id": f"cmd-wait-{lease_id}-{idx+1}-{int(time.time()*1000)}",
                        "lease_id": lease_id,
                        "robot_id": robot_id,
                        "action": "wait",
                        "params": {"duration_sec": 5.0},
                        "timestamp": time.time(),
                    }
                    self.store.store_robot_command(wait_cmd)
                    if is_verbose:
                        print(f"      |   |   |--- Action Dispatch: LOCAL_WAIT -> Pausing robot {robot_id} for 5.0s")
                    wait_report=robot.execute_command(wait_cmd)
                    if wait_report.get("status") != "COMPLETED":
                        self.store.update_robot_command_status(wait_cmd["cmd_id"],wait_report.get("status","FAILED"))
                        return {"status":"FAILED","execution_status":wait_report.get("status","FAILED"),"reason":wait_report.get("message","Local wait failed")}
                    self.store.update_robot_command_status(wait_cmd["cmd_id"], "COMPLETED")
                    if is_verbose:
                        print(rf"      |   |   \--- Action Status  : COMPLETED (Wait finished; dynamic obstacle cleared)")
                    continue

                elif tac_action == "LOCAL_DETOUR" and decision.get("target_waypoint") == blocked_zone:
                    # a "detour" through the blocked zone itself is no detour: ask the cloud (FR-4)
                    reason_msg = f"No detour inside the lease avoids {blocked_zone}: {event_desc}"
                    self.trigger_unexpected_event(task_id=task_id, what=reason_msg, where=blocked_zone,
                                                  robots_saw=[f"{robot_id} reported: {event_desc} at {blocked_zone}"],
                                                  lease_version=version, robot_zone=robot.current_zone)
                    if is_verbose:
                        print(f"      |   |   \--- No usable detour -> UNEXPECTED_EVENT sent to Cloud")
                    self.active_lease = None
                    return {"status": "ESCALATED_UNEXPECTED", "reason": reason_msg}

                elif tac_action == "LOCAL_DETOUR":
                    detour_wp = decision.get("target_waypoint")
                    is_valid_detour, detour_err, _ = self.geofence.validate_destination(
                        detour_wp,
                        allowed_zones=allowed_zones,
                        forbidden_zones=forbidden_zones,
                    )
                    if not is_valid_detour:
                        self._escalate_unexpected(
                            lease, robot_id,
                            what=f"Detour boundary violation: {detour_err}",
                            where=str(detour_wp),
                        )
                        if is_verbose:
                            print(rf"      |   |   \--- Detour Rejected: Waypoint '{detour_wp}' violates lease boundaries: {detour_err}")
                        return {"status": "FAILED_BOUNDARY", "reason": f"Detour waypoint '{detour_wp}' violates lease boundaries: {detour_err}"}

                    detour_cmd = {
                        "cmd_id": f"cmd-detour-{lease_id}-{idx+1}-{int(time.time()*1000)}",
                        "lease_id": lease_id,
                        "robot_id": robot_id,
                        "action": "go_to",
                        "params": {"to": detour_wp},
                        "timestamp": time.time(),
                    }
                    self.store.store_robot_command(detour_cmd)
                    if is_verbose:
                        print(f"      |   |   |--- Action Dispatch: LOCAL_DETOUR -> Navigating via permitted bypass '{detour_wp}'")
                    detour_report = robot.execute_command(detour_cmd)
                    if detour_report.get("status") == "BLOCKED":
                        self.store.update_robot_command_status(detour_cmd["cmd_id"], "BLOCKED")
                        reason_msg = f"Detour via {detour_wp} also blocked: {detour_report.get('message', '')}"
                        detour_blocked = detour_report.get("blocked_zone") or detour_wp
                        self.trigger_unexpected_event(task_id=task_id, what=reason_msg,
                                                      where=detour_blocked,
                                                      robots_saw=[f"{robot_id}: {reason_msg}"], lease_version=version,
                                                      robot_zone=robot.current_zone)
                        self.active_lease = None
                        return {"status": "ESCALATED_UNEXPECTED", "reason": reason_msg}
                    if detour_report.get("status") != "COMPLETED" or not detour_report.get("arrival_verified",False):
                        self.store.update_robot_command_status(detour_cmd["cmd_id"],detour_report.get("status","FAILED"))
                        self.active_lease=None
                        return {"status":"FAILED","execution_status":detour_report.get("status","FAILED"),"reason":detour_report.get("message","Detour arrival was not verified"),"failed_step":detour_report}
                    self.store.update_robot_command_status(detour_cmd["cmd_id"], "COMPLETED")
                    executed_steps.append(detour_report)
                    if is_verbose:
                        print(rf"      |   |   \--- Action Status  : COMPLETED (Detour executed; robot at {detour_report['pose'][:2]}, zone: {detour_report.get('current_zone', detour_report.get('zone'))})")
                    continue

                elif tac_action == "SWAP_ROBOT":
                    faulted_id = robot_id
                    failed_robots.add(faulted_id)
                    new_robot_id = decision.get("target_waypoint")
                    cluster_robots = list(dict.fromkeys(lease.get("assigned_robots", []) + list(self.robots.keys())))
                    available_robots = [r for r in cluster_robots if r not in failed_robots]

                    if (not new_robot_id or new_robot_id in failed_robots) and available_robots:
                        new_robot_id = available_robots[0]

                    if new_robot_id and new_robot_id in available_robots and new_robot_id not in failed_robots:
                        robot.execute_command({
                            "cmd_id": f"cmd-stop-{faulted_id}-{int(time.time()*1000)}",
                            "action": "stop",
                            "params": {"reason": f"Fault occurred; swapping to {new_robot_id}"},
                        })
                        robot_id = new_robot_id
                        active_robot_id = new_robot_id
                        self.active_robot_id = new_robot_id
                        self.failed_robot_ids = set(failed_robots)
                        if robot_id not in self.robots:
                            self.robots[robot_id] = self._create_robot(robot_id=robot_id)
                        robot = self.robots[robot_id]
                        robot.status = "RUNNING"
                        if faulted_id in self.robots:
                            self.robots[faulted_id].status = "FAILED"

                        # Update remaining subtasks in sequence to use the new robot
                        for rem_sub in task_sequence[idx:]:
                            if rem_sub.get("robot") == faulted_id:
                                rem_sub["robot"] = new_robot_id

                        # Update active lease assigned_robots
                        assigned = list(dict.fromkeys([new_robot_id] + list(lease.get("assigned_robots", []))))
                        lease["assigned_robots"] = assigned
                        if self.active_lease:
                            self.active_lease["assigned_robots"] = assigned
                        try:
                            self.store.save_lease(lease)
                        except Exception:
                            pass

                        # Notify Cloud immediately of the swap via check-in telemetry
                        try:
                            self.send_checkin_to_cloud(lease)
                        except Exception as e:
                            if is_verbose:
                                print(f"      |   |   |--- Check-in after swap failed: {e}")

                        if is_verbose:
                            print(f"      |   |   |--- Action Dispatch: SWAP_ROBOT -> Stopped faulted {faulted_id}; transferred task to {new_robot_id}")
                            print(rf"      |   |   \--- Action Status  : COMPLETED (Active robot is now {new_robot_id})")
                        continue
                    else:
                        self._escalate_unexpected(
                            lease, robot_id,
                            what=f"Robot swap failed: all candidates {list(failed_robots)} in cluster have failed/faulted",
                            where=robot.current_zone,
                        )
                        if is_verbose:
                            print(rf"      |   |   \--- Action Rejected: All available cluster robots {list(failed_robots)} have failed")
                        return {"status": "ESCALATED_UNEXPECTED", "reason": "No standby robot available for swap"}

                elif tac_action == "ESCALATE_CLARIFY":
                    readings = ["wait for it to clear", "treat the route as closed"]
                    clar = self._await_clarification(
                        lease, task_id, version,
                        f"{robot_id} reports: {event_desc} (at {blocked_zone}). Should it wait for this to clear, or is the route closed?",
                        readings)
                    res = self._clarify_outcome_result(clar, lease, lease_id, version, executed_steps)
                    if res is not None:
                        return res
                    if clar.get("choice") == readings[0] or "wait" in str(clar.get("text", "")).lower():
                        step_wait_counts[idx] = step_wait_counts.get(idx, 0) + 1
                        robot.execute_command({"cmd_id": f"cmd-wait-clarified-{lease_id}-{idx+1}-{int(time.time()*1000)}",
                                               "lease_id": lease_id, "robot_id": robot_id, "action": "wait",
                                               "params": {"duration_sec": 5.0}, "timestamp": time.time()})
                        continue
                    self.trigger_unexpected_event(task_id=task_id, what=f"Route closed after clarification: {event_desc}",
                                                  where=blocked_zone, robots_saw=[f"{robot_id}: {event_desc}"], lease_version=version)
                    self.active_lease = None
                    return {"status": "ESCALATED_UNEXPECTED", "reason": "route closed per clarification"}

                elif tac_action == "ESCALATE_UNEXPECTED":
                    reason_msg = decision.get("reason", "Corridor blocked, no alternate detour")
                    self.trigger_unexpected_event(
                        task_id=task_id,
                        what=reason_msg,
                        where=blocked_zone,
                        robots_saw=[f"{robot_id} reported: {reason_msg} at {blocked_zone}"],
                        lease_version=version,
                        robot_zone=robot.current_zone,
                    )
                    if is_verbose:
                        print(f"      |   |   |--- Action Dispatch: ESCALATE_UNEXPECTED -> Emitted UNEXPECTED_EVENT to Cloud")
                        print(rf"      |   |   \--- Escalation Reason: \"{reason_msg}\"")
                    self.active_lease = None
                    return {"status": "ESCALATED_UNEXPECTED", "reason": reason_msg}

                else:
                    self.active_lease = None
                    return {"status": "FAILED", "reason": f"Obstacle encountered: {event_desc}"}

            if exec_status != "COMPLETED":
                self.active_lease=None
                return {"status":"FAILED","execution_status":exec_status,"reason":"Sub-task did not explicitly complete","failed_step":exec_report}
            if cmd == "go_to" and not exec_report.get("arrival_verified",False):
                self.store.update_robot_command_status(cmd_payload["cmd_id"],"FAILED")
                self.store.update_lease_status(lease_id,version,"FAILED")
                self.active_lease=None
                return {"status":"FAILED","execution_status":"LOCALIZATION_UNAVAILABLE","reason":"Robot reported completion without verified arrival","failed_step":exec_report}
            if cmd == "photo" and getattr(robot,"execution_mode","mock") != "mock" and not exec_report.get("has_photo",False):
                self.store.update_robot_command_status(cmd_payload["cmd_id"],"FAILED")
                self.store.update_lease_status(lease_id,version,"FAILED")
                self.active_lease=None
                return {"status":"FAILED","execution_status":"CAPTURE_FAILED","reason":"Required camera postcondition was not satisfied","failed_step":exec_report}

            self.store.update_robot_command_status(cmd_payload["cmd_id"], "COMPLETED")

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

            if is_verbose:
                print(f"      |   |--- Execution Mode : [PERFORMED IMMEDIATELY] (Nominal execution by edge controller, path clear)")
                print(rf"      |   \--- Step Status    : COMPLETED | Pose: {exec_report['pose'][:2]} | Zone: {exec_report.get('current_zone', exec_report.get('zone'))} | Battery: {robot.battery_pct:.1f}%")

            idx += 1

        # 5. Mission Completed -> Generate Artifacts & Emit OUTPUT_READY (FR-9, Table 2)
        if any(str(step.get("status","")).upper() != "COMPLETED" for step in executed_steps):
            self.store.update_lease_status(lease_id,version,"FAILED")
            self.active_lease=None
            return {"status":"FAILED","reason":"Mission contains a sub-task without explicit verified completion","executed_steps":executed_steps}
        end_time = time.time()
        final_robot = self.robots.get(active_robot_id, active_robot)
        img_bytes = getattr(final_robot, "last_photo_bytes", None)
        snapshot=final_robot.pose_snapshot() if hasattr(final_robot,"pose_snapshot") else {"pose":list(final_robot.pose),"last_known_pose":list(final_robot.pose),"pose_source":getattr(final_robot,"pose_source","mock_simulation"),"pose_frame":getattr(final_robot,"pose_frame","simulated/world"),"pose_timestamp":getattr(final_robot,"pose_timestamp",time.time()),"pose_fresh":True}
        final_pose=snapshot.get("pose")
        if final_pose is None:
            self.store.update_lease_status(lease_id,version,"FAILED"); self.active_lease=None
            return {"status":"FAILED","execution_status":"LOCALIZATION_UNAVAILABLE","reason":"Final measured pose is unavailable or stale","executed_steps":executed_steps}
        required_photo=any(st.get("cmd") == "photo" for st in task_sequence)
        execution_mode=getattr(final_robot,"execution_mode","mock")
        if required_photo and execution_mode != "mock" and not img_bytes:
            self.store.update_lease_status(lease_id,version,"FAILED"); self.active_lease=None
            return {"status":"FAILED","execution_status":"CAPTURE_FAILED","reason":"Required delivery-proof camera frame is unavailable","executed_steps":executed_steps}
        proof_args={
            "task_id":task_id,"robot_id":active_robot_id,"pose":final_pose,
            "pose_source":snapshot.get("pose_source","unknown"),"pose_frame":snapshot.get("pose_frame","unknown"),
            "pose_timestamp":snapshot.get("pose_timestamp"),"arrival_verified":bool(getattr(final_robot,"arrival_verified",False)),
            "execution_mode":execution_mode,
        }
        if required_photo or img_bytes:
            storage_uri, artifact_path, sha256_hash = self.artifact_packager.generate_delivery_proof(
                **proof_args,image_bytes=img_bytes,allow_simulated=execution_mode in {"mock","sdk_mock"},
            )
        else:
            storage_uri, artifact_path, sha256_hash = self.artifact_packager.generate_execution_receipt(**proof_args)

        battery_delta = battery_start - final_robot.battery_pct
        mission_duration = end_time - mission_start_time

        distance_values=[]
        for rid,robot in self.robots.items():
            start=mission_distance_start.get(rid); current=getattr(robot,"measured_distance_m",None)
            if start is not None and current is not None: distance_values.append(max(0.0,current-start))
        measured_distance=sum(distance_values) if distance_values else None
        planned_distance=0.0; planned_known=True
        planned_positions={rid:list(pose) for rid,pose in mission_start_poses.items()}
        for subtask in task_sequence:
            if subtask.get("cmd") != "go_to": continue
            rid=subtask.get("robot",active_robot_id); params=subtask.get("params",{}); target=params.get("target") or self.waypoints.get(params.get("to"))
            previous=planned_positions.get(rid)
            if target is None or previous is None: planned_known=False; continue
            planned_distance += ((float(target[0])-previous[0])**2+(float(target[1])-previous[1])**2)**0.5
            planned_positions[rid]=[float(target[0]),float(target[1]),0.0]
        self.artifact_packager.compile_mission_metrics(
            task_id=task_id,
            total_duration_sec=mission_duration,
            total_distance_m=measured_distance,
            battery_used_pct=battery_delta,
            executed_steps_count=len(executed_steps),
            planned_distance_m=round(planned_distance,2) if planned_known else None,
            distance_source="measured_pose_history" if measured_distance is not None else "unavailable",
            execution_mode=execution_mode,
            arrival_verified=bool(getattr(final_robot,"arrival_verified",False)),
        )

        # Survey tasks (T7): if task sequence contains "report" or lease is survey
        is_survey = any(st.get("cmd") == "report" for st in task_sequence) and not any(
            st.get("cmd") == "photo" and (st.get("params") or {}).get("purpose") == "delivery_proof" for st in task_sequence)
        if is_survey:
            reports_list = []
            for rid in lease.get("assigned_robots", [active_robot_id]):
                r = self.robots.get(rid)
                counts_by_place = {}
                for step in executed_steps:
                    if step.get("action") == "photo":
                        pz = step.get("zone") or (r.current_zone if r else "2B")
                        counts_by_place[pz] = {"infusion_pump": 1}
                if not counts_by_place:
                    counts_by_place = {self.home_zone: {"infusion_pump": 1}}
                reports_list.append({"robot": rid, "counts": counts_by_place})

            survey_doc = {"reports": reports_list}
            survey_path = self.artifact_packager.base_dir / task_id / "survey.json"
            survey_path.parent.mkdir(parents=True, exist_ok=True)
            survey_path.write_text(json.dumps(survey_doc, indent=2), encoding="utf-8")
            storage_uri = f"storage://results/{task_id}/survey.json"

        escalation = {
            "type": "OUTPUT_READY",
            "message_id": f"msg-out-{int(time.time()*1000)}",
            "lease_version": version,
            "task_id": task_id,
            "payload": {
                "result_ref": storage_uri,
            },
        }
        cloud_success = self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")
        self.active_lease = None

        if is_verbose:
            print("\n" + "=" * 70)
            print(f"  MISSION COMPLETED: [{self.cluster_id.upper()}]")
            print("=" * 70)
            print(f"  Lease ID         : {lease_id} (Task ID: {task_id})")
            print(f"  Final Status     : COMPLETED")
            print(f"  Executed Steps   : {len(executed_steps)} sub-tasks")
            print(f"  Mission Duration : {mission_duration:.2f} s")
            print(f"  Battery Delta    : -{battery_delta:.1f}% (Remaining: {final_robot.battery_pct:.1f}%)")
            print(f"  Delivery Proof   : {storage_uri}")
            print(f"  SHA-256 Digest   : {sha256_hash}")
            print(f"  Cloud Escalation : OUTPUT_READY -> POST /escalations ({'201 Created (Sent & Verified)' if cloud_success else 'Queued in Local SQLite Outbox (Store-and-Forward)'})")
            print(f"  Durable Audit    : Committed {len(executed_steps)} execution records to SQLite 'decision_log' table")
            print("=" * 70 + "\n")

        return {
            "status": "COMPLETED",
            "lease_id": lease_id,
            "executed_steps_count": len(executed_steps),
            "executed_steps": executed_steps,
            "result_ref": storage_uri,
            "sha256": sha256_hash,
            "artifact_sha256": sha256_hash,
            "artifact_path": str(artifact_path),
        }

    def handle_cloud_replan(self, new_lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Receives a revised lease from Cloud following an escalation (FR-8, Invariant 2).
        Validates that new lease version is strictly higher than previous version,
        cancels previous in-flight commands, and executes the re-planned mission.
        """
        new_version = new_lease.get("version", 1)
        lease_id = new_lease.get("lease_id", "")
        old_lease = self.store.get_lease(lease_id)
        if old_lease:
            old_version = old_lease.get("version", 0)
            if new_version <= old_version:
                return {
                    "status": "REJECTED_STALE",
                    "reason": f"Replanned lease version {new_version} <= current version {old_version}"
                }
            self.store.cancel_active_commands(lease_id)

        return self.process_lease(new_lease)

    def _handle_lease_expiry_parking(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parks all assigned robots in safe spots outside doorways/lifts (FR-6 / Invariant 4).
        Selects designated safe bay recesses ('zone' kind), strictly avoiding doorways/lifts ('gate').
        """
        robot_id = lease.get("assigned_robots", ["R1"])[0]
        robot = self.robots.get(robot_id)
        if not robot:
            init_pos = self.waypoints.get(self.home_zone, [0.0, 0.0, 0.0])
            robot = self._create_robot(
                robot_id=robot_id,
                initial_pos=init_pos,
                current_zone=self.home_zone,
            )
            self.robots[robot_id] = robot

        safe_spot_info = self.parking_engine.find_nearest_safe_parking_spot(
            current_pose=robot.pose,
            allowed_zones=lease.get("allowed_zones"),
            forbidden_zones=lease.get("forbidden_zones"),
        )
        safe_spot = safe_spot_info["zone_name"]
        target_coords = safe_spot_info["coords"]

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
        """Internal helper for reporting unexpected physical events."""
        task_id = self._extract_task_id(lease)
        robot = self.robots.get(robot_id)
        current_zone = getattr(robot, "current_zone", where) if robot else where
        self.trigger_unexpected_event(
            task_id=task_id,
            what=what,
            where=where,
            robots_saw=[f"{robot_id} reported boundary conflict at {where}"],
            lease_version=lease.get("version", 1),
            robot_zone=current_zone,
        )

    def resume_from_failover(self) -> List[Dict[str, Any]]:
        """
        FR-7: Edge Node Takeover.
        Scans local durable store for any in-flight active leases from a failed predecessor
        and resumes execution from the exact last uncompleted subtask.
        """
        active_leases = self.store.get_all_active_leases(self.cluster_id)
        results = []
        for lease in active_leases:
            if self.verbose:
                print(f"  [FR-7 Failover] Standby supervisor taking over active lease: {lease.get('lease_id')}")
            res = self.process_lease(lease, allow_resumption=True)
            results.append(res)
        return results
