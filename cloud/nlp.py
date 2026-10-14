"""Deterministic request parsing driven entirely by the pack.

This is the offline model stand-in's brain and the form-style path used by the
C3 baseline and the outage fallback (PRD FR-18). It is context-free on purpose:
it sees the text (and an answer), the pack's patterns, and the location graph.
Nothing here knows what an item or a destination *is*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pack import Pack
from .topology import Topology


@dataclass
class Parsed:
    item: str | None = None
    destination: dict | None = None   # Topology.locate result
    source: dict | None = None
    priority: str = ""
    deadline_minutes: int = 0
    fields: dict = field(default_factory=dict)   # extra spec fields set by resolved readings
    missing: list[str] = field(default_factory=list)


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _resolve_rule(pack: Pack, rule: dict, parsed: Parsed, text_all: str, answer: str | None) -> None:
    """Apply the reading of a clarify-rule's question that the text (or a
    yes/no answer) selects, if any. A reading can fix the item or set spec fields."""
    q = rule.get("question")
    if not q or parsed.item not in q.get("resolve_items", [parsed.item]):
        return
    chosen = None
    for i, rd in enumerate(q["readings"]):
        if _any(rd["patterns"], text_all) and not _any(rd.get("exclude", []), text_all):
            chosen = i
            break
    if chosen is None and answer:
        if "affirm" in q and _any(pack.doc["nlp"]["affirm"], answer):
            chosen = q["affirm"]
        elif "deny" in q and _any(pack.doc["nlp"]["deny"], answer):
            chosen = q["deny"]
    if chosen is None:
        return
    for key, value in q["readings"][chosen]["set"].items():
        if key == "item":
            parsed.item = value
        else:
            parsed.fields[key] = value


def parse(pack: Pack, topo: Topology, text: str, *, answer: str | None = None) -> Parsed:
    p = Parsed()
    text_all = f"{text} {answer}" if answer else text

    def item_in(s: str) -> str | None:
        return next((i["id"] for i in pack.items if _any(i["patterns"], s)), None)

    p.item = item_in(text) or (item_in(answer) if answer else None)

    # "from X" names where the item is now; the rest of the text names where it goes.
    dest_text = text_all
    for rule in pack.doc["nlp"].get("source", []):
        m = re.search(rule["pattern"], text_all, re.IGNORECASE)
        if m and (hit := topo.locate(m.group(rule["group"]))):
            p.source = hit
            dest_text = text_all[: m.start()] + " " + text_all[m.end():]
            break
    p.destination = topo.locate(dest_text)
    if p.destination is None and p.source and pack.doc["nlp"].get("default_destination") == "area_source":
        area = topo.nodes[p.source["anchor"]]["area"]
        node = topo.source_of(area)
        p.destination = {"zone": node, "anchor": node, "label": topo.nodes[node]["kind"]}

    for rule in pack.rules:
        _resolve_rule(pack, rule, p, text_all, answer)

    p.priority = next((pr["id"] for pr in pack.priorities if _any(pr["patterns"], text_all)), pack.doc["default_priority"])
    dl = pack.doc["deadline"]
    minutes = pack.priority(p.priority)["default_deadline_minutes"]
    soft = dl.get("soft")
    if soft and re.search(soft["pattern"], text_all, re.IGNORECASE) and p.priority in soft.get("only_priorities", [p.priority]):
        minutes = soft["minutes"]
    for blob in dl["patterns"]:
        if (m := re.search(blob["pattern"], text_all, re.IGNORECASE)):
            minutes = int(int(m.group(1)) * blob["minutes_per_unit"])
            break
    p.deadline_minutes = max(dl["min_minutes"], min(dl["max_minutes"], minutes))

    if p.item is None:
        p.missing.append("item")
    if p.destination is None:
        p.missing.append("destination")
    return p


def missing_question(pack: Pack, parsed: Parsed) -> dict:
    """The question for a request that lacks an item and/or a destination."""
    qs, nouns = pack.doc["questions"], pack.doc["nouns"]
    key = ("missing_both" if len(parsed.missing) == 2 else
           "missing_item" if "item" in parsed.missing else "missing_destination")
    spec = qs[key]
    readings = ([i["label"] for i in pack.items if not i.get("variant_of")]
                if spec["readings"] == "items" else [r.format(example=nouns["example"]) for r in spec["readings"]])
    return {"question": spec["text"].format(item_noun=nouns["item"], destination_noun=nouns["destination"]),
            "possible_readings": readings, "missing_fields": list(parsed.missing)}


def spec_from(parsed: Parsed, note: str = "deterministic parse") -> dict:
    spec = {"item": parsed.item, "destination": parsed.destination["zone"], "priority": parsed.priority,
            "deadline_minutes": parsed.deadline_minutes, "constraints": [],
            "rationale": f"{parsed.item} to {parsed.destination['zone']} ({parsed.priority}); {note}"}
    if parsed.source:
        spec["source"] = parsed.source["zone"]
    spec.update(parsed.fields)
    return spec
