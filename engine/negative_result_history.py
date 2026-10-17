import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def save_rows(path, values):
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ref(path, pointer=""):
    return {"file": str(path).replace("\\", "/"), "pointer": pointer}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def obligation_identity(text):
    if " " not in text:
        return None
    name, params = text.split(" ", 1)
    try:
        return name, canonical(json.loads(params))
    except json.JSONDecodeError:
        return None


def stage_counts(rows):
    selected = [r for r in rows if r["selected_in_reason"]]
    expressed = [r for r in selected if r["written"] == "full" and r["expression"] != "unit_absent"]
    later = [r for r in expressed if r["same_candidate_task_execution_ids"]]
    passed = [r for r in later if r["subsequent_task_outcome"] == "pass"]
    return {"feedback_exposure": len(rows), "selected_in_edit_reason": len(selected),
            "candidate_expression": len(expressed), "later_applicable_execution": len(later),
            "later_pass": len(passed)}


def stage_table(counts):
    names = ["feedback_exposure", "selected_in_edit_reason", "candidate_expression",
             "later_applicable_execution", "later_pass"]
    values = [counts[name] for name in names]
    return [{"stage": name, "requirements": values[index],
             "rate_from_previous": ratio(values[index], values[index - 1] if index else 0),
             "rate_from_exposure": ratio(values[index], values[0])} for index, name in enumerate(names)]


def conversion(counts):
    return {"selected_per_exposure": ratio(counts["selected_in_edit_reason"], counts["feedback_exposure"]),
            "expression_per_selected": ratio(counts["candidate_expression"], counts["selected_in_edit_reason"]),
            "later_execution_per_expression": ratio(counts["later_applicable_execution"], counts["candidate_expression"]),
            "later_pass_per_later_execution": ratio(counts["later_pass"], counts["later_applicable_execution"]),
            "stage_product": ratio(counts["later_pass"], counts["feedback_exposure"])}


def original_path(root, relative):
    path = Path(relative)
    if path.parts[:2] == ("experiments", "research"):
        research = (Path(r"\\wsl.localhost\Ubuntu\home\jonahwu\.gnosis\research") if os.name == "nt"
                    else Path.home() / ".gnosis" / "research")
        return research.joinpath(*path.parts[2:])
    return root / path


