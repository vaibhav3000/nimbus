"""
Nimbus Edge Supervisor Runner (Person 2)
Runs a continuous Edge Supervisor daemon for a specific cluster (e.g. east-wing or west-wing).
Sends periodic idle heartbeats (POST /fleet/heartbeat), active lease check-ins (POST /checkins),
polls cloud outbox for leases, executes sub-tasks locally, and sends OUTPUT_READY escalations.
Incorporates Person 1's Gazebo hospital topology and waypoints for spatial authority.

Usage:
    python edge/run_supervisor.py --cluster east-wing
    python edge/run_supervisor.py --cluster west-wing
"""

import argparse
from pathlib import Path
import sys
import time

# Ensure repository root is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor


def main():
    parser = argparse.ArgumentParser(description="Run an edge supervisor cluster daemon.")
    parser.add_argument("--cluster", default="east-wing", help="Cluster ID (e.g., east-wing, west-wing)")
    parser.add_argument("--cloud-url", default="http://127.0.0.1:8000", help="Cloud API endpoint URL")
    parser.add_argument("--db", default=None, help="Path to local SQLite DB (defaults to edge_<cluster>.db)")
    parser.add_argument("--interval", type=float, default=2.0, help="Outbox polling interval in seconds")
    parser.add_argument("--heartbeat-interval", type=float, default=15.0, help="Idle heartbeat interval in seconds")
    parser.add_argument("--topology", default=None, help="Path to custom hospital_topology.json")
    parser.add_argument("--waypoints", default=None, help="Path to custom topology_waypoints.json")
    parser.add_argument("--robot-backend", choices=["mock", "sim"], default="mock", help="Robot backend: mock (in-memory) or sim (Person 1 ground_robot_sdk/Gazebo)")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed per-step execution logs")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    db_path = args.db or f"edge_{args.cluster.replace('-', '_')}.db"
    verbose_mode = not args.quiet

    supervisor = EdgeSupervisor(
        cluster_id=args.cluster,
        cloud_url=args.cloud_url,
        db_path=db_path,
        slm_mode="auto",
        topology_file=args.topology,
        waypoints_file=args.waypoints,
        verbose=verbose_mode,
        robot_backend=args.robot_backend,
    )

    print("=" * 70)
    print(f"  NIMBUS EDGE SUPERVISOR DAEMON: [{args.cluster.upper()}]")
    print("=" * 70)
    print(f"  Cluster ID       : {args.cluster}")
    print(f"  Robot Backend    : {args.robot_backend.upper()} ({'ground_robot_sdk / Gazebo' if args.robot_backend == 'sim' else 'in-memory mock'})")
    print(f"  Cloud Endpoint   : {args.cloud_url}")
    print(f"  Local SQLite DB  : {db_path}")
    print(f"  Topology Graph   : {len(supervisor.topology.get('nodes', {}))} nodes, {len(supervisor.waypoints)} waypoints")
    print(f"  Home Zone        : {supervisor.home_zone}")
    print(f"  Safe Parking Bay : {supervisor.safe_parking_zone}")
    print(f"  Edge SLM Mode    : {supervisor.slm.mode} ({supervisor.slm.model_name})")
    print(f"  Live Trace Mode  : {'ENABLED (Real-Time Subtask Streaming)' if verbose_mode else 'QUIET'}")
    print(f"  Polling Interval : {args.interval}s")
    print(f"  Heartbeat Rate   : every {args.heartbeat_interval}s while idle")
    print("  Status           : Active & Listening for Leases...\n")

    processed_leases = set()
    last_heartbeat_time = 0.0

    try:
        while True:
            # 1. Flush any pending escalations (store-and-forward FR-10)
            flushed = supervisor.flush_pending_escalations()
            if flushed > 0:
                print(f"[{args.cluster}] Uploaded {flushed} pending escalation(s) from local SQLite outbox to cloud.")

            now_time = time.time()

            # 2. Send periodic heartbeat to POST /fleet/heartbeat
            if now_time - last_heartbeat_time >= args.heartbeat_interval:
                ok, hb_data = supervisor.send_heartbeat_to_cloud()
                if ok:
                    print(f"[{args.cluster}] Heartbeat sent -> cluster health: {hb_data.get('health', 'healthy')}")
                last_heartbeat_time = now_time

            # 3. Fetch new leases from cloud outbox
            leases = supervisor.fetch_leases_from_cloud()
            for lease in leases:
                lid = lease.get("lease_id")
                ver = lease.get("version", 1)
                key = f"{lid}-v{ver}"
                if key in processed_leases:
                    continue

                target_cluster = lease.get("cluster_id")
                if target_cluster != args.cluster:
                    # Ignore leases targeted to other clusters
                    continue

                result = supervisor.process_lease(lease, verbose=verbose_mode)
                status = result.get("status")
                print(f"[{args.cluster}] >>> Final Mission Status: {status}")
                if status == "COMPLETED":
                    print(f"[{args.cluster}]     Result Ref : {result.get('result_ref')}")
                    if "sha256" in result:
                        print(f"[{args.cluster}]     SHA-256    : {result.get('sha256')}")
                elif status in ("REJECTED", "FAILED_BOUNDARY", "DROPPED_STALE"):
                    print(f"[{args.cluster}]     Reason     : {result.get('reason')}")

                processed_leases.add(key)
                last_heartbeat_time = time.time()

            if args.once:
                break
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n[{args.cluster}] Supervisor shutting down cleanly.")


if __name__ == "__main__":
    main()
