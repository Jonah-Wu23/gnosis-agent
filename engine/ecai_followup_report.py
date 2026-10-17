import itertools
import json
import sqlite3

import numpy as np

from engine.activation import activates, components_version
from engine.ecai_followup import (ABLATION, ALLOCATIONS, BASE, FX, HELDOUT, LEDGER, OUT,
                                  ROOT, TERMINAL, frozen_protocol, liabilities)
from engine.run_ecai import read, table
from engine.scoped_units import save_json


def exact_difference(values):
    delta = np.asarray(values, dtype=float)
    if not len(delta):
        raise ValueError("配对统计没有完整来源或任务")
    observed = abs(delta.mean())
    # 每个来源或任务只有一个差值，重复执行先在组内平均。
    count = sum(abs(np.dot(signs, delta) / len(delta)) >= observed - 1e-12
                for signs in itertools.product((-1, 1), repeat=len(delta)))
    rng = np.random.default_rng(100)
    samples = rng.choice(delta, size=(200000, len(delta))).mean(axis=1)
    return {"difference": float(delta.mean()), "p_two_sided": count / 2 ** len(delta),
            "ci95": np.quantile(samples, [0.025, 0.975]).tolist(),
            "paired_units": len(delta), "differences": delta.tolist()}


def holm(comparisons):
    ordered = sorted(comparisons, key=lambda name: comparisons[name]["p_two_sided"])
    previous = 0.0
    for index, name in enumerate(ordered):
        previous = min(1.0, max(previous, comparisons[name]["p_two_sided"] * (len(ordered) - index)))
        comparisons[name]["p_holm"] = previous
    return comparisons


