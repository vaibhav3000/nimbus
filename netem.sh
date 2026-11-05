#!/usr/bin/env bash
# ==============================================================================
# Nimbus Linux tc/netem Network Fault Injection Script (Experiment E4/E5)
# Validates PRD FR-6 (Lease authority under network cut) & NFR-2 (Resilience).
# Usage:
#   ./netem.sh <profile> [interface]
#   ./netem.sh clear [interface]
# Profiles:
#   mild_wan        : 60ms delay, 15ms jitter, 1.0% loss (Hospital WAN link)
#   shielded_room   : 250ms delay, 80ms jitter, 5.0% loss (Shielded corridor/bay)
#   severe          : 120ms delay, 40ms jitter, 15.0% loss (Severe packet drop)
#   basement_outage : 100% loss (Complete WAN partition / elevator shaft)
#   clear           : Remove tc qdisc rules
# ==============================================================================

set -euo pipefail

PROFILE="mild_wan"
IFACE="eth0"

echo "== Nimbus netem: Applying profile \"\" on interface \"\""

# Clean up any existing qdisc on the interface
sudo tc qdisc del dev "" root 2>/dev/null || true

case "" in
  mild_wan)
    sudo tc qdisc add dev "" root netem delay 60.0ms 15.0ms distribution normal loss 1.00%
    echo "[OK] Injected mild_wan: delay 60ms (jitter 15ms), loss 1.0%"
    ;;
  shielded_room)
    sudo tc qdisc add dev "" root netem delay 250.0ms 80.0ms distribution normal loss 5.00%
    echo "[OK] Injected shielded_room: delay 250ms (jitter 80ms), loss 5.0%"
    ;;
  severe)
    sudo tc qdisc add dev "" root netem delay 120.0ms 40.0ms distribution normal loss 15.00%
    echo "[OK] Injected severe: delay 120ms (jitter 40ms), loss 15.0%"
    ;;
  basement_outage)
    sudo tc qdisc add dev "" root netem loss 100%
    echo "[OK] Injected basement_outage: 100% loss (complete partition)"
    ;;
  clear)
    echo "[OK] Removed netem rules. Interface  restored to nominal line rate."
    ;;
  *)
    echo "Unknown profile: "
    echo "Available profiles: mild_wan, shielded_room, severe, basement_outage, clear"
    exit 1
    ;;
esac

