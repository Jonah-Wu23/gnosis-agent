import argparse
import asyncio
import hashlib
import json
import random
import sqlite3
import time
from typing import Literal

import ijson
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from openai import APIConnectionError, APIStatusError

from engine.activation import components_version, reuse_evidence, validation_plan
from engine.component_host import CandidateError
CandidateError.__reduce__ = lambda self: (CandidateError, (self.candidate_id, self.component, self.message, self.method))
from engine.evolution import EditableUnit, EvolutionWorkspace, paired_selection
from engine.research_evaluator import ResearchEvaluator
from engine.research_model import ResearchModel
from engine.research_plan import ROOT, PLAN_PATH
from engine.research_upstream import starharness, verify_upstreams
from engine.run_ecai import SOURCES, execute_task, read
from engine.run_pilot import config
from engine.run_research import REPAIR_INSTRUCTIONS, evidence_view
from engine.scoped_units import (AIFormatError, Budget, BudgetExceeded, ExecutionTruncated,
                                 POLICY, VERSION_FILES, beijing_peak, experiment_version,
                                 fingerprint, require_model_access, save_json)

BASE = ROOT / "experiments/ecai_2027"
OUT = BASE / "scope_execution_ab"
HELDOUT = BASE / "heldout_new"
ABLATION = BASE / "ablation"
LEDGER = ROOT / "state/ecai_followup_ledger.sqlite"
SEED = 20261009
FX = 7.1
ALLOCATIONS = {"P4": 90.0, "P2": 22.0, "P3": 8.0, "unknown": 6.47, "recovery": 7.8}
REVIEW_FILES = list(dict.fromkeys([*VERSION_FILES, "engine/scoped_units.py", "engine/run_ecai.py",
    "engine/ecai_followup.py", "engine/ecai_followup_report.py", "engine/ecai_followup_offline.py"]))
TERMINAL = {"mature", "source_failed", "invalid_proposal", "component_failure"}
SOURCE_RULE = {"primary": "development_fixed_or_empty_failures", "shortfall": "same_domain_original_fixed_heldout_failures",
               "order": "sorted_task_names_shuffled_with_seed_20261009", "evidence": "first_complete_failure_by_repeat"}


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hooks: list[Literal["task_start", "tool_discovery", "before_call", "after_call", "context", "workflow", "operation"]]
    scope: list[str]


class StructuredUnit(EditableUnit):
    service: list[str] | None
    endpoint_tool: list[str] | None = Field(alias="endpoint/tool")
    activation_predicate: Registration
    intended_effect: str | None
    pre_activation_visibility: str | None
    scope_rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def registered_predicate(self):
        if self.activation_predicate.hooks != self.hooks or self.activation_predicate.scope != self.scope:
            raise ValueError("activation_predicate 必须直接使用单元的登记 hooks 与 scope")
        return self


class EditProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["edit"]
    reason: str
    task: None = None
    units: list[EditableUnit] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_units(self):
        if len({unit.id for unit in self.units}) != len(self.units):
            raise ValueError("整组组件 ID 禁止重复")
        return self


class StructuredEdit(EditProposal):
    units: list[StructuredUnit] = Field(min_length=1)


def implementation_files():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in REVIEW_FILES}


def immutable(path, value):
    if path.exists():
        if read(path) != value:
            raise ValueError("冻结内容发生变化：" + str(path.relative_to(ROOT)))
    else:
        save_json(path, value)


def historical_failures(old):
    empty = components_version([])
    records = {}
    for task in sorted(old["search"]):
        paths = [ROOT / "experiments/research/executions/fixed" / (fingerprint(["baseline", task]) + ".json")]
        paths.extend(BASE / "mechanism/executions" / label / (fingerprint(["ecai", label, task, repeat, empty]) + ".json")
                     for label in ("B0", "B0prime") for repeat in range(2))
        for path in paths:
            if not path.exists():
                continue
            record = read(path)
            if (record["task"] == task and record["version"] == empty and record["success"] is False
                    and record.get("completion") and record.get("assertions") is not None
                    and record.get("boundary_trace") and record.get("termination_status") != "truncated"
                    and not record.get("harness_defects")):
                records[task] = (path, record)
                break
    return records


