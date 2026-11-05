#!/usr/bin/env bash
# Stand-in simulation stack that can run NEXT TO the Docker/Gazebo stack (own ports and folders).
#   ./run_standin.sh            start  -> app http://127.0.0.1:8100   robots http://127.0.0.1:8190
#   ./run_standin.sh stop
#   python3 scripts/demo_e2e.py --api http://127.0.0.1:8100        # send the sample requests
cd "$(dirname "$0")"
export NIMBUS_LOCAL_CLOUD=1 PORT=8100 VIEWER_PORT=8190 DATA_DIR="$PWD/data_standin" LOGS_DIR="$PWD/logs_standin"
if [ "${1:-}" = stop ]; then ./stop_all.sh; exit 0; fi
exec ./run_all.sh kinematic
