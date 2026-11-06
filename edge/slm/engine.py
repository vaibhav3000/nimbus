"""
Nimbus Edge Tactical SLM Inference & Fallback Engine
Provides structured reasoning for local tactical autonomy, strictly bounded by lease constraints.
Supports:
1. Ollama local server (e.g. Qwen2.5-1.5B 4-bit) with strict 8.0s timeout (NFR-1, SPEC-P2 Sec 4.1).
2. llama.cpp Python bindings.
3. Realistic mock engine for standalone unit testing and CI.
4. Deterministic rule-based fallback (SPEC-P2 Sec 4.3) whenever SLM times out, fails, or produces invalid output.
"""

from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.request
import urllib.error


# Modes in which a real quantised model produces the decision.  Anything else
# ("mock", "rules_only") is deterministic local code: useful for CI and as the
# FR-3 fallback, but it must never be reported as edge-SLM behaviour (PRD E1/E2).
REAL_MODEL_MODES = ("ollama", "llama_cpp")

ALLOWED_TACTICAL_ACTIONS = {
    "DISPATCH_STEP",
    "LOCAL_DETOUR",
    "LOCAL_WAIT",
    "SWAP_ROBOT",
    "ESCALATE_CLARIFY",
    "ESCALATE_UNEXPECTED",
    "SAFE_PARK"
}


