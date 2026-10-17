import argparse
import asyncio
import hashlib
import itertools
import json
import os
import random
import sqlite3
import subprocess
from pathlib import Path
from typing import Literal

import ijson
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from engine.activation import KnowledgeTreatment, TaskBoundary, components_version, knowledge_validation_tasks
from engine.evolution import EvolutionWorkspace
from engine.research_evaluator import ResearchEvaluator
from engine.research_model import ResearchModel
from engine.research_plan import ROOT, PLAN_PATH
from engine.research_upstream import verify_upstreams
from engine.run_pilot import config
from engine.scoped_units import (AIFormatError, Budget, POLICY, VERSION_FILES, experiment_version,
                                 fingerprint, require_model_access, save_json)

OUT = ROOT / "experiments" / os.environ.get("GNOSIS_TIMING_RUN", "timing_v4")
LEDGER_PATH = ROOT / "state" / (OUT.name + ".sqlite")
CHECK = ROOT / ".offline_checks/timing"
SKILLAA = ROOT / "scratch/SkillAA"
SKILLAA_COMMIT = "8d99c827152284c902dedb8037f710c85f7ba51e"
SEED = 20261010
ARMS = ("G", "L", "D", "C", "N")
FILES = list(dict.fromkeys([*VERSION_FILES, "engine/timing_research.py", "engine/timing_analysis.py",
                           "engine/timing_offline.py", "engine/skillaa_audit.py",
                           "engine/automation_requirements.txt"]))


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def freeze(path, value):
    if path.exists():
        if read(path) != value:
            raise ValueError("冻结内容发生变化：" + str(path))
    else:
        save_json(path, value)
    return value


def implementation():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


def upstream_skillaa():
    commit = subprocess.check_output(["git", "-C", str(SKILLAA), "rev-parse", "HEAD"], text=True).strip()
    if commit != SKILLAA_COMMIT:
        raise ValueError("SKILLAA 提交与冻结版本不一致")
    changes = subprocess.check_output(["git", "-C", str(SKILLAA), "diff", "HEAD", "--", "graphopt", "scripts"], text=True)
    if changes:
        raise ValueError("SKILLAA 官方执行与门禁代码发生变化")
    return commit


def require_ready():
    files = implementation()
    proof, review = read(CHECK / "result.json"), read(CHECK / "static_review.json")
    if (proof["decision"] != "PASS" or review["decision"] != "PASS"
            or proof["files"] != files or review["files"] != files
            or proof["inventory"] != fingerprint(read(OUT / "inventory.json"))
            or review["inventory"] != proof["inventory"]):
        raise RuntimeError("必要离线检查与独立只读审查必须同时 PASS")
    upstream_skillaa()
    freeze(OUT / "implementation.json", files)
    save_json(OUT / "workflow_audit.json", {"script_status": "clear",
        "experiment_version": experiment_version(OUT), "reviewed_files": files,
        "offline_proof_mode": "real_assets_no_model", "static_review": str(CHECK / "static_review.json")})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceSlot(StrictModel):
    source: str
    alternates: list[str] = Field(min_length=0, max_length=12)
    category: Literal["planning", "execution"]
    related: list[str] = Field(min_length=2, max_length=2)
    outside: str
    rationale: str = Field(min_length=1)


class Design(StrictModel):
    router_instructions: str = Field(min_length=1)
    predictor_instructions: str = Field(min_length=1)
    prediction_limit: int = Field(ge=1, le=3)
    sources: list[SourceSlot] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def independent(self):
        if sum(slot.category == "planning" for slot in self.sources) != 4:
            raise ValueError("来源预判必须为四个 planning 与四个 execution")
        names = [task for slot in self.sources for task in [slot.source, *slot.alternates, *slot.related, slot.outside]]
        if len(set(names)) != len(names):
            raise ValueError("确认来源与迁移任务的模板必须独立")
        return self