def run(root, output):
    root, output = Path(root), Path(output) / "history"
    output.mkdir(parents=True, exist_ok=True)
    discovery = Path("experiments/evo_path/discovery")
    feedback = Path("experiments/feedback_chain")
    evolution = Path("experiments/evolve_mine/evolution")
    extracted = Path("experiments/evolve_mine/extract/exec_features.jsonl")
    raw_outcomes = rows(root / discovery / "outcome.jsonl")
    outcomes = [r for r in raw_outcomes if r["kind"] == "execution"]
    expressions = rows(root / discovery / "expression.jsonl")
    lineage = rows(root / discovery / "lineage.jsonl")
    proposals = rows(root / discovery / "proposal.jsonl")
    features = rows(root / extracted)
    features_by_id = {r["execution_id"]: r for r in features}
    cutoff = datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp()
    registry = {}
    for index, row in enumerate(raw_outcomes):
        if row["kind"] != "execution":
            continue
        identity = row["execution_id"]
        if identity in registry:
            assert registry[identity]["task"] == row["task"]
            assert registry[identity]["version"] == row["version"]
            registry[identity]["aliases"].append(ref(discovery / "outcome.jsonl", f"line:{index + 1}"))
            continue
        registry[identity] = dict(row, aliases=[ref(discovery / "outcome.jsonl", f"line:{index + 1}")],
                                  archived_before_cutoff=bool(row.get("recorded_at") and row["recorded_at"] < cutoff))
        registry[identity]["input_conditions"] = {
            "experiment_version": row.get("experiment_version"), "content_version": row["version"],
            "initial_world_state": "not_extracted_in_historical_table",
            "model_parameters": "not_extracted_in_historical_table",
            "tool_permissions": "not_extracted_in_historical_table",
            "comparison_qualification": "historical_descriptive_only"}
    for index, row in enumerate(features):
        if row["execution_id"] in registry:
            registry[row["execution_id"]]["aliases"].append(ref(extracted, f"line:{index + 1}"))
    save_rows(output / "executions.jsonl", list(registry.values()))

    annotations_path = feedback / "out/annotations_all.json"
    annotations = read(root / annotations_path)
    requests_path = feedback / "source_data/edit_requests_raw.json"
    requests = read(root / requests_path)
    request_index = {(arm, row["action_index"]): (i, row) for arm, values in requests.items()
                     for i, row in enumerate(values)}
    edge_index = {(r["lineage_id"].split("/")[-1], r["event_order"]): r
                  for r in lineage if r["experiment_id"] == "research" and r["kind"] == "edit"}
    edit_events, requirement_events = [], []
    for edit_index, edit in enumerate(annotations["edits"]):
        key = (edit["arm"], edit["action_index"])
        request_position, request = request_index[key]
        edge = edge_index[key]
        event_id = f"research/{edit['arm']}/edit/{edit['action_index']}"
        packet = feedback / "out/edit_packets" / f"{edit['arm']}_{edit['ordinal']:02d}_idx{edit['action_index']}"
        assert (root / packet / "packet.md").is_file()
        attempts = []
        for request_no, attempt in enumerate(request["requests"]):
            attempts.append({"request_file": attempt["request_file"],
                             "working_version": attempt["working_version"],
                             "response_action": attempt.get("response_action", {}).get("action"),
                             "usage": attempt.get("usage"),
                             "reference": ref(requests_path, f"/{edit['arm']}/{request_position}/requests/{request_no}")})
        event = {"event_id": event_id, "arm": edit["arm"], "action_index": edit["action_index"],
                 "parent_version": edge["parent_version"], "candidate_version": edge["candidate_version"],
                 "request_attempts": attempts, "chain_break_existing_annotation": edit["chain_break"],
                 "reference": ref(annotations_path, f"/edits/{edit_index}"), "packet": str(packet / "packet.md")}
        edit_events.append(event)
        for item_index, item in enumerate(edit["items"]):
            matching_details = []
            matching_summaries = []
            for attempt in request["requests"]:
                matching_details.extend(r["execution_id"] for r in attempt["last_execution_feedback"]
                                        if r.get("task") == item["task"] and r.get("execution_id"))
                matching_summaries.extend(r["execution_id"] for r in attempt["available_records"]
                                          if r["task"] == item["task"] and r.get("execution_id"))
            later = [r for r in outcomes if r["arm"] == edit["arm"] and r["task"] == item["task"]
                     and r["version"] == edge["candidate_version"] and r["experiment_id"] == "research"]
            requirement_events.append(dict(item, event_id=event_id, requirement_event_id=f"{event_id}/item/{item_index}",
                                           arm=edit["arm"], parent_version=edge["parent_version"],
                                           candidate_version=edge["candidate_version"],
                                           detailed_feedback_execution_ids=sorted(set(matching_details)),
                                           summary_execution_ids=sorted(set(matching_summaries)),
                                           same_candidate_task_execution_ids=[r["execution_id"] for r in later],
                                           reference=ref(annotations_path, f"/edits/{edit_index}/items/{item_index}"),
                                           semantic_review="existing_annotation",
                                           subsequent_label_unit="task_outcome_existing_annotation"))
    save_rows(output / "edits.jsonl", edit_events)
    save_rows(output / "requirement_events.jsonl", requirement_events)

    distributions = []
    for dimension in ("arm", "task", "event_id"):
        for value in sorted({r[dimension] for r in requirement_events}):
            subset = [r for r in requirement_events if r[dimension] == value]
            for level in sorted({r["delivery"] for r in subset}):
                items = [r for r in subset if r["delivery"] == level]
                distributions.append({"dimension": dimension, "value": value, "delivery": level,
                                      "requirements": len(items), "selected": sum(r["selected_in_reason"] for r in items)})
    save_rows(output / "feedback_distributions.jsonl", distributions)
    task_exposures = []
    for arm, task in sorted({(r["arm"], r["task"]) for r in requirement_events}):
        observed = [r for r in requirement_events if r["arm"] == arm and r["task"] == task]
        per_edit = defaultdict(list)
        for item in observed:
            per_edit[item["event_id"]].append(item)
        events = sorted(per_edit, key=lambda v: int(v.split("/")[-1]))
        first_selected = next((i for i, event in enumerate(events)
                               if any(r["selected_in_reason"] for r in per_edit[event])), None)
        task_exposures.append({"arm": arm, "task": task, "exposure_edits": len(events),
                               "selected_edits": sum(any(r["selected_in_reason"] for r in per_edit[event]) for event in events),
                               "edits_before_first_task_selection": first_selected,
                               "right_censored": first_selected is None, "event_ids": events,
                               "identity_unit": "task_level_feedback; semantic_requirement_identity_not_inferred"})
    save_rows(output / "task_feedback_waiting.jsonl", task_exposures)

    delivery = {}
    for value in sorted({r["delivery"] for r in requirement_events}):
        subset = [r for r in requirement_events if r["delivery"] == value]
        selected = sum(bool(r["selected_in_reason"]) for r in subset)
        delivery[value] = {"requirements": len(subset), "selected": selected, "selected_rate": selected / len(subset)}
    selected = [r for r in requirement_events if r["selected_in_reason"]]
    full = [r for r in requirement_events if r["written"] == "full"]
    feedback_summary = {"edits": len(edit_events), "requirements": len(requirement_events),
                        "delivery": delivery, "selected": len(selected),
                        "written_given_selected": dict(Counter(r["written"] for r in selected)),
                        "all_full_written": len(full),
                        "task_outcome_given_all_full_written": dict(Counter(r["subsequent_task_outcome"] for r in full)),
                        "selected_full_task_outcomes": dict(Counter(r["subsequent_task_outcome"] for r in selected if r["written"] == "full")),
                        "chain_break": dict(Counter(e["chain_break_existing_annotation"] for e in edit_events)),
                        "semantic_status": "existing_263_annotations_plus_explicit_case_reviews"}

    assertion_map_path = feedback / "out/assertion_map.json"
    assertion_map = read(root / assertion_map_path)
    review_path = Path("experiments/negative_result_offline/business/execution_reviews.json")
    obligation_path = Path("experiments/negative_result_offline/business/task_requirements.json")
    reviews = read(root / review_path)
    obligated = read(root / obligation_path)["tasks"]
    obligation_index = {}
    for task, items in obligated.items():
        obligation_index[task] = {}
        for item in items:
            identity = obligation_identity(item["requirement"])
            if identity:
                obligation_index[task][identity] = item
    review_by_task = defaultdict(list)
    for review in reviews:
        review_by_task[review["task"]].append(review)
    review_positions = {review["execution_id"]: index for index, review in enumerate(reviews)}
    review_executions = {}
    status_path = Path("experiments/negative_result_offline/history/candidate_status.json")
    candidate_status = read(root / status_path)
    deployment_status = {(slot["domain"], slot["arm"]): (slot["status"], slot["deployed_version"])
                         for slot in candidate_status["slots"]}
    ancestor_version = candidate_status["protocol"]["ancestor_version"]
    retention = []
    cases_path = feedback / "out/annotations_all.json"
    for case_index, case in enumerate(annotations["cases"]):
        if case["exposure_scope"] != "all_task_executions":
            continue
        observed = []
        for execution_id, execution in assertion_map.items():
            if execution["t"] != case["task"] or execution_id not in registry:
                continue
            matches = [a for a in execution["as"] if a[0] == case["assertion_type"]
                       and canonical(json.loads(a[1])) == canonical(case["params"])]
            if not matches:
                continue
            assert len({a[2] for a in matches}) == 1
            observed.append({"execution_id": execution_id, "passed": bool(matches[0][2]),
                             "arm": execution["a"], "version": registry[execution_id]["version"],
                             "recorded_at": registry[execution_id]["recorded_at"],
                             "reference": ref(assertion_map_path, "/" + execution_id)})
        by_arm = defaultdict(list)
        for item in observed:
            by_arm[item["arm"]].append(item)
        obligation = obligation_index.get(case["task"], {}).get((case["assertion_type"], canonical(case["params"])))
        business_observations = []
        for review in review_by_task.get(case["task"], []):
            matched = [r for r in review["requirements"] if obligation is not None and r["id"] == obligation["id"]]
            if not matched:
                continue
            if review["source_path"] not in review_executions:
                review_executions[review["source_path"]] = read(root / review["source_path"])
            record = review_executions[review["source_path"]]
            arm_key = review["arm"][:-len("_source")] if review["arm"].endswith("_source") else review["arm"]
            if review["arm"] == "source":
                deployment = ("ancestor_first_run", ancestor_version)
            else:
                assert (case["task"].split(".")[0], arm_key) in deployment_status
                deployment = deployment_status[(case["task"].split(".")[0], arm_key)]
            assertion = None
            if obligation["id"].startswith("frozen:"):
                position = int(obligation["id"].split(":")[1])
                if position < len(record["assertions"]):
                    assertion = record["assertions"][position]
                    assert (assertion["type"], canonical(assertion["params"])) == (case["assertion_type"], canonical(case["params"]))
            business_observations.append({"arm": review["arm"], "execution_id": review["execution_id"],
                                          "passed": bool(matched[0]["passed"]), "req_class": matched[0]["req_class"],
                                          "obligation_source": matched[0]["source"],
                                          "full_business_success": bool(review["full_business_success"]),
                                          "recorded_at": record["recorded_at"],
                                          "version": record["version"], "deployment_status": deployment[0],
                                          "version_matches_deployment": record["version"] == deployment[1],
                                          "assertion_record_passed": None if assertion is None else bool(assertion["passed"]),
                                          "judgement_matches_assertion_record": None if assertion is None
                                          else bool(assertion["passed"]) == bool(matched[0]["passed"]),
                                          "reference": ref(review_path, f"/{review_positions[review['execution_id']]}")})
        business_observations.sort(key=lambda r: (r["recorded_at"], r["execution_id"]))
        passed_observations = sum(r["passed"] for r in business_observations)
        if not review_by_task.get(case["task"]):
            business_status = "no_later_business_execution_for_task"
        elif obligation is None:
            business_status = "no_business_obligation_identity"
        elif passed_observations == len(business_observations):
            business_status = "retained_under_business_review"
        elif passed_observations == 0:
            business_status = "regressed_under_business_review"
        else:
            business_status = "partially_retained_under_business_review"
        for arm, values in sorted(by_arm.items()):
            values.sort(key=lambda r: (r["recorded_at"], r["execution_id"]))
            first_pass = next((i for i, r in enumerate(values) if r["passed"]), None)
            followup = values[first_pass + 1:] if first_pass is not None else []
            retention.append({"case_id": f"case/{case_index}", "task": case["task"], "arm": arm,
                              "requirement": case["semantics_zh"], "disputed": case["dispute_flag"],
                              "counterexample_genuine": case["counterexample_genuine"],
                              "requirement_identity": {"task": case["task"], "assertion_type": case["assertion_type"],
                                                       "params": case["params"], "req_class": case["req_class"]},
                              "reference": ref(cases_path, f"/cases/{case_index}"),
                              "observations": values, "first_verified_assertion_pass": first_pass,
                              "later_observed": len(followup), "later_passed": sum(r["passed"] for r in followup),
                              "later_failed": sum(not r["passed"] for r in followup),
                              "measurement": "exact_assertion_retention_on_existing_annotated_applicable_cases",
                              "business_obligation": None if obligation is None else
                              {k: obligation[k] for k in ("id", "requirement", "req_class", "source")},
                              "business_review_scope": "paid_experiment_executions_of_the_same_task_with_the_same_obligation_identity",
                              "business_observations": business_observations,
                              "business_later_executions": len(business_observations),
                              "business_later_passed": passed_observations,
                              "business_later_failed": len(business_observations) - passed_observations,
                              "business_later_task_full_successes": sum(r["full_business_success"] for r in business_observations),
                              "business_later_than_historical": all(
                                  r["recorded_at"] > values[-1]["recorded_at"] for r in business_observations) if business_observations else None,
                              "business_retention_status": business_status})
    save_rows(output / "retention.jsonl", retention)

    expression_groups = defaultdict(list)
    for index, expression in enumerate(expressions):
        expression_groups[(expression["experiment_id"], expression["repair_identity"])].append((index, expression))
    expression_table = []
    for (experiment, unit), values in sorted(expression_groups.items()):
        expression_table.append({"experiment": experiment, "unit": unit, "rows": len(values),
                                 "executions": len({r["execution_id"] for _, r in values}),
                                 "states": dict(Counter(r["state"] for _, r in values)),
                                 "affected": sum(bool(r["affected"]) for _, r in values),
                                 "component": sorted({str(r.get("component")) for _, r in values}),
                                 "references": [ref(discovery / "expression.jsonl", f"line:{i + 1}") for i, _ in values]})
    save_rows(output / "expression_units.jsonl", expression_table)

    component_events = []
    component_execution_ids = sorted({r["execution_id"] for r in expressions if r.get("component") == "E"})
    for execution_id in component_execution_ids:
        execution = registry[execution_id]
        path = original_path(root, execution["evidence_path"])
        raw = read(path)
        assert raw["execution_id"] == execution_id
        for event_no, event in enumerate(raw["injections"]):
            if event.get("component") != "E":
                continue
            boundary_no = event.get("boundary_index")
            boundary = next((b for b in raw["boundary_trace"] if b["index"] == boundary_no), None)
            component_events.append({"execution_id": execution_id, "experiment_id": execution["experiment_id"],
                                     "task": execution["task"], "version": execution["version"],
                                     "strict_success": execution["strict_success"], "unit": event["unit"],
                                     "hook": event.get("hook"), "boundary_index": boundary_no,
                                     "input": event.get("input"), "output": event.get("output"),
                                     "actual_calls": event.get("actual_calls"), "effects": event.get("effects"),
                                     "next_actual_tool_action": event.get("next_actual_tool_action"),
                                     "boundary_payload": boundary,
                                     "reference": ref(path, f"/injections/{event_no}"),
                                     "per_event_seconds": None, "per_event_cost": None})
    save_rows(output / "e_component_events.jsonl", component_events)
    component_summary = []
    for unit in sorted({r["unit"] for r in component_events}):
        unit_events = [r for r in component_events if r["unit"] == unit]
        component_summary.append({"unit": unit, "executions": len({r["execution_id"] for r in unit_events}),
                                  "invocations": len(unit_events), "effect_events": sum(bool(r["effects"]) for r in unit_events),
                                  "effect_on_strict_successful_execution": sum(bool(r["effects"]) and r["strict_success"] for r in unit_events),
                                  "additional_tool_calls": sum(len(r["actual_calls"] or []) for r in unit_events),
                                  "hooks": dict(Counter(r["hook"] for r in unit_events)),
                                  "measurement": "component_events; business_effect_review_separate"})
    save(output / "e_component_summary.json", component_summary)
    unit_version_events = []
    for edit in edit_events:
        arm = edit["arm"]
        base = feedback / "source_data/versions" / arm
        parent_path = base / edit["parent_version"] / "manifest.json"
        child_path = base / edit["candidate_version"] / "manifest.json"
        before = {r["id"]: r for r in read(root / parent_path)["units"]}
        after = {r["id"]: r for r in read(root / child_path)["units"]}
        for unit in sorted(before.keys() | after.keys()):
            change = "added" if unit not in before else "removed" if unit not in after else "unchanged" if before[unit] == after[unit] else "modified"
            unit_version_events.append({"event_id": edit["event_id"], "arm": arm, "unit": unit,
                                        "parent_version": edit["parent_version"], "candidate_version": edit["candidate_version"],
                                        "change": change, "parent_reference": ref(parent_path), "child_reference": ref(child_path),
                                        "rename_or_split_identity": "not_assigned_without_semantic_review"})
    save_rows(output / "unit_version_events.jsonl", unit_version_events)

    generations_path = evolution / "generational_diff.json"
    generations = read(root / generations_path)
    seen_pairs, pairs = set(), []
    for index, pair in enumerate(generations["pairs"]):
        identity = (pair["lineage_id"], pair["task"], pair["parent_version"], pair["child_version"],
                    tuple(sorted(pair["parent_execution_ids"])), tuple(sorted(pair["child_execution_ids"])))
        if identity in seen_pairs:
            continue
        seen_pairs.add(identity)
        parents = [features_by_id[e] for e in pair["parent_execution_ids"]]
        children = [features_by_id[e] for e in pair["child_execution_ids"]]
        result = {"lineage_id": pair["lineage_id"], "task": pair["task"],
                  "parent_version": pair["parent_version"], "child_version": pair["child_version"],
                  "parent_execution_ids": pair["parent_execution_ids"], "child_execution_ids": pair["child_execution_ids"],
                  "delta_strict_success": pair["child_success_rate"] - pair["parent_success_rate"],
                  "delta_partial": pair["delta_pc"], "existing_exploratory_class": pair["outcome_class"],
                  "reference": ref(generations_path, f"/pairs/{index}")}
        for metric in ("n_failed", "n_actions", "cost_usd"):
            result["parent_" + metric] = mean(p[metric] for p in parents)
            result["child_" + metric] = mean(p[metric] for p in children)
            result["delta_" + metric] = result["child_" + metric] - result["parent_" + metric]
        pairs.append(result)
    save_rows(output / "generational_pairs.jsonl", pairs)
    p2 = [r for r in features if r["experiment_id"] == "ecai_p2"]
    p2_tasks = []
    for (arm, task) in sorted({(r["arm"], r["task"]) for r in p2}):
        values = [r for r in p2 if r["arm"] == arm and r["task"] == task]
        p2_tasks.append({"arm": arm, "task": task, "executions": len(values),
                         "strict_success_rate": mean(registry[r["execution_id"]]["strict_success"] for r in values),
                         "mean_tool_failures": mean(r["n_failed"] for r in values),
                         "mean_actions": mean(r["n_actions"] for r in values),
                         "mean_cost_usd": mean(r["cost_usd"] for r in values),
                         "execution_ids": [r["execution_id"] for r in values]})
    save_rows(output / "p2_task_metrics.jsonl", p2_tasks)
    p2_arms = {}
    for arm in sorted({r["arm"] for r in p2_tasks}):
        values = [r for r in p2_tasks if r["arm"] == arm]
        p2_arms[arm] = {"tasks": len(values), "executions": sum(r["executions"] for r in values),
                        **{metric: mean(r[metric] for r in values) for metric in
                           ("strict_success_rate", "mean_tool_failures", "mean_actions", "mean_cost_usd")}}
    zero_tool_failure = [r for r in features if r["n_actions"] > 0 and r["n_failed"] == 0
                         and not registry[r["execution_id"]]["strict_success"]]
    save_rows(output / "zero_tool_failure_executions.jsonl", zero_tool_failure)
    efficiency = {"p2": p2_arms, "generational_original": len(generations["pairs"]),
                  "generational_unique": len(pairs),
                  "fewer_failed_calls_and_higher_strict_success": sum(r["delta_n_failed"] < 0 and r["delta_strict_success"] > 0 for r in pairs),
                  "fewer_failed_calls_without_higher_strict_success": sum(r["delta_n_failed"] < 0 and r["delta_strict_success"] <= 0 for r in pairs),
                  "lower_cost_with_no_strict_loss": sum(r["delta_cost_usd"] < 0 and r["delta_strict_success"] >= 0 for r in pairs),
                  "zero_tool_failure_strict_failure": len(zero_tool_failure),
                  "case_groups": len(annotations["cases"]),
                  "case_disputes": sum(c["dispute_flag"] for c in annotations["cases"]),
                  "case_failure_modes": dict(Counter(mode for c in annotations["cases"] for mode in c["failure_mode"])),
                  "failure_group_denominator": "selected_failure_cases_only; no_population_rate_computed"}

    timing_path = Path("experiments/timing_v4_recovery/analysis.json")
    timing = read(root / timing_path)
    formal_timing = [r for r in outcomes if r["experiment_id"] == "timing_v4" and r["task_role"] == "audit"]
    timing_summary = {k: timing[k] for k in ("sample_size", "primary", "secondary", "noninferiority", "total_paid_audit")}
    timing_summary["reference"] = ref(timing_path)
    timing_summary["execution_table_formal_role_counts"] = dict(Counter(r["task_role"] for r in outcomes if r["experiment_id"] == "timing_v4"))
    timing_summary["source_L_minus_D_recomputed"] = {s["source"]: s["means"]["L"] - s["means"]["D"] for s in timing["sources"]}
    timing_summary["G_equal_N_sources"] = sum(s["means"]["G"] == s["means"]["N"] for s in timing["sources"])
    skillaa_path = Path("experiments/timing_v4_recovery/skillaa/analysis.json")
    skillaa = read(root / skillaa_path)
    skillaa["no_edit_source"] = read(root / "experiments/timing_v4_recovery/skillaa/edits/10d7970932a641828c3d3aa4c99db22e/status.json")
    skillaa["reference"] = ref(skillaa_path)
    save(output / "timing.json", timing_summary)
    save(output / "skillaa.json", skillaa)
    save_rows(output / "version_events.jsonl", [dict(r, reference=ref(discovery / "lineage.jsonl", f"line:{i + 1}")) for i, r in enumerate(lineage)])
    save_rows(output / "proposal_attempts.jsonl", [dict(r, reference=ref(discovery / "proposal.jsonl", f"line:{i + 1}")) for i, r in enumerate(proposals)])

    manifest_units = {}
    for event in requirement_events:
        key = (event["arm"], event["candidate_version"])
        if key not in manifest_units:
            path = feedback / "source_data/versions" / key[0] / key[1] / "manifest.json"
            manifest_units[key] = {unit["id"] for unit in read(root / path)["units"]}
    used_versions = {(row["arm"], row["version"]) for row in outcomes}
    unit_changes = defaultdict(dict)
    for event in unit_version_events:
        unit_changes[event["event_id"]][event["unit"]] = event["change"]
    exposure = []
    for event in requirement_events:
        units = manifest_units[(event["arm"], event["candidate_version"])]
        changes = unit_changes.get(event["event_id"], {})
        referenced = event["unit_ids"]
        expression = "absent" if event["written"] == "none" else "partial_content" if event["written"] == "partial" \
            else "candidate_content" if referenced else "removal_only"
        if expression == "candidate_content" and any(unit not in units for unit in referenced):
            expression = "unit_absent"
        exposure.append(dict(event, expression=expression,
                             expression_units=[{"unit": unit, "change": changes.get(unit, "no_change_record"),
                                                "in_candidate_version": unit in units} for unit in referenced],
                             candidate_version_used_in_later_execution=(event["arm"], event["candidate_version"]) in used_versions))
    stage_tables = []
    for dimension in ("delivery", "arm", "task", "event_id"):
        for value in sorted({r[dimension] for r in exposure}):
            subset = [r for r in exposure if r[dimension] == value]
            counts = stage_counts(subset)
            stage_tables.append(dict({"dimension": dimension, "value": value}, **counts, conversion=conversion(counts)))
    counts = stage_counts(exposure)
    references = [unit for event in exposure for unit in event["expression_units"]]
    full_written = [r for r in exposure if r["written"] == "full"]
    retention_verified = [r for r in retention if r["first_verified_assertion_pass"] is not None]
    retention_followed = [r for r in retention_verified if r["later_observed"] > 0]
    retention_held = [r for r in retention_followed if r["later_failed"] == 0]
    business_rows = [r for r in retention if r["business_observations"]]
    identity_groups = defaultdict(list)
    for event in exposure:
        identity_groups[(event["arm"], event["task"], event["requirement"])].append(event)
    repeated = []
    for (arm, task, requirement), values in sorted(identity_groups.items()):
        if len(values) < 2:
            continue
        values.sort(key=lambda r: int(r["event_id"].split("/")[-1]))
        first = next((index for index, r in enumerate(values) if r["selected_in_reason"]), None)
        repeated.append({"arm": arm, "task": task, "requirement": requirement, "exposures": len(values),
                         "selected_exposures": sum(r["selected_in_reason"] for r in values),
                         "unselected_exposures": sum(not r["selected_in_reason"] for r in values),
                         "waiting_exposures_before_first_selection": first,
                         "never_selected_exposures": len(values) if first is None else 0,
                         "right_censored": first is None,
                         "event_ids": [r["event_id"] for r in values],
                         "identity_rule": "arm+task+exact_annotation_text"})
    exposure_chain = {
        "unit": "edit 反馈包中的一条要求条目（要求暴露）",
        "sequence": "feedback_exposure > selected_in_edit_reason > candidate_expression > later_applicable_execution > later_pass",
        "stages": stage_table(counts),
        "conversion": conversion(counts),
        "stage_product": {"numerator": counts["later_pass"], "denominator": counts["feedback_exposure"],
                          "rate": ratio(counts["later_pass"], counts["feedback_exposure"])},
        "superset_relations": {"written_full_total": len(full_written),
                               "selected_and_written_full": sum(r["selected_in_reason"] for r in full_written),
                               "written_full_without_selection": sum(not r["selected_in_reason"] for r in full_written),
                               "written_full_with_later_execution": sum(bool(r["same_candidate_task_execution_ids"]) for r in full_written),
                               "written_full_with_later_pass": sum(r["subsequent_task_outcome"] == "pass" for r in full_written),
                               "selected_written_full_with_later_execution": counts["later_applicable_execution"],
                               "selected_written_full_with_later_pass": counts["later_pass"],
                               "historical_counts": "179、159、80 分别对应 written_full_total、selected_and_written_full、written_full_with_later_execution"},
        "expression_verification": {"requirement_items_with_unit_reference": sum(bool(r["unit_ids"]) for r in exposure),
                                    "unit_references": len(references),
                                    "unit_references_in_candidate_version": sum(u["in_candidate_version"] for u in references),
                                    "unit_change_in_same_edit": dict(Counter(u["change"] for u in references)),
                                    "written_full_without_unit_reference": sum(r["written"] == "full" and not r["unit_ids"] for r in exposure),
                                    "candidate_version_used_in_later_execution": sum(r["candidate_version_used_in_later_execution"] for r in exposure),
                                    "measurement": "写入单元必须在候选版本 manifest 中存在；候选版本是否进入后继真实执行单独登记，不以存在代替执行"},
        "repeated_requirement_identity": {"groups": len(repeated), "exposures_in_repeated_groups": sum(r["exposures"] for r in repeated),
                                          "groups_never_selected": sum(r["right_censored"] for r in repeated), "rows": repeated},
        "without_observation": {"selected_written_full_without_later_execution": counts["candidate_expression"] - counts["later_applicable_execution"],
                                "retention_sequences_without_later_historical_observation": len(retention_verified) - len(retention_followed),
                                "retention_sequences_without_later_business_observation": len(retention) - len(business_rows),
                                "registration_limit": "候选版本没有后继执行记录时，记录中无法区分当时没有遇到对应对象还是没有复测"},
        "stage_tables": stage_tables,
        "label_units": {"feedback_exposure": "反馈包要求条目", "selected_in_edit_reason": "编辑理由选择",
                        "candidate_expression": "写入候选单元（写入单元在候选版本 manifest 中存在）",
                        "later_applicable_execution": "同任务同候选版本后继执行（既有任务级标注）",
                        "later_pass": "后继执行任务通过（既有任务级标注）"}}
    obligations = {}
    for row in retention:
        obligation = row["business_obligation"]
        if obligation is None:
            continue
        entry = obligations.setdefault((row["task"], obligation["id"]),
                                       {"task": row["task"], "obligation": obligation["id"],
                                        "requirement": obligation["requirement"], "req_class": obligation["req_class"],
                                        "obligation_source": obligation["source"], "sequences": 0,
                                        "sequences_with_later_business_execution": 0,
                                        "observation_executions": {}, "full_task_success_executions": {}})
        entry["sequences"] += 1
        entry["sequences_with_later_business_execution"] += bool(row["business_observations"])
        for observation in row["business_observations"]:
            entry["observation_executions"][observation["execution_id"]] = observation["passed"]
            if observation["full_business_success"]:
                entry["full_task_success_executions"][observation["execution_id"]] = True
    obligation_rows = []
    for key, entry in sorted(obligations.items()):
        values = list(entry["observation_executions"].values())
        passed = sum(values)
        obligation_rows.append({"task": entry["task"], "obligation": entry["obligation"],
                                "requirement": entry["requirement"], "req_class": entry["req_class"],
                                "obligation_source": entry["obligation_source"], "retention_sequences": entry["sequences"],
                                "later_business_executions": len(values), "later_passed": passed,
                                "later_failed": len(values) - passed,
                                "later_task_full_successes": len(entry["full_task_success_executions"]),
                                "status": "retained" if passed == len(values) else "regressed" if not passed else "partially_retained"})
    judgements = [o for row in retention for o in row["business_observations"] if o["judgement_matches_assertion_record"] is not None]
    retention_chain = {
        "unit": "断言义务（case 与臂构成的序列）",
        "stages": [{"stage": "verified_assertion_pass", "sequences": len(retention_verified), "rate_from_previous": None},
                   {"stage": "later_historical_observation", "sequences": len(retention_followed),
                    "rate_from_previous": ratio(len(retention_followed), len(retention_verified))},
                   {"stage": "later_historical_without_failure", "sequences": len(retention_held),
                    "rate_from_previous": ratio(len(retention_held), len(retention_followed))}],
        "stage_product": {"numerator": len(retention_held), "denominator": len(retention_verified),
                          "rate": ratio(len(retention_held), len(retention_verified))},
        "business_verification": {
            "review_reference": ref(review_path), "obligation_reference": ref(obligation_path),
            "later_window": "同任务的付费实验来源执行（source 为祖先首跑，F0R0_source、F1R0_source、F0R1_source、F1R1_source 各为该臂部署版本）",
            "sequences_with_later_business_execution": len(business_rows),
            "sequences_without_later_business_execution": len(retention) - len(business_rows),
            "status_counts": dict(Counter(r["business_retention_status"] for r in retention)),
            "tasks_with_later_business_execution_and_obligation_identity": sorted({r["task"] for r in business_rows}),
            "tasks_with_later_business_execution_without_obligation_identity":
                sorted({r["task"] for r in retention if review_by_task.get(r["task"]) and not r["business_observations"]}),
            "tasks_without_later_business_execution": sorted({r["task"] for r in retention if not review_by_task.get(r["task"])}),
            "obligations_with_later_business_execution": len(obligation_rows),
            "obligation_status_counts": dict(Counter(r["status"] for r in obligation_rows)),
            "obligations_without_any_later_task_full_success": sum(r["later_task_full_successes"] == 0 for r in obligation_rows),
            "obligation_rows": obligation_rows,
            "judgement_agreement_with_assertion_record": {"observations_compared": len(judgements),
                                                          "agreeing": sum(o["judgement_matches_assertion_record"] for o in judgements)},
            "deployment_status_of_later_executions": dict(Counter(o["deployment_status"] for r in retention for o in r["business_observations"])),
            "later_executions_running_the_arm_deployment": sum(o["version_matches_deployment"] for r in retention for o in r["business_observations"]),
            "later_execution_observations": sum(len(r["business_observations"]) for r in retention),
            "measurement": "同一冻结义务身份在更晚的同任务真实执行中的判读；业务评价对冻结义务沿用既有断言判读，新增义务未出现在历史保留 case 中",
            "window_ordering": all(r["business_later_than_historical"] for r in business_rows)}}
    nesting_rules = ["阶段比例只在上下游集合确实嵌套时相乘：暴露链每一步都对上一步取子集。",
                     "later_applicable_execution 与 later_pass 来自既有任务级标注，不是要求级判读；要求级判读只出现在保留链。",
                     "历史保留窗口与业务核验窗口是两段不同执行，二者不嵌套，分别报告比例，不相乘。"]
    candidate_context = {"reference": ref(status_path), "slots": len(candidate_status["slots"]),
                         "slot_status": candidate_status["generation"]["slot_status"],
                         "terminal_generation_loss": candidate_status["generation"]["terminal_generation_loss"],
                         "units_injected_per_arm": candidate_status["blocks"][0]["runtime"]["units_injected_per_arm"],
                         "measurement": "同一版块谱系的付费执行按版本注入全部单元；写入单元随候选版本进入请求的机制由该记录支持，单元存在不等于执行结果"}
    save(output / "feedback_stages.json", {"protocol": {"exposure_layer": "experiments/feedback_chain/out/annotations_all.json 的 263 条要求条目",
                                                        "retention_layer": "experiments/feedback_chain/out/assertion_map.json 的断言序列",
                                                        "business_layer": "experiments/negative_result_offline/business/execution_reviews.json 的 462 次评价",
                                                        "plan_reference": ref(Path("docs/GNOSIS-负结果论文离线深化与证据整合计划-2026-10-16.md"), "§10 §11")},
                                             "exposure_chain": exposure_chain, "retention_chain": retention_chain,
                                             "candidate_context": candidate_context, "nesting_rules": nesting_rules})
    feedback_summary.update({"stage_product_computed": True, "stage_product": exposure_chain["stage_product"],
                             "stage_product_chain": exposure_chain["sequence"], "stage_counts": counts,
                             "stage_conversion": exposure_chain["conversion"],
                             "expression_verification": exposure_chain["expression_verification"],
                             "stage_label_units": exposure_chain["label_units"]})

    summary = {"counts": {"execution_rows": len(outcomes), "unique_executions": len(registry),
                          "feature_aliases": sum(r["execution_id"] in registry for r in features),
                          "pre_cutoff": sum(r["archived_before_cutoff"] for r in registry.values()),
                          "lineage_events": len(lineage), "proposal_records": len(proposals), "expression_records": len(expressions)},
               "feedback": feedback_summary, "efficiency": efficiency,
               "expression": {"states": dict(Counter(r["state"] for r in expressions)),
                              "units_by_experiment": len(expression_table),
                              "component_events": len(component_events), "component_units": component_summary,
                              "affected_E_rows": sum(bool(r["affected"]) for r in expressions if r.get("component") == "E")},
               "retention": {"exact_assertion_sequences": len(retention),
                             "sequences_with_verified_pass_and_followup": sum(r["later_observed"] > 0 for r in retention),
                             "later_observations": sum(r["later_observed"] for r in retention),
                             "later_passed": sum(r["later_passed"] for r in retention),
                             "later_failed": sum(r["later_failed"] for r in retention),
                             "historical_stage_product": retention_chain["stage_product"],
                             "business_function_retention_fully_reviewed": True,
                             "business_function_retention_review_scope":
                                 "每条保留序列都已按业务评价结果分类；更晚的同任务观察覆盖五道保留任务（另一道缺对应义务条目），其余六道保留任务登记为尚无业务观察机会",
                             "business_retention_status_counts": dict(Counter(r["business_retention_status"] for r in retention)),
                             "business_retention_task_counts": {
                                 "later_business_execution_with_obligation_identity": len({r["task"] for r in business_rows}),
                                 "later_business_execution_without_obligation_identity":
                                     len({r["task"] for r in retention if review_by_task.get(r["task"]) and not r["business_observations"]}),
                                 "without_later_business_execution":
                                     len({r["task"] for r in retention if not review_by_task.get(r["task"])})},
                             "business_retention_later_executions_per_task": 5,
                             "business_retention_obligation_status_counts": dict(Counter(r["status"] for r in obligation_rows)),
                             "business_retention_obligations_without_any_later_task_full_success":
                                 sum(r["later_task_full_successes"] == 0 for r in obligation_rows),
                             "business_retention_window_later_than_historical_observations":
                                 all(r["business_later_than_historical"] for r in business_rows),
                             "business_retention_judgement_agreement_with_assertion_record":
                                 retention_chain["business_verification"]["judgement_agreement_with_assertion_record"]},
               "cost": {"historical_execution_reference_cny": sum(r["cost_cny_reference"] for r in registry.values()),
                        "historical_execution_billed_cny": sum(r["cost_cny_billed"] for r in registry.values()),
                        "proposal_records_reference_cny": sum(r["cost_reference_cny"] or 0 for r in proposals),
                        "proposal_records_billed_cny": sum(r["cost_billed_cny"] or 0 for r in proposals),
                        "cost_scope": "execution_and_proposal_records_are_not_a_disjoint_ledger_total"},
               "timing": timing_summary, "skillaa": skillaa,
               "unresolved": ["2006次历史执行的初态、任务权限及实际请求已恢复，完整条件见 completion/historical_input_summary.json；跨批比较保留原批次身份。",
                              "要求级阅读、原始上下文及后继义务结果见 requirements/events.json 与 requirements/observations.json；研究者复核确认见 business/human_review.json。",
                              "要求完成及观察机会以 completion/requirement_flow.json 为准；本文件保留的任务级后继标注用于展示历史标注来源。",
                              "功能保留的扩展观察见 requirements/retention.json；未观察到适用机会的序列单独保留。",
                              "历史提议记录含 inspect 等动作，599 条不能命名为 599 次修改。"]}
    save(output / "summary.json", summary)
    with (output / "feedback_stages.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["delivery", "requirements", "selected", "selected_rate"])
        writer.writeheader()
        writer.writerows(dict(delivery=k, **v) for k, v in delivery.items())
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    args = parser.parse_args()
    result = run(args.root, args.output)
    print(json.dumps({"counts": result["counts"], "feedback": result["feedback"], "efficiency": result["efficiency"],
                      "retention": result["retention"]}, ensure_ascii=False))
