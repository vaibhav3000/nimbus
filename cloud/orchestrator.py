"""The cloud orchestrator: request text in, validated lease out (FR-13..FR-18).

Order of every decision (PRD FR-15, "the LLM proposes, a rule checker verifies"):

  health check -> context pack -> LLM intent (task spec | clarification)
  -> LLM allocation proposal -> Rule Checker -> conditional lease commit

If the model is down, throttled, or answers badly twice, the same pipeline
continues on the deterministic path (form parser + nearest-robot allocator)
with a reduced-autonomy notice, and no lease comes from the LLM path during the
outage (FR-18). Mode C3 uses only that path. Everything it decides is returned
as decision-log rows; the HTTP layer writes them, so the log stays the single
record (FR-20). Rows carry no timing, so a replay is byte-identical (NFR-6).

Domain knowledge (items, locations, priorities, rules, templates, prompts) is
all in the Domain Pack; nothing in this file knows what is being delivered.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

import contracts

from . import leasebuilder, nlp
from .allocator import FleetView, eligible_options, needs_stock
from .checker import Proposal, check
from .clock import Clock, iso, parse_iso
from .context import Context
from .fleet import FleetRegistry
from .mapstate import MapState
from .memory import Memory
from .notify import Notifier
from .trace import Trace, TracingLLM, task_scope
from .orchestrator_types import LLM_MODES, Outcome, row as _row
from .react import Reactor
from .results import ResultStore
from .statements import Statements
from .ticker import Ticker
from .legs import handover_options, make_legs
from .leases import Conflict, MemoryLeaseRepo, _task_of, claim_keys_for
from .llm import CircuitBreakerLLM, LLMError, RuleBasedLLM
from .pack import Pack
from .policy import PolicyBook
from .scheduler import Scheduler
from .tools import ToolRegistry
from .topology import Topology

RENEW_TTL_S = 180        # a renewal extends authority to now + 3 min
MAX_OVERRUN_S = 600      # but never beyond deadline + 10 min; past that the cloud must re-decide
REDUCED_AUTONOMY = "Reduced autonomy: the cloud model is unavailable, so deterministic rules are deciding."


class InvalidLeaseError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class StaleVersion(Exception):
    def __init__(self, current: int):
        super().__init__(f"current lease version is {current}")
        self.current = current


def _with_kb(book: PolicyBook) -> PolicyBook:
    from .retrieval import BedrockKBRetriever
    book.retriever = BedrockKBRetriever.from_env()      # None unless NIMBUS_KB_ID is set
    return book


def _sha(lease: dict) -> str:
    return hashlib.sha256(json.dumps(lease, sort_keys=True).encode()).hexdigest()


def _public(info: dict) -> dict:
    """Model-call facts that are deterministic and so may enter the log."""
    return {k: v for k, v in info.items() if not k.startswith("_")}


class Orchestrator:
    def __init__(self, *, pack: Pack, topo: Topology, fleet: FleetRegistry, policies: PolicyBook, repo,
                 scheduler: Scheduler, llm, clock: Clock, mode: str = "C1",
                 allow_unknown: bool = True, publish=None, probe_timeout_s: float = 0.0, durable=None, results=None):
        self.pack, self.topo, self.fleet, self.policies, self.repo = pack, topo, fleet, policies, repo
        self.scheduler, self.llm, self.clock = scheduler, llm, clock
        self.tools = ToolRegistry(pack)
        self.durable = durable
        self.mapstate = MapState(topo, durable)
        self.notifier = Notifier(clock, durable)
        self.memory = Memory(durable)
        if durable is not None:
            fleet.status_sink = durable.put_robot
        self.trace = Trace()
        if not isinstance(llm, TracingLLM):
            self.llm = TracingLLM(llm, self.trace)
        self._freed = 0          # robots/claims given back by this process (finish, cancel, hold, expiry)
        self.statements = Statements(self)
        self.ticker = Ticker(self)
        self.results = results if results is not None else ResultStore()
        self.load_task = lambda task_id: None    # the HTTP layer plugs in its task store
        self.waits: dict[str, dict] = {}         # task_id -> {"until", "payload", "version"}: "look again later" from the model
        self.react = Reactor(self)
        self.mode, self.allow_unknown = mode, allow_unknown
        self.publish = publish or (lambda topic, payload: None)
        self.probe_timeout_s = probe_timeout_s

    @classmethod
    def from_env(cls, data_dir: Path, publish=None, clock: Clock | None = None) -> "Orchestrator":
        clock = clock or Clock()
        pack = Pack.load()
        topo = Topology(pack)
        tools = ToolRegistry(pack)
        if os.environ.get("NIMBUS_LLM", "fake") == "bedrock":
            from .llm import BedrockLLM
            llm = CircuitBreakerLLM(
                BedrockLLM(os.environ["NIMBUS_BEDROCK_MODEL"], tools, os.environ.get("AWS_REGION", "ap-south-1")), clock)
        else:
            llm = RuleBasedLLM(pack, topo)
        if os.environ.get("NIMBUS_CASSETTE"):  # record real calls once, replay them free
            from .llm import CassetteLLM
            mode = os.environ.get("NIMBUS_CASSETTE_MODE", "replay")
            llm = CassetteLLM(llm if mode == "record" else None, Path(os.environ["NIMBUS_CASSETTE"]), mode)
        stock_store = None
        if os.environ.get("NIMBUS_BACKEND", "local") == "aws":
            from .aws_dynamo import DynamoLeaseRepo, DynamoStockStore
            from .durable import DynamoDurable
            repo = DynamoLeaseRepo.from_env()
            stock_store = DynamoStockStore.from_env()
            durable = DynamoDurable.from_env()
        else:
            from .durable import FileDurable
            repo = MemoryLeaseRepo(Path(data_dir) / "claims.json")
            durable = FileDurable(Path(data_dir) / "durable.json")
        return cls(pack=pack, topo=topo, fleet=FleetRegistry.load(clock, pack, stock_store=stock_store),
                   policies=_with_kb(PolicyBook(pack, durable=durable)), repo=repo, durable=durable,
                   results=ResultStore.from_env(data_dir),
                   scheduler=Scheduler(pack.ranks), llm=llm, clock=clock,
                   mode=os.environ.get("NIMBUS_MODE", "C1"),
                   allow_unknown=os.environ.get("NIMBUS_ALLOW_UNKNOWN", "1") == "1",
                   publish=publish, probe_timeout_s=float(os.environ.get("NIMBUS_PROBE_TIMEOUT", "0")))

    # -- context + health check --------------------------------------------------

    def epoch(self) -> int:
        """A number that changes whenever something a queued task could be waiting on changes: a robot freed, a
        cluster's health, robots or stock, a restriction or a closure. A queued task is retried only when it moved."""
        return self._freed + self.fleet.version + self.policies.version + self.mapstate.version

    def view(self, task_id: str | None = None, extra_forbidden: dict | None = None, exclude=()) -> FleetView:
        return FleetView(self.topo, self.fleet, self.repo, self.policies, self.clock, self.pack,
                         self.allow_unknown, task_id, extra_forbidden or {}, self.mapstate, frozenset(exclude))

    def health_check(self) -> list[str]:
        """Quick pre-delegation check: probe clusters that were alive and went
        quiet. Never-seen clusters (no edge running) are not probed."""
        probed = []
        for cid in sorted(self.fleet.clusters):
            if self.fleet.health(cid) == "stale":
                self.publish(f"nimbus/{cid}/probe", {
                    "type": "PROBE", "cluster_id": cid,
                    "message_id": f"probe-{cid}-{int(self.clock.now().timestamp())}",
                    "ts": iso(self.clock.now())})
                probed.append(cid)
        deadline = time.monotonic() + self.probe_timeout_s
        while probed and time.monotonic() < deadline:
            if all(self.fleet.health(c) == "healthy" for c in probed):
                break
            time.sleep(0.05)
        return probed

    def build_context(self, text: str, task_id: str | None, probes: list[str], history: list | None = None) -> Context:
        loc = self.topo.locate(text)
        # Boolean attributes of the named place (e.g. a flag the rules care about) steer retrieval.
        flags = tuple(k for k, v in self.topo.facts_for(loc["zone"]).items() if v is True) if loc else ()
        view = self.view(task_id)
        return Context.build(
            self.pack, self.topo, now=iso(self.clock.now()), mode=self.mode, fleet=self.fleet.snapshot(),
            policies=self.policies.retrieve(text, extra_terms=flags), restrictions=self.policies.restrictions(),
            busy_robots=view.busy_robots(), probes_sent=probes, history=history,
            memory=self.memory.lines_for(loc["anchor"] if loc else None))

    # -- entry: a new request or an answer -------------------------------------

    def handle_request(self, task: dict, text: str, *, question: dict | None = None,
                       answer: str | None = None, history: list | None = None) -> Outcome:
        probes = self.health_check()
        ctx = self.build_context(text + (" " + answer if answer else ""), task["task_id"], probes, history)
        info: dict = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0}
        notice = None
        use_llm = self.mode in LLM_MODES
        parsed = None
        if use_llm:
            try:
                parsed = self._llm_intent(text, question, answer, ctx, info)
            except LLMError as exc:
                notice, use_llm = REDUCED_AUTONOMY, False
                info["fallback_reason"] = str(exc)
        if parsed is None:
            parsed = self._form_intent(text, answer)
        kind, payload = parsed
        if kind == "clarify" and answer is None and (alt := self._memory_spec(text)):
            kind, payload = "spec", alt        # this place has confirmed its usual choice: do not ask again (the checker still runs)
        if kind == "clarify":
            return self._clarify_outcome(payload, text, ctx, info, notice)
        if kind == "statement":
            out = self.statements.apply(task, {**payload, "zone": self.topo.anchor_of(payload["zone"])}, text)
            out.notice = f"{notice} {out.notice}" if notice else out.notice
            return out
        specs = [self._complete_spec(self._ground(s, text, answer)) for s in (payload if kind == "specs" else [payload])]
        out = self._allocate(task, specs[0], ctx, info, use_llm, notice, request_text=text, answer=answer)
        if out.kind != "clarify":   # after a question the whole message is read again, so extras would be duplicated
            out.children = specs[1:]
        if out.kind == "leased":
            self._remember(specs[0])
        return out

    def _memory_spec(self, text: str) -> dict | None:
        """A complete request that would be clarified, for a place whose usual variant is remembered: the spec with it."""
        p = nlp.parse(self.pack, self.topo, text)
        if p.missing or p.fields.get("variant_confirmed_by_requester"):
            return None
        usual = self.memory.usual(p.destination["anchor"], p.item)
        if not usual:
            return None
        spec = nlp.spec_from(p, note="usual choice remembered for this place")
        spec["item"] = usual
        spec["constraints"] = [f"usual choice for this place: {usual}"]
        return spec

    def _remember(self, spec: dict) -> None:
        """A request was leased: note which variant this place got (the base item counts as "no variant")."""
        item = self.pack.item(spec["item"]) or {}
        base = item.get("variant_of") or spec["item"]
        self.memory.record(self.topo.anchor_of(spec["destination"]) or spec["destination"], base, spec["item"])

    def _ground(self, spec: dict, text: str, answer: str | None) -> dict:
        """Check the model's task against the words the requester used. The model may misread or drop a detail
        the text states plainly, so code restores it: a named source, an urgent wording, an unstated deadline
        (which takes the priority's default, not whatever number the model liked) or a stated one."""
        spec = dict(spec)
        p = nlp.parse(self.pack, self.topo, text, answer=answer)
        if p.source and not spec.get("source"):
            spec["source"] = p.source["zone"]
        default = self.pack.doc["default_priority"]
        if p.priority != default and spec["priority"] == default:
            spec["priority"] = p.priority
        usual = self.memory.usual(self.topo.anchor_of(spec["destination"]) or spec["destination"], spec["item"])
        if usual and not p.fields.get("variant_confirmed_by_requester"):   # not an explicit "the standard one": use the habit
            spec["item"] = usual
            spec["constraints"] = [*spec.get("constraints", []), f"usual choice for this place: {usual}"]
        stated = p.deadline_minutes != self.pack.priority(p.priority)["default_deadline_minutes"]
        spec["deadline_minutes"] = p.deadline_minutes if stated else self.pack.priority(spec["priority"])["default_deadline_minutes"]
        return spec

    def _complete_spec(self, spec: dict) -> dict:
        spec = dict(spec)
        spec["destination_anchor"] = self.topo.anchor_of(spec["destination"])
        spec["requested_at"] = iso(self.clock.now())
        return spec

    def allocate_spec(self, task: dict, spec: dict) -> Outcome:
        """Allocate a task made by another message (an extra item, an extraction): same pipeline, no new intent step."""
        spec = self._complete_spec(spec)
        ctx = self.build_context(task["request_text"], task["task_id"], [])
        info = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0}
        return self._allocate(task, spec, ctx, info, self.mode in LLM_MODES, None, request_text=task["request_text"], answer=None)

    def complete_summary(self, task: dict, result_ref: str) -> dict:
        """The edge reported the last result of a task (OUTPUT_READY): look at what it stored and tell the requester in plain
        words (PRD Table 2: "summarise for the user"). The model writes the sentence only when the result has text worth
        reading; otherwise a template from the facts is used, so a photo does not cost a model call. Returns
        {text, info, rows, calls, tokens_in, tokens_out}."""
        info = self.results.describe(result_ref)
        spec = task.get("spec") or {}
        what = f"{spec.get('item', 'the item')} {('from ' + spec['source'] + ' ') if spec.get('source') else ''}to {spec.get('destination', 'its destination')}"
        facts = f"{what.strip()} for task {task['task_id']}."
        calls = tin = tout = 0
        text, source = f"Done: {facts}" + (f" The result ({info['size']} bytes) is stored for review." if info.get("readable") else ""), "template"
        if self.mode in LLM_MODES and info.get("preview"):
            ctx = self.build_context(task["request_text"], task["task_id"], [])
            try:
                res = self.llm.converse(system=self.pack.prompts["result"], tools=self.tools.names("result"), tool_choice="any",
                                        messages=[{"role": "user", "text": f"TASK: {facts}\nRESULT: {info['preview'][:1500]}"}], context=ctx)
                calls, tin, tout = 1, res.input_tokens, res.output_tokens
                call = res.tool_calls[0] if res.tool_calls else None
                if call and not self.tools.validate(call["name"], call["input"]):
                    text, source = call["input"]["summary"], "llm"
            except LLMError:
                pass
        rows = [_row("cloud" if source == "llm" else "rule", {"result_ref": result_ref, "readable": info.get("readable", False)},
                     {"decision": "summarise_result", "source": source, "summary": text}, (task.get("lease") or {}).get("version"))]
        return {"text": text, "info": info, "rows": rows, "calls": calls, "tokens_in": tin, "tokens_out": tout}

    def expire(self, task: dict, reason: str) -> Outcome:
        """The lease ran out with no completion and no renewal: the claim ends and the task goes back in the queue
        (same mechanics as a hold: stop lease out, claims freed, the reserved item returned)."""
        out = self.hold(task, reason)
        if out.kind == "held":
            out.rows.insert(0, _row("cloud", {"reason": reason}, {"decision": "lease_expired", "reason": reason}, out.lease["version"]))
        return out

    def reconcile(self, cluster_id: str, held: list[dict]) -> list[dict]:
        """After an outage the edge says which leases it still holds (FR-12). Reply, per lease: ok; replaced (here is
        the current version, re-sent); or stop (the task is finished, cancelled or lives on another cluster, and a
        stop lease with a higher version is sent so a returning robot parks). No robot ends up under two leases."""
        out = []
        for h in held:
            lid, ver = h.get("lease_id", ""), h.get("version")
            try:
                tid = lid[len("lease-"):lid.rindex("-v")]
            except ValueError:
                out.append({"lease_id": lid, "action": "unknown"}); continue
            cur = self.repo.current(tid)
            if cur and cur["cluster_id"] == cluster_id and cur["version"] == ver:
                out.append({"lease_id": lid, "action": "ok"})
            elif cur and cur["cluster_id"] == cluster_id and isinstance(ver, int) and cur["version"] > ver:
                self.publish(f"nimbus/{cluster_id}/lease", cur)
                out.append({"lease_id": lid, "action": "replaced", "current": cur["version"]})
            else:
                last = self.repo.last_lease(tid)
                if last is None:
                    out.append({"lease_id": lid, "action": "unknown"}); continue
                version = max((ver or 0) + 1, (cur or last)["version"] if cur else last["version"] + 1)
                zone = next((r.get("zone") for r in self.fleet.clusters[cluster_id]["robots"].values()
                             if r.get("zone") in self.topo.nodes), last["allowed_zones"][0])
                stop = self._stop_lease({**last, "cluster_id": cluster_id}, version, zone)
                self.publish(f"nimbus/{cluster_id}/lease", stop)
                out.append({"lease_id": lid, "action": "stop", "version": version})
        return out

    def hold(self, task: dict, reason: str) -> Outcome:
        """Stop a task that a new restriction blocks and queue it until the restriction is lifted: stop lease out,
        claims freed, the reserved item returned, the task back in the scheduler."""
        stop = self.stand_down(task, reason)
        if stop.kind != "stood_down":
            return stop
        self._freed += 1
        self.repo.free_claims(task["task_id"])
        res = task.get("reservation")
        if res and not task.get("stock_released") and not task.get("stock_settled"):
            self.fleet.return_stock(res["cluster"], res["item"])
            task["stock_released"] = True
        spec = self.current_spec(task)
        self.scheduler.push(task["task_id"], spec["priority"],
                            parse_iso(spec["requested_at"]) + timedelta(minutes=spec["deadline_minutes"]))
        stop.kind, stop.reason = "held", reason
        return stop

    def retry_queued(self, task: dict) -> Outcome:
        ctx = self.build_context(task["request_text"], task["task_id"], [])
        return self._allocate(task, self.current_spec(task), ctx,
                              {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0},
                              self.mode in LLM_MODES, None, request_text=task["request_text"], answer=None, retry=True)

    # -- intent -------------------------------------------------------------------

    def _spec_errors(self, call: dict) -> list[str]:
        errs = self.tools.validate(call["name"], call["input"])
        if errs:
            return errs
        if call["name"] == "announce_restriction":
            if self.topo.anchor_of(call["input"]["zone"]) is None:
                errs.append(f"zone {call['input']['zone']!r} is not a place in the context")
            return errs
        for i in (call["input"]["tasks"] if call["name"] == "submit_task_specs" else
                  [call["input"]] if call["name"] == "submit_task_spec" else []):
            if not self.topo.is_destination(i["destination"]):
                errs.append(f"destination {i['destination']!r} is not a destination in the context")
            if i.get("source") and self.topo.anchor_of(i["source"]) is None:
                errs.append(f"source {i['source']!r} is not a zone in the context")
        return errs

    def _llm_intent(self, text, question, answer, ctx, info):
        messages = [{"role": "user", "text": text}]
        if question and answer:
            messages += [{"role": "assistant", "text": question["question"]}, {"role": "user", "text": answer}]
        system = self.pack.prompts["intent"] + "\n\n" + ctx.to_prompt()
        feedback = None
        for _ in range(2):
            msgs = list(messages)
            if feedback:
                msgs.append({"role": "user", "text": f"Your last tool call was invalid ({feedback}). Call a tool again, correctly."})
            res = self.llm.converse(system=system, messages=msgs, tools=self.tools.names("intent"),
                                    tool_choice="any", context=ctx)
            self._tally(info, res)
            call = res.tool_calls[0] if res.tool_calls else None
            errs = ["no tool call"] if call is None else self._spec_errors(call)
            if not errs:
                return self._intent_kind(call)
            feedback = "; ".join(errs)
            info["retries"] = info.get("retries", 0) + 1
        raise LLMError(f"invalid tool output twice: {feedback}")

    @staticmethod
    def _intent_kind(call):
        name, i = call["name"], call["input"]
        if name == "ask_clarification":
            return "clarify", i
        if name == "announce_restriction":
            return "statement", {"zone": call["input"]["zone"], "kind": i["kind"], "lift": bool(i.get("lift")),
                                 "extract_items": i.get("extract_items", [])}
        if name == "submit_task_specs":
            return "specs", i["tasks"]
        return "spec", i

    def _form_intent(self, text, answer):
        st = nlp.parse_statement(self.pack, self.topo, text)
        if st is not None:
            if "unknown_zone" in st:
                nouns = self.pack.doc["nouns"]
                return "clarify", {"question": f"Which {nouns['destination']} do you mean by {st['unknown_zone']!r}?",
                                   "possible_readings": sorted(self.topo.destinations())[:6]}
            return "statement", st
        p = nlp.parse(self.pack, self.topo, text, answer=answer)
        if p.missing:
            return "clarify", nlp.missing_question(self.pack, p)
        several = nlp.split_items(self.pack, self.topo, text, p)
        if several:
            return "specs", several
        return "spec", nlp.spec_from(p, note="form-style deterministic parse")

    @staticmethod
    def _tally(info, res):
        info["calls"] += 1
        info["input_tokens"] += res.input_tokens
        info["output_tokens"] += res.output_tokens
        info["model"] = res.model_id
        info["_latency_ms"] += res.latency_ms
        info["tokens_estimated"] = info.get("tokens_estimated", False) or res.estimated

    def _clarify_outcome(self, q, text, ctx, info, notice) -> Outcome:
        question = {"question": q["question"], "possible_readings": q["possible_readings"]}
        return Outcome(
            "clarify", question=question, notice=notice,
            rows=[_row("cloud", {"request_text": text, "context": ctx.digest()},
                       {"decision": "ask_clarify", "question": question,
                        "missing_fields": q.get("missing_fields", []), **({"rule": q["rule"]} if q.get("rule") else {}),
                        "llm": _public(info)})],
            tokens_in=info["input_tokens"], tokens_out=info["output_tokens"], llm_calls=info["calls"],
            timings={"llm_ms": info["_latency_ms"]})

    # -- allocation + check + commit ----------------------------------------------

    def _llm_proposal(self, spec, ctx, view, feedback, info, options=None) -> Proposal:
        text = "TASK_SPEC: " + json.dumps(spec, sort_keys=True)
        if options:   # the code has already ranked who could do this: the model chooses among them, it does not search
            text += "\nCANDIDATES (nearest first): " + json.dumps(
                [{"cluster_id": o.cluster_id, "robot_id": o.robot_id, "distance_m": round(o.distance, 1)} for o in options[:8]])
        if feedback:
            text += "\nYour previous proposal was rejected: " + feedback
        res = self.llm.converse(system=self.pack.prompts["allocation"] + "\n\n" + ctx.to_prompt(),
                                messages=[{"role": "user", "text": text}], tools=self.tools.names("allocation"),
                                tool_choice="any", context=ctx, fleet_view=view)
        self._tally(info, res)
        call = res.tool_calls[0] if res.tool_calls else None
        errs = ["no tool call"] if call is None else self.tools.validate(call["name"], call["input"])
        if errs:
            raise LLMError("; ".join(errs))
        i = call["input"]
        return Proposal(i["cluster_id"], i["robot_ids"], i["rationale"], "llm")

    def _candidates(self, spec, ctx, view, use_llm, info, attempts, state):
        """Yield proposals in preference order. The code ranks who is eligible first. With nobody eligible there is
        nothing to propose (no model call). With one eligible cluster the choice is made (nearest free robot) and
        the model is not asked. Only when two or more clusters could take the task does the model choose among
        the ranked candidates (twice at most, the second time with the checker's reasons); then the ranking."""
        options, excluded = eligible_options(spec, view)
        state["excluded"] = [e.__dict__ for e in excluded]
        if not options:
            return
        clusters = list(dict.fromkeys(o.cluster_id for o in options))
        if use_llm and len(clusters) >= 2:
            feedback = None
            for _ in range(2):
                try:
                    prop = self._llm_proposal(spec, ctx, view, feedback, info, options)
                except LLMError as exc:
                    attempts.append({"source": "llm", "error": str(exc)})
                    state["notice"] = REDUCED_AUTONOMY
                    break
                state["feedback"] = None
                yield prop
                feedback = state.get("feedback")
                if not feedback:
                    return
        why = "nearest free robot" if len(clusters) >= 2 else f"only {clusters[0]} can take it; nearest free robot"
        for opt in options:
            yield Proposal(opt.cluster_id, [opt.robot_id], why, "classical")

    def _allocate(self, task, spec, ctx, info, use_llm, notice, *, request_text, answer, retry=False,
                  exclude=()) -> Outcome:
        task_id = task["task_id"]
        view = self.view(task_id, exclude=exclude)
        attempts: list = []
        state: dict = {"notice": notice}
        committed = None
        uses_stock = needs_stock(spec, self.pack)
        # A lost race (robot or stock taken between ranking and commit) is not a
        # reason to reject: re-rank against the new state, a few times, then wait.
        for round_no in range(3):
            state["conflict"] = False
            for prop in self._candidates(spec, ctx, view, use_llm and round_no == 0, info, attempts, state):
                res = check(spec, prop, view)
                attempts.append({"source": prop.source, "cluster": prop.cluster_id, "robots": prop.robot_ids,
                                 "ok": res.ok, "violations": res.violations})
                clarify = next((v for v in res.violations if v["kind"] == "clarify"), None)
                if clarify:
                    return self._clarify_outcome(clarify["question"], request_text, ctx, info, state["notice"])
                if not res.ok:
                    state["feedback"] = "; ".join(f"{v['rule']}: {v['detail']}" for v in res.violations)
                    continue
                live = self.repo.current(task_id)   # a later leg or a re-assignment is the next version of the same task
                lease = leasebuilder.build_lease(task_id, spec, prop, res, topo=self.topo, clock=self.clock,
                                                 pack=self.pack, restricted_zones=list(view.restricted_for(spec["priority"])),
                                                 version=live["version"] + 1 if live else 1)
                errors = contracts.validate_document("lease", lease)
                if errors:
                    raise InvalidLeaseError(errors)
                if uses_stock and not self.fleet.reserve_stock(prop.cluster_id, spec["item"]):
                    attempts[-1]["conflict"] = "stock taken concurrently"
                    state["feedback"], state["conflict"] = "stock taken concurrently", True
                    continue
                try:
                    self.repo.commit(lease, claim_keys_for(lease, self.topo.exclusive_zones()), now=self.clock.now(),
                                     stock=(prop.cluster_id, spec["item"]) if uses_stock and self.fleet.stock_store else None)
                except Conflict as exc:
                    if uses_stock:
                        self.fleet.release_stock(prop.cluster_id, spec["item"])
                        self.fleet.sync_stock()   # the durable count may be lower than our copy
                    attempts[-1]["conflict"] = exc.reasons
                    state["feedback"], state["conflict"] = "; ".join(exc.reasons), True
                    continue
                except Exception:   # a throttle or network error must not leak the reserved item
                    if uses_stock:
                        self.fleet.release_stock(prop.cluster_id, spec["item"])
                    raise
                committed = (prop, res, lease)
                break
            if committed or not state["conflict"]:
                break

        if committed is None:
            ex = state.get("excluded", [])
            restricted = view.restricted_for(spec["priority"])
            bound = next((z for z in (self.topo.anchor_of(spec["destination"]),
                                      self.topo.anchor_of(spec["source"]) if spec.get("source") else None) if z in restricted), None)
            if bound and not spec.get("leg"):   # the real blocker is a restriction, not the clusters' areas: say so, and wait if it is a hold
                r = restricted[bound]
                state["excluded"] = [{"cluster_id": "-", "reason": f"{bound} is {r['effect']} by {r['reason']}",
                                      "transient": r["effect"] == "hold"}]
            # legs only when no single cluster covers the whole job; a busy or out-of-stock cluster just means wait
            elif not spec.get("leg") and ex and all("hand-over" in e["reason"] for e in ex):
                out = self._try_handover(task, spec, ctx, info, use_llm, state["notice"], request_text, answer, retry, exclude)
                if out is not None:
                    return out
            return self._no_lease(task, spec, attempts, state, ctx, info, request_text, retry)

        prop, res, lease = committed
        self.scheduler.remove(task_id)
        pv = {"cluster": lease["cluster_id"], "robots": lease["assigned_robots"], "zones": lease["allowed_zones"]}
        rows = [
            _row("cloud", {"request_text": request_text, **({"answer": answer} if answer else {}),
                           "context": ctx.digest()},
                 {"decision": "propose_lease", **pv, "spec": spec, "proposed_by": prop.source,
                  "rationale": prop.rationale, "attempts": attempts, "llm": _public(info),
                  **({"notice": state["notice"]} if state["notice"] else {})}),
            _row("rule", {"proposal": pv}, {"decision": "approve", "checks": res.checks}, lease["version"]),
            _row("cloud", {"lease_id": lease["lease_id"]},
                 {"decision": "issue_lease", "lease_id": lease["lease_id"], "sha256": _sha(lease)}, lease["version"]),
        ]
        return Outcome("leased", rows=rows, lease=lease, spec=spec, notice=state["notice"],
                       publish=[(f"nimbus/{lease['cluster_id']}/lease", lease)],
                       tokens_in=info["input_tokens"], tokens_out=info["output_tokens"], llm_calls=info["calls"],
                       reservation={"cluster": lease["cluster_id"], "item": spec["item"]} if uses_stock else None,
                       timings={"llm_ms": info["_latency_ms"]})

    def _no_lease(self, task, spec, attempts, state, ctx, info, request_text, retry) -> Outcome:
        excluded = state.get("excluded", [])
        extra = {"tokens_in": info["input_tokens"], "tokens_out": info["output_tokens"], "llm_calls": info["calls"],
                 "timings": {"llm_ms": info["_latency_ms"]}}
        if state.get("conflict"):
            excluded = excluded + [{"cluster_id": "-", "reason": "capacity contended by concurrent requests", "transient": True}]
        if any(e["transient"] for e in excluded):
            deadline = parse_iso(spec["requested_at"]) + timedelta(minutes=spec["deadline_minutes"])
            self.scheduler.push(task["task_id"], spec["priority"], deadline)
            reason = "; ".join(f"{e['cluster_id']}: {e['reason']}" for e in excluded if e["transient"])   # what waiting can fix
            rows = [] if retry else [_row("cloud", {"request_text": request_text, "context": ctx.digest()},
                                          {"decision": "queue", "reason": reason, "excluded": excluded,
                                           "attempts": attempts, "llm": _public(info)})]
            return Outcome("queued", rows=rows, spec=spec, reason=reason, notice=state["notice"], **extra)
        vetoes = [v for a in attempts for v in a.get("violations", [])]
        why = ("; ".join(f"{e['cluster_id']}: {e['reason']}" for e in excluded)
               or "; ".join(f"{v['rule']}: {v['detail']}" for v in vetoes) or "no admissible allocation")
        rows = [_row("cloud", {"request_text": request_text, "context": ctx.digest()},
                     {"decision": "no_lease", "attempts": attempts, "excluded": excluded, "llm": _public(info)}),
                _row("rule", {"spec": spec}, {"decision": "reject", "reason": why})]
        return Outcome("rejected", rows=rows, spec=spec, reason=why, notice=state["notice"], **extra)

    # -- re-planning: lease v2 (FR-16) ---------------------------------------------

    def replan(self, task: dict, *, blocked_zones: list[str], reason: str) -> Outcome:
        """Issue lease v(n+1) for a live task that avoids newly blocked zones.

        Same cluster and robot (cross-cluster hand-over is the next slice);
        the old holder stays bounded by its previous expiry until it hears of v+1.
        """
        task_id = task["task_id"]
        cur = self.repo.current(task_id)
        if cur is None:
            return Outcome("replan_failed", reason="no live lease to replace",
                           rows=[_row("cloud", {"reason": reason}, {"decision": "replan_failed", "why": "no live lease"})])
        spec = task["spec"]
        view = self.view(task_id, {z: f"blocked: {reason}" for z in blocked_zones})
        prop = Proposal(cur["cluster_id"], list(cur["assigned_robots"]), f"re-plan: {reason}", "classical")
        res = check(spec, prop, view, holds_stock=True)
        if not res.ok:
            why = "; ".join(f"{v['rule']}: {v['detail']}" for v in res.violations)
            return Outcome("replan_failed", reason=why, spec=spec,
                           rows=[_row("cloud", {"reason": reason, "blocked_zones": blocked_zones},
                                      {"decision": "replan_failed", "why": why}, cur["version"])])
        lease = leasebuilder.build_lease(task_id, spec, prop, res, topo=self.topo, clock=self.clock, pack=self.pack,
                                         version=cur["version"] + 1,
                                         restricted_zones=list(view.restricted_for(spec["priority"])))
        errors = contracts.validate_document("lease", lease)
        if errors:
            raise InvalidLeaseError(errors)
        try:
            self.repo.commit(lease, claim_keys_for(lease, self.topo.exclusive_zones()), now=self.clock.now())
        except Conflict as exc:   # the version moved or a claim was taken meanwhile: the caller answers 409
            why = "; ".join(exc.reasons)
            return Outcome("replan_failed", reason=why, spec=spec,
                           rows=[_row("cloud", {"reason": reason, "blocked_zones": blocked_zones},
                                      {"decision": "replan_failed", "why": why}, cur["version"])])
        pv = {"cluster": lease["cluster_id"], "robots": lease["assigned_robots"], "zones": lease["allowed_zones"]}
        rows = [
            _row("cloud", {"reason": reason, "blocked_zones": blocked_zones, "replaces_version": cur["version"]},
                 {"decision": "replan", **pv}),
            _row("rule", {"proposal": pv}, {"decision": "approve", "checks": res.checks}, lease["version"]),
            _row("cloud", {"lease_id": lease["lease_id"]},
                 {"decision": "issue_lease", "lease_id": lease["lease_id"], "sha256": _sha(lease)}, lease["version"]),
        ]
        return Outcome("leased", rows=rows, lease=lease, spec=spec,
                       publish=[(f"nimbus/{lease['cluster_id']}/lease", lease)])

    # -- hand-over between clusters: legs, and re-assignment after a closure (SC-2, FR-16) ----------------

    @staticmethod
    def current_spec(task: dict) -> dict:
        """The spec the task is working on now: its current leg, or the whole task when it has none."""
        legs = task.get("legs")
        return legs[task.get("leg_index", 0)] if legs else task["spec"]

    @staticmethod
    def has_next_leg(task: dict) -> bool:
        return bool(task.get("legs")) and task.get("leg_index", 0) < len(task["legs"]) - 1

    def _try_handover(self, task, spec, ctx, info, use_llm, notice, request_text, answer, retry, exclude):
        options = handover_options(spec, self.view(task["task_id"], exclude=exclude))
        if not options:
            return None
        legs = make_legs(spec, options[0])
        out = self._allocate(task, legs[0], ctx, info, use_llm, notice, request_text=request_text, answer=answer,
                             retry=retry, exclude=exclude)
        if out.kind not in ("leased", "queued"):
            return None
        note = _row("cloud", {"spec": spec}, {"decision": "plan_handover", "handoff": options[0].handoff,
                                              "legs": [{"from": options[0].cluster_a}, {"to": options[0].cluster_b}]},
                    out.lease["version"] if out.lease else None)
        out.rows.insert(0, note)
        out.spec, out.legs, out.leg_index = spec, legs, 0
        return out

    def advance_leg(self, task: dict) -> Outcome:
        """The edge finished a leg that is not the last one: lease the next leg as version n+1 (to the cluster
        that can finish the job). If no robot is free it queues, and the earlier leg's lease runs out on its own."""
        idx = task.get("leg_index", 0) + 1
        ctx = self.build_context(task["request_text"], task["task_id"], [])
        info = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0}
        out = self._allocate(task, task["legs"][idx], ctx, info, self.mode in LLM_MODES, None,
                             request_text=task["request_text"], answer=None, retry=True)
        out.leg_index = idx
        out.rows.insert(0, _row("cloud", {"completed_leg": idx}, {"decision": "advance_leg", "next_leg": idx + 1,
                                                                  "result": out.kind}, None))
        return out

    def preview_options(self, task: dict, blocked: list[str], robot_zone: str | None) -> dict:
        """What each way out of a blockage would do, computed without changing anything, so the model chooses among facts
        instead of searching: staying in the cluster (replan) or giving the rest to another cluster (hand_over)."""
        cur = self.repo.current(task["task_id"])
        if cur is None or not task.get("spec"):
            return {}
        view = self.view(task["task_id"], {z: "blocked" for z in blocked})
        prop = Proposal(cur["cluster_id"], list(cur["assigned_robots"]), "preview", "classical")
        res = check(task["spec"], prop, view, holds_stock=True)
        out = {"replan_in_cluster": {"feasible": res.ok, "route": res.routes[0] if res.routes else None,
                                    "why_not": None if res.ok else "; ".join(v["detail"] for v in res.violations)[:160]}}
        here = robot_zone if robot_zone in self.topo.nodes else self.fleet.robot_zone(cur["assigned_robots"][0])
        if here in self.topo.nodes:
            opts, _ = eligible_options({**task["spec"], "source": here}, self.view(task["task_id"], {z: "blocked" for z in blocked}))
            out["hand_over_to"] = [{"cluster": o.cluster_id, "robot": o.robot_id, "distance_m": round(o.distance, 1)}
                                   for o in opts if o.cluster_id != cur["cluster_id"]][:4]
        return out

    def reassign(self, task: dict, reason: str, *, only_cluster: str | None = None, robot_zone: str | None = None) -> Outcome:
        """The cluster holding the task cannot continue (a passage shut and no route inside its area): plan the
        rest of the job from where the robot is parked, as the next lease version. The planner may pick another
        cluster, whose robot collects the item at the parked robot's zone, or split the rest into new legs (a
        different hand-off). A cluster that is cut off simply has no route and drops out of the ranking. The old
        cluster gets a stop lease at the same version when the task moves away from it."""
        cur = self.repo.current(task["task_id"])
        if cur is None:
            return Outcome("replan_failed", reason="no live lease to reassign")
        here = robot_zone if robot_zone in self.topo.nodes else self.fleet.robot_zone(cur["assigned_robots"][0])
        if here not in self.topo.nodes:
            return Outcome("replan_failed", reason="the robot's position is unknown, so nobody can collect the item",
                           rows=[_row("cloud", {"reason": reason}, {"decision": "reassign_failed", "why": "robot position unknown"},
                                      cur["version"])])
        old = cur["cluster_id"]
        exclude = {c for c in self.fleet.clusters if c != only_cluster} if only_cluster else set()
        rest = {**task["spec"], "source": here}
        ctx = self.build_context(task["request_text"], task["task_id"], [])
        info = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0}
        out = self._allocate(task, rest, ctx, info, False, None, request_text=task["request_text"], answer=None,
                             retry=True, exclude=exclude)
        if out.kind != "leased":
            why = out.reason or out.kind
            return Outcome("replan_failed", reason=why, spec=task["spec"],
                           rows=[_row("cloud", {"reason": reason}, {"decision": "reassign_failed", "why": why}, cur["version"])])
        if out.lease["cluster_id"] != old:
            out.publish.append((f"nimbus/{old}/lease", self._stop_lease(cur, out.lease["version"], here)))
        out.rows.insert(0, _row("cloud", {"reason": reason, "from_cluster": old, "parked_at": here},
                                {"decision": "reassign", "to_cluster": out.lease["cluster_id"],
                                 "replaces_version": cur["version"]}, out.lease["version"]))
        out.spec = task["spec"]
        if out.legs is None:
            out.legs, out.leg_index = [], 0
        return out

    def _stop_lease(self, cur: dict, version: int, zone: str) -> dict:
        lease = {**cur, "version": version, "lease_id": f"lease-{_task_of(cur)}-v{version}", "allowed_zones": [zone],
                 "task_sequence": [{"cmd": "stop", "params": {}}], "when_to_escalate": list(cur["when_to_escalate"])}
        errors = contracts.validate_document("lease", lease)
        if errors:
            raise InvalidLeaseError(errors)
        return lease

    # -- stand-down: stop work without changing the lease contract ----------------------------------

    def stand_down(self, task: dict, reason: str) -> Outcome:
        """Publish lease v(n+1) whose only sub-task is `stop` and whose only allowed zone is where the robot is.

        The edge already honours "a higher version replaces the old one" (FR-8), so this halts a task
        (operator cancel or pause, a recall, or the old cluster after a hand-over) with the 12-field
        contract unchanged. Claims stay as they are: the caller releases them when the task is over.
        """
        cur = self.repo.current(task["task_id"])
        if cur is None:
            return Outcome("stand_down_failed", reason="no live lease")
        here = self.fleet.robot_zone(cur["assigned_robots"][0])
        zone = here if here in self.topo.nodes else cur["allowed_zones"][0]
        lease = self._stop_lease(cur, cur["version"] + 1, zone)
        try:
            self.repo.commit(lease, [f"robot:{r}" for r in lease["assigned_robots"]], now=self.clock.now())
        except Conflict as exc:
            return Outcome("stand_down_failed", reason="; ".join(exc.reasons))
        rows = [_row("cloud", {"lease_id": cur["lease_id"], "reason": reason},
                     {"decision": "stand_down", "lease_id": lease["lease_id"], "parked_at": zone, "reason": reason,
                      "sha256": _sha(lease)}, lease["version"])]
        return Outcome("stood_down", rows=rows, lease=lease, publish=[(f"nimbus/{lease['cluster_id']}/lease", lease)])

    # -- C2: the edge is a thin relay and asks the cloud before every non-reflex step ----------------

    def decide_step(self, task_id: str, version: int, cluster_id: str, event: dict) -> dict:
        """One cloud round trip for a relayed edge event (configuration C2 only).

        The model answers; deterministic code verifies it stays inside the live
        lease. If the model is down or answers outside the lease, the C3 rules
        (retry, then detour, then operator) decide, so the robot is never left
        waiting on a bad answer. Returns the decision plus what the call cost.
        """
        cur = self.repo.current(task_id)
        if cur is None:
            return {"error": "no live lease", "status": 404}
        if version < cur["version"]:
            raise StaleVersion(cur["version"])
        if cur["cluster_id"] != cluster_id:
            return {"error": "lease belongs to another cluster", "status": 409}
        info: dict = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "_latency_ms": 0}
        event = dict(event)
        for key in ("zone", "target"):   # a sub-place is reached through the zone that contains it
            if event.get(key):
                event[key] = self.topo.anchor_of(event[key]) or event[key]
        decision, source, error = None, "llm", None
        text = ("LEASE: " + json.dumps({k: cur[k] for k in ("allowed_zones", "forbidden_zones", "speed_limit",
                                                             "assigned_robots", "deadline_ts", "expiry_ts")}, sort_keys=True)
                + "\nSITUATION: " + json.dumps(event, sort_keys=True))
        ctx = self.build_context(event.get("situation", ""), task_id, [])
        try:
            res = self.llm.converse(system=self.pack.prompts["step"] + "\n\n" + ctx.to_prompt(),
                                    messages=[{"role": "user", "text": text}], tools=self.tools.names("step"),
                                    tool_choice="any", context=ctx)
            self._tally(info, res)
            call = res.tool_calls[0] if res.tool_calls else None
            errs = ["no tool call"] if call is None else self.tools.validate(call["name"], call["input"])
            if not errs:
                errs = self._step_errors(call["input"], cur, event)
            if errs:
                error = "; ".join(errs)
            else:
                decision = call["input"]
        except LLMError as exc:
            error = str(exc)
        if decision is None:
            decision, source = self._step_rules(cur, event), "rules"
        out = {"action": decision["action"], "via_zones": decision.get("via_zones", []),
               "rationale": decision["rationale"], "source": source, "lease_version": cur["version"]}
        row = _row("cloud" if source == "llm" else "rule", {"lease_id": cur["lease_id"], "event": event},
                   {"decision": "step_" + out["action"], "via_zones": out["via_zones"], "rationale": out["rationale"],
                    "source": source, **({"rejected_model_answer": error} if error else {}), "llm": _public(info)},
                   cur["version"])
        return {"decision": out, "rows": [row], "tokens_in": info["input_tokens"], "tokens_out": info["output_tokens"],
                "calls": info["calls"], "timings": {"llm_ms": info["_latency_ms"]}}

    @staticmethod
    def _step_errors(ans: dict, lease: dict, event: dict) -> list[str]:
        if ans["action"] != "reroute":
            return []
        usable = set(lease["allowed_zones"]) - set(lease["forbidden_zones"]) - set(event.get("blocked_zones") or [])
        via = ans.get("via_zones") or []
        outside = [z for z in via if z not in usable]
        return ([f"reroute zones outside the lease or blocked: {outside}"] if outside else []) + \
               (["reroute needs via_zones"] if not via else [])

    def _step_rules(self, lease: dict, event: dict) -> dict:
        """C3's finite-state rule: retry once, then detour inside the lease, then hand to the operator."""
        blocked = set(event.get("blocked_zones") or [])
        if not blocked:
            return {"action": "proceed", "rationale": "rules: nothing in the way"}
        if event.get("attempt", 0) == 0:
            return {"action": "wait", "rationale": "rules: retry once before detouring"}
        usable = set(lease["allowed_zones"]) - set(lease["forbidden_zones"])
        path = self.topo.shortest_path(event["zone"], event["target"], allowed=usable, avoid=blocked) \
            if event.get("zone") and event.get("target") else None
        if path:
            return {"action": "reroute", "via_zones": path, "rationale": "rules: detour inside the lease"}
        return {"action": "abort", "rationale": "rules: no detour inside the lease, operator decides"}

    # -- renewal: LLM-free (FR-6, "no LLM is involved in renewal") -------------------

    def renew(self, task_id: str, version: int) -> dict | None:
        """Extend a live lease on a check-in. No model on this path.

        Raises StaleVersion when the edge still reports an older version, so
        it learns the current one. Returns the lease only when expiry moved.
        """
        cur = self.repo.current(task_id)
        if cur is None:
            return None
        if version < cur["version"]:
            raise StaleVersion(cur["version"])
        now = self.clock.now()
        cap = parse_iso(cur["deadline_ts"]) + timedelta(seconds=MAX_OVERRUN_S)
        if now >= cap:
            return None  # too far past the deadline: the cloud must re-decide, not rubber-stamp
        target = min(now + timedelta(seconds=RENEW_TTL_S), cap)
        if target <= parse_iso(cur["expiry_ts"]):
            return None
        try:
            lease = self.repo.renew(task_id, version, iso(target), now=now)
        except Conflict:
            return None
        self.publish(f"nimbus/{lease['cluster_id']}/renewal", {
            "lease_id": lease["lease_id"], "version": lease["version"], "expiry_ts": lease["expiry_ts"],
            "message_id": f"renew-{lease['lease_id']}-{lease['expiry_ts']}"})
        return lease

    # -- lifecycle ------------------------------------------------------------------

    def finish(self, task: dict) -> None:
        """Task delivered: free robots and zones; the item stays consumed."""
        self._freed += 1
        self.repo.release(task["task_id"])
        self.scheduler.remove(task["task_id"])
        res = task.get("reservation")
        if res and not task.get("stock_released") and not task.get("stock_settled"):
            self.fleet.settle_stock(res["cluster"], res["item"])
            task["stock_settled"] = True
        spec, lease = task.get("spec"), task.get("lease")
        # equipment carried out of a place that keeps an inventory into a store: the counts follow it
        if (spec and lease and spec.get("source") and not task.get("inventory_moved")
                and self.topo.nodes.get(self.topo.anchor_of(spec["destination"]) or "", {}).get("kind") in self.topo.source_kinds):
            task["inventory_moved"] = self.fleet.move_stock(self.topo.anchor_of(spec["source"]) or spec["source"],
                                                            lease["cluster_id"], spec["item"])

    def cancel(self, task: dict) -> None:
        """Task cancelled: free robots and zones and give the item back once."""
        self._freed += 1
        self.repo.release(task["task_id"])
        self.scheduler.remove(task["task_id"])
        res = task.get("reservation")
        if res and not task.get("stock_released") and not task.get("stock_settled"):
            self.fleet.return_stock(res["cluster"], res["item"])
            task["stock_released"] = True

    def drain(self, load_task) -> list:
        """Retry queued tasks in priority/deadline order after capacity frees.
        Returns [(task, Outcome)] for the tasks that got a lease."""
        done = []
        ep = self.epoch()
        for entry in self.scheduler.ordered():
            if entry.tried_epoch == ep:      # nothing it could be waiting on has changed since it last failed
                continue
            task = load_task(entry.task_id)
            if task is None or task["status"] != "queued":
                self.scheduler.remove(entry.task_id)
                continue
            out = self.retry_queued(task)
            self.scheduler.mark_tried(entry.task_id, ep)
            if out.kind == "leased":
                done.append((task, out))
        return done

    def recover(self, tasks: list[dict]) -> None:
        for t in tasks:
            if t["status"] == "queued" and t.get("spec"):
                s = t["spec"]
                self.scheduler.push(t["task_id"], s["priority"],
                                    parse_iso(s["requested_at"]) + timedelta(minutes=s["deadline_minutes"]))


def _scoped(fn, arg_index=1):
    """Run an orchestrator entry point inside a trace scope for its task (a task dict or a task id)."""
    import functools

    @functools.wraps(fn)
    def inner(self, *a, **k):
        ref = a[0] if a else next(iter(k.values()), None)
        tid = ref.get("task_id") if isinstance(ref, dict) else ref if isinstance(ref, str) else None
        with task_scope(tid):
            return fn(self, *a, **k)
    return inner


for _name in ("handle_request", "retry_queued", "advance_leg", "reassign", "allocate_spec", "replan", "hold",
              "decide_step", "stand_down"):
    setattr(Orchestrator, _name, _scoped(getattr(Orchestrator, _name)))
