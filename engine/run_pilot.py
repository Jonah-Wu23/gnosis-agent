import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import subprocess
import time
import uuid
import fcntl
from pathlib import Path

from dotenv import load_dotenv

os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"

from engine.scoped_units import Budget, BudgetExceeded, UnitLibrary, append_record, fingerprint, affected_tasks, POLICY, experiment_version, AIFormatError, CallJournal, TaskCostExceeded, save_json, harness_blocker, require_harness_ready, ExecutionBlocked, ExecutionTruncated, ProposalInterrupted, require_model_access

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "experiments/pilot"
DIRTY_MARKER = ROOT / "archive/2026-10-06/VOID.json"
SELECTION = ROOT / "config/pilot_selection.json"
WORLD_SELECTION = ROOT / "config/appworld_selection.json"
BUDGET_LEDGER = ROOT / "state/pilot_budget.sqlite"


def config():
    # 配置只在内存使用，不输出端点、模型名、凭据或配置内容。
    return json.loads((ROOT / "config/runtime.json").read_text(encoding="utf-8"))


def budget():
    return Budget(BUDGET_LEDGER, limit=POLICY["pilot_limit_usd"], rates=config()["rate_card"], output=OUT,
                  window_limit=POLICY["window_limit_usd"], window_seconds=POLICY["window_seconds"],
                  cap=POLICY["total_limit_usd"])


