import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from engine.evo_path import (EMPTY, EXP, FX, OUT, P1, P4, TIMING, Ledgers, load_json, read_jsonl)
from engine.scoped_units import save_json

RECOVERY = EXP / "timing_v4_recovery"


def percentile(values, q):
    # 线性插值分位数，与 numpy 默认方法一致
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def proposal_distribution(proposals):
    by_status = Counter()
    funnel = defaultdict(lambda: Counter())
    for row in proposals:
        key = (row["experiment_id"], row["lineage_id"])
        funnel[key]["proposed"] += 1
        funnel[key][row["legality"]] += 1
        if row["status"]:
            funnel[key]["status:" + str(row["status"])] += 1
        by_status[(row["experiment_id"], row["legality"], str(row["status"]))] += 1
    return {"by_status": {"|".join(k): v for k, v in sorted(by_status.items())},
            "funnel": {"|".join(k): dict(v) for k, v in sorted(funnel.items())}}


def lineage_stats(lineage):
    groups = defaultdict(list)
    for row in lineage:
        groups[(row["experiment_id"], row["lineage_id"])].append(row)
    per_lineage = {}
    for (experiment_id, lineage_id), rows in sorted(groups.items()):
        rows.sort(key=lambda r: r["event_order"])
        # 检查点 = 谱系树中内容相异的版本数（集合口径），多分支谱系不按全局事件序去重
        checkpoint_set = {r["candidate_version"] for r in rows if r["candidate_version"]}
        # 链深 = 沿 parent→candidate 边的最长真实继承链（重复加载同一版本不计）
        child_by_candidate = {}
        parents = set()
        for row in rows:
            if row["kind"] in ("edit", "attempt", "version_transition") and row["candidate_version"]:
                child_by_candidate[row["candidate_version"]] = row["parent_version"]
                parents.add(row["parent_version"])
        depth = 0
        for version in checkpoint_set:
            chain, seen, current = 0, set(), version
            while current in child_by_candidate and current not in seen:
                seen.add(current)
                chain += 1
                current = child_by_candidate[current]
            depth = max(depth, chain)
        branch_depths = [depth] if depth else []
        selections = [r for r in rows if r["kind"] == "selection"]
        per_lineage[lineage_id] = {
            "experiment_id": experiment_id,
            "source_family": sorted({r["source_family"] for r in rows if r["source_family"]}),
            "events": len(rows),
            "modifications": sum(1 for r in rows if r["kind"] in ("edit", "attempt", "version_transition")),
            "checkpoints": len(checkpoint_set),
            "max_branch_depth": max(branch_depths) if branch_depths else 0,
            "checkpoint_versions": sorted(checkpoint_set),
            "selection_events": len(selections),
            "accepted": sum(1 for r in selections if r["selection_event"] == "accepted"),
            "rejected_or_abstained": sum(1 for r in selections if r["selection_event"] in ("rejected", "abstained")),
            "format_failures": sum(1 for r in rows if r["selection_event"] == "proposal_format_failure"),
            "final": next((r["stats"] for r in reversed(rows) if r["kind"] == "final"), {}),
        }
    checkpoint_counts = [v["checkpoints"] for v in per_lineage.values()]
    depth_counts = [v["max_branch_depth"] for v in per_lineage.values()]
    return {"per_lineage": per_lineage,
            "lineages_total": len(per_lineage),
            "lineages_ge3_checkpoints": sum(1 for c in checkpoint_counts if c >= 3),
            "lineages_ge6_checkpoints": sum(1 for c in checkpoint_counts if c >= 6),
            "lineages_ge3_by_chain_depth": sum(1 for c in depth_counts if c >= 3),
            "lineages_ge6_by_chain_depth": sum(1 for c in depth_counts if c >= 6)}


def expression_stats(expressions):
    by_state = Counter()
    by_unit = defaultdict(Counter)
    e_component = {"executions": set(), "rows": 0, "affected": 0, "units": set()}
    for row in expressions:
        by_state[(row["experiment_id"], row["state"])] += 1
        by_unit[row["repair_identity"]][row["state"]] += 1
        if row.get("component") == "E":
            e_component["executions"].add(row["execution_id"])
            e_component["rows"] += 1
            e_component["affected"] += bool(row["affected"])
            e_component["units"].add(row["repair_identity"])
    return {"by_state": {"|".join(k): v for k, v in sorted(by_state.items())},
            "by_unit": {k: dict(v) for k, v in sorted(by_unit.items())},
            "e_component": {"executions": len(e_component["executions"]), "rows": e_component["rows"],
                            "affected": e_component["affected"], "units": sorted(e_component["units"])}}


def timing_summary():
    analysis = load_json(RECOVERY / "analysis.json")
    sources = analysis["sources"]
    equal_gn = [s["source"] for s in sources if s["means"]["G"] == s["means"]["N"]]
    return {"sources": {s["source"]: {"category": s["category"], "means": s["means"], "L-D": s["L-D"]}
                        for s in sources},
            "sources_G_equals_N": equal_gn,
            "primary": analysis["primary"],
            "total_paid_audit": analysis["total_paid_audit"]}


