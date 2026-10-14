"""Domain Pack loader: the single place domain knowledge enters the core.

A pack is a directory (packs/<name>/): pack.json plus topology.json, fleet.json,
policies.json and prompt files. Loading validates the pack against
packs/pack.schema.json and cross-checks it against the *contracts* (lease
priority enum, robot sub-task commands) and against itself (kinds exist,
items referenced by rules exist, restrictions cite real policies), so a bad
pack fails at load with every problem listed, not mid-request.

The pack hash goes into every decision-log context digest, so a result always
says which rules produced it (NFR-6).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from jsonschema import Draft202012Validator

import contracts

PACKS_DIR = Path(__file__).resolve().parents[1] / "packs"
CORE_TOOLS = {"submit_task_spec", "ask_clarification", "propose_allocation"}


class PackError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("invalid pack:\n  - " + "\n  - ".join(problems))
        self.problems = problems


class Pack:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.doc = json.loads((self.root / "pack.json").read_text(encoding="utf-8"))
        self.topology_doc = json.loads((self.root / "topology.json").read_text(encoding="utf-8"))
        self.fleet_doc = json.loads((self.root / "fleet.json").read_text(encoding="utf-8"))
        self.policy_docs = json.loads((self.root / "policies.json").read_text(encoding="utf-8"))
        self.prompts: dict[str, str] = {}
        for key, rel in self.doc.get("prompts", {}).items():
            f = self.root / rel
            self.prompts[key] = f.read_text(encoding="utf-8").strip() if f.exists() else ""
        sc = self.root / "scenarios.json"
        self.scenarios: list[dict] = json.loads(sc.read_text(encoding="utf-8")) if sc.exists() else []
        blob = json.dumps([self.doc, self.topology_doc, self.fleet_doc, self.policy_docs, self.prompts], sort_keys=True)
        self.hash = hashlib.sha256(blob.encode()).hexdigest()
        self._validate()

    @classmethod
    def load(cls, name: str | None = None) -> "Pack":
        name = name or os.environ.get("NIMBUS_PACK") or (PACKS_DIR / "DEFAULT").read_text(encoding="utf-8").strip()
        root = Path(name) if os.sep in name or name.startswith(".") else PACKS_DIR / name
        return cls(root)

    # -- identity ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.doc["name"]

    @property
    def id(self) -> str:
        return f"{self.name}@{self.doc['version']}:{self.hash[:8]}"

    # -- typed views ----------------------------------------------------------------

    @property
    def items(self) -> list[dict]:
        return self.doc["items"]

    def item(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i["id"] == item_id), None)

    def uses_stock(self, item_id: str) -> bool:
        i = self.item(item_id)
        return bool(i) and i.get("uses_stock", True)

    @property
    def priorities(self) -> list[dict]:
        return sorted(self.doc["priorities"], key=lambda p: p["rank"])

    def priority(self, pid: str) -> dict:
        return next(p for p in self.doc["priorities"] if p["id"] == pid)

    @property
    def ranks(self) -> dict[str, int]:
        return {p["id"]: p["rank"] for p in self.doc["priorities"]}

    @property
    def rules(self) -> list[dict]:
        return self.doc["rules"]

    @property
    def restrictions(self) -> dict:
        return self.doc["restrictions"]

    @property
    def ontology(self) -> dict:
        return self.doc["ontology"]

    # -- validation -------------------------------------------------------------------

    def _validate(self) -> None:
        d, problems = self.doc, []
        schema = json.loads((PACKS_DIR / "pack.schema.json").read_text(encoding="utf-8"))
        problems += [f"pack.json {e.json_path}: {e.message}" for e in Draft202012Validator(schema).iter_errors(d)]
        if problems:
            raise PackError(problems)

        def rx(label: str, pattern: str) -> None:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"{label}: bad regex {pattern!r}: {exc}")

        item_ids = [i["id"] for i in d["items"]]
        if len(set(item_ids)) != len(item_ids):
            problems.append("items: duplicate ids")
        for i in d["items"]:
            for p in i["patterns"]:
                rx(f"item {i['id']}", p)
            if i.get("variant_of") and i["variant_of"] not in item_ids:
                problems.append(f"item {i['id']}: variant_of {i['variant_of']!r} is not an item")
            if i.get("template") and i["template"] not in d["task_templates"]:
                problems.append(f"item {i['id']}: unknown template {i['template']!r}")

        lease_enum = contracts.load_schema("lease")["properties"]["priority"]["enum"]
        pids = [p["id"] for p in d["priorities"]]
        ranks = [p["rank"] for p in d["priorities"]]
        if len(set(pids)) != len(pids) or len(set(ranks)) != len(ranks):
            problems.append("priorities: ids and ranks must be unique")
        if d["default_priority"] not in pids:
            problems.append(f"default_priority {d['default_priority']!r} is not a priority")
        for p in d["priorities"]:
            if p["lease_priority"] not in lease_enum:
                problems.append(f"priority {p['id']}: lease_priority {p['lease_priority']!r} is not in the lease contract enum {lease_enum}")
            for pat in p["patterns"]:
                rx(f"priority {p['id']}", pat)

        for blob in d["deadline"]["patterns"]:
            rx("deadline", blob["pattern"])
        if "soft" in d["deadline"]:
            rx("deadline.soft", d["deadline"]["soft"]["pattern"])
        for key in ("affirm", "deny"):
            for pat in d["nlp"][key]:
                rx(f"nlp.{key}", pat)
        for s in d["nlp"].get("source", []):
            rx("nlp.source", s["pattern"])

        kinds = {n["kind"] for n in self.topology_doc["nodes"].values()}
        for k in d["ontology"]["destination_kinds"] + d["ontology"]["source_kinds"]:
            if k not in kinds:
                problems.append(f"ontology: kind {k!r} has no node in topology.json")
        for loc in d["locators"]:
            rx(f"locator {loc['label']}", loc["pattern"])
            for k in loc.get("kinds", []) + loc.get("parent_kinds", []):
                if k not in kinds:
                    problems.append(f"locator {loc['label']}: kind {k!r} has no node in topology.json")
        nodes = self.topology_doc["nodes"]
        for a, b, _w in self.topology_doc["edges"]:
            for n in (a, b):
                if n not in nodes:
                    problems.append(f"topology edge references unknown node {n!r}")
        areas = {n["area"] for n in nodes.values() if n["area"] != "shared"}
        for cid, area in self.topology_doc["clusters"].items():
            if area not in areas:
                problems.append(f"cluster {cid}: area {area!r} has no nodes")
            elif not any(n["kind"] in d["ontology"]["source_kinds"] and n["area"] == area for n in nodes.values()):
                problems.append(f"cluster {cid}: area {area!r} has no source node")
        for cid, c in self.fleet_doc["clusters"].items():
            if cid not in self.topology_doc["clusters"]:
                problems.append(f"fleet cluster {cid!r} is not in topology.json clusters")
            if c["home"] not in nodes:
                problems.append(f"fleet cluster {cid!r}: home {c['home']!r} is not a topology node")
            for item in c["stock"]:
                if item not in item_ids:
                    problems.append(f"fleet cluster {cid!r}: stock of unknown item {item!r}")

        cmds = contracts.load_schema("robot_subtask")["properties"]["cmd"]["enum"]
        for name, steps in d["task_templates"].items():
            for s in steps:
                if s["cmd"] not in cmds:
                    problems.append(f"task template {name}: {s['cmd']!r} is not a robot sub-task command {cmds}")
        if d["default_template"] not in d["task_templates"]:
            problems.append(f"default_template {d['default_template']!r} is not defined")

        policy_ids = {p["id"] for p in self.policy_docs}
        for r in d["rules"]:
            if r["otherwise"] == "clarify" and "question" not in r:
                problems.append(f"rule {r['id']}: clarify needs a question")
            for it in r.get("question", {}).get("resolve_items", []):
                if it not in item_ids:
                    problems.append(f"rule {r['id']}: resolve_items names unknown item {it!r}")
            for rd in r.get("question", {}).get("readings", []):
                for pat in rd["patterns"] + rd.get("exclude", []):
                    rx(f"rule {r['id']}", pat)
                si = rd["set"].get("item")
                if si and si not in item_ids:
                    problems.append(f"rule {r['id']}: reading sets unknown item {si!r}")
            if r["id"] not in policy_ids:
                problems.append(f"rule {r['id']} has no matching document in policies.json")
        for kind, rs in d["restrictions"].items():
            if rs["policy_id"] not in policy_ids:
                problems.append(f"restriction {kind}: policy {rs['policy_id']!r} is not in policies.json")
            for p in rs.get("exempt_priorities", []):
                if p not in pids:
                    problems.append(f"restriction {kind}: exempt priority {p!r} is not a priority")

        for step, tools in d["steps"].items():
            for t in tools:
                if t not in CORE_TOOLS:
                    problems.append(f"step {step}: unknown tool {t!r}")
        for key in d["prompts"]:
            if not self.prompts.get(key):
                problems.append(f"prompt {key!r}: file missing or empty")
        if problems:
            raise PackError(problems)