def read_records(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return [json.loads(line) for line in stream]


def execution_ledger(arm, version):
    folder = OUT / "runs" / version
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (arm + ".jsonl")


def baseline_records():
    version = experiment_version(OUT)
    return [r for r in read_records(execution_ledger("base", version))
            if r["experiment_version"] == version and r["version"] == fingerprint([])
            and r["learning_role"] == "learning"]


def require_baseline():
    records = baseline_records()
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    planned = {task for tasks in selected["domains"].values() for task in tasks}
    if len(records) != len(planned) or {r["task"] for r in records} != planned:
        raise ExecutionBlocked("本版本的干净 AutomationBench 基线未完成：先跑完 30 题，再启动提议与先导")
    return records


def execution_failure(error):
    from openai import APIConnectionError, APIStatusError
    from engine.component_host import CandidateError
    from verifiers.errors import InvalidModelResponseError, ToolParseError

    if not isinstance(error, BaseException):
        return RuntimeError(str(error)), "harness_or_integration_error"
    cause = error
    while cause.__cause__ is not None and not isinstance(cause, (
            CandidateError, TaskCostExceeded, BudgetExceeded, ExecutionBlocked,
            InvalidModelResponseError, ToolParseError, ExecutionTruncated)):
        cause = cause.__cause__
    if isinstance(cause, ExecutionTruncated):
        category = "execution_truncated"
    elif isinstance(cause, ExecutionBlocked):
        category = "execution_blocked"
    elif isinstance(cause, BudgetExceeded):
        category = "budget_stop"
    elif isinstance(cause, (APIConnectionError, APIStatusError)):
        category = "service_connection_failure"
    elif isinstance(cause, (CandidateError, InvalidModelResponseError, ToolParseError)):
        category = "ai_code_or_format_error"
    elif isinstance(cause, TaskCostExceeded):
        category = "task_cost_cap"
    else:
        category = "harness_or_integration_error"
    return cause, category


def learning_records(task):
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    permitted = {t for tasks in selected["domains"].values() for t in tasks}
    if task not in permitted:
        raise PermissionError("学习任务未获得先导授权")
    # 学习材料只取本版本且具有学习授权的执行记录。
    records = []
    version = experiment_version(OUT)
    for path in sorted((OUT / "runs" / version).glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        records.extend(r for r in read_records(path) if r["task"] == task and r["learning_role"] == "learning")
    return sorted(records, key=lambda r: r.get("recorded_at", 0))


def prepare_automation():
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values

    selection = {"seed": 100, "domains": {}, "contracts": {}, "task_contracts": {}}
    for domain in ("sales", "operations", "hr", "marketing", "support", "finance"):
        dataset = get_domain_dataset(domain)
        indices = list(range(len(dataset)))
        random.Random(100).shuffle(indices)
        rows = [dataset[i] for i in indices[:5]]
        names = []
        for row in rows:
            info = row["info"]
            if isinstance(info, str):
                info = json.loads(info)
            info = strip_none_values(info)
            name = info["task_name"]
            names.append(name)
            selection["contracts"][name] = compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"])
            selection["task_contracts"][name] = fingerprint(row)
        selection["domains"][domain] = names
    selection["automation_commit"] = subprocess.check_output(["git", "-C", str(ROOT / "scratch/AutomationBench"), "rev-parse", "HEAD"], text=True).strip()
    selection["ace_commit"] = subprocess.check_output(["git", "-C", str(ROOT / "scratch/ace"), "rev-parse", "HEAD"], text=True).strip()
    path = SELECTION
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != selection:
        raise ValueError("先导任务或上游版本发生变化")
    path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print("AutomationBench 固定选择：6 领域各 5 题", flush=True)


async def automation(args):
    from automationbench.domains import get_domain_dataset
    from automationbench.rubric import create_rubric
    from verifiers.types import ClientConfig
    from pydantic_core import to_jsonable_python
    from engine.automation_adapter import MeteredClient, ScopedAutomationEnv, ScopedInjector, restore_messages
    from engine.component_host import CandidateError

    require_harness_ready(OUT)
    load_dotenv(ROOT / ".env")
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    meter = budget()
    phase = "simple" if args.simple else "automation"
    library = UnitLibrary(OUT / "units")
    pair_tasks = None
    if args.candidate:
        unit = library.read(args.candidate)
        pair_tasks = sorted(set(affected_tasks(unit, selected["contracts"])) - {unit["source_task"]})
        random.Random(selected["seed"]).shuffle(pair_tasks)
        pair_tasks = set(pair_tasks[:8])
    injector = ScopedInjector(library, config(), args.candidate) if library.units(args.candidate) else None
    arm = args.candidate or "base"
    version = experiment_version(OUT)
    if getattr(args, "version", None) and args.version != version:
        raise ValueError("程序版本与实验开始前固定的版本不同")
    ledger = execution_ledger(arm, version)
    current_version = injector.version if injector else fingerprint([])
    done = set() if getattr(args, "rerun", False) else {
        r["task"] for r in read_records(ledger) if r["version"] == current_version}
    client = MeteredClient(ClientConfig(api_key_var="MODEL_API", api_base_url=os.environ["MODER_PORVIDER"]), meter,
                           "pairing" if args.candidate else phase)
    try:
        domains = ("simple",) if args.simple else tuple(selected["domains"])
        if args.domains:
            domains = tuple(d for d in domains if d in args.domains)
        datasets = {}
        worklist = []
        for domain in domains:
            datasets[domain] = get_domain_dataset(domain)
            if args.simple:
                indices = list(range(5))
            else:
                wanted = set(selected["domains"][domain])
                if args.tasks:
                    wanted &= set(args.tasks)
                elif pair_tasks is not None:
                    wanted &= pair_tasks
                indices = [i for i, row in enumerate(datasets[domain])
                           if json.loads(row["info"])["task_name"] in wanted]
            worklist.extend((domain, index) for index in indices)
        # 服务故障与接入层错误不是 AI 的成绩：只留 failures.jsonl 与原始记录，不进成绩账本，
        # 整批结束后对这些题补跑一次，补跑仍在循环中追加。
        service_failed = set()
        for domain, index in worklist:
            row = datasets[domain][index]
            name = json.loads(row["info"])["task_name"]
            if name in done:
                continue
            env = ScopedAutomationEnv(dataset=datasets[domain].select([index]), rubric=create_rubric(),
                                       max_turns=POLICY["max_steps"], toolset="api", injector=injector)
            started = time.monotonic()
            execution_id = uuid.uuid4().hex
            save_json(OUT / "runtime_status.json", {"phase": "execution", "arm": arm,
                      "role": getattr(args, "role", "learning"), "task": name,
                      "experiment_version": version, "at": time.time()})
            try:
                result = await env.evaluate(client=client, model=os.environ["MODER_NAME"],
                                             sampling_args={"reasoning_effort": "high", "temperature": 0,
                                                            "seed": POLICY["seed"], "max_tokens": POLICY["execution_output_tokens"]},
                                             num_examples=1, rollouts_per_example=1, max_concurrent=1,
                                             state_columns=["scope_touches", "injection_events", "_assertion_results", "_perf", "error", "gnosis_cost_usd", "wire_trace", "boundary_trace", "boundary_schema", "termination_status", "actual_tool_actions", "active_extensions", "first_effect_boundaries", "component_capability_calls", "harness_defects"])
            except BudgetExceeded:
                raise
            except Exception as error:
                _, category = execution_failure(error)
                if category == "harness_or_integration_error":
                    harness_blocker(OUT, error, phase, name)
                raise
            record = result["outputs"][0]
            error = record.pop("error", None)
            execution_error = None
            if error:
                cause, category = execution_failure(error)
                execution_error = {"error_type": type(cause).__name__, "error": str(cause), "category": category}
                append_record(OUT / "failures.jsonl", {"phase": phase, "task": name, "version": version,
                                                       **execution_error})
            record = to_jsonable_python(record)
            record["prompt"] = to_jsonable_python(restore_messages(record["prompt"]))
            record["completion"] = to_jsonable_python(restore_messages(record["completion"]))
            raw_path = ledger.parent / "raw" / arm
            raw_path.mkdir(parents=True, exist_ok=True)
            (raw_path / (name + ".json")).write_text(json.dumps(record), encoding="utf-8")
            if execution_error and execution_error["category"] in ("budget_stop", "execution_blocked"):
                raise cause
            if execution_error and execution_error["category"] == "harness_or_integration_error":
                harness_blocker(OUT, cause, phase, name)
                raise cause
            if execution_error and execution_error["category"] == "service_connection_failure":
                if name in service_failed:
                    append_record(OUT / "errors.jsonl", {"version": version, "phase": phase, "task": name,
                                  "category": execution_error["category"], "error": execution_error["error"],
                                  "note": "补跑后仍未完成，不计入成绩账本，该题保持未完成"})
                    print(f"{phase} {domain} {name}: {execution_error['category']} 补跑后仍失败，未计入成绩", flush=True)
                else:
                    service_failed.add(name)
                    worklist.append((domain, index))
                    print(f"{phase} {domain} {name}: {execution_error['category']}，不计入成绩，整批结束后补跑", flush=True)
                continue
            partial = record["metrics"]["partial_credit"]
            success = bool(record["metrics"]["task_completed_correctly"])
            if execution_error:
                success = False
            record = {"task": name, "domain": domain, "success": success, "partial_credit": partial,
                      "version": injector.version if injector else fingerprint([]),
                      "task_contract": fingerprint(row), "touches": record["scope_touches"],
                      "injections": record["injection_events"],
                      "prompt": record["prompt"],
                      "completion": record["completion"],
                      "assertions": record["_assertion_results"],
                      "execution_error": execution_error,
                      "experiment_version": version, "learning_role": getattr(args, "role", "learning"),
                      "recorded_at": time.time(),
                      "execution_id": execution_id,
                      "confirmation_of": getattr(args, "confirmation_of", {}).get(name),
                      "wire_trace": record.get("wire_trace", []),
                      "boundary_trace": record.get("boundary_trace", []),
                      "boundary_schema": record.get("boundary_schema"),
                      "termination_status": record.get("termination_status"),
                      "actual_tool_actions": record.get("actual_tool_actions", []),
                      "active_extensions": record.get("active_extensions", {}),
                      "first_effect_boundaries": record.get("first_effect_boundaries", {}),
                      "component_capability_calls": record.get("component_capability_calls", 0),
                      "perf": record["_perf"], "duration_s": time.monotonic() - started,
                      "cost_usd": record["gnosis_cost_usd"]}
            append_record(ledger, record)
            print(f"{phase} {domain} {name}: pass={success} partial={partial:.3f} cost=${record['cost_usd']:.5f}", flush=True)
    finally:
        await client.close()
        if injector:
            injector.close()


def appworld(args):
    require_harness_ready(OUT)
    os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")
    from appworld import AppWorld, load_task_ids, update_root
    from appworld.evaluator import evaluate_tasks
    from engine.run_experiment import build_agent, build_component_host
    from engine.agent_runtime import load_candidate
    from engine.gnosis_lm import GnosisLM
    from engine.llm_client import client

    update_root(os.environ["APPWORLD_ROOT"])
    meter = budget()
    cfg = config()
    cfg["sampling"]["max_retries"] = POLICY["service_retries"]
    client.max_retries = 0
    candidates = load_candidate(str(ROOT / "candidates/base_v2"))
    task_ids = load_task_ids("test_challenge")
    random.Random(100).shuffle(task_ids)
    task_ids = task_ids[:40]
    selection_path = WORLD_SELECTION
    if selection_path.exists() and json.loads(selection_path.read_text()) != task_ids:
        raise ValueError("AppWorld 任务选择发生变化")
    selection_path.write_text(json.dumps(task_ids), encoding="utf-8")
    if args.task:
        if args.task not in task_ids:
            raise ValueError("任务不在固定先导集合")
        task_ids = [args.task]
    ledger = OUT / "appworld_base.jsonl"
    done = {r["task"] for r in read_records(ledger)
            if r.get("experiment_version") == experiment_version(OUT)}

    class PilotLM(GnosisLM):
        def _chat_once(self, messages, max_tokens=None):
            from openai import APIConnectionError, APIStatusError

            require_model_access()
            journal.verify_version()
            options = {"max_tokens": max_tokens or self.max_tokens, "temperature": self.temperature,
                       "seed": self.seed, "reasoning_effort": "high"}
            call_id = meter.reserve("appworld", messages, options["max_tokens"])
            folder = journal.request(call_id, "appworld", messages, options)
            try:
                response = super()._chat_once(messages, max_tokens)
            except (APIConnectionError, APIStatusError) as error:
                self._service_delay = journal.retry_service(folder, error, self._retry_attempt)
                meter.release(call_id, "服务失败未取得计费响应，按未计费释放")
                raise
            journal.response(folder, response)
            meter.settle(call_id, response.usage)
            return response

        def retry_delay(self, attempt, error):
            return self._service_delay

    journal = CallJournal(OUT, experiment_version(OUT))
    experiment = "gnosis_pilot_20261006_" + journal.version[:12]
    AppWorld.init_defaults.update(experiment_name=experiment, **cfg["appworld_config"])
    with build_component_host(cfg) as host:
        host.load(candidates.name, candidates.component_files)
        agent = build_agent(candidates, cfg, host)
        agent.language_model = PilotLM(rate_card=cfg["rate_card"], max_tokens=cfg["sampling"]["max_tokens"],
                                       temperature=cfg["sampling"]["temperature"], seed=cfg["sampling"]["seed"],
                                       max_retries=POLICY["service_retries"],
                                       retry_after_n_seconds=POLICY["service_retry_delay_seconds"])
        agent.logger.verbose = False
        for task_id in task_ids:
            if task_id in done:
                continue
            before = agent.language_model.total_cost
            started = time.monotonic()
            receipt_path = OUT / "appworld_receipts" / (task_id + ".json")
            receipt_path.parent.mkdir(exist_ok=True)
            with (OUT / "appworld_execution.log").open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log):
                if args.evaluate_existing:
                    from appworld.common.path_store import path_store
                    saved = Path(path_store.experiment_outputs) / experiment / "tasks" / task_id / "misc"
                    usage = json.loads((saved / "usage.json").read_text(encoding="utf-8"))["task"]
                    receipt = {"steps": len(list((saved / "lm_calls").glob("*.json"))),
                               "cost_usd": usage["cost"], "duration_s": None}
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                elif not receipt_path.exists():
                    agent.solve_task(task_id)
                    receipt = {"steps": agent.step_number, "cost_usd": agent.language_model.total_cost - before,
                               "duration_s": time.monotonic() - started}
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                agent.set_finished(experiment, task_id)
                evaluation = evaluate_tasks(task_ids=[task_id], experiment_name=experiment,
                                             include_details=True, save_reports=True)
            task_eval = evaluation["individual"][task_id]
            success = bool(task_eval["success"])
            record = {"task": task_id, "domain": "test_challenge", "success": success,
                      "experiment_version": journal.version,
                      "evaluation": task_eval, **receipt}
            append_record(ledger, record)
            print(f"appworld {task_id}: pass={success} steps={record['steps']} cost=${record['cost_usd']:.5f}", flush=True)


def proposal(args):
    from engine.scope_proposer import propose

    require_harness_ready(OUT)
    version = experiment_version(OUT)
    if getattr(args, "version", None) and args.version != version:
        raise RuntimeError("提议入口的接入程序版本已经改变")
    records = require_baseline()
    history = learning_records(args.task)
    record = history[-1]
    recent = []
    previous_round = round_state().get(getattr(args, "round_index", 0) - 1)
    if previous_round and previous_round.get("unit"):
        recent = [r for r in read_records(execution_ledger(previous_round["unit"], version))
                  if r["learning_role"] == "learning"]
    library = UnitLibrary(OUT / "units")
    previous_unit = library.read(args.candidate) if args.candidate else None
    failures = [r for r in read_records(OUT / "component_failures.jsonl")
                if previous_unit and r["unit"] == previous_unit["id"]]
    unit = propose(record, library, budget(), ROOT / "scratch/ace", previous_unit,
                   failures[-1] if failures else None, [*history[:-1], *recent], records, getattr(args, "repair_root", None),
                   components=getattr(args, "components", POLICY["default_components"]),
                   checkpoint=getattr(args, "proposal_checkpoint", None))
    print(f"候选单元 {unit['id']}，组件={unit['component']}，来源={unit['source_task']}", flush=True)
    return unit


def evaluate_saved(args):
    os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")
    from appworld import update_root
    from appworld.evaluator import evaluate_tasks

    update_root(os.environ["APPWORLD_ROOT"])
    with (OUT / "appworld_execution.log").open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log):
        evaluation = evaluate_tasks(task_ids=[args.task], experiment_name="gnosis_pilot_20261006_" + experiment_version(OUT)[:12],
                                     include_details=True, save_reports=True)
    record = {"task": args.task, "execution_terminated": "budget",
              "evaluation": evaluation["individual"][args.task]}
    append_record(OUT / "interrupted_evaluations.jsonl", record)
    print(json.dumps({"task": args.task, "execution_terminated": "budget",
                      "success": record["evaluation"]["success"]}))


