from pathlib import Path

p = Path("edge/supervisor/supervisor.py")
text = p.read_text(encoding="utf-8")

text = text.replace('task_id = lease.get("task_id", lease_id)', 'task_id = self._extract_task_id(lease)')
text = text.replace('task_id = lease.get("task_id", lease["lease_id"])', 'task_id = self._extract_task_id(lease)')

old_send = '''        url = f"{self.cloud_url}/escalations"
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
        except Exception:
            # Remains pending in SQLite store for upload on reconnect (FR-10)
            pass
        return False'''

new_send = '''        url = f"{self.cloud_url}/escalations"
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
        return False'''

text = text.replace(old_send, new_send)

flush_method = '''
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
'''

if "def flush_pending_escalations" not in text:
    text += flush_method

p.write_text(text, encoding="utf-8")
print("Done patching supervisor.py")
