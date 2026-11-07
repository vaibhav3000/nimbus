"""C8 - Request-to-lease latency under parallel load (NFR-1 cloud part).

Two load shapes against the cloud's public POST /requests. Simulated edges only heartbeat (so both clusters are
healthy and leases can be issued); every task is cancelled by the operator endpoint as soon as its decision is back, which
frees its robot and returns any reserved item, so the run consumes no inventory and leaves no work behind:
  closed loop  N concurrent clients (N = 1, 2, 4, 8, 16, 32, 64), each sending its next request as soon as the last
               one is answered, --per-level requests per level
  open loop    Poisson arrivals at fixed rates (--rates, requests/s) for --window s each
Per request: HTTP status, request-to-decision time (the POST round trip: intent model call, allocation, rule
checker, lease commit) and the outcome (leased / queued / asked). The cancel is sent after the timer stops, from a
separate pool, so it adds the same background load at every level and is not part of the measured time.

Queueing model: service time S = mean decision time at N = 1; for the closed loop the asymptotic bound
R(N) = max(S, N S / c) is fitted for c (effective parallel servers); for the open loop the M/M/c (Erlang C)
mean response with that S and c is drawn over the measured points.

By default it runs against a local server with real Bedrock (--target local-bedrock). The shared deployment is only
used when asked for (--target deployed with NIMBUS_CLOUD_URL set), and only when no edge is connected to it.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

TEXTS = ["Deliver a wheelchair to 2B, routine.", "Deliver a blanket to 2C.", "Bring a monitor to ward 3.",
         "IV pole to 2C, urgent.", "A blanket to 2B please.", "Please bring a wheelchair to ward 3 within 30 minutes.",
         "Deliver an IV pole to ward 2B.", "Bring a monitor to 2C, routine."]


CANCELLER = ThreadPoolExecutor(max_workers=8)


def _cancel(target, tid):
    target.api.post(f"/tasks/{tid}/control", {"action": "cancel", "message_id": f"exp-cancel-{uuid.uuid4().hex[:10]}"}, timeout=30)


def one(target, edges, rng_text, shape, level, rd, lock, recs):
    t_send = time.time()
    st, out, dt = target.request(rng_text)
    tid = out.get("task_id")
    if tid:
        CANCELLER.submit(_cancel, target, tid)
    rec = {"shape": shape, "level": level, "t_send": round(t_send, 3), "http": st, "decision_s": round(dt, 3),
           "status": out.get("status"), "task_id": tid, "error": out.get("error")}
    with lock:
        recs.append(rec)


def closed_loop(target, edges, n, per_level, rd, rng, lock, recs):
    count = [0]

    def client():
        while True:
            with lock:
                if count[0] >= per_level:
                    return
                count[0] += 1
                text = rng.choice(TEXTS)
            one(target, edges, text, "closed", n, rd, lock, recs)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as ex:
        for _ in range(n):
            ex.submit(client)
    return time.time() - t0


def open_loop(target, edges, rate, window, rd, rng, lock, recs):
    t0, t, futs = time.time(), 0.0, []
    with ThreadPoolExecutor(max_workers=256) as ex:
        while True:
            t += rng.expovariate(rate)
            if t > window:
                break
            delay = t0 + t - time.time()
            if delay > 0:
                time.sleep(delay)
            futs.append(ex.submit(one, target, edges, rng.choice(TEXTS), "open", rate, rd, lock, recs))
    return time.time() - t0


def erlang_c_response(lam, s, c):
    """Mean response time of M/M/c with arrival rate lam, mean service s, c servers (inf if unstable)."""
    mu = 1.0 / s
    a = lam / mu
    if a >= c:
        return float("inf")
    rho = a / c
    summ = sum(a ** k / math.factorial(k) for k in range(c))
    pc = (a ** c / math.factorial(c)) / ((1 - rho) * summ + a ** c / math.factorial(c))
    return pc / (c * mu - lam) + s


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter), "local-bedrock")
    ap.add_argument("--levels", type=int, nargs="+", default=[1, 2, 4, 8, 16, 32, 64])
    ap.add_argument("--per-level", type=int, default=100)
    ap.add_argument("--rates", type=float, nargs="+", default=[0.5, 1, 2, 4])
    ap.add_argument("--window", type=float, default=60)
    ap.add_argument("--force", action="store_true", help="run even if real edges are connected (do not)")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    target = lib.open_target(a.target)
    busy = lib.edges_connected(target) if target.is_deployed else []
    if busy and not a.force:
        target.close()
        raise SystemExit(f"real edges are connected to {busy}: their robots would receive this load test's leases. "
                         "Run C8 when no edge is connected (or pass --force).")
    rd = lib.ResultDir("C8" if a.target != "local" else "C8_local", "Request-to-lease latency under parallel load (NFR-1)", target)
    rd.reset_raw()
    edges = lib.Edges(target).start(poll=False)
    lock = threading.Lock()
    recs: list[dict] = []
    walls = {}
    try:
        for n in a.levels:
            before = len(recs)
            walls[("closed", n)] = closed_loop(target, edges, n, a.per_level, rd, rng, lock, recs)
            print(f"[C8] closed N={n}: {len(recs) - before} requests in {walls[('closed', n)]:.1f} s")
            time.sleep(3)
        for rate in a.rates:
            before = len(recs)
            walls[("open", rate)] = open_loop(target, edges, rate, a.window, rd, rng, lock, recs)
            print(f"[C8] open {rate}/s: {len(recs) - before} requests in {walls[('open', rate)]:.1f} s")
            time.sleep(3)
        time.sleep(5)
    finally:
        CANCELLER.shutdown(wait=True)
        for r in recs:
            rd.raw("requests", r)
        edges.stop()
        target.close()
    summary = []
    for (shape, level), wall in walls.items():
        sub = [r for r in recs if r["shape"] == shape and r["level"] == level]
        ok = [r for r in sub if r["http"] in (200, 201)]
        d = [r["decision_s"] for r in ok]
        leased = [r for r in ok if r["status"] == "leased"]
        ci = lib.bootstrap_ci(d, stat=lambda v: lib.percentile(v, 95))
        summary.append({"shape": shape, "level": level, "requests": len(sub), "errors": len(sub) - len(ok),
                        "error_rate": lib.r((len(sub) - len(ok)) / len(sub)) if sub else None,
                        "throughput_rps": lib.r(len(ok) / wall), "decision_p50_s": lib.r(lib.percentile(d, 50)),
                        "decision_mean_s": lib.r(sum(d) / len(d)) if d else None,
                        "decision_p95_s": lib.r(lib.percentile(d, 95)), "p95_ci_low": lib.r(ci[0]), "p95_ci_high": lib.r(ci[1]),
                        "decision_max_s": lib.r(max(d) if d else None), "leased_at_once": len(leased),
                        "request_to_lease_p95_s": lib.r(lib.percentile([r["decision_s"] for r in leased], 95)),
                        "queued": sum(r["status"] == "queued" for r in ok), "asked": sum(r["status"] == "needs_clarification" for r in ok),
                        "http_codes": dict(sorted({str(r["http"]): sum(1 for x in sub if x["http"] == r["http"]) for r in sub}.items())),
                        "source": target.source})
    rd.summary(summary)
    closed = [s for s in summary if s["shape"] == "closed" and s["decision_mean_s"]]
    S = next((s["decision_mean_s"] for s in closed if s["level"] == 1), None) or (closed[0]["decision_mean_s"] if closed else 1.0)
    best_c, best_err = 1, float("inf")
    for c in range(1, 129):
        err = sum((max(S, s["level"] * S / c) - s["decision_mean_s"]) ** 2 for s in closed)
        if err < best_err:
            best_c, best_err = c, err
    rd.env.update({"service_time_s": S, "fitted_servers_c": best_c})
    plt = lib.plt()
    names = []
    fig, ax = plt.subplots()
    xs = [s["level"] for s in closed]
    ax.plot(xs, [s["decision_p50_s"] for s in closed], "o-", label="p50")
    ax.errorbar(xs, [s["decision_p95_s"] for s in closed],
                yerr=[[s["decision_p95_s"] - (s["p95_ci_low"] or s["decision_p95_s"]) for s in closed],
                      [(s["p95_ci_high"] or s["decision_p95_s"]) - s["decision_p95_s"] for s in closed]], fmt="s-", capsize=3, label="p95 (95% CI)")
    ax.plot(xs, [max(S, n * S / best_c) for n in xs], "k:", label=f"queueing bound, S={S:.2f} s, c={best_c}")
    lib.pass_line(ax, 30, "NFR-1: p95 within 30 s")
    ax.set_xscale("log", base=2); ax.set_yscale("log"); ax.set_xlabel("concurrent requests (closed loop)"); ax.set_ylabel("request to decision (s)")
    ax.set_title("C8: latency vs parallel requests"); ax.legend(fontsize=8)
    names.append(lib.save(fig, rd, "latency_vs_concurrency"))
    fig, ax = plt.subplots()
    ax.plot(xs, [s["throughput_rps"] for s in closed], "o-", label="closed loop (measured)")
    op = [s for s in summary if s["shape"] == "open"]
    ax.plot([s["level"] * 1 for s in op], [s["throughput_rps"] for s in op], "s", label="open loop: x = offered rate")
    ax.axhline(best_c / S, color="k", ls=":", label=f"capacity c/S = {best_c / S:.1f} req/s")
    ax.set_xscale("log", base=2); ax.set_xlabel("concurrent requests, or offered rate (req/s)"); ax.set_ylabel("completed requests / s")
    ax.set_title("C8: throughput and saturation"); ax.legend(fontsize=8)
    names.append(lib.save(fig, rd, "throughput"))
    if op:
        fig, ax = plt.subplots()
        lam = [s["level"] for s in op]
        ax.plot(lam, [s["decision_mean_s"] for s in op], "o", label="measured mean")
        grid = [x / 10 for x in range(1, int(max(lam) * 10) + 1)]
        ax.plot(grid, [erlang_c_response(x, S, best_c) for x in grid], "-", label=f"M/M/{best_c} prediction")
        ax.set_xlabel("Poisson arrival rate (requests/s)"); ax.set_ylabel("mean request to decision (s)")
        ax.set_title("C8: open-loop load vs M/M/c model"); ax.legend(fontsize=8)
        names.append(lib.save(fig, rd, "open_loop_vs_mmc"))
    worst = max((s["decision_p95_s"] or 0) for s in summary)
    knee = next((s["level"] for s in closed if s["decision_p95_s"] and s["decision_p95_s"] > 30), None)
    expected = next((s for s in summary if s["shape"] == "open" and s["level"] == min(a.rates)), None)
    ok = expected is not None and (expected["decision_p95_s"] or 99) <= 30 and expected["error_rate"] == 0
    rd.finish(claim="A request becomes a lease (or a clear queue/question decision) within 30 s at the expected load, and the curve shows where the cloud saturates.",
              pass_bar="Request to lease p95 <= 30 s on a good network (NFR-1).",
              verdict="PASS" if ok else "FAIL",
              measured=(f"at {min(a.rates)} req/s: p95 {expected['decision_p95_s'] if expected else None} s, errors {expected['error_rate'] if expected else None}; "
                        f"worst p95 over all levels {worst:.2f} s; p95 first above 30 s at N={knee}; "
                        f"fitted S={S:.2f} s, c={best_c} -> capacity about {best_c / S:.1f} req/s"),
              notes=["Decision time is measured at the client, so it includes the internet path to API Gateway.",
                     "'Leased at once' needs a free robot; under parallel load several requests compete for the same robot, so some are "
                     "queued for a moment: that is correct behaviour and not cloud latency.",
                     "Every task is cancelled right after its decision (robot freed, item returned), so the run leaves no work and uses no stock."],
              plots=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