def paired(args, candidate=None):
    """构造配对：候选臂某题第一次执行对基线，输掉的题用后续重跑确认。"""
    library = UnitLibrary(OUT / "units")
    version = experiment_version(OUT)
    unit = library.read(candidate or args.candidate)
    before = {r["task"]: r for r in read_records(execution_ledger("base", version))}
    history = {}
    for record in read_records(execution_ledger(unit["id"], version)):
        history.setdefault(record["task"], []).append(record)
    pairs = []
    for task, records in history.items():
        if task == unit["source_task"] or task not in before:
            continue
        records.sort(key=lambda r: r.get("recorded_at", 0))
        executions = [r for r in records if not r.get("confirmation_of")]
        if not executions:
            raise ValueError("确认复测缺少对应的首次执行记录")
        old, first = before[task], executions[0]
        if old["task_contract"] != first["task_contract"]:
            raise ValueError("配对任务契约发生变化")
        if old["experiment_version"] != first["experiment_version"]:
            raise ValueError("接入程序改变前后的成绩禁止计作 AI 配对改善")
        pair = {"unit": unit["id"], "task": task, "domain": first["domain"],
                "before": old["success"], "after": first["success"],
                "before_partial": old["partial_credit"], "after_partial": first["partial_credit"],
                "before_version": old["version"], "after_version": first["version"],
                "after_execution": first["execution_id"],
                "experiment_version": first["experiment_version"]}
        confirmations = [r for r in records if r.get("confirmation_of") == first["execution_id"]]
        if len(confirmations) > 1:
            raise ValueError("同一配对任务被确认复测超过一次")
        if confirmations:
            confirmation = confirmations[0]
            if confirmation["version"] != first["version"] or confirmation["task_contract"] != first["task_contract"]:
                raise ValueError("确认复测改变了候选版本或任务契约")
            pair["confirm_after"] = confirmation["success"]
        pairs.append(pair)
    return unit, sorted(pairs, key=lambda p: p["task"])


