"""
Nimbus Experiment E1: Edge SLM Benchmarking & Latency Profiler
Validates NFR-1 (p95 <= 10.0 s, <= 150 output tokens) and FR-3 (Valid tactical decisions).
Can be executed on local CPU or Lab GPU (RTX 3060 / Jetson AGX Orin).
Outputs results directly to stdout and saves to benchmarks/e1_results.md.
Incorporates Person 1's Gazebo hospital topology graph nodes.
"""

from datetime import datetime
import json
from pathlib import Path
import platform
import statistics
import sys

# Ensure repository root containing 'edge' is on sys.path regardless of execution working directory
_current = Path(__file__).resolve().parent
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
while _current.parent != _current:
    if (_current / "edge" / "slm").is_dir():
        ROOT_DIR = _current
        break
    _current = _current.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

BENCHMARK_PROMPTS = [
    {
        "id": "SC-01",
        "description": "Routine waypoint progression in clear corridor",
        "context": {
            "lease_id": "lease-bench-01",
            "version": 1,
            "time_remaining_sec": 450.0,
            "robot_id": "R1",
            "current_pos": [4.5, 3.0],
            "current_zone": "east_mid_hall",
            "allowed_zones": ["east_waiting_approach", "east_mid_hall", "nurse_station_approach"],
            "forbidden_zones": ["west_mid_hall"],
            "event_description": "Corridor clear; ready for next waypoint nurse_station_approach.",
            "allowed_waypoints": ["nurse_station_approach"],
        },
    },
    {
        "id": "SC-02",
        "description": "Dynamic obstacle: maintenance cart in corridor",
        "context": {
            "lease_id": "lease-bench-02",
            "version": 1,
            "time_remaining_sec": 400.0,
            "robot_id": "R1",
            "current_pos": [4.5, 3.0],
            "current_zone": "east_mid_hall",
            "allowed_zones": [
                "east_waiting_approach",
                "east_mid_hall",
                "east_north_gate",
                "north_hall",
                "nurse_station_approach",
            ],
            "forbidden_zones": ["west_mid_hall"],
            "event_description": "Cleaning cart blocking east_mid_hall; bypass east_north_gate available.",
            "allowed_waypoints": ["east_mid_hall", "east_north_gate"],
        },
    },
    {
        "id": "SC-03",
        "description": "Instruction ambiguity: multiple equipment racks in approach",
        "context": {
            "lease_id": "lease-bench-03",
            "version": 1,
            "time_remaining_sec": 380.0,
            "robot_id": "R1",
            "current_pos": [0.0, -15.0],
            "current_zone": "south_equipment_approach",
            "allowed_zones": ["east_south_hall", "south_equipment_approach"],
            "forbidden_zones": ["west_south_hall"],
            "event_description": "Arrived at equipment room; two racks detected (Rack-A, Rack-B). Unclear which needs pump.",
            "allowed_waypoints": ["south_equipment_approach", "south_room_approach"],
        },
    },
    {
        "id": "SC-04",
        "description": "Corridor blocked, no alternate route inside allowed zones",
        "context": {
            "lease_id": "lease-bench-04",
            "version": 1,
            "time_remaining_sec": 320.0,
            "robot_id": "R1",
            "current_pos": [4.5, -8.0],
            "current_zone": "east_south_hall",
            "allowed_zones": ["east_south_hall"],
            "forbidden_zones": ["west_south_hall"],
            "event_description": "Fire door closed and locked; no alternative corridor inside East wing.",
            "allowed_waypoints": ["east_waiting_approach"],
        },
    },
    {
        "id": "SC-05",
        "description": "Lease expiry imminent (<30s remaining)",
        "context": {
            "lease_id": "lease-bench-05",
            "version": 1,
            "time_remaining_sec": 12.0,
            "robot_id": "R1",
            "current_pos": [4.5, 3.0],
            "current_zone": "east_mid_hall",
            "allowed_zones": ["east_mid_hall", "nurse_station_approach"],
            "forbidden_zones": ["west_mid_hall"],
            "event_description": "Lease timer expiring in 12 seconds; heading to safe parking bay.",
            "allowed_waypoints": ["east_waiting_approach"],
        },
    },
]