def cash_rows():
    if not LEDGER.exists():
        return []
    with sqlite3.connect(LEDGER.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [dict(zip(("call_id", "phase", "reference_usd", "billed_usd", "reserved_usd", "note"), row))
                for row in db.execute("SELECT id,phase,cost,billed,reserved,note FROM calls ORDER BY at,id")]


def historical_cash_rows():
    result = []
    for name, period in (("research_ledger.sqlite", "original_experiment"), ("ecai_p1_ledger.sqlite", "P1")):
        path = ROOT / "state" / name
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            result.extend({**dict(zip(("call_id", "phase", "reference_usd", "billed_usd", "reserved_usd"), row)),
                "period": period, "ledger": str(path.relative_to(ROOT)), "charge_against_remaining_pool": False}
                for row in db.execute("SELECT id,phase,cost,billed,reserved FROM calls ORDER BY at,id"))
    if len({row["call_id"] for row in result}) != len(result):
        raise ValueError("历史账本存在重复调用 ID，费用归属需要核对")
    return result


def expense(rows, prefix):
    selected = [row for row in rows if row["phase"].startswith(prefix)]
    return {"reference_cny": sum(row["reference_usd"] or 0 for row in selected) * FX,
            "cash_cny": sum(row["billed_usd"] if row["billed_usd"] is not None else row["reference_usd"] or 0 for row in selected) * FX,
            "unsettled_cny": sum(row["reserved_usd"] for row in selected if row["reference_usd"] is None) * FX,
            "calls": len(selected)}


def records(folder, arm):
    return [read(path) for path in sorted((folder / "executions" / arm).glob("*.json"))
            if not path.name.endswith(".failure.json")]


def contract_audit(units, record):
    trace = {event["index"]: event for event in record["boundary_trace"]}
    first = {unit["id"]: next((event["index"] for event in record["boundary_trace"] if activates(unit, event)), None)
             for unit in units}
    violations, missing = [], []
    unit_map = {unit["id"]: unit for unit in units}
    if not record.get("boundary_schema") or not trace or not record.get("wire_trace"):
        missing.append("boundary_or_wire_trace")
    for index, event in enumerate(record["injections"]):
        unit = unit_map.get(event["unit"])
        position = event.get("boundary_index")
        if unit is None or position not in trace:
            missing.append(f"injections/{index}/boundary_or_unit")
            continue
        activation = first[unit["id"]]
        path = f"injections/{index}"
        if activation is None or position < activation:
            violations.append({"path": path, "unit": unit["id"], "boundary": position,
                               "kind": "pre_activation_intervention", "event": trace[position]})
        elif unit["component"] == "E" and not activates(unit, trace[position]):
            violations.append({"path": path, "unit": unit["id"], "boundary": position,
                               "kind": "outside_registration", "event": trace[position]})
        elif event.get("kind") == "knowledge_injection" and not activates(unit, trace[position]):
            violations.append({"path": path, "unit": unit["id"], "boundary": position,
                               "kind": "outside_registration", "event": trace[position]})
    for index, wire in enumerate(record.get("wire_trace", [])):
        position = wire.get("boundary_index")
        if position not in trace or "tool_names" not in wire:
            missing.append(f"wire_trace/{index}/visibility_metadata")
            continue
        for unit in units:
            activation = first[unit["id"]]
            if activation is not None and position >= activation:
                continue
            text = any(unit["content"] in (message.get("content") or "")
                       for message in wire["request"] if isinstance(message.get("content"), str))
            catalog = [name for name in wire["tool_names"] if name.startswith("gnosis_" + unit["id"] + "_")]
            if text or catalog:
                violations.append({"path": f"wire_trace/{index}", "unit": unit["id"], "boundary": position,
                    "kind": "pre_activation_visibility", "content_visible": text, "tools_visible": catalog})
    for unit_id, position in (record.get("active_extensions") or {}).items():
        if unit_id not in first or position != first[unit_id]:
            violations.append({"path": "active_extensions/" + unit_id, "boundary": position,
                               "kind": "activation_position_mismatch"})
    text_positions = [event["boundary_index"] for event in record["injections"]
                      if event.get("kind") == "knowledge_injection" and "boundary_index" in event]
    e_events = [event for event in record["injections"] if event.get("component") == "E"]
    return {"status": "FAIL" if violations else "UNKNOWN" if missing else "PASS",
        "first_registered_activation": first, "first_text_injection": min(text_positions, default=None),
        "first_effect_boundaries": record.get("first_effect_boundaries", {}),
        "tool_catalog_changes": [{"boundary": event.get("boundary_index"), "unit": event["unit"],
            "tools": event.get("output", {}).get("tools")} for event in e_events if "tool_catalog" in event.get("effects", [])],
        "nonempty_effects": sum(bool(event.get("effects")) for event in e_events),
        "capability_calls": [call for event in e_events for call in event.get("actual_calls", [])],
        "violations": violations, "missing": missing}


def candidate_tables(protocol, cash):
    candidates, validation = [], []
    base = {row["task"]: row for row in records(OUT, "B0") if row["learning_role"] == "selection"}
    noise = {row["task"]: row for row in records(OUT, "B0prime") if row["learning_role"] == "selection"}
    source_base = {row["task"]: row for row in records(OUT, "source_B0")}
    pilot_base = {}
    for label in ("C1", "C2", "C3"):
        pilot = read(BASE / "mechanism" / label / "candidate.json")
        first = next((attempt for attempt in pilot["attempts"] if attempt["version"] == components_version([])
                      and attempt["execution_id"] is not None), None)
        if first is not None:
            record = read(BASE / "mechanism/executions" / label / (first["execution_id"] + ".json"))
            pilot_base[record["task"]] = record
    for slot in [*protocol["slots"], *protocol["unavailable_slots"]]:
        folder = OUT / "candidates" / slot["id"]
        candidate = read(folder / "candidate.json") if (folder / "candidate.json").exists() else {"status": "pending", **slot}
        empty_record = source_base.get(slot["source"]) or pilot_base.get(slot["source"], {})
        row = {**candidate, "source_empty_success": empty_record.get("success"),
               "source_baseline_kind": "contemporary" if slot["source"] in source_base else "P1_pilot",
               "source_baseline_execution": empty_record.get("execution_id"),
               "effective_local": None, "audit_complete": False, "local": None,
               "planned_affected": None, "reused": None, "audit_status": "UNKNOWN",
               "decision_consistent": None, "saved_cash_cny": None, "formal_release": None,
               "proposal_cost": expense(cash, "P4/" + slot["id"] + "/proposal"),
               "source_cost": expense(cash, "P4/" + slot["id"] + "/execution/" + candidate["source_execution"])
                              if candidate.get("source_execution") else expense(cash, "P4/" + slot["id"] + "/execution/")}
        if candidate["status"] in TERMINAL - {"mature"}:
            row["effective_local"] = False
        if candidate.get("version"):
            units = read(folder / "versions" / candidate["version"] / "manifest.json")["units"]
            row["local"] = all(unit["scope"] != ["GLOBAL"] for unit in units)
        else:
            units = []
        if candidate["status"] == "mature" and (folder / "validation_plan.json").exists():
            plan = read(folder / "validation_plan.json")
            selected = {item["task"]: item for item in plan}
            runs = {record["task"]: record for record in records(OUT, slot["id"]) if record["learning_role"] == "selection"}
            audits = []
            for task in protocol["mechanism_tasks"]:
                record = runs.get(task)
                item = selected[task]
                audit = contract_audit(units, record) if record else {"status": "UNKNOWN"}
                audits.append(audit)
                cost = expense(cash, "P4/" + slot["id"] + "/execution/" + record["execution_id"]) if record else None
                validation.append({"candidate": slot["id"], "source": slot["source"], "interface": slot["interface"],
                    "task": task, "affected": item["affected"], "predicted_boundary": item["first_boundary"],
                    "B0": base.get(task, {}).get("success"), "B0prime": noise.get(task, {}).get("success"),
                    "candidate_success": record["success"] if record else None,
                    "label_flip": record["success"] != base[task]["success"] if record and task in base else None,
                    "noise_flip": base[task]["success"] != noise[task]["success"] if task in base and task in noise else None,
                    "execution_id": record["execution_id"] if record else None,
                    "B0_id": item["evidence_execution"], "cost": cost, "audit": audit,
                    "tool_calls": len(record["actual_tool_actions"]) if record else None})
            row.update(planned_affected=sum(item["affected"] for item in plan),
                       reused=sum(not item["affected"] for item in plan),
                       audit_complete=len(runs) == len(protocol["mechanism_tasks"]))
            row["coverage"] = "zero_saving" if row["reused"] == 0 else "no_activation_coverage" if row["planned_affected"] == 0 else "mixed"
            row["audit_status"] = "FAIL" if any(audit["status"] == "FAIL" for audit in audits) else (
                "PASS" if row["audit_complete"] and all(audit["status"] == "PASS" for audit in audits) else "UNKNOWN")
            row["full_validation_cost"] = expense(cash, "P4/" + slot["id"] + "/execution/")
            # 来源与验证共享 arm，但执行身份不同，逐调用费用按真实记录关联。
            source_id = candidate["source_execution"]
            row["source_cost"] = expense(cash, "P4/" + slot["id"] + "/execution/" + source_id)
            full_costs = [expense(cash, "P4/" + slot["id"] + "/execution/" + record["execution_id"]) for record in runs.values()]
            selected_costs = [expense(cash, "P4/" + slot["id"] + "/execution/" + record["execution_id"])
                              for task, record in runs.items() if selected[task]["affected"]]
            row["full_validation_cost"] = {key: sum(cost[key] for cost in full_costs) for key in ("cash_cny", "reference_cny", "unsettled_cny")}
            row["selective_validation_cost"] = {key: sum(cost[key] for cost in selected_costs) for key in ("cash_cny", "reference_cny", "unsettled_cny")}
            row["saved_cash_cny"] = row["full_validation_cost"]["cash_cny"] - row["selective_validation_cost"]["cash_cny"]
            if (folder / "selective_decision.json").exists() and (folder / "full_decision.json").exists():
                a, b = read(folder / "selective_decision.json"), read(folder / "full_decision.json")
                row.update(selective_decision=a, full_decision=b, formal_release=b["accepted"],
                           decision_consistent=a["accepted"] == b["accepted"])
                row["disagreement_tasks"] = [task for task in runs if not selected[task]["affected"]
                                             and runs[task]["success"] != base[task]["success"]]
            if row["audit_complete"] and row["decision_consistent"] is not None:
                if row["audit_status"] == "UNKNOWN":
                    row["effective_local"] = None
                else:
                    row["effective_local"] = bool(row["local"] and row["coverage"] == "mixed" and
                        row["audit_status"] == "PASS" and row["decision_consistent"] and row["reused"] > 0)
            if row["audit_complete"]:
                row["deployment_mean_cash_cny"] = row["full_validation_cost"]["cash_cny"] / len(protocol["mechanism_tasks"])
                row["full_process_cash_cny"] = row["proposal_cost"]["cash_cny"] + row["source_cost"]["cash_cny"] + row["full_validation_cost"]["cash_cny"]
                row["selective_process_cash_cny"] = row["proposal_cost"]["cash_cny"] + row["source_cost"]["cash_cny"] + row["selective_validation_cost"]["cash_cny"]
        candidates.append(row)
    return candidates, validation


def source_pairs(protocol, candidates):
    pairs = []
    for source in protocol["sources"]:
        pair = {"source": source["task"], "layer": source["layer"], "domain": source["domain"]}
        for interface in ("A", "B"):
            rows = [row for row in candidates if row["source"] == source["task"] and row["interface"] == interface]
            pair[interface + "_lower"] = sum(row["effective_local"] is True for row in rows) / 2
            pair[interface + "_upper"] = sum(row["effective_local"] is not False for row in rows) / 2
            for metric, values in (("legal", [row.get("legal") for row in rows]),
                                   ("source_success", [row.get("source_success") if row["status"] == "mature" or row["status"] == "source_failed"
                                                       else False if row["status"] in TERMINAL else None for row in rows]),
                                   ("mature_local", [row["local"] if row["status"] == "mature" else False if row["status"] in TERMINAL else None for row in rows])):
                pair[interface + "_" + metric + "_rate"] = sum(values) / 2 if all(value is not None for value in values) else None
                pair[interface + "_" + metric + "_bounds"] = [sum(value is True for value in values) / 2,
                                                               sum(value is not False for value in values) / 2]
        pair["complete"] = all(pair[interface + "_lower"] == pair[interface + "_upper"] for interface in ("A", "B"))
        pair["difference"] = pair["B_lower"] - pair["A_lower"] if pair["complete"] else None
        pair["difference_bounds"] = [pair["B_lower"] - pair["A_upper"], pair["B_upper"] - pair["A_lower"]]
        for metric in ("legal", "source_success", "mature_local"):
            a, b = pair["A_" + metric + "_rate"], pair["B_" + metric + "_rate"]
            pair[metric + "_difference"] = b - a if a is not None and b is not None else None
        pairs.append(pair)
    for domain in protocol["missing_source_domains"]:
        pairs.append({"source": None, "layer": "new", "domain": domain, "complete": False,
            "status": "source_shortfall", "A_lower": 0.0, "A_upper": 1.0, "B_lower": 0.0,
            "B_upper": 1.0, "difference": None, "difference_bounds": [-1.0, 1.0]})
    return pairs


def heldout_tables(protocol, cash):
    rows, grouped = [], {}
    for arm in protocol["versions"]:
        folder = ABLATION if arm == "gnosis_no_e" else HELDOUT
        saved = {(record["task"], record["repeat"]): record for record in records(folder, arm)}
        grouped[arm] = saved
        for task in protocol["heldout_tasks"]:
            for repeat in range(2):
                record = saved.get((task, repeat))
                row = {"arm": arm, "task": task, "repeat": repeat, "status": "completed" if record else "pending",
                       "success": record["success"] if record else None,
                       "partial_credit": record["partial_credit"] if record else None,
                       "execution_id": record["execution_id"] if record else None,
                       "tool_calls": len(record["actual_tool_actions"]) if record else None,
                       "cost": expense(cash, ("P3/" if arm == "gnosis_no_e" else "P2/") + arm + "/execution/" + record["execution_id"]) if record else None,
                       "effects_events": sum(bool(event.get("effects")) for event in record["injections"]) if record else None,
                       "first_effect_boundaries": record.get("first_effect_boundaries") if record else None,
                       "actions": record["actual_tool_actions"] if record and arm in ("gnosis", "gnosis_no_e") else None}
                rows.append(row)
    comparisons = {}
    complete = all(len(saved) == 36 for saved in grouped.values())
    complete_blocks = sum(all((task, repeat) in grouped[arm] for arm in grouped for repeat in range(2))
                          for task in protocol["heldout_tasks"])
    if complete:
        for arm in ("fixed", "global", "gnosis_no_e"):
            delta = [sum(grouped["gnosis"][(task, repeat)]["success"] - grouped[arm][(task, repeat)]["success"]
                         for repeat in range(2)) / 2 for task in protocol["heldout_tasks"]]
            comparisons["gnosis_vs_" + arm] = exact_difference(delta)
        holm(comparisons)
    summaries = {arm: {"completed": len(saved), "success_rate": sum(row["success"] for row in saved.values()) / 36 if len(saved) == 36 else None,
        "effects_executions": sum(any(event.get("effects") for event in row["injections"]) for row in saved.values()),
        "cost": expense(cash, ("P3/" if arm == "gnosis_no_e" else "P2/") + arm + "/")}
        for arm, saved in grouped.items()}
    for arm, summary in summaries.items():
        summary["deployment_mean_cash_cny"] = summary["cost"]["cash_cny"] / 36 if summary["completed"] == 36 else None
    return rows, {"complete": complete, "complete_blocks": complete_blocks, "comparisons": comparisons, "arms": summaries}


def mechanism_annotations(protocol, validation, candidates):
    path = OUT / "mechanism_annotations.json"
    if not path.exists():
        return {"status": "unreviewed", "repeatable": False, "positive_explanation_complete": False, "observations": []}
    observations = read(path)
    indexed = {(row["candidate"], row["task"]): row for row in validation}
    slots = {slot["id"]: slot for slot in protocol["slots"]}
    candidate_map = {row["id"]: row for row in candidates}
    groups = {}
    explained_local_sources = set()
    for observation in observations:
        slot = slots[observation["candidate"]]
        if observation["source"] != slot["source"] or not observation["trigger"] or not observation["mechanism"]:
            raise ValueError("机制解释缺少具体触发条件或来源身份不一致")
        kind = observation["kind"]
        if kind not in ("interface_locality", "local_source_conflict", "contract_failure", "global_dependency", "unresolved"):
            raise ValueError("未知的机制解释类型")
        # 解释由研究者填写；程序只核对真实执行路径，不从关键词推断语义。
        evidence_path = ROOT / observation["evidence_path"]
        if not evidence_path.is_file() or not observation["trace_location"]:
            raise ValueError("机制解释缺少真实证据路径")
        if kind == "contract_failure":
            row = indexed[(observation["candidate"], observation["task"])]
            if not row["audit"].get("violations"):
                raise ValueError("契约失效解释缺少审计违规执行")
        if kind == "local_source_conflict":
            candidate = candidate_map[observation["candidate"]]
            if candidate["local"] is not True or candidate["status"] != "source_failed":
                raise ValueError("局部来源冲突解释需要真实局部候选且来源执行失败")
        if kind == "interface_locality":
            candidate = candidate_map[observation["candidate"]]
            if slot["interface"] != "B" or candidate["effective_local"] is not True:
                raise ValueError("界面局部性解释缺少有效局部 B 候选")
            explained_local_sources.add(slot["source"])
        groups.setdefault((kind, observation["mechanism"]), set()).add(slot["source"])
    new = {source["task"] for source in protocol["sources"] if source["layer"] == "new"}
    repeated = any(len(sources) >= 2 and bool(sources & new) and kind in ("local_source_conflict", "contract_failure")
                   for (kind, _), sources in groups.items())
    return {"status": "reviewed" if observations else "unreviewed", "repeatable": repeated, "observations": observations,
            "positive_explanation_complete": len(explained_local_sources) >= 2,
            "unresolved": any(row["kind"] == "unresolved" for row in observations)}


def report():
    protocol = frozen_protocol()
    cash = cash_rows()
    candidates, validation = candidate_tables(protocol, cash)
    pairs = source_pairs(protocol, candidates)
    new_rows, heldout = heldout_tables(protocol, cash)
    all_terminal = len(candidates) == 24 and all(row["status"] in TERMINAL for row in candidates)
    audits_complete = all(row["audit_complete"] for row in candidates if row["status"] == "mature")
    main = exact_difference([pair["difference"] for pair in pairs]) if all(pair["complete"] for pair in pairs) else None
    liability = liabilities()
    settled = not any(row["reference_usd"] is None for row in cash) and all(row["status"] != "unknown" for row in liability)
    receipt_path = OUT / "latest_balance.json"
    receipt = read(receipt_path) if receipt_path.exists() else None
    paid = sum(row["billed_usd"] if row["billed_usd"] is not None else row["reference_usd"] or 0 for row in cash) * FX
    expected = 134.27 - paid - sum(row["future_cny"] for row in liability if row["status"] == "paid_after_snapshot")
    cash_reconciled = bool(receipt and set(receipt["covered_call_ids"]) == {row["call_id"] for row in cash}
                           and abs(receipt["balance_cny"] - expected) <= receipt["rounding_margin_cny"] + 1e-9)
    financially_complete = settled and cash_reconciled
    source_successes = {row["source"] for row in candidates if row["interface"] == "B" and row["effective_local"] is True
                        and row["saved_cash_cny"] > 0}
    new_improvements = sum(pair["layer"] == "new" and pair["difference"] is not None and pair["difference"] > 0 for pair in pairs)
    annotations = mechanism_annotations(protocol, validation, candidates)
    positive_signal = bool(all_terminal and audits_complete and main and main["difference"] > 0 and main["p_two_sided"] <= 0.05
                           and new_improvements >= 2 and len(source_successes) >= 2 and heldout["complete"] and financially_complete)
    positive = positive_signal and annotations["positive_explanation_complete"] and not annotations.get("unresolved", False)
    complete = all_terminal and audits_complete and heldout["complete"] and financially_complete
    unresolved = annotations.get("unresolved", False)
    route = "PASS" if positive else "未完成" if (not complete or unresolved or annotations["status"] == "unreviewed"
        or positive_signal and not annotations["positive_explanation_complete"]) else (
        "可重复失效机制" if annotations["repeatable"] else "FAIL")
    bounds = {interface: [sum(row["effective_local"] is True for row in candidates if row["interface"] == interface) / 12,
        sum(row["effective_local"] is not False for row in candidates if row["interface"] == interface) / 12] for interface in ("A", "B")}
    shared = sum(expense(cash, "P4/" + name + "/")["cash_cny"] for name in ("source_B0", "B0", "B0prime"))
    interface_costs = {interface: {"full_process_cash_cny": sum(expense(cash, "P4/" + row["id"] + "/")["cash_cny"]
        for row in candidates if row["interface"] == interface) + shared / 2,
        "shared_baseline_allocation_cny": shared / 2,
        "strategy_saving_cny": sum(row["saved_cash_cny"] or 0 for row in candidates if row["interface"] == interface),
        "slot_denominator": 12} for interface in ("A", "B")}
    for interface, cost in interface_costs.items():
        cost["selective_process_cash_cny"] = cost["full_process_cash_cny"] - cost["strategy_saving_cny"]
        rows = [row for row in candidates if row["interface"] == interface]
        cost["proposal_cash_cny"] = sum(row["proposal_cost"]["cash_cny"] for row in rows)
        cost["source_cash_cny"] = sum(row["source_cost"]["cash_cny"] for row in rows)
        cost["full_validation_cash_cny"] = sum(row.get("full_validation_cost", {}).get("cash_cny", 0) for row in rows)
        cost["successful_candidate_cash_cny"] = sum(expense(cash, "P4/" + row["id"] + "/")["cash_cny"]
                                                  for row in rows if row["status"] == "mature")
    for row in cash:
        row.update(period="followup", ledger=str(LEDGER.relative_to(ROOT)),
                   effective_reserved_cny=row["reserved_usd"] * FX if row["reference_usd"] is None else 0)
    historical = historical_cash_rows()
    if {row["call_id"] for row in cash} & {row["call_id"] for row in historical}:
        raise ValueError("新增调用 ID 与历史账本重叠")
    table(OUT / "budget_reconciliation.csv", cash)
    table(OUT / "historical_budget_reconciliation.csv", historical)
    table(OUT / "unknown_liability_reconciliation.csv", liability)
    table(OUT / "p4_candidates.csv", candidates)
    table(OUT / "p4_validation.csv", validation)
    table(OUT / "p4_source_pairs.csv", pairs)
    table(HELDOUT / "p2_heldout_new.csv", [row for row in new_rows if row["arm"] != "gnosis_no_e"])
    controls = {(row["task"], row["repeat"]): row for row in new_rows if row["arm"] == "gnosis"}
    table(ABLATION / "p3_no_e_ablation.csv", [{**row, "gnosis_control": controls[(row["task"], row["repeat"])]}
        for row in new_rows if row["arm"] == "gnosis_no_e"])
    summary = {"execution_complete": all_terminal and audits_complete and heldout["complete"],
        "financially_complete": financially_complete, "p1_original_positive": "FAIL", "p4_route": route,
        "primary": main, "endpoint_bounds": bounds, "source_pairs": pairs,
        "interface_costs": interface_costs, "heldout": heldout, "mechanism": annotations,
        "actual_cash": {stage: expense(cash, stage + "/") for stage in ("P4", "P2", "P3")},
        "unknown_liability": liability, "allocations": ALLOCATIONS,
        "missing_source_domains": protocol["missing_source_domains"],
        "official_cash_reconciliation": {"reconciled": cash_reconciled, "expected_balance_cny": expected, "receipt": receipt}}
    save_json(OUT / "summary.json", summary)
    lines = ["# 投稿路线裁决", "", "P1 原正结果：FAIL。", f"P4 后续机制证据：{route}。",
        f"固定提议槽位结局完整：{all_terminal}；成熟候选审计完整：{audits_complete}。",
        f"P2/P3 完整八执行任务块：{heldout['complete_blocks']}/18；现金核对完整：{financially_complete}。",
        f"来源缺额领域：{','.join(protocol['missing_source_domains']) or '无'}。",
        f"有效局部产出率 A/B 上下界：{json.dumps(bounds, ensure_ascii=False)}。",
        f"六来源配对统计：{json.dumps(main, ensure_ascii=False)}。",
        f"新增来源改善数：{new_improvements}/3；满足实际验证成本下降的 B 来源数：{len(source_successes)}。",
        "", "逐来源证据见 p4_source_pairs.csv，实际干预与具体执行路径见 p4_validation.csv。",
        "验证策略差额与完整审计实际现金分别记录，未结算责任继续占用预留。",
        "机制解释由研究者在 mechanism_annotations.json 登记具体触发条件和真实轨迹位置。"]
    (OUT / "route_decision.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"route": route, "execution_complete": summary["execution_complete"],
                      "model_calls": 0}), flush=True)
    return summary


if __name__ == "__main__":
    report()