def stage_pairs():
    """阶段末配对：对照固定取空知识库那批成绩，候选取该题最后一次运行，每题只留一个配对。"""
    library = UnitLibrary(OUT / "units")
    version = experiment_version(OUT)
    empty = fingerprint([])
    sources = {u["source_task"] for u in library.units()}
    base = {r["task"]: r for r in read_records(execution_ledger("base", version))
            if r["version"] == empty}
    latest = {}
    for unit in library.units():
        for record in read_records(execution_ledger(unit["id"], version)):
            if record["task"] not in base:
                continue
            previous = latest.get(record["task"])
            if previous is None or record.get("recorded_at", 0) >= previous[0].get("recorded_at", 0):
                latest[record["task"]] = (record, unit["id"])
    pairs = []
    for task, (record, unit_id) in sorted(latest.items()):
        if task in sources:
            continue
        old = base[task]
        if old["task_contract"] != record["task_contract"]:
            raise ValueError("配对任务契约发生变化")
        if old["experiment_version"] != record["experiment_version"]:
            raise ValueError("接入程序改变前后的成绩禁止计作配对改善")
        confirmed = False
        if record.get("confirmation_of"):
            original = next((r for r in read_records(execution_ledger(unit_id, version))
                             if r["execution_id"] == record["confirmation_of"]), None)
            if original is None or original["task"] != task or original["version"] != record["version"]:
                raise ValueError("确认复测缺少同一任务与候选版本的首次执行")
            confirmed = old["success"] and not original["success"] and not record["success"]
        pairs.append({"unit": unit_id, "task": task, "domain": record["domain"],
                      "before": old["success"], "after": record["success"],
                      "confirmed_regression": bool(confirmed), "after_execution": record["execution_id"],
                      "injected_units": sorted({e["unit"] for e in record.get("injections", [])}),
                      "before_version": old["version"], "after_version": record["version"],
                      "experiment_version": record["experiment_version"]})
    return pairs


def gate(args):
    """阶段一准入：源任务由败转胜，影响面内没有确认退化。"""
    library = UnitLibrary(OUT / "units")
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    version = experiment_version(OUT)
    existing = library.read(args.candidate)
    if existing["status"] in ("accepted", "abstained") and existing.get("evidence", {}).get("kind") == "admission":
        if existing["provenance"]["experiment_version"] != version:
            raise ValueError("准入记录不属于当前实验版本")
        library.complete_admission(existing["id"])
        if not any(r.get("unit") == existing["id"] and r.get("kind") == "admission"
                   for r in read_records(OUT / "promotions.jsonl")):
            append_record(OUT / "promotions.jsonl", {"unit": existing["id"], **existing["evidence"]})
        return existing["evidence"]
    unit, pairs = paired(args)
    base = {r["task"]: r for r in read_records(execution_ledger("base", version))}
    history = sorted([r for r in read_records(execution_ledger(unit["id"], version))
                      if r["task"] == unit["source_task"]], key=lambda r: r.get("recorded_at", 0))
    planned_pairs = getattr(args, "gate_tasks", None)
    missing = [] if planned_pairs is None else sorted(set(planned_pairs) - {p["task"] for p in pairs})
    unconfirmed = [p["task"] for p in pairs if p["before"] and not p["after"] and "confirm_after" not in p]
    if not history or unit["source_task"] not in base or missing or unconfirmed:
        evidence = {"kind": "admission", "decision": "abstained", "pairs": pairs,
                    "reasons": ["execution_evidence_incomplete"], "missing_pairs": missing,
                    "unconfirmed_pairs": unconfirmed, "source_pair": None,
                    "confirmed_regressions": [], "retired": [], "experiment_version": version}
        unit.update(status="abstained", evidence=evidence)
        save_json(library.root / unit["id"] / "unit.json", unit)
        append_record(OUT / "promotions.jsonl", {"unit": unit["id"], **evidence})
        print(json.dumps({k: v for k, v in evidence.items() if k != "pairs"}, ensure_ascii=False), flush=True)
        return evidence
    source = {"task": unit["source_task"], "before": base[unit["source_task"]]["success"],
              "after": history[0]["success"]}
    evidence = library.admit(unit["id"], source, pairs, selected["contracts"])
    append_record(OUT / "promotions.jsonl", {"unit": unit["id"], **evidence})
    print(json.dumps({k: v for k, v in evidence.items() if k != "pairs"}, ensure_ascii=False), flush=True)
    return evidence


async def confirm(args):
    """只重跑输掉的配对任务一次，重跑仍输才算确认退化。"""
    library = UnitLibrary(OUT / "units")
    unit, pairs = paired(args)
    lost = [p["task"] for p in pairs if p["before"] and not p["after"] and "confirm_after" not in p]
    if not lost:
        print(json.dumps({"unit": unit["id"], "lost_pairs": [], "rerun": 0}, ensure_ascii=False), flush=True)
        return []
    repeat_args = argparse.Namespace(**vars(args))
    repeat_args.tasks = lost
    repeat_args.rerun = True
    repeat_args.role = "heldout"
    repeat_args.confirmation_of = {p["task"]: p["after_execution"] for p in pairs if p["task"] in lost}
    await automation(repeat_args)
    print(json.dumps({"unit": unit["id"], "lost_pairs": lost, "rerun": len(lost)}, ensure_ascii=False), flush=True)
    return lost


def stagegate(args):
    """阶段末：对本阶段新增单元的配对整体做原版 SAGE 二元检验。

    判负的后果：只有确认退化才撤回在那道题上被注入过的单元；仅仅不显著时保留单元，标记为未显著。
    """
    library = UnitLibrary(OUT / "units")
    pairs = stage_pairs()
    if not pairs:
        evidence = {"kind": "stage_gate", "decision": "not_applicable",
                    "reason": "no_accepted_units" if not library.units() else "no_independent_pairs",
                    "units": [u["id"] for u in library.units()], "wins": 0, "losses": 0,
                    "p_value": None, "pairs": [], "confirmed_regressions": [], "retired": [],
                    "experiment_version": experiment_version(OUT)}
        append_record(OUT / "promotions.jsonl", evidence)
        print("阶段检验不适：" + evidence["reason"], flush=True)
        return evidence
    evidence = library.decide_stage(pairs, POLICY["sage_alpha"], POLICY["sage_penalty"],
                                    POLICY["sage_regression_cap"])
    evidence["experiment_version"] = experiment_version(OUT)
    if evidence["decision"] == "abstained":
        evidence["note"] = "不显著，单元保留"
    retired = []
    if evidence["confirmed_regressions"]:
        affected = set(evidence["confirmed_regressions"])
        injected = {unit_id: p["task"] for p in pairs if p["task"] in affected
                    for unit_id in p["injected_units"]}
        for unit in list(library.units()):
            if unit["id"] in injected:
                library.retire(unit["id"], "阶段末确认退化：" + injected[unit["id"]])
                retired.append(unit["id"])
    evidence["retired"] = retired
    append_record(OUT / "promotions.jsonl", evidence)
    print(json.dumps({k: v for k, v in evidence.items() if k != "pairs"}, ensure_ascii=False), flush=True)
    return evidence


