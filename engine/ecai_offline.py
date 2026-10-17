import json
import copy
import argparse
import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import os
import sqlite3
import threading
import time
from pathlib import Path

from engine.research_plan import ROOT
from engine.run_ecai import (CashBudget, EcaiModel, SOURCE_LAUNCH, FX, protocol, matrix, OUT,
                             BUDGET_REVISION, SOURCES, source_learning_records, source_proposal_messages)
from engine.evolution import EvolutionWorkspace
from engine.research_evaluator import ResearchEvaluator
from engine.run_pilot import config
from engine.scoped_units import BudgetExceeded, POLICY, save_json, fingerprint


def copy_ledger(source, target):
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copied:
            original.backup(copied)


def main():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    folder = ROOT / ".offline_checks/ecai"
    folder.mkdir(parents=True, exist_ok=True)
    current = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=folder)
    state = current.state()
    assert state["balance_snapshot_cny"] == 171.41
    assert abs(state["future_cash_responsibility_cny"] - state["archived_unsettled_cny"]) < 1e-9
    assert state["paid_before_snapshot_cny"] > 0
    historical = ROOT / "experiments/research"
    original_ledger = (ROOT / "state/research_ledger.sqlite").resolve()
    with sqlite3.connect(original_ledger.as_uri() + "?mode=ro", uri=True) as db:
        call_id, phase = db.execute("SELECT id,phase FROM calls WHERE phase LIKE 'learning/%/execution/%' AND cost>0 LIMIT 1").fetchone()
    request = json.loads((historical / "io" / call_id / "request.json").read_text(encoding="utf-8"))
    rejected_path = folder / "rejected.sqlite"
    copy_ledger(original_ledger, rejected_path)
    rejected = CashBudget(rejected_path, None, rates=config()["rate_card"], output=folder)
    launch_token = SOURCE_LAUNCH.set(call_id)
    try:
        try:
            rejected.reserve(phase, request["messages"], POLICY["execution_output_tokens"])
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("真实历史费用已超过 P1 上限，应拒绝预留")
    finally:
        SOURCE_LAUNCH.reset(launch_token)
    with rejected.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0

    # 使用真实费用账本及真实请求参数检查同一启动内的多次请求计数。
    admitted_path = folder / "admitted.sqlite"
    copy_ledger(ROOT / current.archived[0]["ledger"], admitted_path)
    admitted = CashBudget(admitted_path, None, rates=config()["rate_card"], output=folder)
    launch_token = SOURCE_LAUNCH.set(call_id)
    reserved = []
    try:
        for _ in range(2):
            reserved.append(admitted.reserve(phase, request["messages"], POLICY["execution_output_tokens"]))
    finally:
        SOURCE_LAUNCH.reset(launch_token)
    with admitted.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 1
    for identifier in reserved:
        admitted.release(identifier, "离线预算检查没有发送请求")

    checkpoint = next((historical / "gnosis/requests").glob("*.json"))
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    model = EcaiModel(admitted, saved["version"])
    model.output = historical
    response = model.resume(checkpoint, saved["messages"], saved["options"], None)
    assert response is not None and response.usage.total_tokens > 0

    class ContentionBudget(CashBudget):
        def connect(self):
            db = super().connect()
            db.execute("PRAGMA busy_timeout=5")
            return db

    contended = ContentionBudget(admitted_path, None, rates=config()["rate_card"], output=folder)
    locked = threading.Event()

    def hold_lock():
        with sqlite3.connect(admitted_path) as db:
            db.execute("BEGIN EXCLUSIVE")
            locked.set()
            time.sleep(0.2)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    locked.wait()
    contended._run_with_retry(lambda db: db.execute("UPDATE source_runs SET at=at"))
    lock_state = contended.state()
    worker.join()
    assert lock_state["spent_usd"] >= 0
    result = {"status": "passed", "checks": ["snapshot_and_archived_cash", "rejected_request_does_not_count",
              "same_launch_counts_once", "completed_checkpoint_reuses_real_response", "real_sqlite_contention"],
              "model_calls": 0, "fixtures": "real_request_and_financial_ledgers"}
    save_json(folder / "result.json", result)
    print(json.dumps(result), flush=True)


def checkpoint_matrix():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    old, frozen = protocol()
    meter = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=OUT)
    with meter.connect() as db:
        before = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    evaluator = ResearchEvaluator({**old, "selection": frozen["mechanism_tasks"]}, meter)
    workspace = EvolutionWorkspace(OUT / "B0", evaluator.version)
    records = asyncio.run(matrix(workspace, evaluator, workspace.state["working"], "B0", frozen["mechanism_tasks"]))
    with meter.connect() as db:
        after = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    assert len(records) == 36 and len({row["execution_id"] for row in records}) == 36
    assert before == after
    result = {"status": "passed", "cached_executions": len(records), "new_model_calls": after - before,
              "check": "independent_event_loops_reuse_real_completed_matrix"}
    save_json(ROOT / ".offline_checks/ecai/threaded_checkpoint.json", result)
    print(json.dumps(result), flush=True)