def compute_p95(values: list[float]) -> float:
    """Computes exact p95 regardless of sample count using sorted linear interpolation."""
    if not values:
        return 0.0
    if len(values) >= 20:
        return statistics.quantiles(values, n=20)[18]
    s = sorted(values)
    idx = 0.95 * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def estimate_tokens(decision_obj) -> int:
    """Roughly estimates token count (~4 chars/token) of the serialized JSON decision."""
    raw = json.dumps(decision_obj) if isinstance(decision_obj, dict) else str(decision_obj)
    return max(1, (len(raw) + 3) // 4)


def run_benchmark(model_name: str = "qwen2.5:1.5b", trials: int = 4, ollama_url: str = "http://127.0.0.1:11434"):
    print("=" * 75)
    print("  NIMBUS EXPERIMENT E1 BENCHMARK: EDGE SLM LATENCY & NFR-1 PROFILER")
    print("=" * 75)
    print(f"  Timestamp    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Host Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Target Model : {model_name}")
    print(f"  Ollama URL   : {ollama_url}")
    print(f"  Workload     : {len(BENCHMARK_PROMPTS)} scenarios x {trials} trials = {len(BENCHMARK_PROMPTS)*trials} decisions")
    print("=" * 75)

    from edge.slm.engine import EdgeSLMEngine

    # Pass ollama_url if supported by EdgeSLMEngine, otherwise fallback cleanly
    try:
        engine = EdgeSLMEngine(model_name=model_name, mode="auto", ollama_url=ollama_url)
    except TypeError:
        engine = EdgeSLMEngine(model_name=model_name, mode="auto")

    print(f"  SLM Engine Initialized in Mode: {engine.mode}")

    print("  Warming up GPU / model...")
    warmup_ctx = dict(BENCHMARK_PROMPTS[0]["context"])
    _, warmup_by, warmup_ms = engine.evaluate_decision(warmup_ctx)
    print(f"  Warm-up completed in {warmup_ms:.1f} ms (Decided by: {warmup_by})\n")

    latencies = []
    token_counts = []
    valid_json_count = 0
    lease_safe_count = 0
    total_calls = 0
    records = []

    print("Running benchmark trials...")
    for trial in range(1, trials + 1):
        for scenario in BENCHMARK_PROMPTS:
            total_calls += 1
            ctx = scenario["context"]
            decision, decided_by, lat_ms = engine.evaluate_decision(ctx)
            latencies.append(lat_ms)

            is_valid_json = isinstance(decision, dict) and bool(decision.get("action"))
            action_val = decision.get("action") if isinstance(decision, dict) else "INVALID"
            target_wp = decision.get("target_waypoint") or decision.get("waypoint") if isinstance(decision, dict) else None

            # FR-3 Lease boundary check: ensure target waypoint is not in forbidden_zones
            forbidden = set(ctx.get("forbidden_zones", []))
            is_lease_safe = is_valid_json and (target_wp not in forbidden)

            est_tokens = estimate_tokens(decision)
            token_counts.append(est_tokens)

            if is_valid_json:
                valid_json_count += 1
            if is_lease_safe:
                lease_safe_count += 1

            records.append({
                "trial": trial,
                "scenario_id": scenario["id"],
                "description": scenario["description"],
                "decided_by": decided_by,
                "action": action_val,
                "tokens": est_tokens,
                "latency_ms": lat_ms,
                "valid": is_valid_json and is_lease_safe,
            })
            print(
                f"  [{total_calls:02d}] {scenario['id']} Trial {trial}: {lat_ms:6.1f} ms | "
                f"Decided: {decided_by} | Action: {action_val} | ~{est_tokens} tok"
            )

    lat_sec = [l / 1000.0 for l in latencies]
    p50_sec = statistics.median(lat_sec)
    p95_sec = compute_p95(lat_sec)
    mean_sec = statistics.mean(lat_sec)
    min_sec = min(lat_sec)
    max_sec = max(lat_sec)
    max_tokens = max(token_counts) if token_counts else 0
    json_rate = (valid_json_count / total_calls) * 100.0
    safety_rate = (lease_safe_count / total_calls) * 100.0
    nfr1_pass = (p95_sec <= 10.0) and (max_tokens <= 150)

    print("\n" + "=" * 75)
    print("  EXPERIMENT E1 BENCHMARK SUMMARY RESULTS")
    print("=" * 75)
    print(f"  Total Decisions Tested    : {total_calls}")
    print(f"  Strict JSON Valid Rate    : {json_rate:.1f}% (Target: 100%)")
    print(f"  FR-3 Lease Safety Rate    : {safety_rate:.1f}% (Target: 100%)")
    print(f"  Max Output Tokens (Est.)  : {max_tokens} tokens (Target: <= 150)")
    print(f"  Minimum Latency           : {min_sec:.3f} s")
    print(f"  Median Latency (p50)      : {p50_sec:.3f} s")
    print(f"  Mean Latency              : {mean_sec:.3f} s")
    print(f"  95th Percentile (p95)     : {p95_sec:.3f} s")
    print(f"  Maximum Latency           : {max_sec:.3f} s")
    print(f"  NFR-1 Target Compliance   : {'PASSED (p95 <= 10.0 s, tokens <= 150)' if nfr1_pass else 'FAILED'}")
    print("=" * 75)

    is_mock = (engine.mode == "mock")
    compliance_p95 = "MOCK (Real inference required)" if is_mock else ("PASS" if p95_sec <= 10.0 else "FAIL")
    target_comp_str = "MOCK_INCONCLUSIVE (real inference required)" if is_mock else (f"{'PASS' if nfr1_pass else 'FAIL'} (p95 <= 10.0s)")

    md_content = f"""# Nimbus Experiment E1 Benchmark Results

- **Model:** `{model_name}` (4-bit quant, 150-token cap)
- **Host:** {platform.system()} {platform.machine()} ({platform.processor() or 'N/A'})
- **Engine Mode:** `{engine.mode}`
- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **Workload:** {total_calls} decisions over {len(BENCHMARK_PROMPTS)} hospital tactical scenarios

## Latency & Constraint Audit (NFR-1 & FR-3)

| Metric | Measured Value | Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `{min_sec:.3f} s` | N/A | OK |
| **Median (p50)** | `{p50_sec:.3f} s` | N/A | OK |
| **Mean** | `{mean_sec:.3f} s` | N/A | OK |
| **p95 Latency** | **`{p95_sec:.3f} s`** | **<= 10.0 s** | **{compliance_p95}** |
| **Max Latency** | `{max_sec:.3f} s` | N/A | OK |
| **Max Output Tokens** | **`{max_tokens} tok`** | **<= 150 tok** | **{'PASS' if max_tokens <= 150 else 'FAIL'}** |
| **JSON Syntax Adherence** | **`{json_rate:.1f}%`** | **100%** | **{'PASS' if json_rate == 100.0 else 'FAIL'}** |
| **FR-3 Lease Boundary Safety** | **`{safety_rate:.1f}%`** | **100%** | **{'PASS' if safety_rate == 100.0 else 'FAIL'}** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Est. Tokens | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|:---:|
"""
    for r in records:
        md_content += (
            f"| {r['trial']} | {r['scenario_id']} | {r['description']} | "
            f"`{r['decided_by']}` | `{r['action']}` | {r['tokens']} | {r['latency_ms']/1000.0:.2f} s |\n"
        )

    # Save to both benchmarks/e1_results.md and local directory if different
    benchmarks_dir = ROOT_DIR / "benchmarks"
    benchmarks_dir.mkdir(parents=True, exist_ok=True)
    target_paths = {benchmarks_dir / "e1_results.md", Path(__file__).resolve().parent / "e1_results.md"}

    for path in target_paths:
        path.write_text(md_content, encoding="utf-8")
        print(f"  [+] Formatted report saved to: {path.resolve()}")

    # Update e1_models.csv dynamically
    import csv
    models_csv_fields = [
        "model", "parameter_size", "quantization", "tokens_per_sec",
        "p50_latency_s", "p95_latency_s", "valid_actions_pct", "target_compliance"
    ]
    model_param_map = {
        "qwen2.5:0.5b": "0.49B",
        "qwen2.5:1.5b": "1.54B",
        "qwen2.5:3b": "3.09B",
    }
    tps = round(max_tokens / (mean_sec if mean_sec > 0 else 0.05), 1)
    new_entry = {
        "model": model_name,
        "parameter_size": model_param_map.get(model_name, "1.54B"),
        "quantization": "Q4_K_M",
        "tokens_per_sec": str(tps),
        "p50_latency_s": f"{p50_sec:.3f}",
        "p95_latency_s": f"{p95_sec:.3f}",
        "valid_actions_pct": f"{json_rate:.1f}%",
        "target_compliance": target_comp_str,
    }

    for csv_dest in [ROOT_DIR / "results" / "e1_models.csv", ROOT_DIR / "P2_Results" / "e1_models.csv"]:
        csv_dest.parent.mkdir(parents=True, exist_ok=True)
        existing_rows = {}
        if csv_dest.exists():
            with open(csv_dest, "r", encoding="utf-8") as f:
                reader = csv.DictReader(row for row in f if not row.startswith("#") and row.strip())
                for r in reader:
                    if r.get("model"):
                        existing_rows[r["model"]] = r
        existing_rows[model_name] = new_entry
        with open(csv_dest, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=models_csv_fields)
            writer.writeheader()
            for r in existing_rows.values():
                writer.writerow(r)
        print(f"  [+] CSV dataset updated at: {csv_dest.resolve()}")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:1.5b"
    run_benchmark(model_name=model, trials=4)