def fixed_heldout_failure(old, domain):
    version, units = original_units("fixed")
    if units:
        raise ValueError("历史 fixed 来源证据必须来自空组件版本")
    pool = sorted(task for task in old["heldout"] if old["tasks"][task]["domain"] == domain)
    random.Random(SEED).shuffle(pool)
    for task in pool:
        for repeat in range(5):
            path = ROOT / "experiments/research/executions/fixed" / (
                fingerprint(["fixed", "heldout", version, task, repeat]) + ".json")
            if not path.exists():
                continue
            record = read(path)
            if (record["task"] == task and record["version"] == version and record["repeat"] == repeat
                    and record["learning_role"] == "heldout" and record["arm"] == "fixed"
                    and record["success"] is False and record.get("completion")
                    and record.get("assertions") is not None and record.get("boundary_trace")
                    and record.get("termination_status") == "completed" and not record.get("execution_error")
                    and not record.get("harness_defects")
                    and record["task_contract"] == old["tasks"][task]["contract"]):
                return task, path, record
    return None


def require_unstarted_followup():
    if LEDGER.exists():
        with sqlite3.connect(LEDGER.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]:
                raise ValueError("后续批次已有调用，来源修订必须登记为新批次")
    for folder in (OUT, HELDOUT, ABLATION):
        if (any((folder / "executions").glob("*/*.json")) or any((folder / "io").glob("*/request.json"))
                or any((folder / "candidates").glob("*/requests/*.json"))):
            raise ValueError("后续批次已有请求或执行，禁止更换冻结来源")


def original_units(arm):
    folder = ROOT / "experiments/research" / arm
    state = read(folder / "workspace.json")
    version = state["frozen"]
    if not version:
        raise ValueError("正式实验版本尚未冻结：" + arm)
    units = read(folder / "versions" / version / "manifest.json")["units"]
    if components_version(units) != version:
        raise ValueError("正式实验冻结版本内容发生变化")
    return version, units


def pending_p1():
    path = ROOT / "state/ecai_p1_ledger.sqlite"
    billing = read(ROOT / "state/ecai_billing_reconciliation.json")
    covered = set(next(row["covered_call_ids"] for row in billing["ledgers"]
                       if row["ledger"] == "state/ecai_p1_ledger.sqlite"))
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [{"call_id": identifier, "phase": phase, "reserved_cny": reserved * FX}
                for identifier, phase, reserved in db.execute(
                    "SELECT id,phase,reserved FROM calls WHERE cost IS NULL ORDER BY id") if identifier not in covered]


