import asyncio
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from jsonschema import ValidationError as JsonSchemaValidationError
from openai.types.chat import ChatCompletion
from pydantic import ValidationError
from verifiers.types import AssistantMessage, ToolCall

from automationbench.domains import get_domain_dataset
from automationbench.rubric import create_rubric, partial_credit, task_completed_correctly

from engine.activation import (changed_units, components_version, response_termination, validate_scope,
                               validation_plan, reuse_evidence)
from engine.automation_adapter import ScopedAutomationEnv, ScopedInjector, restore_messages, EXECUTION_STATE
from engine.evolution import (ActionRejected, EditableUnit, EvolutionWorkspace, ResearchBudget,
                              VersionLibrary, paired_selection)
from engine.research_model import ResearchModel
from engine.research_plan import ROOT, prepare_plan
from engine.research_retest import retest_comparison
from engine.research_upstream import official_ace, starharness
from engine.run_pilot import config
from engine.research_plan import ARM_LEARNING_LIMITS
from engine.run_research import train_ace
from engine.scoped_units import (POLICY, VERSION_FILES, Budget, BudgetExceeded, TaskCostExceeded, UnitLibrary,
                                 AIFormatError, ExecutionBlocked, ExecutionTruncated, require_model_access,
                                 save_json, experiment_version, fingerprint)


# 先导记录已报废归档，只作离线机制检查的真实工具轨迹与响应样本，不进学习、基线与统计。
FIXTURES = ROOT / "archive/2026-10-07-void/experiments/pilot"


def ledger_snapshot():
    from engine.run_research import RESEARCH_LEDGER
    if not RESEARCH_LEDGER.exists():
        return None
    with sqlite3.connect("file:" + str(RESEARCH_LEDGER) + "?mode=ro", uri=True) as db:
        return db.execute("SELECT COUNT(*),SUM(cost),SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END) FROM calls").fetchone()


