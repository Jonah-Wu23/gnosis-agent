import argparse
import asyncio
import hashlib
import json
import os
import random
import itertools
from pathlib import Path

from dotenv import load_dotenv
from jsonpointer import JsonPointerException, resolve_pointer
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from automationbench.domains import get_domain_dataset
from automationbench.runner import compute_allowed_services, strip_none_values
from engine.activation import components_version
from engine.evolution import EditableUnit, EvolutionWorkspace
from engine.research_evaluator import ResearchEvaluator
from engine.research_model import ResearchModel
from engine.research_upstream import verify_upstreams
from engine.run_pilot import config
from engine.run_research import REPAIR_INSTRUCTIONS, audit_output, evidence_view
from engine.scoped_units import AIFormatError, Budget, ExecutionBlocked, POLICY, beijing_peak, experiment_version, fingerprint, save_json

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "experiments/feedback_chain"
FROZEN = BASE / "frozen"
OUT = BASE / "paid"
ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
SEED = 20261014
CONTENT_CHARS = 80000
FX = 7.1


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def seal(path, value):
    if path.exists() and read(path) != value:
        raise ValueError("冻结内容发生变化：" + path.name)
    save_json(path, value)
    return value


def ancestor(selection):
    path = BASE / "source_data/versions/gnosis" / selection["ancestor_version"] / "manifest.json"
    units = [EditableUnit.model_validate(u).model_dump() for u in read(path)["units"]]
    if components_version(units) != selection["ancestor_version"]:
        raise ValueError("祖先内容与版本不一致")
    return units


