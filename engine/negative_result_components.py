import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from engine.negative_result_history import original_path


def activates(unit, event):
    # engine/activation.py 的 unit_hooks 与 activates 原文；该模块经 engine/scoped_units 依赖 fcntl，Windows 上无法导入。
    # applicable 的最终判据以 engine/evo_path.py 写入 expression.jsonl 的字段为准，本函数只用于补出边界计数，运行时会逐行核对。
    hooks = unit["hooks"] if "hooks" in unit else ["task_start", "after_call"]
    if event["hook"] not in hooks:
        return False
    scope = unit["scope"]
    return "GLOBAL" in scope or any(value in scope for value in
                                    (event.get("service"), event.get("endpoint"), event.get("tool")))


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/negative_result_offline"
DISCOVERY = Path("experiments/evo_path/discovery")
HISTORY = OUT / "history"
PAID = ROOT / "experiments/feedback_chain/paid"

# 与 engine/evo_path.py 的 Versions 同一批版本仓库；只覆盖本次出现可执行组件的实验
VERSION_ROOTS = (["experiments/research/" + arm for arm in ("gnosis", "global", "ace", "fixed", "repeat")]
                 + ["experiments/research.source_20261007/" + arm
                    for arm in ("ace", "fixed", "global", "gnosis", "repeat")]
                 + ["experiments/ecai_2027/mechanism/" + name for name in ("B0", "C1", "C2", "C3")]
                 + ["experiments/ecai_2027/scope_execution_ab/B0"]
                 + ["experiments/ecai_2027/scope_execution_ab/candidates/" + name
                    for name in sorted(path.name for path in (ROOT / "experiments/ecai_2027/scope_execution_ab/candidates").iterdir())]
                 + ["experiments/ecai_2027/heldout_new/" + arm for arm in ("fixed", "global", "gnosis")]
                 + ["experiments/ecai_2027/ablation/gnosis_no_e", "experiments/timing_v4/common"])


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ref(path, pointer=""):
    return {"file": str(path).replace("\\", "/"), "pointer": pointer}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def local_path(value):
    return original_path(ROOT, str(value).replace("\\", "/"))


class Versions:
    def __init__(self):
        self.index = {}
        for root in VERSION_ROOTS:
            folder = local_path(root) / "versions"
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                if (entry / "manifest.json").exists():
                    self.index.setdefault(entry.name, entry / "manifest.json")
        self.cache = {}

    def units(self, version):
        if version not in self.cache:
            self.cache[version] = read(self.index[version])["units"]
        return self.cache[version]


def execution_records(registry, expression_rows):
    needed = {row["execution_id"] for row in expression_rows}
    paths = {}
    for identity, entry in registry.items():
        if identity in needed:
            paths[identity] = local_path(entry["evidence_path"])
    if set(paths) != needed:
        raise ValueError("组件执行记录缺注册路径: " + str(sorted(needed - set(paths))[:3]))
    return {identity: read(path) for identity, path in paths.items()}


