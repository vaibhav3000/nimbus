#!/usr/bin/env bash
# ==============================================================================
# Nimbus Linux tc/netem Network Fault Injection Script (Experiment E4/E5)
# Validates PRD FR-6 (Lease authority under network cut) & NFR-2 (Resilience).
# Usage:
#   ./netem.sh <profile> [interface]        # default interface: eth0
#   ./netem.sh clear [interface]
#   ./netem.sh show  [interface]
# Profiles:
#   mild_wan        : 60ms delay, 15ms jitter, 1.0% loss (Hospital WAN link)
#   shielded_room   : 250ms delay, 80ms jitter, 5.0% loss (Shielded corridor/bay)
#   severe          : 120ms delay, 40ms jitter, 15.0% loss (Severe packet drop)
#   basement_outage : 100% loss (Complete WAN partition / elevator shaft)
#   clear           : Remove tc qdisc rules
#   show            : Print the qdisc currently attached
#
# Shapes only the named interface, i.e. the edge->cloud WAN leg.  The robot LAN
# is a separate interface and must stay untouched (PRD Sec. 7: traces are
# replayed "on the edge-to-cloud WAN link ... leaving the robot LAN unaffected").
# ==============================================================================

set -euo pipefail

PROFILE="${1:-}"
IFACE="${2:-eth0}"

if [ -z "$PROFILE" ]; then
  echo "usage: $0 <mild_wan|shielded_room|severe|basement_outage|clear|show> [interface]" >&2
  exit 1
fi

if ! command -v tc >/dev/null 2>&1; then
  echo "!! tc not found: install iproute2 (apt-get install -y iproute2)." >&2
  echo "   The container also needs NET_ADMIN, e.g. docker run --cap-add=NET_ADMIN." >&2
  echo "   Without it this host cannot shape the WAN link, so E4 cannot be run" >&2
  echo "   as real trace replay here -- do not report modelled E4 output as replayed." >&2
  exit 1
fi

# tc needs CAP_NET_ADMIN.  Inside the container we are already root and sudo may
# not be installed, so only reach for it when we actually need to escalate.
if [ "$(id -u)" -eq 0 ]; then
  TC="tc"
elif command -v sudo >/dev/null 2>&1; then
  TC="sudo tc"
else
  echo "!! need root or sudo to change qdiscs on $IFACE" >&2
  exit 1
fi

# Validate the interface only when iproute2 is present; some slim container
# images ship tc without `ip`, and a missing `ip` must not reject a valid name.
if command -v ip >/dev/null 2>&1; then
  if ! ip link show "$IFACE" >/dev/null 2>&1; then
    echo "!! no such interface: $IFACE" >&2
    echo "   available: $(ip -o link show | awk -F': ' '{print $2}' | paste -sd' ')" >&2
    exit 1
  fi
fi

if [ "$PROFILE" = show ]; then
  $TC qdisc show dev "$IFACE"
  exit 0
fi

echo "== Nimbus netem: applying profile \"$PROFILE\" on interface \"$IFACE\""

# Clean up any existing qdisc on the interface (absent qdisc is not an error).
$TC qdisc del dev "$IFACE" root 2>/dev/null || true

case "$PROFILE" in
  mild_wan)
    $TC qdisc add dev "$IFACE" root netem delay 60.0ms 15.0ms distribution normal loss 1.00%
    echo "[OK] Injected mild_wan: delay 60ms (jitter 15ms), loss 1.0%"
    ;;
  shielded_room)
    $TC qdisc add dev "$IFACE" root netem delay 250.0ms 80.0ms distribution normal loss 5.00%
    echo "[OK] Injected shielded_room: delay 250ms (jitter 80ms), loss 5.0%"
    ;;
  severe)
    $TC qdisc add dev "$IFACE" root netem delay 120.0ms 40.0ms distribution normal loss 15.00%
    echo "[OK] Injected severe: delay 120ms (jitter 40ms), loss 15.0%"
    ;;
  basement_outage)
    $TC qdisc add dev "$IFACE" root netem loss 100%
    echo "[OK] Injected basement_outage: 100% loss (complete partition)"
    ;;
  clear)
    # The del above already removed it; report the resulting state.
    echo "[OK] Removed netem rules. Interface $IFACE restored to nominal line rate."
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    echo "Available profiles: mild_wan, shielded_room, severe, basement_outage, clear, show" >&2
    exit 1
    ;;
esac

$TC qdisc show dev "$IFACE"