def prior_baseline():
    paths = sorted((FIXTURES / "runs").glob("*/base.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if len(rows) == 30 and len({r["task"] for r in rows}) == 30:
            return rows
    raise FileNotFoundError("离线重放需要已完成的 30 道先导真实执行记录")


def saved_responses(prior):
    responses = []
    for old in prior:
        for call in old.get("wire_trace", []):
            path = FIXTURES / "io" / call["call"] / "response.json"
            if path.exists():
                responses.append(ChatCompletion.model_validate_json(path.read_text(encoding="utf-8")))
    return responses


class OfflineChecks:
    def __init__(self):
        self.count = 0

    def check(self, condition, message):
        self.count += 1
        if not condition:
            raise AssertionError(message)

    def rejects(self, error_type, operation, message):
        self.count += 1
        try:
            operation()
        except error_type:
            return
        raise AssertionError(message)

    async def arejects(self, error_type, operation, message):
        self.count += 1
        try:
            await operation()
        except error_type:
            return
        raise AssertionError(message)


async def replay(env, row, prior, version, units, repeat=0):
    state = await env.setup_state({"prompt": restore_messages(row["prompt"]), "info": row["info"],
                                   "trajectory": [], "completion": []})
    history = list(state["prompt"])
    outputs = []
    for message in restore_messages(prior["completion"]):
        if message.role != "assistant":
            continue
        if not message.tool_calls:
            history.append(message)
            continue
        result = await env.env_response([*history, message], state)
        history.extend([message, *result])
        outputs.extend(result)
        state["trajectory"].append({"prompt": history, "completion": [message], "response": None})
    partial = partial_credit(state)
    record = {"task": prior["task"], "domain": prior["domain"], "version": components_version(units),
              "experiment_version": version, "task_contract": fingerprint(row), "learning_role": "learning",
              "success": bool(task_completed_correctly(state)), "partial_credit": partial,
              "assertions": state["_assertion_results"], "execution_id": uuid.uuid4().hex, "repeat": repeat,
              "boundary_trace": state["boundary_trace"], "boundary_schema": state["boundary_schema"],
              "actual_tool_actions": state["actual_tool_actions"],
              "active_extensions": state.get("active_extensions", {}),
              "first_effect_boundaries": state.get("first_effect_boundaries", {}),
              "component_capability_calls": state.get("component_capability_calls", 0),
              "harness_defects": state.get("harness_defects", []),
              "cost_usd": 0.0,
              "prompt": [m.model_dump(mode="json") for m in state["prompt"]],
              "completion": [m.model_dump(mode="json") for m in history[len(state["prompt"]):]],
              "touches": state["scope_touches"], "injections": state["injection_events"],
              "termination_status": state["termination_status"], "offline_tool_replay": True}
    return record, state, outputs


def dataset_row(dataset, task):
    index = next(i for i, row in enumerate(dataset) if json.loads(row["info"])["task_name"] == task)
    return dataset.select([index]), dataset[index]


IDENTIFIER = re.compile(r"\b[0-9a-f]{12,}\b|\b[a-z][a-z0-9]*_[a-z0-9_]{6,}\b")


def regenerated_identifier_divergence(prior, record):
    """判断评分差异是否来自服务端重新生成的标识符。

    上游世界用 uuid4 生成新实体标识符。轨迹里后续调用引用的标识符在重放中会重新生成，
    于是出现「某次失败调用自己引用的标识符，本次重放从未创建，环境真实返回找不到该实体」。
    只有能逐次对上这一组合的差异才算不可比，其余差异视为运行时行为改变。
    """
    referenced = set(IDENTIFIER.findall(json.dumps([message.get("tool_calls") for message in prior["completion"]
                                                    if isinstance(message, dict)], ensure_ascii=False)))
    created = set(IDENTIFIER.findall(" ".join(action["raw"] or "" for action in record["actual_tool_actions"]
                                              if not action.get("failed"))))
    for action in record["actual_tool_actions"]:
        if not action.get("failed"):
            continue
        failure = action["raw"] or ""
        arguments = json.dumps(action.get("arguments") or {}, ensure_ascii=False)
        if any(identifier not in created and identifier in failure and identifier in arguments
               for identifier in referenced):
            return True
    return False


def environment(dataset, row, folder, injector=None):
    return ScopedAutomationEnv(dataset=dataset, rubric=create_rubric(), toolset="api",
                               injector=injector, execution_output=folder)


async def setup(dataset, row, folder, injector=None):
    return await environment(dataset, row, folder, injector).setup_state(
        {"prompt": restore_messages(row["prompt"]), "info": row["info"], "trajectory": [], "completion": []})


async def tool_call(env, state, tool_name, arguments, tool_call_id):
    token = EXECUTION_STATE.set(state)
    try:
        args = env.update_tool_args(tool_name, arguments, [], state)
        return await env.call_tool(tool_name, args, tool_call_id)
    finally:
        EXECUTION_STATE.reset(token)


def executable_unit(service, read_arguments, search_arguments):
    """可执行组件：在调用结果位置读取真实数据，并登记一个可复用的操作函数。"""
    parameters = {"type": "object", "properties": {key: {"type": "string"} for key in sorted(read_arguments)},
                  "required": ["method", "url"], "additionalProperties": False}
    source = f"""const READ = {json.dumps(parameters)};
const SEARCH = {json.dumps(search_arguments)};

export function onBoundary(input) {{
  const tools = [{{ name: 'read', description: 'Read any authorized endpoint and return its fields', parameters: READ }}];
  if (input.event.hook === 'operation') {{
    if (!input.results) return {{ calls: [{{ tool: 'api_fetch', arguments: input.input.arguments }}], state: input.state }};
    return {{ result: JSON.parse(input.results[0].raw), state: input.state }};
  }}
  if (input.event.hook !== 'after_call' || !input.input.arguments || !input.input.arguments.url) {{
    return {{ tools, state: input.state }};
  }}
  if (input.results) {{
    return {{ tools, text: input.input.text,
             state: {{ ready: true, called: input.results.map(function (r) {{ return r.tool; }}), fetched: input.results[0].raw }} }};
  }}
  if (input.state && input.state.ready) return {{ tools, text: input.input.text, state: input.state }};
  return {{ tools, state: input.state,
           calls: [{{ tool: 'api_fetch', arguments: input.input.arguments }},
                   {{ tool: 'api_search', arguments: SEARCH }}] }};
}}"""
    return {"id": "read_api", "scope": [service], "hooks": ["after_call", "tool_discovery"],
            "summary": "Read authorized endpoints through the original capabilities",
            "content": "Read API data through api_fetch and keep the real fields for later steps.",
            "component": "E", "source": source}


def output_folder():
    folder = ROOT / ".offline_checks" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    version = experiment_version(folder)
    save_json(folder / "workflow_audit.json",
              {"script_status": "clear", "experiment_version": version,
               "reviewed_files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                  for name in VERSION_FILES}})
    return folder, version


def host_checks(checks, cfg):
    host_dir = ROOT / cfg["host"]["host_dir"]
    host_env = {**os.environ, "PATH": cfg["host"]["node_bin_dir"] + os.pathsep + os.environ["PATH"]}
    for command in (["npm", "run", "typecheck"], ["npm", "run", "selftest"]):
        subprocess.run(command, cwd=host_dir, env=host_env, check=True)


async def unit_checks(checks, folder, version, cfg):
    """秒级检查：不重放先导轨迹，覆盖协议、预算、断点恢复、状态机与观测账本。"""
    host_checks(checks, cfg)
    plan = prepare_plan()
    checks.check(POLICY["execution_output_tokens"] == 393216, "执行输出额度没有统一到官方上限")
    checks.rejects(ExecutionBlocked, require_model_access, "实际模型调用未被禁止")
    checks.rejects(ValueError, lambda: validate_scope(["sales", "GLOBAL"]), "混合作用域必须被拒绝")
    checks.check(not set(plan["search"]) & set(plan["selection"]) and not set(plan["heldout"]) & set(plan["search"] + plan["selection"]), "任务集合发生交叉")
    checks.check(len(plan["search"]) == 30 and len(plan["heldout"]) == 30, "开发材料或最终留出覆盖不完整")
    BenchmarkAdapter, decide, promotion_review = starharness()
    BenchmarkAdapter.validate_scenario_sets(None, plan["search"], plan["selection"], plan["heldout"])
    Reflector, Curator, *_ = official_ace()
    checks.check(Reflector.__module__.startswith("ace.") and Curator.__module__.startswith("ace."), "ACE 未加载官方实现")
    checks.check(callable(decide) and callable(promotion_review), "StarHarness 选择与判定函数未接入")
    for module in ("engine.extension_protocol", "engine.research_evaluator", "engine.service_quota",
                   "engine.research_retest", "engine.component_sandbox"):
        checks.check(importlib.import_module(module) is not None, "改造后的模块无法导入：" + module)
    from engine.extension_protocol import validate_extension
    from engine.service_quota import live_quota
    checks.check(callable(validate_extension) and callable(live_quota), "扩展协议或服务额度接口缺失")
    checks.rejects(ValidationError, lambda: validate_extension({"unknown": 1}), "扩展协议没有拒绝未知字段")
    checks.rejects(ValueError, lambda: validate_extension(
        {"tools": [{"name": "not an identifier", "description": "x", "parameters": {"type": "object"}}]}),
        "扩展协议没有拒绝非标识符函数名")
    if "GNOSIS_QUOTA_URL" not in os.environ:
        checks.rejects(ExecutionBlocked, live_quota, "缺少实时额度接口时应停止而非猜测")

    unit = {"id": "read_api", "scope": ["google_sheets"], "hooks": ["after_call"],
            "summary": "read", "content": "read", "component": None, "source": None}
    checks.rejects(ValidationError, lambda: EditableUnit.model_validate({**unit, "scope": ["x", "GLOBAL"]}), "工作区必须拒绝混合作用域")
    library = UnitLibrary(folder / "legacy_units")
    checks.rejects(AIFormatError, lambda: library.propose({**unit, "source_task": "t", "prediction": "p",
                                                           "replaces": []}), "提议入口必须拒绝混合作用域")
    second = {**unit, "id": "second_unit"}
    checks.check(changed_units([unit], [{**unit, "scope": ["GLOBAL"]}]) != [], "作用域修改没有计入影响")
    checks.check(len(changed_units([unit, second], [second, unit])) == 2, "组件顺序改变没有计入影响")
    checks.check(changed_units([unit, second], [unit]) == [second], "组件删除没有计入影响")

    workspace = EvolutionWorkspace(folder / "unit_workspace", version)
    checks.rejects(ActionRejected, workspace.require_mature, "来源失败时禁止独立验证")
    checks.rejects(ValueError, lambda: workspace.source_succeeds("t", "wrong-version"), "来源成功记录必须属于当前工作版本")
    frozen = workspace.freeze()
    checks.rejects(ActionRejected, lambda: workspace.edit([unit]), "冻结后仍可继续学习")
    checks.check(await train_ace(plan, workspace, None, None) == frozen, "冻结后的继续执行入口没有直接返回冻结版本")

    meter = Budget(folder / "budget.sqlite", None, output=folder)
    research_budget = ResearchBudget(meter, 0)
    responses = saved_responses(prior_baseline())
    truncated = [r for r in responses if r.choices[0].finish_reason == "length"]
    normal = [r for r in responses if r.choices[0].finish_reason == "stop"]
    checks.check(bool(truncated) and bool(normal), "没有真实截断和正常完成记录")
    checks.check(all(response_termination(r) == "truncated" for r in truncated), "真实截断被误判为完成")
    checks.check(all(response_termination(r) == "completed" for r in normal), "正常完成状态被误判")
    prompt = prior_baseline()[0]["prompt"]
    call_id = research_budget.reserve("learning/gnosis/execution/offline", prompt, POLICY["execution_output_tokens"])
    cost = research_budget.settle(call_id, normal[0].usage)
    checks.check(cost > 0 and research_budget.state()["remaining_usd"] is None, "账本没有按真实 usage 计费或仍设有总额上限")
    checks.check(research_budget.reserve("learning/gnosis/proposal", prompt, 30_000_000) is not None, "无上限账本拒绝了大额请求")
    checks.rejects(ValueError, lambda: research_budget.reserve("contingency/gnosis/proposal", prompt, 1000), "未登记用途的请求未被拒绝")
    with meter.connect() as db:
        db.execute("UPDATE calls SET cost=? WHERE cost IS NULL", (ARM_LEARNING_LIMITS["gnosis"],))
    checks.rejects(BudgetExceeded, lambda: research_budget.reserve("learning/gnosis/proposal", prompt, 1000), "组学习额度用完后没有结束学习")
    checks.check(research_budget.reserve("evaluation/gnosis/execution/offline", prompt, 1000) is not None, "组额度错误地限制了留出评估")
    def task_guard(db, amount, reason):
        raise TaskCostExceeded("单题剩余额度不足")
    checks.rejects(TaskCostExceeded, lambda: research_budget.reserve("evaluation/gnosis/execution/offline", prompt,
                   POLICY["execution_output_tokens"], guard=task_guard), "单题剩余额度没有参与请求前判定")

    research_budget = ResearchBudget(Budget(folder / "model_budget.sqlite", None, output=folder), 0)
    model = ResearchModel(research_budget, version)
    messages = [{"role": "user", "content": "offline"}]
    options = {"max_tokens": 100}
    missing = research_budget.reserve("learning/gnosis/proposal", messages, 100)
    checkpoint = folder / "requests" / "resume_missing.json"
    save_json(checkpoint, {"version": version, "messages": messages, "options": options,
                           "status": "inflight", "call": missing})
    checks.check(model.resume(checkpoint, messages, options, None) is None, "缺少响应的断点没有退回重发")
    with research_budget.meter.connect() as db:
        released = db.execute("SELECT cost, note FROM calls WHERE id=?", (missing,)).fetchone()
    checks.check(released[0] == 0.0 and released[1], "退回重发时没有按未计费释放预留")
    for label, response in (("complete", normal[0]), ("truncated", truncated[0])):
        saved_call = research_budget.reserve("learning/gnosis/proposal", messages, 100)
        io = folder / "io" / saved_call
        io.mkdir(parents=True)
        (io / "response.json").write_text(response.model_dump_json(indent=2), encoding="utf-8")
        saved = folder / "requests" / ("resume_" + label + ".json")
        save_json(saved, {"version": version, "messages": messages, "options": options, "status": "inflight", "call": saved_call})
        if label == "truncated":
            checks.rejects(ExecutionTruncated, lambda s=saved: model.resume(s, messages, options, None), "断点恢复没有复查终止原因")
        else:
            checks.check(model.resume(saved, messages, options, None).choices[0].finish_reason == "stop", "断点恢复没有取得真实响应")
            with research_budget.meter.connect() as db:
                before_calls = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            checks.check(model.resume(saved, messages, options, None).choices[0].finish_reason == "stop", "完成断点没有按记录返回")
            with research_budget.meter.connect() as db:
                checks.check(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == before_calls,
                             "完成断点的二次恢复新增了账本记录")

    # 提议断点在并发账本动态计费字段（budget, arm_remaining_usd）微小漂移时无损复用；核心业务字段变化则拒绝。
    dyn_saved = folder / "requests" / "resume_dynamic.json"
    dyn_call = research_budget.reserve("learning/gnosis/proposal", messages, 100)
    (folder / "io" / dyn_call).mkdir(parents=True, exist_ok=True)
    ((folder / "io" / dyn_call) / "response.json").write_text(normal[0].model_dump_json(indent=2), encoding="utf-8")
    saved_msg = [{"role": "system", "content": "prompt"},
                 {"role": "user", "content": json.dumps({"working_version": "v1", "budget": {"spent_usd": 1.0}, "arm_remaining_usd": 7.5})}]
    save_json(dyn_saved, {"version": version, "messages": saved_msg, "options": options, "status": "inflight", "call": dyn_call})
    drifted_msg = [{"role": "system", "content": "prompt"},
                   {"role": "user", "content": json.dumps({"working_version": "v1", "budget": {"spent_usd": 1.05}, "arm_remaining_usd": 7.45})}]
    checks.check(model.resume(dyn_saved, drifted_msg, options, None, allow_budget_drift=True).choices[0].finish_reason == "stop", "账本动态计费字段微小漂移后未无损复用")
    changed_msg = [{"role": "system", "content": "prompt"},
                   {"role": "user", "content": json.dumps({"working_version": "v2", "budget": {"spent_usd": 1.05}, "arm_remaining_usd": 7.45})}]
    checks.rejects(ValueError, lambda: model.resume(dyn_saved, changed_msg, options, None, allow_budget_drift=True), "核心版本变化时未拦截")

    # 模式检查：断点续跑模式与新任务模式的边界与权限守卫
    req_options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 1}
    os.environ["GNOSIS_RESUME_ONLY"] = "1"
    try:
        missing_cp = folder / "requests" / "nonexistent.json"
        checks.rejects(FileNotFoundError, lambda: model.request(messages, "learning/gnosis/proposal", missing_cp, options=req_options),
                       "断点续跑模式下缺少断点未硬失败")
        empty_cp = folder / "requests" / "empty_cp.json"
        empty_call = research_budget.reserve("learning/gnosis/proposal", messages, req_options["max_tokens"])
        save_json(empty_cp, {"version": version, "messages": messages, "options": req_options, "status": "inflight", "call": empty_call})
        with research_budget.meter.connect() as db:
            pending_before = db.execute("SELECT * FROM calls WHERE id=?", (empty_call,)).fetchone()
        checks.rejects(RuntimeError, lambda: model.request(messages, "learning/gnosis/proposal", empty_cp, options=req_options),
                       "断点续跑模式下缺少响应记录未硬失败")
        with research_budget.meter.connect() as db:
            checks.check(db.execute("SELECT * FROM calls WHERE id=?", (empty_call,)).fetchone() == pending_before,
                         "严格续跑缺少响应时改变了账本预留")
    finally:
        os.environ.pop("GNOSIS_RESUME_ONLY", None)

    checks.rejects(ExecutionBlocked, lambda: model.request(messages, "learning/gnosis/proposal", missing_cp, options=req_options),
                   "新任务模式下未命中断点且禁用模型时未被权限守卫拦截")

    # 参数无法解析的调用不经过 call_tool，模型看到的这条观测仍必须留下 after_call 边界。
    dataset = get_domain_dataset("sales")
    dataset_one, task_row = dataset_row(dataset, "sales.sheets_reconciliation")
    state = await setup(dataset_one, task_row, folder)
    broken = AssistantMessage(role="assistant", content="",
                              tool_calls=[ToolCall(id="broken_call", name="api_fetch", arguments="{not json")])
    records = await environment(dataset_one, task_row, folder).env_response([*state["prompt"], broken], state)
    checks.check(len(records) == 1 and records[0].tool_call_id == "broken_call", "参数无法解析的调用没有返回错误消息")
    events = [e for e in state["boundary_trace"] if e["hook"] == "after_call"]
    checks.check(len(events) == 1 and events[0]["tool"] == "api_fetch", "未经 call_tool 的观测没有留下 after_call 边界")
    checks.check(any(a.get("rejected") for a in state["actual_tool_actions"]), "未经 call_tool 的观测没有登记实际动作")


async def run(full_replay=False):
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"
    before_ledger = ledger_snapshot()
    checks = OfflineChecks()
    folder, version = output_folder()
    cfg = config()
    await unit_checks(checks, folder, version, cfg)

    prior = prior_baseline()
    datasets = {domain: get_domain_dataset(domain) for domain in {r["domain"] for r in prior}}
    replayed = []
    if full_replay:
        sample = prior
    else:
        # 快速模式保留一道含读取与搜索的任务，另加一道来源成功题，够支撑后面的机制检查。
        sample = [prior[0], *[r for r in prior if r["success"] and r is not prior[0]][:1]]
    errors, searches, raised, unrouted, defects = 0, 0, 0, 0, 0
    verified, regenerated, unexplained = [], [], []
    for old in sample:
        dataset_one, row = dataset_row(datasets[old["domain"]], old["task"])
        actual, state, output = await replay(environment(dataset_one, row, folder), row, old, version, [])
        if actual["success"] == old["success"] and actual["partial_credit"] == old["partial_credit"]:
            verified.append(old["task"])
        elif regenerated_identifier_divergence(old, actual):
            regenerated.append(old["task"])
        else:
            unexplained.append(old["task"])
        tool_events = [e for e in state["boundary_trace"] if e["hook"] == "after_call"]
        checks.check(len(tool_events) == len(output), "工具观测没有完整经过 after_call：" + old["task"])
        checks.check(any(e["hook"] == "tool_discovery" for e in state["boundary_trace"]), "工具目录未经过发现边界")
        checks.check(not state.get("harness_defects"),
                     "接入层改写了原装行为：" + json.dumps(state.get("harness_defects"), ensure_ascii=False))
        for event in tool_events:
            try:
                payload = json.loads(event["raw"]) if event["tool"] in ("api_fetch", "api_search") and event["raw"] else None
            except json.JSONDecodeError:
                payload = None
            errors += isinstance(payload, dict) and "error" in payload
            searches += event["tool"] == "api_search"
            raised += isinstance(payload, dict) and "raised" in payload
        unrouted += sum(1 for action in state["actual_tool_actions"] if action.get("rejected"))
        defects += len(state.get("harness_defects", []))
        replayed.append(actual)
        if len(replayed) % 10 == 0:
            print(f"[offline] 已完成原装轨迹重放 {len(replayed)}/{len(sample)}", flush=True)
    checks.check(len(verified) + len(regenerated) + len(unexplained) == len(sample), "重放任务覆盖不完整")
    checks.check(not unexplained, "真实工具重放没有重现原装评分，且差异不能归因于重新生成的标识符：" + ", ".join(unexplained))
    checks.check(errors > 0 and searches > 0, "先导重放没有覆盖真实错误与搜索响应")
    checks.check(raised > 0, "原装工具抛出的异常没有留下调用边界")
    if full_replay:
        checks.check(len(verified) >= 15, "逐项一致的评分重放覆盖不足")
    print(f"[offline] 原装评分重放：{len(verified)} 题逐项一致，{len(regenerated)} 题因服务端标识符重新生成不可比"
          f"（{errors} 个错误响应，{searches} 次搜索，{raised} 次原装异常，{unrouted} 条未经 call_tool 的观测）", flush=True)

    verified_set = set(verified)
    chosen = next(r for r in replayed if r["task"] in verified_set
                  and any(a["tool"] == "api_fetch" and str((a["arguments"].get("method") or "")).upper() == "GET"
                          for a in r["actual_tool_actions"]))
    first_read = next(a for a in chosen["actual_tool_actions"]
                      if a["tool"] == "api_fetch" and str((a["arguments"].get("method") or "")).upper() == "GET")
    search_call = next((a for a in chosen["actual_tool_actions"] if a["tool"] == "api_search"), None)
    checks.check(search_call is not None, "来源任务里没有可复用的真实搜索调用")
    service, endpoint = ScopedAutomationEnv.call_location("api_fetch", first_read["arguments"])
    unit = executable_unit(service, first_read["arguments"], search_call["arguments"])
    workspace = EvolutionWorkspace(folder / "workspace", version)
    working = workspace.edit([unit])
    checks.check(workspace.state["working"] == working and workspace.state["released"] != working, "工作版本与发布版本未分开")
    checks.check(EvolutionWorkspace(workspace.root, version).load_version(working) == [unit], "部分改动没有持久保留")

    dataset_one, row = dataset_row(datasets[chosen["domain"]], chosen["task"])
    injector = ScopedInjector(VersionLibrary(workspace, working), cfg)
    try:
        state = await setup(dataset_one, row, folder, injector)
        base_state = await setup(dataset_one, row, folder)
        checks.check(state["prompt"] == base_state["prompt"], "局部组件在激活前改变提示")
        checks.check(state["tool_defs"] == base_state["tool_defs"], "局部组件在激活前改变工具目录")
        checks.check(not injector.hosts and not state.get("operation_tools"), "局部组件在初始化阶段提前执行")
        response = await tool_call(environment(dataset_one, row, folder, injector), state,
                                   "api_fetch", first_read["arguments"], "offline_read")
        checks.check(len(state["operation_tools"]) == 1, "激活后没有登记可调用操作函数")
        checks.check(list(state["active_extensions"]) == [unit["id"]], "组件首次激活位置没有登记")
        checks.check(unit["id"] in state["first_effect_boundaries"], "组件实际生效位置没有登记")
        calls = [a for a in state["actual_tool_actions"] if a.get("component_call")]
        checks.check(len(calls) == 2 and {c["tool"] for c in calls} == {"api_fetch", "api_search"}, "组件能力调用没有登记")
        checks.check(state["component_state"][unit["id"]]["ready"] is True, "组件没有取回真实调用结果")
        interventions = [e for e in state.get("injection_events", []) if e.get("component") == "E"]
        checks.check(interventions and all("next_actual_tool_action" in e for e in interventions),
                     "组件干预没有关联后续实际动作")
        discoveries = [e for e in state["boundary_trace"] if e["hook"] == "tool_discovery" and e["tool"] == "api_search"]
        checks.check(any(e["endpoint"] for e in discoveries), "组件发起的搜索没有登记发现位置")
        operation = next(iter(state["operation_tools"]))
        await checks.arejects(JsonSchemaValidationError, lambda: tool_call(environment(dataset_one, row, folder, injector), state,
                              operation, {"method": "GET"}, "offline_bad"), "操作函数参数校验没有生效")
        result = await tool_call(environment(dataset_one, row, folder, injector), state,
                                 operation, first_read["arguments"], "offline_operation")
        checks.check(json.loads(result.content) == json.loads(response.content), "操作函数没有通过原装能力取得真实数据")
        other = next(r for r in replayed if r["task"] != chosen["task"])
        other_dataset, other_row = dataset_row(datasets[other["domain"]], other["task"])
        checks.check(not (await setup(other_dataset, other_row, folder, injector)).get("operation_tools"), "操作函数跨任务泄漏")
    finally:
        injector.close()

    denied_unit = {**unit, "id": "denied_read", "scope": [endpoint], "hooks": ["after_call"],
                   "source": "export function onBoundary(input) { return { text: process.env.HOME }; }"}
    denied_workspace = EvolutionWorkspace(folder / "denied_workspace", version)
    denied_version = denied_workspace.edit([denied_unit])
    denied_injector = ScopedInjector(VersionLibrary(denied_workspace, denied_version), cfg)
    try:
        denied_state = await setup(dataset_one, row, folder, denied_injector)
        denied = await tool_call(environment(dataset_one, row, folder, denied_injector), denied_state,
                                 "api_fetch", first_read["arguments"], "offline_denied")
        checks.check("[GNOSIS 隔离]" in denied.content, "权限拒绝没有返回可见反馈")
        checks.check(json.loads(denied.content.split("[GNOSIS 隔离]")[0].rstrip()) == json.loads(response.content),
                     "拒绝后原始失败状态被改写")
        checks.check((denied_workspace.root / "permission_denials.jsonl").exists(), "权限拒绝没有留下记录")
    finally:
        denied_injector.close()

    # 组件发起的能力调用同样要触发其他组件的干预。
    observer_unit = {**unit, "id": "observer_read", "scope": [endpoint], "hooks": ["after_call", "tool_discovery"],
                     "source": "export function onBoundary(input) { if (input.event.hook !== 'after_call') return { state: input.state }; return { state: {seen: true}, text: (input.input.text || '') + '\\n[observer]' }; }"}
    paired_workspace = EvolutionWorkspace(folder / "propagation_workspace", version)
    paired_version = paired_workspace.edit([unit, observer_unit])
    paired_injector = ScopedInjector(VersionLibrary(paired_workspace, paired_version), cfg)
    try:
        paired_state = await setup(dataset_one, row, folder, paired_injector)
        paired_response = await tool_call(environment(dataset_one, row, folder, paired_injector), paired_state,
                                          "api_fetch", first_read["arguments"], "offline_propagation")
        checks.check("[observer]" in paired_response.content, "其他组件没有干预受测 AI 看到的观测")
        checks.check(observer_unit["id"] in paired_state.get("active_extensions", {}),
                     "其他组件没有在组件发起的调用上登记激活位置")
        checks.check("[observer]" in (paired_state["component_state"].get(unit["id"], {}).get("fetched") or ""),
                     "组件发起的能力调用没有触发其他组件干预")
    finally:
        paired_injector.close()

    error_pair = next(((r, a) for r in replayed if r["task"] in verified_set for a in r["actual_tool_actions"]
                       if a["tool"] == "api_fetch" and a["failed"] and "raised" not in (a["raw"] or "")), None)
    checks.check(error_pair is not None, "先导重放没有产生错误响应")
    error_record, error_call = error_pair
    error_dataset, error_row = dataset_row(datasets[error_record["domain"]], error_record["task"])
    error_prior = next(old for old in prior if old["task"] == error_record["task"])
    _, error_endpoint = ScopedAutomationEnv.call_location("api_fetch", error_call["arguments"])
    error_unit = {**unit, "id": "error_feedback", "scope": [error_endpoint], "hooks": ["after_call"],
                  "source": "export function onBoundary(input) { return { state: JSON.parse(input.input.text) }; }"}
    error_workspace = EvolutionWorkspace(folder / "error_workspace", version)
    error_version = error_workspace.edit([error_unit])
    error_injector = ScopedInjector(VersionLibrary(error_workspace, error_version), cfg)
    try:
        _, error_state, _ = await replay(environment(error_dataset, error_row, folder, error_injector),
                                         error_row, error_prior, version, [error_unit])
        matched = next(a for a in error_state["actual_tool_actions"]
                       if a["tool"] == "api_fetch" and a["arguments"] == error_call["arguments"] and a.get("failed"))
        checks.check(matched["raw"] == error_call["raw"], "错误响应反馈与原始记录不一致")
        checks.check("error" in error_state["component_state"][error_unit["id"]], "错误观测未进入组件")
    finally:
        error_injector.close()

    endpoint_unit = {**unit, "id": "endpoint_read", "hooks": ["after_call"]}
    plans = validation_plan([], [endpoint_unit], replayed, version)
    global_plans = validation_plan([], [endpoint_unit], replayed, version, global_validation=True)
    checks.check(0 < sum(p["affected"] for p in plans) < len(global_plans), "局部验证没有复用作用域外证据")
    checks.check(all(p["affected"] for p in global_plans), "全局验证没有重跑全部任务")
    for record, item in zip(replayed, plans, strict=True):
        if not item["affected"]:
            reused = reuse_evidence(record, [endpoint_unit], item)
            checks.check(reused["success"] == record["success"] and reused["validation_cost_usd"] == 0
                         and reused["reused_from"] == record["execution_id"], "证据复用篡改成绩、来源或重复计费")
        else:
            checks.rejects(ValueError, lambda r=record, p=item: reuse_evidence(r, [endpoint_unit], p), "受影响证据必须重跑")
    legacy_record = {k: v for k, v in replayed[0].items() if k not in ("boundary_schema", "boundary_trace")}
    checks.check(validation_plan([], [endpoint_unit], [legacy_record], version)[0]["affected"], "缺失边界的旧证据被错误复用")
    checks.check(all(p["affected"] for p in validation_plan([], [{**unit, "scope": ["GLOBAL"], "hooks": ["task_start"]}], replayed, version)), "GLOBAL 未触发全量验证")

    # 来源成功题单独建一套组件与工作区：成熟条件、发布门与边界预测都在这一套证据上核对。
    successful = next(r for r in replayed if r["task"] in verified_set and r["success"])
    source_prior = next(old for old in prior if old["task"] == successful["task"])
    source_dataset, source_row = dataset_row(datasets[successful["domain"]], successful["task"])
    source_read = next(a for a in successful["actual_tool_actions"]
                       if a["tool"] == "api_fetch" and str((a["arguments"].get("method") or "")).upper() == "GET")
    source_search = next((a for a in successful["actual_tool_actions"] if a["tool"] == "api_search"), None)
    checks.check(source_search is not None, "来源成功题里没有可复用的真实搜索调用")
    source_service, _ = ScopedAutomationEnv.call_location("api_fetch", source_read["arguments"])
    source_unit = executable_unit(source_service, source_read["arguments"], source_search["arguments"])
    source_workspace = EvolutionWorkspace(folder / "source_workspace", version)
    source_working = source_workspace.edit([source_unit])
    before_records = []
    for repeat in (0, 1):
        record, _, _ = await replay(environment(source_dataset, source_row, folder), source_row,
                                    source_prior, version, [], repeat=repeat)
        before_records.append(record)
    checks.check(components_version([]) == before_records[0]["version"], "对照版本不是空的整组组件")
    after_records = []
    for repeat in (0, 1):
        source_injector = ScopedInjector(VersionLibrary(source_workspace, source_working), cfg)
        try:
            record, _, _ = await replay(environment(source_dataset, source_row, folder, source_injector),
                                        source_row, source_prior, version, [source_unit], repeat=repeat)
        finally:
            source_injector.close()
        after_records.append(record)
    for record in after_records:
        source_workspace.observe(record)
        if record["success"]:
            source_workspace.source_succeeds(successful["task"], record["version"])
    if after_records[0]["success"]:
        checks.check(source_workspace.state["source_successes"].get(successful["task"]) == source_working, "来源题成功后没有记录成熟条件")
        source_workspace.require_mature()
    comparison = paired_selection(before_records, after_records, [successful["task"]], 2)
    checks.rejects(ValueError, lambda: paired_selection(before_records, after_records, [successful["task"]], 1), "复测次数不对称未被拒绝")
    checks.rejects(ValueError, lambda: paired_selection([before_records[0], before_records[0]], after_records,
                                                        [successful["task"]], 2), "复用了同一次执行填充复测")
    comparison.update(validation_cost_usd=0.0, reused_count=0, executed_count=2)
    checks.check(comparison["before_version"] == source_workspace.state["released"] and comparison["after_version"] == source_workspace.state["working"], "配对结果与整组版本不一致")
    source_workspace.publish(comparison)
    if comparison["decision"] == "accepted":
        checks.check(source_workspace.state["released"] == source_working, "接受后没有推进发布版本")
    else:
        checks.check(source_workspace.state["released"] != source_working, "未接受却推进了发布版本")
    source_workspace.freeze()

    retest_workspace = EvolutionWorkspace(folder / "retest_workspace", version)
    distant_version = retest_workspace.edit([unit])

    chosen_prior = next(old for old in prior if old["task"] == chosen["task"])
    chosen_before = []
    for repeat in (0, 1):
        record, _, _ = await replay(environment(dataset_one, row, folder), row, chosen_prior, version, [],
                                    repeat=repeat)
        chosen_before.append(record)

    # 证据集包含两道题：候选只在其中一道上激活，另一道必须被复用，复用准确性才有可比对象。
    retest_records = chosen_before + before_records
    retest_tasks = [chosen["task"], successful["task"]]

    async def retest_execute(task, repeat, label):
        target_prior = next(old for old in prior if old["task"] == task)
        target_dataset, target_row = dataset_row(datasets[target_prior["domain"]], task)
        retest_injector = ScopedInjector(VersionLibrary(retest_workspace, distant_version), cfg)
        try:
            record, _, _ = await replay(environment(target_dataset, target_row, folder, retest_injector),
                                        target_row, target_prior, version, [unit], repeat=repeat)
        finally:
            retest_injector.close()
        return record

    # 核心读数：在未包含该组件的历史轨迹上预测的首次生效位置，必须等于带组件执行时的实测首次激活位置。
    predicted = validation_plan([], [source_unit], before_records, version, global_validation=False)[0]["first_boundary"]
    observed = [record["active_extensions"].get(source_unit["id"]) for record in after_records]
    checks.check(all(value is not None for value in observed), "组件在真实执行里没有登记首次激活位置")
    checks.check(predicted is not None and predicted in observed,
                 f"预测的首次生效位置与实测不一致：{predicted} vs {observed}")

    retest = await retest_comparison([], [unit], retest_records, version, retest_tasks, 2, retest_execute)
    checks.check(retest["reuse_accuracy"]["compared"] == 2 and retest["reuse_accuracy"]["rate"] is not None,
                 "证据复用准确性没有逐题比较")
    checks.check(retest["scoped"]["reused_count"] == 2 and retest["scoped"]["executed_count"] == 2,
                 "按影响范围重测没有按影响面划分复用与重跑")
    checks.check(retest["global"]["reused_count"] == 0 and retest["global"]["executed_count"] == 4,
                 "全局重测没有重跑全部任务")

    checks.check(ledger_snapshot() == before_ledger, "离线验证改变了真实费用账本")
    proof = {"status": "passed", "mode": "mechanism+full_replay" if full_replay else "mechanism",
             "boundary_prediction": {"predicted": predicted, "observed": observed},
             "reuse_accuracy": retest["reuse_accuracy"],
             "experiment_version": version, "checks": checks.count,
             "full_tool_replay": full_replay, "tool_replay_tasks": len(replayed),
             "score_verified_tasks": len(verified), "regenerated_identifier_tasks": regenerated,
             "error_responses": errors, "search_responses": searches, "raised_tool_errors": raised,
             "unrouted_observations": unrouted, "harness_defects": defects,
             "model_calls": 0, "real_ledger_unchanged": True, "at": time.time()}
    save_json(ROOT / "state/research_offline_check.json", proof)
    print("[offline] " + json.dumps(proof, ensure_ascii=False), flush=True)


async def unit_only():
    """只跑秒级单元检查，用于修复迭代；不写准入证明。"""
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"
    checks = OfflineChecks()
    folder, version = output_folder()
    await unit_checks(checks, folder, version, config())
    print(f"[offline] 单元检查通过：{checks.count} 条", flush=True)