def scan_components(versions, registry, expression_rows, records):
    # 单元定义取自执行所用版本 manifest；组件调用取自真实执行记录的 injections
    unit_rows, invocations = [], []
    for row in expression_rows:
        identity = row["execution_id"]
        record = records[identity]
        if record["execution_id"] != identity:
            raise ValueError("执行记录身份不一致: " + identity)
        entry = registry[identity]
        definitions = {item["id"]: item for item in versions.units(record["version"])}
        uid = row["repair_identity"]
        unit = definitions[uid]
        if unit.get("component") != row["component"]:
            raise ValueError("组件类型与版本 manifest 不一致: " + uid)
        trace = record.get("boundary_trace") or []
        applicable = sum(1 for event in trace if activates(unit, event))
        if bool(applicable) != row["applicable"]:
            raise ValueError("适用机会判定与 expression.jsonl 不一致: " + uid + "/" + identity)
        events = [event for event in record["injections"] if event.get("unit") == uid]
        unit_rows.append({"unit": uid, "component": unit.get("component"),
                          "hooks": unit.get("hooks") or ["task_start", "after_call"],
                          "scope": unit.get("scope"), "execution_id": identity,
                          "experiment_id": entry["experiment_id"], "task": entry["task"],
                          "version": record["version"], "recorded_at": entry["recorded_at"],
                          "applicable_boundaries": applicable, "invocations": len(events),
                          "effect_events": sum(1 for event in events if event.get("effects")),
                          "first_effect_boundary": (record.get("first_effect_boundaries") or {}).get(uid),
                          "component_tool_calls": sum(len(event.get("actual_calls") or []) for event in events),
                          "strict_success": record["success"], "partial_credit": record.get("partial_credit"),
                          "loaded": row["loaded"], "state": row["state"],
                          "reference": ref(entry["evidence_path"], f"/injections")})
        for event in events:
            invocations.append({"unit": uid, "component": unit.get("component"),
                                "execution_id": identity, "experiment_id": entry["experiment_id"],
                                "hook": event.get("hook"), "boundary_index": event.get("boundary_index"),
                                "input": event.get("input"), "output": event.get("output"),
                                "effects": event.get("effects") or [], "actual_calls": event.get("actual_calls") or [],
                                "next_actual_tool_action": event.get("next_actual_tool_action"),
                                "reference": ref(entry["evidence_path"], f"/injections"),
                                "boundary_payload": next(({"hook": b.get("hook"), "service": b.get("service"),
                                                           "endpoint": b.get("endpoint"), "tool": b.get("tool"),
                                                           "step": b.get("step")}
                                                          for b in trace if b.get("index") == event.get("boundary_index")), None)})
    return unit_rows, invocations


def effect_labels(invocations):
    return dict(Counter(label for item in invocations for label in item["effects"]))


def payload_summary(invocations, key, limit=8):
    counts = Counter((item["boundary_payload"] or {}).get(key) for item in invocations)
    return {str(name): count for name, count in counts.most_common(limit)}


