import json
from pathlib import Path

data_dir = Path("/root/nimbus/data")
data_dir.mkdir(parents=True, exist_ok=True)

with open(data_dir / "claims.json", "w") as f:
    json.dump({"leases": {}, "claims": {}}, f, indent=2)

with open(data_dir / "tasks.json", "w") as f:
    json.dump({"_seen_message_ids": []}, f, indent=2)

(data_dir / "decisions.jsonl").write_text("")
(data_dir / "outbox.jsonl").write_text("")

print("Clean data directory initialized successfully.")