class Repair(StrictModel):
    useful: bool
    body: str = Field(min_length=1)
    contract_end: int = Field(ge=0)
    category: Literal["planning", "execution"]
    rationale: str
    delay: TaskBoundary
    deadline: TaskBoundary | None

    @model_validator(mode="after")
    def split(self):
        if self.category == "planning":
            if not 0 < self.contract_end < len(self.body):
                raise ValueError("规划依赖修复的契约与剩余正文必须都是原正文的非空连续部分")
        else:
            if not 0 <= self.contract_end <= len(self.body):
                raise ValueError("纯执行细节修复允许 contract_end=0（无规划契约）或原正文的连续切分点")
        return self


class TaskRoute(StrictModel):
    selected: bool
    knowledge_need: str
    delay: TaskBoundary
    deadline: TaskBoundary | None
    deadline_certainty: Literal["clear", "uncertain", "absent"]

    @model_validator(mode="after")
    def deadline_consistency(self):
        if self.deadline_certainty == "absent" and self.deadline is not None:
            raise ValueError("不存在截止点时 deadline 必须为 null")
        if self.deadline_certainty == "clear" and self.deadline is None:
            raise ValueError("明确的截止点必须提供具体 TaskBoundary")
        return self


class Prediction(StrictModel):
    knowledge_need: str
    latest_boundary: dict[str, TaskBoundary | None]
    tasks: list[str]

    @model_validator(mode="after")
    def validate_prediction(self):
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("预测任务列表 tasks 不能包含重复项")
        return self


class ProcessReview(StrictModel):
    deadline_certainty: Literal["clear", "uncertain", "absent"]
    critical_action_indices: list[int]
    recovery_action_indices: list[int]
    constraint_damaged: bool
    rationale: str


def task_description(row):
    from engine.automation_adapter import restore_messages
    return "\n".join(message.content for message in restore_messages(row["prompt"]) if message.role == "user")


def compact_record(path):
    keys = {"task", "domain", "success", "partial_credit", "assertions", "version", "execution_id",
            "prompt", "completion", "boundary_trace", "actual_tool_actions", "termination_status"}
    with path.open("rb") as stream:
        return {key: value for key, value in ijson.kvitems(stream, "", use_float=True) if key in keys}


def discovery_assets():
    base = ROOT / "experiments/ecai_2027"
    candidates = []
    for path in sorted((base / "mechanism").glob("C*/candidate.json")):
        record = read(path)
        if record["status"] == "mature":
            candidates.append((record["source"], path.parent / "versions" / record["version"] / "manifest.json"))
    for candidate in read(base / "scope_execution_ab/candidates_frozen.json"):
        if candidate["status"] == "mature":
            candidates.append((candidate["source"], base / "scope_execution_ab/candidates" / candidate["id"]
                               / "versions" / candidate["version"] / "manifest.json"))
    result = []
    for source, path in candidates:
        units = read(path)["units"]
        texts = [unit["content"] for unit in units if unit["component"] != "E" and unit["content"]]
        if texts:
            result.append({"source": source, "manifest": str(path.relative_to(ROOT)),
                           "body": "\n".join(texts), "units": units})
    return result


