import itertools
import json
import os
import sqlite3
from pathlib import Path

import numpy as np
from scipy.stats import beta

from engine.activation import TaskBoundary
from engine.timing_research import ARMS, OUT, ROOT, SEED, LEDGER_PATH, read
from engine.scoped_units import save_json


def ledger_rows():
    path = LEDGER_PATH
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [dict(zip(("call", "phase", "cost", "billed", "reserved"), row))
                for row in db.execute("SELECT id,phase,cost,billed,reserved FROM calls ORDER BY at,id")]


def expense(rows):
    return {"reference_cny": sum(row["cost"] or 0 for row in rows) * 7.1,
            "billed_cny": sum(row["billed"] or 0 for row in rows) * 7.1,
            "unknown_reserved_cny": sum(row["reserved"] for row in rows if row["cost"] is None) * 7.1,
            "calls": len(rows)}


def capture_interval(captured, total):
    if total == 0:
        return {"rate": None, "ci95": None, "captured": 0, "total": 0}
    return {"rate": captured / total, "captured": captured, "total": total,
            "ci95": [float(beta.ppf(0.025, captured, total - captured + 1)) if captured else 0.0,
                     float(beta.ppf(0.975, captured + 1, total - captured)) if captured < total else 1.0]}


def paired_effect(values, samples=10000):
    delta = np.asarray(values, dtype=float)
    if not len(delta):
        raise ValueError("缺少完整来源，不能运行配对统计")
    observed = abs(delta.mean())
    exceed = sum(abs(np.dot(signs, delta) / len(delta)) >= observed - 1e-12
                 for signs in itertools.product((-1, 1), repeat=len(delta)))
    bootstrap = np.random.default_rng(SEED).choice(delta, size=(samples, len(delta))).mean(axis=1)
    return {"difference": float(delta.mean()), "p_two_sided": float(exceed) / 2 ** len(delta),
            "ci95": np.quantile(bootstrap, [0.025, 0.975]).tolist(), "independent_sources": len(delta)}


def interaction(values, labels, samples=10000):
    values = np.asarray(values, dtype=float)
    planning = np.array([label == "planning" for label in labels])
    if not planning.any() or planning.all():
        raise ValueError("交互检验需要两种预判来源")
    observed = values[planning].mean() - values[~planning].mean()
    assignments = list(itertools.combinations(range(len(values)), int(planning.sum())))
    exceed = 0
    for chosen in assignments:
        mask = np.zeros(len(values), dtype=bool)
        mask[list(chosen)] = True
        difference = values[mask].mean() - values[~mask].mean()
        exceed += abs(difference) >= abs(observed) - 1e-12
    rng = np.random.default_rng(SEED)
    bootstrap = (rng.choice(values[planning], (samples, int(planning.sum()))).mean(axis=1)
                 - rng.choice(values[~planning], (samples, int((~planning).sum()))).mean(axis=1))
    return {"interaction": float(observed), "p_two_sided": float(exceed) / len(assignments),
            "permutations": len(assignments), "ci95": np.quantile(bootstrap, [0.025, 0.975]).tolist(),
            "assumption": "source_effects_exchangeable_under_null",
            "planning_sources": int(planning.sum()), "execution_sources": int((~planning).sum())}


def holm(comparisons):
    previous = 0.0
    for rank, name in enumerate(sorted(comparisons, key=lambda key: comparisons[key]["p_two_sided"])):
        previous = max(previous, min(1.0, comparisons[name]["p_two_sided"] * (len(comparisons) - rank)))
        comparisons[name]["p_holm"] = previous
    return comparisons


def visibility(record, route):
    deadline = TaskBoundary.model_validate(route["deadline"]).locate(record["boundary_trace"]) if route["deadline"] else None
    first = {}
    for request in record["knowledge_requests"]:
        wire = record["wire_trace"][request["request_index"]]
        if wire["call"] != request["call"]:
            raise ValueError("可见时刻与实际请求不一致")
        for part in request["parts"]:
            first.setdefault(part, request)
    body = first.get("remainder")
    return {"first_visible": first, "deadline": deadline["index"] if deadline else None,
            "deadline_certainty": route["deadline_certainty"],
            "deadline_violated": (body is None or body["boundary_index"] >= deadline["index"]) if deadline else None,
            "delay_reached": TaskBoundary.model_validate(record["knowledge_treatment"]["delay"]).locate(record["boundary_trace"]) is not None}


def records_for(source, protocol, output=None):
    from engine.scoped_units import fingerprint
    from engine.timing_research import Repair, TaskRoute, treatment
    repair = Repair.model_validate(source["repair"])
    rows = {}
    for item in source["order"]:
        value = treatment(repair, TaskRoute.model_validate(source["routes"][item["task"]]), source["source"],
                          item["task"], item["arm"], source["route_version"])
        identity = fingerprint(["timing.v1", item["arm"], item["task"], item["repeat"], value, fingerprint(protocol)])
        record = read((output or OUT) / "executions" / item["arm"] / (identity + ".json"))
        if record["knowledge_treatment"] != value or record["experiment_version"] != protocol["experiment_version"]:
            raise ValueError("分析记录与冻结处理不一致")
        rows[(item["task"], item["arm"], item["repeat"])] = record
    return rows


