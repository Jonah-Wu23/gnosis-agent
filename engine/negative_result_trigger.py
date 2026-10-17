import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean

from jsonpointer import resolve_pointer

ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
FACTOR_F = {"F0": "编辑请求不含整理后的反馈", "F1": "编辑请求含整理后的反馈"}
FACTOR_R = {"R0": "改动单元用普通文本", "R1": "改动单元必须写成边界表示 JSON"}
FX = 7.1
# 付费实验按闲时单价记账：输入 1 元、缓存命中 0.02 元、输出 4 元每百万 token
RATES = {"input_per_mtok": 1.0, "cache_read_per_mtok": 0.02, "output_per_mtok": 4.0}
BOUNDARY_KEYS = {"applies_when", "avoids", "guidance"}
VALUE_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?|[A-Z]{2,}-?\d+[A-Za-z0-9-]*")
# engine/run_research.py 的 evidence_view 字段表；该模块依赖 Linux 的 fcntl，Windows 上无法导入
EVIDENCE_KEEP = ("task", "domain", "success", "partial_credit", "assertions", "version", "execution_id",
                 "learning_role", "repeat", "termination_status", "execution_error", "harness_defects",
                 "active_extensions", "first_effect_boundaries", "prompt", "completion", "cost_usd")
EVIDENCE_BOUNDARY = ("index", "hook", "service", "endpoint", "tool", "step")
EVIDENCE_INTERVENTION = ("unit", "component", "kind", "hook", "boundary_index", "effects", "output",
                         "next_actual_tool_action")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def save_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ref(path, pointer=""):
    return {"file": str(path).replace("\\", "/"), "pointer": pointer}


def evidence_view(record):
    view = {key: record.get(key) for key in EVIDENCE_KEEP}
    view["boundary_trace"] = [{k: e.get(k) for k in EVIDENCE_BOUNDARY} for e in record.get("boundary_trace", [])]
    view["interventions"] = [{k: e.get(k) for k in EVIDENCE_INTERVENTION} for e in record.get("injections", [])]
    return view


def cost_cny(usage):
    return (usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0)) * RATES["input_per_mtok"]
            + usage.get("prompt_cache_hit_tokens", 0) * RATES["cache_read_per_mtok"]
            + usage.get("completion_tokens", 0) * RATES["output_per_mtok"]) / 1e6


def content_form(unit):
    try:
        parsed = json.loads(unit["content"])
    except (json.JSONDecodeError, TypeError):
        return "text"
    if isinstance(parsed, dict) and set(parsed) == BOUNDARY_KEYS and all(parsed[k].strip() for k in BOUNDARY_KEYS):
        return "boundary_json"
    return "text"


def unit_change(before, after):
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    removed = [line for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag in ("delete", "replace")
               for line in before_lines[i1:i2]]
    added = [line for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace")
             for line in after_lines[j1:j2]]
    common = sum(i2 - i1 for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag == "equal")
    return {"removed_lines": removed, "added_lines": added,
            "removed_chars": sum(len(x) for x in removed), "added_chars": sum(len(x) for x in added),
            "equal_line_ratio": round(common / max(len(before_lines), len(after_lines)), 4)}


def value_tokens(text):
    return sorted({token.strip(",.;:") for token in VALUE_TOKEN.findall(text) if len(token.strip(",.;:")) > 1})