def prepare():
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values

    upstreams = verify_upstreams()
    upstreams["SkillAA"] = upstream_skillaa()
    old = read(PLAN_PATH)
    excluded = set(old["tasks"])
    base = ROOT / "experiments/ecai_2027"
    for path in (base / "scope_execution_ab/protocol.json", base / "heldout_new/protocol.json"):
        protocol = read(path)
        excluded.update(protocol["tasks"])
    pilot = read(ROOT / "config/pilot_selection.json")
    excluded.update(task for tasks in pilot["domains"].values() for task in tasks)
    for arch in (ROOT / "archive").glob("*/design.json"):
        archived_design = read(arch)
        for slot in archived_design.get("sources", []):
            for t in [slot.get("source"), *slot.get("alternates", [])]:
                if t:
                    excluded.add(t)
    tasks, templates, excluded_templates = {}, {}, set()
    for domain in pilot["domains"]:
        for row in get_domain_dataset(domain):
            info = strip_none_values(json.loads(row["info"]))
            name = info["task_name"]
            # 原装基准以 task_name 登记模板；显式模板 ID 会把参数变体合并。
            template = info.get("template_id", name)
            templates[name] = template
            if name in excluded:
                excluded_templates.add(template)
            tasks[name] = {"domain": domain, "contract": fingerprint(row), "template": template,
                "services": compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"]),
                "description": task_description(row)}
    available = sorted(name for name in tasks if templates[name] not in excluded_templates)
    random.Random(SEED).shuffle(available)
    result = {"schema": "gnosis.timing.inventory.v1", "seed": SEED, "upstreams": upstreams,
              "excluded_tasks": sorted(excluded), "excluded_templates": sorted(excluded_templates),
              "order": available, "tasks": {name: tasks[name] for name in available},
              "discovery": discovery_assets(),
              "analysis": {"alpha": 0.05, "primary": "source_category_L_minus_D_interaction",
                  "secondary": ["G-L", "C-L", "L-N"], "noninferiority_loss": 0.1,
                  "required_ci_halfwidth": 0.1, "repeats": 2, "bootstrap_samples": 10000},
              "model": {"provider": "https://api.deepseek.com", "model": "deepseek-flash",
                  "reasoning_effort": "high", "thinking": "enabled", "temperature": 1,
                  "max_tokens": 393216}, "execution": {"toolset": "api", "max_steps": 50}}
    return freeze(OUT / "inventory.json", result)


def meter_model():
    load_dotenv(ROOT / ".env")
    if os.environ["MODER_PORVIDER"].rstrip("/") not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        raise ValueError("时机实验必须使用 DeepSeek 官方服务")
    if os.environ["MODER_NAME"] != "deepseek-flash":
        raise ValueError("时机实验模型与冻结条件不一致")
    meter = Budget(LEDGER_PATH, None, rates=config()["rate_card"], output=OUT)
    return meter, TimingModel(meter, experiment_version(OUT))


class TimingModel(ResearchModel):
    def resume(self, checkpoint, messages, options, validate=None, allow_budget_drift=False):
        saved = read(checkpoint)
        if saved["status"] == "inflight" and not (self.output / "io" / saved["call"] / "response.json").exists():
            if (saved["version"], saved["messages"], saved["options"]) != (self.version, messages, options):
                raise ValueError("未结算请求的断点输入发生变化")
            if os.environ.get("GNOSIS_RESUME_ONLY") == "1":
                raise RuntimeError("严格续跑缺少真实响应")
            # 未知扣费继续保留责任，后续请求使用独立调用 ID。
            return None
        return super().resume(checkpoint, messages, options, validate, allow_budget_drift)


def finances():
    from engine.ecai_followup import liabilities
    from engine.timing_analysis import ledger_rows, expense
    historical = liabilities()
    current = expense(ledger_rows())
    unknown = sum(row["future_cny"] for row in historical)
    diagnostic_cny = 10.3763888970953 + 11.939717860455099 + 11.7275659511809
    result = {"balance_reported_in_plan_cny": 91.34, "historical_liabilities": historical,
        "historical_unknown_cny": unknown, "diagnostic_batch_cny": diagnostic_cny, "new_calls": current,
        "available_after_known_responsibility_cny": 91.34 - unknown - diagnostic_cny - current["billed_cny"] - current["unknown_reserved_cny"],
        "settled_historical_charges_deducted_again": False}
    save_json(OUT / "financial_state.json", result)
    return result