def prepare(revise_sources=False):
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values

    old = read(PLAN_PATH)
    prior = read(BASE / "mechanism/protocol.json")
    upstreams = verify_upstreams()
    if revise_sources:
        require_unstarted_followup()
    failures = historical_failures(old)
    domains = sorted({old["tasks"][task]["domain"] for task in old["search"]})
    sources = [{"task": task, "layer": "pilot"} for task in SOURCES]
    missing = []
    for domain in domains:
        if any(old["tasks"][task]["domain"] == domain for task in SOURCES):
            continue
        pool = sorted(task for task in old["search"] if old["tasks"][task]["domain"] == domain)
        random.Random(SEED).shuffle(pool)
        task = next((task for task in pool if task in failures), None)
        evidence_pool = "development"
        if task is None:
            selected = fixed_heldout_failure(old, domain)
            if selected is not None:
                task, path, record = selected
                failures[task] = (path, record)
                evidence_pool = "original_fixed_heldout"
        if task is None:
            missing.append(domain)
        else:
            sources.append({"task": task, "layer": "new", "selection_pool": evidence_pool})
    names = {source["task"] for source in sources}
    panel = []
    for domain in domains:
        pool = sorted(task for task in prior["mechanism_tasks"]
                      if task not in names and old["tasks"][task]["domain"] == domain)
        random.Random(SEED).shuffle(pool)
        panel.extend(pool[:2])
    if len(panel) != 12 or any(task not in failures for task in names):
        save_json(OUT / "selection_shortfall.json", {"missing_domains": missing,
            "missing_evidence": sorted(names - failures.keys()), "panel_size": len(panel)})
        raise ValueError("六来源或十二题面板缺额，不能冻结完整实验")

    tasks = dict(old["tasks"])
    excluded = set().union(*(set(old[key]) for key in ("search", "selection", "heldout", "checkpoint"))) | names | set(panel)
    # 学习工作区和 P1 的真实执行身份也排除，禁止把曾见过的任务重新当作留出。
    for parent in (ROOT / "experiments/research/executions", BASE / "mechanism/executions"):
        for path in parent.glob("*/*.json"):
            if not path.name.endswith(".failure.json"):
                with path.open("rb") as stream:
                    excluded.add(next(ijson.items(stream, "task")))
    heldout = []
    for domain in domains:
        rows = {json.loads(row["info"])["task_name"]: row for row in get_domain_dataset(domain)}
        pool = sorted(set(rows) - excluded)
        random.Random(SEED).shuffle(pool)
        if len(pool) < 3:
            raise ValueError("新增留出任务缺额：" + domain)
        heldout.extend(pool[:3])
        for task in pool[:3]:
            row = rows[task]
            info = strip_none_values(json.loads(row["info"]))
            tasks[task] = {"domain": domain, "contract": fingerprint(row),
                "services": compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"])}

    versions = {}
    for arm in ("fixed", "global", "gnosis"):
        version, units = original_units(arm)
        versions[arm] = {"version": version, "units": units}
    gnosis = versions["gnosis"]["units"]
    removed = [unit for unit in gnosis if unit["id"] == "ace_op_vp_guard"]
    no_e = [unit for unit in gnosis if unit["id"] != "ace_op_vp_guard"]
    if len(removed) != 1 or removed[0]["component"] != "E" or len(no_e) != 13 or any(unit["component"] for unit in no_e):
        raise ValueError("原 GNOSIS 版本不符合指定的一项 E 加十三项文本消融条件")
    versions["gnosis_no_e"] = {"version": components_version(no_e), "units": no_e}

    rng = random.Random(SEED)
    rng.shuffle(sources)
    slots = []
    for index, source in enumerate(sources):
        task = source["task"]
        path, record = failures[task]
        source.update(domain=tasks[task]["domain"], evidence_path=str(path.relative_to(ROOT)),
                      evidence_execution=record["execution_id"], evidence_fingerprint=fingerprint(evidence_view(record)),
                      evidence_role=record["learning_role"], evidence_repeat=record["repeat"])
        source_slots = [{"id": f"S{index + 1}_{interface}{repeat + 1}", "source": task,
                         "interface": interface, "repeat": repeat}
                        for interface in ("A", "B") for repeat in range(2)]
        rng.shuffle(source_slots)
        slots.extend(source_slots)
    unavailable = [{"id": f"missing_{domain}_{interface}{repeat + 1}", "source": None,
                    "domain": domain, "interface": interface, "repeat": repeat, "status": "source_shortfall"}
                   for domain in missing for interface in ("A", "B") for repeat in range(2)]
    blocks = []
    for task in heldout:
        order = [{"arm": arm, "repeat": repeat} for arm in versions for repeat in range(2)]
        rng.shuffle(order)
        blocks.append({"task": task, "order": order})
    version = experiment_version(ROOT / ".offline_checks/ecai_followup")
    result = {"schema": "gnosis.ecai.followup.v1", "seed": SEED, "sources": sources,
        "mechanism_tasks": panel, "heldout_tasks": heldout, "slots": slots, "unavailable_slots": unavailable,
        "missing_source_domains": missing, "ready": not missing, "blocks": blocks,
        "versions": versions, "tasks": tasks, "upstreams": upstreams, "original_plan": old["identity"],
        "experiment_version": version, "model_conditions": {"provider": "deepseek_official", "model": "deepseek-flash",
            "thinking": "enabled", "reasoning_effort": "high", "temperature": 1, "max_tokens": 393216,
            "max_steps": 50, "toolset": "api", "parallel_tasks": 8},
        "balance_snapshot_cny": 134.27, "allocations_cny": ALLOCATIONS,
        "source_repeats": 1, "mechanism_repeats": 1, "heldout_repeats": 2,
        "interfaces": {"A": EditProposal.model_json_schema(), "B": StructuredEdit.model_json_schema()},
        "shared_instructions": REPAIR_INSTRUCTIONS,
        "release_rule": "strict_mean_partial_tiebreak_zero_task_regressions_SAGE_separate",
        "source_selection_rule": SOURCE_RULE,
        "statistics": {"unit_p4": "source", "unit_p2_p3": "task", "bootstrap": 200000,
            "bootstrap_seed": 100, "exact_two_sided": True, "holm_comparisons": 3}}
    # 首次初始化前检查后续目录；已有执行时只接受完整的既有协议。
    path = OUT / "protocol.json"
    if not path.exists() and (any(any((folder / "executions").glob("*/*.json")) for folder in (OUT, HELDOUT, ABLATION))
                              or LEDGER.exists()):
        raise ValueError("存在后续执行或账本却缺少冻结协议，禁止初始化")
    if revise_sources:
        save_json(path, result)
    else:
        immutable(path, result)
    if missing:
        save_json(OUT / "selection_shortfall.json", {"missing_domains": missing,
            "eligible_sources": len(sources), "unavailable_proposal_slots": len(unavailable),
            "six_source_condition": False, "model_calls": 0})
    elif (OUT / "selection_shortfall.json").exists():
        (OUT / "selection_shortfall.json").unlink()
    if not (OUT / "cash_protocol.json").exists():
        liabilities = pending_p1()
        if sum(row["reserved_cny"] for row in liabilities) > ALLOCATIONS["unknown"]:
            raise ValueError("P1 未知责任超过计划预留")
        save_json(OUT / "cash_protocol.json", {"balance_cny": 134.27, "unknown_calls": liabilities,
            "assumption": "用户确认余额已包含此前扣费；后续没有外部充值或账户支出"})
    for folder in (HELDOUT, ABLATION):
        child = {"parent": fingerprint(result), "tasks": heldout,
                 "blocks": blocks, "versions": versions, "experiment_version": version}
        if revise_sources:
            save_json(folder / "protocol.json", child)
        else:
            immutable(folder / "protocol.json", child)
    for source in sources:
        record = failures[source["task"]][1]
        immutable(OUT / "evidence" / (source["task"] + ".json"), evidence_view(record))
    print(json.dumps({"status": "frozen", "sources": len(sources), "panel": len(panel),
        "proposal_slots": len(slots), "unavailable_slots": len(unavailable), "ready": not missing,
        "heldout_tasks": len(heldout), "model_calls": 0}), flush=True)
    return result


def frozen_protocol():
    protocol = read(OUT / "protocol.json")
    if experiment_version(ROOT / ".offline_checks/ecai_followup") != protocol["experiment_version"]:
        raise ValueError("后续运行条件已改变，需要冻结新协议")
    return protocol


def liabilities():
    frozen = read(OUT / "cash_protocol.json")["unknown_calls"]
    path = OUT / "liability_resolution.json"
    resolutions = read(path) if path.exists() else []
    by_id = {row["call_id"]: row for row in resolutions}
    if len(by_id) != len(resolutions) or not set(by_id) <= {row["call_id"] for row in frozen}:
        raise ValueError("未知责任核对记录的调用身份重复或超出冻结集合")
    result = []
    for row in frozen:
        resolution = by_id.get(row["call_id"])
        if resolution is None:
            result.append({**row, "status": "unknown", "future_cny": row["reserved_cny"], "released_cny": 0})
            continue
        if resolution["status"] not in ("paid_before_snapshot", "paid_after_snapshot", "not_billed"):
            raise ValueError("未知责任核对状态非法")
        evidence = ROOT / resolution["evidence_path"]
        if not evidence.is_file() or not resolution["evidence_path"]:
            raise ValueError("责任核对缺少真实结算证据")
        amount = float(resolution["cash_cny"])
        if amount < 0 or (resolution["status"] == "not_billed" and amount != 0):
            raise ValueError("责任核对费用非法")
        future = amount if resolution["status"] == "paid_after_snapshot" else 0
        result.append({**row, **resolution, "future_cny": future,
                       "released_cny": row["reserved_cny"] - future})
    return result


def cash_balance():
    import httpx
    from engine.llm_client import client

    response = client.with_options(max_retries=0, timeout=httpx.Timeout(60, connect=30)).get(
        "/user/balance", cast_to=dict[str, object])
    balances = [row for row in response["balance_infos"] if row["currency"] == "CNY"]
    if len(balances) != 1 or not response["is_available"]:
        raise BudgetExceeded("官方余额不可用")
    amount = float(balances[0]["total_balance"])
    with sqlite3.connect(LEDGER.resolve().as_uri() + "?mode=ro", uri=True) as db:
        covered = [row[0] for row in db.execute("SELECT id FROM calls ORDER BY id")]
    save_json(OUT / "latest_balance.json", {"balance_cny": amount, "at": time.time(), "model_calls": 0,
        "covered_call_ids": covered, "rounding_margin_cny": 0.01})
    return amount


class FollowupBudget(Budget):
    def __init__(self, output=OUT, path=LEDGER):
        super().__init__(path, None, rates=config()["rate_card"], output=output)

    def reserve(self, phase, prompt, max_tokens, guard=None, contingency_reason=None):
        if beijing_peak():
            raise BudgetExceeded("当前为高峰计价时段，保留断点并等待闲时")
        official = cash_balance()
        liability = sum(row["future_cny"] for row in liabilities())
        stage = phase.split("/")[0]
        if stage not in ("P4", "P2", "P3"):
            raise ValueError("未知的后续费用阶段")

        def cash_guard(db, amount, reason):
            rows = db.execute("SELECT phase,cost,billed,reserved FROM calls").fetchall()
            used = {name: 0.0 for name in ("P4", "P2", "P3")}
            pending = 0.0
            for call_phase, cost, billed, reserved in rows:
                used[call_phase.split("/")[0]] += (reserved if cost is None else billed if billed is not None else cost) * FX
                if cost is None:
                    pending += reserved * FX
            used[stage] += amount * FX
            if pending + amount * FX + sum(row["future_cny"] for row in liabilities() if row["status"] == "unknown") > official:
                raise BudgetExceeded("官方余额不足以覆盖请求和未结算责任")
            if sum(used.values()) + liability > 134.27:
                raise BudgetExceeded("剩余资金池不足，保存未完成槽位")
            overflow = sum(max(0, used[name] - ALLOCATIONS[name]) for name in used)
            recovery = ALLOCATIONS["recovery"] + ALLOCATIONS["unknown"] - liability
            if overflow > recovery:
                raise BudgetExceeded("阶段额度与浮动资金已用完")
            if guard is not None:
                guard(db, amount, reason)

        return super().reserve(phase, prompt, max_tokens, guard=cash_guard,
                               contingency_reason=contingency_reason)

    def release(self, call_id, reason):
        # 已发送却未收到 usage 的请求继续占用预留；只有可定位的未发送失败才释放。
        failure_path = self.output / "io" / call_id / "failure.json"
        if not failure_path.exists():
            raise RuntimeError("请求缺少传输失败证据，禁止释放现金预留")
        failure = read(failure_path)
        if failure["billing_unconfirmed"]:
            return False
        return super().release(call_id, reason)


class FollowupModel(ResearchModel):
    def resume(self, checkpoint, messages, options, validate=None, allow_budget_drift=False):
        saved = read(checkpoint)
        folder = self.output / "io" / saved["call"]
        if saved["status"] == "inflight" and not (folder / "response.json").exists():
            if (saved["version"], saved["messages"], saved["options"]) != (self.version, messages, options):
                raise ValueError("提议断点输入发生变化")
            failure = folder / "failure.json"
            if not failure.exists() or not read(failure)["retry_scheduled"]:
                raise RuntimeError("提议请求缺少响应或可恢复服务失败，保留责任并停止")
            self.meter.release(saved["call"], "恢复已记录的传输失败")
            return None
        return super().resume(checkpoint, messages, options, validate, allow_budget_drift)

    def request(self, messages, phase, checkpoint, validate=None, options=None, allow_budget_drift=False):
        from engine.llm_client import chat, client

        options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 1, **(options or {})}
        checkpoint = checkpoint.resolve()
        start = 0
        if checkpoint.exists():
            response = self.resume(checkpoint, messages, options, validate, allow_budget_drift)
            if response is not None:
                return response
            failure = read(self.output / "io" / read(checkpoint)["call"] / "failure.json")
            start = failure["service_attempt"] + 1
            time.sleep(max(0, failure["retry_delay_seconds"] -
                           (time.time() - (self.output / "io" / read(checkpoint)["call"] / "failure.json").stat().st_mtime)))
        require_model_access()
        client.max_retries = 0
        for attempt in range(start, POLICY["service_retries"] + 1):
            self.journal.verify_version()
            call_id = self.meter.reserve(phase, messages, options["max_tokens"])
            folder = self.journal.request(call_id, phase, messages, options)
            save_json(checkpoint, {"version": self.version, "messages": messages, "options": options,
                "status": "inflight", "call": call_id})
            try:
                response = chat(messages, **options)
            except (APIConnectionError, APIStatusError) as error:
                delay = self.journal.retry_service(folder, error, attempt)
                self.meter.release(call_id, "已记录的服务失败，按真实传输责任管理预留")
                if delay is None:
                    raise
                time.sleep(delay)
                continue
            self.finish(checkpoint, call_id, response, messages, options)
            self.checked(response, validate, folder)
            return response
        raise RuntimeError("服务重试次数已用完，保留未完成提议槽位")


