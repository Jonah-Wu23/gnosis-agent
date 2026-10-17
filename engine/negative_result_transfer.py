import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np


PAID = Path("experiments/feedback_chain/paid")
PROTOCOL = Path("experiments/feedback_chain/frozen/execution_v1.json")
LEDGER = Path("state/feedback_chain_ledger.sqlite")
HISTORY = Path("experiments/negative_result_offline/history")
BUSINESS = Path("experiments/negative_result_offline/business")
CARRY = Path("experiments/negative_result_offline/carry.json")
COSTS = Path("experiments/negative_result_offline/costs.json")
ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
EXECUTION_ARMS = ("source",) + ARMS + tuple(arm + "_source" for arm in ARMS) + ("ancestor", "carry")
CARRY_PREFIX = "Review your previous execution and complete the original task in the fresh environment.\n"
CARRY_POINTER = "/wire_trace/0/request/3/content"
SEGMENT_GAP_SECONDS = 600
FX = 7.1
# 去掉各后端写入的生成时间，使同一份数据重复运行得到逐字节相同的图
FIG_METADATA = {"svg": {"Date": None}, "png": {}, "pdf": {"CreationDate": None, "ModDate": None}}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def table(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ref(path, pointer=""):
    return {"file": str(path).replace("\\", "/"), "pointer": pointer}


def mean(values):
    return sum(values) / len(values) if values else None


def execution_record(paid, execution_id):
    for arm in EXECUTION_ARMS:
        path = paid / "executions" / arm / (execution_id + ".json")
        if path.is_file():
            return read(path)
    raise ValueError("执行记录缺失: " + execution_id)


def collect(paid, reviews):
    facts = {}
    for review in reviews:
        identity = review["execution_id"]
        record = execution_record(paid, identity)
        facts[identity] = {"actions": len(record["actual_tool_actions"]),
                           "failed_actions": sum(bool(action.get("failed")) for action in record["actual_tool_actions"]),
                           "cost_usd": record["cost_usd"], "version": record["version"]}
        if review["arm"] in ("ancestor", "carry"):
            facts[identity]["initial_state"] = digest(record["initial_world"])
            facts[identity]["assertions"] = record["assertions"]
        if review["arm"] == "carry":
            content = record["wire_trace"][0]["request"][3]["content"]
            if not content.startswith(CARRY_PREFIX):
                raise ValueError("携带轨迹输入协议不一致: " + identity)
            evidence = json.loads(content[len(CARRY_PREFIX):])
            roles = Counter(message["role"] for message in evidence["completion"])
            facts[identity]["carried"] = {"fields": sorted(evidence), "assertions": evidence["assertions"],
                                          "tool_results": roles["tool"], "assistant_messages": roles["assistant"]}
    return facts


def ledger_executions(calls):
    grouped = defaultdict(list)
    for call in calls:
        if "/execution/" in call["phase"]:
            grouped[call["phase"].rsplit("/", 1)[-1]].append(call)
    index = {}
    for execution_id, values in grouped.items():
        values.sort(key=lambda call: (call["at"], call["id"]))
        segments, previous = [], None
        for call in values:
            if previous is None or call["at"] - previous > SEGMENT_GAP_SECONDS:
                segments.append([])
            segments[-1].append(call)
            previous = call["at"]
        index[execution_id] = {"calls": len(values), "settled_calls": sum(call["cost"] is not None for call in values),
                               "segments": len(segments), "segment_sizes": [len(segment) for segment in segments],
                               "segment_settled_cny": [sum(call["cost"] or 0 for call in segment if call["cost"] is not None) * FX for segment in segments],
                               "cny": sum(call["cost"] or 0 for call in values) * FX,
                               "billed_cny": sum((call["billed"] if call["billed"] is not None else (call["cost"] or 0)) for call in values) * FX,
                               "unsettled_reserved_cny": sum(call["reserved"] for call in values if call["cost"] is None) * FX}
    return index


def ledger_calls(root):
    connection = sqlite3.connect((root / LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    values = [dict(row) for row in connection.execute(
        "SELECT id,phase,reserved,cost,billed,input_tokens,output_tokens,cached_tokens,at FROM calls ORDER BY id")]
    connection.close()
    return values


def cost_groups(calls):
    grouped = defaultdict(list)
    for call in calls:
        group = "task_execution" if "/execution/" in call["phase"] else "organization" if "/organize" in call["phase"] else "editing"
        grouped[group].append(call)
    return {group: {"calls": len(values), "settled_calls": sum(call["cost"] is not None for call in values),
                    "reference_cny": sum(call["cost"] or 0 for call in values) * FX,
                    "billed_cny": sum((call["billed"] if call["billed"] is not None else (call["cost"] or 0)) for call in values) * FX,
                    "unsettled_reserved_cny": sum(call["reserved"] for call in values if call["cost"] is None) * FX}
            for group, values in grouped.items()}


def review_index(reviews):
    migration, source = {}, {}
    for row in reviews:
        if row["scope"] == "migration":
            migration[(row["domain"], row["arm"], row["task"], row["repeat"])] = row
        else:
            source[(row["domain"], row["arm"])] = row
    return migration, source


def classes(review):
    counts = defaultdict(lambda: {"applicable": 0, "passed": 0})
    for item in review["requirements"]:
        counts[item["req_class"]]["applicable"] += 1
        counts[item["req_class"]]["passed"] += int(item["passed"])
    return counts


def literal_walk(node, values):
    if isinstance(node, dict):
        for value in node.values():
            literal_walk(value, values)
    elif isinstance(node, list):
        for value in node:
            literal_walk(value, values)
    elif isinstance(node, str):
        values.add(node)
    elif isinstance(node, (int, float)):
        values.add(str(node))


def source_literals(review):
    # 字面量只从失败要求的结构化参数中取；来源题的对象与取值由断言参数本身给出。
    values = set()
    for item in review["requirements"]:
        if item["passed"]:
            continue
        text = item["requirement"]
        start = text.find("{")
        if start >= 0:
            literal_walk(json.loads(text[start:]), values)
    return sorted(values)


def distinctive(literals):
    # 可辨识字面量：带数字、邮箱、金额或由多个词构成，用于判断候选是否携带来源题对象与取值。
    return [literal for literal in literals
            if any(character.isdigit() for character in literal) or "@" in literal or "$" in literal or " " in literal]


def source_layer(paid, protocol, source_reviews, ledger, slots, blocks, diffs, facts):
    result = []
    for block in protocol["blocks"]:
        domain = block["domain"]
        first = source_reviews[(domain, "source")]
        literals = source_literals(first)
        retests = []
        for arm in ARMS:
            review = source_reviews[(domain, arm + "_source")]
            candidate = blocks[domain]["candidates"][arm]
            slot = slots[(domain, arm)]
            added = [line for item in diffs if (item["domain"], item["arm"]) == (domain, arm)
                     for line in (item.get("added_lines", []) or [item.get("child_text", "")])]
            carried = sorted({literal for literal in literals
                              if any(literal in line or literal.replace(",", "") in line for line in added)})
            if facts[review["execution_id"]]["version"] != candidate["version"]:
                raise ValueError("来源复测版本与槽位部署版本不一致: " + domain + "/" + arm)
            retests.append({"arm": arm, "slot_status": slot["status"], "candidate_status": candidate["status"],
                            "candidate_deployed": not slot["deployed_ancestor"], "deployed_version": candidate["version"],
                            "retest_version": facts[review["execution_id"]]["version"],
                            "retest_matches_slot_version": True,
                            "strict_success": review["strict_success"], "business_success": review["full_business_success"],
                            "failed_requirements": [item["id"] for item in review["requirements"] if not item["passed"]],
                            "actions": facts[review["execution_id"]]["actions"],
                            "cny": ledger[review["execution_id"]]["cny"],
                            "execution_id": review["execution_id"],
                            "carried_source_literals": carried,
                            "carried_distinctive_literals": distinctive(carried),
                            "units_touched": sorted({item["unit"] for item in diffs if (item["domain"], item["arm"]) == (domain, arm)}),
                            "reference": ref(paid / "executions" / (arm + "_source") / (review["execution_id"] + ".json"))})
        deployed = [retest for retest in retests if retest["candidate_deployed"]]
        result.append({"domain": domain, "source_task": block["source"], "source_execution_id": first["execution_id"],
                       "source_version": facts[first["execution_id"]]["version"],
                       "source_strict_success": first["strict_success"], "source_business_success": first["full_business_success"],
                       "source_actions": facts[first["execution_id"]]["actions"],
                       "source_cny": ledger[first["execution_id"]]["cny"],
                       "source_failed_requirements": [{"id": item["id"], "requirement": item["requirement"], "req_class": item["req_class"]}
                                                      for item in first["requirements"] if not item["passed"]],
                       "source_literals": literals, "retests": retests,
                       "deployed_candidates": len(deployed), "same_version_retests": len(retests) - len(deployed),
                       "retest_strict_change": sum(retest["strict_success"] for retest in retests) - len(retests) * int(first["strict_success"]),
                       "retest_business_change": sum(retest["business_success"] for retest in retests) - len(retests) * int(first["full_business_success"]),
                       "deployed_strict_change": sum(retest["strict_success"] for retest in deployed) - len(deployed) * int(first["strict_success"]),
                       "deployed_business_change": sum(retest["business_success"] for retest in deployed) - len(deployed) * int(first["full_business_success"]),
                       "reference": ref(paid / "blocks" / (domain + ".json"))})
    return result


def migration_layer(protocol, reviews, source_reviews, ledger, blocks, slots, facts):
    candidates, per_arm, per_class = [], defaultdict(list), defaultdict(lambda: defaultdict(
        lambda: {"applicable": 0, "passed": 0, "executions": 0, "executions_all_passed": 0}))
    for block in protocol["blocks"]:
        domain = block["domain"]
        peer = {task["task"]: task for task in block["migration"]}
        ancestor = {(review["task"], review["repeat"]): review for review in reviews.values()
                    if review["domain"] == domain and review["arm"] == "ancestor"}
        for arm in ARMS:
            candidate = blocks[domain]["candidates"][arm]
            slot = slots[(domain, arm)]
            runs = [review for review in reviews.values() if review["domain"] == domain and review["arm"] == arm]
            pairs, per_task = [], {}
            counters = Counter()
            for run in sorted(runs, key=lambda row: (row["task"], row["repeat"])):
                reference = ancestor[(run["task"], run["repeat"])]
                pair = {"task": run["task"], "repeat": run["repeat"],
                        "execution_id": run["execution_id"], "ancestor_execution_id": reference["execution_id"],
                        "business": int(run["full_business_success"]), "ancestor_business": int(reference["full_business_success"]),
                        "strict": int(run["strict_success"]), "ancestor_strict": int(reference["strict_success"]),
                        "frozen": int(run["frozen_success"]), "ancestor_frozen": int(reference["frozen_success"]),
                        "actions": facts[run["execution_id"]]["actions"], "ancestor_actions": facts[reference["execution_id"]]["actions"],
                        "failed_actions": facts[run["execution_id"]]["failed_actions"],
                        "ancestor_failed_actions": facts[reference["execution_id"]]["failed_actions"],
                        "necessary_present": any(item["req_class"] == "necessary" for item in run["requirements"]),
                        "necessary_all_passed": all(item["passed"] for item in run["requirements"] if item["req_class"] == "necessary"),
                        "prohibited_present": any(item["req_class"] == "prohibited" for item in run["requirements"]),
                        "prohibited_all_passed": all(item["passed"] for item in run["requirements"] if item["req_class"] == "prohibited"),
                        "cny": ledger[run["execution_id"]]["cny"], "ancestor_cny": ledger[reference["execution_id"]]["cny"]}
                pairs.append(pair)
                task = per_task.setdefault(run["task"], {"business": 0, "ancestor_business": 0, "gains": 0, "losses": 0})
                task["business"] += pair["business"]
                task["ancestor_business"] += pair["ancestor_business"]
                if pair["business"] and not pair["ancestor_business"]:
                    task["gains"] += 1
                    counters["business_gains"] += 1
                if pair["ancestor_business"] and not pair["business"]:
                    task["losses"] += 1
                    counters["business_losses"] += 1
                counters["strict_gains"] += int(pair["strict"] and not pair["ancestor_strict"])
                counters["strict_losses"] += int(pair["ancestor_strict"] and not pair["strict"])
                if pair["necessary_present"]:
                    space = "fewer" if pair["actions"] < pair["ancestor_actions"] else "more"
                    outcome = "necessary_complete" if pair["necessary_all_passed"] else "necessary_miss"
                    counters[space + "_actions_" + outcome] += 1
                for req_class, counts in classes(run).items():
                    entry = per_class[arm][req_class]
                    entry["applicable"] += counts["applicable"]
                    entry["passed"] += counts["passed"]
                    entry["executions"] += 1
                    entry["executions_all_passed"] += int(counts["applicable"] > 0 and counts["passed"] == counts["applicable"])
            retest_id = source_reviews[(domain, arm + "_source")]["execution_id"]
            total = slot["generation_cost_cny"] + ledger[retest_id]["cny"] + sum(pair["cny"] for pair in pairs)
            record = {"domain": domain, "arm": arm, "slot_status": slot["status"], "candidate_status": candidate["status"],
                      "candidate_deployed": not slot["deployed_ancestor"], "deployed_version": candidate["version"],
                      "runs": len(pairs), "business_successes": sum(pair["business"] for pair in pairs),
                      "ancestor_business_successes": sum(pair["ancestor_business"] for pair in pairs),
                      "frozen_successes": sum(pair["frozen"] for pair in pairs),
                      "ancestor_frozen_successes": sum(pair["ancestor_frozen"] for pair in pairs),
                      "strict_successes": sum(pair["strict"] for pair in pairs),
                      "ancestor_strict_successes": sum(pair["ancestor_strict"] for pair in pairs),
                      "business_gains": counters["business_gains"], "business_losses": counters["business_losses"],
                      "business_net": counters["business_gains"] - counters["business_losses"],
                      "strict_gains": counters["strict_gains"], "strict_losses": counters["strict_losses"],
                      "strict_net": counters["strict_gains"] - counters["strict_losses"],
                      "actions": sum(pair["actions"] for pair in pairs), "ancestor_actions": sum(pair["ancestor_actions"] for pair in pairs),
                      "failed_actions": sum(pair["failed_actions"] for pair in pairs),
                      "ancestor_failed_actions": sum(pair["ancestor_failed_actions"] for pair in pairs),
                      "fewer_actions_with_necessary_miss": counters["fewer_actions_necessary_miss"],
                      "fewer_actions_necessary_complete": counters["fewer_actions_necessary_complete"],
                      "more_actions_necessary_complete": counters["more_actions_necessary_complete"],
                      "more_actions_with_necessary_miss": counters["more_actions_necessary_miss"],
                      "generation_cny": slot["generation_cost_cny"], "source_retest_cny": ledger[retest_id]["cny"],
                      "migration_cny": sum(pair["cny"] for pair in pairs),
                      "ancestor_cny": sum(pair["ancestor_cny"] for pair in pairs), "candidate_total_cny": total,
                      "cost_per_net_gain_cny": total / (counters["business_gains"] - counters["business_losses"])
                      if counters["business_gains"] > counters["business_losses"] else None,
                      "per_task": [dict(task=task, shared_structure=peer[task]["shared_structure"], **value)
                                   for task, value in sorted(per_task.items())],
                      "pairs": pairs}
            candidates.append(record)
            per_arm[arm].append(record)
    return candidates, per_arm, per_class


def carry_layer(protocol, reviews, ledger, carry_json, facts):
    pairs = []
    domain_of = {task: entry["domain"] for task, entry in protocol["plan"]["tasks"].items()}
    for task in protocol["plan"]["heldout"]:
        for repeat in range(3):
            domain = domain_of[task]
            first = reviews[(domain, "ancestor", task, repeat)]
            fresh = reviews[(domain, "ancestor", task, (repeat + 1) % 3)]
            carried = reviews[(domain, "carry", task, repeat)]
            entry = facts[carried["execution_id"]]["carried"]
            pairs.append({"task": task, "repeat": repeat,
                          "first_id": first["execution_id"], "fresh_id": fresh["execution_id"], "carry_id": carried["execution_id"],
                          "first_frozen": int(first["frozen_success"]), "fresh_frozen": int(fresh["frozen_success"]),
                          "carry_frozen": int(carried["frozen_success"]),
                          "first_business": int(first["full_business_success"]), "fresh_business": int(fresh["full_business_success"]),
                          "carry_business": int(carried["full_business_success"]),
                          "first_strict": int(first["strict_success"]), "fresh_strict": int(fresh["strict_success"]),
                          "carry_strict": int(carried["strict_success"]),
                          "first_cny": ledger[first["execution_id"]]["cny"], "fresh_cny": ledger[fresh["execution_id"]]["cny"],
                          "carry_cny": ledger[carried["execution_id"]]["cny"],
                          "first_actions": facts[first["execution_id"]]["actions"], "fresh_actions": facts[fresh["execution_id"]]["actions"],
                          "carry_actions": facts[carried["execution_id"]]["actions"],
                          "carry_failed_actions": facts[carried["execution_id"]]["failed_actions"],
                          "same_initial_state": (facts[first["execution_id"]]["initial_state"] == facts[carried["execution_id"]]["initial_state"]
                                                 == facts[fresh["execution_id"]]["initial_state"]),
                          "initial_state_digest": facts[first["execution_id"]]["initial_state"],
                          "evidence_fields": entry["fields"],
                          "carried_tool_results": entry["tool_results"], "carried_assistant_messages": entry["assistant_messages"],
                          "carried_assertions_match_first": entry["assertions"] == facts[first["execution_id"]]["assertions"],
                          "carried_failed_assertions": [{"type": item["type"], "params": item["params"]}
                                                        for item in entry["assertions"] if not item["passed"]],
                          "carry_failed_requirements": [item["id"] for item in carried["requirements"] if not item["passed"]],
                          "reference": ref(Path("experiments/feedback_chain/paid/executions/carry") / (carried["execution_id"] + ".json"),
                                           CARRY_POINTER)})
    situations = {}
    for name, selector in (("all", lambda pair: True), ("first_failed", lambda pair: not pair["first_frozen"]),
                           ("first_passed", lambda pair: bool(pair["first_frozen"]))):
        selected = [pair for pair in pairs if selector(pair)]
        situation = {"pairs": len(selected)}
        for metric in ("frozen", "business"):
            fresh_success = sum(pair["fresh_" + metric] for pair in selected)
            carry_success = sum(pair["carry_" + metric] for pair in selected)
            fresh_total = sum(pair["first_cny"] + pair["fresh_cny"] for pair in selected)
            carry_total = sum(pair["first_cny"] + pair["carry_cny"] for pair in selected)
            net = carry_success - fresh_success
            situation[metric] = {"fresh_successes": fresh_success, "carry_successes": carry_success, "extra_successes": net,
                                 "fresh_total_cny": fresh_total, "carry_total_cny": carry_total,
                                 "incremental_total_cny": carry_total - fresh_total,
                                 "fresh_cny_per_pair": fresh_total / len(selected), "carry_cny_per_pair": carry_total / len(selected),
                                 "incremental_cny_per_pair": (carry_total - fresh_total) / len(selected),
                                 "fresh_cny_per_success": fresh_total / fresh_success if fresh_success else None,
                                 "carry_cny_per_success": carry_total / carry_success if carry_success else None,
                                 "incremental_cny_per_extra_success": (carry_total - fresh_total) / net if net else None}
        situations[name] = situation
    frozen = carry_json["summary"]
    reproduction = {"pair_counts": all(situations[name]["pairs"] == frozen[name]["pairs"] for name in ("all", "first_failed", "first_passed")),
                    "all_fresh_cny_per_pair": abs(situations["all"]["frozen"]["fresh_cny_per_pair"] - frozen["all"]["fresh_cny_per_success"] * frozen["all"]["fresh_success"] / 72) < 1e-6,
                    "all_carry_cny_per_pair": abs(situations["all"]["frozen"]["carry_cny_per_pair"] - frozen["all"]["carry_cny_per_success"] * frozen["all"]["carry_success"] / 72) < 1e-6,
                    "all_incremental_cny_per_pair_is_0_0086": abs(situations["all"]["frozen"]["incremental_cny_per_pair"] - 0.0085545) < 1e-6,
                    "first_failed_cny_per_extra_success": abs(situations["first_failed"]["frozen"]["incremental_cny_per_extra_success"]
                                                              - frozen["first_failed"]["incremental_cny_per_extra_success"]) < 1e-9,
                    "first_passed_carry_cny_per_success": abs(situations["first_passed"]["frozen"]["carry_cny_per_success"]
                                                              - frozen["first_passed"]["carry_cny_per_success"]) < 1e-9,
                    "first_failed_per_success_pair": abs(situations["first_failed"]["frozen"]["fresh_cny_per_success"]
                                                         - frozen["first_failed"]["fresh_cny_per_success"]) < 1e-9}
    if not all(reproduction.values()):
        raise ValueError("冻结携带轨迹费用未复现")
    extra = [pair for pair in pairs if not pair["fresh_frozen"] and pair["carry_frozen"]]
    same = [pair for pair in pairs if pair["fresh_frozen"] and pair["carry_frozen"]]
    reuse = Counter(pair["first_id"] for pair in pairs) + Counter(pair["fresh_id"] for pair in pairs)
    return {"pairs": pairs, "situations": situations, "reproduction": reproduction,
            "input": {"prefix": CARRY_PREFIX.strip(), "pointer": CARRY_POINTER, "carried_fields": pairs[0]["evidence_fields"],
                      "assertions_match_first_in_pairs": sum(pair["carried_assertions_match_first"] for pair in pairs),
                      "carried_tool_result_messages": mean([pair["carried_tool_results"] for pair in pairs]),
                      "carried_assistant_messages": mean([pair["carried_assistant_messages"] for pair in pairs]),
                      "availability": [
                          {"item": "任务事实", "carried_fields": ["task", "domain", "prompt", "version"],
                           "runtime": "实际运行可取得"},
                          {"item": "前次动作", "carried_fields": ["completion 中的 assistant 消息与工具调用", "interventions", "boundary_trace", "active_extensions"],
                           "runtime": "实际运行可取得"},
                          {"item": "工具结果", "carried_fields": ["completion 中的 tool 消息"],
                           "runtime": "实际运行可取得"},
                          {"item": "断言反馈及其取值", "carried_fields": ["assertions 的 type、params、passed"],
                           "runtime": "本实验评价器反馈；真实部署须提供同等可用评价器"},
                          {"item": "运行结局记录", "carried_fields": ["success", "partial_credit", "cost_usd", "termination_status", "execution_error", "harness_defects", "first_effect_boundaries", "execution_id", "repeat"],
                           "runtime": "运行期记录可取得"}]},
            "initial_state": {"pairs_with_identical_initial_state": sum(pair["same_initial_state"] for pair in pairs),
                              "distinct_initial_state_digests": len({pair["initial_state_digest"] for pair in pairs}),
                              "side_effect_check": "首跑、同题重跑与携带执行的初态摘要逐对相等，前次世界写入未进入新初态"},
            "cyclic_pairing": {"unique_first_executions": len({pair["first_id"] for pair in pairs}),
                               "unique_fresh_executions": len({pair["fresh_id"] for pair in pairs}),
                               "max_reuse_times": max(reuse.values()),
                               "first_equals_fresh_within_pair": sum(pair["first_id"] == pair["fresh_id"] for pair in pairs),
                               "structure": "每题 3 次祖先执行各出现两次，一次作首跑、一次作另一队的重跑；配对按原计划保留"},
            "extra_successes": {"count": len(extra),
                                "cases": [{"task": pair["task"], "repeat": pair["repeat"], "fresh_id": pair["fresh_id"],
                                           "carry_id": pair["carry_id"], "carried_tool_results": pair["carried_tool_results"],
                                           "carried_failed_assertions": pair["carried_failed_assertions"],
                                           "carry_actions": pair["carry_actions"], "carry_cny": pair["carry_cny"],
                                           "reference": pair["reference"]} for pair in extra]},
            "same_result_pairs": {"count": len(same), "shared_identity": "新上下文重跑与携带轨迹执行均通过冻结要求；首跑结果另列",
                                  "carry_business_failures": sum(not pair["carry_business"] for pair in same),
                                  "carry_failed_requirements": sum(len(pair["carry_failed_requirements"]) for pair in same),
                                  "mean_first_actions": mean([pair["first_actions"] for pair in same]),
                                  "mean_fresh_actions": mean([pair["fresh_actions"] for pair in same]),
                                  "mean_carry_actions": mean([pair["carry_actions"] for pair in same]),
                                  "carry_more_actions_than_fresh": sum(pair["carry_actions"] > pair["fresh_actions"] for pair in same),
                                  "first_cny": sum(pair["first_cny"] for pair in same),
                                  "carry_cny": sum(pair["carry_cny"] for pair in same),
                                  "carry_failed_actions": sum(pair["carry_failed_actions"] for pair in same)},
            "layer_definition": {"layer1": "冻结的两次执行总费用与第二次执行结果，按对求均值",
                                 "layer2": "每次最终成功承担的平均总费用，以第二次执行的成功数作分母",
                                 "layer3": "首跑失败条件下的再次执行增量，增量费用除以新增成功"},
            "retry_policy": retry_policy(pairs)}


def retry_policy(pairs):
    # 分支使用冻结评价器结果；运行期无同等评价器时该信号不可得，结果单独命名。
    passed = [pair for pair in pairs if pair["first_frozen"]]
    failed = [pair for pair in pairs if not pair["first_frozen"]]
    first_total = sum(pair["first_cny"] for pair in pairs)
    policies = {
        "always_fresh_retry": {"pairs": len(pairs),
                               "second_execution": "同题新上下文",
                               "total_cny": sum(pair["first_cny"] + pair["fresh_cny"] for pair in pairs),
                               "frozen_successes": sum(pair["fresh_frozen"] for pair in pairs)},
        "always_carried_retry": {"pairs": len(pairs),
                                 "second_execution": "携带轨迹",
                                 "total_cny": sum(pair["first_cny"] + pair["carry_cny"] for pair in pairs),
                                 "frozen_successes": sum(pair["carry_frozen"] for pair in pairs)},
        "stop_when_first_passes_fresh": {"pairs": len(pairs), "second_execution": "首跑失败才同题重跑，新上下文",
                                         "stopped_pairs": len(passed), "retried_pairs": len(failed),
                                         "total_cny": first_total + sum(pair["fresh_cny"] for pair in failed),
                                         "frozen_successes": len(passed) + sum(pair["fresh_frozen"] for pair in failed)},
        "stop_when_first_passes_carried": {"pairs": len(pairs), "second_execution": "首跑失败才重跑，携带轨迹",
                                           "stopped_pairs": len(passed), "retried_pairs": len(failed),
                                           "total_cny": first_total + sum(pair["carry_cny"] for pair in failed),
                                           "frozen_successes": len(passed) + sum(pair["carry_frozen"] for pair in failed)}}
    for policy in policies.values():
        policy["cny_per_success"] = policy["total_cny"] / policy["frozen_successes"] if policy["frozen_successes"] else None
    return {"policies": policies, "decision_signal": "冻结评价器首跑结果",
            "qualification": "分支标签来自事后评价器；实际部署须先具备同等运行期判断，本组结果按评价器标签单独命名",
            "first_run_total_cny": first_total}


def cost_recomputation(paid, reviews, ledger, calls, facts, sources, candidates, carry, costs_json):
    record_total = sum(fact["cost_usd"] for fact in facts.values()) * FX
    ledger_total = sum(value["cny"] for value in ledger.values())
    resumed, recovered = [], []
    for execution_id, value in ledger.items():
        if value["segments"] == 1:
            continue
        review = next(row for row in reviews.values() if row["execution_id"] == execution_id)
        record_cny = facts[execution_id]["cost_usd"] * FX
        entry = {"execution_id": execution_id, "task": review["task"], "arm": review["arm"], "repeat": review["repeat"],
                 "ledger_calls": value["calls"], "ledger_segments": value["segments"], "segment_sizes": value["segment_sizes"],
                 "segment_settled_cny": value["segment_settled_cny"],
                 "saved_trajectory_wire_calls": len(execution_record(paid, execution_id)["wire_trace"]),
                 "settled_calls_before_suspension": value["segment_sizes"][0] - 1,
                 "settled_cny_before_suspension": value["segment_settled_cny"][0],
                 "record_cny": record_cny, "ledger_cny": value["cny"], "difference_cny": value["cny"] - record_cny,
                 "unsettled_reserved_cny": value["unsettled_reserved_cny"],
                 "strict_success": review["strict_success"], "business_success": review["full_business_success"],
                 "reference": ref(paid / "executions" / review["arm"] / (execution_id + ".json"))}
        resumed.append(entry)
        if abs(entry["difference_cny"]) > 1e-6:
            entry["reconciled"] = abs(value["segment_settled_cny"][0] - entry["difference_cny"]) < 1e-6
            entry["note"] = "挂起前段只在账本中，已保存轨迹覆盖挂起后段；结局与断言取自恢复后的记录"
            recovered.append(entry)
    if len(recovered) != 1 or not recovered[0]["reconciled"]:
        raise ValueError("账本分段执行与记录差额不匹配: " + str([row["execution_id"] for row in recovered]))
    groups = cost_groups(calls)
    if not all(abs(groups[name]["reference_cny"] - costs_json["groups"][name]["reference_cny"]) < 1e-6 for name in groups):
        raise ValueError("账本分组费用与 costs.json 不一致")
    source_cny = sum(row["source_cny"] for row in sources)
    retest_cny = sum(retest["cny"] for row in sources for retest in row["retests"])
    return {"ledger_execution_cny": ledger_total, "record_execution_cny": record_total,
            "difference_cny": ledger_total - record_total, "recovered_cny": sum(row["difference_cny"] for row in recovered),
            "recovered_executions": recovered, "resumed_executions": resumed,
            "resumed_note": "挂起后恢复的执行有两次；只有一次恢复后的记录漏记挂起前段费用，其余逐段一致",
            "groups": groups, "total_billed_cny": sum(group["billed_cny"] for group in groups.values()),
            "costs_json_groups_reproduced": True,
            "layers": {"source_first_run_cny": source_cny, "source_retest_cny": retest_cny,
                       "migration_runs_cny": sum(sum(pair["cny"] for pair in row["pairs"]) for row in candidates),
                       "ancestor_runs_cny": sum(row["ancestor_cny"] for row in candidates) / len(ARMS),
                       "generation_cny": sum(row["generation_cny"] for row in candidates),
                       "carry_pair_total_cny": sum(pair["first_cny"] + pair["carry_cny"] for pair in carry["pairs"])},
            "review_provider_cost": {"status": "missing", "verdict_files": 432,
                                     "reason": "外部盲审提供方费用不在本账本中，缺失部分单列，不计零"},
            "learning_amortization_cny": [{"uses": uses,
                                           "organize_and_edit": (groups["organization"]["billed_cny"] + groups["editing"]["billed_cny"]) / uses}
                                          for uses in (1, 10, 100, 1000)]}


def historical_reference(root, protocol):
    events = rows(root / HISTORY / "version_events.jsonl")
    metrics = rows(root / HISTORY / "p2_task_metrics.jsonl")
    by_task = defaultdict(list)
    for index, event in enumerate(events):
        by_task[event["lineage_id"].split("/", 1)[-1]].append(
            {"experiment_id": event["experiment_id"], "kind": event["kind"], "selection_event": event["selection_event"],
             "parent_version": event["parent_version"], "candidate_version": event["candidate_version"],
             "reference": ref(HISTORY / "version_events.jsonl", f"line:{index + 1}")})
    sources = sorted(block["source"] for block in protocol["blocks"])
    arms = {}
    for arm in sorted({row["arm"] for row in metrics}):
        values = [row for row in metrics if row["arm"] == arm]
        arms[arm] = {"tasks": len(values), "executions": sum(row["executions"] for row in values),
                     "mean_strict_success_rate": mean([row["strict_success_rate"] for row in values]),
                     "mean_tool_failures": mean([row["mean_tool_failures"] for row in values]),
                     "repeat_structure": "每题 2 次同内容执行"}
    tasks = {row["task"] for row in metrics}
    return {"natural_history_events": {"total": len(events), "by_kind": dict(Counter(event["kind"] for event in events)),
                                       "source_task_event_counts": {task: len(by_task.get(task, [])) for task in sources},
                                       "source_tasks_with_events": {task: by_task[task] for task in sources if by_task.get(task)},
                                       "heldout_tasks_with_events": sum(task in by_task for task in protocol["plan"]["heldout"]),
                                       "use": "来源题在自然历史中已登记的编辑、选择与版本边，用于交代来源复测之外的已有同题背景"},
            "same_task_repeat_background": {"arms": arms,
                                            "task_overlap_with_this_experiment": len(tasks & (set(sources) | set(protocol["plan"]["heldout"]))),
                                            "limit": "该批 18 道题与本实验的来源题、迁移题不重合，只能作为同内容重复波动的范围参照，不能与本题逐次配对"}}


def plots(sources, candidates, carry, output):
    folder = output / "figures"
    folder.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "svg.fonttype": "none", "svg.hashsalt": "gnosis-negative-result"})
    figure, axes = plt.subplots(1, 3, figsize=(13.8, 4.4), gridspec_kw={"width_ratios": [1.1, 1, 1]})

    grid = np.array([[int(row["source_business_success"])] + [int(retest["business_success"]) for retest in row["retests"]]
                     for row in sources], dtype=float)
    axes[0].imshow(grid, cmap=ListedColormap(["#efe9e0", "#2f6f4f"]), vmin=0, vmax=1, aspect="auto")
    axes[0].set_xticks(range(5), ["first", "F0R0", "F1R0", "F0R1", "F1R1"])
    axes[0].set_yticks(range(len(sources)), [row["domain"] for row in sources])
    for i, row in enumerate(sources):
        for j, retest in enumerate(row["retests"]):
            kind = "E" if retest["candidate_deployed"] else "I" if retest["candidate_status"] == "invalid_edit" else "A"
            axes[0].text(j + 1, i, kind, ha="center", va="center", fontsize=8, color="white" if retest["business_success"] else "black")
    axes[0].set_title("Source task: first run and four retests\nfill = business success; E edited candidate / I invalid candidate / A untriggered", fontsize=9)

    domains = sorted({row["domain"] for row in candidates})
    gains = [sum(row["business_gains"] for row in candidates if row["domain"] == domain) for domain in domains]
    losses = [sum(row["business_losses"] for row in candidates if row["domain"] == domain) for domain in domains]
    position = np.arange(len(domains))
    axes[1].bar(position - 0.2, gains, width=0.38, color="#2f6f4f", label="new successes")
    axes[1].bar(position + 0.2, losses, width=0.38, color="#b4472f", label="regressions")
    for index, (gain, loss) in enumerate(zip(gains, losses)):
        axes[1].text(index - 0.2, gain + 0.12, str(gain), ha="center", fontsize=8)
        axes[1].text(index + 0.2, loss + 0.12, str(loss), ha="center", fontsize=8)
    axes[1].set_xticks(position, domains, rotation=30, ha="right")
    axes[1].set_ylim(0, max(gains + losses) * 1.35)
    axes[1].set_ylabel("paired runs (48 per domain)")
    axes[1].legend(frameon=False, fontsize=8, loc="upper left")
    axes[1].set_title("Migration against same-period ancestor\nbusiness-evaluated, four arms pooled", fontsize=9)

    names = ["first_failed", "first_passed", "all"]
    values = [[carry["situations"][name]["business"]["fresh_successes"], carry["situations"][name]["business"]["carry_successes"]]
              for name in names]
    counts = [carry["situations"][name]["pairs"] for name in names]
    position = np.arange(len(names))
    axes[2].bar(position - 0.2, [value[0] / count for value, count in zip(values, counts)], width=0.38, color="#94b7ca", label="fresh rerun")
    axes[2].bar(position + 0.2, [value[1] / count for value, count in zip(values, counts)], width=0.38, color="#2f6f4f", label="carried trace")
    for index, (value, count) in enumerate(zip(values, counts)):
        axes[2].text(index - 0.2, value[0] / count + 0.02, f"{value[0]}/{count}", ha="center", fontsize=8)
        axes[2].text(index + 0.2, value[1] / count + 0.02, f"{value[1]}/{count}", ha="center", fontsize=8)
    axes[2].set_xticks(position, ["first failed", "first passed", "all pairs"])
    axes[2].set_ylim(0, 1.18)
    axes[2].set_ylabel("business success rate")
    axes[2].legend(frameon=False, fontsize=8)
    axes[2].set_title("Same task, carried trajectory\npaired against the cyclic ancestor rerun", fontsize=9)
    figure.tight_layout()
    for extension in ("svg", "png", "pdf"):
        figure.savefig(folder / ("figure4_source_migration_carry." + extension), dpi=450,
                       metadata=FIG_METADATA[extension])
    plt.close(figure)


def run(root, output):
    root, output = Path(root), Path(output)
    paid = root / PAID
    protocol = read(root / PROTOCOL)
    reviews = read(root / BUSINESS / "execution_reviews.json")
    business_summary = read(root / BUSINESS / "summary.json")
    carry_json = read(root / CARRY)
    costs_json = read(root / COSTS)
    status = read(root / HISTORY / "candidate_status.json")
    slots = {(slot["domain"], slot["arm"]): slot for slot in status["slots"]}
    diffs = rows(root / HISTORY / "edit_content_diffs.jsonl")
    blocks = {block["domain"]: read(paid / "blocks" / (block["domain"] + ".json")) for block in protocol["blocks"]}
    calls = ledger_calls(root)
    ledger = ledger_executions(calls)
    migration_reviews, source_reviews = review_index(reviews)
    if len(reviews) != 462 or len(migration_reviews) != 432 or len(source_reviews) != 30:
        raise ValueError("业务评价表数量不符")
    facts = collect(paid, reviews)
    if sorted(ledger) != sorted(row["execution_id"] for row in reviews):
        raise ValueError("账本执行集合与评价集合不一致")
    recomputed = defaultdict(lambda: defaultdict(int))
    for review in reviews:
        key = (review["scope"], review["domain"], review["arm"])
        recomputed[key]["n"] += 1
        recomputed[key]["strict_successes"] += int(review["strict_success"])
        recomputed[key]["full_business_successes"] += int(review["full_business_success"])
    for row in business_summary:
        key = (row["scope"], row["domain"], row["arm"])
        if any(recomputed[key][field] != row[field] for field in ("n", "strict_successes", "full_business_successes")):
            raise ValueError("业务汇总与逐次评价不一致")
    sources = source_layer(paid, protocol, source_reviews, ledger, slots, blocks, diffs, facts)
    candidates, per_arm, per_class = migration_layer(protocol, migration_reviews, source_reviews, ledger, blocks, slots, facts)
    carry = carry_layer(protocol, migration_reviews, ledger, carry_json, facts)
    recomputation = cost_recomputation(paid, migration_reviews, ledger, calls, facts, sources, candidates, carry, costs_json)
    history = historical_reference(root, protocol)
    statistics_json = read(output / "statistics.json")
    arm_rows = []
    for arm in ARMS:
        values = per_arm[arm]
        arm_rows.append({"arm": arm, "candidates": len(values), "deployed": sum(row["candidate_deployed"] for row in values),
                         "runs": sum(row["runs"] for row in values),
                         "business_successes": sum(row["business_successes"] for row in values),
                         "ancestor_business_successes": sum(row["ancestor_business_successes"] for row in values),
                         "business_gains": sum(row["business_gains"] for row in values),
                         "business_losses": sum(row["business_losses"] for row in values),
                         "frozen_successes": sum(row["frozen_successes"] for row in values),
                         "ancestor_frozen_successes": sum(row["ancestor_frozen_successes"] for row in values),
                         "generation_cny": sum(row["generation_cny"] for row in values),
                         "source_retest_cny": sum(row["source_retest_cny"] for row in values),
                         "migration_cny": sum(row["migration_cny"] for row in values)})
    result = {
        "protocol": {"schema": protocol["schema"], "experiment_version": status["protocol"]["experiment_version"],
                     "ancestor_version": protocol["ancestor_version"],
                     "factor_F": status["protocol"]["factor_F"], "factor_R": status["protocol"]["factor_R"],
                     "unit": "来源层每次 1 次执行；迁移层每候选 12 次配对执行；携带层每对 2 次执行",
                     "metric": "业务成功为完整业务评价结果；冻结成功为冻结要求判读；两套结果并列表述",
                     "cost": f"账本已结算费用 × {FX}，含恢复执行挂起前段的付费调用",
                     "references": {"protocol": ref(PROTOCOL), "business_reviews": ref(BUSINESS / "execution_reviews.json"),
                                    "business_summary": ref(BUSINESS / "summary.json"), "carry": ref(CARRY), "costs": ref(COSTS),
                                    "candidate_status": ref(HISTORY / "candidate_status.json"), "ledger": ref(LEDGER),
                                    "edit_content_diffs": ref(HISTORY / "edit_content_diffs.jsonl")}},
        "execution_index": {"reviews": len(reviews), "migration": len(migration_reviews), "source": len(source_reviews),
                            "ledger_executions": len(ledger), "business_summary_reproduced": True},
        "source_layer": {"blocks": sources,
                         "summary": {"source_tasks": len(sources),
                                     "first_run_business_successes": sum(row["source_business_success"] for row in sources),
                                     "retests": sum(len(row["retests"]) for row in sources),
                                     "retests_on_deployed_candidate": sum(row["deployed_candidates"] for row in sources),
                                     "retests_on_ancestor": sum(row["same_version_retests"] for row in sources),
                                     "sources_with_deployed_candidate": sum(row["deployed_candidates"] > 0 for row in sources)}},
        "migration_layer": {"candidates": candidates, "by_arm": arm_rows, "requirement_classes": {arm: {req_class: dict(values) for req_class, values in sorted(per_class[arm].items())} for arm in ARMS},
                            "requirement_classes_pooled": {req_class: {"applicable": sum(per_class[arm][req_class]["applicable"] for arm in ARMS),
                                                                       "passed": sum(per_class[arm][req_class]["passed"] for arm in ARMS),
                                                                       "executions": sum(per_class[arm][req_class]["executions"] for arm in ARMS),
                                                                       "executions_all_passed": sum(per_class[arm][req_class]["executions_all_passed"] for arm in ARMS)}
                                                           for req_class in sorted({name for arm in ARMS for name in per_class[arm]})},
                            "action_check_pooled": {name: sum(row[name] for row in candidates) for name in
                                                    ("fewer_actions_with_necessary_miss", "fewer_actions_necessary_complete",
                                                     "more_actions_necessary_complete", "more_actions_with_necessary_miss")},
                            "action_check_unit": "同一任务同一 repeat 的迁移执行与祖先执行比较动作数，必要行动全通过按完整业务要求判定",
                            "cost_definition": "候选完整费用 = 该槽位生成费用 + 来源复测 + 该臂 12 次迁移执行；祖先 12 次执行由四条臂共用，单列不计入候选",
                            "comparison_unit": "迁移执行与同题同 repeat 的祖先执行配对；四条臂共用同一批 12 次祖先执行",
                            "invalid_candidates": [{"domain": row["domain"], "arm": row["arm"]} for row in candidates if row["candidate_status"] == "invalid_edit"],
                            "untriggered_candidates": [{"domain": row["domain"], "arm": row["arm"]} for row in candidates if row["slot_status"] == "untriggered_source_passed"]},
        "joint_term": {"contract": "F1R1 − F1R0 − F0R1 + F0R0，按来源块计算，取自同计划二因素统计结果",
                       "reference": ref(Path("experiments/negative_result_offline/statistics.json")),
                       "R1_increment": {name: statistics_json["R1_increment"][name] for name in ("effect", "p_two_sided", "ci95")},
                       "interaction": {name: statistics_json["interaction"][name] for name in ("effect", "p_two_sided", "ci95")},
                       "holm_adjusted": statistics_json["secondary_holm"],
                       "block_cells": [{"domain": block["domain"],
                                        "cells": {arm: ("candidate" if not slots[(block["domain"], arm)]["deployed_ancestor"] else "ancestor")
                                                  for arm in ARMS}} for block in protocol["blocks"]],
                       "blocks_with_all_four_cells_from_candidates": sum(
                           all(not slots[(block["domain"], arm)]["deployed_ancestor"] for arm in ARMS) for block in protocol["blocks"]),
                       "reading": "联合项在三个未触发块上四格全部为祖先执行；在 finance、sales、support 上各有一格或两格为祖先执行，按真实流程结局阅读"},
        "carry_layer": carry, "cost_recomputation": recomputation, "historical_reference": history,
        "task_groups": [{"domain": block["domain"], "source": block["source"],
                         "migration": [{"task": item["task"], "shared_structure": item["shared_structure"], "rationale": item["rationale"]}
                                       for item in block["migration"]]} for block in protocol["blocks"]],
        "unresolved": ["F0R1 与 F1R1 在 finance、sales、support 各有一个候选未生成，这些槽位部署祖先版本，表示增量在这些块上只在其余槽位成立。",
                       "相邻适用条件的相反情形按已登记分组阅读；程序只核验候选新增内容中的来源题对象与取值，未判定条件本身是否反转。",
                       "来源复测在未触发编辑的块上为同版本重跑，其变化归入执行波动，不计入修复效果。",
                       "外部盲审提供方费用在账本中缺失，按缺失单列，不计零。",
                       "历史 repeat 臂的题目与本实验不重合，只作同内容重复波动参照，未与本题逐次配对。"]}
    save(output / "transfer.json", result)
    lookup = {(row["domain"], row["arm"]): row for row in candidates}
    source_first = {row["domain"]: row for row in sources}
    table(output / "tables/table_transfer.csv", [
        {"domain": row["domain"], "arm": row["arm"], "slot_status": row["slot_status"], "candidate_status": row["candidate_status"],
         "candidate_deployed": row["candidate_deployed"], "migration_runs": row["runs"],
         "source_first_business": source_first[row["domain"]]["source_business_success"],
         "source_first_strict": source_first[row["domain"]]["source_strict_success"],
         "source_retest_business": next(retest["business_success"] for retest in source_first[row["domain"]]["retests"] if retest["arm"] == row["arm"]),
         "source_retest_strict": next(retest["strict_success"] for retest in source_first[row["domain"]]["retests"] if retest["arm"] == row["arm"]),
         "source_retest_same_version": next(not retest["candidate_deployed"] for retest in source_first[row["domain"]]["retests"] if retest["arm"] == row["arm"]),
         "source_business_change": next(retest["business_success"] for retest in source_first[row["domain"]]["retests"] if retest["arm"] == row["arm"]) - int(source_first[row["domain"]]["source_business_success"]),
         "source_strict_change": next(retest["strict_success"] for retest in source_first[row["domain"]]["retests"] if retest["arm"] == row["arm"]) - int(source_first[row["domain"]]["source_strict_success"]),
         "business_successes": row["business_successes"], "ancestor_business_successes": row["ancestor_business_successes"],
         "business_gains": row["business_gains"], "business_losses": row["business_losses"], "business_net": row["business_net"],
         "strict_gains": row["strict_gains"], "strict_losses": row["strict_losses"],
         "frozen_successes": row["frozen_successes"], "ancestor_frozen_successes": row["ancestor_frozen_successes"],
         "fewer_actions_with_necessary_miss": row["fewer_actions_with_necessary_miss"],
         "fewer_actions_necessary_complete": row["fewer_actions_necessary_complete"],
         "cost_scope": "candidate_specific" if row["candidate_deployed"] else "shared_ancestor_deployment",
         "generation_cny": round(row["generation_cny"], 6), "source_retest_cny": round(row["source_retest_cny"], 6),
         "migration_cny": round(row["migration_cny"], 6), "candidate_total_cny": round(row["candidate_total_cny"], 6),
         "cost_per_net_gain_cny": round(row["cost_per_net_gain_cny"], 6) if row["cost_per_net_gain_cny"] else "",
         "carried_source_literals": "|".join(sorted({literal for retest in source_first[row["domain"]]["retests"]
                                                     if retest["arm"] == row["arm"] for literal in retest["carried_source_literals"]})),
         "carried_distinctive_literals": "|".join(sorted({literal for retest in source_first[row["domain"]]["retests"]
                                                          if retest["arm"] == row["arm"] for literal in retest["carried_distinctive_literals"]}))}
        for row in (lookup[key] for key in sorted(lookup))])
    plots(sources, candidates, carry, output)
    print(json.dumps({"source": result["source_layer"]["summary"], "migration_by_arm": arm_rows,
                      "carry_business": {name: result["carry_layer"]["situations"][name]["business"] for name in ("all", "first_failed", "first_passed")},
                      "carry_reproduction": result["carry_layer"]["reproduction"],
                      "cost": {"ledger": result["cost_recomputation"]["ledger_execution_cny"], "record": result["cost_recomputation"]["record_execution_cny"],
                               "recovered_cny": result["cost_recomputation"]["recovered_cny"]}}, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    arguments = parser.parse_args()
    run(arguments.root, arguments.output)
