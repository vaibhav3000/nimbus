"""The lease bundle (PRD Sec. 6: "with each lease, the edge receives the data and rules it needs, so it never has to
fetch them mid-task"; context Q-03).

The lease schema has exactly 12 fields and does not change, so what the edge needs travels beside it as a
`BUNDLE` message on the cluster's `notice` topic, one per lease version: the part of the map the lease allows (with
speed caps and distances, so the edge can plan detours without asking), the rules that apply, the restrictions and
closures in force, and when to escalate. When a results bucket is configured the full bundle is also stored in S3 and
the message carries `bundle_ref`. The message is kept small (a few KB, like the other cloud-edge messages).
"""

from __future__ import annotations

import json
import os

from .clock import iso

MAX_BYTES = 4000


def build_bundle(orch, lease: dict, spec: dict) -> dict:
    topo, now = orch.topo, orch.clock.now()
    allowed = set(lease["allowed_zones"])
    dest = topo.anchor_of(spec["destination"]) if spec.get("destination") else None
    flags = tuple(k for k, v in topo.facts_for(dest).items() if v is True) if dest else ()
    rules = [{"id": p["id"], "text": p["text"][:240]} for p in orch.policies.retrieve(
        f"{spec.get('item', '')} {spec.get('destination', '')}", extra_terms=flags)]
    restr = {z: {"kind": r["kind"], "effect": r["effect"], "exempt": r["exempt_priorities"]}
             for z, r in orch.policies.restrictions().items() if z in allowed or z == dest}
    closed = {z: c["state"] for z, c in orch.mapstate.closed(now).items() if z in allowed}
    bundle = {
        "map": {"nodes": {n: {k: v for k, v in {"kind": topo.nodes[n]["kind"], "cap": topo.nodes[n].get("speed_cap"),
                                                 "pos": topo.nodes[n].get("pos")}.items() if v is not None}
                          for n in sorted(allowed) if n in topo.nodes},
                "edges": [[a, b, w] for a, b, w in orch.pack.topology_doc["edges"] if a in allowed and b in allowed]},
        "rules": rules, "restrictions": restr, "closed": closed,
        "guidance": {"stay_inside": lease["allowed_zones"], "never_enter": lease["forbidden_zones"],
                     "escalate_when": lease["when_to_escalate"], "priority": lease["priority"]},
    }
    if len(json.dumps(bundle)) > MAX_BYTES:                      # keep it small: drop rule texts first, then distances
        bundle["rules"] = [{"id": r["id"]} for r in rules]
    if len(json.dumps(bundle)) > MAX_BYTES:
        bundle["map"]["edges"] = [[a, b] for a, b, *_ in bundle["map"]["edges"]]
    return bundle


def bundle_publish(orch, lease: dict, spec: dict) -> tuple[str, dict] | None:
    """(topic, payload) for the bundle of this lease, or None when bundles are switched off (NIMBUS_BUNDLES=0)."""
    if os.environ.get("NIMBUS_BUNDLES", "1") == "0":
        return None
    bundle = build_bundle(orch, lease, spec)
    task_id = lease["lease_id"][len("lease-"):lease["lease_id"].rindex("-v")]
    msg = {"type": "BUNDLE", "task_id": task_id, "lease_id": lease["lease_id"], "lease_version": lease["version"],
           "message_id": f"bundle-{lease['lease_id']}", "ts": iso(orch.clock.now()), "bundle": bundle}
    if orch.results.bucket:                                      # the full copy lives in S3; the edge may fetch it, it need not
        ref = orch.results.put_json(f"bundles/{lease['lease_id']}.json", msg)
        if ref:
            msg["bundle_ref"] = ref
    return (f"nimbus/{lease['cluster_id']}/notice", msg)
