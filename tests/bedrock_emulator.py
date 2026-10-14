"""A stand-in for the bedrock-runtime client that answers *only from the
request it receives* (system prompt text, messages, tool list), exactly the
information a real model gets. If the context pack omitted something a model
needs to allocate, this emulator could not answer, so tests built on it also
check the prompt's completeness."""

import json
import re

from cloud.llm import RuleBasedLLM
from cloud.pack import Pack
from cloud.topology import Topology

pack = Pack.load()
topo = Topology(pack)
FLEET_LINE = re.compile(r"^\s+(\S+) area=(\S+) health=(\S+) robots\[(.*?)\] stock\[(.*?)\]", re.M)


def _reply(name, payload, tin, tout):
    return {"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "emu", "name": name, "input": payload}}]}},
        "usage": {"inputTokens": tin, "outputTokens": tout}, "stopReason": "tool_use"}


class EmulatedBedrockRuntime:
    def __init__(self):
        self.requests = []
        self.rules = RuleBasedLLM(pack, topo)

    def converse(self, **kw):
        self.requests.append(kw)
        system = kw["system"][0]["text"]
        messages = [{"role": m["role"], "text": m["content"][0]["text"]} for m in kw["messages"]]
        tools = [t["toolSpec"]["name"] for t in kw["toolConfig"]["tools"]]
        tin = (len(system) + sum(len(m["text"]) for m in messages)) // 4
        if "propose_allocation" in tools:
            spec = json.loads(messages[-1]["text"].split("TASK_SPEC:", 1)[1].splitlines()[0])
            ward = topo.anchor_of(spec["destination"])
            for cid, area, health, robots, stock in FLEET_LINE.findall(system):
                free = [r.split("=")[0] for r in robots.split(", ") if r.endswith("=idle")]
                have = dict(kv.split("=") for kv in stock.split(", ") if kv)
                if (ward in topo.cluster_zones(cid) and health in ("healthy", "unknown")
                        and int(have.get(spec["item"], 0)) > 0 and free):
                    return _reply("propose_allocation", {"cluster_id": cid, "robot_ids": [free[0]],
                                                         "rationale": f"{free[0]} is idle in {cid}"}, tin, 30)
            return _reply("propose_allocation", {"cluster_id": "none", "robot_ids": ["none"], "rationale": "nothing fits"}, tin, 30)
        call = self.rules._intent(messages)
        return _reply(call["name"], call["input"], tin, 80)