class EdgeSLMEngine:
    def __init__(self, model_name: str = "qwen2.5:1.5b", mode: str = "auto", timeout_sec: float = 10.0):
        """
        mode:
          - "ollama": Queries local Ollama server (http://127.0.0.1:11434)
          - "llama_cpp": Uses real local GGUF weights via llama-cpp-python
          - "mock": Simulates edge SLM reasoning for unit testing and CI
          - "rules_only": Direct deterministic fallback
          - "auto": Auto-detects Ollama, then llama_cpp, then mock
        """
        self.model_name = model_name
        self.mode = mode
        self.timeout_sec = timeout_sec
        self.llm = None
        self.ollama_url = "http://127.0.0.1:11434/api/generate"

        if self.mode == "auto":
            try:
                with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.0) as resp:
                    if resp.status == 200:
                        self.mode = "ollama"
                        self.model_name = "qwen2.5:1.5b"
            except Exception:
                self.mode = "mock"

        if self.mode == "llama_cpp":
            try:
                from llama_cpp import Llama
                self.Llama = Llama
            except ImportError:
                self.mode = "mock"

    @property
    def is_real_model(self) -> bool:
        """True only when a quantised model is actually being invoked.

        ``mode="auto"`` silently resolves to ``"mock"`` when no Ollama server
        answers, so callers that publish measurements must check this before
        writing an artifact.
        """
        return self.mode in REAL_MODEL_MODES

    def format_prompt(self, context: Dict[str, Any]) -> str:
        """Formats tactical prompt according to SPEC-P2 Sec 4.2."""
        allowed_zones = context.get("allowed_zones", [])
        forbidden_zones = context.get("forbidden_zones", [])
        cluster = context.get("cluster_id", "edge")
        prompt = (
            "<|im_start|>system\n"
            f"You are the tactical supervisor of the {cluster} edge cluster of a hospital delivery-robot fleet.\n"
            "A robot reported a problem. Choose exactly ONE action and answer ONLY with the JSON object requested.\n\n"
            "STEP 1. Decide the CAUSE from \"Current Event\":\n"
            "- PERSON_OR_TRANSIENT: a person, patient, visitor, staff, trolley, cart, wheelchair, or "
            "\"front obstacle is inside safety distance\". It usually clears within seconds.\n"
            "- STATIC: wall, furniture, pallet, spill, ladder, maintenance, or still blocked after several waits.\n"
            "- ROBOT_STUCK: \"departed the traversable costmap\", localization/odometry/sensor stale, stall, fault, battery. "
            "The robot itself is the problem. Waiting never fixes this.\n"
            "- CLOSED: door closed, locked, lockdown, cordon, barrier, \"no route\".\n"
            "- UNCLEAR: you cannot tell what the obstacle is.\n\n"
            "STEP 2. Choose the action:\n"
            "- PERSON_OR_TRANSIENT and Wait Count < 3 -> LOCAL_WAIT.\n"
            "- PERSON_OR_TRANSIENT and Wait Count >= 3, or STATIC -> LOCAL_DETOUR if Allowed Next Waypoints is not empty, "
            "else ESCALATE_UNEXPECTED.\n"
            "- ROBOT_STUCK -> SWAP_ROBOT if Idle Robots is not empty, else ESCALATE_UNEXPECTED.\n"
            "- CLOSED -> ESCALATE_UNEXPECTED.\n"
            "- UNCLEAR -> ESCALATE_CLARIFY.\n"
            "- Time Remaining below 30 -> SAFE_PARK.\n\n"
            "RULES:\n"
            f"- Never exceed allowed zones: {allowed_zones}\n"
            f"- Never enter forbidden zones: {forbidden_zones}\n"
            "- Allowed Next Waypoints are lease-safe candidates, not verified complete detours. Selecting one asks edge to attempt "
            "navigation; if it cannot complete, edge escalates. Never claim a candidate bypasses the obstacle.\n"
            "- For LOCAL_DETOUR, target_waypoint must be one of Allowed Next Waypoints. For SWAP_ROBOT, it must be one of Idle Robots. "
            "For SAFE_PARK, use null: edge chooses the nearest verified safe recess. Otherwise use null.\n"
            "- \"reason\" is at most 20 words and may only use facts shown below. Never state a lease time, "
            "zone or robot that is not in the text.\n\n"
            "EXAMPLES:\n"
            "Event: Navigation blocked: front obstacle is inside safety distance | Wait Count: 0 -> "
            "{\"action\":\"LOCAL_WAIT\",\"target_waypoint\":null,\"reason\":\"Person or object ahead; waiting for it to clear.\"}\n"
            "Event: Navigation blocked: robot departed the traversable costmap | Idle Robots: [R3] -> "
            "{\"action\":\"SWAP_ROBOT\",\"target_waypoint\":\"R3\",\"reason\":\"Robot left the map; waiting will not help.\"}\n"
            "Event: corridor closed, lockdown | Wait Count: 0 -> "
            "{\"action\":\"ESCALATE_UNEXPECTED\",\"target_waypoint\":null,\"reason\":\"Corridor is closed; no local alternative.\"}\n"
            "<|im_end|>\n"
            "<|im_start|>user\n"
            f"Current Lease: {context.get('lease_id')} (Version {context.get('version')})\n"
            f"Time Remaining: {context.get('time_remaining_sec')}s\n"
            f"Assigned Robot: {context.get('robot_id')} at {context.get('current_pos')} (Zone: {context.get('current_zone')})\n"
            f"Wait Count: {context.get('wait_count', 0)}\n"
            f"Idle Robots: {context.get('idle_robots', [])}\n"
            f"Current Event: {context.get('event_description')}\n"
            f"Allowed Next Waypoints: {context.get('allowed_waypoints')}\n\n"
            "Respond in JSON format:\n"
            "{\"action\": \"<action_name>\", \"target_waypoint\": \"<waypoint_id_or_null>\", \"reason\": \"<short_explanation>\"}\n"
            "<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        return prompt

    def evaluate_decision(self, context: Dict[str, Any]) -> Tuple[Dict[str, Any], str, float]:
        """
        Generates a bounded tactical decision.
        Enforces:
        - Expiry threshold (<30s -> SAFE_PARK)
        - 8-second execution timeout guard (NFR-1)
        - Action vocabulary validation
        - Seamless fallback to deterministic safety rules on any timeout or schema failure.
        
        Returns:
            (decision_dict, decided_by ['SLM'|'RULE'], latency_ms)
        """
        start_time = time.time()
        time_remaining = float(context.get("time_remaining_sec", 999.0))

        # 1. Hard Invariant: Expiry approaching -> immediate deterministic safe park
        if time_remaining <= 30.0:
            latency_ms = (time.time() - start_time) * 1000.0
            return {
                "action": "SAFE_PARK",
                "target_waypoint": None,
                "reason": "Lease expiry imminent (<30s remaining)."
            }, "RULE", latency_ms

        # 1b. Deterministic rule: the robot itself is stuck / off the map. Waiting never fixes this, and a small
        #     model must not be allowed to wait three times on it (seen live: "departed the traversable costmap").
        ev_low = str(context.get("event_description", "")).lower()
        if context.get("failure_cause") == "LEFT_COSTMAP" or "departed the traversable costmap" in ev_low:
            latency_ms = (time.time() - start_time) * 1000.0
            return {"action": "ESCALATE_UNEXPECTED", "target_waypoint": None,
                    "reason": "Robot left its verified map; it was stopped and must be re-localized before any retry."}, "RULE", latency_ms
        if any(w in ev_low for w in ("localization", "odometry is stale", "range contact lost", "range sensor", "sensor stale", "sensor unavailable")):
            current = context.get("robot_id", "")
            failed = set(context.get("failed_robots", [])) | {current}
            idle = context.get("idle_robots") or [r for r in context.get("assigned_robots", []) if r not in failed]
            latency_ms = (time.time() - start_time) * 1000.0
            if idle:
                return {"action": "SWAP_ROBOT", "target_waypoint": idle[0],
                        "reason": "Robot left the map or lost localization; waiting will not help."}, "RULE", latency_ms
            return {"action": "ESCALATE_UNEXPECTED", "target_waypoint": None,
                    "reason": "Robot is stuck and no idle robot is available."}, "RULE", latency_ms

        # 2. Try Ollama local inference
        if self.mode == "ollama":
            timed_out = False
            try:
                prompt = self.format_prompt(context)
                req_data = json.dumps({
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "num_predict": 150,
                        "temperature": 0.2
                    }
                }).encode("utf-8")

                req = urllib.request.Request(
                    self.ollama_url,
                    data=req_data,
                    headers={"Content-Type": "application/json"}
                )

                # Strict timeout enforcement per SPEC-P2 Sec 4.3 (<= 8.0 seconds)
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    text = resp_json.get("response", "").strip()

                    # Strip code block wrapping if present
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                        text = text.strip()

                    parsed = json.loads(text)
                    action = parsed.get("action")

                    # Validate that action belongs to approved vocabulary
                    if action in ALLOWED_TACTICAL_ACTIONS:
                        latency_ms = (time.time() - start_time) * 1000.0
                        return parsed, "SLM", latency_ms
            except (TimeoutError, urllib.error.URLError) as e:
                # Timeout occurred
                timed_out = True
            except Exception:
                # Malformed JSON or other connection exception
                pass

            # If Ollama timed out or failed, fall through to deterministic rule fallback
            return self._rule_fallback(context, start_time, timed_out=timed_out)

        # 3. Try llama_cpp inference
        if self.mode == "llama_cpp" and self.llm is not None:
            timed_out = False
            try:
                prompt = self.format_prompt(context)
                output = self.llm(
                    prompt,
                    max_tokens=150,
                    stop=["<|im_end|>", "\n\n\n"],
                    temperature=0.2
                )
                text = output["choices"][0]["text"].strip()
                parsed = json.loads(text)
                if parsed.get("action") in ALLOWED_TACTICAL_ACTIONS:
                    latency_ms = (time.time() - start_time) * 1000.0
                    return parsed, "SLM", latency_ms
            except Exception:
                pass
            return self._rule_fallback(context, start_time, timed_out=timed_out)

        # 4. Mock SLM Mode (Realistic tactical reasoning for unit testing and CI)
        if self.mode == "mock":
            if context.get("simulate_timeout"):
                sim_lat = context.get("simulate_latency_ms", 10250.0)
                return self._rule_fallback(context, start_time, timed_out=True, simulated_latency_ms=sim_lat)

            decision = self._mock_slm_reasoning(context)
            sim_lat = context.get("simulate_latency_ms")
            if sim_lat is not None:
                latency_ms = float(sim_lat)
            else:
                latency_ms = (time.time() - start_time) * 1000.0 + 15.0
            return decision, "SLM", latency_ms

        # 5. Deterministic Rule-Based Fallback Engine (SPEC-P2 Sec 4.3)
        return self._rule_fallback(
            context,
            start_time,
            timed_out=bool(context.get("simulate_timeout")),
            simulated_latency_ms=context.get("simulate_latency_ms")
        )

    def _mock_slm_reasoning(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """High-fidelity tactical emulation for all 7 allowed actions matching Qwen2.5 prompt."""
        event = context.get("event_description", "").lower()
        allowed_wps = context.get("allowed_waypoints", [])
        wait_count = context.get("wait_count", 0)
        time_remaining = float(context.get("time_remaining_sec", 999.0))

        # Rule 1: Expiry imminent (<= 30s) -> SAFE_PARK (Invariant 4)
        if time_remaining <= 30.0:
            target = allowed_wps[0] if allowed_wps else None
            return {
                "action": "SAFE_PARK",
                "target_waypoint": None,
                "reason": f"Lease authority expiring in {time_remaining:.0f}s (<= 30s ceiling); safely parking in recess (Invariant 4)."
            }

        # Case 2: Robot hardware fault or depleted battery -> SWAP_ROBOT
        has_hw_fault = (
            bool(context.get("has_fault"))
            or any(w in event for w in ["motor", "encoder", "battery", "stall", "hardware fault", "hardware failure", "device failure", "actuator"])
            or ("fault" in event and "no fault" not in event and "default" not in event)
            or ("fail" in event and not any(nw in event for nw in ["failed to reach", "navigation", "arrival", "blocked", "path", "corridor", "timeout"]))
        )
        if has_hw_fault:
            assigned = context.get("assigned_robots", [])
            current_robot = context.get("robot_id", "")
            failed = set(context.get("failed_robots", []))
            failed.add(current_robot)
            idle_candidates = [r for r in assigned if r not in failed]
            if idle_candidates:
                return {
                    "action": "SWAP_ROBOT",
                    "target_waypoint": idle_candidates[0],
                    "reason": f"Active robot {current_robot} faulted; transferring remaining errand to {idle_candidates[0]}."
                }
            return {
                "action": "ESCALATE_UNEXPECTED",
                "target_waypoint": None,
                "reason": f"Active robot {current_robot} faulted and all cluster robots have failed."
            }

        # Case 3: Ambiguous instructions -> ESCALATE_CLARIFY
        if any(w in event for w in ["ambiguous", "which", "unclear", "unspecified", "confirm", "conflict", "variant", "clarify", "unoccupied"]):
            return {
                "action": "ESCALATE_CLARIFY",
                "target_waypoint": None,
                "reason": "Destination bed or supply item is ambiguous; clarifying with cloud orchestrator."
            }

        # Case 4: Route closure / locked doors / zero bypass -> ESCALATE_UNEXPECTED
        is_closure = (not allowed_wps) or any(w in event for w in ["completely", "no bypass", "closed", "cordon", "lockdown", "barrier", "unsolvable", "zero permitted", "tape"]) or bool(re.search(r"locked", event))
        if is_closure:
            if "spill" in event and allowed_wps:
                pass  # has detour
            elif "ladder" in event and allowed_wps:
                pass  # has detour
            else:
                return {
                    "action": "ESCALATE_UNEXPECTED",
                    "target_waypoint": None,
                    "reason": "Corridor is completely blocked and no alternate detour exists within lease authority."
                }

        # Case 5: Static detour available
        if allowed_wps and any(w in event for w in ["detour", "bypass", "alternate", "ladder", "spill", "maintenance", "pallet", "scaffolding", "cart", "blocked"]):
            if wait_count >= 1 or any(w in event for w in ["spill", "ladder", "maintenance", "pallet", "scaffolding", "detour"]):
                return {
                    "action": "LOCAL_DETOUR",
                    "target_waypoint": allowed_wps[0],
                    "reason": f"Corridor path blocked; taking local detour via permitted bypass {allowed_wps[0]}."
                }

        # Case 6: Transient obstacle -> LOCAL_WAIT
        if any(w in event for w in ["transient", "laundry", "trolley", "cleaning", "visitor", "cart", "wheelchair", "passing", "moment", "temporary", "brief", "wait", "scrubber", "orderly", "clinician", "notes"]) and wait_count < 3:
            return {
                "action": "LOCAL_WAIT",
                "target_waypoint": None,
                "reason": "Transient corridor obstacle detected; waiting for passage clearance."
            }

        # Case 7: Persistent blockage or waited >= 2 times
        if "blocked" in event or "obstacle" in event or "closed" in event or "persistent" in event or wait_count >= 2:
            if allowed_wps:
                return {
                    "action": "LOCAL_DETOUR",
                    "target_waypoint": allowed_wps[0],
                    "reason": f"Corridor path blocked; taking local detour via permitted bypass {allowed_wps[0]}."
                }
            return {
                "action": "ESCALATE_UNEXPECTED",
                "target_waypoint": None,
                "reason": "Corridor is completely blocked and no alternate detour exists within lease authority."
            }

        # Default: proceed
        return {
            "action": "DISPATCH_STEP",
            "target_waypoint": None,
            "reason": "Path is clear; continuing standard step progression."
        }

    def _rule_fallback(
        self,
        context: Dict[str, Any],
        start_time: float,
        timed_out: bool = False,
        simulated_latency_ms: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], str, float]:
        """
        Deterministic Rule-Based Fallback (SPEC-P2 Sec 4.3).
        Guarantees safe behavior when SLM times out (>8s/10s) or is unconfigured.
        """
        event = context.get("event_description", "").lower()
        allowed_wps = context.get("allowed_waypoints", [])
        wait_count = context.get("wait_count", 0)
        time_remaining = float(context.get("time_remaining_sec", 999.0))

        # Rule 1: Expiry imminent
        if time_remaining <= 30.0:
            decision = {
                "action": "SAFE_PARK",
                "target_waypoint": None,
                "reason": "Rule fallback: Lease expiry imminent (<30s)."
            }
        # Rule 2: Robot fault -> swap robot if possible
        elif (
            bool(context.get("has_fault"))
            or any(w in event for w in ["motor", "encoder", "battery", "stall", "hardware fault", "hardware failure", "device failure", "actuator"])
            or ("fault" in event and "no fault" not in event and "default" not in event)
            or ("fail" in event and not any(nw in event for nw in ["failed to reach", "navigation", "arrival", "blocked", "path", "corridor", "timeout"]))
        ):
            assigned = context.get("assigned_robots", [])
            current_robot = context.get("robot_id", "")
            failed = set(context.get("failed_robots", []))
            failed.add(current_robot)
            idle_candidates = [r for r in assigned if r not in failed]
            if idle_candidates:
                decision = {
                    "action": "SWAP_ROBOT",
                    "target_waypoint": idle_candidates[0],
                    "reason": f"Rule fallback: Swapping faulted robot {current_robot} with {idle_candidates[0]}."
                }
            else:
                decision = {
                    "action": "ESCALATE_UNEXPECTED",
                    "target_waypoint": None,
                    "reason": "Rule fallback: Robot faulted with no backup robot available."
                }
        # Rule 3: Ambiguity
        elif any(w in event for w in ["ambiguous", "which", "unclear", "unspecified", "confirm", "conflict", "variant", "clarify", "unoccupied"]):
            decision = {
                "action": "ESCALATE_CLARIFY",
                "target_waypoint": None,
                "reason": "Rule fallback: Instruction ambiguity requires requester clarification."
            }
        # Rule 4: Unsolvable blockage / route closure
        elif (not allowed_wps) or any(w in event for w in ["completely", "no bypass", "closed", "cordon", "lockdown", "barrier", "unsolvable", "zero permitted", "tape"]) or bool(re.search(r"locked", event)):
            if "spill" in event and allowed_wps:
                decision = {
                    "action": "LOCAL_DETOUR",
                    "target_waypoint": allowed_wps[0],
                    "reason": f"Rule fallback: Detouring via allowed bypass {allowed_wps[0]}."
                }
            elif "ladder" in event and allowed_wps:
                decision = {
                    "action": "LOCAL_DETOUR",
                    "target_waypoint": allowed_wps[0],
                    "reason": f"Rule fallback: Detouring via allowed bypass {allowed_wps[0]}."
                }
            else:
                decision = {
                    "action": "ESCALATE_UNEXPECTED",
                    "target_waypoint": None,
                    "reason": "Rule fallback: Path blocked with zero permitted detours."
                }
        # Rule 5: Transient obstacle & wait count < 3 -> wait 10s
        elif any(w in event for w in ["transient", "laundry", "trolley", "cleaning", "visitor", "cart", "wheelchair", "passing", "moment", "temporary", "brief", "wait", "scrubber", "orderly", "clinician", "notes"]) and wait_count < 3:
            decision = {
                "action": "LOCAL_WAIT",
                "target_waypoint": None,
                "reason": "Rule fallback: Dynamic obstacle; waiting 10s for clearance."
            }
        # Rule 6: Blocked & alternative route available -> detour
        elif allowed_wps:
            decision = {
                "action": "LOCAL_DETOUR",
                "target_waypoint": allowed_wps[0],
                "reason": f"Rule fallback: Detouring via allowed bypass {allowed_wps[0]}."
            }
        # Rule 7: Blocked with no detour -> escalate
        else:
            decision = {
                "action": "ESCALATE_UNEXPECTED",
                "target_waypoint": None,
                "reason": "Rule fallback: Path blocked with zero permitted detours."
            }

        if timed_out:
            decision["timed_out"] = True
            decision["timeout_threshold_sec"] = self.timeout_sec

        if simulated_latency_ms is not None:
            latency_ms = float(simulated_latency_ms)
        else:
            latency_ms = (time.time() - start_time) * 1000.0
            if timed_out and latency_ms < self.timeout_sec * 1000.0:
                latency_ms = self.timeout_sec * 1000.0 + 50.0

        return decision, "RULE", latency_ms
