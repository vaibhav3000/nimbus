"""
Patch script for adding heartbeat and check-in to supervisor.py
"""

from pathlib import Path

p = Path("edge/supervisor/supervisor.py")
text = p.read_text(encoding="utf-8")

# Ensure imports
if "import re" not in text:
    text = text.replace("import json", "import json\nimport re\nimport uuid")
if "Tuple" not in text:
    text = text.replace("from typing import Any, Dict, List, Optional", "from typing import Any, Dict, List, Optional, Tuple")

# Add send_heartbeat_to_cloud and send_checkin_to_cloud
liveness_methods = '''
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
'''

if "def send_heartbeat_to_cloud" not in text:
    target = "    def process_lease(self, lease: Dict[str, Any]) -> Dict[str, Any]:"
    text = text.replace(target, liveness_methods + "\n" + target)

# Check-in inside process_lease
old_process_start = '''        self.active_lease = lease
        task_id = self._extract_task_id(lease)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = set(lease.get("allowed_zones", []))
        forbidden_zones = set(lease.get("forbidden_zones", []))

        # Check expiry timestamp (ISO 8601 UTC)'''

new_process_start = '''        self.active_lease = lease
        task_id = self._extract_task_id(lease)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = set(lease.get("allowed_zones", []))
        forbidden_zones = set(lease.get("forbidden_zones", []))

        # Initial check-in on lease acceptance
        self.send_checkin_to_cloud(lease)
        last_checkin_time = time.time()

        # Check expiry timestamp (ISO 8601 UTC)'''

if "last_checkin_time = time.time()" not in text:
    text = text.replace(old_process_start, new_process_start)

# Add periodic check-in in loop
old_loop_step = '''            cmd = subtask["cmd"]
            robot_id = subtask.get("robot", lease["assigned_robots"][0])'''

new_loop_step = '''            # Periodic check-in every 5s during active execution (Item 1)
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
            robot_id = subtask.get("robot", lease["assigned_robots"][0])'''

if "Periodic check-in every 5s" not in text:
    text = text.replace(old_loop_step, new_loop_step)

# Reset active lease when completed or failed
old_complete = '''        self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")

        return {'''

new_complete = '''        self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")
        self.active_lease = None

        return {'''

text = text.replace(old_complete, new_complete)

p.write_text(text, encoding="utf-8")
print("Updated supervisor.py with heartbeat and check-in successfully!")