def damage_screen(lineage, expressions, outcomes, proposals):
    """机器筛选损伤候选：只聚合客观事件，语义判定留给研究者逐例核对。"""
    candidates = []

    # 1. E 组件到达但 effects 为空
    e_rows = [r for r in expressions if r.get("component") == "E"]
    if e_rows:
        by_version = Counter((r["version"], r["state"]) for r in e_rows)
        candidates.append({"type": "组件到达后无影响", "id": "e_component_empty_effects",
                           "evidence": sorted({r["evidence_path"] for r in e_rows})[:5],
                           "detail": {"rows": len(e_rows), "executions": len({r["execution_id"] for r in e_rows}),
                                      "affected_rows": sum(1 for r in e_rows if r["affected"]),
                                      "by_version": {"|".join((v[:8], s)): c for (v, s), c in by_version.items()}}})

    # 2. timing：路由选中但知识未进入任何请求（已适用未到达）
    timing_unreached = [r for r in expressions if r["experiment_id"] == "timing_v4"
                        and r["state"] == "applicable_not_reached"]
    if timing_unreached:
        by_source = Counter(r["repair_identity"] for r in timing_unreached)
        candidates.append({"type": "知识已适用未到达", "id": "timing_selected_not_visible",
                           "evidence": [r["evidence_path"] for r in timing_unreached[:8]],
                           "detail": {"rows": len(timing_unreached), "by_source": dict(by_source)}})

    # 3. research 谱系内：同任务先成功后失败（功能继承损伤候选）
    outcomes_by_task = defaultdict(list)
    for row in outcomes:
        if row["kind"] == "execution" and row["experiment_id"] == "research" and row["task_role"] == "learning":
            outcomes_by_task[(row["arm"], row["task"])].append(row)
    flips = []
    for (arm, task), rows in outcomes_by_task.items():
        rows.sort(key=lambda r: r["recorded_at"] or 0)
        for earlier, later in zip(rows, rows[1:]):
            if earlier["strict_success"] and not later["strict_success"]:
                flips.append({"arm": arm, "task": task,
                              "before": {"version": earlier["version"], "execution_id": earlier["execution_id"],
                                         "recorded_at": earlier["recorded_at"]},
                              "after": {"version": later["version"], "execution_id": later["execution_id"],
                                        "recorded_at": later["recorded_at"]}})
    if flips:
        candidates.append({"type": "同任务版本间功能退化", "id": "research_learning_regressions",
                           "evidence": [], "detail": {"flips": flips}})

    # 4. research 谱系内单元在后续版本被移除或改写
    lineage_groups = defaultdict(list)
    for row in lineage:
        if row["experiment_id"] == "research" and row["kind"] == "edit":
            lineage_groups[row["lineage_id"]].append(row)
    dropped = []
    for lineage_id, rows in lineage_groups.items():
        rows.sort(key=lambda r: r["event_order"])
        previous = set()
        for row in rows:
            units = set(row["stats"].get("units", []))
            lost = previous - units
            if lost:
                dropped.append({"lineage_id": lineage_id, "at_event": row["event_order"],
                                "version": row["candidate_version"], "dropped_units": sorted(lost)})
            previous = units
    if dropped:
        candidates.append({"type": "单元在后续版本被移除", "id": "unit_dropped_between_versions",
                           "evidence": [], "detail": {"events": dropped}})

    # 5. P4 提议漏斗流失
    p4 = [r for r in proposals if r["experiment_id"] == "ecai_p4"]
    status = Counter(r["status"] for r in p4)
    candidates.append({"type": "P4 提议漏斗", "id": "p4_funnel",
                       "evidence": ["experiments/ecai_2027/scope_execution_ab/p4_candidates.csv"],
                       "detail": {"status": dict(status)}})

    # 6. P1 无编辑重复的标签翻转（自然波动背景）
    p1_flips = {"rows": 0, "flips": 0, "tasks": []}
    paired = P1 / "paired_results.csv"
    if paired.exists():
        with paired.open(encoding="utf-8-sig") as stream:
            for record in csv.DictReader(stream):
                p1_flips["rows"] += 1
                if record.get("B0") != record.get("B0prime"):
                    p1_flips["flips"] += 1
                    p1_flips["tasks"].append(record["task"])
    candidates.append({"type": "无编辑重复标签翻转", "id": "p1_noise_flips",
                       "evidence": ["experiments/ecai_2027/mechanism/paired_results.csv"],
                       "detail": p1_flips})
    return candidates


