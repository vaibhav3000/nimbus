"""The tool registry: which tools the cloud agent has, their schemas, and
which step may use which (least privilege). Schemas are generated from the
pack, so a new domain's items, priority classes and extra spec fields appear
in the tool definitions without touching this file.

Tool classes (see docs/p3/06-agent-architecture.md):
  propose  produces a structured proposal that is validated and stored
  read     side-effect-free lookups (cloud/readtools.py)
The model is never given a commit-class tool: only the Committer, after the
Rule Checker approves, writes leases.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator

from .pack import Pack


class ToolRegistry:
    def __init__(self, pack: Pack):
        self.pack = pack
        item_ids = [i["id"] for i in pack.items]
        props = {
            "item": {"enum": item_ids},
            "source": {"type": "string", "description": "Where the item is now, only if the request names it."},
            "destination": {"type": "string", "minLength": 1, "description": "A zone id from the context."},
            "priority": {"enum": [p["id"] for p in pack.priorities]},
            "deadline_minutes": {"type": "integer", "minimum": pack.doc["deadline"]["min_minutes"],
                                 "maximum": pack.doc["deadline"]["max_minutes"]},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string", "minLength": 1},
            **pack.doc.get("spec_extensions", {}),
        }
        self._tools = {
            "submit_task_spec": {
                "class": "propose",
                "description": ("Record the structured task once every field is known. Call this ONLY when the item, "
                                "its variant and the destination are unambiguous; otherwise call ask_clarification."),
                "schema": {"type": "object", "additionalProperties": False,
                           "required": ["item", "destination", "priority", "deadline_minutes", "rationale"],
                           "properties": props},
            },
            "ask_clarification": {
                "class": "propose",
                "description": ("Ask the requester one short question when a field is missing or ambiguous, or a rule "
                                "in the context requires confirmation."),
                "schema": {"type": "object", "additionalProperties": False, "required": ["question", "possible_readings"],
                           "properties": {"question": {"type": "string", "minLength": 1},
                                          "possible_readings": {"type": "array", "minItems": 1,
                                                                "items": {"type": "string", "minLength": 1}},
                                          "missing_fields": {"type": "array", "items": {"type": "string"}}}},
            },
            "propose_allocation": {
                "class": "propose",
                "description": ("Propose which edge cluster and robot should run the task. Choose only clusters that are "
                                "healthy or unknown, that hold the item (unless the task names a source) and whose area "
                                "contains the destination. A deterministic Rule Checker verifies your proposal."),
                "schema": {"type": "object", "additionalProperties": False, "required": ["cluster_id", "robot_ids", "rationale"],
                           "properties": {"cluster_id": {"type": "string", "minLength": 1},
                                          "robot_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                          "rationale": {"type": "string", "minLength": 1}}},
            },
        }
        for t in self._tools.values():
            Draft202012Validator.check_schema(t["schema"])

    def names(self, step: str | None = None) -> list[str]:
        return list(self.pack.doc["steps"][step]) if step else list(self._tools)

    def tool_class(self, name: str) -> str:
        return self._tools[name]["class"]

    def schema(self, name: str) -> dict:
        return self._tools[name]["schema"]

    def validate(self, name: str, payload: dict) -> list[str]:
        if name not in self._tools:
            return [f"unknown tool {name!r}"]
        return [f"{e.json_path}: {e.message}" for e in Draft202012Validator(self._tools[name]["schema"]).iter_errors(payload)]

    def bedrock_config(self, names: list[str], *, force: bool = True) -> dict:
        """Converse `toolConfig` for the named tools."""
        return {
            "tools": [{"toolSpec": {"name": n, "description": self._tools[n]["description"],
                                    "inputSchema": {"json": self._tools[n]["schema"]}}} for n in names],
            **({"toolChoice": {"any": {}}} if force else {}),
        }
