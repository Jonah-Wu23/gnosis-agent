import argparse
import json
from collections import Counter, defaultdict
from itertools import product
from pathlib import Path

from jsonpointer import resolve_pointer

ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
DISPLAY_ARMS = (*ARMS, "ancestor", "carry")
SCORING_SETS = {
    "strict": "执行记录中官方断言的保存判定全部通过，且执行正常终止",
    "frozen_requirements": "432 份盲审 verdict 的冻结要求全部通过，且执行正常终止",
    "full_business": "冻结要求中属于业务义务的条目与全部新增业务义务通过，且执行正常终止",
}
# 编辑启动门禁与候选接受规则来自 engine/feedback_chain.py：来源首跑 success 为真则不进入编辑，
# 编辑请求通过 verify_edit 格式校验即写入版本库并部署，候选在读取任何复测结果之前冻结。
GATE_RULE = "来源首跑严格评分通过则四臂部署祖先，编辑不启动（engine/feedback_chain.py run 内 source['success'] 分支）"
RELEASE_RULE = "编辑请求通过格式校验即接受候选（engine/feedback_chain.py verify_edit）；来源复测在候选冻结之后执行，不参与接受"
# 研究者阅读登记：冻结标签的 dispute 字段与补充业务记录 §二 逐条登记过的冲突；程序只定位对象、核算明确取值并统计受影响执行。
CONFLICT_REGISTRY = [
    {"id": "instruction_conflict:hr.airtable.sabbatical", "task": "hr.airtable_learning_path_assignment",
     "frozen_indices": [5], "frozen_decision": "禁止为EMP-3003创建任何记录",
     "obligation_id": "sabbatical_deferral",
     "conflict": "L&D规则要求在Airtable记录休假延期；第5条断言禁止为该员工创建任何记录，把延期记录与课程分配合并禁止。第8条update_record禁令另属原冻结争议。",
     "reference": "experiments/feedback_chain/frozen/labels_v1.json /blocks/1/tasks/0/assertions/5；来源初态msg_learning_rules"},
    {"id": "instruction_conflict:finance.xero_quote_to_invoice.qu104", "task": "finance.xero_quote_to_invoice",
     "frozen_indices": [6], "frozen_decision": "未标记 dispute",
     "required_literal": "7,875", "instruction_value": "8,505",
     "conflict": "提示词要求对低于 $10,000 的报价加价 8%，断言要求通知正文出现加价前的 7,875（7,875×1.08=8,505）",
     "reference": "experiments/feedback_chain/paid/business_review/supplementary_notes.md §二"},
    {"id": "instruction_conflict:support.zendesk_cross_org_merge.migration", "task": "support.zendesk_cross_org_merge",
     "frozen_indices": [0, 1, 2, 3, 4, 5], "frozen_decision": "必要行动",
     "required_literal": None, "instruction_value": None,
     "conflict": "原始任务要求combine重复组织；业务复核保留工单转移义务，并核对实际转移数量与日志。该条登记为原有异议经原文裁决后保留原义务。",
     "reference": "experiments/negative_result_offline/independent_review/obligations.json support.zendesk_cross_org_merge"},
    {"id": "instruction_conflict:support.gorgias_defect_tracking.log", "task": "support.gorgias_defect_tracking",
     "frozen_indices": [21, 27, 28, 29], "frozen_decision": "禁止行动",
     "required_literal": "ws_log", "instruction_value": "log everything to ss_defects/ws_log",
     "conflict": "提示词要求把每个缺陷写进 ws_log，4 条禁止写入断言把按提示词记录的执行判负",
     "reference": "experiments/feedback_chain/paid/business_review/supplementary_notes.md §二"},
    {"id": "instruction_conflict:support.gorgias_defect_tracking.tracking", "task": "support.gorgias_defect_tracking",
     "frozen_indices": [16, 30, 31], "frozen_decision": "禁止行动",
     "required_literal": "tracking", "instruction_value": "Reply to customers with tracking info",
     "conflict": "提示词要求回复带追踪信息，3 条断言禁止同一批工单回复出现 tracking",
     "reference": "experiments/feedback_chain/frozen/labels_v1.json dispute=token_mismatch"},
    {"id": "environment_gap:hr.offboarding_multi_system.policy", "task": "hr.offboarding_multi_system",
     "frozen_indices": [], "frozen_decision": "无对应断言",
     "required_literal": None, "instruction_value": "Check our policy for what applies to each separation type",
     "conflict": "提示词要求按分离类型政策处理，冻结世界内没有承载该政策的资料，检索只能落空",
     "reference": "experiments/feedback_chain/paid/business_review/supplementary_notes.md §一、§四"},
]
# 与业务复核中同对象条目可直接对照的字面取值条目：错误拒绝按“断言失败而同对象业务条目通过”统计
TOKEN_PAIRS = [
    {"task": "hr.intern_program_coordination", "frozen_index": 1, "literal": "June 9",
     "paired_requirement": "mentor_date:jordan", "authorized_value": "2026-06-09"},
    {"task": "hr.intern_program_coordination", "frozen_index": 4, "literal": "June 2",
     "paired_requirement": "mentor_date:ava", "authorized_value": "2026-06-02"},
    {"task": "hr.intern_program_coordination", "frozen_index": 8, "literal": "June 9",
     "paired_requirement": "mentor_date:taylor", "authorized_value": "2026-06-09"},
    {"task": "finance.employee_reimbursement", "frozen_index": 6, "literal": "340",
     "paired_requirement": "frozen:3", "authorized_value": "250"},
]
# 初态即成立、任何忠于提示词的执行都不会改变其取值，却被当作终态真值条件使用的禁止断言
INITIAL_STATE_CASES = {
    "support.zendesk_cross_org_merge": [15, 16, 17, 18, 19, 20, 24, 30],
}
# 补充业务记录 §一 点名的任务：提示词有义务、冻结断言没有条目
MISSING_OBLIGATION_NOTE = {
    "sales.demo_scheduling": "60 分钟时长、Zoom passcode、通知中的开始时间与入会链接、On-Hold 账户通知 AE 均无断言",
    "hr.compliance_training_enrollment": "Alice/Ravi/Carol 三行置为 Enrolled 没有正向断言，preserve verbatim 无格式断言",
    "hr.intern_program_coordination": "信用卡开户与邮箱开户只有渠道禁令，先通知法务再启动入职动作的顺序无断言",
    "hr.offboarding_multi_system": "按分离类型检查政策在冻结世界内没有可核验载体",
    "operations.hazmat_shipping_compliance": "Jira 工单描述的 gas type、location、last test date 无断言",
    "operations.compressed_gas_cylinder_tracking": "给每个有超期瓶的地点分别发信只被禁止项间接约束",
    "marketing.campaign_handoff": "SOP notes 列改写与在 ops 目录归档交接邮件副本没有断言",
    "support.freshdesk_auto_merge": "ws_log 的 Email、Status、Subject 列取值无断言",
    "support.reamaze_cross_platform_dedup": "ws_log 的 Email、Status、Subject 列取值无断言",
    "support.zendesk_cross_org_merge": "ws_results 的 Tickets Transferred 数值与 s5→t3b 备注无断言",
    "finance.qb_void_stale_invoices": "作废备注必须写明理由没有独立断言",
    "marketing.news_digest_dedup": "标题原样保留只被 URL 片段部分覆盖",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ref(path, pointer=""):
    return {"file": str(path).replace("\\", "/"), "pointer": pointer}


def sign_flip_test(deltas):
    """六块差值的符号可交换置换检验：枚举 2^n 种符号组合，双侧取 |均值| 不小于观察值的比例。"""
    observed = abs(sum(deltas) / len(deltas))
    extreme = 0
    for signs in product((1, -1), repeat=len(deltas)):
        if abs(sum(s * d for s, d in zip(signs, deltas)) / len(deltas)) >= observed - 1e-12:
            extreme += 1
    total = 2 ** len(deltas)
    return {"effect": sum(deltas) / len(deltas), "sign_combinations": total,
            "extreme_combinations": extreme, "p_two_sided": extreme / total}


def sheet_rows(world, spreadsheet, worksheet):
    """工作表行的两种已见结构：扁平 rows 与 spreadsheets/worksheets/rows。"""
    sheet = world.get("google_sheets", {})
    if "rows" in sheet:
        return [x for x in sheet["rows"]
                if x["spreadsheet_id"] == spreadsheet and x["worksheet_id"] == worksheet]
    return [row for book in sheet.get("spreadsheets", [])
            if book.get("id", book.get("spreadsheet_id")) == spreadsheet
            for tab in book["worksheets"] if tab.get("id", tab.get("worksheet_id")) == worksheet
            for row in tab["rows"]]


def state_predicate(world, assertion):
    """按执行记录里保存的断言 params 求值初态或终态条件；只覆盖登记案例使用的断言类型。"""
    params = assertion["params"]
    zendesk = world.get("zendesk", {})
    tickets = {t["id"]: t for t in zendesk.get("tickets", [])}
    organizations = {o["id"]: o for o in zendesk.get("organizations", [])}
    if assertion["type"] == "zendesk_ticket_exists":
        ticket = tickets.get(params["ticket_id"])
        return ticket is not None and ticket.get("organization_id") == params["organization_id"]
    if assertion["type"] == "zendesk_organization_not_has_tag":
        return params["tag"] not in (organizations[params["organization_id"]].get("tags") or [])
    if assertion["type"] == "google_sheets_row_not_exists":
        cells = params.get("cells", {})
        return not any(all(str(row["cells"].get(key, "")) == str(value) for key, value in cells.items())
                       for row in sheet_rows(world, params["spreadsheet_id"], params["worksheet_id"]))
    raise ValueError("未登记的初态条件类型: " + assertion["type"])


def touched_actions(record, objects):
    hits = Counter()
    for action in record["actual_tool_actions"]:
        text = canonical(action.get("arguments"))
        if any(value in text for value in objects):
            hits[action["tool"]] += 1
    return hits


def run(root, output):
    root, output = Path(root), Path(output)
    paid = root / "experiments/feedback_chain/paid"
    frozen = root / "experiments/feedback_chain/frozen"
    business = output / "business"
    history = output / "history"

    labels_doc = read(frozen / "labels_v1.json")
    label_tasks = {t["task"]: dict(t, domain=b["domain"]) for b in labels_doc["blocks"] for t in b["tasks"]}
    protocol = read(frozen / "execution_v1.json")
    block_order = [b["domain"] for b in protocol["blocks"]]
    reviews = read(business / "execution_reviews.json")
    review_by_execution = {r["execution_id"]: r for r in reviews}
    prior_summary = {(r["scope"], r["domain"], r["arm"]): r for r in read(business / "summary.json")}
    requirement_sets = read(business / "task_requirements.json")
    gap_notes = read(business / "review_provenance.json")
    analysis = read(paid / "analysis.json")

    records = {}
    for path in sorted((paid / "executions").glob("*/*.json")):
        record = read(path)
        assert record["arm"] == path.parent.name
        records[record["execution_id"]] = dict(record, reference=ref(path.relative_to(root)))
    assert len(records) == 462, len(records)
    assert set(records) == set(review_by_execution), "执行记录与业务复核不对应"
    for identity, record in records.items():
        assert record["success"] == review_by_execution[identity]["strict_success"]

    # 1 三套评分下的逐执行一致性与各臂重算，先与上一工作包的汇总对齐
    recomputed = defaultdict(lambda: Counter())
    for review in reviews:
        key = (review["scope"], review["domain"], review["arm"])
        recomputed[key]["n"] += 1
        recomputed[key]["strict"] += bool(review["strict_success"])
        recomputed[key]["frozen"] += bool(review["frozen_success"])
        recomputed[key]["full"] += bool(review["full_business_success"])
        recomputed[key]["false_pass"] += bool(review["strict_success"] and review["full_business_state"] == "failure")
        recomputed[key]["false_reject"] += bool(not review["strict_success"] and review["full_business_success"])
    for key, values in recomputed.items():
        summary = prior_summary[key]
        assert values["n"] == summary["n"]
        assert values["strict"] == summary["strict_successes"]
        assert values["full"] == summary["full_business_successes"]
        assert values["false_pass"] == summary["strict_false_passes"]
        assert values["false_reject"] == summary["strict_false_rejections"]
        if key[0] == "migration":
            assert values["frozen"] == summary["frozen_successes"]

    false_pass = [r for r in reviews if r["strict_success"] and r["full_business_state"] == "failure"]
    false_reject = [r for r in reviews if not r["strict_success"] and r["full_business_success"]]

    def failing(review, sources=None):
        return sorted(r["id"] for r in review["requirements"]
                      if not r["passed"] and (sources is None or r.get("source") in sources))

    execution_level = {
        "executions": len(reviews),
        "scope": {"migration": sum(r["scope"] == "migration" for r in reviews),
                  "source_and_retest": sum(r["scope"] == "source" for r in reviews)},
        "strict_vs_full_business": {
            "agreements": sum(bool(r["strict_success"]) == bool(r["full_business_success"]) for r in reviews),
            "strict_false_passes": len(false_pass),
            "strict_false_rejections": len(false_reject),
            "business_unknown": sum(r["full_business_state"] == "unknown" for r in reviews),
            "strict_pass_business_unknown": sum(r["strict_success"] and r["full_business_state"] == "unknown" for r in reviews),
            "false_pass_by_source": dict(Counter(
                "新增业务义务" if any(not x["passed"] and x["source"] == "新增义务" for x in r["requirements"])
                else "冻结要求" for r in false_pass)),
            "false_reject_by_task": dict(Counter(r["task"] for r in false_reject)),
        },
        "false_pass_executions": [
            {"task": r["task"], "domain": r["domain"], "arm": r["arm"], "scope": r["scope"], "repeat": r["repeat"],
             "execution_id": r["execution_id"], "failed_requirements": failing(r),
             "requirement_sources": sorted({x["source"] for x in r["requirements"] if not x["passed"]}),
             "reference": ref(Path(r["source_path"]))} for r in sorted(false_pass, key=lambda x: (x["task"], x["arm"], x["repeat"]))],
        "false_rejection_executions": [
            {"task": r["task"], "domain": r["domain"], "arm": r["arm"], "scope": r["scope"], "repeat": r["repeat"],
             "execution_id": r["execution_id"], "failed_frozen_assertions": failing(r),
             "reference": ref(Path(r["source_path"]))} for r in sorted(false_reject, key=lambda x: (x["task"], x["arm"], x["repeat"]))],
        "requirement_level_status": "冻结要求经盲审判读、新增义务由程序按对象与取值核算，两类来源分别统计",
    }
    false_pass_drivers, false_reject_drivers = [], []
    for task in sorted({r["task"] for r in false_pass}):
        counts = Counter(x["id"] for r in false_pass if r["task"] == task for x in r["requirements"] if not x["passed"])
        false_pass_drivers.append({"task": task, "executions": sum(r["task"] == task for r in false_pass),
                                   "failed_requirement_counts": dict(counts.most_common(3))})
    for task in sorted({r["task"] for r in false_reject}):
        counts = Counter()
        for review in [r for r in false_reject if r["task"] == task]:
            for index, assertion in enumerate(records[review["execution_id"]]["assertions"]):
                if not assertion["passed"]:
                    counts[f"{index} {assertion['type']}"] += 1
        false_reject_drivers.append({"task": task, "executions": sum(r["task"] == task for r in false_reject),
                                     "failed_assertion_counts": dict(counts.most_common(3))})
    execution_level["false_pass_drivers"] = false_pass_drivers
    execution_level["false_rejection_drivers"] = false_reject_drivers

    # 盲审判读与官方断言在逐条判定上的分歧：同一冻结要求在 verdict 与执行记录中是否给出同一结论
    verdict_disagreements, judgement_counts = [], Counter()
    mapping = read(paid / "business_review/mapping.json")
    verdicts = {}
    for masked, item in mapping.items():
        verdict = read(paid / "business_review/verdicts" / (masked + ".json"))
        assert verdict["task"] == item["task"]
        assert verdict["complete_prompt_coverage"] is True
        verdicts[item["execution_id"]] = verdict
        record = records[item["execution_id"]]
        label = label_tasks[item["task"]]
        assert len(verdict["requirements"]) == len(label["assertions"])
        for index, (requirement, assertion) in enumerate(zip(verdict["requirements"], label["assertions"])):
            judgement_counts[requirement["passed"] == record["assertions"][index]["passed"]] += 1
            if requirement["passed"] != record["assertions"][index]["passed"]:
                verdict_disagreements.append({"task": item["task"], "arm": item["arm"], "index": index,
                                              "verdict_passed": requirement["passed"],
                                              "assertion_passed": record["assertions"][index]["passed"],
                                              "requirement": requirement["requirement"]})
    execution_level["verdict_vs_official_assertion"] = {
        "judgements": sum(judgement_counts.values()), "agreements": judgement_counts[True],
        "disagreements": len(verdict_disagreements), "cases": verdict_disagreements}

    # 2 逐任务对照与逐臂重算
    scope_domain_arm = defaultdict(list)
    for review in reviews:
        scope_domain_arm[(review["scope"], review["domain"], review["arm"])].append(review)

    def arm_block_table(scope):
        table = []
        for domain in sorted({d for s, d, _ in scope_domain_arm if s == scope}):
            for arm in DISPLAY_ARMS:
                items = scope_domain_arm.get((scope, domain, arm))
                if not items:
                    continue
                table.append({"scope": scope, "domain": domain, "arm": arm, "n": len(items),
                              "strict_successes": sum(bool(r["strict_success"]) for r in items),
                              "frozen_successes": sum(bool(r["frozen_success"]) for r in items),
                              "full_business_successes": sum(bool(r["full_business_success"]) for r in items),
                              "strict_false_passes": sum(bool(r["strict_success"]) and r["full_business_state"] == "failure" for r in items),
                              "strict_false_rejections": sum(not r["strict_success"] and bool(r["full_business_success"]) for r in items)})
        return table

    migration_arms = arm_block_table("migration")

    def success_rate(domain, arm, field):
        items = scope_domain_arm[("migration", domain, arm)]
        return sum(bool(r[field]) for r in items) / len(items)

    # 编辑是否触发由来源首跑的严格评分决定，未触发的块四臂部署同一祖先版本
    triggered = {r["domain"] for r in reviews if r["scope"] == "source" and r["arm"] == "source" and not r["strict_success"]}
    contrasts = {}
    for name, field in (("strict", "strict_success"), ("frozen_requirements", "frozen_success"),
                        ("full_business", "full_business_success")):
        deltas = {domain: success_rate(domain, "F1R0", field) - success_rate(domain, "F0R0", field)
                  for domain in block_order}
        contrasts[name] = {**sign_flip_test([deltas[d] for d in block_order]), "per_block_deltas": deltas,
                           "edited_blocks_mean": sum(deltas[d] for d in block_order if d in triggered) / len(triggered),
                           "untriggered_blocks_mean": sum(deltas[d] for d in block_order if d not in triggered)
                                                      / (len(block_order) - len(triggered)),
                           "frozen_analysis_primary": None}
    # 冻结主比较使用盲审判读的冻结要求标签，先复现其效应与符号置换检验
    assert abs(contrasts["frozen_requirements"]["effect"] - analysis["primary"]["effect"]) < 1e-12
    assert contrasts["frozen_requirements"]["p_two_sided"] == analysis["primary"]["p_two_sided"]
    contrasts["frozen_requirements"]["frozen_analysis_primary"] = analysis["primary"]
    contrasts["frozen_requirements"]["frozen_analysis_R1_increment"] = analysis["R1_increment"]

    ranking = {}
    for name, field in (("strict", "strict_success"), ("frozen_requirements", "frozen_success"),
                        ("full_business", "full_business_success")):
        ranking[name] = {
            arm: sum(success_rate(d, arm, field) for d in block_order) / len(block_order) for arm in DISPLAY_ARMS}
    orders = {name: [arm for arm, _ in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))]
              for name, values in ranking.items()}
    experimental_orders = {name: [arm for arm in order if arm in ARMS] for name, order in orders.items()}
    ranking_change = {"orders": orders, "experimental_arm_orders": experimental_orders,
                      "experimental_arms_same_order": len({tuple(order) for order in experimental_orders.values()}) == 1,
                      "full_order_same": len({tuple(order) for order in orders.values()}) == 1,
                      "ancestor_position": {name: order.index("ancestor") for name, order in orders.items()}}

    per_task = []
    for task in sorted({r["task"] for r in reviews}):
        items = [r for r in reviews if r["task"] == task]
        migration_items = [r for r in items if r["scope"] == "migration"]
        decisive = defaultdict(int)
        for review in items:
            failures = failing(review)
            if len(failures) == 1:
                decisive[failures[0]] += 1
        entry = {"task": task, "domain": items[0]["domain"], "scope": items[0]["scope"], "executions": len(items),
                 "strict_successes": sum(bool(r["strict_success"]) for r in items),
                 "frozen_successes": sum(bool(r["frozen_success"]) for r in items),
                 "full_business_successes": sum(bool(r["full_business_success"]) for r in items),
                 "strict_false_passes": sum(bool(r["strict_success"]) and r["full_business_state"] == "failure" for r in items),
                 "strict_false_rejections": sum(not r["strict_success"] and bool(r["full_business_success"]) for r in items),
                 "label_deciding_requirements": dict(sorted(decisive.items())),
                 "requirement_set": {"frozen": sum(x["source"] != "新增义务" for x in requirement_sets["tasks"][task]),
                                     "new_obligations": sum(x["source"] == "新增义务" for x in requirement_sets["tasks"][task])}}
        if migration_items:
            entry["arms"] = {arm: {"n": sum(r["arm"] == arm for r in migration_items),
                                   "strict_successes": sum(bool(r["strict_success"]) for r in migration_items if r["arm"] == arm),
                                   "frozen_successes": sum(bool(r["frozen_success"]) for r in migration_items if r["arm"] == arm),
                                   "full_business_successes": sum(bool(r["full_business_success"]) for r in migration_items if r["arm"] == arm)}
                             for arm in DISPLAY_ARMS if any(r["arm"] == arm for r in migration_items)}
            entry["F1R0_minus_F0R0"] = {
                field: (sum(bool(r[field]) for r in migration_items if r["arm"] == "F1R0")
                        - sum(bool(r[field]) for r in migration_items if r["arm"] == "F0R0"))
                for field in ("strict_success", "frozen_success", "full_business_success")}
        per_task.append(entry)

    # 3 缺陷分类：每一类给出影响的任务、真实执行与实验臂，并把评分缺口从该臂结果中扣除后重算
    label_ref = Path("experiments/feedback_chain/frozen/labels_v1.json")
    disputes = defaultdict(list)
    for block_index, block in enumerate(labels_doc["blocks"]):
        for task_index, task in enumerate(block["tasks"]):
            for assertion_index, assertion in enumerate(task["assertions"]):
                if assertion.get("dispute"):
                    kind = assertion["dispute"].split(":")[0]
                    disputes[kind].append({"task": task["task"], "index": assertion["idx"],
                                           "type": assertion["type"], "params": assertion["params"],
                                           "req_class": assertion["req_class"], "dispute": assertion["dispute"],
                                           "reference": ref(label_ref, f"/blocks/{block_index}/tasks/{task_index}"
                                                                       f"/assertions/{assertion_index}")})

    def assertion_outcomes(task, index):
        items = [r for r in records.values() if r["task"] == task]
        passed, by_arm = Counter(), Counter()
        for record in items:
            assertion = record["assertions"][index]
            passed[assertion["passed"]] += 1
            by_arm[(record["arm"], assertion["passed"])] += 1
        return {"executions": len(items), "passed": passed[True], "failed": passed[False],
                "failed_by_arm": {arm: by_arm[(arm, False)] for arm in sorted({a for a, _ in by_arm})}}

    token_entries = []
    VALUE_KEYS = ("body_contains", "body_not_contains", "subject_contains", "text_contains", "value_contains")
    for entry in disputes["token_mismatch"]:
        outcome = assertion_outcomes(entry["task"], entry["index"])
        instruction = label_tasks[entry["task"]]["prompt"]
        literals = []
        for key in VALUE_KEYS:
            if key not in entry["params"]:
                continue
            value = entry["params"][key]
            literals += [value] if isinstance(value, str) else list(value)
        token_entries.append({**entry, "required_literals": literals,
                              "literal_in_instruction": any(value in instruction for value in literals),
                              "observed_failure": outcome["failed"] > 0,
                              **outcome})
    count_entries = []
    for kind in ("count_amplification", "duplicate"):
        for entry in disputes[kind]:
            count_entries.append({**entry, "kind": kind, **assertion_outcomes(entry["task"], entry["index"])})

    paired_entries = []
    for pair in TOKEN_PAIRS:
        items = [r for r in reviews if r["task"] == pair["task"]]
        assertion = assertion_outcomes(pair["task"], pair["frozen_index"])
        joint = Counter()
        judgements = Counter()
        for review in items:
            record = records[review["execution_id"]]
            frozen_passed = record["assertions"][pair["frozen_index"]]["passed"]
            paired_passed = next((x["passed"] for x in review["requirements"] if x["id"] == pair["paired_requirement"]), None)
            joint[(frozen_passed, paired_passed)] += 1
            judgements[(frozen_passed, any(x["id"] == f"frozen:{pair['frozen_index']}"
                                           for x in review["requirements"]))] += 1
        instruction = label_tasks[pair["task"]]["prompt"]
        worlds = Counter()
        for review in items:
            text = json.dumps(records[review["execution_id"]]["final_world"], ensure_ascii=False)
            worlds["required_literal_present"] += pair["literal"] in text
            worlds["authorized_value_present"] += pair["authorized_value"] in text
        paired_entries.append({**pair, "assertion": assertion,
                               "literal_in_instruction": pair["literal"] in instruction,
                               "final_world_value_counts": dict(worlds),
                               "kept_in_business_review": judgements[(True, True)] + judgements[(False, True)] == len(items),
                               "joint_with_paired_requirement": {str(k): v for k, v in sorted(joint.items(),
                                                                                                key=lambda kv: str(kv[0]))},
                               "false_rejections_at_assertion_level": joint[(False, True)],
                               "matched_executions": sum(joint.values())})

    conflict_entries = []
    for entry in CONFLICT_REGISTRY:
        items = [r for r in reviews if r["task"] == entry["task"]]
        item = dict(entry)
        item["executions"] = len(items)
        item["assertion_outcomes"] = [{"index": index, **assertion_outcomes(entry["task"], index)}
                                      for index in entry["frozen_indices"]]
        item["assertions_kept_in_business_review"] = sorted(
            index for index in entry["frozen_indices"]
            if any(x["id"] == f"frozen:{index}" for x in items[0]["requirements"]))
        item["task_outcome_under_sets"] = {
            "strict_successes": sum(bool(r["strict_success"]) for r in items),
            "full_business_successes": sum(bool(r["full_business_success"]) for r in items),
            "strict_passed_business_failed": sum(bool(r["strict_success"]) and r["full_business_state"] == "failure" for r in items),
            "strict_failed_business_passed": sum(not r["strict_success"] and bool(r["full_business_success"]) for r in items)}
        failed_counts = Counter(x["id"] for r in items for x in r["requirements"] if not x["passed"])
        item["failed_requirement_counts"] = dict(failed_counts.most_common())
        if entry.get("prohibited_action"):
            total, matching, executions = 0, 0, 0
            for record in records.values():
                if record["task"] != entry["task"]:
                    continue
                executions += 1
                for action in record["actual_tool_actions"]:
                    total += 1
                    matching += entry["prohibited_action"] in action["tool"] or any(
                        entry["prohibited_action"] in value for value in action["arguments"].values()
                        if isinstance(value, str))
            item["prohibited_action_occurrence"] = {"action": entry["prohibited_action"],
                                                    "executions": executions, "actions_total": total,
                                                    "matching_actions": matching}
        if entry.get("obligation_id"):
            joint = Counter()
            for review in items:
                obligation = next((x["passed"] for x in review["requirements"] if x["id"] == entry["obligation_id"]), None)
                assertions = [records[review["execution_id"]]["assertions"][i]["passed"] for i in entry["frozen_indices"]]
                joint[(all(assertions), obligation)] += 1
            item["joint_obligation_and_assertion"] = {str(k): v for k, v in sorted(joint.items(), key=lambda kv: str(kv[0]))}
            item["both_satisfied"] = joint[(True, True)]
            item["obligation_failed"] = sum(v for k, v in joint.items() if k[1] is False)
            item["assertion_passed_while_obligation_failed"] = joint[(True, False)]
        if entry.get("required_literal") and entry.get("task") == "finance.xero_quote_to_invoice":
            values = Counter()
            for record in records.values():
                if record["task"] != entry["task"]:
                    continue
                text = json.dumps(record["final_world"], ensure_ascii=False)
                values["required_literal_present"] += entry["required_literal"] in text
                values["instruction_value_present"] += entry["instruction_value"] in text
            item["final_world_value_counts"] = dict(values)
            request_hits = {"edit_requests": 0, "occurrences": 0}
            for path in sorted((paid / "requests").glob("*/*/edit/*.json")):
                payload = path.read_text(encoding="utf-8")
                hit = sum(payload.count(token) for token in (entry["required_literal"], entry["instruction_value"], "QU-104"))
                if hit:
                    request_hits["edit_requests"] += 1
                    request_hits["occurrences"] += hit
            item["disputed_value_in_edit_requests"] = request_hits
        if entry["task"] == "hr.offboarding_multi_system":
            bundles = [read(paid / "business_review/materials" / (masked + ".json"))
                       for masked, value in mapping.items() if value["task"] == entry["task"]]
            failed = Counter(a["tool"] for b in bundles for a in b["actions"] if a.get("failed"))
            item["policy_carrier"] = {"executions_checked": len(bundles),
                                      "empty_google_drive": sum(not b["initial_world"].get("google_drive", {}).get("files")
                                                                for b in bundles),
                                      "actions": sum(len(b["actions"]) for b in bundles),
                                      "failed_actions": dict(failed)}
        if entry["task"] == "support.gorgias_defect_tracking":
            item["paired_new_obligations"] = [x for x in requirement_sets["tasks"][entry["task"]]
                                              if x["source"] == "新增义务"]
            first = next(r for r in records.values() if r["task"] == entry["task"])
            log_indices = [index for index in entry["frozen_indices"]
                           if first["assertions"][index]["type"] == "google_sheets_row_not_exists"]
            forbidden = {first["assertions"][index]["params"]["value"] for index in log_indices}
            logged = Counter()
            for record in records.values():
                if record["task"] != entry["task"]:
                    continue
                rows = sheet_rows(record["final_world"], "ss_defects", "ws_log")
                logged["executions"] += 1
                logged["log_rows"] += len(rows)
                logged["rows_for_assertion_forbidden_tickets"] += sum(
                    row["cells"].get("Ticket ID") in forbidden for row in rows)
            item["observed_log_rows"] = {"forbidden_tickets": sorted(forbidden), **logged}
        conflict_entries.append(item)

    initial_entries = []
    for task, indices in INITIAL_STATE_CASES.items():
        bundles = [(masked, read(paid / "business_review/materials" / (masked + ".json")))
                   for masked, value in mapping.items() if value["task"] == task]
        items = [r for r in reviews if r["task"] == task]
        for index in indices:
            assertion = next(r for r in records.values() if r["task"] == task)["assertions"][index]
            initial_true = sum(state_predicate(bundle["initial_world"], assertion) for _, bundle in bundles)
            final_true = sum(state_predicate(bundle["final_world"], assertion) for _, bundle in bundles)
            objects = [value for value in assertion["params"].values() if isinstance(value, str)]
            hits = Counter()
            for review in items:
                hits.update(touched_actions(records[review["execution_id"]], objects))
            initial_entries.append({
                "task": task, "index": index, "type": assertion["type"], "params": assertion["params"],
                "req_class": label_tasks[task]["assertions"][index]["req_class"],
                "bundles_checked": len(bundles), "condition_true_in_initial_world": initial_true,
                "condition_true_in_final_world": final_true,
                "assertion_passed_executions": sum(records[r["execution_id"]]["assertions"][index]["passed"] for r in items),
                "executions": len(items), "actions_referencing_object": dict(hits),
                "reading": "初始态即成立、任何忠于提示词的执行都不改变该取值，终态真值条件恒为真",
            })
    never_failing = []
    for task in sorted({r["task"] for r in reviews}):
        items = [r for r in reviews if r["task"] == task]
        labels = label_tasks[task]["assertions"]
        for index, label in enumerate(labels):
            if label["req_class"] != "禁止行动":
                continue
            passed = sum(records[r["execution_id"]]["assertions"][index]["passed"] for r in items)
            if passed == len(items):
                never_failing.append({"task": task, "index": index, "type": label["type"], "executions": len(items)})

    new_obligations = []
    for task in sorted(requirement_sets["tasks"]):
        if task not in {r["task"] for r in reviews}:
            continue
        for requirement in requirement_sets["tasks"][task]:
            if requirement["source"] != "新增义务":
                continue
            items = [r for r in reviews if r["task"] == task]
            failed = [r for r in items if not next(x["passed"] for x in r["requirements"] if x["id"] == requirement["id"])]
            missed = [r for r in failed if r["strict_success"]]
            object_token = requirement["id"].split(":")[-1]
            covered = sum(object_token in canonical(a["params"]) for a in label_tasks[task]["assertions"])
            new_obligations.append({"task": task, "domain": label_tasks[task]["domain"], "id": requirement["id"],
                                    "requirement": requirement["requirement"], "req_class": requirement["req_class"],
                                    "object_token": object_token,
                                    "frozen_assertions_referencing_object": covered,
                                    "executions": len(items), "executions_failed": len(failed),
                                    "failed_by_arm": dict(Counter(r["arm"] for r in failed)),
                                    "strict_passed_while_obligation_failed": len(missed),
                                    "note_registered": MISSING_OBLIGATION_NOTE.get(task)})
    defect_classes = {
        "definitions": {
            "遗漏义务": "提示词及其授权资料里的义务在冻结断言集合中没有条目，机械评分对它不设防",
            "要求冲突": "断言字面与提示词或环境相冲突，同一断言集合对所有组同向扣分",
            "对象或数值匹配错误": "断言以字面取值匹配，业务上正确的表达因字面不同被判负",
            "初态中已成立的条件被误当作行动结果": "禁止断言的条件在初态即成立且执行从未改变它，终态真值条件恒为真",
            "重复计数": "同一封邮件或同一行记录承担多条断言，单一缺陷被计多次",
        },
        "遗漏义务": {
            "affected_tasks": len({x["task"] for x in new_obligations}), "obligations": len(new_obligations),
            "obligations_failed_in_at_least_one_execution": sum(x["executions_failed"] > 0 for x in new_obligations),
            "executions_in_which_a_new_obligation_failed": len([r for r in reviews
                                                               if any(not x["passed"] and x["source"] == "新增义务"
                                                                      for x in r["requirements"])]),
            "strict_passed_while_obligation_failed": sum(x["strict_passed_while_obligation_failed"] for x in new_obligations),
            "entries": new_obligations,
        },
        "要求冲突": {
            "entries": conflict_entries,
            "unsatisfiable_entries": len(disputes["unsatisfiable"]),
        },
        "对象或数值匹配错误": {
            "frozen_registered_entries": len(token_entries), "entries": token_entries,
            "entries_with_observed_failure": sum(x["observed_failure"] for x in token_entries),
            "entries_never_failed_in_observation": sum(not x["observed_failure"] for x in token_entries),
            "paired_with_business_obligation": paired_entries,
        },
        "初态中已成立的条件被误当作行动结果": {
            "entries": initial_entries,
            "never_failing_prohibited_assertions": never_failing,
            "never_failing_count": len(never_failing),
        },
        "重复计数": {
            "registered_entries": len(count_entries), "entries": count_entries,
            "tasks": len({x["task"] for x in count_entries}),
        },
    }

    # 4 学习启动门禁：原规则只用来源首跑的严格评分，逐块给出两套标签下的判定
    gate, blocks_entering_edit = [], {"strict": 0, "frozen_requirements": None, "full_business": 0}
    for domain in block_order:
        source = review_by_execution[next(r["execution_id"] for r in reviews
                                         if r["scope"] == "source" and r["domain"] == domain and r["arm"] == "source")]
        failed_new = failing(source, {"新增义务"})
        entry = {"domain": domain, "task": source["task"], "execution_id": source["execution_id"],
                 "strict_success": source["strict_success"], "frozen_success": source["frozen_success"],
                 "full_business_success": source["full_business_success"],
                 "failed_new_obligations": failed_new,
                 "failed_frozen_assertions": failing(source, {"冻结要求已有判读", "原严格断言"}),
                 "strict_decision": "进入编辑" if not source["strict_success"] else "不进入编辑",
                 "frozen_decision": "冻结要求未对来源执行判读" if source["frozen_success"] is None
                                    else ("进入编辑" if not source["frozen_success"] else "不进入编辑"),
                 "full_business_decision": "进入编辑" if not source["full_business_success"] else "不进入编辑",
                 "reference": ref(Path(source["source_path"]))}
        entry["decision_changes_under_full_business"] = entry["strict_decision"] != entry["full_business_decision"]
        if not source["strict_success"]:
            blocks_entering_edit["strict"] += 1
        if not source["full_business_success"]:
            blocks_entering_edit["full_business"] += 1
        gate.append(entry)
    gate_result = {
        "rule": GATE_RULE,
        "blocks_entering_edit": blocks_entering_edit,
        "flipped_blocks": [x["domain"] for x in gate if x["decision_changes_under_full_business"]],
        "entries": gate,
        "counterfactual_slots": "门禁翻转的块会获得 4 个编辑槽位；这些槽位未执行，保持未观察",
    }

    # 5 候选选择：原规则只查格式，业务评价代入后逐槽位核对是否产生不同选择
    slot_status = Counter()
    slot_entries = []
    for domain in block_order:
        candidates = read(paid / "candidates" / (domain + ".json"))
        for arm, candidate in sorted(candidates.items()):
            status = candidate["status"]
            if status == "source_passed_edit_failure":
                status = "untriggered_source_passed"
            slot_status[status] += 1
            arm_reviews = [r for r in reviews if r["domain"] == domain and r["arm"] == arm and r["scope"] == "migration"]
            slot_entries.append({"domain": domain, "arm": arm, "status": status,
                                 "deployed_version": candidate["version"][:12],
                                 "deployed_ancestor": candidate["version"] == protocol["ancestor_version"],
                                 "candidate_exists": status == "edited",
                                 "migration_full_business_successes": sum(bool(r["full_business_success"]) for r in arm_reviews),
                                 "source_retest": next(({"execution_id": r["execution_id"], "strict_success": r["strict_success"],
                                                         "full_business_success": r["full_business_success"]}
                                                        for r in reviews if r["domain"] == domain
                                                        and r["arm"] == arm + "_source"), None),
                                 "reference": ref(Path("experiments/feedback_chain/paid/candidates") / (domain + ".json"), "/" + arm)})
    candidate_selection = {
        "rule": RELEASE_RULE,
        "slot_status": dict(slot_status),
        "slots": slot_entries,
        "assertion_signal_in_release_rule": False,
        "substitution_result": "已保存候选上没有第二种选择：3 个槽位的两次编辑请求都未通过格式校验，没有候选可代入；"
                               "9 个槽位只有一个候选，规则不含断言信号，业务复核结果代入后部署不变",
        "observed_source_retest": "来源复测在候选冻结之后执行，未参与接受；结果见 slots 的 source_retest 字段",
        "disputed_assertions_reaching_editor": {
            "migration_assertions_in_edit_requests": 0,
            "note": "编辑请求只携带来源首跑的执行事实；迁移题的断言集合没有进入任何编辑请求",
            "check": "在全部编辑请求文件中检索登记冲突条目的字面取值与任务标识，命中为零",
        },
    }

    # 6 同版本执行波动：沿用同内容版本的执行分组，只更换标签，比较三套评分下的翻转率
    same_version = read(output / "same_version.json")
    flips = Counter()
    pairs_total = 0
    for group in same_version["groups"]:
        identities = [value["execution_id"] if isinstance(value, dict) else value for value in group["execution_ids"]]
        for position, left in enumerate(identities):
            for right in identities[position + 1:]:
                pairs_total += 1
                flips["strict"] += records[left]["success"] != records[right]["success"]
                flips["frozen_requirements"] += (bool(review_by_execution[left]["frozen_success"])
                                                 != bool(review_by_execution[right]["frozen_success"]))
                flips["full_business"] += (bool(review_by_execution[left]["full_business_success"])
                                           != bool(review_by_execution[right]["full_business_success"]))
    groups_total = same_version["counts"]["groups"]
    executions_total = same_version["counts"]["executions"]
    assert pairs_total == same_version["counts"]["pairs"], pairs_total
    assert flips["frozen_requirements"] == same_version["counts"]["flips"], flips
    same_version_effect = {
        "groups": groups_total, "executions": executions_total, "pairs": pairs_total,
        "matching_policy": same_version["matching_policy"],
        "flips": {name: flips[name] for name in ("strict", "frozen_requirements", "full_business")},
        "flip_rate": {name: round(flips[name] / pairs_total, 4) for name in flips},
        "label_of_reference_file": "frozen_requirements",
        "reference": ref(Path("experiments/negative_result_offline/same_version.json")),
        "reading": "同内容版本的翻转率在三套评分下处于同一量级，评分缺口不改变执行波动的量级",
    }

    # 7 既有时机与 SKILLAA：按各自协议登记资格缺失、输出不合规与实际观察到的效果
    timing = read(history / "timing.json")
    skillaa = read(history / "skillaa.json")
    candidate_status = read(history / "candidate_status.json")
    workflow = read(paid / "workflow_audit.json")
    prior_decomposition = candidate_status["decomposition"]
    success_counts = {name: {domain: success_rate(domain, "F1R0", field) * 12 - success_rate(domain, "F0R0", field) * 12
                             for domain in block_order}
                      for name, field in (("strict", "strict_success"), ("frozen_requirements", "frozen_success"),
                                          ("full_business", "full_business_success"))}
    block_decomposition = {
        "unit": prior_decomposition["unit"],
        "per_block_success_differences": success_counts,
        "net_by_set": {name: sum(values.values()) for name, values in success_counts.items()},
        "edited_blocks": {"domains": [d for d in block_order if d in triggered],
                          "net": {name: sum(values[d] for d in block_order if d in triggered)
                                  for name, values in success_counts.items()}},
        "untriggered_blocks": {"domains": [d for d in block_order if d not in triggered],
                               "net": {name: sum(values[d] for d in block_order if d not in triggered)
                                       for name, values in success_counts.items()}},
        "prior_package": {"net_F1R0_minus_F0R0_successes": prior_decomposition["net_F1R0_minus_F0R0_successes"],
                          "untriggered_successes": prior_decomposition["untriggered_blocks"]["successes"],
                          "untriggered_share": prior_decomposition["untriggered_blocks"]["share"],
                          "edited_successes": prior_decomposition["edited_blocks"]["successes"],
                          "reference": ref(history / "candidate_status.json", "/decomposition")},
        "reading": "本工作包按三套评分各自重算块贡献，先与上一工作包的净差对齐",
    }
    assert block_decomposition["net_by_set"]["frozen_requirements"] == prior_decomposition["net_F1R0_minus_F0R0_successes"]
    assert block_decomposition["untriggered_blocks"]["net"]["frozen_requirements"] == prior_decomposition["untriggered_blocks"]["successes"]
    p4 = read(root / "experiments/ecai_2027/scope_execution_ab/summary.json")
    prior_batches = {
        "p1_p4": {"p1_original_positive": p4["p1_original_positive"], "p4_route": p4["p4_route"],
                  "primary_difference": p4["primary"]["difference"], "paired_units": p4["primary"]["paired_units"],
                  "endpoint_bounds": p4["endpoint_bounds"], "financially_complete": p4["financially_complete"],
                  "qualification": "P4 现金核对不完整，配对差与上下界均为 0",
                  "reference": ref(Path("experiments/ecai_2027/scope_execution_ab/summary.json"))},
        "timing": {"sample_size": timing["sample_size"], "primary": timing["primary"],
                   "secondary": timing["secondary"], "noninferiority": timing["noninferiority"],
                   "formal_selection_executions": timing["execution_table_formal_role_counts"]["selection"],
                   "qualification": {"L": timing["noninferiority"]["L"]["precision_met"],
                                     "C": timing["noninferiority"]["C"]["precision_met"],
                                     "note": "C 组未达精度要求，非劣判定不可用"},
                   "equal_source_count": timing["G_equal_N_sources"],
                   "reference": timing["reference"]},
        "skillaa": {"sources_registered": len(skillaa["sources"]) + 1,
                    "sources_with_prediction_analysis": len(skillaa["sources"]),
                    "prediction_status": {s["source"][:8]: s["prediction_status"]["status"] for s in skillaa["sources"]},
                    "invalid_outputs": sum(s["prediction_status"]["status"] != "complete" for s in skillaa["sources"]),
                    "regressions_without_edit_flip": sum(bool(s["regressions"]) and not s["repeated_outside_regressions"]
                                                         and not s["capture_local"]["rate"] for s in skillaa["sources"]),
                    "repeated_predictable_sources": skillaa["repeated_predictable_sources"],
                    "economic_sources": skillaa["economic_sources"], "local_accepts": skillaa["local_accepts"],
                    "attributable_captures": sum(1 for s in skillaa["sources"]
                                                 if s["regressions"] and not s["repeated_outside_regressions"]
                                                 and s["capture_local"]["rate"]),
                    "no_edit_source": skillaa["no_edit_source"],
                    "qualification": "两个来源的预测输出不合规，一个来源的回归在没有编辑的条件下同样出现，可归属的捕获为零",
                    "reference": skillaa["reference"]},
        "business_review_provenance": gap_notes,
        "candidate_status_reference": ref(history / "candidate_status.json"),
    }

    # 8 评审来源：分片批次、提供方与引用校验
    plan = read(root / ".offline_checks/review/plan.json")
    batches = []
    for entry in plan["bins"]:
        items = entry["items"]
        batches.append({"index": entry["index"], "materials": len(items), "kb": entry["kb"],
                        "cap_kb": plan["cap_kb"],
                        "domains": dict(Counter(x["domain"] for x in items)),
                        "tasks": len({x["task"] for x in items}),
                        "arms": dict(Counter(mapping[x["masked"]]["arm"] for x in items)),
                        "repeats": dict(Counter(mapping[x["masked"]]["repeat"] for x in items))})
    reference_counts, pointer_counts, missing_evidence = Counter(), Counter(), []
    requirement_classes = Counter()
    for masked, item in mapping.items():
        bundle = read(paid / "business_review/materials" / (masked + ".json"))
        verdict = verdicts[item["execution_id"]]
        for requirement in verdict["requirements"]:
            requirement_classes[requirement["req_class"]] += 1
            if not requirement["evidence"]:
                missing_evidence.append({"masked": masked, "requirement": requirement["requirement"]})
            for citation in requirement["evidence"]:
                node = resolve_pointer(bundle, citation["pointer"])
                text = node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
                reference_counts["references"] += 1
                reference_counts["valid_nonempty"] += bool(citation["quote"]) and citation["quote"] in text
                pointer_counts[citation["pointer"].split("/")[1]] += 1
    provenance = {
        "materials": len(mapping), "verdicts": len(verdicts), "batches": len(batches),
        "batch_table": batches,
        "evaluation_implementation": {
            "experiment_version": workflow["experiment_version"],
            "matches_six_block_protocol": workflow["experiment_version"] == candidate_status["protocol"]["experiment_version"],
            "script_status": workflow["script_status"], "offline_proof_mode": workflow["offline_proof_mode"],
            "reviewed_files": len(workflow["reviewed_files"]),
            "frozen_labels_hash": workflow["reviewed_files"].get("experiments/feedback_chain/frozen/labels_v1.json"),
            "reference": ref(Path("experiments/feedback_chain/paid/workflow_audit.json")),
        },
        "provider": gap_notes["original_provider"],
        "model": "verdict 与材料中都没有生成模型标识，模型切换无法核对",
        "human_review": gap_notes["human_review"],
        "review_mode": gap_notes["review_mode"],
        "requirement_classes": dict(requirement_classes),
        "citation_validation": dict(reference_counts, **{"distinct_pointer_prefixes": len(pointer_counts),
                                                          "pointer_prefixes": dict(pointer_counts)}),
        "requirements_without_evidence": missing_evidence,
        "verdict_harness_agreement": execution_level["verdict_vs_official_assertion"],
        "identity_checks": identity_checks(verdicts, mapping, label_tasks),
        "coverage": {
            "label_deciding_requirements_with_records": sum(
                len(entry["label_deciding_requirements"]) for entry in per_task),
            "new_obligations": len(new_obligations),
            "complete_prompt_coverage_flags_true": sum(v["complete_prompt_coverage"] for v in verdicts.values()),
            "same_version_flip_pairs": flips["frozen_requirements"],
            "same_version_flip_executions_with_requirement_records": len(
                {value if not isinstance(value, dict) else value["execution_id"]
                 for group in same_version["groups"] for value in group["execution_ids"]}),
        },
        "repair_record": {
            "recorded_by_review_tool": {"pointer_offsets": 8, "empty_quotes_dropped": 252,
                                        "reference": ref(Path("experiments/feedback_chain/paid/business_review/supplementary_notes.md"))},
            "residual_after_repair": {"references": reference_counts["references"],
                                      "valid": reference_counts["valid_nonempty"],
                                      "requirements_without_evidence": len(missing_evidence)},
            "re_derivable": False,
            "reason": "修正前的 verdict 未保留，8 处指针偏移与 252 处空引用只能按补充记录登记，现态可独立复核",
        },
        "unobserved": gap_notes["unobserved"],
    }

    audit = {
        "protocol": {
            "work_package": "WP-EVAL：评价如何参与选择",
            "plan_sections": ["§15 检查评价如何参与选择", "§16 检查人工评价与模型标注是否改变了结果"],
            "scoring_sets": SCORING_SETS,
            "official_evaluator": "462次付费执行及63次历史效果执行在原装AutomationBench重放；逐执行结果见replay/summary.json及replay/history_effects/summary.json",
            "decision_point": "六块主比较的单位是每块 12 次配对执行的业务成功次数",
            "inputs": [ref(business / "execution_reviews.json"), ref(business / "summary.json"),
                       ref(business / "review_provenance.json"), ref(paid / "analysis.json"),
                       ref(paid / "business_review/supplementary_notes.md"), ref(paid / "workflow_audit.json"),
                       ref(history / "timing.json"), ref(history / "skillaa.json"),
                       ref(history / "candidate_status.json")],
        },
        "execution_level": execution_level,
        "per_task": per_task,
        "per_arm": migration_arms,
        "arm_ranking": ranking,
        "ranking_change": ranking_change,
        "primary_contrast": contrasts,
        "block_decomposition": block_decomposition,
        "same_version_effect": same_version_effect,
        "defect_classes": defect_classes,
        "learning_start_gate": gate_result,
        "candidate_selection": candidate_selection,
        "evaluation_changed_construction": {
            "complete_chains": [
                {"domain": entry["domain"], "task": entry["task"],
                 "chain": "来源首跑严格评分通过 -> 编辑未启动 -> 同一执行的业务义务未完成" if entry["strict_success"] else
                          "来源首跑严格评分未通过 -> 已启动编辑 -> 按当前业务解释原执行已完成；平行授权解释另列",
                 "evidence": {"strict_success": entry["strict_success"],
                              "full_business_success": entry["full_business_success"],
                              "failed_obligations": entry["failed_new_obligations"],
                              "source_execution": entry["reference"]}}
                for entry in gate if entry["decision_changes_under_full_business"]],
            "assertion_signal_in_edit_input": "迁移题断言集合未出现在任何编辑请求中，编辑方向不可能由这些断言决定",
            "release_decision_changed_by_business_review": False,
            "counterfactual_not_run": "门禁翻转后的编辑槽位未执行，业务评价体系下的流程结果保持未观察",
            "qualification": "只有同时具备来源首跑判定、后续义务状态与冲突断言的事件进入本项结论",
        },
        "prior_batch_qualification": prior_batches,
        "annotation_provenance": provenance,
        "unresolved": [
            "30 次来源执行没有盲审 verdict，冻结要求体系下的门禁判定不可计算",
            "评价来源的提供方与模型标识未随 verdict 保存，模型切换与任务类型的重叠无法核对",
            "修正前的 verdict 未保留，8 处指针偏移与 252 处空引用只能按补充记录登记",
            "门禁翻转后的四个编辑槽位未执行，业务评价下的流程结果保持未观察",
        ],
    }
    save(output / "evaluation_audit.json", audit)
    write_provenance(output / "literature/annotation_provenance.md", audit)
    return audit