def cost_parameters(outcomes):
    # 完整修改周期：提议 + 来源执行 + 门禁与验证；只有走完验证的周期计入 P75
    cycles = []
    with (P4 / "p4_candidates.csv").open(encoding="utf-8-sig") as stream:
        for record in csv.DictReader(stream):
            def money(key):
                value = record.get(key) or ""
                return json.loads(value).get("cash_cny") if value.startswith("{") else None
            proposal = money("proposal_cost")
            source = money("source_cost")
            selective = money("selective_validation_cost")
            full = money("full_validation_cost")
            parts = [v for v in (proposal, source, selective, full) if v is not None]
            complete = selective is not None and full is not None
            cycles.append({"slot": record["id"], "source": record["source"], "status": record["status"],
                           "complete": complete, "total_cny": sum(parts)})
    ledgers = Ledgers()
    p1 = ledgers.members["ecai_p1"]
    p1_cycles = []
    with sqlite3.connect("file:%s?mode=ro" % p1.path, uri=True) as db:
        for cid in ("C1", "C2", "C3"):
            cost = db.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE phase LIKE ? OR phase LIKE ?",
                              ("learning/%s/%%" % cid, "evaluation/%s/%%" % cid)).fetchone()[0]
            billed = db.execute("SELECT COALESCE(SUM(billed),0) FROM calls WHERE phase LIKE ? OR phase LIKE ?",
                                ("learning/%s/%%" % cid, "evaluation/%s/%%" % cid)).fetchone()[0]
            p1_cycles.append({"candidate": cid, "cost_cny": cost * FX, "billed_cny": billed * FX})
    complete_cycles = [c["total_cny"] for c in cycles if c["complete"]]
    p75 = percentile(complete_cycles + [c["cost_cny"] for c in p1_cycles], 0.75)
    b_budget = 3 * p75 if p75 is not None else None

    formal = [r for r in outcomes if r["kind"] == "execution" and r["in_formal_denominator"]]
    eval_costs = defaultdict(list)
    for row in formal:
        cost = row["cost_cny_reference"] or (row["cost_usd"] or 0) * FX
        if cost > 0:
            eval_costs[row["experiment_id"]].append(cost)
    pooled = [c for costs in eval_costs.values() for c in costs]
    c_bar = sum(pooled) / len(pooled) if pooled else None
    per_experiment = {k: {"n": len(v), "mean_cny": sum(v) / len(v)} for k, v in sorted(eval_costs.items())}
    n, e = 8, 2
    budget = {"assumed": {"B": 10.0, "c_bar": 0.30,
                          "main": 2 * n * 10.0 + 48 * n * 0.30, "external": 2 * (2 * 10.0 + 48 * 0.30)},
              "measured": None}
    if b_budget is not None and c_bar is not None:
        main = 2 * n * b_budget + 48 * n * c_bar
        external = e * (2 * b_budget + 48 * c_bar)
        budget["measured"] = {"B": b_budget, "c_bar": c_bar, "n": n, "main": main,
                              "external": external, "total": main + external}
    return {"modification_cycles_p4": cycles, "modification_cycles_p1": p1_cycles,
            "complete_cycle_cny": {"values": complete_cycles + [c["cost_cny"] for c in p1_cycles],
                                   "p75": p75, "B_times_3": b_budget},
            "evaluation_cost": {"per_experiment": per_experiment, "pooled_mean_cny": c_bar,
                                "executions": len(pooled)},
            "budget": budget}


def analyze(_args):
    lineage = read_jsonl(OUT / "lineage.jsonl")
    proposals = read_jsonl(OUT / "proposal.jsonl")
    expressions = read_jsonl(OUT / "expression.jsonl")
    outcomes = read_jsonl(OUT / "outcome.jsonl")
    analysis = {"proposal": proposal_distribution(proposals),
                "lineage": lineage_stats(lineage),
                "expression": expression_stats(expressions),
                "timing": timing_summary(),
                "outcome": {"rows": len(outcomes),
                            "by_experiment": dict(Counter(r["experiment_id"] for r in outcomes)),
                            "formal": sum(1 for r in outcomes if r["in_formal_denominator"])}}
    save_json(OUT / "analysis.json", analysis)
    damages = damage_screen(lineage, expressions, outcomes, proposals)
    save_json(OUT / "damage_candidates.json", damages)
    costs = cost_parameters(outcomes)
    save_json(OUT / "cost_parameters.json", costs)
    print(json.dumps({"proposal_status": analysis["proposal"]["by_status"],
                      "lineages": {k: {kk: v[kk] for kk in ("checkpoints", "modifications", "accepted")}
                                   for k, v in analysis["lineage"]["per_lineage"].items()},
                      "ge3": analysis["lineage"]["lineages_ge3_checkpoints"],
                      "ge6": analysis["lineage"]["lineages_ge6_checkpoints"],
                      "ge3_depth": analysis["lineage"]["lineages_ge3_by_chain_depth"],
                      "ge6_depth": analysis["lineage"]["lineages_ge6_by_chain_depth"],
                      "expression_states": analysis["expression"]["by_state"],
                      "budget": costs["budget"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["analyze"])
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()