async def mcpcheck(args):
    from contextlib import AsyncExitStack
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from engine.automation_adapter import restore_messages
    from engine.mcp_proxy import CONTEXT_TOOL
    from automationbench.domains import get_domain_dataset

    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    domain = next(d for d, tasks in selected["domains"].items() if args.task in tasks)
    row = next(r for r in get_domain_dataset(domain) if json.loads(r["info"])["task_name"] == args.task)
    messages = restore_messages(row["prompt"])
    instruction = "\n".join(m.content for m in messages if m.role == "user")
    downstream = [sys.executable, "-m", "engine.automation_adapter", "--domain", domain, "--task", args.task]
    proxy = [sys.executable, "-m", "engine.mcp_proxy", "--service", "automationbench", "--automationbench"]
    if args.candidate:
        proxy.extend(["--candidate", args.candidate])
    proxy.extend(["--", *downstream])
    async with AsyncExitStack() as stack:
        sessions = []
        for command in (downstream, proxy):
            read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(command=command[0], args=command[1:], cwd=str(ROOT))))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions.append(session)
        direct_tools = await sessions[0].list_tools()
        proxy_tools = await sessions[1].list_tools()
        native_proxy = [tool.model_dump(mode="json") for tool in proxy_tools.tools if tool.name != CONTEXT_TOOL]
        if [tool.model_dump(mode="json") for tool in direct_tools.tools] != native_proxy:
            raise AssertionError("MCP 代理改变了原装工具定义")
        answers = [await s.call_tool("api_search", {"query": instruction, "top_k": 5}) for s in sessions]
        if any(answer.isError for answer in answers):
            error = next(answer for answer in answers if answer.isError)
            raise RuntimeError("MCP 工具调用失败：" + error.content[0].text)
        if answers[0].model_dump(mode="json") != answers[1].model_dump(mode="json"):
            raise AssertionError("作用域外的 MCP 工具返回发生变化")
        resource = await sessions[1].read_resource("gnosis://capabilities")
        capabilities = json.loads(resource.contents[0].text)
        proof = {"task": args.task, "tool_definitions_identical": True,
                 "out_of_scope_response_identical": True, "capability_resource_read": True,
                 "real_tool": "api_search"}
        if args.candidate:
            if args.candidate not in capabilities["active_units"]:
                raise AssertionError("能力账本没有记录当前验证单元")
            proof["candidate"] = args.candidate
        context = await sessions[1].call_tool(CONTEXT_TOOL, {"task": {"id": args.task,
            "instruction": instruction}, "messages": [m.model_dump(mode="json") for m in messages], "step": 0})
        if context.isError:
            raise AssertionError("MCP 完整上下文插件执行失败")
        rendered = json.loads(context.content[0].text)
        if rendered["status"] not in ("completed", "model_request", "stopped"):
            raise AssertionError("MCP 完整上下文返回协议错误")
        proof.update(full_context_messages=len(messages), context_status=rendered["status"],
                     requires_live_model_verification=rendered["status"] == "model_request")
        (OUT / "mcp_transport.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof), flush=True)


def componentcheck(args):
    from automationbench.tools.api.fetch import api_fetch, _router_service, _url_to_internal_path
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values
    from automationbench.schema.world import WorldState
    from engine.automation_adapter import ScopedInjector, restore_messages
    from engine.automation_adapter import matches
    from engine.component_host import HostError, CandidateError

    library = UnitLibrary(OUT / "units")
    unit = library.read(args.candidate)
    record = learning_records(unit["source_task"])[-1]
    messages = restore_messages(record["completion"])
    row = next(r for r in get_domain_dataset(record["domain"])
               if json.loads(r["info"])["task_name"] == record["task"])
    info = json.loads(row["info"])
    initial = strip_none_values(info["initial_state"])
    replay_world = WorldState(**initial)
    replay_world.meta.allowed_services = compute_allowed_services(
        initial, [strip_none_values(a) for a in info["assertions"]], info["zapier_tools"])
    calls = {tc.id: tc for m in messages if m.role == "assistant" for tc in m.tool_calls or []}
    injector = ScopedInjector(library, config(), args.candidate)
    prompt = restore_messages(record["prompt"])
    state = {"task": record["task"], "task_instruction": "\n".join(m.content for m in prompt if m.role == "user")}
    prompt = restore_messages(injector.begin_task([m.model_dump(mode="json") for m in prompt], state))
    observations = []
    native_failures = []
    try:
        if "GLOBAL" in unit["scope"] and unit["component"] in ("C", "W"):
            initial = injector.workflow([m.model_dump(mode="json") for m in prompt], state, 0)
            context = injector.process_context(initial, state, 0) if not state.get("final_env_response") else {"messages": initial}
            observations.append({"endpoint": "GLOBAL", "input_context": initial,
                                 "output_context": context["messages"],
                                 "pending_model_request": context.get("request"),
                                 "workflow_stopped": bool(state.get("final_env_response"))})
        for position, message in enumerate(messages):
            if message.role != "tool":
                continue
            call = calls[message.tool_call_id]
            if call.name != "api_fetch":
                continue
            arguments = json.loads(call.arguments)
            # 按原始顺序重放原装工具，依据真实异常识别学习轨迹中的错误返回。
            try:
                native = api_fetch(replay_world, **arguments)
            except Exception as error:
                if message.content != str(error):
                    raise RuntimeError("学习记录的工具错误无法由原装接口复现") from error
                native_failures.append({"tool_call": call.id, "error_type": type(error).__name__,
                                        "error": str(error)})
                continue
            path, router = _url_to_internal_path(arguments["url"])
            if router is None:
                continue
            service = _router_service(router)
            endpoint = service + ":" + arguments["method"].upper() + " " + path
            if not matches(unit["scope"], service, endpoint):
                continue
            payload = json.loads(native)
            if isinstance(payload, dict) and "error" in payload:
                continue
            rendered = injector.apply(message.content, service, endpoint, state,
                                      len(observations), arguments)
            full = [m.model_dump(mode="json") for m in [*prompt, *messages[:position + 1]]]
            full[-1]["content"] = rendered
            full = injector.workflow(full, state, len(observations))
            context = injector.process_context(full, state, len(observations))
            observations.append({"endpoint": endpoint, "original": message.content, "rendered": rendered,
                                 "input_context": full, "output_context": context["messages"],
                                 "pending_model_request": context.get("request")})
        if not observations:
            raise CandidateError(unit["id"], unit["component"], "授权学习记录没有触达候选使用范围")
        search_observation = next((m for m in messages if m.role == "tool"
                                   and calls[m.tool_call_id].name == "api_search"), None)
        outside_identical = None
        if search_observation is not None and "GLOBAL" not in unit["scope"]:
            search_call = calls[search_observation.tool_call_id]
            raw = search_observation.content
            outside = injector.apply(raw, "automationbench", "automationbench:api_search", state,
                                     call=json.loads(search_call.arguments))
            outside_identical = outside == raw
            if not outside_identical:
                raise CandidateError(unit["id"], unit["component"], "组件修改了作用域外的观测")
        proof = {"unit": unit["id"], "source_task": record["task"],
                 "component": unit["component"], "real_observations": len(observations),
                 "kind": "saved_record_processing", "experiment_version": experiment_version(OUT),
                 "requires_live_model_verification": any(o["pending_model_request"] is not None for o in observations),
                 "out_of_scope_identical": outside_identical, "out_of_scope_tool": "api_search", "observations": observations,
                 "native_tool_failures": native_failures,
                 "injections": state.get("injection_events", [])}
        (OUT / ("component_" + unit["id"] + ".json")).write_text(
            json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in proof.items() if k not in ("observations", "injections")}))
        return proof
    except (HostError, CandidateError) as error:
        append_record(OUT / "component_failures.jsonl", {"unit": unit["id"],
                      "error_type": type(error).__name__, "error": str(error),
                      "category": "ai_code_or_format_error" if isinstance(error, CandidateError) else "harness_or_integration_error",
                      "experiment_version": experiment_version(OUT)})
        raise
    finally:
        injector.close()