def unit_entry(unit, unit_rows, invocations, lineage, versions):
    unit_rows = sorted(unit_rows, key=lambda row: (row["recorded_at"], row["execution_id"]))
    invocations = [item for item in invocations if item["unit"] == unit]
    loaded = [row for row in unit_rows if row["loaded"]]
    first = loaded[0]
    introduction = []
    for index, event in enumerate(lineage):
        if event["kind"] not in ("edit", "attempt", "repair_adoption", "proposal"):
            continue
        units = (event.get("stats") or {}).get("units") or []
        if unit not in units:
            continue
        parent = event.get("parent_version")
        known = parent in versions.index
        introduction.append({"experiment_id": event["experiment_id"], "kind": event["kind"],
                             "event_order": event["event_order"], "parent_version": parent,
                             "candidate_version": event.get("candidate_version"),
                             "selection_event": event.get("selection_event"),
                             "parent_manifest_available": known,
                             "parent_contains_unit": (unit in {u["id"] for u in versions.units(parent)}) if known else None,
                             "reference": ref(DISCOVERY / "lineage.jsonl", f"line:{index + 1}")})
    introduced = [item for item in introduction
                  if item["parent_contains_unit"] is False and item["candidate_version"]]
    introduced.sort(key=lambda item: item["event_order"])
    effect_rows = [row for row in unit_rows if row["effect_events"]]
    outputs = Counter(json.dumps(sorted((item["output"] or {}).keys()), ensure_ascii=False) for item in invocations)
    text_chars = [len((item["output"] or {}).get("text") or "") for item in invocations]
    return {
        "unit": unit, "component": first["component"], "hooks": first["hooks"], "scope": first["scope"],
        "birth": {"first_loaded_execution": first["execution_id"], "experiment_id": first["experiment_id"],
                  "task": first["task"], "version": first["version"], "recorded_at": first["recorded_at"],
                  "reference": first["reference"],
                  "lineage_records_listing_unit": len(introduction),
                  "lineage_first_listing": introduction[0] if introduction else None,
                  "lineage_introducing_events": introduced,
                  "introduction_rule": "父版本 manifest 不含该单元而候选版本含该单元，取谱系记录中最早一次"},
        "reach": {"executions_with_unit_row": len(unit_rows), "executions_loaded": len(loaded),
                  "executions_applicable": sum(row["applicable_boundaries"] > 0 for row in unit_rows),
                  "executions_with_invocation_event": sum(row["invocations"] > 0 for row in unit_rows),
                  "applicable_boundaries": sum(row["applicable_boundaries"] for row in unit_rows),
                  "invocations": len(invocations), "hooks": dict(Counter(item["hook"] for item in invocations)),
                  "experiments": dict(Counter(row["experiment_id"] for row in invocations)),
                  "executions_by_experiment": dict(Counter(row["experiment_id"] for row in unit_rows)),
                  "trigger_payload_endpoint": payload_summary(invocations, "endpoint"),
                  "trigger_payload_tool": payload_summary(invocations, "tool"),
                  "trigger_payload_service": payload_summary(invocations, "service"),
                  "state_counts": dict(Counter(row["state"] for row in unit_rows))},
        "effect": {"executions_with_effect_event": len(effect_rows), "effect_events": sum(row["effect_events"] for row in unit_rows),
                   "effect_labels": effect_labels(invocations),
                   "executions_with_first_effect_boundary": sum(row["first_effect_boundary"] is not None for row in unit_rows),
                   "invocations_without_effect": sum(1 for item in invocations if not item["effects"]),
                   "effect_share_of_invocations": ratio(sum(len(item["effects"]) for item in invocations), len(invocations)),
                   "strict_successes_among_effect_executions": sum(row["strict_success"] for row in effect_rows),
                   "experiments_with_effect": dict(Counter(row["experiment_id"] for row in effect_rows)),
                   "effect_executions": sorted({row["execution_id"] for row in effect_rows})},
        "downstream": {"component_issued_tool_calls": sum(len(item["actual_calls"]) for item in invocations),
                       "invocations_with_component_tool_calls": sum(1 for item in invocations if item["actual_calls"]),
                       "invocations_with_next_actual_tool_action": sum(1 for item in invocations
                                                                        if item["next_actual_tool_action"] is not None),
                       "output_shape_counts": {name: count for name, count in
                                               Counter("state" if json.loads(name) == ["state"] else
                                                       "text" if json.loads(name) == ["text"] else "other"
                                                       for name in outputs.elements()).most_common()},
                       "output_text_chars_max": max(text_chars) if text_chars else 0},
        "business": {"full_business_review_available": False,
                     "strict_success_rate": ratio(sum(row["strict_success"] for row in unit_rows), len(unit_rows)),
                     "mean_partial_credit": (sum(row["partial_credit"] or 0 for row in unit_rows) / len(unit_rows)),
                     "measurement": "历史执行只有官方断言判读与部分分；完整业务义务评价只覆盖 experiments/feedback_chain/paid 的 462 次执行"}}


