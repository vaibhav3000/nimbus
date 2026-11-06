"""Re-score a recorded C1 run under the cloud's CURRENT grounding rules, without calling the model again.

The recorded answers (raw/requests_bedrock.jsonl) are what the model produced on an older cloud. Applying the deterministic
grounding that the cloud now does after the model (cloud/orchestrator.py: a destination must come from the requester's
words, else ask; priority follows the requester's words; an unstated deadline is the priority's default) shows what the
same answers would score today. Writes whatif.json next to the run.

    python experiments/cloud/c1_whatif.py results/final/C1_deployed
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

os.environ.setdefault("NIMBUS_PACK", "hospital_sim6")
sys.path.insert(0, str(lib.ROOT))
from cloud import nlp  # noqa: E402
from cloud.pack import Pack  # noqa: E402
from cloud.topology import Topology  # noqa: E402

FIELDS = ("item", "destination", "priority", "deadline_minutes")


def main(argv=None) -> int:
    run = Path((argv or sys.argv[1:])[0])
    pack = Pack.load()
    topo = Topology(pack)
    recs = [json.loads(line) for line in (run / "raw" / "requests_bedrock.jsonl").open()]

    def regrounded(r):
        words = nlp.parse(pack, topo, r["text"])
        if r["asked"] or "destination" in words.missing:
            return True, {}
        spec = dict(r.get("spec") or {})
        spec["priority"] = words.priority
        default = pack.priority(words.priority)["default_deadline_minutes"]
        stated = words.deadline_minutes != default
        spec["deadline_minutes"] = words.deadline_minutes if stated else default
        return False, spec

    def score(pred):
        right = total = vague = vague_asked = asked = false_q = 0
        for r in recs:
            a, spec = pred(r)
            asked += a
            if "ask" in r["gold"]:
                vague += 1
                vague_asked += a
                continue
            false_q += a
            for f in FIELDS:
                total += 1
                right += (not a) and spec.get(f) == r["gold"][f]
        return {"field_accuracy": round(right / total, 4), "field_ci": [round(x, 4) for x in lib.wilson(right, total)],
                "clarification_recall": round(vague_asked / vague, 4), "clarification_precision": round(vague_asked / asked, 4) if asked else None,
                "questions_on_clear_requests": false_q, "decisions": len(recs)}

    out = {"recorded_on": "the cloud as deployed at the time of the run",
           "as_recorded": score(lambda r: (r["asked"], r.get("spec") or {})),
           "with_current_grounding": score(regrounded),
           "note": "same model answers; only the deterministic code after the model differs"}
    (run / "whatif.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