def run(root, output):
    root, output = Path(root), Path(output) / "history"
    paid = root / "experiments/feedback_chain/paid"
    frozen = root / "experiments/feedback_chain/frozen"
    protocol = read(frozen / "execution_v1.json")
    selection = read(frozen / "selection_v1.json")
    ancestor_version = selection["ancestor_version"]
    experiment_version = json.loads((paid / "workspace/workspace.json").read_text(encoding="utf-8"))["experiment_version"]
    versions_file = read(paid / "versions" / (experiment_version + ".json"))

    unit_cache = {}

    def version_digest(version):
        if version not in unit_cache:
            units = read(paid / "workspace/versions" / version / "manifest.json")["units"]
            unit_cache[version] = {unit["id"]: unit for unit in units}
        return unit_cache[version]

    ancestor_units = version_digest(ancestor_version)

    # 冻结业务判读：432 份盲审 verdict 与执行记录对应，重建 analysis.json 的分臂均值
    mapping = read(paid / "business_review/mapping.json")
    verdict_by_execution = {}
    for masked, item in mapping.items():
        verdict = read(paid / "business_review/verdicts" / (masked + ".json"))
        verdict_by_execution[item["execution_id"]] = {"masked_id": masked, "verdict": verdict, "block": item}
    review_path = root / "experiments/negative_result_offline/business/execution_reviews.json"
    business_review = ({x["execution_id"]: x for x in read(review_path)} if review_path.exists() else None)

    executions, delivery = {}, {"executions": 0, "injections": 0, "delivered": 0, "missing": []}
    for path in sorted((paid / "executions").glob("*/*.json")):
        record = read(path)
        arm = path.parent.name
        version = record["version"]
        units = version_digest(version)
        calls = {}
        for wire in record["wire_trace"]:
            calls.setdefault(wire["call"], "\n".join(str(m.get("content", "")) for m in wire["request"]))
        missing = []
        for injection in record["injections"]:
            if injection.get("kind") != "knowledge_injection":
                continue
            delivery["injections"] += 1
            materialized = units[injection["unit"]]["content"] in calls[injection["next_agent_call"]]
            delivery["delivered"] += materialized
            if not materialized:
                missing.append(injection["unit"])
        delivery["executions"] += 1
        if missing:
            delivery["missing"].append({"execution_id": record["execution_id"], "arm": arm, "units": sorted(set(missing))})
        verdict = verdict_by_execution.get(record["execution_id"])
        business = None
        if verdict:
            business = (all(r["passed"] for r in verdict["verdict"]["requirements"])
                        and record["termination_status"] == "completed" and record["execution_error"] is None)
        executions[record["execution_id"]] = {
            "execution_id": record["execution_id"], "arm": arm, "task": record["task"], "domain": record["domain"],
            "repeat": record["repeat"], "version": version, "content_is_ancestor": version == ancestor_version,
            "success": record["success"], "failed_assertions": sum(not a["passed"] for a in record["assertions"]),
            "assertion_count": len(record["assertions"]), "business_success": business,
            "termination_status": record["termination_status"], "execution_error": record["execution_error"],
            "harness_defects": len(record["harness_defects"]), "cost_cny": record["cost_usd"] * FX,
            "duration_s": record["duration_s"], "recorded_at": record["recorded_at"],
            "task_contract": record["task_contract"], "initial_world": digest(record["initial_world"]),
            "experiment_version": record["experiment_version"], "active_extensions": record["active_extensions"],
            "triggered_units": sorted({i["unit"] for i in record["injections"] if i.get("kind") == "knowledge_injection"}),
            "full_business_success": business_review[record["execution_id"]]["full_business_success"] if business_review else None,
            "reference": ref(path.relative_to(root))}

    # errors.jsonl 记录每次失败的付费调用，同一断点重跑会出现重复行，按 call 去重
    error_records, errored = [], set()
    for line in (paid / "errors.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["call"] in errored:
            continue
        errored.add(item["call"])
        request_path = paid / "io" / item["call"] / "request.json"
        error_records.append({**item, "phase": read(request_path)["phase"] if request_path.exists() else None})

    blocks, slots, organize_records, feedback_facts, diffs, correspondence = [], [], [], [], [], []
    for block in protocol["blocks"]:
        domain = block["domain"]
        published = read(paid / "blocks" / (domain + ".json"))
        candidates = read(paid / "candidates" / (domain + ".json"))
        source = next(x for x in executions.values() if x["arm"] == "source" and x["domain"] == domain)
        triggered = not source["success"]
        source_full_business = None
        failed_requirements = []
        if business_review:
            reviewed = business_review[source["execution_id"]]
            source_full_business = reviewed["full_business_success"]
            failed_requirements = [{"id": r["id"], "requirement": r["requirement"], "source": r.get("source")}
                                   for r in reviewed["requirements"] if not r["passed"]]
        organize = {"payload_chars": None, "attempts": [], "items": None, "citations": None}
        evidence = None
        if triggered:
            source_record = read(root / executions[source["execution_id"]]["reference"]["file"])
            evidence = evidence_view(source_record)
            evidence_text = json.dumps(evidence, ensure_ascii=False)
            for number, path in enumerate(sorted((paid / "requests" / domain / "organize").glob("*.json"))):
                request = read(path)
                usage = request["response"].get("usage", {})
                item = {"attempt": number, "status": request["status"], "call": request["call"],
                        "payload_chars": len(request["messages"][1]["content"]),
                        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                        "cost_cny": cost_cny(usage), "accepted": request["call"] not in errored,
                        "reference": ref(path.relative_to(root))}
                organize["attempts"].append(item)
                if number == 0:
                    organize["payload_chars"] = item["payload_chars"]
            accepted = organize["attempts"][-1]
            if accepted["accepted"]:
                content = read(paid / "requests" / domain / "organize" / ("%d.json" % accepted["attempt"]))
                items = json.loads(content["response"]["choices"][0]["message"]["content"])["items"]
                verified, unverified, uncovered, introduced = 0, [], set(), []
                cited_pointers = set()
                for index, entry in enumerate(items):
                    for citation in entry["evidence"]:
                        try:
                            target = resolve_pointer(evidence, citation["pointer"])
                        except Exception:  # 引用路径不存在本身就是要记录的事实
                            target = "__missing__"
                        text = target if isinstance(target, str) else json.dumps(target, ensure_ascii=False)
                        if citation["quote"] in text:
                            verified += 1
                        else:
                            unverified.append({"item": index, "pointer": citation["pointer"]})
                        cited_pointers.add(citation["pointer"])
                    tokens = value_tokens(" ".join([entry["requirement"], entry["object"], entry["consequence"]]))
                    absent = [t for t in tokens if t not in evidence_text]
                    if absent:
                        introduced.append({"item": index, "values": absent})
                    correspondence.append({"kind": "organized_item", "domain": domain, "item": index,
                                           "requirement": entry["requirement"], "object": entry["object"],
                                           "consequence": entry["consequence"],
                                           "citations": [c["pointer"] for c in entry["evidence"]],
                                           "reference": ref((paid / "requests" / domain / "organize" / ("%d.json" % accepted["attempt"])).relative_to(root),
                                                            "/response/choices/0/message/content")})
                for pointer in cited_pointers:
                    if pointer.startswith("/assertions/"):
                        uncovered.add(int(pointer.split("/")[2]))
                organize["items"] = len(items)
                organize["citations"] = {"verified": verified, "unverified": unverified,
                                         "distinct_pointers": len(cited_pointers),
                                         "pointer_prefixes": dict(Counter(c["pointer"].split("/")[1]
                                                                          for entry in items for c in entry["evidence"]
                                                                          if c["pointer"].startswith("/"))),
                                         "source_assertions_cited": len(uncovered),
                                         "source_assertions_total": source["assertion_count"],
                                         "values_absent_from_source": introduced}
        for arm in ARMS:
            candidate = candidates[arm]
            status = candidate["status"]
            if status == "source_passed_edit_failure":
                status = "untriggered_source_passed" if not triggered else "unexpected"
            paths = sorted((paid / "requests" / domain / arm / "edit").glob("*.json"))
            calls = []
            for number, path in enumerate(paths):
                request = read(path)
                usage = request["response"].get("usage", {})
                payload = request["messages"][1]["content"]
                calls.append({"attempt": number, "status": request["status"], "call": request["call"],
                              "payload_chars": len(payload), "has_organized_feedback": "organized_feedback" in json.loads(payload),
                              "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                              "cost_cny": cost_cny(usage), "accepted": request["call"] not in errored,
                              "reference": ref(path.relative_to(root))})
            failures = [{"call": error["call"], "error_type": error["error_type"],
                         "transport_error_type": error["transport_error_type"], "phase": error["phase"],
                         "error": error["error"][:300]}
                        for error in error_records if error["call"] in {call["call"] for call in calls}]
            units_after = version_digest(candidate["version"])
            changes = {"added": sorted(set(units_after) - set(ancestor_units)),
                       "removed": sorted(set(ancestor_units) - set(units_after)),
                       "modified": sorted(k for k in units_after if k in ancestor_units and units_after[k] != ancestor_units[k])}
            changed = [*changes["added"], *changes["removed"], *changes["modified"]]
            representation = sorted(k for k in units_after if k in changed and content_form(units_after[k]) == "boundary_json")
            slot = {"domain": domain, "arm": arm, "factor_f": FACTOR_F[arm[:2]], "factor_r": FACTOR_R[arm[2:]],
                    "edit_order_index": block["edit_order"].index(arm), "edit_triggered": triggered, "status": status,
                    "deployed_version": candidate["version"], "deployed_ancestor": candidate["version"] == ancestor_version,
                    "edit_calls": len(calls), "calls": calls,
                    "format_failures": failures, "generation_cost_cny": round(sum(c["cost_cny"] for c in calls), 6),
                    "units_before": len(ancestor_units), "units_after": len(units_after),
                    "added": changes["added"], "removed": changes["removed"], "modified": changes["modified"],
                    "content_chars_before": sum(len(u["content"]) for u in ancestor_units.values()),
                    "content_chars_after": sum(len(u["content"]) for u in units_after.values()),
                    "changed_content_chars": sum(len(units_after[k]["content"]) for k in changed),
                    "representation_units": representation,
                    "text_units": sorted(k for k in changed if k not in representation),
                    "representation_share_of_changed": round(len(representation) / len(changed), 4) if changed else None,
                    "representation_chars": sum(len(units_after[k]["content"]) for k in representation),
                    "representation_share_of_content": round(sum(len(units_after[k]["content"]) for k in representation)
                                                             / sum(len(u["content"]) for u in units_after.values()), 4),
                    "serialized_chars_after": sum(len(json.dumps(u, ensure_ascii=False)) for u in units_after.values()),
                    "reference": ref((paid / "candidates" / (domain + ".json")).relative_to(root), "/" + arm)}
            slot["stop_point"] = ("来源首跑严格通过，编辑请求未发出" if not triggered else
                                  "两次编辑请求都未通过格式校验，部署祖先版本" if status == "invalid_edit" else
                                  "合法候选写入版本库并部署")
            slots.append(slot)
            if status == "edited":
                for unit in changes["added"]:
                    diffs.append({"domain": domain, "arm": arm, "unit": unit, "change": "added",
                                  "parent_chars": 0, "child_chars": len(units_after[unit]["content"]),
                                  "parent_form": None, "child_form": content_form(units_after[unit]),
                                  "component": units_after[unit].get("component"), "hooks": units_after[unit].get("hooks"),
                                  "child_text": units_after[unit]["content"],
                                  "candidate_reference": ref((paid / "workspace/versions" / candidate["version"] / "manifest.json").relative_to(root))})
                for unit in changes["removed"]:
                    diffs.append({"domain": domain, "arm": arm, "unit": unit, "change": "removed",
                                  "parent_chars": len(ancestor_units[unit]["content"]), "child_chars": 0,
                                  "parent_form": content_form(ancestor_units[unit]), "child_form": None,
                                  "component": ancestor_units[unit].get("component"), "hooks": ancestor_units[unit].get("hooks"),
                                  "parent_text": ancestor_units[unit]["content"],
                                  "candidate_reference": ref((paid / "workspace/versions" / candidate["version"] / "manifest.json").relative_to(root))})
                for unit in changes["modified"]:
                    change = unit_change(ancestor_units[unit]["content"], units_after[unit]["content"])
                    diffs.append({"domain": domain, "arm": arm, "unit": unit, "change": "modified",
                                  "parent_chars": len(ancestor_units[unit]["content"]),
                                  "child_chars": len(units_after[unit]["content"]),
                                  "parent_form": content_form(ancestor_units[unit]), "child_form": content_form(units_after[unit]),
                                  "component": units_after[unit].get("component"), "hooks": units_after[unit].get("hooks"),
                                  **change,
                                  "candidate_reference": ref((paid / "workspace/versions" / candidate["version"] / "manifest.json").relative_to(root))})
            source_retest = next((x for x in executions.values() if x["arm"] == arm + "_source" and x["domain"] == domain), None)
            migration = [x for x in executions.values() if x["arm"] == arm and x["domain"] == domain
                         and x["task"] in {t for t in protocol["plan"]["heldout"]}]
            correspondence.append({"kind": "slot", "domain": domain, "arm": arm, "status": status,
                                   "change": {"added": changes["added"], "removed": changes["removed"],
                                              "modified": changes["modified"],
                                              "added_chars": sum(len(units_after[k]["content"]) for k in changes["added"]),
                                              "removed_chars": sum(len(ancestor_units[k]["content"]) for k in changes["removed"]),
                                              "modified_child_chars": sum(len(units_after[k]["content"]) for k in changes["modified"]),
                                              "representation_units": representation},
                                   "source_retest": {"execution_id": source_retest["execution_id"],
                                                     "strict_success": source_retest["success"],
                                                     "failed_assertions": source_retest["failed_assertions"],
                                                     "full_business_success": source_retest["full_business_success"]} if source_retest else None,
                                   "migration": {"executions": len(migration),
                                                 "strict_successes": sum(x["success"] for x in migration),
                                                 "frozen_business_successes": sum(x["business_success"] for x in migration),
                                                 "full_business_successes": sum(x["full_business_success"] for x in migration),
                                                 "cost_cny": round(sum(x["cost_cny"] for x in migration), 4)},
                                   "reference": ref((paid / "candidates" / (domain + ".json")).relative_to(root), "/" + arm)})
        edits = [s for s in slots if s["domain"] == domain and s["status"] == "edited"]
        runtime = [x for x in executions.values() if x["domain"] == domain and x["arm"] in (*ARMS, "ancestor", "carry")]
        blocks.append({"domain": domain, "source_task": source["task"], "source_execution_id": source["execution_id"],
                       "source_strict_success": source["success"], "source_assertion_count": source["assertion_count"],
                       "source_term_uncompleted": source["termination_status"] != "completed" or source["execution_error"] is not None,
                       "source_cost_cny": round(source["cost_cny"], 4), "source_recorded_at": source["recorded_at"],
                       "edit_triggered": triggered, "trigger_rule": "严格评分未通过才进入编辑",
                       "source_full_business_success": source_full_business,
                       "business_requirements_failed": failed_requirements,
                       "slots": {s["arm"]: s["status"] for s in slots if s["domain"] == domain},
                       "deployed_versions": {s["arm"]: s["deployed_version"][:12] for s in slots if s["domain"] == domain},
                       "edited_slots": len(edits), "untriggered_slots": 0 if triggered else len(ARMS),
                       "invalid_edit_slots": sum(s["status"] == "invalid_edit" for s in slots if s["domain"] == domain),
                       "generation_cost_cny": round(sum(s["generation_cost_cny"] for s in slots if s["domain"] == domain)
                                                    + sum(a["cost_cny"] for a in organize["attempts"]), 4),
                       "block_billed_cny": published["billed_cny"],
                       "evidence_view_chars": len(json.dumps(evidence, ensure_ascii=False)) if evidence else None,
                       "runtime": {"executions": len(runtime),
                                   "experiment_versions": sorted({x["experiment_version"] for x in runtime}),
                                   "harness_defects": sum(x["harness_defects"] for x in runtime),
                                   "terminations": dict(Counter(x["termination_status"] for x in runtime)),
                                   "execution_errors": sum(x["execution_error"] is not None for x in runtime),
                                   "mean_duration_s": round(mean(x["duration_s"] for x in runtime), 1),
                                   "units_injected_per_arm": {arm: len({u for x in runtime if x["arm"] == arm
                                                                       for u in x["triggered_units"]})
                                                              for arm in (*ARMS, "ancestor", "carry")},
                                   "component_units_per_arm": {arm: sorted({k for x in runtime if x["arm"] == arm
                                                                            for k in x["active_extensions"]})
                                                               for arm in (*ARMS, "ancestor", "carry")}},
                       "reference": ref((paid / "blocks" / (domain + ".json")).relative_to(root))})
        organize_records.append(dict(organize, domain=domain, triggered=triggered,
                                     reference=ref((paid / "requests" / domain / "organize").relative_to(root))))
        if triggered:
            base_payload = [s for s in slots if s["domain"] == domain and s["arm"].startswith("F0")][0]
            organized_payload = [s for s in slots if s["domain"] == domain and s["arm"].startswith("F1")][0]
            f0 = next(c for c in base_payload["calls"] if c["attempt"] == 0)
            f1 = next(c for c in organized_payload["calls"] if c["attempt"] == 0)
            organized = json.loads(read(root / f1["reference"]["file"])["messages"][1]["content"])["organized_feedback"]
            feedback_facts.append({"domain": domain, "source_execution_id": source["execution_id"],
                                   "evidence_view_chars": len(json.dumps(evidence, ensure_ascii=False)),
                                   "organize_payload_chars": organize["payload_chars"],
                                   "organize_calls": len(organize["attempts"]),
                                   "organize_cost_cny": round(sum(a["cost_cny"] for a in organize["attempts"]), 6),
                                   "organized_items": organize["items"],
                                   "organized_feedback_chars": len(json.dumps(organized, ensure_ascii=False)),
                                   "edit_payload_chars_F0": f0["payload_chars"], "edit_payload_chars_F1": f1["payload_chars"],
                                   "edit_payload_delta_chars": f1["payload_chars"] - f0["payload_chars"],
                                   "edit_payload_delta_share": round((f1["payload_chars"] - f0["payload_chars"]) / f0["payload_chars"], 4),
                                   "citations": organize["citations"],
                                   "source_assertions_total": source["assertion_count"],
                                   "semantic_note": "整理条目的引用链与取值由程序核验，内容语义见 researcher_reading 与 organized 记录"})

    # 六块主比较与分解：单位是每块 12 次配对执行的成功次数
    effects = []
    for block in protocol["blocks"]:
        domain = block["domain"]
        trigger = read(paid / "blocks" / (domain + ".json"))["candidates"]["F0R0"]["status"] != "source_passed_edit_failure"
        means = {}
        for arm in (*ARMS, "ancestor", "carry"):
            values = [x for x in executions.values() if x["domain"] == domain and x["arm"] == arm
                      and x["task"] in set(protocol["plan"]["heldout"])]
            means[arm] = {"executions": len(values),
                          "business_successes": sum(x["business_success"] for x in values),
                          "strict_successes": sum(x["success"] for x in values),
                          "cost_cny": round(sum(x["cost_cny"] for x in values), 4)}
        pairs = [(x["task"], x["repeat"]) for x in executions.values() if x["domain"] == domain and x["arm"] == "F1R0"
                 and x["task"] in set(protocol["plan"]["heldout"])]
        outcomes = {(x["task"], x["repeat"], x["arm"]): x["business_success"] for x in executions.values()
                    if x["domain"] == domain and x["arm"] in ("F1R0", "F0R0")}
        effect = {"domain": domain, "triggered": trigger, "arms": means,
                  "delta_F1R0_F0R0_business_successes": means["F1R0"]["business_successes"] - means["F0R0"]["business_successes"],
                  "delta_F1R0_F0R0_business_rate": round((means["F1R0"]["business_successes"] - means["F0R0"]["business_successes"]) / 12, 6),
                  "delta_F1R0_F0R0_strict_successes": means["F1R0"]["strict_successes"] - means["F0R0"]["strict_successes"],
                  "task_repeat_pairs": len(pairs),
                  "discordant_task_repeat_pairs": sum(outcomes[(task, repeat, "F1R0")] != outcomes[(task, repeat, "F0R0")]
                                                      for task, repeat in pairs)}
        if not trigger:
            counts = [means[arm]["business_successes"] for arm in (*ARMS, "ancestor")]
            effect["same_content_arm_success_range"] = [min(counts), max(counts)]
            effect["same_content_arm_success_spread"] = max(counts) - min(counts)
        effects.append(effect)
    frozen_analysis = read(paid / "analysis.json")
    for effect, published in zip(effects, frozen_analysis["blocks"]):
        assert effect["domain"] == published["domain"]
        for arm in (*ARMS, "ancestor", "carry"):
            assert abs(effect["arms"][arm]["business_successes"] / 12 - published["business"][arm]) < 1e-12
    untriggered = [e for e in effects if not e["triggered"]]
    edited = [e for e in effects if e["triggered"]]
    net = sum(e["delta_F1R0_F0R0_business_successes"] for e in effects)
    assert len(effects) == len(untriggered) + len(edited)

    # 未经干预的块内参照：内容版本相同的多次执行在四个臂名下的差异幅度
    same_version = []
    for effect in untriggered:
        tasks = sorted({x["task"] for x in executions.values() if x["domain"] == effect["domain"]
                        and x["arm"] in ARMS and x["task"] in set(protocol["plan"]["heldout"])})
        for task in tasks:
            values = [x for x in executions.values() if x["domain"] == effect["domain"] and x["task"] == task
                      and x["arm"] in (*ARMS, "ancestor")]
            successes = {arm: sum(x["business_success"] for x in values if x["arm"] == arm) for arm in (*ARMS, "ancestor")}
            same_version.append({"domain": effect["domain"], "task": task,
                                 "content_versions": sorted({x["version"][:12] for x in values}),
                                 "executions": len(values), "business_successes": sum(successes.values()),
                                 "per_arm_successes": successes,
                                 "per_arm_executions": {arm: sum(x["arm"] == arm for x in values) for arm in (*ARMS, "ancestor")},
                                 "arm_spread_successes": max(successes.values()) - min(successes.values())})
    for effect in untriggered:
        rows = [x for x in same_version if x["domain"] == effect["domain"]]
        effect["same_content_arm_spread_successes"] = sum(x["arm_spread_successes"] for x in rows)
        effect["same_content_task_spread_max"] = max(x["arm_spread_successes"] for x in rows)
    inputs = defaultdict(lambda: {"groups": 0, "content_versions": set(), "contracts": set(), "world_digests": set()})
    for effect in effects:
        tasks = sorted({x["task"] for x in executions.values() if x["domain"] == effect["domain"]
                        and x["arm"] in (*ARMS, "ancestor") and x["task"] in set(protocol["plan"]["heldout"])})
        for task in tasks:
            for repeat in range(3):
                values = [x for x in executions.values() if x["domain"] == effect["domain"] and x["task"] == task
                          and x["repeat"] == repeat and x["arm"] in (*ARMS, "ancestor")]
                inputs[effect["domain"]]["groups"] += 1
                inputs[effect["domain"]]["content_versions"].add(tuple(sorted({x["version"] for x in values})))
                inputs[effect["domain"]]["contracts"].update({x["task_contract"] for x in values})
                inputs[effect["domain"]]["world_digests"].update({x["initial_world"] for x in values})
    input_identity = [{"domain": domain, "task_repeat_groups": value["groups"],
                       "content_version_set_sizes": dict(Counter(len(v) for v in value["content_versions"])),
                       "task_contracts": len(value["contracts"]), "initial_world_digests": len(value["world_digests"]),
                       "experiment_versions": len({x["experiment_version"] for x in executions.values()})}
                      for domain, value in sorted(inputs.items())]
    status_counts = Counter(s["status"] for s in slots)
    per_arm_status = {}
    for arm in ARMS:
        per_arm_status[arm] = dict(Counter(s["status"] for s in slots if s["arm"] == arm))
    r1_slots = [s for s in slots if s["arm"].endswith("R1") and s["edit_triggered"]]
    r0_slots = [s for s in slots if s["arm"].endswith("R0") and s["edit_triggered"]]
    summary = {
        "protocol": {"experiment": selection["experiment"], "experiment_version": experiment_version,
                     "ancestor_version": ancestor_version,
                     "factor_F": FACTOR_F, "factor_R": FACTOR_R, "blocks": len(protocol["blocks"]), "arms": len(ARMS),
                     "edit_slots": len(protocol["blocks"]) * len(ARMS), "content_budget_chars": protocol["candidate_content_chars"],
                     # 与 engine/scoped_units.py 的 POLICY 一致：格式错误重试 1 次，共 2 次尝试
                     "format_retries": 1, "model": protocol["model"],
                     "cost_rates_cny_per_mtok": RATES, "fx": FX,
                     "implementation_files": len(versions_file["files"]),
                     "reference": ref((paid / "versions" / (experiment_version + ".json")).relative_to(root))},
        "evidence_sources": {"business_review_execution_records": len(business_review) if business_review else None,
                             "business_review_reference": ref(review_path.relative_to(root)) if business_review else None,
                             "business_review_note": "30 次来源执行与 432 次迁移执行的补充业务评价来自同计划业务评价工作包，本脚本只读取其结果"},
        "sources": [{k: v for k, v in b.items() if k in ("domain", "source_task", "source_execution_id", "source_strict_success",
                                                         "source_assertion_count", "source_term_uncompleted", "source_cost_cny",
                                                         "source_recorded_at", "edit_triggered", "trigger_rule",
                                                         "source_full_business_success", "business_requirements_failed", "reference")}
                    for b in blocks],
        "blocks": blocks,
        "slots": slots,
        "generation": {"slot_status": dict(status_counts), "status_by_arm": per_arm_status,
                       "edit_calls": sum(s["edit_calls"] for s in slots),
                       "format_failures": sum(len(s["format_failures"]) for s in slots),
                       "slots_with_format_failure": sum(bool(s["format_failures"]) for s in slots),
                       "terminal_generation_loss": status_counts.get("invalid_edit", 0),
                       "terminal_loss_by_arm": dict(Counter(s["arm"] for s in slots if s["status"] == "invalid_edit")),
                       "terminal_loss_by_factor_R": {k: sum(s["status"] == "invalid_edit" for s in slots if s["arm"].endswith(k))
                                                     for k in ("R0", "R1")},
                       "any_format_failure_by_factor_R": {k: sum(bool(s["format_failures"]) for s in slots if s["arm"].endswith(k))
                                                          for k in ("R0", "R1")},
                       "cost_cny": {"organize_and_edit": round(sum(b["generation_cost_cny"] for b in blocks), 4),
                                    "blocks_billed_total": round(sum(b["block_billed_cny"] for b in blocks), 3)}},
        "organize": organize_records,
        "feedback_facts": feedback_facts,
        "representation": {"R1_slots_triggered": len(r1_slots),
                           "R1_slots_materialized": sum(s["status"] == "edited" for s in r1_slots),
                           "R1_units_under_representation": sum(len(s["representation_units"]) for s in r1_slots),
                           "R1_units_changed": sum(len(s["added"]) + len(s["removed"]) + len(s["modified"]) for s in r1_slots),
                           "share_of_R1_changed_units_in_representation":
                               round(sum(len(s["representation_units"]) for s in r1_slots)
                                     / max(sum(len(s["added"]) + len(s["removed"]) + len(s["modified"]) for s in r1_slots), 1), 4),
                           "R0_slots_triggered": len(r0_slots),
                           "R0_units_changed": sum(len(s["added"]) + len(s["removed"]) + len(s["modified"]) for s in r0_slots),
                           "note": "R1 只对新增或改写的单元生效；祖先未改动的单元按原文保留"},
        "input_delivery": {"execution_records": delivery["executions"], "knowledge_injections": delivery["injections"],
                           "delivered": delivery["delivered"], "missing": delivery["missing"],
                           "check": "注入单元的候选内容出现在下一次真实模型请求中（与 engine/feedback_chain.py verify_delivery 同规则）"},
        "input_identity": input_identity,
        "block_effects": effects,
        "decomposition": {
            "unit": "每块 12 次配对执行中的业务成功次数；六块合计 72",
            "net_F1R0_minus_F0R0_successes": net,
            "net_business_rate": round(net / 72, 6),
            "untriggered_blocks": {"domains": [e["domain"] for e in untriggered],
                                   "successes": sum(e["delta_F1R0_F0R0_business_successes"] for e in untriggered),
                                   "share": round(sum(e["delta_F1R0_F0R0_business_successes"] for e in untriggered) / net, 4),
                                   "intervention": "四臂部署同一祖先版本，编辑未触发",
                                   "per_block_successes": {e["domain"]: e["delta_F1R0_F0R0_business_successes"] for e in untriggered},
                                   "same_content_arm_success_spread": {e["domain"]: e["same_content_arm_success_spread"] for e in untriggered},
                                   "observed_within_same_content_spread": {e["domain"]: abs(e["delta_F1R0_F0R0_business_successes"]) <= e["same_content_arm_success_spread"] for e in untriggered},
                                   "sign_pattern": [1 if e["delta_F1R0_F0R0_business_successes"] > 0 else -1 if e["delta_F1R0_F0R0_business_successes"] < 0 else 0 for e in untriggered]},
            "edited_blocks": {"domains": [e["domain"] for e in edited],
                              "successes": sum(e["delta_F1R0_F0R0_business_successes"] for e in edited),
                              "share": round(sum(e["delta_F1R0_F0R0_business_successes"] for e in edited) / net, 4),
                              "intervention": "F1R0 与 F0R0 都部署合法候选，真实编辑进入执行",
                              "per_block_successes": {e["domain"]: e["delta_F1R0_F0R0_business_successes"] for e in edited}},
            "discordant_task_repeat_pairs": {"total": sum(e["discordant_task_repeat_pairs"] for e in effects),
                                             "pairs": sum(e["task_repeat_pairs"] for e in effects),
                                             "untriggered_blocks": sum(e["discordant_task_repeat_pairs"] for e in untriggered),
                                             "edited_blocks": sum(e["discordant_task_repeat_pairs"] for e in edited),
                                             "per_block": {e["domain"]: e["discordant_task_repeat_pairs"] for e in effects}},
            "generation_failures_in_primary_contrast": sum(s["status"] == "invalid_edit" and s["arm"] in ("F1R0", "F0R0") for s in slots),
            "generation_failures": {"slots": status_counts.get("invalid_edit", 0),
                                    "arms": sorted(s["arm"] for s in slots if s["status"] == "invalid_edit"),
                                    "affects_primary_contrast": False,
                                    "affects": "R1 增量与交互项；三块中 R1 槽位的候选有一半从未生成"},
            "same_version_reference": {"untriggered_same_content_arm_spread_successes": sum(x["same_content_arm_spread_successes"] for x in untriggered),
                                       "untriggered_observed_delta_successes": sum(e["delta_F1R0_F0R0_business_successes"] for e in untriggered),
                                       "same_version_json": read(root / "experiments/negative_result_offline/same_version.json")["counts"],
                                       "note": "未触发块的臂名差异全部来自相同内容的重复执行"},
            "total_cost_cny": round(sum(b["block_billed_cny"] for b in blocks), 3)},
        "same_version_groups": same_version,
        "correspondence": correspondence,
        "researcher_reading": {
            "method": "研究者阅读来源执行记录、整理条目与候选单元全文；程序只定位引用链并核算明确取值",
            "organized_feedback": {
                "finance": "10 条整理条目：任务结果一条、8 条严格断言一条、额度政策一条；48 次引用落在 /assertions，唯一失败的 Baxter 通知断言按 7 次引用展开。整理件把来源首跑自己的严格断言结果按“要求—对象—后果”重述。",
                "sales": "14 条：任务结果一条、13 条严格断言按条目展开；48 次引用落在 /assertions。失败的索引 0、11、12 三条断言（含公告须含 CLOSED、不得含 WIN: 与 MAJOR WIN:）在整理件中均可读。",
                "support": "7 条：任务结果一条、6 个要求组展开；105 次引用落在 /assertions，覆盖 37 条断言中的 33 条，未引用的是不得写入 ws_log 一类否定断言的重复条目。"},
            "edit_semantics": {
                "finance": "三个合法候选都改写 ace_op_credit_alloc，写入“credit note 明细优先于列表、404 属形式问题需换别名重试、按明细值重算余额”；F1R0 另加一个确定性 E 单元 ace_op_xero_credit_detail，在执行开始取明细并把差异注入上下文。可迁移的是取值权威性规则本身，Baxter、CN-302、INV-5004 的取值只服务来源题。",
                "sales": "改动集中在 ace_op_contract_win：把频道里已观察到的公告格式定为权威，禁止外部顾问与内部建议引入的前缀，并禁止按账户备注改投频道。可迁移的是“建议不等于已采纳政策”，Quantum 的金额与频道名只服务来源题。",
                "support": "新增一个合并与去重单元：先读工作簿全部标签页，按任务指定的键匹配，只有权威来源认定同一问题的记录才合并，其余保持打开且不写备注不入日志。可迁移的是“先读全部标签页、备注可以排除记录”，g_211 与 g_212 只服务来源题。"},
            "trigger_chain": {
                "hr": "来源首跑 9 条断言全过，编辑未启动。最新资格规则邮件要求休假员工在 Airtable 记录延期；运行只创建了 4 条在职员工记录，补充业务评价据此判定 sabbatical_deferral 未完成。断言集又含一条禁止任何 update_record 的条目，履行该延期义务的写回动作会被判负。严格通过既漏掉真实义务，又排斥履行该义务的动作。",
                "marketing": "来源首跑严格通过，补充业务评价也通过：三个符合条件客户的邮件、跟踪表追加与全部排除对象都有一致动作，未发现遗漏义务。",
                "operations": "来源首跑严格通过，补充业务评价也通过：2 条 Monday 记录、1 封汇总邮件与 4 条排除对象一致，未发现遗漏义务。"}},
        "unresolved": ["30 次来源执行的完整业务义务评价来自同计划业务评价工作包，本脚本只读取其结果，未重新逐条复核。",
                       "R1 候选只把改动单元写成边界表示，未改动单元保持祖先原文；表示干预覆盖的内容比例按单元数与字符数给出。",
                       "生成失败的成因由格式校验错误类型与重试次数确定，模型为何生成非法 JSON 不在此记录范围内。"]}
    save(output / "candidate_status.json", summary)
    save_rows(output / "edit_content_diffs.jsonl", diffs)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    args = parser.parse_args()
    result = run(args.root, args.output)
    print(json.dumps({"generation": result["generation"], "decomposition": result["decomposition"],
                      "representation": result["representation"]}, ensure_ascii=False))