def paid_trace():
    records = [read(path) for path in sorted((PAID / "executions").glob("*/*.json"))]
    reviews = {row["execution_id"]: row for row in read(OUT / "business/execution_reviews.json")}
    if len(records) != 462 or len(reviews) != 462:
        raise ValueError("付费执行记录或业务评价数量不符")
    units = defaultdict(lambda: {"invocations": 0, "hooks": Counter(), "effect_events": 0,
                                 "component_tool_calls": 0, "executions": set(), "arms": Counter(),
                                 "trigger_payloads": Counter(), "strict_successes": 0, "frozen_successes": 0,
                                 "business_successes": 0, "reviewed_executions": 0})
    boundary_before_call, tool_actions, total_invocations = 0, 0, 0
    for record in records:
        review = reviews[record["execution_id"]]
        summary = record["component_invocations"]
        if summary != sum(1 for event in record["injections"] if event.get("component")):
            raise ValueError("component_invocations 与调用记录不一致: " + record["execution_id"])
        boundaries = {event["index"]: event for event in record["boundary_trace"]}
        boundary_before_call += sum(1 for event in record["boundary_trace"] if event["hook"] == "before_call")
        tool_actions += len(record["actual_tool_actions"])
        if record.get("first_effect_boundaries"):
            raise ValueError("付费执行出现效果边界，需重新核对: " + record["execution_id"])
        for event in record["injections"]:
            if not event.get("component"):
                continue
            total_invocations += 1
            entry = units[event["unit"]]
            entry["invocations"] += 1
            entry["hooks"][event.get("hook")] += 1
            entry["effect_events"] += sum(1 for value in (event.get("effects") or []))
            entry["component_tool_calls"] += len(event.get("actual_calls") or [])
            entry["executions"].add(record["execution_id"])
            entry["arms"][record["arm"]] += 1
            boundary = boundaries.get(event.get("boundary_index")) or {}
            entry["trigger_payloads"][boundary.get("endpoint") or boundary.get("tool") or boundary.get("hook")] += 1
        for unit in {event["unit"] for event in record["injections"] if event.get("component")}:
            entry = units[unit]
            entry["reviewed_executions"] += 1
            entry["strict_successes"] += int(review["strict_success"])
            entry["frozen_successes"] += int(bool(review["frozen_success"]))
            entry["business_successes"] += int(review["full_business_success"])
    result = {"scope": "experiments/feedback_chain/paid 的 462 次执行；其中 432 次为隔离迁移题、30 次为来源首跑或来源复测",
              "executions": len(records), "component_invocations": total_invocations,
              "records_with_component_invocations": len(records),
              "records_with_capability_calls": sum(1 for record in records if record["component_capability_calls"]),
              "executions_with_effect_boundary": sum(1 for record in records if record.get("first_effect_boundaries")),
              "before_call_boundaries": boundary_before_call, "actual_tool_actions": tool_actions,
              "units": {}, "review_reference": ref(Path("experiments/negative_result_offline/business/execution_reviews.json"))}
    for unit, entry in sorted(units.items()):
        result["units"][unit] = {"invocations": entry["invocations"], "hooks": dict(entry["hooks"]),
                                 "effect_events": entry["effect_events"],
                                 "component_tool_calls": entry["component_tool_calls"],
                                 "executions_loading": len(entry["executions"]),
                                 "reviewed_executions": entry["reviewed_executions"],
                                 "invocations_by_arm": dict(entry["arms"]),
                                 "trigger_payloads": {str(name): count for name, count in entry["trigger_payloads"].most_common(8)},
                                 "strict_successes": entry["strict_successes"],
                                 "frozen_successes": entry["frozen_successes"],
                                 "business_successes": entry["business_successes"]}
    return result


def burden(unit_rows, invocations, paid):
    target = "ace_op_vp_guard"
    historical_executions = {row["execution_id"] for row in unit_rows}
    experiments = Counter(row["experiment_id"] for row in unit_rows)
    effect_rows = [row for row in unit_rows if row["effect_events"]]
    payload = Counter()
    for item in invocations:
        boundary = item["boundary_payload"] or {}
        payload[(boundary.get("endpoint") or boundary.get("tool") or boundary.get("hook"))] += 1
    total_registry = Counter(row["experiment_id"] for row in
                             [json.loads(line) for line in (HISTORY / "executions.jsonl").read_text(encoding="utf-8").splitlines()
                              if line.strip()])
    paid_entry = paid["units"][target]
    return {"unit": target,
            "historical_scope": {"experiments": dict(experiments), "executions": len(historical_executions),
                                 "invocations": len(invocations), "effect_events": sum(row["effect_events"] for row in unit_rows),
                                 "executions_with_effect": len(effect_rows),
                                 "component_tool_calls": sum(row["component_tool_calls"] for row in unit_rows),
                                 "hooks": dict(Counter(item["hook"] for item in invocations)),
                                 "executions_share_of_registry": {name: ratio(count, total_registry[name])
                                                                  for name, count in experiments.items()},
                                 "trigger_payloads": {str(name): count for name, count in payload.most_common(10)}},
            "paid_scope": {**paid_entry, "effect_boundary_records": paid["executions_with_effect_boundary"],
                           "before_call_boundaries": paid["before_call_boundaries"],
                           "actual_tool_actions": paid["actual_tool_actions"],
                           "invocations_share_of_before_call": ratio(paid_entry["invocations"] - paid_entry["hooks"].get("task_start", 0),
                                                                     paid["before_call_boundaries"])},
            "cost": {"settled_task_execution_cny": read(OUT / "costs.json")["groups"]["task_execution"]["billed_cny"],
                     "attributable_cny": None,
                     "measurement": "账本按执行结算，没有分组件计量；组件调用不产生独立账单行，"
                                    "因此报告调用次数与所在执行的已结算费用，不把执行总费用记为组件费用"},
            "reading": "结构长期存在且被反复调用，未产生效果事件，也未发出额外工具调用"}


