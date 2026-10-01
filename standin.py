"""Cloud stand-in: a rule-driven fake orchestrator (FR-13/14/15 shape only).

This module is exactly what Person 3 replaces with the Bedrock orchestrator
plus scheduler plus rule checker. Its decisions are deterministic rules, not
an LLM: it parses the request text crudely, decides clarify-vs-lease, and
builds a lease document that validates against the published lease schema.
Every decision is written to the decision log (FR-20) by the caller.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# SC-1: bed 2B-12 is a paediatric bed; the stand-in's static hospital fact.
PAEDIATRIC_BED_RE = re.compile(r"\b2B-?12\b", re.IGNORECASE)
ZONE_RE = re.compile(r"\b(?:ward\s*)?(\d[A-Z])[- ]?(\d{1,3})\b", re.IGNORECASE)
WARD_RE = re.compile(r"\bward\s+(\d+)\b", re.IGNORECASE)

URGENT_RE = re.compile(r"\burgent\b|\basap\b|\bnow\b", re.IGNORECASE)

WHEN_TO_ESCALATE = ["door_closed", "deadline_at_risk", "robot_failed"]
CLUSTER_FOR_REQUEST = "east-wing"  # stand-in: every request goes to the East wing


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_zones(text: str) -> list[str]:
    zones = [f"{m.group(1)}-{m.group(2)}" for m in ZONE_RE.finditer(text)]
    zones += [f"ward-{m.group(1)}" for m in WARD_RE.finditer(text)]
    return zones or ["unspecified"]


def wants_clarification(text: str) -> bool:
    """True when the stand-in's rules say the request is too vague to lease.

    Stand-in shape of FR-13's "ask when unsure": bed 2B-12 is paediatric, so a
    request that names it without naming the pump type gets a clarifying
    question before any lease exists.
    """
    return bool(PAEDIATRIC_BED_RE.search(text)) and not re.search(
        r"paediatric|pediatric|standard", text, re.IGNORECASE
    )


def clarifying_question() -> dict:
    """The question and its possible readings (CLARIFY payload shape, Table 2)."""
    return {
        "question": "2B-12 is a paediatric bed. Do you need a paediatric pump?",
        "possible_readings": ["paediatric infusion pump", "standard infusion pump"],
    }


def build_lease(task_id: str, text: str, version: int = 1) -> dict:
    """A schema-valid lease v1 for the request (stand-in scheduler + assignment)."""
    urgent = bool(URGENT_RE.search(text))
    zones = extract_zones(text)
    issued = datetime.now(timezone.utc)
    deadline_min = 12 if urgent else 60  # SC-1: urgent deliveries get 12 minutes
    return {
        "lease_id": f"lease-{task_id}-v{version}",
        "version": version,
        "priority": "urgent" if urgent else "routine",
        "deadline_ts": (issued + timedelta(minutes=deadline_min))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "expiry_ts": (issued + timedelta(minutes=deadline_min + 3))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "allowed_zones": zones,
        "forbidden_zones": [],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": zones[0]}},
            {"cmd": "beep", "robot": "R1", "params": {}},
        ],
        "cluster_id": CLUSTER_FOR_REQUEST,
        "when_to_escalate": WHEN_TO_ESCALATE,
    }
