# C3: Memory and habits (FR-19)

- **Claim:** Confirmed habits let the cloud resolve a repeat request without asking again, without overriding an explicit choice.
- **PRD pass bar:** Most of 20 context-dependent requests resolved without asking (FR-19).
- **Measured:** memory on: 19/20 cases right, habit cases resolved without asking 10/10; memory off: the same habit requests asked 10/10 times (questions saved by memory)
- **Verdict:** PASS
- **Source of numbers:** real_bedrock (local-bedrock, model apac.amazon.nova-lite-v1:0)
- **Command:** `python experiments/cloud/c3_memory.py --target local-bedrock`
- **Commit:** 485d898, run 2026-11-06T17:58:38Z to 2026-11-06T18:03:13Z

## Plots

![memory_ablation](plots/memory_ablation.png)

## Notes

- The case set repeats three phrasings; labels are drafts until reviewed.
- A habit needs 2 confirmations (cloud/memory.py CONFIRMATIONS); controls with 0 or 1 confirmation must still ask.
- With memory on, 19 of 20 cases were right. The miss is ctx-19 ("A standard infusion pump to 2B, no paediatric needed."): the model asked a question although the request names the item. Its memory-off twin (ctx-20) was also asked. The memory-off rows for habit requests are expected to ask (that is the comparison).

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