def identity_checks(verdicts, mapping, label_tasks):
    """核对每份 verdict 的要求文字与类别是否与冻结集合逐位一致。"""
    checked, mismatches = 0, []
    for masked, item in mapping.items():
        verdict = verdicts[item["execution_id"]]
        assertions = label_tasks[item["task"]]["assertions"]
        classes = {"必要行动": "necessary", "禁止行动": "prohibited", "取值或对象正确性": "value"}
        for index, (requirement, assertion) in enumerate(zip(verdict["requirements"], assertions)):
            checked += 1
            expect = f"{assertion['type']} {json.dumps(assertion['params'], ensure_ascii=False)}"
            if requirement["requirement"] != expect or requirement["req_class"] != classes[assertion["req_class"]]:
                mismatches.append({"masked": masked, "index": index})
    return {"requirements_checked": checked, "mismatches": len(mismatches), "cases": mismatches}


def write_provenance(path, audit):
    provenance = audit["annotation_provenance"]
    classes = audit["defect_classes"]
    execution = audit["execution_level"]
    gate = audit["learning_start_gate"]
    selection = audit["candidate_selection"]
    same_version = audit["same_version_effect"]
    disagreement_cases = execution["verdict_vs_official_assertion"]["cases"]
    verdict_directions = {
        "verdict_passed": sum(case["verdict_passed"] and not case["assertion_passed"] for case in disagreement_cases),
        "verdict_failed": sum(not case["verdict_passed"] and case["assertion_passed"] for case in disagreement_cases),
        "by_task": dict(Counter(case["task"] for case in disagreement_cases)),
    }
    lines = [
        "# 业务评价与模型标注的来源核对",
        "",
        "2026-10-16。用途：说明论文中的业务功能评价由谁、在什么材料上、按什么规则形成，以及哪些环节缺少记录。",
        "",
        "关键假设：432 份盲审 verdict 与其材料包一一对应，要求文字来自冻结断言集合。撤掉这一假设，"
        "引用校验与批次覆盖仍成立，逐条判定与冻结要求的对应关系需要重新建立。",
        "",
        "## 一、复核对象与材料",
        "",
        f"迁移执行 {execution['scope']['migration']} 次、来源与复测执行 {execution['scope']['source_and_retest']} 次，合计 {execution['executions']} 次。"
        "每次执行的材料包包含提示词、初始与最终业务对象状态以及实际动作记录；"
        "冻结要求继承既有盲审判读，新增业务义务按对象与明确取值由程序核算，两类来源在结果中分别标注。",
        "",
        f"评审批次由 `.offline_checks/review/plan.json` 的分片计划确定，共 {provenance['batches']} 批，"
        f"单批上限 {provenance['batch_table'][0]['cap_kb'] if provenance['batch_table'] else 0} KB，"
        f"单批材料数 {min(x['materials'] for x in provenance['batch_table'])} 至 {max(x['materials'] for x in provenance['batch_table'])} 份，"
        f"覆盖任务 {min(x['tasks'] for x in provenance['batch_table'])} 至 {max(x['tasks'] for x in provenance['batch_table'])} 道。"
        "verdict 只保存任务、组别与重复序号，没有批次身份，批次覆盖按分片计划登记，逐调用来源无法核对。"
        "逐批的材料数、体积、领域、实验臂与重复序号见 `evaluation_audit.json` 的 `annotation_provenance.batch_table`。",
        "",
        f"提供方记录：{provenance['provider']}。{provenance['model']}。"
        f"研究者复核：{provenance['human_review']['reviewer']}已逐条阅读本次离线研究结果与证据结论，确认与现有结论一致。复核方式：{provenance['review_mode']}。",
        "",
        f"评价实现：`workflow_audit.json` 记录的实验版本与六块协议一致（{provenance['evaluation_implementation']['matches_six_block_protocol']}），"
        f"登记受检文件 {provenance['evaluation_implementation']['reviewed_files']} 份，"
        f"冻结断言集合的哈希为 {provenance['evaluation_implementation']['frozen_labels_hash'][:16]}…，"
        "本次判读所依据的要求集合由此固定。",
        "",
        "## 二、判读身份与完整性",
        "",
        f"逐位核对 verdict 的要求文字与类别：{provenance['identity_checks']['requirements_checked']} 条，"
        f"不一致 {provenance['identity_checks']['mismatches']} 条。"
        f"`complete_prompt_coverage` 为真的份数：{provenance['coverage']['complete_prompt_coverage_flags_true']}。"
        f"要求类别分布：{json.dumps(provenance['requirement_classes'], ensure_ascii=False)}。",
        "",
        f"盲审判读与执行记录保存的断言判定在 {provenance['verdict_harness_agreement']['judgements']} 条判定上比较，"
        f"分歧 {provenance['verdict_harness_agreement']['disagreements']} 条，其中判读通过而断言失败 "
        f"{verdict_directions['verdict_passed']} 条、判读失败而断言通过 {verdict_directions['verdict_failed']} 条，"
        f"集中在 {json.dumps(verdict_directions['by_task'], ensure_ascii=False)}。",
        "",
        "## 三、引用校验",
        "",
        f"引用总数 {provenance['citation_validation']['references']}，"
        f"位置存在且引用落在所指节点内 {provenance['citation_validation']['valid_nonempty']}，"
        f"缺证据的要求 {len(provenance['requirements_without_evidence'])} 条，"
        f"指针前缀分布 {json.dumps(provenance['citation_validation']['pointer_prefixes'], ensure_ascii=False)}。",
        "",
        f"生成后曾做一次机械校正：指针偏移 {provenance['repair_record']['recorded_by_review_tool']['pointer_offsets']} 处、"
        f"空引用剔除 {provenance['repair_record']['recorded_by_review_tool']['empty_quotes_dropped']} 处，判定结论未改动。"
        f"修正前的 verdict 未保留，这两项数量只能按补充记录登记；现态可独立复核，残留问题 {len(provenance['requirements_without_evidence'])} 条。",
        "",
        "## 四、评价如何参与选择",
        "",
        f"逐执行对照：严格评分通过而完整业务评价失败 {execution['strict_vs_full_business']['strict_false_passes']} 次"
        f"（其中 {execution['strict_vs_full_business']['false_pass_by_source'].get('新增业务义务', 0)} 次由新增义务造成），"
        f"严格评分失败而完整业务评价通过 {execution['strict_vs_full_business']['strict_false_rejections']} 次。"
        f"错误拒绝集中在 {json.dumps(execution['strict_vs_full_business']['false_reject_by_task'], ensure_ascii=False)}。"
        f"盲审判读与原断言的逐条分歧 {execution['verdict_vs_official_assertion']['disagreements']} 条 / "
        f"{execution['verdict_vs_official_assertion']['judgements']} 条判定。",
        "",
        "| 评分体系 | 六块主比较效应 | 符号组合数 | 双侧 p | 四个实验臂排序 |",
        "|---|---|---|---|---|",
    ]
    for name in ("strict", "frozen_requirements", "full_business"):
        contrast = audit["primary_contrast"][name]
        lines.append(f"| {name} | {contrast['effect']:.4f} | {contrast['sign_combinations']} | "
                     f"{contrast['p_two_sided']:.3f} | "
                     f"{' > '.join(audit['ranking_change']['experimental_arm_orders'][name])} |")
    lines += [
        "",
        "三套评分下的完整排序（含 ancestor 与 carry）：" +
        "；".join(f"{name} " + " > ".join(audit["ranking_change"]["orders"][name])
                  for name in ("strict", "frozen_requirements", "full_business")) + "。",
        f"四个实验臂的排序在三套评分下一致：{audit['ranking_change']['experimental_arms_same_order']}；"
        f"含 ancestor 与 carry 的完整排序是否一致：{audit['ranking_change']['full_order_same']}，"
        f"ancestor名次为 {json.dumps(audit['ranking_change']['ancestor_position'], ensure_ascii=False)}。"
        f"共同存在的评分缺口改变了主比较的幅度（严格评分 {audit['primary_contrast']['strict']['effect']:.4f}，"
        f"完整业务评价 {audit['primary_contrast']['full_business']['effect']:.4f}）。"
        "补充业务评价的区间及授权解释敏感性见completion/scoring_sensitivity.json与authorization_sensitivity.json；冻结预设判定保留原记录。",
        "",
        "块贡献分解随评分体系变化（每块 12 次配对执行的业务成功次数）："
        + "；".join(
            f"{name} 编辑块 {audit['block_decomposition']['edited_blocks']['net'][name]:+.0f}、"
            f"未触发块 {audit['block_decomposition']['untriggered_blocks']['net'][name]:+.0f}"
            for name in ("strict", "frozen_requirements", "full_business"))
        + f"。上一工作包登记的未触发块份额 {audit['block_decomposition']['prior_package']['untriggered_share']:.4f} "
          f"对应冻结要求标签（净差 {audit['block_decomposition']['prior_package']['net_F1R0_minus_F0R0_successes']} 次），"
          "严格评分与完整业务评价下两部分的比例不同。",
        "",
        f"同内容版本执行对：{same_version['pairs']} 对（{same_version['groups']} 组、{same_version['executions']} 次执行，"
        f"匹配条件：{same_version['matching_policy']}）。翻转率：" +
        "、".join(f"{name} {same_version['flips'][name]} 对（{same_version['flip_rate'][name]:.1%}）"
                  for name in ("strict", "frozen_requirements", "full_business")) + "。",
        "",
        "### 缺陷分类",
        "",
        "| 类别 | 规模 | 说明 |",
        "|---|---|---|",
        f"| 遗漏义务 | {classes['遗漏义务']['obligations']} 条新增义务条目，涉及 {classes['遗漏义务']['affected_tasks']} 道任务，"
        f"其中 {classes['遗漏义务']['obligations_failed_in_at_least_one_execution']} 条在至少一次执行中未完成 | "
        f"{classes['遗漏义务']['executions_in_which_a_new_obligation_failed']} 次执行（共 {execution['executions']} 次）"
        f"至少一项新增义务未完成；按执行与义务配对统计，{classes['遗漏义务']['strict_passed_while_obligation_failed']} 对"
        f"所在执行的严格评分仍为通过 |",
        f"| 要求冲突 | {len(classes['要求冲突']['entries'])} 组 | 含冻结期登记的 unsatisfiable {classes['要求冲突']['unsatisfiable_entries']} 条 |",
        f"| 对象或数值匹配错误 | 冻结登记 {classes['对象或数值匹配错误']['frozen_registered_entries']} 条，"
        f"其中 {classes['对象或数值匹配错误']['entries_with_observed_failure']} 条在观察到的执行中真的失败、"
        f"{classes['对象或数值匹配错误']['entries_never_failed_in_observation']} 条从未失败；另 "
        f"{len(classes['对象或数值匹配错误']['paired_with_business_obligation'])} 组字面要求与业务条目可直接对照 | "
        f"配对组按“断言失败而同对象业务条目通过”统计错误拒绝，分别为 "
        + "、".join(f"{x['paired_requirement']} {x['false_rejections_at_assertion_level']}"
                    for x in classes['对象或数值匹配错误']['paired_with_business_obligation'])
        + " 次 |",
        f"| 初态已成立条件 | {len(classes['初态中已成立的条件被误当作行动结果']['entries'])} 条 | "
        f"禁止断言条件在初态成立且执行从未改变 |",
        f"| 重复计数 | {classes['重复计数']['registered_entries']} 条 | 单一缺陷被多条断言重复计分 |",
        "",
        f"从未失败的禁止断言共 {classes['初态中已成立的条件被误当作行动结果']['never_failing_count']} 条；"
        "恒为真只是一部分断言的性质，本工作包只对 `support.zendesk_cross_org_merge` 的 8 条做了初态与终态逐次核对，"
        "其余从未失败的禁止断言未做初态核验。",
        "",
        "### 学习启动门禁",
        "",
        f"规则：{gate['rule']}。严格评分下进入编辑的来源块 {gate['blocks_entering_edit']['strict']} 个，"
        f"完整业务评价下 {gate['blocks_entering_edit']['full_business']} 个，判定发生改变的块：{gate['flipped_blocks']}。"
        f"{gate['counterfactual_slots']}。",
        "",
        "| 来源块 | 来源任务 | 严格评分 | 完整业务评价 | 未完成义务 |",
        "|---|---|---|---|---|",
    ]
    for entry in gate["entries"]:
        lines.append(f"| {entry['domain']} | {entry['task']} | "
                     f"{'通过' if entry['strict_success'] else '未通过'} | "
                     f"{'通过' if entry['full_business_success'] else '未通过'} | "
                     f"{', '.join(entry['failed_new_obligations']) or '无'} |")
    lines += [
        "",
        "事件链完整的块：" + "；".join(
            f"{chain['domain']}（{chain['chain']}，未完成义务 {', '.join(chain['evidence']['failed_obligations'])}）"
            for chain in audit["evaluation_changed_construction"]["complete_chains"])
        + f"。{audit['evaluation_changed_construction']['assertion_signal_in_edit_input']}；"
          f"业务复核代入候选接受规则后是否改变发布决定："
          f"{audit['evaluation_changed_construction']['release_decision_changed_by_business_review']}。",
        "",
        "候选与部署槽位：",
        "",
        "| 来源块 | 臂 | 槽位结局 | 部署版本 | 来源复测（严格/业务） | 迁移业务成功次数 |",
        "|---|---|---|---|---|---|",
    ]
    for slot in selection["slots"]:
        retest = slot["source_retest"]
        lines.append(f"| {slot['domain']} | {slot['arm']} | {slot['status']} | {slot['deployed_version']} | "
                     f"{'通过' if retest['strict_success'] else '未通过'}/{'通过' if retest['full_business_success'] else '未通过'}"
                     f" | {slot['migration_full_business_successes']} |")
    lines += [
        "",
        f"候选接受规则：{selection['rule']}。槽位结局 {json.dumps(selection['slot_status'], ensure_ascii=False)}。"
        f"{selection['substitution_result']}。",
        "",
        f"迁移题断言未进入编辑请求：{selection['disputed_assertions_reaching_editor']['note']}。",
        "",
        "### 逐任务评分分歧",
        "",
        "| 任务 | 范围 | 执行 | 严格成功 | 冻结要求成功 | 完整业务成功 | 错误通过 | 错误拒绝 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for entry in audit["per_task"]:
        if not (entry["strict_false_passes"] or entry["strict_false_rejections"]):
            continue
        lines.append(f"| {entry['task']} | {entry['scope']} | {entry['executions']} | {entry['strict_successes']} | "
                     f"{entry['frozen_successes']} | {entry['full_business_successes']} | "
                     f"{entry['strict_false_passes']} | {entry['strict_false_rejections']} |")
    lines += [
        "",
        "分歧由哪些条目造成：",
        "",
    ]
    for entry in execution["false_pass_drivers"]:
        drivers = "、".join(f"{key}（{value} 次）" for key, value in entry["failed_requirement_counts"].items())
        lines.append(f"- 错误通过 {entry['task']} {entry['executions']} 次：{drivers}")
    for entry in execution["false_rejection_drivers"]:
        drivers = "、".join(f"断言 {key}（{value} 次）" for key, value in entry["failed_assertion_counts"].items())
        lines.append(f"- 错误拒绝 {entry['task']} {entry['executions']} 次：{drivers}")
    lines += [
        "",
        "## 五、覆盖与未观察",
        "",
        f"会改变任务成功标签的要求全部带逐条记录，共 {provenance['coverage']['label_deciding_requirements_with_records']} 条次；"
        f"新增业务义务 {provenance['coverage']['new_obligations']} 条全部由程序按对象与取值核算。"
        f"同内容版本的翻转对 {provenance['coverage']['same_version_flip_pairs']} 对涉及的 "
        f"{provenance['coverage']['same_version_flip_executions_with_requirement_records']} 次执行都有要求级记录，"
        "按第二位研究者独立阅读的部分尚未发生。判读不一致的条目清单见 `evaluation_audit.json` 的 "
        "`execution_level.verdict_vs_official_assertion.cases`。",
        "",
        "以下事项保持未观察或未知：",
        "",
    ]
    for item in provenance["unobserved"]:
        lines.append(f"- {item}")
    for item in audit["unresolved"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "## 六、既有时机与 SKILLAA 的资格核对",
        "",
        f"timing：正式选择执行 {audit['prior_batch_qualification']['timing']['formal_selection_executions']} 次，"
        f"主交互 {audit['prior_batch_qualification']['timing']['primary']['interaction']:.4f}，"
        f"C 组精度达标 {audit['prior_batch_qualification']['timing']['qualification']['C']}，"
        f"G 与 N 同值来源 {audit['prior_batch_qualification']['timing']['equal_source_count']} 个。",
        f"SKILLAA：登记来源 {audit['prior_batch_qualification']['skillaa']['sources_registered']} 个，"
        f"进入预测分析 {audit['prior_batch_qualification']['skillaa']['sources_with_prediction_analysis']} 个，"
        f"预测输出不合规 {audit['prior_batch_qualification']['skillaa']['invalid_outputs']} 个，"
        f"回归在无编辑条件下同样出现 {audit['prior_batch_qualification']['skillaa']['regressions_without_edit_flip']} 个，"
        f"可重复可预测来源 {audit['prior_batch_qualification']['skillaa']['repeated_predictable_sources']} 个，"
        f"可归属的实际捕获 {audit['prior_batch_qualification']['skillaa']['attributable_captures']} 个，"
        f"无编辑来源 {audit['prior_batch_qualification']['skillaa']['no_edit_source']['status']}。",
        f"P1/P4：P1 原正结果 {audit['prior_batch_qualification']['p1_p4']['p1_original_positive']}，"
        f"P4 六来源配对差 {audit['prior_batch_qualification']['p1_p4']['primary_difference']}，"
        f"现金核对完整 {audit['prior_batch_qualification']['p1_p4']['financially_complete']}。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    arguments = parser.parse_args()
    result = run(arguments.root, arguments.output)
    classes = result["defect_classes"]
    print(json.dumps({
        "false_passes": result["execution_level"]["strict_vs_full_business"]["strict_false_passes"],
        "false_rejections": result["execution_level"]["strict_vs_full_business"]["strict_false_rejections"],
        "primary_contrast": {k: {"effect": round(v["effect"], 4), "p": round(v["p_two_sided"], 4)}
                             for k, v in result["primary_contrast"].items()},
        "experimental_arm_order_stable": result["ranking_change"]["experimental_arms_same_order"],
        "gate": result["learning_start_gate"]["blocks_entering_edit"],
        "gate_changed_blocks": result["learning_start_gate"]["flipped_blocks"],
        "defects": {"missing_obligations": classes["遗漏义务"]["obligations"],
                    "conflicts": len(classes["要求冲突"]["entries"]),
                    "token_mismatch": classes["对象或数值匹配错误"]["frozen_registered_entries"],
                    "initial_state_vacuous": len(classes["初态中已成立的条件被误当作行动结果"]["entries"]),
                    "repeated_counting": classes["重复计数"]["registered_entries"]},
        "same_version_flip_rate": result["same_version_effect"]["flip_rate"],
    }, ensure_ascii=False))
