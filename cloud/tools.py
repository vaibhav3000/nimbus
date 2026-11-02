"""The tool registry: which tools the cloud agent has, their schemas, and
which step may use which (least privilege). Schemas are generated from the
pack, so a new domain's items, priority classes and extra spec fields appear
in the tool definitions without touching this file.

Tool classes (see context/p3-cloud.md):
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
        spec_item = {"type": "object", "additionalProperties": False,
                     "required": ["item", "destination", "priority", "deadline_minutes"],
                     "properties": {k: v for k, v in props.items() if k != "rationale"}}
        kinds = sorted(pack.restrictions)
        self._tools = {
            "submit_task_spec": {
                "class": "propose",
                "description": ("Record the structured task once every field is known. Call this ONLY when the item, "
                                "its variant and the destination are unambiguous; otherwise call ask_clarification."),
                "schema": {"type": "object", "additionalProperties": False,
                           "required": ["item", "destination", "priority", "deadline_minutes", "rationale"],
                           "properties": props},
            },
            "submit_task_specs": {
                "class": "propose",
                "description": ("Record SEVERAL tasks from one message (for example two items to the same place). Each entry is "
                                "a complete task; use submit_task_spec when there is only one."),
                "schema": {"type": "object", "additionalProperties": False, "required": ["tasks", "rationale"],
                           "properties": {"tasks": {"type": "array", "minItems": 2, "maxItems": 8, "items": spec_item},
                                          "rationale": {"type": "string", "minLength": 1}}},
            },
            "announce_restriction": {
                "class": "propose",
                "description": ("The requester is not asking for a delivery but telling the system a rule about a place "
                                "(for example that it is now restricted in some way, or no longer is). Give the place, the kind of "
                                "restriction, which items to move out of it (only if they ask), and whether it is being lifted."),
                "schema": {"type": "object", "additionalProperties": False, "required": ["zone", "kind", "rationale"],
                           "properties": {"zone": {"type": "string", "minLength": 1}, "kind": {"enum": kinds or ["none"]},
                                          "extract_items": {"type": "array", "items": {"enum": item_ids}},
                                          "lift": {"type": "boolean"},
                                          "rationale": {"type": "string", "minLength": 1}}},
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
            "decide_step": {
                "class": "propose",
                "description": ("C2 only: the edge relay has no model, so it asks the cloud what to do next. Answer with "
                                "proceed, wait, reroute (via zones inside the lease and not blocked) or abort."),
                "schema": {"type": "object", "additionalProperties": False, "required": ["action", "rationale"],
                           "properties": {"action": {"enum": ["proceed", "wait", "reroute", "abort"]},
                                          "via_zones": {"type": "array", "items": {"type": "string"}},
                                          "rationale": {"type": "string", "minLength": 1}}},
            },
            # -- look-ups the model may call while it thinks (class "read": no side effects, pure functions of state)
            "get_fleet": {"class": "read", "description": "Clusters with health, robots (state, zone) and stock.",
                          "schema": {"type": "object", "additionalProperties": False, "properties": {}}},
            "get_stock": {"class": "read", "description": "Stock counts, for one cluster or all.",
                          "schema": {"type": "object", "additionalProperties": False,
                                     "properties": {"cluster_id": {"type": "string"}}}},
            "get_route": {"class": "read", "description": "Cheapest route between two zones, optionally avoiding zones.",
                          "schema": {"type": "object", "additionalProperties": False, "required": ["src", "dst"],
                                     "properties": {"src": {"type": "string"}, "dst": {"type": "string"},
                                                    "avoid": {"type": "array", "items": {"type": "string"}}}}},
            "get_policies": {"class": "read", "description": "Rules relevant to a short query.",
                             "schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                                        "properties": {"query": {"type": "string"}}}},
            "get_task_log": {"class": "read", "description": "What has been decided so far for a task.",
                             "schema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                                        "properties": {"task_id": {"type": "string"}}}},
            "get_map": {"class": "read", "description": "Zones currently closed or blocked, and active restrictions.",
                        "schema": {"type": "object", "additionalProperties": False, "properties": {}}},
            "get_robot": {"class": "read", "description": "One robot: its cluster, state, zone, battery and the task it is running.",
                          "schema": {"type": "object", "additionalProperties": False, "required": ["robot_id"],
                                     "properties": {"robot_id": {"type": "string"}}}},
            "get_task": {"class": "read", "description": "One task: status, what was asked, the lease it holds, its legs.",
                         "schema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                                    "properties": {"task_id": {"type": "string"}}}},
            "eta": {"class": "read", "description": "Distance and travel time between two zones at a speed (m/s).",
                    "schema": {"type": "object", "additionalProperties": False, "required": ["src", "dst"],
                               "properties": {"src": {"type": "string"}, "dst": {"type": "string"}, "speed": {"type": "number", "exclusiveMinimum": 0},
                                              "avoid": {"type": "array", "items": {"type": "string"}}}}},
            # -- terminal choices after an escalation (validated by code before anything happens)
            "escalate_operator": {"class": "propose", "description": "Nothing the cloud can do fixes this: tell the operator, in one plain sentence.",
                                  "schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                                             "properties": {"text": {"type": "string", "minLength": 1}, "rationale": {"type": "string"}}}},
            "wait_and_retry": {"class": "propose", "description": "The blockage may clear by itself: look again after this many seconds instead of re-planning now.",
                               "schema": {"type": "object", "additionalProperties": False, "required": ["seconds", "rationale"],
                                          "properties": {"seconds": {"type": "integer", "minimum": 10, "maximum": 600},
                                                         "rationale": {"type": "string", "minLength": 1}}}},
            "replan": {"class": "propose", "description": "Issue a new lease version for the same cluster that avoids these zones.",
                       "schema": {"type": "object", "additionalProperties": False, "required": ["avoid_zones", "rationale"],
                                  "properties": {"avoid_zones": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                                 "rationale": {"type": "string", "minLength": 1}}}},
            "hand_over": {"class": "propose", "description": "Give the rest of the task to another cluster.",
                          "schema": {"type": "object", "additionalProperties": False, "required": ["to_cluster", "rationale"],
                                     "properties": {"to_cluster": {"type": "string", "minLength": 1},
                                                    "rationale": {"type": "string", "minLength": 1}}}},
            "answer_clarification": {"class": "propose", "description": "Answer the edge's question from the rules or the context.",
                                     "schema": {"type": "object", "additionalProperties": False, "required": ["answer", "rationale"],
                                                "properties": {"answer": {"type": "string", "minLength": 1},
                                                               "rationale": {"type": "string", "minLength": 1}}}},
            "ask_requester": {"class": "propose", "description": "The cloud cannot answer: ask the person who made the request.",
                              "schema": {"type": "object", "additionalProperties": False, "required": ["question", "possible_readings"],
                                         "properties": {"question": {"type": "string", "minLength": 1},
                                                        "possible_readings": {"type": "array", "minItems": 1,
                                                                              "items": {"type": "string", "minLength": 1}}}}},
            "no_action": {"class": "propose", "description": "Nothing needs to change.",
                          "schema": {"type": "object", "additionalProperties": False, "required": ["rationale"],
                                     "properties": {"rationale": {"type": "string", "minLength": 1}}}},
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
