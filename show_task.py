import json

tasks = json.load(open("/root/nimbus/data/tasks.json"))
for tid, task in tasks.items():
    if tid.startswith("_"): continue
    print("=== Task:", tid, "===")
    print("Status:", task.get("status"))
    lease = task.get("lease")
    if lease:
        print("Cluster:", lease.get("cluster_id"))
        print("Assigned Robots:", lease.get("assigned_robots"))
        print("Allowed Zones:", lease.get("allowed_zones"))
        print("Task Sequence:")
        for step in lease.get("task_sequence", []):
            print("  -", step)
