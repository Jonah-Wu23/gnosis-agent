import argparse
import json
from pathlib import Path

import numpy as np
from scipy.stats import binomtest

from engine.run_pilot import OUT, budget, read_records, baseline_records
from engine.scoped_units import experiment_version


def stats():
    version = experiment_version(OUT)
    automation = baseline_records()
    appworld = [r for r in read_records(OUT / "appworld_base.jsonl") if r.get("experiment_version") == version]
    groups = {}
    for domain in ("sales", "operations", "hr", "marketing", "support", "finance", "test_challenge"):
        rows = [r for r in automation + appworld if r["domain"] == domain]
        if not rows:
            continue
        passed = sum(r["success"] for r in rows)
        interval = binomtest(passed, len(rows)).proportion_ci()
        costs = [r["cost_usd"] for r in rows]
        groups[domain] = {"n": len(rows), "passed": passed, "strict_rate": passed / len(rows),
                           "strict_ci95": [interval.low, interval.high], "cost_mean_usd": float(np.mean(costs)),
                           "cost_p50_usd": float(np.quantile(costs, 0.5)),
                           "cost_p90_usd": float(np.quantile(costs, 0.9)), "cost_total_usd": sum(costs)}
        if domain != "test_challenge":
            groups[domain]["partial_credit"] = float(np.mean([r["partial_credit"] for r in rows]))
            groups[domain]["tool_calls_mean"] = float(np.mean([r["perf"]["tool_calls"] for r in rows]))
    meter = budget()
    calls = meter.totals()
    state = meter.state()
    injections = {}
    for path in (OUT / "runs" / version).glob("*.jsonl"):
        for record in read_records(path):
            for event in record["injections"]:
                group = injections.setdefault(event["unit"], {"knowledge_activations": 0,
                                               "component_invocations": 0, "next_calls_recorded": 0})
                if event.get("kind") == "knowledge_injection":
                    group["knowledge_activations"] += 1
                    group["next_calls_recorded"] += int("next_agent_call" in event)
                else:
                    group["component_invocations"] += 1
    result = {"experiment_version": version, "groups": groups, "injections": injections,
              "budget": calls, "total_cost_usd": state["spent_usd"],
              "unsettled_usd": state["unsettled_usd"],
              "pilot_limit_usd": state["ceiling_usd"],
              "research_cap_usd": state["research_cap_usd"],
              "remaining_pilot_usd": state["remaining_usd"],
              "window": {key: state[key] for key in ("window_limit_usd", "window_seconds",
                                                     "window_used_usd", "window_remaining_usd")},
              "automation_n": len(automation), "appworld_n": len(appworld)}
    if len(automation) == 30:
        mean = float(np.mean([r["cost_usd"] for r in automation]))
        strict = float(np.mean([r["success"] for r in automation]))
        n = 15 if mean <= 0.020 else 12 if mean <= 0.035 else 10 if mean <= 0.050 else None
        result["allocation"] = {"train_per_domain": n, "test_per_domain": n,
                                 "mean_automation_cost_usd": mean, "strict_rate": strict,
                                 "g0_cost": mean <= 0.05, "g0_difficulty": 0.2 <= strict <= 0.65}
    (OUT / "statistics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