def repair_component(args):
    from engine.component_host import CandidateError

    library = UnitLibrary(OUT / "units")
    unit = library.read(args.candidate) if args.candidate else proposal(args)
    if not unit["component"]:
        print(f"纯文字单元，跳过组件检查：{unit['id']}", flush=True)
        return unit
    root = unit.get("provenance", {}).get("repair_root") or unit["id"]
    attempts = [r for r in read_records(OUT / "repair_attempts.jsonl") if r["root"] == root]
    attempted = max((r["attempt"] for r in attempts), default=0)
    if attempts:
        latest = attempts[-1]
        args.task = unit["source_task"]
        args.repair_root = root
        if latest["stage"] == "requested":
            args.candidate = latest["failed_unit"]
            args.proposal_checkpoint = OUT / latest["checkpoint"]
            unit = proposal(args)
            append_record(OUT / "repair_attempts.jsonl", {**latest, "stage": "proposed", "unit": unit["id"]})
        else:
            unit = library.read(latest["unit"])
    while True:
        args.candidate = unit["id"]
        if not unit["component"]:
            print(f"纯文字单元，组件准备完成：{unit['id']}", flush=True)
            return unit
        try:
            proof = componentcheck(args)
        except CandidateError:
            if attempted >= POLICY["candidate_repairs"]:
                raise
            args.task = unit["source_task"]
            args.repair_root = root
            attempted += 1
            args.proposal_checkpoint = OUT / "proposal_checkpoints" / experiment_version(OUT) / "repair" / root / str(attempted)
            request = {"root": root, "failed_unit": unit["id"], "attempt": attempted, "stage": "requested",
                       "checkpoint": str(args.proposal_checkpoint.relative_to(OUT)),
                       "experiment_version": experiment_version(OUT)}
            append_record(OUT / "repair_attempts.jsonl", request)
            unit = proposal(args)
            append_record(OUT / "repair_attempts.jsonl", {**request, "stage": "proposed", "unit": unit["id"]})
            continue
        if proof["requires_live_model_verification"]:
            print(f"组件接口准备完成，模型子请求待源题验证：{unit['id']}", flush=True)
        else:
            print(f"组件准备验证通过：{unit['id']}", flush=True)
        return unit


