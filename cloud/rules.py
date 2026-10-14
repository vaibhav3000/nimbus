"""A tiny, safe condition language for pack rules (no eval, no code in data).

Conditions are JSON: {"all": [...]}, {"any": [...]}, {"not": c}, and leaves
{"eq": [a, b]}, {"ne": ...}, {"in": [a, [list]]}, {"truthy": "$path"},
{"gt"|"ge"|"lt"|"le": [a, b]}. An operand beginning with "$" is a path into
the facts ("$spec.item", "$dest.<attribute>", "$source.zone"); anything else
is a literal. A missing path is None, never an error, so a rule about an
attribute some zones lack simply does not apply to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def lookup(path: str, facts: dict):
    cur = facts
    for part in path.lstrip("$").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _val(operand, facts):
    if isinstance(operand, str) and operand.startswith("$"):
        return lookup(operand, facts)
    return operand


def evaluate(cond: dict, facts: dict) -> bool:
    if not isinstance(cond, dict) or len(cond) != 1:
        raise ValueError(f"a condition has exactly one operator: {cond!r}")
    (op, arg), = cond.items()
    if op == "all":
        return all(evaluate(c, facts) for c in arg)
    if op == "any":
        return any(evaluate(c, facts) for c in arg)
    if op == "not":
        return not evaluate(arg, facts)
    if op == "truthy":
        return bool(_val(arg, facts))
    a, b = (_val(x, facts) for x in arg)
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "in":
        return a in (b or [])
    try:
        if op == "gt": return a > b
        if op == "ge": return a >= b
        if op == "lt": return a < b
        if op == "le": return a <= b
    except TypeError:
        return False
    raise ValueError(f"unknown operator {op!r}")


def render(template: str, facts: dict) -> str:
    """Fill {a.b} placeholders from the facts; a missing value renders empty."""
    return re.sub(r"\{([\w.]+)\}", lambda m: str(lookup(m.group(1), facts) if lookup(m.group(1), facts) is not None else ""), template)


@dataclass
class RuleOutcome:
    passed: list = field(default_factory=list)      # ["policy:<id>", ...] rules that applied and held
    violations: list = field(default_factory=list)  # checker-shaped dicts


def apply_rules(rules: list[dict], facts: dict) -> RuleOutcome:
    """Evaluate every pack rule against the facts of one proposal."""
    out = RuleOutcome()
    for rule in rules:
        if not evaluate(rule["applies"], facts):
            continue
        if evaluate(rule["requires"], facts):
            out.passed.append(f"policy:{rule['id']}")
            continue
        v = {"rule": "policy", "detail": rule["message"], "kind": rule["otherwise"], "policy": rule["id"]}
        if rule["otherwise"] == "clarify":
            q = rule["question"]
            v["question"] = {
                "question": render(q["text"], facts),
                "possible_readings": [r["label"] for r in q["readings"]],
                "missing_fields": [q.get("missing_field", "confirmation")],
                "rule": rule["id"],
            }
        out.violations.append(v)
    return out