def proposal_messages(protocol, slot):
    source = next(source for source in protocol["sources"] if source["task"] == slot["source"])
    evidence = read(OUT / "evidence" / (slot["source"] + ".json"))
    scope_values = sorted({"GLOBAL", *protocol["tasks"][slot["source"]]["services"],
        *(event[key] for event in evidence["boundary_trace"] for key in ("service", "endpoint", "tool") if event[key])})
    payload = {"source": slot["source"], "current_units": [], "evidence": [evidence],
        "legal_scope": scope_values, "schema": protocol["interfaces"][slot["interface"]],
        "tool_instructions": protocol["shared_instructions"]}
    instructions = ("Return a single edit proposal for the fixed source using the supplied schema. "
                    "Generate independently from an empty group. GLOBAL and local scopes are equally legal. "
                    "No additional task execution or feedback is available.")
    if slot["interface"] == "B":
        instructions += (" Fill every structured field, also for GLOBAL. Use explicit null for inapplicable items. "
            "activation_predicate contains exactly the registered hooks and scope; free prose cannot create new activation rules. "
            "The descriptive fields are stored for research and removed before runtime.")
    if fingerprint(evidence) != source["evidence_fingerprint"]:
        raise ValueError("冻结来源证据发生变化")
    return [{"role": "system", "content": protocol["shared_instructions"] + "\n" + instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def parse_proposal(text, interface, allowed):
    proposal = (StructuredEdit if interface == "B" else EditProposal).model_validate_json(text)
    if any(not set(unit.scope) <= set(allowed) for unit in proposal.units):
        raise AIFormatError("提议 scope 超出合法清单")
    return proposal


def runtime_units(proposal):
    return [{name: getattr(unit, name) for name in EditableUnit.model_fields} for unit in proposal.units]


def load_components(workspace, version):
    from engine.automation_adapter import ScopedInjector
    from engine.evolution import VersionLibrary

    injector = ScopedInjector(VersionLibrary(workspace, version), config())
    try:
        for unit in injector.units:
            if unit["component"]:
                injector.host_for(unit)
    finally:
        injector.close()


def evaluator_plan(protocol):
    return {"tasks": protocol["tasks"], "search": [source["task"] for source in protocol["sources"]],
            "selection": protocol["mechanism_tasks"], "heldout": protocol["heldout_tasks"], "checkpoint": []}


def require_review(protocol):
    proof = read(ROOT / ".offline_checks/ecai_followup/result.json")
    review = read(OUT / "static_review.json")
    files = implementation_files()
    if (proof["status"] != "PASS" or proof["protocol"] != fingerprint(protocol)
            or proof["files"] != files or review["decision"] != "PASS"
            or review["implementation"] != fingerprint(files) or review["protocol"] != fingerprint(protocol)):
        raise RuntimeError("当前代码的必要离线检查与独立静态复核必须同时 PASS")
    for folder in (OUT, HELDOUT, ABLATION):
        save_json(folder / "workflow_audit.json", {"script_status": "clear",
            "experiment_version": protocol["experiment_version"], "reviewed_files": files,
            "offline_proof_mode": "followup_real_assets", "static_review": str(OUT / "static_review.json")})


async def execution(evaluator, workspace, version, task, arm, stage, role, repeat=0, kind="execution"):
    return await execute_task(evaluator, workspace, version, task, arm, stage, role, repeat,
        execution_id=fingerprint(["ecai_followup", stage, arm, task, repeat, version]), kind=kind)


async def generate_slot(protocol, slot, evaluator, model):
    folder = OUT / "candidates" / slot["id"]
    path = folder / "candidate.json"
    if path.exists() and read(path)["status"] in TERMINAL:
        return read(path)
    workspace = EvolutionWorkspace(folder, evaluator.version)
    result = {**slot, "status": "pending", "version": None, "source_success": None,
              "legal": None, "source_execution": None}
    messages = proposal_messages(protocol, slot)
    allowed = json.loads(messages[1]["content"])["legal_scope"]
    for retry in range(2):
        try:
            response = await asyncio.to_thread(model.request, messages,
                "P4/" + slot["id"] + "/proposal", folder / "requests" / f"{retry}.json",
                validate=lambda text: parse_proposal(text, slot["interface"], allowed),
                options={"response_format": {"type": "json_object"}})
            proposal = parse_proposal(response.choices[0].message.content, slot["interface"], allowed)
            break
        except (ValidationError, AIFormatError, ExecutionTruncated) as error:
            if retry:
                result.update(status="invalid_proposal", legal=False, error_type=type(error).__name__, error=str(error))
                save_json(path, result)
                return result
            messages = [*messages, {"role": "user", "content": "Protocol validation failed: " + str(error)}]
    immutable(folder / "proposal.json", proposal.model_dump(by_alias=True))
    version = workspace.edit(runtime_units(proposal))
    result.update(version=version, legal=True, scopes=[unit.scope for unit in proposal.units])
    try:
        await asyncio.to_thread(load_components, workspace, version)
        record = await execution(evaluator, workspace, version, slot["source"], slot["id"], "P4", "learning")
    except CandidateError as error:
        result.update(status="component_failure", error_type=type(error).__name__, error=str(error))
        save_json(path, result)
        return result
    result.update(status="mature" if record["success"] else "source_failed",
                  source_success=record["success"], source_execution=record["execution_id"])
    if record["success"]:
        workspace.state["frozen"] = version
        workspace.save()
    save_json(path, result)
    return result


def release_decision(before, after, tasks, repeats=1):
    sage = paired_selection(before, after, tasks, repeats)
    _, decide, _ = starharness()
    a = {task: sum(row["success"] for row in before if row["task"] == task) / repeats for task in tasks}
    b = {task: sum(row["success"] for row in after if row["task"] == task) / repeats for task in tasks}
    partial_a = sum(row["partial_credit"] for row in before) / len(before)
    partial_b = sum(row["partial_credit"] for row in after) / len(after)
    mean_a, mean_b = sum(a.values()) / len(tasks), sum(b.values()) / len(tasks)
    accepted, reason = decide(mean_b, b, partial_b, {"mean": mean_a, "per_scenario": a, "verifier_rate": partial_a})
    return {"accepted": bool(accepted and sage["losses"] == 0), "reason": reason,
        "mean_before": mean_a, "mean_after": mean_b, "partial_before": partial_a,
        "partial_after": partial_b, "wins": sage["wins"], "losses": sage["losses"], "sage": sage}


async def p4(protocol):
    evaluator = ResearchEvaluator(evaluator_plan(protocol), FollowupBudget())
    model = FollowupModel(evaluator.meter, evaluator.version)
    baseline = EvolutionWorkspace(OUT / "B0", evaluator.version)
    empty = baseline.state["working"]
    # 三个新增来源仅取得同期空组件结果，修复器始终使用预先冻结的历史证据。
    for source in protocol["sources"]:
        if source["layer"] == "new":
            await execution(evaluator, baseline, empty, source["task"], "source_B0", "P4", "learning")
    candidates = []
    for slot in protocol["slots"]:
        candidate = await generate_slot(protocol, slot, evaluator, model)
        candidates.append(candidate)
        print(json.dumps({"slot": slot["id"], "status": candidate["status"]}), flush=True)
    immutable(OUT / "candidates_frozen.json", candidates)
    tasks = protocol["mechanism_tasks"]
    baselines = {}
    for label in ("B0", "B0prime"):
        baselines[label] = [await execution(evaluator, baseline, empty, task, label, "P4", "selection") for task in tasks]
    immutable(OUT / "baseline.json", baselines)
    base = baselines["B0"]
    # 选择计划在读取任何候选机制成绩前保存，所有候选采用同一 B0。
    for candidate in candidates:
        if candidate["status"] == "mature":
            workspace = EvolutionWorkspace(OUT / "candidates" / candidate["id"], evaluator.version)
            immutable(workspace.root / "validation_plan.json",
                validation_plan([], workspace.load_version(candidate["version"]), base, evaluator.version))
    for source in protocol["sources"]:
        current = [candidate for candidate in candidates if candidate["source"] == source["task"] and candidate["status"] == "mature"]
        # 每题先按冻结 A/B 顺序执行受影响记录；选择判断保存后再审计复用部分。
        for affected in (True, False):
            for task in tasks:
                for candidate in current:
                    workspace = EvolutionWorkspace(OUT / "candidates" / candidate["id"], evaluator.version)
                    item = next(item for item in read(workspace.root / "validation_plan.json") if item["task"] == task)
                    if item["affected"] == affected:
                        await execution(evaluator, workspace, candidate["version"], task, candidate["id"], "P4", "selection")
            for candidate in current:
                workspace = EvolutionWorkspace(OUT / "candidates" / candidate["id"], evaluator.version)
                plan = read(workspace.root / "validation_plan.json")
                units = workspace.load_version(candidate["version"])
                records = []
                for original, item in zip(base, plan, strict=True):
                    if affected and not item["affected"]:
                        records.append(reuse_evidence(original, units, item))
                    else:
                        records.append(await execution(evaluator, workspace, candidate["version"], original["task"],
                                                       candidate["id"], "P4", "selection"))
                name = "selective_decision.json" if affected else "full_decision.json"
                immutable(workspace.root / name, release_decision(base, records, tasks))
    save_json(OUT / "execution_complete.json", {"completed": True, "protocol": fingerprint(protocol)})


async def p2_p3(protocol):
    if not (OUT / "execution_complete.json").exists():
        raise RuntimeError("P4 完整审计尚未完成，P2/P3 不能启动")
    evaluators, workspaces = {}, {}
    for arm, saved in protocol["versions"].items():
        output = ABLATION if arm == "gnosis_no_e" else HELDOUT
        evaluator = ResearchEvaluator(evaluator_plan(protocol), FollowupBudget(output))
        workspace = EvolutionWorkspace(output / arm, evaluator.version)
        version = workspace.write_version(saved["units"])
        if version != saved["version"]:
            raise ValueError("新增评价未保持原冻结组件版本")
        workspace.state.update(working=version, released=version, frozen=version)
        workspace.save()
        evaluators[arm], workspaces[arm] = evaluator, workspace
    for block in protocol["blocks"]:
        records = []
        for slot in block["order"]:
            arm = slot["arm"]
            record = await execution(evaluators[arm], workspaces[arm], protocol["versions"][arm]["version"],
                block["task"], arm, "P3" if arm == "gnosis_no_e" else "P2", "heldout", slot["repeat"])
            records.append({"arm": arm, "repeat": slot["repeat"], "execution_id": record["execution_id"]})
        immutable(HELDOUT / "blocks" / (block["task"] + ".json"), {"task": block["task"], "records": records})
        print(json.dumps({"block": block["task"], "completed": len(records)}), flush=True)
    save_json(HELDOUT / "execution_complete.json", {"completed": True, "protocol": fingerprint(protocol)})
    save_json(ABLATION / "execution_complete.json", {"completed": True, "protocol": fingerprint(protocol)})


async def run(command):
    protocol = frozen_protocol()
    require_review(protocol)
    if not protocol["ready"]:
        raise ValueError("预定六来源存在缺额，正式实验停止：" + ",".join(protocol["missing_source_domains"]))
    require_model_access()
    load_dotenv(ROOT / ".env")
    from engine.llm_client import BASE_URL, MODEL
    if BASE_URL.rstrip("/") != "https://api.deepseek.com" or MODEL != "deepseek-flash":
        raise ValueError("模型服务与冻结条件不一致")
    try:
        if command in ("p4", "followup"):
            await p4(protocol)
        if command in ("p2_p3", "followup"):
            await p2_p3(protocol)
    except Exception as error:
        save_json(OUT / "interruption.json", {"command": command, "error_type": type(error).__name__,
            "status": "budget_stopped" if isinstance(error, BudgetExceeded) else "interrupted",
            "at": time.time()})
        raise
    finally:
        from engine import run_ecai
        if run_ecai.TASK_POOL is not None:
            run_ecai.TASK_POOL.shutdown(wait=True, cancel_futures=True)
            run_ecai.TASK_POOL = None
    from engine.ecai_followup_report import report
    cash_balance()
    report()


def main(command=None):
    if command is None:
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=["prepare_followup", "revise_sources", "offline_followup", "p4", "p2_p3", "followup", "followup_report"])
        command = parser.parse_args().command
    if command == "prepare_followup":
        prepare()
    elif command == "revise_sources":
        prepare(revise_sources=True)
    elif command == "offline_followup":
        from engine.ecai_followup_offline import check
        check()
    elif command == "followup_report":
        from engine.ecai_followup_report import report
        report()
    else:
        asyncio.run(run(command))


if __name__ == "__main__":
    main()