def ask(model, instructions, payload, schema, phase, validate_result=None):
    messages = [{"role": "system", "content": instructions + "\nReturn JSON matching this schema:\n"
                + json.dumps(schema.model_json_schema())},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    def validate(text):
        try:
            value = schema.model_validate_json(text)
            if validate_result is not None:
                validate_result(value)
        except (ValidationError, ValueError) as error:
            raise AIFormatError(str(error)) from error
    for attempt in range(POLICY["format_retries"] + 1):
        path = OUT / "requests" / phase / (str(attempt) + ".json")
        try:
            response = model.request(messages, phase, path, validate=validate)
            return schema.model_validate_json(response.choices[0].message.content)
        except AIFormatError as error:
            if attempt == POLICY["format_retries"]:
                raise
            messages = [*messages, {"role": "user", "content": str(error)}]


def develop():
    require_ready()
    inventory = read(OUT / "inventory.json")
    _, model = meter_model()
    design = ask(model,
        "Develop the frozen router and predictor on these real discovery repairs. Identify knowledge needed before "
        "critical decisions versus execution details. Select eight independent new source templates (four planning, "
        "four execution), two related transfer templates and one outside template for each source from the frozen available order. "
        "Select the earliest suitable templates in that order. For planning slots, choose sources whose failure "
        "hinges on early policy, eligibility, threshold, or roster authority (e.g. HR policies, sales qualification rules, or marketing campaign criteria). "
        "For execution slots, choose sources whose failure hinges on mechanical multi-system syncing, IDs, formatting, or verbatim tokens "
        "(e.g. Operations inventory/sync or Finance reconciliation/mechanics). No transfer outcomes exist or may be used. "
        "For each source freeze up to ten same-family alternate source templates in available order, used only "
        "when the preceding source baseline succeeds or yields no useful repair. Alternates must be related to "
        "the same transfer templates and globally distinct. Specify reusable router and predictor instructions "
        "and a prediction size cap based only on discovery.",
        {"discovery": inventory["discovery"], "tasks": inventory["tasks"], "order": inventory["order"]},
        Design, "design")
    names = [task for slot in design.sources for task in [slot.source, *slot.alternates, *slot.related, slot.outside]]
    if not set(names) <= inventory["tasks"].keys():
        raise ValueError("设计选取了冻结来源池之外的任务")
    if len({inventory["tasks"][name]["template"] for name in names}) != len(names):
        raise ValueError("设计混用了同一模板的参数变体")
    freeze(OUT / "design.json", design.model_dump(mode="json"))
    return design


def evaluator_plan(inventory, design):
    return {"tasks": inventory["tasks"], "search": [task for slot in design.sources for task in [slot.source, *slot.alternates]],
            "selection": [task for slot in design.sources for task in [*slot.related, slot.outside]],
            "heldout": [], "checkpoint": []}


async def execute(evaluator, workspace, task, arm, role, repeat=0, treatment=None, identity=None):
    version = components_version([])
    execution_id = fingerprint(["timing.v1", arm, task, repeat, treatment, identity])
    return await evaluator.execute(workspace, version, task, arm, bucket="timing", role=role,
        repeat=repeat, execution_id=execution_id, knowledge_treatment=treatment)


def treatment(repair, route, source, task, arm, route_version):
    return KnowledgeTreatment(source=source, task=task, treatment=arm, selected=route.selected,
        route_version=route_version, contract=repair.body[:repair.contract_end],
        remainder=repair.body[repair.contract_end:], delay=route.delay).model_dump(mode="json")


def verify_requests(record):
    value = KnowledgeTreatment.model_validate(record["knowledge_treatment"])
    for request in record["knowledge_requests"]:
        wire = record["wire_trace"][request["request_index"]]
        trace = record["boundary_trace"][:request["boundary_index"] + 1]
        text, parts = value.text_at(trace)
        if wire["call"] != request["call"] or request["parts"] != parts:
            raise ValueError("知识提供记录与实际模型请求不一致")
        if text and wire["request"][0] != {"role": "system", "content": text}:
            raise ValueError("实际模型请求中的知识正文或消息角色发生变化")
    if not record["knowledge_requests"]:
        raise ValueError("任务缺少实际请求边界记录")


def require_preflight():
    proof = read(OUT / "preflight.json")
    if proof["decision"] != "PASS" or proof["files"] != implementation():
        raise RuntimeError("五处理真实请求与 SKILLAA 单题接入检查尚未通过")


async def preflight():
    require_ready()
    inventory = read(OUT / "inventory.json")
    asset = inventory["discovery"][0]
    source = asset["source"]
    old = read(PLAN_PATH)
    plan = {"tasks": {source: old["tasks"][source]}, "search": [source], "selection": [], "heldout": [], "checkpoint": []}
    candidate_path = next(path for path in (ROOT / "experiments/ecai_2027/mechanism").glob("C*/candidate.json")
                          if read(path)["source"] == source)
    candidate = read(candidate_path)
    path = next(path for path in (ROOT / "experiments/ecai_2027/mechanism/executions").glob("*/*.json")
                if path.stem == candidate["attempts"][0]["execution_id"])
    event = next(event for event in compact_record(path)["boundary_trace"] if event["hook"] == "after_call" and event["tool"] == "api_fetch")
    delay = TaskBoundary(hook="after_call", tool=event["tool"], service=event["service"], endpoint=event["endpoint"])
    split = asset["body"].index("\n") + 1
    meter, model = meter_model()
    evaluator = ResearchEvaluator(plan, meter)
    workspace = EvolutionWorkspace(OUT / "common", evaluator.version)
    ids = []
    for arm in ARMS:
        value = KnowledgeTreatment(source=source, task=source, treatment=arm, selected=True,
            route_version="preflight", contract=asset["body"][:split], remainder=asset["body"][split:], delay=delay).model_dump(mode="json")
        record = await execute(evaluator, workspace, source, "preflight_" + arm, "learning", treatment=value)
        verify_requests(record)
        ids.append(record["execution_id"])
    from engine.skillaa_audit import native, rollout, AUDIT
    native()
    from graphopt.types import SkillGraph
    external = read(AUDIT / "inventory.json")
    external_rows = rollout(model, SkillGraph.from_dict(external["graph"]),
                            [external["items"][external["source_ids"][0]]], "preflight")
    freeze(OUT / "preflight.json", {"decision": "PASS", "files": implementation(), "executions": ids,
                                   "skillaa_case": external_rows[0]["id"]})


async def candidates():
    require_ready()
    require_preflight()
    inventory = read(OUT / "inventory.json")
    design = Design.model_validate(read(OUT / "design.json"))
    meter, model = meter_model()
    evaluator = ResearchEvaluator(evaluator_plan(inventory, design), meter)
    workspace = EvolutionWorkspace(OUT / "common", evaluator.version)
    async def generate(slot):
        folder = OUT / "sources" / slot.source
        baseline = await execute(evaluator, workspace, slot.source, "source_N", "learning")
        if baseline["success"]:
            freeze(folder / "status.json", {"status": "baseline_success", "source": slot.source})
            return None
        from engine.run_research import evidence_view
        feedback = evidence_view(baseline)
        repair_prompt = (
            "Produce task knowledge repairing this genuine failure. Preserve only text instructions; tools and "
            "parameters remain native. Classify the failure category:\n"
            "- 'planning': if success depends on an early policy, authority, eligibility, or hold decision that "
            "must precede subsequent effectful actions (delaying knowledge causes errors or extra recovery).\n"
            "- 'execution': if the high-level plan/intent is fixed by the task/workflow and the failure was caused by "
            "execution-level details (such as tool parameter names, column IDs, destination fields, payload format, "
            "verbatim tokens, or endpoint syntax).\n"
            "If category is 'planning', split body at a sentence boundary into an early planning contract (up to "
            "character offset contract_end > 0) and remaining body.\n"
            "If category is 'execution', provide the execution knowledge; if there is no separate planning contract, "
            "set contract_end = 0.\n"
            "Identify the actual API delay boundary and earliest critical decision deadline (null if absent or uncertain). "
            "Transfer outcomes are hidden."
        )
        for attempt in range(POLICY["candidate_repairs"]):
            repair = await asyncio.to_thread(ask, model,
                repair_prompt,
                {"task": inventory["tasks"][slot.source], "evidence": feedback},
                Repair, "proposal/" + slot.source + "/" + str(attempt))
            freeze(folder / ("proposal_" + str(attempt) + ".json"), repair.model_dump(mode="json"))
            if repair.category != slot.category:
                freeze(folder / "status.json", {"status": "category_mismatch", "source": slot.source,
                                               "expected": slot.category, "observed": repair.category})
                return None
            if not repair.useful:
                feedback = {"previous": feedback, "proposal": repair.model_dump(mode="json"),
                            "error": "Repair must contain useful task knowledge"}
                continue
            route = TaskRoute(selected=True, knowledge_need=repair.rationale, delay=repair.delay,
                              deadline=repair.deadline, deadline_certainty="clear" if repair.deadline else "absent")
            config_value = treatment(repair, route, slot.source, slot.source, "G", "source-confirmation")
            record = await execute(evaluator, workspace, slot.source, "source_G", "learning",
                                   treatment=config_value, identity=attempt)
            if record["success"]:
                row = {"source": slot.source, "repair": repair.model_dump(mode="json"),
                       "baseline_execution": baseline["execution_id"], "source_execution": record["execution_id"],
                       "attempt": attempt, "slot": slot.model_dump(mode="json")}
                freeze(folder / "candidate.json", row)
                return row
            feedback = evidence_view(record)
        else:
            freeze(folder / "status.json", {"status": "no_eligible_repair", "source": slot.source})
        return None
    accepted = []
    for frozen_slot in design.sources:
        for task in [frozen_slot.source, *frozen_slot.alternates]:
            slot = frozen_slot.model_copy(update={"source": task, "alternates": []})
            row = await generate(slot)
            if row is not None:
                accepted.append(row)
                break
    freeze(OUT / "candidates.json", accepted)
    return accepted


def register():
    require_ready()
    inventory = read(OUT / "inventory.json")
    design = Design.model_validate(read(OUT / "design.json"))
    candidates = read(OUT / "candidates.json")
    if not candidates:
        raise ValueError("冻结来源池没有满足合法性与来源效用条件的候选")
    _, model = meter_model()
    route_version = fingerprint(design.router_instructions)
    registrations = []
    planning_count = sum(c["slot"]["category"] == "planning" for c in candidates)
    execution_count = sum(c["slot"]["category"] == "execution" for c in candidates)
    scope_adj = {
        "schema": "gnosis.timing.scope_adjustment.v1",
        "scope_adjustment": "none" if len(candidates) == 8 and planning_count == 4 and execution_count == 4 else "partial",
        "planning_sources_evaluated": planning_count,
        "execution_sources_evaluated": execution_count,
        "interaction_test_status": "ready" if len(candidates) == 8 and planning_count == 4 and execution_count == 4 else "unmet_sample_condition_not_executed",
        "date": "2026-10-11"
    }
    save_json(OUT / "scope_adjustment.json", scope_adj)
    for candidate in candidates:
        repair = Repair.model_validate(candidate["repair"])
        slot = SourceSlot.model_validate(candidate["slot"])
        tasks = [*slot.related, slot.outside]
        folder = OUT / "sources" / slot.source
        routes = {}
        for task in tasks:
            route = ask(model, design.router_instructions,
                {"task": inventory["tasks"][task]["description"], "contract": repair.body[:repair.contract_end],
                 "default_delay": repair.delay.model_dump(mode="json"), "source_category": repair.category},
                TaskRoute, "route/" + slot.source + "/" + task)
            routes[task] = route.model_dump(mode="json")
        save_json(folder / "registration_status.json", {"status": "registered", "source": slot.source})
        from engine.run_research import evidence_view
        baseline = read(OUT / "executions/source_N" / (candidate["baseline_execution"] + ".json"))
        pred_prompt = (
            f"{design.predictor_instructions}\n\n"
            f"REQUIRED OUTPUT SCHEMA RULES:\n"
            f"- 'tasks': a list of task IDs requiring retest, chosen ONLY from {list(tasks)}. Maximum {design.prediction_limit} items. Do NOT output role labels like 'K' or descriptions in this field.\n"
            f"- 'latest_boundary': an object mapping EXACTLY each of {list(tasks)} to its latest TaskBoundary (or null).\n"
            f"- 'knowledge_need': text description of needed knowledge and role breakdown."
        )
        prediction = ask(model, pred_prompt,
            {"tasks": {task: inventory["tasks"][task]["description"] for task in tasks},
             "old_evidence": evidence_view(baseline),
             "edit": {"before": "", "after": repair.body}, "size_limit": design.prediction_limit},
            Prediction, "prediction/" + slot.source)
        if (not set(prediction.tasks) <= set(tasks) or len(set(prediction.tasks)) != len(prediction.tasks)
                or len(prediction.tasks) > design.prediction_limit or set(prediction.latest_boundary) != set(tasks)):
            save_json(folder / "registration_status.json", {
                "status": "registration_failed", "source": slot.source, "error": "预测集合身份或大小不符合冻结协议"
            })
            continue
        order = [{"task": task, "arm": arm, "repeat": repeat}
                 for task in tasks for arm in ARMS for repeat in range(2)]
        random.Random(f"{SEED}:{slot.source}").shuffle(order)
        selected = {task: route["selected"] for task, route in routes.items()}
        version = {"route_version": route_version, "contract": repair.body[:repair.contract_end],
                   "remainder": repair.body[repair.contract_end:]}
        dependencies = {arm: knowledge_validation_tasks({**version, "remainder": "", "treatment": arm},
                         {**version, "treatment": arm}, selected, tasks, tasks) for arm in ("G", "L", "D", "C")}
        save_json(folder / "registration_status.json", {"status": "registered", "source": slot.source})
        registrations.append({**candidate, "routes": routes, "prediction": prediction.model_dump(mode="json"),
            "order": order, "pool": tasks, "route_version": route_version,
            "contract_consumers": tasks, "route_consumers": tasks, "body_update_sets": dependencies,
            "contract_update_set": knowledge_validation_tasks({**version, "contract": ""}, version, selected, tasks, tasks),
            "route_update_set": knowledge_validation_tasks({**version, "route_version": ""}, version, selected, tasks, tasks)})
    return freeze(OUT / "protocol.json", {"schema": "gnosis.timing.v1", "inventory": fingerprint(inventory),
        "design": design.model_dump(mode="json"), "analysis": inventory["analysis"], "sources": registrations,
        "scope_adjustment": scope_adj,
        "experiment_version": experiment_version(OUT), "implementation": implementation(),
        "sample_size": len(registrations), "target_size": 8,
        "confirmation_calls": 30 * len(registrations)})


async def confirm():
    require_ready()
    require_preflight()
    protocol = read(OUT / "protocol.json")
    if protocol["implementation"] != implementation() or protocol["experiment_version"] != experiment_version(OUT):
        raise ValueError("确认阶段程序与冻结协议不一致")
    design = Design.model_validate(protocol["design"])
    inventory = read(OUT / "inventory.json")
    meter, _ = meter_model()
    evaluator = ResearchEvaluator(evaluator_plan(inventory, design), meter)
    workspace = EvolutionWorkspace(OUT / "common", evaluator.version)
    async def source_block(source):
        repair = Repair.model_validate(source["repair"])
        rows = []
        for item in source["order"]:
            route = TaskRoute.model_validate(source["routes"][item["task"]])
            value = treatment(repair, route, source["source"], item["task"], item["arm"], source["route_version"])
            record = await execute(evaluator, workspace, item["task"], item["arm"], "selection",
                                   item["repeat"], value, fingerprint(protocol))
            verify_requests(record)
            rows.append(record)
        freeze(OUT / "blocks" / (source["source"] + ".json"), [row["execution_id"] for row in rows])
    await asyncio.gather(*(source_block(source) for source in protocol["sources"]))


def process_review():
    require_ready()
    from engine.timing_analysis import records_for, visibility
    source_output = Path(os.environ.get("GNOSIS_TIMING_PARENT", str(OUT)))
    protocol = read(source_output / "protocol.json")
    inventory = read(source_output / "inventory.json")
    _, model = meter_model()
    for source in protocol["sources"]:
        records = records_for(source, protocol, output=source_output)
        for (task, arm, repeat), record in records.items():
            process_path = OUT / "process" / (record["execution_id"] + ".json")
            status_path = OUT / "process_status" / (record["execution_id"] + ".json")
            if status_path.exists():
                continue
            if process_path.exists():
                raise ValueError("过程审阅缺少原始响应核验状态：" + record["execution_id"])
            position = visibility(record, source["routes"][task])
            n_actions = len(record["actual_tool_actions"])
            def validate_indices(value):
                cited = value.critical_action_indices + value.recovery_action_indices
                if any(index < 0 or index >= n_actions for index in cited):
                    raise ValueError(f"动作索引必须位于 actual_tool_actions 的有效范围 0 至 {n_actions - 1}")
            try:
                result = ask(model,
                    "Review this real trajectory without task scores or treatment labels. Use the task requirements "
                    "and API observations to identify the earliest consequential decision, constraint damage, and "
                    "actual recovery actions. Recovery means an action needed to repair a previous decision after "
                    "the knowledge became available. Cite zero-based indices into actual_tool_actions. "
                    "Mark multiple plausible deadlines uncertain; do not infer hidden actions.",
                    {"task": inventory["tasks"][task]["description"], "knowledge": source["repair"]["body"],
                     "visible_positions": position["first_visible"], "registered_deadline": source["routes"][task]["deadline"],
                     "actual_tool_actions": record["actual_tool_actions"], "completion": record["completion"]},
                    ProcessReview, "process/" + record["execution_id"], validate_result=validate_indices)
            except AIFormatError:
                freeze(status_path, {"status": "invalid_model_output", "execution_id": record["execution_id"],
                                     "phase": "process/" + record["execution_id"]})
                continue
            freeze(process_path, result.model_dump(mode="json"))
            freeze(status_path, {"status": "valid", "execution_id": record["execution_id"],
                                 "phase": "process/" + record["execution_id"]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "offline", "preflight", "develop", "candidates", "register", "confirm",
                                            "process", "analyze", "finances", "skillaa_prepare", "skillaa_run", "skillaa_analyze"])
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare()
        print(json.dumps({"available_templates": len(value["order"]), "discovery_repairs": len(value["discovery"])}))
    elif args.command == "offline":
        from engine.timing_offline import check
        check()
    elif args.command == "develop":
        develop()
    elif args.command == "preflight":
        asyncio.run(preflight())
    elif args.command == "candidates":
        asyncio.run(candidates())
    elif args.command == "register":
        register()
    elif args.command == "confirm":
        asyncio.run(confirm())
    elif args.command == "process":
        process_review()
    elif args.command == "analyze":
        from engine.timing_analysis import analyze
        analyze()
    elif args.command == "finances":
        value = finances()
        print(json.dumps({"historical_unknown_cny": value["historical_unknown_cny"], "new_calls": value["new_calls"]}))
    else:
        from engine.skillaa_audit import prepare as skillaa_prepare, run as skillaa_run, analyze as skillaa_analyze
        {"skillaa_prepare": skillaa_prepare, "skillaa_run": skillaa_run, "skillaa_analyze": skillaa_analyze}[args.command]()


if __name__ == "__main__":
    main()