def effect_cases(unit_rows, invocations):
    groups = defaultdict(list)
    for row in unit_rows:
        if row["effect_events"]:
            groups[row["unit"]].append(row)
    cases = []
    for unit, values in sorted(groups.items()):
        values.sort(key=lambda row: (row["recorded_at"], row["execution_id"]))
        acts = [row for row in values if row["component_tool_calls"] > 0]
        passed = [row for row in values if row["strict_success"]]
        failed = [row for row in values if not row["strict_success"]]
        connected = [row for row in acts if row["strict_success"]]
        cases.append({"unit": unit, "component": values[0]["component"],
                      "experiments": dict(Counter(row["experiment_id"] for row in values)),
                      "executions_with_effect": len(values),
                      "effect_events": sum(row["effect_events"] for row in values),
                      "executions_with_component_tool_calls": len(acts),
                      "strict_successes_among_effect_executions": len(passed),
                      "strict_success_rate_among_effect_executions": ratio(len(passed), len(values)),
                      "positive_example": passed[0]["execution_id"] if passed else None,
                      "negative_example": failed[0]["execution_id"] if failed else None,
                      "tool_call_linked_success_example": connected[0]["execution_id"] if connected else None,
                      "executions": [{"execution_id": row["execution_id"], "experiment_id": row["experiment_id"],
                                      "task": row["task"], "hooks": row["hooks"],
                                      "component_tool_calls": row["component_tool_calls"],
                                      "strict_success": row["strict_success"], "partial_credit": row["partial_credit"],
                                      "reference": row["reference"]} for row in values]})
    effect_invocations = [item for item in invocations if item["effects"]]
    return {"units_with_effect": len(cases),
            "effect_events": sum(case["effect_events"] for case in cases),
            "executions_with_effect": len({row["execution_id"] for row in unit_rows if row["effect_events"]}),
            "unit_execution_pairs_with_effect": sum(case["executions_with_effect"] for case in cases),
            "executions_with_component_tool_calls": sum(case["executions_with_component_tool_calls"] for case in cases),
            "component_tool_calls_total": sum(len(item["actual_calls"]) for item in effect_invocations),
            "cases": cases,
            "business_verification": "全部63次效果执行的官方重放与业务关联见 completion/historical_component_business.json；"
                                     "其中13次属于本次30道任务的完整业务要求范围"}


