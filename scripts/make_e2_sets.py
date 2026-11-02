"""Generate the DRAFT E2 sets (PRD E2: 50 requests with a quarter vague, 10 plain-language rules, 20 context-dependent
requests) into evaluation/e2/. The labels are drafts written from templates: a person must review and correct them before they count
as gold (every file says so). Synthetic data only. Re-running is deterministic.

    python scripts/make_e2_sets.py            # writes evaluation/e2/{requests,rules,context}.json and evaluation/manifest.json
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evaluation" / "e2"
STATUS = "DRAFT: labels written from templates; a person must review before use as gold"

ITEMS = {"wheelchair": ["a wheelchair", "a wheel chair"], "iv_pole": ["an iv pole", "an IV pole"], "monitor": ["a monitor", "a patient monitor"],
         "blanket": ["a blanket", "two blankets"]}
PLACES = {"east_waiting_approach": ["2B", "ward 2B"], "west_waiting_approach": ["2C", "ward 2C"], "south_room_approach": ["ward 3"]}
FRAMES = ["Deliver {item} to {place}", "I need {item} in {place}", "Please bring {item} to {place}", "{Item} to {place} please", "Can someone take {item} to {place}"]


def plain(rng: random.Random, n: int) -> list[dict]:
    out = []
    for i in range(n):
        item = rng.choice(list(ITEMS)); node = rng.choice(list(PLACES))
        urgent = rng.random() < 0.35
        phrase = rng.choice(ITEMS[item]); place = rng.choice(PLACES[node])
        text = rng.choice(FRAMES).format(item=phrase, Item=phrase.capitalize(), place=place) + (", urgent" if urgent else ", routine" if rng.random() < .3 else "")
        gold = {"item": item, "destination": node, "priority": "urgent" if urgent else "routine", "deadline_minutes": 12 if urgent else 60}
        if rng.random() < 0.2:
            mins = rng.choice([15, 20, 30, 45])
            text += f" within {mins} minutes"; gold["deadline_minutes"] = mins
        out.append({"id": f"req-{len(out) + 1:02d}", "text": text + ".", "gold": gold})
    return out


VAGUE = [
    ("a pump to 2B", "variant"), ("Need an infusion pump at 2B-12, urgent.", "variant"), ("infusion pump to 2B please", "variant"),
    ("I need a wheelchair", "destination"), ("bring a monitor", "destination"), ("send an iv pole urgently", "destination"),
    ("bring something to ward 3", "item"), ("equipment for 2C", "item"), ("take it to 2B", "item"),
    ("pump needed at 2B", "variant"), ("a paediatric thing for 2B", "item"), ("something urgent to ward 3", "item"), ("need a blanket", "destination"),
]


def build_requests() -> dict:
    rng = random.Random(252)
    rows = plain(rng, 37)
    for text, about in VAGUE:
        rows.append({"id": f"req-{len(rows) + 1:02d}", "text": text, "gold": {"ask": True, "about": about}})
    rng.shuffle(rows)
    for i, r in enumerate(rows, 1):
        r["id"] = f"req-{i:02d}"
    return {"status": STATUS, "synthetic": True, "n": len(rows), "vague": sum("ask" in r["gold"] for r in rows), "requests": rows}


RULES = [
    ("Ward 2C is now isolation.", {"zone": "west_waiting_approach", "kind": "isolation", "lift": False, "extract_items": []}),
    ("Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in.",
     {"zone": "west_waiting_approach", "kind": "isolation", "lift": False, "extract_items": ["infusion_pump"]}),
    ("2B is in isolation, nothing routine goes in.", {"zone": "east_waiting_approach", "kind": "isolation", "lift": False, "extract_items": []}),
    ("Please isolate ward 3.", {"zone": "south_room_approach", "kind": "isolation", "lift": False, "extract_items": []}),
    ("Ward 2B is now infectious, take the wheelchairs out.", {"zone": "east_waiting_approach", "kind": "isolation", "lift": False, "extract_items": []}),   # nothing to move: 2B holds no wheelchairs
    ("Ward 2C is no longer in isolation.", {"zone": "west_waiting_approach", "kind": "isolation", "lift": True, "extract_items": []}),
    ("Isolation is over in ward 3.", {"zone": "south_room_approach", "kind": "isolation", "lift": True, "extract_items": []}),
    ("2B is not in isolation any more.", {"zone": "east_waiting_approach", "kind": "isolation", "lift": True, "extract_items": []}),
    ("Quarantine ward 2C and move the monitors out.", {"zone": "west_waiting_approach", "kind": "isolation", "lift": False, "extract_items": []}),   # 2C holds pumps, not monitors
    ("ward 3 is now quarantined", {"zone": "south_room_approach", "kind": "isolation", "lift": False, "extract_items": []}),
]


def build_rules() -> dict:
    return {"status": STATUS, "synthetic": True, "n": len(RULES), "rules": [{"id": f"rule-{i:02d}", "text": t, "gold": g} for i, (t, g) in enumerate(RULES, 1)]}


def build_context() -> dict:
    """Habit cases: after N confirmed paediatric requests at 2B, "a pump to 2B" should not need a question (FR-19). Controls
    without a habit must still ask; explicit "standard" must be honoured."""
    cases = []
    for i in range(10):    # habit built: two confirmations, then the bare request
        cases.append({"id": f"ctx-{len(cases) + 1:02d}", "setup": 2, "text": ["a pump to 2B", "Need an infusion pump at 2B-12, urgent.", "infusion pump to 2B please"][i % 3],
                      "gold": {"ask": False, "item": "infusion_pump_paediatric"}, "kind": "habit"})
    for i in range(5):     # no habit: one confirmation is not enough
        cases.append({"id": f"ctx-{len(cases) + 1:02d}", "setup": i % 2, "text": ["a pump to 2B", "Need an infusion pump at 2B-12, urgent."][i % 2],
                      "gold": {"ask": True, "about": "variant"}, "kind": "control"})
    for i in range(5):     # the requester says "standard": honoured even with a habit
        cases.append({"id": f"ctx-{len(cases) + 1:02d}", "setup": 2, "text": "A standard infusion pump to 2B, no paediatric needed.",
                      "gold": {"ask": False, "item": "infusion_pump"}, "kind": "override"})
    return {"status": STATUS, "synthetic": True, "n": len(cases), "cases": cases}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, doc in (("requests", build_requests()), ("rules", build_rules()), ("context", build_context())):
        (OUT / f"{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        print(f"wrote evaluation/e2/{name}.json ({doc['n']} items)")
    manifest = {"note": "Every dataset used by the experiments. Only synthetic data is allowed (PRD NFR-5, checked by scripts/audit_data.py).",
                "datasets": [{"path": f"evaluation/e2/{n}.json", "synthetic": True, "description": d} for n, d in
                             (("requests", "50 staff requests, a quarter vague, template-generated"), ("rules", "10 plain-language rule statements"),
                              ("context", "20 context-dependent requests with habit setup"))]
                + [{"path": f"traces/{n}", "kind": "network-measurement", "description": "network RTT/loss traces recorded by the team on campus (no patient data)", "contains_personal_data": False}
                   for n in ("iisc_basement.csv", "iisc_corridor.csv", "iisc_lift.csv")]}
    (ROOT / "evaluation" / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("wrote evaluation/manifest.json")


if __name__ == "__main__":
    main()