def prepare():
    selection = read(FROZEN / "selection_v1.json")
    labels = read(FROZEN / "labels_v1.json")
    old_plan = read(ROOT / "config/research_selection.json")
    exposure = read(BASE / "out/exposure_registry.json")["exposure"]
    clean = read(BASE / "out/clean_pool.json")
    units = ancestor(selection)
    sources = [b["source"] for b in selection["blocks"]]
    migrations = [m["task"] for b in selection["blocks"] for m in b["migration"]]
    if len(set(sources)) != 6 or len(set(migrations)) != 24 or set(sources) & set(migrations):
        raise ValueError("来源或迁移任务数量、隔离关系错误")
    if not set(sources) <= set(old_plan["search"]):
        raise ValueError("来源任务退出了开发集")
    for block in selection["blocks"]:
        if len(block["migration"]) != 4:
            raise ValueError("来源块缺少四道迁移题")
        for item in block["migration"]:
            if item["task"] in exposure or item["task"] not in clean[block["domain"]]:
                raise ValueError("迁移任务存在历史暴露")
    task_labels = {t["task"]: t for b in labels["blocks"] for t in b["tasks"]}
    if set(task_labels) != set(sources + migrations):
        raise ValueError("标签任务集合与选题不一致")
    tasks = {}
    for domain in [b["domain"] for b in selection["blocks"]]:
        for row in get_domain_dataset(domain):
            info = strip_none_values(json.loads(row["info"]))
            name = info["task_name"]
            if name not in task_labels:
                continue
            label = task_labels[name]
            instruction = next(m["content"] for m in json.loads(row["prompt"]) if m["role"] == "user") if isinstance(row["prompt"], str) else next(m["content"] for m in row["prompt"] if m["role"] == "user")
            assertions = [{"type": a["type"], **a["params"]} for a in label["assertions"]]
            if instruction != label["prompt"] or assertions != info["assertions"]:
                raise ValueError("标签与官方任务定义不一致：" + name)
            if [a["idx"] for a in label["assertions"]] != list(range(len(assertions))):
                raise ValueError("断言索引缺失或重复")
            tasks[name] = {"domain": domain, "contract": fingerprint(row),
                           "services": compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"])}
    if set(tasks) != set(task_labels):
        raise ValueError("官方任务库缺少冻结任务")
    rng = random.Random(SEED)
    blocks = []
    for block in selection["blocks"]:
        edit_order = rng.sample(list(ARMS), len(ARMS))
        retest_order = rng.sample(list(ARMS), len(ARMS))
        migration_order = []
        for repeat in range(3):
            for item in rng.sample(block["migration"], 4):
                for arm in rng.sample([*ARMS, "ancestor"], 5):
                    migration_order.append({"task": item["task"], "repeat": repeat, "arm": arm})
        carry_order = [{"task": m["task"], "repeat": r, "arm": "carry"}
                       for r in range(3) for m in rng.sample(block["migration"], 4)]
        blocks.append({**block, "edit_order": edit_order, "retest_order": retest_order,
                       "evaluation_order": [*migration_order, *carry_order]})
    q5 = read(BASE / "out/q5_verify.json")
    if q5["examples"] or q5.get("records_with_injections") != 670:
        raise ValueError("Q5 真实记录核验未完成")
    files = ["selection_v1.json", "labels_v1.json", "比较规则_v1.md", "冻结评价标签.md"]
    result = {"schema": "gnosis.feedback_chain.execution.v1", "seed": SEED,
              "ancestor_version": selection["ancestor_version"], "blocks": blocks,
              "plan": {"search": sources, "heldout": migrations, "selection": [], "checkpoint": [], "tasks": tasks},
              "contracts": {name: hashlib.sha256((FROZEN / name).read_bytes()).hexdigest() for name in files},
              "upstreams": verify_upstreams(), "candidate_content_chars": CONTENT_CHARS,
              "candidate_unit_limit": 24, "budget_cny": 200, "fx": FX,
              "source_success": "四候选登记编辑失败并部署祖先；六块均保留",
              "business_evaluation": "按冻结提示词和原始资料盲审全部结果，核对初始状态、最终状态与实际动作；争议断言不删除业务义务",
              "masked_assertions": "仅移除 token_mismatch 与 unsatisfiable；duplicate 和 count_amplification 全部保留",
              "inference": "六块等权；双侧精确符号翻转检验64种；95%均值偏移反演区间；阈值0.10；少于六完整块只作描述",
              "model": {"provider": "https://api.deepseek.com", "name": "deepseek-flash",
                        "thinking": "enabled", "reasoning_effort": "high", "temperature": 1, "max_tokens": 393216},
              "counts": {"sources": 6, "retests": 24, "evaluation": 432, "edits": 24, "organization": 6}}
    if sum(len(b["evaluation_order"]) for b in blocks) != 432:
        raise ValueError("运行顺序缺少评价单元")
    if sum(len(json.dumps(u, ensure_ascii=False)) for u in units) > CONTENT_CHARS:
        raise ValueError("祖先超出四组共同内容预算")
    path = FROZEN / "execution_v1.json"
    if path.exists() and read(path) != result and any((OUT / "io").glob("*/request.json")):
        raise ValueError("付费调用已经开始，禁止重写执行契约")
    save_json(path, result)
    print("PASS：6 来源、24 零暴露迁移题、558 断言与官方定义一致；462 次执行顺序已冻结。")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(StrictModel):
    pointer: str
    quote: str = Field(min_length=1)


class OrganizedItem(StrictModel):
    requirement: str = Field(min_length=1)
    object: str = Field(min_length=1)
    consequence: str = Field(min_length=1)
    evidence: list[Citation] = Field(min_length=1)


class OrganizedFeedback(StrictModel):
    items: list[OrganizedItem] = Field(min_length=1)


class Edit(StrictModel):
    reason: str = Field(min_length=1)
    units: list[EditableUnit] = Field(min_length=1, max_length=24)


class BoundaryContent(StrictModel):
    applies_when: str = Field(min_length=1)
    avoids: str = Field(min_length=1)
    guidance: str = Field(min_length=1)


def ask(model, payload, schema, phase, instructions, validate_extra):
    messages = [{"role": "system", "content": instructions + "\nReturn JSON conforming to:\n" + json.dumps(schema.model_json_schema())},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    def validate(text):
        try:
            parsed = schema.model_validate_json(text)
            validate_extra(parsed)
        except (ValidationError, ValueError, JsonPointerException) as error:
            raise AIFormatError(str(error)) from error
    for attempt in range(POLICY["format_retries"] + 1):
        try:
            response = model.request(messages, phase, OUT / "requests" / phase / f"{attempt}.json", validate=validate)
            return schema.model_validate_json(response.choices[0].message.content)
        except AIFormatError as error:
            if attempt == POLICY["format_retries"]:
                raise
            messages = [*messages, {"role": "user", "content": str(error)}]


def verify_citations(value, evidence):
    for item in value.items:
        for citation in item.evidence:
            original = resolve_pointer(evidence, citation.pointer)
            if not isinstance(original, str):
                original = json.dumps(original, ensure_ascii=False)
            if citation.quote not in original:
                raise ValueError("反馈整理引用与原始输入不一致")


def verify_edit(value, arm, original):
    units = [u.model_dump() for u in value.units]
    if len({u["id"] for u in units}) != len(units):
        raise ValueError("候选单元 ID 重复")
    if sum(len(json.dumps(u, ensure_ascii=False)) for u in units) > CONTENT_CHARS:
        raise ValueError("候选超出共同内容预算")
    before = {u["id"]: u for u in original}
    for unit in units:
        if arm.endswith("R1") and before.get(unit["id"]) != unit:
            BoundaryContent.model_validate_json(unit["content"])


class FeedbackBudget(Budget):
    def reserve(self, *args, **kwargs):
        if beijing_peak():
            raise ExecutionBlocked("进入高峰时段，暂停新增模型调用并保留断点")
        return super().reserve(*args, **kwargs)


class FeedbackModel(ResearchModel):
    def resume(self, checkpoint, messages, options, validate=None, allow_budget_drift=False):
        saved = read(checkpoint)
        if saved["status"] == "inflight" and not (self.output / "io" / saved["call"] / "response.json").exists():
            if (saved["version"], saved["messages"], saved["options"]) != (self.version, messages, options):
                raise ValueError("未结算请求的断点输入发生变化")
            if os.environ.get("GNOSIS_RESUME_ONLY") == "1":
                raise RuntimeError("严格续跑缺少真实响应")
            return None
        return super().resume(checkpoint, messages, options, validate, allow_budget_drift)


def verify_delivery(record, units):
    calls = {w["call"]: w for w in record["wire_trace"]}
    by_id = {u["id"]: u for u in units}
    for injection in record["injections"]:
        if injection.get("kind") != "knowledge_injection":
            continue
        wire = calls[injection["next_agent_call"]]
        request = "\n".join(str(m.get("content", "")) for m in wire["request"])
        if by_id[injection["unit"]]["content"] not in request:
            raise ValueError("新实验登记的经验未送达实际模型请求")


def finance(meter):
    value = meter.state()
    return {"billed_cny": value["billed_usd"] * FX, "unsettled_cny": value["unsettled_usd"] * FX}


async def run():
    protocol = read(FROZEN / "execution_v1.json")
    for name, digest in protocol["contracts"].items():
        if hashlib.sha256((FROZEN / name).read_bytes()).hexdigest() != digest:
            raise ValueError("付费执行前冻结文件发生变化")
    load_dotenv(ROOT / ".env")
    if os.environ["MODER_PORVIDER"].rstrip("/") not in ("https://api.deepseek.com", "https://api.deepseek.com/v1") or os.environ["MODER_NAME"] != "deepseek-flash":
        raise ValueError("官方模型配置与冻结条件不一致")
    os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"
    version = experiment_version(OUT)
    proof = read(FROZEN / "preflight_v1.json")
    if proof["status"] != "passed" or proof["experiment_version"] != version or proof["protocol"] != fingerprint(protocol) or proof["implementation"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("当前实验版本尚未通过零费用联调")
    audit_output(OUT, version)
    audit_path = OUT / "workflow_audit.json"
    audit = read(audit_path)
    reviewed = ["engine/feedback_chain.py", "experiments/feedback_chain/frozen/execution_v1.json",
                *["experiments/feedback_chain/frozen/" + name for name in protocol["contracts"]]]
    audit["reviewed_files"].update({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in reviewed})
    save_json(audit_path, audit)
    rates = {"input_per_mtok": 1 / FX, "output_per_mtok": 4 / FX,
             "cache_read_per_mtok": 0.02 / FX, "peak_multiplier": 2}
    meter = FeedbackBudget(ROOT / "state/feedback_chain_ledger.sqlite", None, rates=rates, output=OUT)
    model = FeedbackModel(meter, version)
    evaluator = ResearchEvaluator(protocol["plan"], meter)
    workspace = EvolutionWorkspace(OUT / "workspace", version)
    original = ancestor(protocol)
    base_version = workspace.write_version(original)
    completed = []

    async def execute(task, arm, candidate, role, repeat=0, revision=None):
        identity = fingerprint(["feedback_chain.v1", task, arm, candidate, role, repeat])
        record = await evaluator.execute(workspace, candidate, task, arm, bucket="feedback_chain", role=role,
                                         repeat=repeat, execution_id=identity, revision=revision, capture_world=True)
        verify_delivery(record, workspace.load_version(candidate))
        return record

    for block in protocol["blocks"]:
        block_path = OUT / "blocks" / (block["domain"] + ".json")
        if block_path.exists():
            completed.append(read(block_path))
            continue
        if beijing_peak():
            raise RuntimeError("当前处于高峰时段，按冻结条件暂停新来源块")
        money = finance(meter)
        estimate = max([25.6, *(b["billed_cny"] for b in completed)]) * 1.2
        start_path = OUT / "block_started" / (block["domain"] + ".json")
        started = start_path.exists()
        if not started and 200 - money["billed_cny"] - money["unsettled_cny"] < estimate:
            save_json(OUT / "budget_stop.json", {**money, "next_complete_block_estimate_cny": estimate, "completed_blocks": len(completed)})
            print("预算不足以启动下一完整块，已保留断点。")
            return
        if not started:
            save_json(start_path, {"billed_before_cny": money["billed_cny"]})
        source = await execute(block["source"], "source", base_version, "learning")
        source_evidence = evidence_view(source)
        candidates = {}
        if source["success"]:
            candidates = {arm: {"version": base_version, "status": "source_passed_edit_failure"} for arm in ARMS}
        else:
            def citations(value):
                verify_citations(value, source_evidence)
            try:
                organized = ask(model, source_evidence, OrganizedFeedback, block["domain"] + "/organize",
                                "Reorganize only the supplied source execution facts by requirement, object and action consequence. Cite exact source substrings with JSON Pointers. Do not invent correct values or introduce other task knowledge.", citations)
            except AIFormatError as error:
                organized = None
                for arm in ("F1R0", "F1R1"):
                    candidates[arm] = {"version": base_version, "status": "invalid_organization", "error": str(error)}
            for arm in block["edit_order"]:
                if arm in candidates:
                    continue
                payload = {"ancestor": original, "source": source_evidence, "content_budget_chars": CONTENT_CHARS}
                if arm.startswith("F1"):
                    payload["organized_feedback"] = organized.model_dump()
                representation = "Changed or new units must encode content as JSON with nonempty applies_when, avoids and guidance. Preserve unchanged ancestor units verbatim." if arm.endswith("R1") else "Use ordinary free text for unit content."
                try:
                    edited = ask(model, payload, Edit, block["domain"] + "/" + arm + "/edit",
                                 REPAIR_INSTRUCTIONS + "\nExactly one edit opportunity. Return the complete candidate group. " + representation,
                                 lambda value: verify_edit(value, arm, original))
                    candidate = workspace.write_version([u.model_dump() for u in edited.units])
                    candidates[arm] = {"version": candidate, "status": "edited", "reason": edited.reason}
                except AIFormatError as error:
                    candidates[arm] = {"version": base_version, "status": "invalid_edit", "error": str(error)}
        # 四个候选在读取任何复测或迁移结果前一并冻结。
        seal(OUT / "candidates" / (block["domain"] + ".json"), candidates)
        for arm in block["retest_order"]:
            await execute(block["source"], arm + "_source", candidates[arm]["version"], "learning")
        results = []
        for item in block["evaluation_order"]:
            arm, task, repeat = item["arm"], item["task"], item["repeat"]
            candidate = candidates[arm]["version"] if arm in ARMS else base_version
            revision = None
            if arm == "carry":
                first = await execute(task, "ancestor", base_version, "heldout", repeat)
                revision = evidence_view(first)
            record = await execute(task, arm, candidate, "heldout", repeat, revision)
            results.append({"arm": arm, "task": task, "repeat": repeat, "execution_id": record["execution_id"]})
        cost = finance(meter)["billed_cny"] - read(start_path)["billed_before_cny"]
        value = {"domain": block["domain"], "candidates": candidates, "results": results, "billed_cny": cost}
        seal(block_path, value)
        completed.append(value)
        print(f"来源块 {block['domain']} 完成，累计扣费 {finance(meter)['billed_cny']:.3f} 元。")
    print("六个来源块执行完毕，业务盲审后按冻结规则分析。")


async def check():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"
    from pydantic_core import to_jsonable_python
    from engine.automation_adapter import ScopedInjector
    from engine.evolution import VersionLibrary
    from engine.research_offline import dataset_row, environment, output_folder, replay
    from automationbench.schema.world import WorldState
    protocol = prepare()
    folder, version = output_folder()
    workspace = EvolutionWorkspace(folder / "feedback_chain", version)
    units = ancestor(protocol)
    candidate = workspace.write_version(units)
    count = 0
    replayed = set()
    history = Path.home() / ".gnosis/research/executions/gnosis"
    for path in sorted(history.glob("*.json")):
        prior = read(path)
        if prior["version"] != candidate or prior["task"] not in protocol["plan"]["search"] or prior["task"] in replayed:
            continue
        dataset, row = dataset_row(get_domain_dataset(prior["domain"]), prior["task"])
        injector = ScopedInjector(VersionLibrary(workspace, candidate), config())
        try:
            record, state, _ = await replay(environment(dataset, row, folder, injector), row, prior, version, units)
            final = to_jsonable_python(state["world"])
            if WorldState.model_validate(final).model_dump(mode="json") != final:
                raise ValueError("最终业务状态不能无损保存")
            if not record["actual_tool_actions"] or record["harness_defects"]:
                raise ValueError("真实来源轨迹重放未通过")
            if len(record["assertions"]) != len(json.loads(row["info"])["assertions"]):
                raise ValueError("评分结果未覆盖全部断言")
            save_json(folder / (prior["domain"] + "_world.json"), {"initial_world": json.loads(row["info"])["initial_state"], "final_world": final})
            replayed.add(prior["task"])
            count += 1
        finally:
            injector.close()
    if count == 0:
        raise ValueError("找不到冻结祖先的真实来源轨迹，零费用联调未执行")
    proof = {"status": "passed", "model_calls": 0, "historical_sources_replayed": count,
             "experiment_version": version,
             "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "protocol": fingerprint(protocol)}
    save_json(FROZEN / "preflight_v1.json", proof)
    print(f"PASS：{count} 道真实来源轨迹在冻结祖先上重放，业务状态保存与评分覆盖通过，模型调用 0 次。")


class RequirementVerdict(StrictModel):
    requirement: str = Field(min_length=1)
    req_class: str
    passed: bool
    evidence: list[Citation] = Field(min_length=1)


class BusinessVerdict(StrictModel):
    task: str
    complete_prompt_coverage: bool
    requirements: list[RequirementVerdict] = Field(min_length=1)


def export_audit():
    protocol = read(FROZEN / "execution_v1.json")
    labels = {t["task"]: t for b in read(FROZEN / "labels_v1.json")["blocks"] for t in b["tasks"]}
    mapping = {}
    for block in protocol["blocks"]:
        for item in read(OUT / "blocks" / (block["domain"] + ".json"))["results"]:
            record = read(OUT / "executions" / item["arm"] / (item["execution_id"] + ".json"))
            masked = fingerprint(["feedback_business_review", record["execution_id"]])
            bundle = {"task": record["task"], "instruction": labels[record["task"]]["prompt"],
                      "initial_world": record["initial_world"], "final_world": record["final_world"],
                      "actions": [{k: a[k] for k in ("tool", "arguments", "raw", "failed", "rejected") if k in a}
                                  for a in record["actual_tool_actions"]]}
            seal(OUT / "business_review/materials" / (masked + ".json"), bundle)
            mapping[masked] = {**item, "domain": block["domain"]}
    seal(OUT / "business_review/mapping.json", mapping)
    save_json(OUT / "business_review/verdict_schema.json", BusinessVerdict.model_json_schema())
    print("432 份业务评价材料已隐藏组别，评价只读取 materials，逐件结果写入 verdicts/<同名>.json。")


def paired_inference(differences):
    values = np.asarray(differences, dtype=float)
    signs = np.asarray(list(itertools.product((-1, 1), repeat=6)))
    observed = float(values.mean())
    p = float(np.mean(np.abs(signs @ values / 6) >= abs(observed) - 1e-12))
    # 冻结共同偏移模型，在百分点万分之一的网格上反演双侧检验。
    shifts = np.linspace(-1, 1, 20001)
    samples = (signs @ values[:, None] - signs.sum(axis=1)[:, None] * shifts) / 6
    probabilities = np.mean(np.abs(samples) >= np.abs(observed - shifts)[None, :] - 1e-12, axis=0)
    accepted = shifts[probabilities >= 0.05]
    return {"effect": observed, "p_two_sided": p, "ci95": [float(accepted.min()), float(accepted.max())],
            "ci_grid_resolution": 0.0001, "model": "共同均值偏移与块差符号可交换"}


def analyze():
    protocol = read(FROZEN / "execution_v1.json")
    mapping = read(OUT / "business_review/mapping.json")
    if len(mapping) != 432:
        raise ValueError("六个完整来源块的盲审材料缺失")
    labels = {t["task"]: t for b in read(FROZEN / "labels_v1.json")["blocks"] for t in b["tasks"]}
    groups = {}
    categories = {}
    strict = {}
    sensitivity = {}
    rows = {}
    for masked, item in mapping.items():
        bundle = read(OUT / "business_review/materials" / (masked + ".json"))
        verdict = BusinessVerdict.model_validate(read(OUT / "business_review/verdicts" / (masked + ".json")))
        if verdict.task != item["task"] or not verdict.complete_prompt_coverage:
            raise ValueError("业务审查未覆盖全部任务义务")
        for requirement in verdict.requirements:
            if requirement.req_class not in ("necessary", "prohibited", "value"):
                raise ValueError("业务要求类别必须为 necessary、prohibited 或 value")
            for citation in requirement.evidence:
                original = resolve_pointer(bundle, citation.pointer)
                text = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False)
                if citation.quote not in text:
                    raise ValueError("业务评价证据不存在")
        record = read(OUT / "executions" / item["arm"] / (item["execution_id"] + ".json"))
        success = all(r.passed for r in verdict.requirements) and record["termination_status"] == "completed" and record["execution_error"] is None
        key = (item["domain"], item["arm"])
        groups.setdefault(key, []).append(int(success))
        for category in ("necessary", "prohibited", "value"):
            applicable = [r for r in verdict.requirements if r.req_class == category]
            if applicable:
                categories.setdefault((*key, category), []).append(int(all(r.passed for r in applicable)))
        strict.setdefault(key, []).append(int(record["success"]))
        kept = [a["passed"] for a, l in zip(record["assertions"], labels[item["task"]]["assertions"], strict=True)
                if not l["dispute"] or l["dispute"].split(":", 1)[0] not in ("token_mismatch", "unsatisfiable")]
        sensitivity.setdefault(key, []).append(int(all(kept) and record["execution_error"] is None and record["termination_status"] == "completed"))
        rows[(item["task"], item["arm"], item["repeat"])] = {"success": success, "cost": record["cost_usd"] * FX}
    blocks = []
    for block in protocol["blocks"]:
        domain = block["domain"]
        if any(len(groups[(domain, arm)]) != 12 for arm in [*ARMS, "ancestor", "carry"]):
            raise ValueError("来源块评价次数不完整")
        means = {arm: float(np.mean(groups[(domain, arm)])) for arm in [*ARMS, "ancestor", "carry"]}
        blocks.append({"domain": domain, "business": means,
                       "category_completion": {arm: {category: float(np.mean(categories[(domain, arm, category)]))
                                                     if (domain, arm, category) in categories else None
                                                     for category in ("necessary", "prohibited", "value")} for arm in means},
                       "strict": {arm: float(np.mean(strict[(domain, arm)])) for arm in means},
                       "sensitivity": {arm: float(np.mean(sensitivity[(domain, arm)])) for arm in means},
                       "F1R0_minus_F0R0": means["F1R0"] - means["F0R0"]})
    result = {"blocks": blocks, "primary": paired_inference([b["F1R0_minus_F0R0"] for b in blocks]),
              "R1_increment": paired_inference([b["business"]["F0R1"] - b["business"]["F0R0"] for b in blocks]),
              "interaction": [b["business"]["F1R1"] - b["business"]["F1R0"] - b["business"]["F0R1"] + b["business"]["F0R0"] for b in blocks]}
    lower, upper = result["primary"]["ci95"]
    result["primary"]["decision"] = "precision_insufficient" if lower <= 0 and upper >= 0.10 else "below_practical_threshold" if upper < 0.10 else "practical_gain_supported" if lower >= 0.10 else "positive_but_practical_gain_uncertain"
    paired = []
    for task in protocol["plan"]["heldout"]:
        for repeat in range(3):
            first = rows[(task, "ancestor", repeat)]
            fresh = rows[(task, "ancestor", (repeat + 1) % 3)]
            carry = rows[(task, "carry", repeat)]
            paired.append({"task": task, "repeat": repeat, "fresh_success": fresh["success"], "carry_success": carry["success"],
                           "fresh_strategy_cny": first["cost"] + fresh["cost"], "carry_strategy_cny": first["cost"] + carry["cost"]})
    result["trajectory_pairs"] = paired
    save_json(OUT / "analysis.json", result)
    print("六块业务盲审与预设统计已完成：paid/analysis.json。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "check", "run", "audit", "analyze"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "check":
        asyncio.run(check())
    elif args.command == "audit":
        export_audit()
    elif args.command == "analyze":
        analyze()
    else:
        asyncio.run(run())


if __name__ == "__main__":
    main()