def isolationcheck(args):
    from engine.automation_adapter import restore_messages
    from engine.component_sandbox import IsolatedComponentHost
    from engine.component_host import CandidateError
    from engine.scope_proposer import learning_view
    from automationbench.domains import get_domain_dataset

    cfg = config()["host"]
    audit = OUT / "isolation_checks.jsonl"
    host = IsolatedComponentHost(ROOT / cfg["host_dir"], cfg["node_bin_dir"], audit)
    protected = [ROOT / ".env", BUDGET_LEDGER, ROOT / "scratch/AutomationBench/automationbench/rubric/__init__.py"]
    if not all(p.exists() for p in protected):
        raise RuntimeError("实际保护对象缺失，隔离检查不能执行")
    script = (
        "import os,json,sys,socket,errno; paths=json.loads(sys.argv[1]); "
        "r={'protected_files_invisible':all(not os.path.exists(p) for p in paths),"
        "'credentials_absent':all(os.getenv(k) is None for k in ['MODEL_API','MODER_PORVIDER','MODER_NAME']),"
        "'host_process_invisible':not os.path.exists('/proc/'+sys.argv[2])}; "
        "s=socket.socket(); s.settimeout(1); r['network_isolated']=s.connect_ex(('1.1.1.1',443)) in [errno.ENETUNREACH,errno.EHOSTUNREACH]; "
        "s.close(); print(json.dumps(r))"
    )
    result = subprocess.run(host.sandbox_command(["/usr/bin/python3", "-c", script,
                                                  json.dumps([str(p) for p in protected]), str(os.getpid())]),
                            capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"})
    checks = json.loads(result.stdout)
    if not all(checks.values()):
        raise AssertionError("操作系统隔离检查失败")
    row = next(r for r in get_domain_dataset("sales") if json.loads(r["info"])["task_name"] == "sales.sheets_reconciliation")
    messages = [m.model_dump(mode="json") for m in restore_messages(row["prompt"])]
    inputs = {"task": {"id": "sales.sheets_reconciliation", "instruction": "\n".join(m["content"] for m in messages if m["role"] == "user")},
              "messages": messages, "step": 0, "budget": {"maxSteps": 50, "remainingSteps": 50,
              "remainingCostUsd": None, "maxOutputTokensPerTask": 8192}}
    try:
        host.load_unit({"id": "verification_context", "component": "C",
                        "source": (ROOT / "candidates/base_v2/C/context.ts").read_text(encoding="utf-8")})
        actual = host.c_build_context("verification_context", inputs)
        if actual["messages"] != messages or len(messages) <= 1:
            raise AssertionError("完整真实上下文未经过插件接口")
        checks["full_real_context"] = len(messages)
    finally:
        host.close()
    probes = {
        "process": "globalThis['pro' + 'cess'].env",
        "network": "fetch('https://example.com')",
        "constructor_escape": "input.messages.constructor.constructor('return process')()",
        "external_module": None,
    }
    for name, expression in probes.items():
        unit_id = "verification_" + name
        source = ("import fs from 'node:fs'; export function buildContext(input) { return fs.readFileSync('/work/file'); }"
                  if expression is None else "export function buildContext(input) { return " + expression + "; }")
        plugin = IsolatedComponentHost(ROOT / cfg["host_dir"], cfg["node_bin_dir"], audit)
        try:
            plugin.load_unit({"id": unit_id, "component": "C", "source": source})
            plugin.c_build_context(unit_id, inputs)
        except CandidateError as error:
            if not getattr(error, "permission_denied", False):
                raise
            checks[name + "_denied"] = True
        else:
            raise AssertionError("越界能力请求未被拒绝：" + name)
        finally:
            plugin.close()
    try:
        learning_view({"task": inputs["task"]["id"], "learning_role": "heldout"})
    except PermissionError:
        checks["heldout_input_denied"] = True
    else:
        raise AssertionError("留出身份通过了学习入口的权限检查")
    proof = {"kind": "security_verification", "actor": "research_controller", "checks": checks,
             "experiment_version": experiment_version(OUT)}
    append_record(audit, proof)
    print(json.dumps(proof, ensure_ascii=False), flush=True)


def round_state():
    state = {}
    for record in read_records(OUT / "evolution_rounds.jsonl"):
        state[record["round"]] = record
    return state


def failed_tasks():
    """干净基线里失败的题，按固定顺序，供每轮轮换来源题。"""
    return sorted(r["task"] for r in baseline_records() if not r["success"])


def observed_task_cost():
    version = experiment_version(OUT)
    rows = []
    for path in sorted((OUT / "runs").glob("*/*.jsonl")):
        rows.extend(r for r in read_records(path) if r.get("experiment_version") == version)
    return (sum(r["cost_usd"] for r in rows) / len(rows)) if rows else POLICY["observed_cost_default_usd"]


def last_base_record(task):
    version = experiment_version(OUT)
    records = [r for r in read_records(execution_ledger("base", version)) if r["task"] == task]
    return records[-1] if records else None


async def source_probe(args):
    """在当前知识库下单独重跑源任务；已经通过就换下一道，避免白花一整轮。"""
    probe_args = argparse.Namespace(**vars(args))
    probe_args.candidate = None
    probe_args.tasks = [args.task]
    probe_args.role = "learning"
    await automation(probe_args)
    record = last_base_record(args.task)
    return bool(record and record["success"])


async def evolve(args):
    from engine.component_host import CandidateError

    require_harness_ready(OUT)
    require_baseline()
    library = UnitLibrary(OUT / "units")
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    args.version = experiment_version(OUT)
    state = round_state()
    used = {r.get("learning_task") for r in state.values()}
    pending = [task for task in failed_tasks() if task not in used]
    for round_index in range(POLICY["pilot_unit_rounds"]):
        args.round_index = round_index
        current = state.get(round_index)
        if current and current.get("stage") == "gated":
            continue
        if current and current.get("stage") == "abstained":
            continue
        if current and current.get("stage") in ("budget_stop", "no_failed_source_task"):
            print("先导已正常结束：" + current["stage"], flush=True)
            return
        if current and current.get("experiment_version") != args.version:
            raise RuntimeError("轮次断点不属于当前实验版本")
        need = POLICY["round_budget_tasks"] * observed_task_cost() + 2 * POLICY["observed_cost_default_usd"]
        meter = budget()
        remaining = meter.state()["remaining_usd"]
        if remaining < need:
            append_record(OUT / "evolution_rounds.jsonl", {"round": round_index, "stage": "budget_stop",
                          "remaining_usd": remaining, "need_usd": need, "experiment_version": args.version})
            print(f"余额 ${remaining:.4f} 不足一整轮最坏费用 ${need:.4f}，先导正常结束", flush=True)
            break
        if not current:
            chosen = None
            while pending:
                args.task = pending.pop(0)
                if not await source_probe(args):
                    chosen = args.task
                    break
            if chosen is None:
                append_record(OUT / "evolution_rounds.jsonl", {"round": round_index,
                              "stage": "no_failed_source_task", "experiment_version": args.version})
                print("干净基线的失败题在当前知识库下都已通过，先导正常结束", flush=True)
                break
            args.repair_root = None
            current = {"round": round_index, "learning_task": chosen, "stage": "proposing",
                       "previous_unit": args.candidate, "experiment_version": args.version,
                       "checkpoint": str(Path("proposal_checkpoints") / args.version / ("round_" + str(round_index)))}
            append_record(OUT / "evolution_rounds.jsonl", current)
        args.task = current["learning_task"]
        try:
            if current["stage"] == "proposing":
                args.candidate = current["previous_unit"]
                args.repair_root = None
                args.proposal_checkpoint = OUT / current["checkpoint"]
                unit = proposal(args)
                current = {**current, "unit": unit["id"], "stage": "proposed"}
                append_record(OUT / "evolution_rounds.jsonl", current)
            args.candidate = current["unit"]
            unit = library.read(args.candidate)
            if unit["status"] in ("accepted", "abstained") and unit.get("evidence", {}).get("kind") == "admission":
                gate(args)
                append_record(OUT / "evolution_rounds.jsonl", {**current, "stage": "gated"})
                continue
            if current["stage"] != "prepared":
                save_json(OUT / "runtime_status.json", {"phase": "component_preparation",
                          "round": round_index, "task": args.task, "at": time.time(),
                          "experiment_version": args.version})
                unit = repair_component(args)
                current = {**current, "unit": unit["id"], "stage": "prepared"}
                append_record(OUT / "evolution_rounds.jsonl", current)
        except (AIFormatError, CandidateError) as error:
            unit_id = args.candidate if current["stage"] != "proposing" else None
            evidence = {"kind": "component_preparation", "decision": "abstained",
                        "reasons": ["ai_code_or_format_error"], "error_type": type(error).__name__,
                        "experiment_version": args.version}
            if unit_id:
                failed_unit = library.read(unit_id)
                failed_unit.update(status="abstained", evidence=evidence)
                save_json(library.root / unit_id / "unit.json", failed_unit)
            append_record(OUT / "promotions.jsonl", {"unit": unit_id, **evidence})
            append_record(OUT / "evolution_rounds.jsonl", {**current, "unit": unit_id, "stage": "abstained", **evidence})
            print(json.dumps({"round": round_index, "unit": unit_id, **evidence}, ensure_ascii=False), flush=True)
            continue
        args.candidate = unit["id"]
        args.task = unit["source_task"]
        args.tasks = [unit["source_task"]]
        args.role = "learning"
        await automation(args)
        pair_tasks = sorted(set(affected_tasks(unit, selected["contracts"])) - {unit["source_task"]})
        random.Random(POLICY["seed"] + round_index).shuffle(pair_tasks)
        pair_tasks = pair_tasks[:POLICY["max_pairs"]]
        candidate = args.candidate
        args.role = "heldout"
        args.candidate = None
        args.tasks = [unit["source_task"], *pair_tasks]
        await automation(args)
        args.candidate = candidate
        if pair_tasks:
            args.tasks = pair_tasks
            await automation(args)
            await confirm(args)
        args.gate_tasks = pair_tasks
        gate(args)
        append_record(OUT / "evolution_rounds.jsonl", {"round": round_index, "unit": unit["id"],
                      "stage": "gated", "learning_task": unit["source_task"],
                      "experiment_version": experiment_version(OUT)})


def summarize():
    version = experiment_version(OUT)
    rows = baseline_records()
    world = [r for r in read_records(OUT / "appworld_base.jsonl") if r.get("experiment_version") == version]
    domains = {}
    for domain in ("sales", "operations", "hr", "marketing", "support", "finance"):
        group = [r for r in rows if r["domain"] == domain]
        domains[domain] = {"completed": len(group), "planned": 5,
                           "pass_rate": sum(r["success"] for r in group) / len(group) if group else None,
                           "partial_credit": sum(r["partial_credit"] for r in group) / len(group) if group else None,
                           "cost_usd": sum(r["cost_usd"] for r in group)}
    mean_cost = sum(r["cost_usd"] for r in rows) / len(rows) if rows else None
    strict = sum(r["success"] for r in rows) / len(rows) if rows else None
    baseline_complete = len(rows) == 30
    rounds = list(round_state().values())
    stage_results = [r for r in read_records(OUT / "promotions.jsonl")
                     if r.get("kind") == "stage_gate" and r.get("experiment_version") == version]
    pilot_ended = bool(rounds) and (rounds[-1]["stage"] in ("budget_stop", "no_failed_source_task")
                                   or len(rounds) == POLICY["pilot_unit_rounds"]
                                   and all(r["stage"] in ("gated", "abstained") for r in rounds))
    complete = baseline_complete and pilot_ended and bool(stage_results)
    result = {"complete": complete, "automation_completed": len(rows), "appworld_completed": len(world),
              "experiment_version": version, "baseline_complete": baseline_complete,
              "appworld_status": "deferred" if not world else "executed", "pilot_ended": pilot_ended,
              "rounds": rounds, "stage_gate": stage_results[-1] if stage_results else None,
              "domains": domains, "automation_pass_rate": strict, "automation_mean_cost_usd": mean_cost,
              "appworld_pass_rate": sum(r["success"] for r in world) / len(world) if world else None,
              "budget": budget().totals(),
              "g0": "pass" if baseline_complete and mean_cost <= 0.05 and 0.2 <= strict <= 0.65 else "pending" if not baseline_complete else "fail"}
    (OUT / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def readiness():
    import shutil

    cfg = config()
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    planned = {task for tasks in selection["domains"].values() for task in tasks}
    version = experiment_version(ROOT / "state")
    clean_base = read_records(execution_ledger("base", version))
    empty = fingerprint([])
    current_units = UnitLibrary(OUT / "units").units()
    checks = {
        "dirty_batch_declared_void": DIRTY_MARKER.is_file(),
        "clean_automation_baseline": len(clean_base) == len(planned)
            and {r["task"] for r in clean_base} == planned
            and all(r["version"] == empty and r["experiment_version"] == version
                    and r["learning_role"] == "learning" for r in clean_base),
        "knowledge_library_empty": not current_units,
        "appworld_baseline_deferred": True,
        "appworld_output_directory_empty": not any(Path("/home/jonahwu/appworld_root/experiments/outputs").iterdir()),
        "budget_ledger_present": BUDGET_LEDGER.is_file(),
        "credentials_file_present": (ROOT / ".env").is_file(),
        "os_isolation_available": shutil.which("bwrap") is not None,
        "component_runtime_available": all(p.exists() for p in (
            Path(cfg["host"]["node_bin_dir"]) / "node", ROOT / cfg["host"]["host_dir"] / "src/scoped-worker.mjs",
            ROOT / cfg["host"]["host_dir"] / "node_modules/quickjs-emscripten/package.json",
            ROOT / cfg["host"]["host_dir"] / "node_modules/cordis/package.json")),
        "fixed_upstream_versions": all(subprocess.check_output(["git", "-C", str(ROOT / "scratch" / name),
            "rev-parse", "HEAD"], text=True).strip() == selection[key]
            for name, key in (("AutomationBench", "automation_commit"), ("ace", "ace_commit"))),
    }
    try:
        require_harness_ready(OUT)
    except ExecutionBlocked:
        checks["script_audit_clear"] = False
    else:
        checks["script_audit_clear"] = True
    blockers = [name for name, value in checks.items() if not value]
    meter = budget()
    state = meter.state()
    result = {"status": "blocked" if blockers else "ready",
              "experiment_version": experiment_version(ROOT / "state"),
              "current_result_files": sum(p.is_file() for p in OUT.rglob("*")),
              "current_units": [u["id"] for u in current_units],
              "void_results": {"marker": str(DIRTY_MARKER.relative_to(ROOT)), "status": "void",
                               "use": "禁止作为学习材料、基线、配对证据与图表输入"},
              "authorized_learning_source": str((OUT / "runs").relative_to(ROOT)) + "/<本版本>/",
              "checks": checks, "blockers": blockers, "policy": POLICY,
              "budget": {**state, "phases": meter.totals()},
              "live_model_verification": "clean_baseline_verified" if not blockers else "pending"}
    (ROOT / "state/preparation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if blockers:
        raise ExecutionBlocked("准备条件未通过：" + ", ".join(blockers))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "readiness", "automation", "appworld", "propose", "gate", "confirm", "stagegate", "summary", "mcpcheck", "componentcheck", "evolve", "evaluate-saved", "set-budget", "budget", "repair", "isolationcheck"])
    parser.add_argument("--simple", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("--components", action="store_true", default=POLICY["default_components"],
                        help="启用组件接口；缺省采用实验规则，由受测 AI 选择组件类型")
    parser.add_argument("--rerun", action="store_true", help="忽略账本中的已完成记录，重跑指定任务")
    parser.add_argument("--tasks", nargs="*")
    parser.add_argument("--domains", nargs="*")
    parser.add_argument("--task")
    parser.add_argument("--version")
    parser.add_argument("--role", choices=["learning", "heldout"], default="learning")
    parser.add_argument("--evaluate-existing", action="store_true")
    parser.add_argument("--previous-limit", type=float)
    parser.add_argument("--limit", type=float)
    args = parser.parse_args()
    try:
        dispatch(args)
    except BudgetExceeded as error:
        record = {"stage": "budget_stop", "phase": args.action, "error_type": type(error).__name__,
                  "experiment_version": experiment_version(OUT), **budget().state()}
        append_record(OUT / "budget_stops.jsonl", record)
        if args.action == "evolve":
            append_record(OUT / "evolution_rounds.jsonl", {"round": getattr(args, "round_index", 0), **record})
        print(json.dumps(record, ensure_ascii=False), flush=True)
    except (ExecutionBlocked, ProposalInterrupted, AIFormatError):
        raise
    except Exception as error:
        from openai import APIConnectionError, APIStatusError
        from engine.component_host import CandidateError

        if args.action in ("automation", "appworld", "propose", "confirm", "evolve", "repair", "componentcheck"):
            if not isinstance(error, (APIConnectionError, APIStatusError, CandidateError)) and not (OUT / "harness_blocker.json").exists():
                harness_blocker(OUT, error, args.action, args.task)
        raise


def dispatch(args):
    OUT.mkdir(parents=True, exist_ok=True)
    if args.action == "set-budget":
        Budget.change_limit(BUDGET_LEDGER, args.previous_limit, args.limit,
                            "用户确认先导上限按受测模型周额度剩余 20%（折合约 $7）定档为 $5，"
                            "5 小时滑动窗口上限 $12，仍在 $45 研究总预算之内",
                            cap=POLICY["total_limit_usd"])
        print(f"先导预算已授权调整：${args.previous_limit} → ${args.limit}", flush=True)
    elif args.action == "budget":
        meter = budget()
        print(json.dumps({**meter.state(), "phases": meter.totals()}, ensure_ascii=False, indent=2), flush=True)
    elif args.action == "prepare":
        prepare_automation()
    elif args.action == "readiness":
        readiness()
    elif args.action == "automation":
        asyncio.run(automation(args))
    elif args.action == "appworld":
        appworld(args)
    elif args.action == "propose":
        proposal(args)
    elif args.action == "gate":
        gate(args)
    elif args.action == "confirm":
        asyncio.run(confirm(args))
    elif args.action == "stagegate":
        stagegate(args)
    elif args.action == "mcpcheck":
        asyncio.run(mcpcheck(args))
    elif args.action == "componentcheck":
        componentcheck(args)
    elif args.action == "evolve":
        asyncio.run(evolve(args))
    elif args.action == "repair":
        repair_component(args)
    elif args.action == "isolationcheck":
        isolationcheck(args)
    elif args.action == "evaluate-saved":
        evaluate_saved(args)
    else:
        summarize()


if __name__ == "__main__":
    main()