def run(root, output):
    root, output = Path(root), Path(output)
    expression_rows = [row for row in rows(root / DISCOVERY / "expression.jsonl") if row.get("component")]
    registry = {row["execution_id"]: row for row in rows(output / "history/executions.jsonl")}
    lineage = rows(output / "history/version_events.jsonl")
    versions = Versions()
    records = execution_records(registry, expression_rows)
    unit_rows, invocations = scan_components(versions, registry, expression_rows, records)
    summary = {item["unit"]: item for item in read(output / "history/e_component_summary.json")}
    check = {}
    for unit, published in summary.items():
        values = [row for row in unit_rows if row["unit"] == unit]
        check[unit] = {"executions": sum(row["invocations"] > 0 for row in values) == published["executions"],
                       "invocations": len([item for item in invocations if item["unit"] == unit]) == published["invocations"],
                       "effect_events": sum(row["effect_events"] for row in values) == published["effect_events"],
                       "additional_tool_calls": sum(row["component_tool_calls"] for row in values)
                       == published["additional_tool_calls"]}
    if not all(all(flags.values()) for flags in check.values()):
        raise ValueError("单元级扫描与既有组件汇总不一致: " + json.dumps(
            {unit: flags for unit, flags in check.items() if not all(flags.values())}, ensure_ascii=False))
    units = [unit_entry(unit, [row for row in unit_rows if row["unit"] == unit],
                        invocations, lineage, versions)
             for unit in sorted({row["unit"] for row in unit_rows})]
    paid = paid_trace()
    result = {
        "protocol": {"work_package": "WP-COMPONENTS：功能表达、后续保留与构造负担",
                     "plan_sections": ["§11"],
                     "plan_reference": ref(root / "docs/GNOSIS-负结果论文离线深化与证据整合计划-2026-10-16.md", "§11"),
                     "unit_of_analysis": "版本 manifest 中 component 非空的单元在真实执行边界上的一次调用",
                     "reach_rule": "调用事件在真实执行记录的 injections 中登记，且其 boundary_index 指向 boundary_trace 的同一次边界",
                     "applicability_rule": "按 engine/activation.py 的 activates，用单元自身 hooks 与 scope 匹配 boundary_trace",
                     "effect_rule": "调用事件的 effects 非空，或执行记录的 first_effect_boundaries 含该单元",
                     "sources": {"expression": ref(DISCOVERY / "expression.jsonl"),
                                 "registry": ref(output / "history/executions.jsonl"),
                                 "lineage": ref(DISCOVERY / "lineage.jsonl"),
                                 "component_summary": ref(output / "history/e_component_summary.json"),
                                 "paid_executions": ref(Path("experiments/feedback_chain/paid/executions")),
                                 "business_reviews": ref(output / "business/execution_reviews.json"),
                                 "costs": ref(output / "costs.json")},
                     "cross_check": {"e_component_summary_units": len(summary), "unit_field_checks": check,
                                     "result": "一致"}},
        "units": units,
        "totals": {"units": len(units), "executions_with_component_unit": len({row["execution_id"] for row in unit_rows}),
                   "component_invocations": len(invocations),
                   "effect_events": sum(row["effect_events"] for row in unit_rows),
                   "executions_with_effect": len({row["execution_id"] for row in unit_rows if row["effect_events"]}),
                   "component_tool_calls": sum(row["component_tool_calls"] for row in unit_rows),
                   "components": dict(Counter(row["component"] for row in unit_rows)),
                   "experiments": dict(Counter(row["experiment_id"] for row in unit_rows))},
        "paid_deployment": paid,
        "burden": burden([row for row in unit_rows if row["unit"] == "ace_op_vp_guard"],
                         [item for item in invocations if item["unit"] == "ace_op_vp_guard"], paid),
        "effect_cases": effect_cases(unit_rows, invocations),
        "unresolved": ["50次历史效果执行的任务未纳入本次30道任务的完整业务要求范围，保留官方重放结果。",
                       "账本按执行结算，组件调用没有独立计量，构造负担只能报调用次数与所在执行的费用。",
                       "付费实验的组件调用只登记钩子、边界与被注入单元，未登记等待时间；真实执行耗时无法按组件拆分。",
                       "单元改名与拆分的版本阅读结果见 requirements/unit_relationships.json。"]}
    save(output / "history/component_business.json", result)
    print(json.dumps({"totals": result["totals"], "paid": {k: v for k, v in paid.items() if k != "units"},
                      "burden_reading": result["burden"]["reading"],
                      "effect_cases": {k: v for k, v in result["effect_cases"].items() if k != "cases"}},
                     ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    arguments = parser.parse_args()
    run(arguments.root, arguments.output)
