import argparse
import csv
import hashlib
import itertools
import json
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from engine.negative_result_offline import inference


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/negative_result_offline"
ARMS = ("F0R0", "F1R0", "F0R1", "F1R1", "ancestor", "carry")
SCORES = {"strict": "strict_success", "frozen": "frozen_success", "business": "full_business_success",
          "executable": "executable_business_success", "business_unknown_completed": "business_unknown_completed"}

# 逐题阅读提示词、来源改动及授权资料后的固定关系，不按关键词推断适用性。
TRANSFER_READING = {
    "finance.xero_multicurrency_payment": ("related_principle", "来源信用分配改为入账；需按币种、最新汇率与发票状态判断", "来源分配金额与发票余额规则没有信用票对象上的表达机会；同为数值权威与禁止处理条件"),
    "finance.xero_quote_to_invoice": ("authorization_conflict", "来源禁止未审批提额；新题直接要求低价报价加价，同时引用既定转单程序", "直接加价请求与已接受报价价格政策形成授权冲突，两种解释另作敏感性分析"),
    "finance.qb_void_stale_invoices": ("related_principle", "从Xero分配迁移到QuickBooks作废；团队更新影响单据是否可处理", "近期逐单据例外仍需读取；信用票明细与来源对象数值不适用"),
    "finance.employee_reimbursement": ("related_principle", "报销按类别上限计算，并对Entertainment保持原状", "封顶是本题金额规则；不能把来源信用票的全额分配及具体数值移植"),
    "hr.benefits_open_enrollment_processing": ("no_edit", "按life events及corrections更新福利", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "hr.compliance_training_enrollment": ("no_edit", "按课程时间及部门更正报名", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "hr.intern_program_coordination": ("no_edit", "背景核验、导师更正与法务通知顺序", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "hr.offboarding_multi_system": ("no_edit_environment_gap", "按离职类型处理，授权政策在环境中缺失", "来源首跑通过，四臂部署祖先；政策适用部分保持未观察"),
    "marketing.event_sponsorship_screen": ("no_edit", "依审批政策筛选并保留其他行", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "marketing.trial_extension_processing": ("no_edit", "依Policy页计算延期并通知", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "marketing.news_digest_dedup": ("no_edit", "依最新政策去重并计数", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "marketing.campaign_handoff": ("no_edit", "按交接程序记录与归档", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "operations.equipment_calibration_scheduling": ("no_edit", "近期校准窗口与holds/overrides", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "operations.fleet_vehicle_maintenance": ("no_edit", "里程单位换算、维护政策及车辆排除", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "operations.hazmat_shipping_compliance": ("no_edit", "国际危险品、重新分类与声明缺失条件", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "operations.compressed_gas_cylinder_tracking": ("no_edit", "自有非空瓶过期检测与逐地点通知", "来源首跑通过，四臂部署祖先；无新增来源功能可解释"),
    "sales.multi_hop_lookup": ("different_routing", "本题授权按账户等级向邮件组路由，并对支持升级另行通知", "来源固定Slack频道及公告前缀不能替代本题邮件路由政策"),
    "sales.demo_scheduling": ("opposite_routing", "本题按金额阈值选择Slack频道，另有On Hold停止与通知AE分支", "来源保持标准deal-wins频道；本题采用表内阈值路由，且无需关闭商机"),
    "sales.docusign_completion_processor": ("opposite_authority_condition", "原文runbook明确要求检查account notes中的credit hold与partner billing", "来源忽略账户备注中的个人频道偏好；本题账户备注被runbook明确授权影响处理，应读出具体差别"),
    "sales.slack_channel_for_new_account": ("different_operation", "依general置顶命名规则创建客户频道、邀请账户团队并更新CRM", "来源关闭商机及复用既有公告频道不适用；本题需创建新频道"),
    "support.reamaze_cross_platform_dedup": ("opposite_identity_condition", "ws_aliases明确认定不同邮箱为同一客户，并要求双方内部备注", "来源仅按相同邮箱分组；本题别名表授权跨邮箱匹配，且原单也需回链"),
    "support.freshdesk_auto_merge": ("additional_identity_condition", "Similarity Rules要求exact_subject_match及same_requester两条件同时成立", "来源相同邮箱不足以决定合并；本题需同requester且主题相同并保留主单回链"),
    "support.zendesk_cross_org_merge": ("different_entity_and_operation", "按ws_candidates与ws_dnm合并组织，转移工单并填写真实转移数量", "来源关闭重复工单的动作不能完成组织合并；空组织允许转移数为零但仍需记结果"),
    "support.gorgias_defect_tracking": ("different_operation_and_exclusions", "产品目录与已知缺陷决定新bug、回复及日志；本题要求log everything", "来源合并任务的关闭/不写备注/不记日志规则需限制在合并场景，缺陷跟踪按本题授权解释"),
}

CARRY_READING = {
    "finance.xero_multicurrency_payment": "前次已通过；新上下文重跑缺付款记录，携带执行保持付款动作。需并列检查原币金额与USD通知。",
    "finance.employee_reimbursement": "前次反馈含Bob通知不得出现340的字面禁令；携带执行按该禁令改写通知。业务审批额250与允许说明原申报340另行核验。",
    "hr.benefits_open_enrollment_processing": "前次失败涉及BambooHR实际更新或错误通知对象，携带输入保留更正及逐项断言；核对对象状态和选定方案。",
    "hr.intern_program_coordination": "该新增冻结成功对应前次已通过、新上下文重跑日期字面失败；业务要求使用源日期，审批与法务顺序另行判读。",
    "marketing.news_digest_dedup": "前次已通过，携带输入提供六篇文章的冻结期望；完整授权另含当日解禁和策展邮件，业务计数单独评价。",
    "operations.equipment_calibration_scheduling": "前次已通过，携带执行保持不为LAB-2008创建任务；新上下文重跑增加了被排除对象的任务。",
    "operations.hazmat_shipping_compliance": "冻结新增成功来自不提SHP-810的断言；原制裁邮件要求在合规报告中警示该对象，声明对象与警示对象须分开。",
    "sales.multi_hop_lookup": "前次已通过，携带执行保持等级、汇率与支持升级通知；新上下文重跑缺对应完整通知。币种写回状态另行评价。",
    "support.freshdesk_auto_merge": "反馈给出主单和重复单两侧引用要求；携带执行增加或保持双向记录，业务仍需日志字段及合并对象准确。",
    "support.gorgias_defect_tracking": "前次断言包含对回复词语和日志的禁止条件；携带执行满足冻结标签的同时，完整原始缺陷处理与log everything另行核验。",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def lines(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def csv_table(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def stats(reviews, protocol):
    lookup = {(r["task"], r["arm"], r["repeat"]): dict(r, business_unknown_completed=r["full_business_success"] or r["full_business_state"] == "unknown")
              for r in reviews if r["scope"] == "migration"}
    blocks = protocol["blocks"]

    def differences(field, excluded_task=None, repeat=None, excluded_tasks=()):
        result = []
        for block in blocks:
            tasks = [m["task"] for m in block["migration"] if m["task"] != excluded_task and m["task"] not in excluded_tasks]
            repeats = range(3) if repeat is None else (repeat,)
            vals = [int(lookup[t, "F1R0", r][field]) - int(lookup[t, "F0R0", r][field]) for t in tasks for r in repeats]
            result.append(sum(vals) / len(vals))
        return result

    results, exports = {}, []
    for name, field in SCORES.items():
        values = differences(field)
        full = inference(values)
        lo, hi = full["ci95"]
        full["predefined_decision"] = "precision_insufficient" if lo <= 0 and hi >= .10 else "below_practical_threshold" if hi < .10 else "practical_gain_supported" if lo >= .10 else "positive_but_practical_gain_uncertain"
        for comparison in (full,):
            exports.append({"scoring": name, "comparison": "six_blocks", "effect_pp": comparison["effect"] * 100,
                            "p": comparison["p_two_sided"], "ci_lower_pp": comparison["ci95"][0] * 100,
                            "ci_upper_pp": comparison["ci95"][1] * 100, "decision": full["predefined_decision"]})
        result = {"primary": full,
                  "leave_one_block_out": [{"removed": b["domain"], **inference([d for i, d in enumerate(values) if i != j])} for j, b in enumerate(blocks)],
                  "leave_one_task_out": [{"removed": t, **inference(differences(field, excluded_task=t))} for t in protocol["plan"]["heldout"]],
                  "repeats": [{"repeat": r, **inference(differences(field, repeat=r))} for r in range(3)]}
        source_trigger = {b["domain"]: next(r for r in reviews if r["arm"] == "source" and r["domain"] == b["domain"])["strict_success"] is False for b in blocks}
        result["trigger_subsets"] = {str(triggered): inference([v for v, b in zip(values, blocks) if source_trigger[b["domain"]] == triggered]) for triggered in (True, False)}
        result["environment_gap_tasks_separated"] = {"excluded_tasks": ["hr.offboarding_multi_system", "hr.compliance_training_enrollment"],
                                                     **inference(differences(field, excluded_tasks=("hr.offboarding_multi_system", "hr.compliance_training_enrollment")))}
        results[name] = result
    save(OUT / "completion/scoring_sensitivity.json", results)
    csv_table(OUT / "tables/scoring_intervals.csv", exports)
    alternatives = [("accepted_quote_policy", "finance.xero_quote_to_invoice"),
                    ("vp_standard_prefix", "sales.slack_deal_notification"),
                    ("recall_excludes_logging", "support.gorgias_defect_tracking")]
    variants = []
    for flags in itertools.product((False, True), repeat=len(alternatives)):
        enabled = {task: name for (name, task), flag in zip(alternatives, flags) if flag}

        def value(row):
            if row["task"] in enabled:
                return row["alternative_success"][enabled[row["task"]]]
            return row["full_business_success"]

        deltas = [sum(int(value(lookup[m["task"], "F1R0", repeat])) - int(value(lookup[m["task"], "F0R0", repeat]))
                      for m in b["migration"] for repeat in range(3)) / 12 for b in blocks]
        variants.append({"interpretations": list(enabled.values()), "primary": inference(deltas),
                         "migration_successes": {arm: sum(value(r) for r in reviews if r["scope"] == "migration" and r["arm"] == arm) for arm in ARMS},
                         "source_learning_trigger": {r["domain"]: not value(r) for r in reviews if r["arm"] == "source"}})
    save(OUT / "completion/authorization_sensitivity.json", variants)
    uncertainty = []
    for direction in ("minimum_F1R0_minus_F0R0", "maximum_F1R0_minus_F0R0"):
        vals = []
        for b in blocks:
            difference = 0
            for m in b["migration"]:
                for repeat in range(3):
                    a, c = lookup[m["task"], "F1R0", repeat], lookup[m["task"], "F0R0", repeat]
                    left = a["full_business_success"] or (a["full_business_state"] == "unknown" and direction.startswith("maximum"))
                    right = c["full_business_success"] or (c["full_business_state"] == "unknown" and direction.startswith("minimum"))
                    difference += int(left) - int(right)
            vals.append(difference / 12)
        uncertainty.append({"assignment": direction, **inference(vals)})
    save(OUT / "completion/unknown_obligation_bounds.json", uncertainty)
    return results


def transfer_reading(reviews, protocol):
    task_labels = read(ROOT / "experiments/feedback_chain/frozen/labels_v1.json")
    label_indices = {t["task"]: (bi, ti) for bi, b in enumerate(task_labels["blocks"]) for ti, t in enumerate(b["tasks"])}
    rows = []
    for block in protocol["blocks"]:
        for task in block["migration"]:
            identity = task["task"]
            relation, condition, change = TRANSFER_READING[identity]
            bi, ti = label_indices[identity]
            first = next(r for r in reviews if r["task"] == identity and r["arm"] == "ancestor" and r["repeat"] == 0)
            rows.append({"task": identity, "source": block["source"], "relation": relation,
                         "condition_reading": condition, "difference_from_source": change,
                         "source_selected_shared_structure": task["shared_structure"],
                         "selection_rationale": task["rationale"],
                         "prompt_reference": {"file": "experiments/feedback_chain/frozen/labels_v1.json", "pointer": f"/blocks/{bi}/tasks/{ti}/prompt"},
                         "authorized_material_reference": {"file": first["source_path"], "pointer": "/initial_world"},
                         "origin": "root_agent_reading_of_prompts_authorized_materials_and_candidate_diffs"})
    if len(rows) != 24 or len(TRANSFER_READING) != 24:
        raise ValueError("迁移适用关系未覆盖24题")
    save(OUT / "completion/transfer_applicability.json", rows)
    csv_table(OUT / "tables/transfer_applicability.csv", [{k: v for k, v in row.items() if isinstance(v, str)} for row in rows])
    return rows


def lineage_diagrams(status, transfer):
    figures = OUT / "figures"
    figures.mkdir(exist_ok=True)
    slots = {(r["domain"], r["arm"]): r for r in status["slots"]}
    metrics = {(r["domain"], r["arm"]): r for r in transfer["migration_layer"]["candidates"]}
    sources = {r["domain"]: r for r in transfer["source_layer"]["blocks"]}
    graph = ["flowchart LR", 'A["Frozen ancestor<br/>0faf0fdd660e"]']
    edge_rows = []
    for block in status["blocks"]:
        domain = block["domain"]
        graph.append(f'subgraph {domain}["{domain}"]')
        graph.append(f'{domain}_source["Source: strict {int(block["source_strict_success"])} / business {int(block["source_full_business_success"])}"]')
        for arm in ARMS[:4]:
            key = domain + "_" + arm
            slot, metric = slots[domain, arm], metrics[domain, arm]
            state = "edited" if slot["status"] == "edited" else "invalid edit; ancestor" if slot["status"] == "invalid_edit" else "not triggered; ancestor"
            label = f'{arm}: {state}<br/>edit {metric["generation_cny"]:.4f} CNY<br/>retest {metric["source_retest_cny"]:.4f} CNY; migrate {metric["migration_cny"]:.4f} CNY<br/>business {metric["business_successes"]}/12; ancestor {metric["ancestor_business_successes"]}/12'
            graph.append(f'{key}["{label}"]')
            graph.append(f'{domain}_source --> {key}')
            edge_rows.append({"domain": domain, "arm": arm, "parent": status["protocol"]["ancestor_version"],
                              "deployed_version": slot["deployed_version"], "status": slot["status"],
                              "generation_cny": metric["generation_cny"], "source_retest_cny": metric["source_retest_cny"],
                              "migration_cny": metric["migration_cny"], "business_successes": metric["business_successes"],
                              "reference": slot["reference"]})
        graph.append("end")
        graph.append(f'A --> {domain}_source')
    text = "\n".join(graph) + "\n"
    (figures / "figureS2_candidate_lineage.mmd").write_text(text, encoding="utf-8")
    (figures / "figureS2_candidate_lineage.md").write_text("# 冻结祖先、24个候选槽位与后续评价\n\n来源首跑与整理费用在块级共享，不在四槽位重复加计。节点费用采用账本结算；business为补充业务评价。\n\n```mermaid\n" + text + "```\n", encoding="utf-8")
    save(OUT / "completion/candidate_edges.json", edge_rows)
    # 历史图只为真实内容版本建立边，提议未生成候选单独登记为事件。
    events = lines(OUT / "history/version_events.jsonl")
    versions, edges = {}, []
    for event in events:
        parent, child = event.get("parent_version"), event.get("candidate_version")
        if parent and child:
            versions.setdefault((event["lineage_id"], parent), "v" + str(len(versions)))
            versions.setdefault((event["lineage_id"], child), "v" + str(len(versions)))
            edges.append(event)
    history_graph = ["flowchart LR"]
    for (family, version), name in versions.items():
        history_graph.append(f'{name}["{family}<br/>{version[:12]}"]')
    for event in edges:
        a, b = versions[event["lineage_id"], event["parent_version"]], versions[event["lineage_id"], event["candidate_version"]]
        history_graph.append(f'{a} -->|"{event["selection_event"]}"| {b}')
    (figures / "figureS3_historical_lineage.mmd").write_text("\n".join(history_graph) + "\n", encoding="utf-8")
    save(OUT / "completion/historical_edges.json", edges)
    return {"candidate_slots": len(edge_rows), "historical_events": len(events), "historical_version_edges": len(edges), "historical_vertices": len(versions)}


def carry_evidence(reviews):
    indexed = {r["execution_id"]: r for r in reviews}
    pairs = read(OUT / "carry.json")["pairs"]
    mapping = read(ROOT / "experiments/feedback_chain/paid/business_review/mapping.json")
    masks = {r["execution_id"]: key for key, r in mapping.items()}
    verdicts = {identity: read(ROOT / "experiments/feedback_chain/paid/business_review/verdicts" / (mask + ".json")) for identity, mask in masks.items()}
    evidence = []
    for pair in pairs:
        first, fresh, carry = (indexed[pair[key]] for key in ("first_id", "fresh_id", "carry_id"))
        changed = []
        for index, (a, b, c) in enumerate(zip(verdicts[first["execution_id"]]["requirements"], verdicts[fresh["execution_id"]]["requirements"], verdicts[carry["execution_id"]]["requirements"], strict=True)):
            if a["requirement"] != b["requirement"] or a["requirement"] != c["requirement"]:
                raise ValueError("携带轨迹要求身份不一致")
            if a["passed"] != c["passed"] or b["passed"] != c["passed"]:
                changed.append({"requirement": a["requirement"], "first_passed": a["passed"], "fresh_passed": b["passed"], "carry_passed": c["passed"],
                                "carry_evidence": c["evidence"], "fresh_evidence": b["evidence"], "frozen_requirement_index": index})
        final_failures = [{"id": r["id"], "requirement": r["requirement"], "source": r["source"], "pointers": r["evidence_pointers"]} for r in carry["requirements"] if not r["passed"]]
        gain = pair["carry_success"] > pair["fresh_success"]
        row = {"task": pair["task"], "repeat": pair["repeat"], "first_execution_id": pair["first_id"], "fresh_execution_id": pair["fresh_id"], "carry_execution_id": pair["carry_id"],
               "first_frozen": pair["first_success"], "fresh_frozen": pair["fresh_success"], "carry_frozen": pair["carry_success"],
               "first_business": first["full_business_success"], "fresh_business": fresh["full_business_success"], "carry_business": carry["full_business_success"],
               "frozen_extra_success": gain, "first_failed_recovery": bool(not pair["first_success"] and pair["carry_success"]),
               "changed_frozen_requirements": changed, "carry_business_failures": final_failures,
               "case_reading": CARRY_READING[pair["task"]] if gain else "两种再次执行的冻结任务标签相同；要求分歧和补充业务遗漏继续单列。",
               "actions": {key: pair[key] for key in ("fresh_actions", "carry_actions")},
               "fresh_total_cny": pair["fresh_total_cny"], "carry_total_cny": pair["carry_total_cny"],
               "source_files": {"first": first["source_path"], "fresh": fresh["source_path"], "carry": carry["source_path"]},
               "input_reference": {"file": carry["source_path"], "pointer": "/wire_trace/0/request/3/content"}}
        evidence.append(row)
    summary = {"pairs": len(evidence), "frozen_extra_successes": sum(r["frozen_extra_success"] for r in evidence),
               "extra_success_after_first_failed": sum(r["frozen_extra_success"] and not r["first_frozen"] for r in evidence),
               "extra_success_after_first_passed": sum(r["frozen_extra_success"] and r["first_frozen"] for r in evidence),
               "all_first_failed_recoveries": sum(r["first_failed_recovery"] for r in evidence),
               "same_frozen_result_pairs": sum(not r["frozen_extra_success"] for r in evidence),
               "same_frozen_result_with_business_failure": sum(not r["frozen_extra_success"] and not r["carry_business"] for r in evidence),
               "qualification": "差异要求连接真实前次反馈、后次动作和终态引用；新上下文重跑与首跑是循环中的不同执行。"}
    save(OUT / "completion/carry_evidence.json", {"summary": summary, "cases": evidence})
    return summary


def requirement_flow():
    events = read(OUT / "requirements/events.json")
    observations = read(OUT / "requirements/observations.json")
    grouped = defaultdict(list)
    for row in observations:
        grouped[row["requirement_event_id"]].append(row)
    selected = [r for r in events if r["selected_in_reason"]]
    expressed = [r for r in selected if r["written"] == "full"]
    observed = [r for r in expressed if any(o["requirement_completion"] in ("pass", "fail") for o in grouped[r["requirement_event_id"]])]
    completed = [r for r in observed if any(o["requirement_completion"] == "pass" for o in grouped[r["requirement_event_id"]])]
    stages = [("feedback exposure", events), ("selected in edit reason", selected), ("fully written", expressed),
              ("requirement outcome observed", observed), ("requirement completed at least once", completed)]
    counts = [{"stage": name, "requirements": len(rows), "rate_from_previous": len(rows) / len(stages[i - 1][1]) if i else None,
               "rate_from_exposure": len(rows) / len(events), "requirement_event_ids": [r["requirement_event_id"] for r in rows]} for i, (name, rows) in enumerate(stages)]
    result = {"unit": "一次编辑包中的一项有明确身份的要求暴露", "stages": counts,
              "definition": "后继阶段使用要求完成判读，任务成败另列；完成至少一次与每次都完成分别统计。",
              "completed_in_every_observed_opportunity": sum(all(o["requirement_completion"] == "pass" for o in grouped[r["requirement_event_id"]]
                                                                 if o["requirement_completion"] in ("pass", "fail")) for r in observed),
              "unidentifiable_summary_events": sum(r["identity_review"] == "summary_only_no_requirement_identity" for r in events),
              "source": "experiments/negative_result_offline/requirements/observations.json"}
    save(OUT / "completion/requirement_flow.json", result)
    return {"stages": [{"stage": r["stage"], "requirements": r["requirements"]} for r in counts],
            "completed_in_every_observed_opportunity": result["completed_in_every_observed_opportunity"]}


def component_business():
    from engine.negative_result_business import evaluate, completion_states
    from engine.negative_result_history import original_path

    registry = {r["execution_id"]: r for r in lines(OUT / "history/executions.jsonl")}
    labels = {r["task"]: r for b in read(ROOT / "experiments/feedback_chain/frozen/labels_v1.json")["blocks"] for r in b["tasks"]}
    reports = OUT / "replay/history_effects/records"
    results = []
    for path in sorted(reports.glob("*.json")):
        replay = read(path)
        record = read(original_path(ROOT, registry[replay["execution_id"]]["evidence_path"]))
        if replay["assertion_differences"] or replay["saved_success"] != replay["success"]:
            raise ValueError("历史组件轨迹重放与原始断言不一致")
        effects = [{"index": i, "unit": event.get("unit"), "effects": event.get("effects"),
                    "next_actual_tool_action": event.get("next_actual_tool_action"), "boundary_index": event.get("boundary_index")}
                   for i, event in enumerate(record["injections"]) if event.get("effects")]
        value = {"execution_id": replay["execution_id"], "task": record["task"], "strict_success": record["success"],
                 "effects": effects, "reference": {"file": registry[replay["execution_id"]]["evidence_path"], "pointer": "/injections"},
                 "replay_reference": {"file": path.relative_to(ROOT).as_posix(), "pointer": "/final_world"}}
        if record["task"] in labels:
            enriched = dict(record, initial_world=replay["initial_world"], final_world=replay["final_world"])
            checks = evaluate(enriched, labels[record["task"]])
            value.update(completion_states(enriched, checks))
            value["requirements"] = checks
            value["business_coverage"] = "与本次30题共用完整业务义务及同一解释；原始断言已重放"
        else:
            value["business_coverage"] = "该任务退出30题业务复核范围；逐项断言在官方状态重放完成，任务级业务完整性保持未核实"
            value["requirements"] = [{"id": "official:" + str(i), **item} for i, item in enumerate(replay["assertions"])]
        results.append(value)
    summary = {"historical_effect_executions": len(results), "official_replay_equal": len(results),
               "with_shared_full_business_review": sum("full_business_state" in r for r in results),
               "business_states": dict(Counter(r["full_business_state"] for r in results if "full_business_state" in r)),
               "replay_strict_successes": sum(r["strict_success"] for r in results),
               "qualification": "效果事件、下游动作及要求状态逐执行关联；多个效果与多个动作共存时不把整题结果归于单一组件。"}
    save(OUT / "completion/historical_component_business.json", {"summary": summary, "executions": results})
    return summary


def frozen_disagreements():
    paid = ROOT / "experiments/feedback_chain/paid"
    mapping = read(paid / "business_review/mapping.json")
    decisions = []
    for masked, item in mapping.items():
        if item["task"] not in ("finance.xero_multicurrency_payment", "hr.benefits_open_enrollment_processing",
                                "hr.intern_program_coordination", "marketing.event_sponsorship_screen"):
            continue
        record = read(paid / "executions" / item["arm"] / (item["execution_id"] + ".json"))
        verdict = read(paid / "business_review/verdicts" / (masked + ".json"))
        for i, (assertion, requirement) in enumerate(zip(record["assertions"], verdict["requirements"], strict=True)):
            if assertion["passed"] == requirement["passed"]:
                continue
            if item["task"] == "finance.xero_multicurrency_payment":
                number = assertion["params"]["invoice_number"]
                invoice = next(x for x in record["final_world"]["xero"]["invoices"] if x["invoice_number"] == number)
                judged = any(x["invoice_id"] == invoice["invoice_id"] for x in record["final_world"]["xero"]["payments"])
                explanation = "付款invoice_id能与实际发票编号对应；付款存在与原币金额正确分别核验。"
            elif item["task"] == "hr.benefits_open_enrollment_processing":
                actions = record["final_world"]["bamboohr"]["actions"].get("update_employee", [])
                judged = any(a["params"].get("employee_id") in ("1001", "EMP-1001") for a in actions)
                explanation = "实际BambooHR动作使用numeric id 1001，对象Alice的员工编号为EMP-1001；动作对象与方案在新增义务中另核验。"
            elif item["task"] == "hr.intern_program_coordination":
                address = assertion["params"]["to"]
                name, literal_date = assertion["params"]["body_contains"]
                source_date = "2026-06-09" if literal_date == "June 9" else "2026-06-02"
                judged = any(address in m.get("to", []) and name in m.get("body_plain", "") and
                             (source_date in m.get("body_plain", "") or literal_date in m.get("body_plain", ""))
                             for m in record["final_world"]["gmail"]["messages"])
                explanation = "通知保留源资料ISO日期，日历事实与June日期相同；日期格式与导师审批及通知顺序分别核验。"
            else:
                expected = str(assertion["params"]["body_contains"])
                judged = any("finance@company.example.com" in m.get("to", []) and expected in m.get("body_plain", "").replace(",", "")
                             for m in record["final_world"]["gmail"]["messages"])
                explanation = "财务通知中的千位分隔符没有改变对应金额；源表数值、六个活动与总额另有业务核验。"
            decisions.append({"execution_id": item["execution_id"], "task": item["task"], "index": i,
                              "official": assertion["passed"], "frozen": requirement["passed"], "adjudicated": judged,
                              "reading": explanation, "evidence": requirement["evidence"],
                              "source": "experiments/feedback_chain/paid/business_review/materials/" + masked + ".json"})
    if len(decisions) != 44:
        raise ValueError("冻结判读分歧没有完整覆盖44条")
    result = {"reviewer": "root_agent_reads_original_evidence_and_recomputes_explicit_object_values",
              "summary": {"disagreements_reviewed": len(decisions), "support_frozen": sum(r["adjudicated"] == r["frozen"] for r in decisions),
                          "support_official": sum(r["adjudicated"] == r["official"] for r in decisions)}, "decisions": decisions}
    save(OUT / "completion/frozen_disagreement_review.json", result)
    return result["summary"]


def guard_protocol_replay():
    from jsonpointer import resolve_pointer
    from engine.negative_result_history import original_path

    material = read(OUT / "requirements/vp_guard_inputs.json")
    sources = {row["source_sha256"]: row["source"] for row in material["source_versions"].values()}
    lead_runs = {row["execution_id"] for row in material["events"] if row["gate"] == "target_prompt_already_contains_VP_in_first_200"}
    paths = {r["execution_id"]: r["evidence_path"] for r in lines(OUT / "history/executions.jsonl")}
    paths.update({r["execution_id"]: r["path"] for r in read(OUT / "executions.json")})
    records, events = {}, []
    for item in material["events"]:
        if not item["target_route"] and not (item["hook"] == "task_start" and item["execution_id"] in lead_runs):
            continue
        identity = item["execution_id"]
        if identity not in records:
            records[identity] = read(original_path(ROOT, paths[identity]))
        record = records[identity]
        injection = resolve_pointer(record, item["reference"]["pointer"])
        position = int(item["reference"]["pointer"].rsplit("/", 1)[-1])
        earlier = [value for value in record["injections"][:position] if value.get("unit") == "ace_op_vp_guard"]
        state = earlier[-1]["output"].get("state") if earlier else None
        boundary = next(row for row in record["boundary_trace"] if row["index"] == item["boundary_index"])
        context = {"event": boundary, "input": injection["input"], "state": state,
                   "task": {"id": record["task"], "instruction": "\n".join(message["content"] for message in record["prompt"] if message["role"] == "user")}}
        events.append({"execution_id": identity, "context": context, "source_sha256": item["source_sha256"],
                       "captured_output": injection["output"], "captured_effects": injection["effects"], "reference": item["reference"]})
    save(OUT / "completion/guard_replay_inputs.json", {"sources": sources, "events": events})
    subprocess.run(["node", str(ROOT / "engine/negative_result_guard_replay.cjs")], cwd=ROOT, check=True)
    result = read(OUT / "completion/guard_protocol_replay.json")
    if result["summary"]["argument_rewrites"] or result["summary"]["emitted_calls"]:
        raise ValueError("组件协议重放产生了历史未登记效果")
    return result["summary"]


def integrate_database(reviews, status, cost, protocol):
    connection = sqlite3.connect(OUT / "research.sqlite")
    # 分析派生表可重建，原始实验数据库始终只读。
    names = ("research_executions", "execution_inputs", "proposal_events", "version_edges", "feedback_events", "requirement_observations", "functional_retention", "business_judgements", "review_sources", "candidate_slots", "evidence_links")
    for name in names:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {name} (identity TEXT PRIMARY KEY, execution_id TEXT, task TEXT, source_file TEXT, source_pointer TEXT, payload TEXT)")
        connection.execute(f"DELETE FROM {name}")

    def insert(name, identity, value, execution=None, task=None, source=None, pointer=""):
        connection.execute(f"INSERT INTO {name} VALUES (?,?,?,?,?,?)", (str(identity), execution, task, source, pointer, json.dumps(value, ensure_ascii=False)))

    historic = lines(OUT / "history/executions.jsonl")
    paid = read(OUT / "executions.json")
    seen = set()
    for index, row in enumerate(historic):
        identity = row["execution_id"]
        if identity in seen:
            raise ValueError("历史执行身份重复")
        seen.add(identity)
        insert("research_executions", identity, row, identity, row["task"], "experiments/negative_result_offline/history/executions.jsonl", f"line:{index + 1}")
    for index, row in enumerate(paid):
        identity = row["execution_id"]
        if identity in seen:
            raise ValueError("付费执行与历史执行身份重复，需要合并来源")
        seen.add(identity)
        insert("research_executions", identity, row, identity, row["task"], "experiments/negative_result_offline/executions.json", f"/{index}")
    for index, row in enumerate(read(OUT / "completion/historical_input_conditions.json")):
        insert("execution_inputs", row["execution_id"], row, row["execution_id"], row["task"], "experiments/negative_result_offline/completion/historical_input_conditions.json", f"/{index}")
    for name, filename in (("proposal_events", "proposal_attempts.jsonl"), ("version_edges", "version_events.jsonl"), ("feedback_events", "requirement_events.jsonl")):
        for index, row in enumerate(lines(OUT / "history" / filename)):
            insert(name, name + ":" + str(index), row, task=row.get("task"), source="experiments/negative_result_offline/history/" + filename, pointer=f"line:{index + 1}")
    for index, row in enumerate(read(OUT / "requirements/observations.json")):
        insert("requirement_observations", row["requirement_event_id"] + "/" + row["execution_id"], row, row["execution_id"], source="experiments/negative_result_offline/requirements/observations.json", pointer=f"/{index}")
    for index, row in enumerate(read(OUT / "requirements/retention.json")):
        insert("functional_retention", str(index), row, task=row.get("task"), source="experiments/negative_result_offline/requirements/retention.json", pointer=f"/{index}")
    for index, row in enumerate(status["slots"]):
        insert("candidate_slots", row["domain"] + "/" + row["arm"], row, task=next(b["source"] for b in protocol["blocks"] if b["domain"] == row["domain"]), source="experiments/negative_result_offline/history/candidate_status.json", pointer=f"/slots/{index}")
    for index, review in enumerate(reviews):
        for j, requirement in enumerate(review["requirements"]):
            insert("business_judgements", review["execution_id"] + "/" + requirement["id"], requirement, review["execution_id"], review["task"], "experiments/negative_result_offline/business/execution_reviews.json", f"/{index}/requirements/{j}")
        insert("review_sources", review["execution_id"], {"reviewer": review["reviewer"], "masked_id": review.get("masked_id"), "scope": review["scope"]}, review["execution_id"], review["task"], "experiments/negative_result_offline/business/execution_reviews.json", f"/{index}")
    for section in ("independent_review", "requirements", "replay"):
        for path in sorted((OUT / section).glob("*.json")):
            insert("evidence_links", path.relative_to(OUT).as_posix(), {"kind": section}, source=path.relative_to(ROOT).as_posix())
    counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}
    connection.commit()
    connection.close()
    save(OUT / "completion/database_counts.json", counts)
    return counts


def run():
    reviews = read(OUT / "business/execution_reviews.json")
    protocol = read(ROOT / "experiments/feedback_chain/frozen/execution_v1.json")
    status = read(OUT / "history/candidate_status.json")
    transfer = read(OUT / "transfer.json")
    costs = read(OUT / "costs.json")
    statistical = stats(reviews, protocol)
    applicability = transfer_reading(reviews, protocol)
    diagrams = lineage_diagrams(status, transfer)
    carry = carry_evidence(reviews)
    flow = requirement_flow()
    components = component_business()
    frozen_review = frozen_disagreements()
    guard = guard_protocol_replay()
    db = integrate_database(reviews, status, costs, protocol)
    summary = {"statistics": {name: {k: value for k, value in data["primary"].items() if k != "sign_combinations"} for name, data in statistical.items()},
               "lineage": diagrams, "transfer_tasks": len(applicability), "carry_evidence": carry,
               "requirement_flow": flow, "historical_component_business": components, "database": db, "model_calls": 0}
    summary["frozen_disagreement_review"] = frozen_review
    summary["guard_protocol_replay"] = guard
    save(OUT / "completion/summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    run()
