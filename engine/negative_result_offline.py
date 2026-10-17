import argparse
import csv
import hashlib
import itertools
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "experiments/negative_result_offline"
PAID = ROOT / "experiments/feedback_chain/paid"
ARMS = ("F0R0", "F1R0", "F0R1", "F1R1", "ancestor", "carry")
FX = 7.1
# 去掉各后端写入的生成时间，使同一份数据重复运行得到逐字节相同的图
FIG_METADATA = {"svg": {"Date": None}, "png": {}, "pdf": {"CreationDate": None, "ModDate": None}}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def table(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def inference(values, interval=True):
    values = np.asarray(values, dtype=float)
    n = len(values)
    signs = np.asarray(list(itertools.product((-1, 1), repeat=n)))
    observed = float(values.mean())
    statistics = signs @ values / n
    result = {"block_differences": values.tolist(), "effect": observed,
              "p_two_sided": float(np.mean(np.abs(statistics) >= abs(observed) - 1e-12)),
              "sign_combinations": [{"signs": s.tolist(), "statistic": float(t)} for s, t in zip(signs, statistics)],
              "comparison_unit": "source_block", "assumption": "共同偏移及块差符号可交换"}
    if interval:
        shifts = np.linspace(-1, 1, 20001)
        probabilities = np.mean(np.abs((signs @ values[:, None] - signs.sum(axis=1)[:, None] * shifts) / n)
                                >= np.abs(observed - shifts)[None, :] - 1e-12, axis=0)
        indices = np.flatnonzero(probabilities >= 0.05)
        splits = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
        result["accepted_intervals"] = [[float(shifts[s[0]]), float(shifts[s[-1]])] for s in splits if len(s)]
        result["ci95"] = [float(shifts[indices[0]]), float(shifts[indices[-1]])]
        result["grid_resolution"] = 0.0001
        result["accepted_points"] = len(indices)
        result["endpoint_p"] = [float(probabilities[indices[0]]), float(probabilities[indices[-1]])]
    return result


def collect():
    protocol = read(ROOT / "experiments/feedback_chain/frozen/execution_v1.json")
    mapping = read(PAID / "business_review/mapping.json")
    masked = {v["execution_id"]: k for k, v in mapping.items()}
    ledger = sqlite3.connect((ROOT / "state/feedback_chain_ledger.sqlite").as_uri() + "?mode=ro", uri=True)
    ledger_costs = {phase.rsplit("/", 1)[-1]: (cost, billed) for phase, cost, billed in ledger.execute(
        "SELECT phase,SUM(cost),SUM(COALESCE(billed,cost)) FROM calls WHERE phase LIKE '%/execution/%' "
        "GROUP BY phase ORDER BY phase")}
    ledger.close()
    rows, records = [], {}
    for path in sorted((PAID / "executions").glob("*/*.json")):
        record = read(path)
        identity = record["execution_id"]
        if identity in records:
            raise ValueError("实验执行身份重复")
        records[identity] = record
        verdict = read(PAID / "business_review/verdicts" / (masked[identity] + ".json")) if identity in masked else None
        complete = record["execution_error"] is None and record["termination_status"] == "completed"
        wire = record["wire_trace"][0]
        request_file = PAID / "io" / wire["call"] / "request.json"
        request = read(request_file)
        # 仅移除传输重试身份；实际提示、工具与生成参数全部保留。
        params = {k: v for k, v in request["parameters"].items() if k not in ("service_attempt", "retry_of", "format_attempt")}
        row = {"execution_id": identity, "task": record["task"], "domain": record["domain"],
               "arm": record["arm"], "repeat": record["repeat"], "version": record["version"],
               "experiment_version": record["experiment_version"], "task_contract": record["task_contract"],
               "strict": int(record["success"]), "frozen": int(complete and all(r["passed"] for r in verdict["requirements"])) if verdict else None,
               "record_cost_cny": record["cost_usd"] * FX, "cost_cny": ledger_costs[identity][0] * FX,
               "billed_cny": ledger_costs[identity][1] * FX, "duration_s": record["duration_s"],
               "actions": len(record["actual_tool_actions"]), "failed_actions": sum(bool(a.get("failed")) for a in record["actual_tool_actions"]),
               "termination": record["termination_status"], "execution_error_present": record["execution_error"] is not None,
               "initial_state": digest(record["initial_world"]), "first_request": digest(wire["request"]),
               "request_parameters": digest(params), "recorded_at": record["recorded_at"],
               "path": path.relative_to(ROOT).as_posix(), "pointer": "/"}
        rows.append(row)
    if len(rows) != 462 or len(masked) != 432:
        raise ValueError("冻结实验执行数量不完整")
    return protocol, rows, records


def statistics(protocol, rows):
    heldout = [r for r in rows if r["frozen"] is not None]
    by_key = {(r["task"], r["arm"], r["repeat"]): r for r in heldout}
    candidates = {b["domain"]: read(PAID / "candidates" / (b["domain"] + ".json")) for b in protocol["blocks"]}
    blocks = []
    for b in protocol["blocks"]:
        row = {"domain": b["domain"], "edited": any(c["status"] == "edited" for c in candidates[b["domain"]].values())}
        for score in ("strict", "frozen"):
            row[score] = {arm: float(np.mean([r[score] for r in heldout if r["domain"] == b["domain"] and r["arm"] == arm])) for arm in ARMS}
        row["difference"] = row["frozen"]["F1R0"] - row["frozen"]["F0R0"]
        blocks.append(row)
    primary = inference([b["difference"] for b in blocks])
    frozen = read(PAID / "analysis.json")
    checks = {k: bool(np.allclose(primary[k], frozen["primary"][k], atol=1e-12)) for k in ("effect", "p_two_sided", "ci95")}
    if not all(checks.values()):
        raise ValueError("独立复算与冻结统计不一致")
    subsets = {str(edited): inference([b["difference"] for b in blocks if b["edited"] == edited]) for edited in (True, False)}
    sensitivity = {"leave_one_block_out": [{"removed": b["domain"], **inference([a["difference"] for a in blocks if a != b])} for b in blocks],
                   "repeat": [], "tasks": []}
    for repeat in range(3):
        differences = [np.mean([by_key[(t["task"], "F1R0", repeat)]["frozen"] - by_key[(t["task"], "F0R0", repeat)]["frozen"] for t in b["migration"]]) for b in protocol["blocks"]]
        sensitivity["repeat"].append({"repeat": repeat, **inference(differences)})
    for task in protocol["plan"]["heldout"]:
        values = {arm: [by_key[(task, arm, r)]["frozen"] for r in range(3)] for arm in ARMS}
        sensitivity["tasks"].append({"task": task, "results": values, "net_success_difference": sum(values["F1R0"]) - sum(values["F0R0"])})
    r1 = inference([b["frozen"]["F0R1"] - b["frozen"]["F0R0"] for b in blocks])
    joint = inference([b["frozen"]["F1R1"] - b["frozen"]["F1R0"] - b["frozen"]["F0R1"] + b["frozen"]["F0R0"] for b in blocks])
    # 次要表示和交互构成同一比较族。
    ordered = sorted([(r1["p_two_sided"], "representation"), (joint["p_two_sided"], "interaction")])
    adjusted, previous = {}, 0
    for i, (p, name) in enumerate(ordered):
        previous = max(previous, min(1, (len(ordered) - i) * p))
        adjusted[name] = previous
    contribution = {"edited_net_success": round(sum(b["difference"] * 12 for b in blocks if b["edited"])),
                    "unedited_net_success": round(sum(b["difference"] * 12 for b in blocks if not b["edited"]))}
    contribution["unedited_share_of_net_difference"] = contribution["unedited_net_success"] / sum(contribution.values())
    result = {"blocks": blocks, "primary": primary, "frozen_reproduction": checks, "trigger_subsets": subsets,
              "contribution": contribution, "R1_increment": r1, "interaction": joint, "secondary_holm": adjusted, "sensitivity": sensitivity}
    write(OUTPUT / "statistics.json", result)
    table(OUTPUT / "tables/block_effects.csv", [{"domain": b["domain"], "editing_triggered": b["edited"], "effect_pp": b["difference"] * 100,
          **{arm: b["frozen"][arm] for arm in ARMS}} for b in blocks])
    table(OUTPUT / "tables/task_results.csv", [{"task": t["task"], "net_success_difference": t["net_success_difference"],
          **{arm: sum(t["results"][arm]) for arm in ARMS}} for t in sensitivity["tasks"]])
    return result


def identical_versions(rows):
    groups = defaultdict(list)
    for r in rows:
        if r["frozen"] is not None and r["arm"] != "carry":
            key = tuple(r[k] for k in ("task", "version", "experiment_version", "task_contract", "initial_state", "request_parameters", "first_request"))
            groups[key].append(r)
    summaries, flips = [], []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        pairs = list(itertools.combinations(group, 2))
        changed = [(a, b) for a, b in pairs if a["frozen"] != b["frozen"]]
        summaries.append({"task": key[0], "version": key[1], "executions": len(group), "successes": sum(r["frozen"] for r in group),
                          "pairs": len(pairs), "discordant_pairs": len(changed), "arms": sorted(set(r["arm"] for r in group)),
                          "execution_ids": [r["execution_id"] for r in group], "input_equality": "完整首请求、初态、任务契约、版本及生成工具参数相同",
                          "first_at": min(r["recorded_at"] for r in group), "last_at": max(r["recorded_at"] for r in group),
                          "actions_min_max": [min(r["actions"] for r in group), max(r["actions"] for r in group)],
                          "cost_cny_min_max": [min(r["cost_cny"] for r in group), max(r["cost_cny"] for r in group)]})
        flips.extend({"task": key[0], "left": a["execution_id"], "right": b["execution_id"]} for a, b in changed)
    result = {"matching_policy": "精确条件匹配，时间身份保留；执行顺序不同，展示分布，不执行标签置换", "groups": summaries,
              "discordant_pairs": flips, "counts": {"groups": len(summaries), "executions": sum(g["executions"] for g in summaries),
              "pairs": sum(g["pairs"] for g in summaries), "flips": len(flips)}}
    write(OUTPUT / "same_version.json", result)
    return result


def carried_trajectory(protocol, rows, records):
    indexed = {(r["task"], r["arm"], r["repeat"]): r for r in rows}
    pairs = []
    for task in protocol["plan"]["heldout"]:
        for repeat in range(3):
            first, fresh, carry = [indexed[(task, arm, rep)] for arm, rep in (("ancestor", repeat), ("ancestor", (repeat + 1) % 3), ("carry", repeat))]
            messages = records[carry["execution_id"]]["wire_trace"][0]["request"]
            content = messages[-1]["content"]
            prefix = "Review your previous execution and complete the original task in the fresh environment.\n"
            if not content.startswith(prefix):
                raise ValueError("携带轨迹输入协议不一致")
            evidence = json.loads(content[len(prefix):])
            pairs.append({"task": task, "repeat": repeat, "first_id": first["execution_id"], "fresh_id": fresh["execution_id"], "carry_id": carry["execution_id"],
                          "first_success": first["frozen"], "fresh_success": fresh["frozen"], "carry_success": carry["frozen"],
                          "fresh_total_cny": first["cost_cny"] + fresh["cost_cny"], "carry_total_cny": first["cost_cny"] + carry["cost_cny"],
                          "same_initial_state": first["initial_state"] == carry["initial_state"], "evidence_fields": sorted(evidence),
                          "carried_assertions_match": evidence.get("assertions") == records[first["execution_id"]]["assertions"],
                          "carry_request_path": carry["path"], "input_pointer": "/wire_trace/0/request/3/content",
                          "carry_actions": carry["actions"], "fresh_actions": fresh["actions"]})
    if not all(p["same_initial_state"] for p in pairs):
        raise ValueError("携带轨迹执行初态发生变化")
    summaries = {}
    for condition, selected in (("all", pairs), ("first_failed", [p for p in pairs if not p["first_success"]]), ("first_passed", [p for p in pairs if p["first_success"]])):
        sums = {k: sum(p[k] for p in selected) for k in ("fresh_success", "carry_success", "fresh_total_cny", "carry_total_cny")}
        net = sums["carry_success"] - sums["fresh_success"]
        summaries[condition] = {"pairs": len(selected), **sums,
                               "incremental_cny_per_extra_success": (sums["carry_total_cny"] - sums["fresh_total_cny"]) / net if net else None,
                               "fresh_cny_per_success": sums["fresh_total_cny"] / sums["fresh_success"] if sums["fresh_success"] else None,
                               "carry_cny_per_success": sums["carry_total_cny"] / sums["carry_success"] if sums["carry_success"] else None}
    result = {"pairs": pairs, "summary": summaries, "feedback_availability": {"task_and_previous_actions": "实际运行可取得",
              "tool_results": "实际运行可取得", "assertions": "本实验评价器反馈；真实部署须提供同等可用评价器"},
              "pair_dependence": "祖先执行循环共用，按题先聚合；72对包含72个唯一祖先执行，每个祖先在两种位置使用"}
    write(OUTPUT / "carry.json", result)
    table(OUTPUT / "tables/carry_pairs.csv", [{k: v for k, v in p.items() if not isinstance(v, list)} for p in pairs])
    return result


def costs(rows):
    conn = sqlite3.connect((ROOT / "state/feedback_chain_ledger.sqlite").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 账本按 id 排序读取：无 ORDER BY 时扫描顺序随 WAL 状态变化，浮点求和末位会漂移
    calls = [dict(r) for r in conn.execute(
        "SELECT id,phase,reserved,cost,input_tokens,output_tokens,cached_tokens,at,billed FROM calls ORDER BY id")]
    conn.close()
    grouped = defaultdict(list)
    for call in calls:
        group = "task_execution" if "/execution/" in call["phase"] else "organization" if "/organize" in call["phase"] else "editing"
        grouped[group].append(call)
    summaries = {}
    for group, values in grouped.items():
        summaries[group] = {"calls": len(values), "settled_calls": sum(v["cost"] is not None for v in values),
                            "reference_cny": sum(v["cost"] or 0 for v in values) * FX,
                            "billed_cny": sum(v["billed"] if v["billed"] is not None else (v["cost"] or 0) for v in values) * FX,
                            "unsettled_reserved_cny": sum(v["reserved"] for v in values if v["cost"] is None) * FX,
                            "input_tokens": sum(v["input_tokens"] or 0 for v in values), "output_tokens": sum(v["output_tokens"] or 0 for v in values),
                            "cached_tokens": sum(v["cached_tokens"] or 0 for v in values)}
    execution_total = sum(r["cost_cny"] for r in rows)
    if not np.isclose(execution_total, summaries["task_execution"]["reference_cny"], atol=1e-7):
        raise ValueError("执行费用与账本结算费用不一致")
    phases = defaultdict(list)
    for call in calls:
        phases[call["phase"]].append(call)
    phase_rows = [{"phase": phase, "calls": len(v), "reference_cny": sum(x["cost"] or 0 for x in v) * FX,
                   "billed_cny": sum(x["billed"] if x["billed"] is not None else (x["cost"] or 0) for x in v) * FX} for phase, v in phases.items()]
    discrepancies = [{"execution_id": r["execution_id"], "task": r["task"], "record_cny": r["record_cost_cny"],
                      "ledger_cny": r["cost_cny"], "difference_cny": r["cost_cny"] - r["record_cost_cny"],
                      "source": r["path"]} for r in rows if not np.isclose(r["cost_cny"], r["record_cost_cny"], atol=1e-9)]
    result = {"groups": summaries, "execution_ledger_reference_cny": execution_total,
              "execution_record_reference_cny": sum(r["record_cost_cny"] for r in rows),
              "execution_record_ledger_discrepancies": discrepancies,
              "total_billed_cny": sum(s["billed_cny"] for s in summaries.values()),
              "review_provider_cost": {"status": "missing", "verdict_files": 432, "reason": "本账本无外部盲审调用结算；不把缺失费用计零"},
              "learning_amortization": [{"uses": n, "cny_per_use": sum(summaries[g]["billed_cny"] for g in ("editing", "organization")) / n} for n in (1, 10, 100, 1000)],
              "ledger_calls": calls}
    write(OUTPUT / "costs.json", result)
    table(OUTPUT / "tables/cost_phases.csv", phase_rows)
    return result


def database(rows, cost_result):
    conn = sqlite3.connect(OUTPUT / "research.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS executions (execution_id TEXT PRIMARY KEY, task TEXT, domain TEXT, arm TEXT, version TEXT, strict INTEGER, frozen INTEGER, cost_cny REAL, source_file TEXT, source_pointer TEXT, metadata TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS costs (call_id TEXT PRIMARY KEY, phase TEXT, reference_usd REAL, billed_usd REAL, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, source_file TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS artifacts (path TEXT PRIMARY KEY, sha256 TEXT, bytes INTEGER)")
    for r in rows:
        conn.execute("INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (r["execution_id"], r["task"], r["domain"], r["arm"], r["version"], r["strict"], r["frozen"], r["cost_cny"], r["path"], r["pointer"], json.dumps(r, ensure_ascii=False)))
    for c in cost_result["ledger_calls"]:
        conn.execute("INSERT OR REPLACE INTO costs VALUES (?,?,?,?,?,?,?,?)", (c["id"], c["phase"], c["cost"], c["billed"], c["input_tokens"], c["output_tokens"], c["cached_tokens"], "state/feedback_chain_ledger.sqlite#calls"))
    for path in sorted(OUTPUT.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".csv", ".md", ".mmd"):
            content = path.read_bytes()
            conn.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?)", (path.relative_to(OUTPUT).as_posix(), hashlib.sha256(content).hexdigest(), len(content)))
    conn.commit()
    conn.close()


def plots(stats, carry):
    folder = OUTPUT / "figures"
    folder.mkdir(exist_ok=True)
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "svg.fonttype": "none", "svg.hashsalt": "gnosis-negative-result"})
    fig, ax = plt.subplots(figsize=(7.3, 3.6))
    blocks = stats["blocks"]
    ax.barh([b["domain"] for b in blocks], [b["difference"] * 100 for b in blocks], color=["#256b87" if b["edited"] else "#ce8b45" for b in blocks])
    ax.axvline(0, color="black", linewidth=.7)
    ax.set_xlabel("F1R0 minus F0R0 (percentage points)")
    ax.set_title("Frozen requirements; blue: editing triggered; orange: ancestor retained")
    fig.tight_layout()
    for ext in ("svg", "png", "pdf"):
        fig.savefig(folder / ("figure3_block_effects." + ext), dpi=450, metadata=FIG_METADATA[ext])
    plt.close(fig)
    tasks = stats["sensitivity"]["tasks"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 6.8), gridspec_kw={"width_ratios": [2, 1]})
    values = np.array([[sum(t["results"][a]) for a in ARMS] for t in tasks])
    picture = axes[0].imshow(values, vmin=0, vmax=3, cmap="Blues", aspect="auto")
    axes[0].set_xticks(range(6), ARMS, rotation=35, ha="right")
    axes[0].set_yticks(range(24), [t["task"] for t in tasks], fontsize=7)
    for i in range(24):
        for j in range(6):
            axes[0].text(j, i, str(values[i, j]), ha="center", va="center", fontsize=7, color="white" if values[i, j] > 1 else "black")
    axes[0].set_title("Successes in three runs per task")
    s = carry["summary"]["all"]
    axes[1].bar(["Fresh retry", "Carried trace"], [s["fresh_success"] / 72, s["carry_success"] / 72], color=["#94b7ca", "#256b87"])
    axes[1].set_ylim(0, 1.1)
    axes[1].set_ylabel("Frozen requirement completion")
    axes[1].set_title("Cyclic pairs; aggregate within task")
    fig.tight_layout()
    # 图 4 由 engine/negative_result_transfer.py 输出；任务级结果属补充材料，编号改为 S1
    for ext in ("svg", "png", "pdf"):
        fig.savefig(folder / ("figureS1_task_results_carry." + ext), dpi=450, metadata=FIG_METADATA[ext])
    plt.close(fig)


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    protocol, rows, records = collect()
    write(OUTPUT / "executions.json", rows)
    stats = statistics(protocol, rows)
    same = identical_versions(rows)
    carry = carried_trajectory(protocol, rows, records)
    cost = costs(rows)
    plots(stats, carry)
    database(rows, cost)
    result = {"execution_count": len(rows), "primary": stats["primary"], "contribution": stats["contribution"],
              "same_version": same["counts"], "carry": carry["summary"], "cost_groups": cost["groups"],
              "frozen_reproduction": stats["frozen_reproduction"], "model_calls": 0}
    write(OUTPUT / "summary.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "primary"}, ensure_ascii=False, indent=2))
    return result


def complete_all():
    from engine import negative_result_business, negative_result_history, negative_result_trigger
    from engine import negative_result_components, negative_result_requirements, negative_result_evaluation
    from engine import negative_result_transfer, negative_result_independent_review, negative_result_figures
    from engine import negative_result_conditions, negative_result_completion

    replay = read(OUTPUT / "replay/summary.json")
    historical_replay = read(OUTPUT / "replay/history_effects/summary.json")
    if replay["executions"] != 462 or replay["assertions_equal"] != 462 or replay["initial_equal"] != 462:
        raise ValueError("462次官方重放尚未全部复现")
    if historical_replay["executions"] != 63 or historical_replay["assertions_equal"] != 63:
        raise ValueError("历史效果轨迹官方重放尚未完成")
    negative_result_business.main()
    result = run()
    for module in (negative_result_history, negative_result_trigger, negative_result_components,
                   negative_result_requirements, negative_result_evaluation, negative_result_transfer):
        module.run(ROOT, OUTPUT)
    negative_result_independent_review.audit()
    negative_result_conditions.run()
    integrated = negative_result_completion.run()
    negative_result_figures.run(ROOT, OUTPUT)
    result["integrated_research"] = integrated
    result["official_replay"] = {"paid": replay["executions"], "history_effects": historical_replay["executions"],
                                 "paid_assertions_equal": replay["assertions_equal"], "history_assertions_equal": historical_replay["assertions_equal"]}
    result["business_states"] = dict(Counter(r["full_business_state"] for r in read(OUTPUT / "business/execution_reviews.json")))
    write(OUTPUT / "summary.json", result)
    database(read(OUTPUT / "executions.json"), read(OUTPUT / "costs.json"))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="从已完成的官方重放连续重算全部离线分析")
    args = parser.parse_args()
    complete_all() if args.all else run()
