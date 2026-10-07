import asyncio

from engine.activation import validation_plan, reuse_evidence
from engine.evolution import paired_selection


async def run_mechanism(records, after, plan, tasks, repeats, execute, label):
    executed, reused, executed_keys, reused_keys, cost, outcomes, reused_outcomes = [], [], [], [], 0.0, {}, {}

    async def one(base, item):
        return reuse_evidence(base, after, item) if not item["affected"] else await execute(base["task"], base["repeat"], label)
    candidate = list(await asyncio.gather(*(one(b, i) for b, i in zip(records, plan, strict=True))))
    for base, item, record in zip(records, plan, candidate, strict=True):
        key = base["task"] + "#" + str(base["repeat"])
        if not item["affected"]:
            reused.append(base["task"])
            reused_keys.append(key)
            reused_outcomes[key] = bool(record["success"])
        else:
            executed.append(base["task"])
            executed_keys.append(key)
            cost += record["cost_usd"]
        outcomes[key] = bool(record["success"])
    comparison = paired_selection(records, candidate, tasks, repeats)
    return {"decision": comparison["decision"], "wins": comparison["wins"], "losses": comparison["losses"],
            "p_value": comparison["p_value"], "executed_tasks": executed, "reused_tasks": reused,
            "executed_keys": executed_keys, "reused_keys": reused_keys,
            "executed_count": len(executed), "reused_count": len(reused), "cost_usd": cost,
            "outcomes": outcomes, "reused_outcomes": reused_outcomes}


async def retest_comparison(before, after, records, experiment_version, tasks, repeats, execute):
    """同一批冻结候选上比较全局重测与按影响范围重测。

    两侧共用同一份历史证据与同一个候选版本，差别只在哪些任务重新执行。
    证据复用的准确性只统计"按影响范围重测复用了证据、全局重测重新执行"的题：
    其余题两侧都没有复用，拿它们比较只会把确定性重跑的稳定性误报成复用准确性。
    """
    scoped_plan = validation_plan(before, after, records, experiment_version, global_validation=False)
    global_plan = validation_plan(before, after, records, experiment_version, global_validation=True)
    scoped = await run_mechanism(records, after, scoped_plan, tasks, repeats, execute, "scoped_retest")
    global_ = await run_mechanism(records, after, global_plan, tasks, repeats, execute, "global_retest")
    shared = sorted(set(scoped["reused_outcomes"]) & set(global_["outcomes"]))
    agreements = [{"task": key.rsplit("#", 1)[0], "repeat": int(key.rsplit("#", 1)[1]),
                   "reused_success": scoped["reused_outcomes"][key], "rerun_success": global_["outcomes"][key]}
                  for key in shared if scoped["reused_outcomes"][key] != global_["outcomes"][key]]
    return {"tasks": tasks, "repeats": repeats, "scoped": scoped, "global": global_,
            "reuse_accuracy": {"compared": len(shared), "matched": len(shared) - len(agreements),
                               "rate": (len(shared) - len(agreements)) / len(shared) if shared else None,
                               "reused_keys": shared, "disagreements": agreements},
            "consistent_decision": scoped["decision"] == global_["decision"],
            "execution_saving": global_["executed_count"] - scoped["executed_count"],
            "cost_saving_usd": global_["cost_usd"] - scoped["cost_usd"]}