def validation_summary(source, records, arm, ledger):
    pool = source["pool"]
    # 新修复为从空知识增加正文，旧版本的修复实际使用集合为空。
    history = set()
    host = set(source["body_update_sets"][arm])
    predicted = host | set(source["prediction"]["tasks"])
    old_new = [(records[(task, "N", repeat)], records[(task, arm, repeat)]) for task in pool for repeat in range(2)]
    regressions = [(old, new) for old, new in old_new if old["success"] and not new["success"]]
    noisy_tasks = {task for task in pool if records[(task, "N", 0)]["success"] != records[(task, "N", 1)]["success"]}
    result = {"pool": pool, "arm": arm, "source": source["source"], "no_edit_flip_tasks": sorted(noisy_tasks),
              "repeated_regression_tasks": [task for task in pool if all(
                  records[(task, "N", repeat)]["success"] and not records[(task, arm, repeat)]["success"] for repeat in range(2))]}
    overhead = [row for row in ledger if row["phase"].startswith(("route/" + source["source"] + "/",
                                                                "prediction/" + source["source"]))]
    for name, selected in (("history", history), ("host", host), ("predicted", predicted), ("full", set(pool))):
        pairs = [(old, new) for old, new in old_new if old["task"] in selected]
        calls = {new["execution_id"] for _, new in pairs}
        cash = expense([row for row in ledger if row["phase"].split("/")[-1] in calls])
        caught = sum(old["task"] in selected for old, _ in regressions)
        local_net = sum(int(new["success"]) - int(old["success"]) for old, new in pairs)
        full_net = sum(int(new["success"]) - int(old["success"]) for old, new in old_new)
        result[name] = {"tasks": sorted(selected), "retest_fraction": len(selected) / len(pool),
            "regression_capture": capture_interval(caught, len(regressions)),
            "outside_regressions": len(regressions) - caught, "net_gain": local_net,
            "accepted": local_net > 0, "full_accepted": full_net > 0,
            "decision_changed": (local_net > 0) != (full_net > 0), "execution_cost": cash}
    result["routing_prediction_cost"] = expense(overhead)
    full_cash = result["full"]["execution_cost"]["billed_cny"]
    result["deployment_saved_cny"] = full_cash - result["host"]["execution_cost"]["billed_cny"] - expense(overhead)["billed_cny"]
    return result


def analyze():
    source_output = Path(os.environ.get("GNOSIS_TIMING_PARENT", str(OUT)))
    protocol = read(source_output / "protocol.json")
    ledger = ledger_rows()
    source_rows, process_rows, validation = [], [], []
    for source in protocol["sources"]:
        records = records_for(source, protocol, output=source_output)
        means = {arm: sum(record["success"] for (_, treatment, _), record in records.items() if treatment == arm) / 6
                 for arm in ARMS}
        clear_tasks = [task for task in source["pool"] if source["routes"][task]["deadline_certainty"] == "clear"]
        clear_difference = (np.mean([int(records[(task, "L", repeat)]["success"]) - int(records[(task, "D", repeat)]["success"])
                                    for task in clear_tasks for repeat in range(2)]).item() if clear_tasks else None)
        source_rows.append({"source": source["source"], "category": source["slot"]["category"], "means": means,
                            "L-D": means["L"] - means["D"], "clear_L-D": clear_difference,
                            "clear_tasks": clear_tasks})
        for (task, arm, repeat), record in records.items():
            process_path = OUT / "process" / (record["execution_id"] + ".json")
            review = read(process_path) if process_path.exists() else None
            status_path = OUT / "process_status" / (record["execution_id"] + ".json")
            review_status = read(status_path)["status"] if status_path.exists() else "not_attempted"
            process_rows.append({"source": source["source"], "task": task, "arm": arm, "repeat": repeat,
                "execution_id": record["execution_id"], "success": record["success"], "cost_usd": record["cost_usd"],
                "tool_calls": len(record["actual_tool_actions"]), "blind_process_review": review,
                "process_review_status": review_status,
                "recovery_calls": len(review["recovery_action_indices"]) if review else None,
                **visibility(record, source["routes"][task])})
        validation.extend(validation_summary(source, records, arm, ledger) for arm in ("G", "L", "D", "C"))
    samples = protocol["analysis"]["bootstrap_samples"]
    primary = interaction([row["L-D"] for row in source_rows], [row["category"] for row in source_rows], samples)
    comparisons = {}
    for pair in protocol["analysis"]["secondary"]:
        left, right = pair.split("-")
        comparisons[pair] = paired_effect([row["means"][left] - row["means"][right] for row in source_rows], samples)
    noninferiority = {}
    for arm in ("L", "C"):
        effect = paired_effect([row["means"][arm] - row["means"]["G"] for row in source_rows], samples)
        precise = (effect["ci95"][1] - effect["ci95"][0]) / 2 <= protocol["analysis"]["required_ci_halfwidth"]
        noninferiority[arm] = {**effect, "precision_met": precise,
            "noninferior": precise and effect["ci95"][0] > -protocol["analysis"]["noninferiority_loss"]}
    zero_miss = sum(all(row["host"]["outside_regressions"] == 0 for row in validation if row["source"] == source["source"])
                    for source in protocol["sources"])
    report = {"sample_size": len(source_rows), "target_size": 8, "sources": source_rows,
              "execution_source": str(source_output),
              "primary": primary, "secondary": holm(comparisons), "noninferiority": noninferiority,
              "visibility": process_rows, "validation": validation, "total_paid_audit": expense(ledger),
              "zero_miss_source_upper_bound": 1 - 0.05 ** (1 / len(source_rows)) if zero_miss == len(source_rows) else None,
              "primary_significant": bool(len(source_rows) == 8 and primary["p_two_sided"] <= protocol["analysis"]["alpha"])}
    save_json(OUT / "analysis.json", report)
    print(json.dumps({"sources": len(source_rows), "primary": primary, "primary_significant": report["primary_significant"]}))
    return report