def environment_process():
    from automationbench.rubric import create_rubric
    from engine.automation_adapter import ScopedAutomationEnv

    old, frozen = protocol()
    meter = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=OUT)
    evaluator = ResearchEvaluator(old, meter)
    dataset, row = evaluator.task(frozen["mechanism_tasks"][0])
    environment = ScopedAutomationEnv(dataset=dataset, rubric=create_rubric(), max_turns=POLICY["max_steps"],
                                      toolset="api", execution_output=OUT)
    asyncio.run(environment._teardown())
    return {"status": "passed", "main_thread": threading.current_thread() is threading.main_thread(),
            "real_task_contract_checked": True, "model_calls": 0}


def environment_setup():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        result = pool.submit(environment_process).result()
    assert result["main_thread"]
    save_json(ROOT / ".offline_checks/ecai/process_environment.json", result)
    print(json.dumps(result), flush=True)


def resume_budget():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    old, frozen = protocol()
    meter = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=OUT)
    evaluator = ResearchEvaluator({**old, "selection": frozen["mechanism_tasks"]}, meter)
    with meter.connect() as db:
        counts = dict(db.execute("SELECT arm,COUNT(*) FROM source_runs GROUP BY arm"))
        before = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    assert counts == {"C1": 2, "C2": 2, "C3": 1}
    assert frozen["cash_limit_cny"] == 68 and frozen["source_proposal_limit_cny"] is None
    assert sum(BUDGET_REVISION["allocations_cny"].values()) == 171.41
    contexts = {}
    for index in (0, 2):
        label, source = "C" + str(index + 1), SOURCES[index]
        workspace = EvolutionWorkspace(OUT / label, evaluator.version)
        attempts = json.loads((workspace.root / "attempts.json").read_text(encoding="utf-8"))
        records, failures = source_learning_records(source, label, evaluator, attempts)
        messages = source_proposal_messages(source, workspace.load_version(attempts[-1]["version"]), records, failures)
        payload = json.loads(messages[1]["content"])
        assert all(record["task"] == source for record in payload["evidence"])
        contexts[label] = messages
    frozen_workspace = EvolutionWorkspace(OUT / "C2", evaluator.version)
    assert frozen_workspace.state["frozen"] == json.loads((OUT / "C2/candidate.json").read_text(encoding="utf-8"))["version"]
    saved = json.loads((OUT / "C1/requests/1_0.json").read_text(encoding="utf-8"))
    response = EcaiModel(meter, evaluator.version).resume(OUT / "C1/requests/1_0.json", saved["messages"], saved["options"], None)
    assert response.usage.total_tokens > 0
    folder = ROOT / ".offline_checks/ecai"
    copied = folder / "resume_budget.sqlite"
    copy_ledger(meter.path, copied)
    checked = copy.copy(meter)
    checked.path = copied
    identifier = checked.reserve("learning/C1/proposal/2", contexts["C1"], POLICY["proposal_output_tokens"])
    with checked.connect() as db:
        reservation = db.execute("SELECT reserved FROM calls WHERE id=?", (identifier,)).fetchone()[0]
        paid = db.execute("SELECT SUM(COALESCE(billed,cost,reserved)) FROM calls WHERE phase LIKE 'learning/C1/proposal/%' AND id!=?",
                          (identifier,)).fetchone()[0]
        assert FX * (paid + reservation) > 2
        assert dict(db.execute("SELECT arm,COUNT(*) FROM source_runs GROUP BY arm")) == counts
    assert checked.state()["p1_cash_responsibility_upper_bound_cny"] <= 68
    checked.release(identifier, "离线检查没有发送推理请求")
    with meter.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == before
    result = {"status": "passed", "source_execution_counts": counts, "execution_version": evaluator.version,
              "source_only_contexts": True, "saved_proposal_reused": True, "old_source_cap_exceeded_and_new_cap_admitted": True,
              "budget_revision": BUDGET_REVISION, "budget_revision_fingerprint": fingerprint(BUDGET_REVISION),
              "model_calls": 0}
    save_json(folder / "resume_budget.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["original", "checkpoint_matrix", "environment_setup", "resume_budget"], default="original")
    arguments = parser.parse_args()
    if arguments.command == "checkpoint_matrix":
        checkpoint_matrix()
    elif arguments.command == "environment_setup":
        environment_setup()
    elif arguments.command == "resume_budget":
        resume_budget()
    else:
        main()
