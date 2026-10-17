import argparse
import asyncio
import csv
import contextvars
import json
import multiprocessing
import random
import sqlite3
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import ijson
import itertools
import matplotlib
import numpy as np
from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ConfigDict, ValidationError

from engine.activation import changed_units, components_version, reuse_evidence, validation_plan
from engine.evolution import EditableUnit, EvolutionWorkspace, paired_selection
from engine.component_host import CandidateError
CandidateError.__reduce__ = lambda self: (CandidateError, (self.candidate_id, self.component, self.message, self.method))
from engine.research_evaluator import ResearchEvaluator
from engine.research_model import ResearchModel, proposal_messages_match
from engine.research_plan import ROOT, PLAN_PATH
from engine.research_upstream import starharness, verify_upstreams
from engine.run_pilot import config
from engine.run_research import REPAIR_INSTRUCTIONS, audit_output, evidence_view
from engine.scoped_units import Budget, BudgetExceeded, AIFormatError, ExecutionBlocked, POLICY, beijing_peak, fingerprint, require_model_access, save_json

OUT = ROOT / "experiments/ecai_2027/mechanism"
SOURCES = ["finance.xero_credit_allocation", "operations.purchase_order_three_way_match",
           "hr.airtable_learning_path_assignment"]
FX = 7.1
matplotlib.use("Agg")
SOURCE_LAUNCH = contextvars.ContextVar("ecai_source_launch", default=None)
TASK_POOL = None
BUDGET_REVISION = {"name": "p1_proposal_quota_removed_68",
                   "allocations_cny": {"P1": 68, "P2": 64, "P3": 24, "P4": 8, "recovery": 7.41},
                   "source_proposal_limit_cny": None}


class InterruptedProposal(RuntimeError):
    pass


class SourceExecutionLimit(BudgetExceeded):
    pass


class EcaiModel(ResearchModel):
    def resume(self, checkpoint, messages, options, validate, allow_budget_drift=False):
        saved = read(checkpoint)
        if saved.get("status") == "inflight" and not (self.output / "io" / saved["call"] / "response.json").exists():
            if saved["version"] != self.version or saved["options"] != options or not proposal_messages_match(
                    saved["messages"], messages, allow_budget_drift):
                raise ValueError("提议断点输入发生变化")
            # 未收到 usage 的旧请求继续占用现金预留，新请求使用独立调用记录。
            return None
        return super().resume(checkpoint, messages, options, validate, allow_budget_drift)

    def request(self, messages, phase, checkpoint, validate=None, options=None, allow_budget_drift=False):
        options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 1, **(options or {})}
        checkpoint = checkpoint.resolve()
        if checkpoint.exists():
            response = self.resume(checkpoint, messages, options, validate, allow_budget_drift)
            if response is not None:
                return response
        require_model_access()
        from engine.llm_client import chat, client
        client.max_retries = 0
        for attempt in range(POLICY["service_retries"] + 1):
            self.journal.verify_version()
            call_id = self.meter.reserve(phase, messages, options["max_tokens"])
            folder = self.journal.request(call_id, phase, messages, options)
            save_json(checkpoint, {"version": self.version, "messages": messages, "options": options,
                                   "status": "inflight", "call": call_id})
            try:
                response = chat(messages, **options)
            except (APIConnectionError, APIStatusError) as error:
                delay = self.journal.retry_service(folder, error, attempt)
                self.meter.release(call_id, "服务失败，按传输阶段核对现金预留")
                if delay is None:
                    raise
                time.sleep(delay)
                continue
            self.finish(checkpoint, call_id, response, messages, options)
            self.checked(response, validate, folder)
            return response
        raise RuntimeError("服务重试流程未返回响应")


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    units: list[EditableUnit]


class CashBudget(Budget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.archived = []
        for path in sorted((ROOT / "archive").glob("2026-10-09-ecai*-void*/ecai_p1_ledger.sqlite")):
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                settled, pending = db.execute("SELECT COALESCE(SUM(COALESCE(billed,cost)),0),"
                    "COALESCE(SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END),0) FROM calls").fetchone()
                settled_ids = [row[0] for row in db.execute("SELECT id FROM calls WHERE cost IS NOT NULL")]
            self.archived.append({"ledger": str(path.relative_to(ROOT)), "status": "void",
                                  "cash_cny": settled * FX, "unsettled_cny": pending * FX,
                                  "settled_call_ids": settled_ids})
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS source_runs (launch_id TEXT PRIMARY KEY, arm TEXT, phase TEXT, at REAL)")
        snapshot_path = ROOT / "state/ecai_cash_snapshot.json"
        if not snapshot_path.exists():
            live = super().state()
            save_json(snapshot_path, {"balance_cny": 171.41, "recorded_at": time.time(),
                "allocations_cny": {"P1": 48, "P2": 64, "P3": 24, "P4": 8, "recovery": 27.41},
                "live_paid_before_snapshot_cny": live["billed_usd"] * FX,
                "settled_before_snapshot_calls": {row["ledger"]: row["settled_call_ids"] for row in self.archived},
                "archive_paid_before_snapshot_cny": {row["ledger"]: row["cash_cny"] for row in self.archived}})
        self.snapshot = read(snapshot_path)
        self.allocations = BUDGET_REVISION["allocations_cny"]
        billing_path = ROOT / "state/ecai_billing_reconciliation.json"
        self.billing = read(billing_path) if billing_path.exists() and self.path == (ROOT / "state/ecai_p1_ledger.sqlite").resolve() else None
        balance_path = ROOT / "state/ecai_balance_receipt.json"
        self.balance_receipt = read(balance_path) if balance_path.exists() and self.path == (ROOT / "state/ecai_p1_ledger.sqlite").resolve() else None
        self.covered_live = set()
        self.covered_live_cash = 0.0
        self.official_unassigned = 0.0
        if self.billing is not None:
            self.official_unassigned = self.billing["unassigned_cash_cny"]
            coverage = {row["ledger"]: row for row in self.billing["ledgers"]}
            live = coverage["state/ecai_p1_ledger.sqlite"]
            self.covered_live = set(live["covered_call_ids"])
            self.covered_live_cash = live["known_cash_cny"]
            for row in self.archived:
                covered = coverage[row["ledger"]]
                with sqlite3.connect((ROOT / row["ledger"]).resolve().as_uri() + "?mode=ro", uri=True) as db:
                    identifiers = {item[0] for item in db.execute("SELECT id FROM calls")}
                if identifiers != set(covered["covered_call_ids"]):
                    raise ValueError("归档费用记录超出官方对账范围")
                row["cash_cny"] = covered["known_cash_cny"]
                row["raw_unsettled_cny"] = row["unsettled_cny"]
                row["unsettled_cny"] = 0.0

    def cash_usage(self, db):
        rows = db.execute("SELECT id,cost,billed,reserved FROM calls").fetchall()
        if not self.covered_live.issubset({row[0] for row in rows}):
            raise ValueError("官方对账覆盖的调用记录缺失")
        uncovered = [row for row in rows if row[0] not in self.covered_live]
        paid = sum((row[2] if row[2] is not None else row[1]) or 0 for row in uncovered) * FX
        pending = sum(row[3] for row in uncovered if row[1] is None) * FX
        # 官方余项全部计入有效 P1 的现金上界，归档归属尚未分清也不会少计责任。
        return paid + pending + self.covered_live_cash + self.official_unassigned, pending

    def archived_future_responsibility(self):
        previous = self.snapshot["archive_paid_before_snapshot_cny"]
        return sum(row["unsettled_cny"] + row["cash_cny"] - previous.get(row["ledger"], 0) for row in self.archived)

    def balance_responsibility(self, db):
        if self.balance_receipt is None:
            return 0.0
        covered = set(self.balance_receipt["covered_call_ids"])
        rows = db.execute("SELECT id,cost,billed,reserved FROM calls").fetchall()
        if not covered.issubset({row[0] for row in rows}):
            raise ValueError("官方余额锚点覆盖的调用记录缺失")
        pending = sum(row[3] for row in rows if row[0] in covered and row[0] not in self.covered_live and row[1] is None)
        subsequent = sum((row[2] if row[2] is not None else row[1]) if row[1] is not None else row[3]
                         for row in rows if row[0] not in covered)
        return (self.snapshot["balance_cny"] - self.balance_receipt["balance_cny"]
                + self.balance_receipt["rounding_margin_cny"] + FX * (pending + subsequent))

    def state(self):
        state = super().state()
        raw_unsettled = state["unsettled_usd"] * FX
        with self.connect() as db:
            live_responsibility, pending = self.cash_usage(db)
            balance_responsibility = self.balance_responsibility(db)
        state["unsettled_usd"] = pending / FX
        state.update(archived_cash_cny=sum(row["cash_cny"] for row in self.archived),
                     archived_unsettled_cny=sum(row["unsettled_cny"] for row in self.archived))
        state["total_cash_responsibility_cny"] = live_responsibility + state["archived_cash_cny"] + state["archived_unsettled_cny"]
        state["balance_snapshot_cny"] = self.snapshot["balance_cny"]
        state["paid_before_snapshot_cny"] = self.snapshot["live_paid_before_snapshot_cny"] + sum(self.snapshot["archive_paid_before_snapshot_cny"].values())
        state["future_cash_responsibility_cny"] = max(balance_responsibility, live_responsibility - self.snapshot["live_paid_before_snapshot_cny"] + self.archived_future_responsibility())
        state["available_after_reservations_cny"] = self.snapshot["balance_cny"] - state["future_cash_responsibility_cny"]
        state.update(p1_cash_responsibility_upper_bound_cny=max(live_responsibility, balance_responsibility),
                     budget_revision=BUDGET_REVISION,
                     official_balance_receipt={key: self.balance_receipt[key] for key in
                         ("at", "balance_cny", "rounding_margin_cny", "assumption")} if self.balance_receipt else None,
                     balance_responsibility_upper_bound_cny=balance_responsibility,
                     raw_live_unsettled_cny=raw_unsettled,
                     official_reconciliation={key: self.billing[key] for key in
                         ("status", "day", "official_cash_cny", "known_cash_cny", "unassigned_cash_cny", "export_files")}
                         if self.billing else None,
                     official_unassigned_cash_cny=self.official_unassigned)
        return state

    def release(self, call_id, reason):
        path = self.output / "io" / call_id / "failure.json"
        if path.exists():
            failure = read(path)
            if failure.get("billing_unconfirmed"):
                failure["settlement_status"] = "unconfirmed_reserved"
                save_json(path, failure)
                return
        return super().release(call_id, reason)

    def reserve(self, phase, prompt, max_tokens, guard=None, contingency_reason=None):
        def check(db, amount, reason):
            used, pending = self.cash_usage(db)
            multiplier = self.rates.get("peak_multiplier", 1) if beijing_peak() else 1
            prospective = used + pending * (multiplier - 1) + FX * amount * multiplier
            balance_prospective = self.balance_responsibility(db) + pending * (multiplier - 1) + FX * amount * multiplier if self.balance_receipt else 0.0
            future = max(balance_prospective, prospective - self.snapshot["live_paid_before_snapshot_cny"] + self.archived_future_responsibility())
            if future > self.snapshot["balance_cny"]:
                raise BudgetExceeded("余额快照后的现金及预留达到 171.41 元，保留断点")
            if self.archived_future_responsibility() + self.official_unassigned > self.allocations["recovery"]:
                raise BudgetExceeded("归档未结算现金责任超过故障恢复分配，保留断点")
            if max(prospective, balance_prospective) > self.allocations["P1"]:
                raise BudgetExceeded("P1 累计现金及预留达到 68 元，保留断点")
            if guard is not None:
                guard(db, amount, reason)
            launch_id = SOURCE_LAUNCH.get()
            if launch_id is not None and phase.startswith("learning/") and "/execution/" in phase:
                arm = phase.split("/")[1]
                if db.execute("SELECT 1 FROM source_runs WHERE launch_id=?", (launch_id,)).fetchone() is None:
                    count = db.execute("SELECT COUNT(*) FROM source_runs WHERE arm=?", (arm,)).fetchone()[0]
                    if count >= 6:
                        raise SourceExecutionLimit("来源真实执行次数达到六次")
                    db.execute("INSERT INTO source_runs VALUES (?,?,?,?)", (launch_id, arm, phase, time.time()))
        return super().reserve(phase, prompt, max_tokens, check, contingency_reason)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def table(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()})


def protocol():
    path = OUT / "protocol.json"
    old = read(PLAN_PATH)
    tasks = []
    for domain in sorted({old["tasks"][t]["domain"] for t in old["search"]}):
        pool = sorted(t for t in old["search"] if t not in SOURCES and old["tasks"][t]["domain"] == domain)
        random.Random(20261008).shuffle(pool)
        tasks.extend(pool[:3])
    result = {"schema": "gnosis.ecai.p1.v1", "sources": SOURCES, "mechanism_tasks": tasks,
              "repeats": 2, "source_execution_limit": 6, "cash_limit_cny": 68,
              "source_proposal_limit_cny": None, "seed": 20261008,
              "balance_snapshot_cny": 171.41, "recovery_allocation_cny": 7.41,
              "release_rule": "original_deterministic_task_mean_partial_credit_tiebreak_and_zero_paired_task_regressions",
              "upstreams": verify_upstreams(), "original_plan": old["identity"]}
    if len(tasks) != 18:
        raise ValueError("机制任务必须为六领域各三题")
    if path.exists():
        previous = read(path)
        budget_fields = {"cash_limit_cny", "source_proposal_limit_cny", "recovery_allocation_cny"}
        if {key: value for key, value in previous.items() if key not in budget_fields} != {
                key: value for key, value in result.items() if key not in budget_fields}:
            raise ValueError("冻结 P1 执行条件发生变化")
        if tuple(previous[key] for key in ("cash_limit_cny", "source_proposal_limit_cny", "recovery_allocation_cny")) not in (
                (48, 2, 27.41), (68, None, 7.41)):
            raise ValueError("P1 预算版本不属于本次授权续跑")
    save_json(path, result)
    save_json(OUT / "budget_revision.json", BUDGET_REVISION)
    return old, result


def compact_records(path):
    fields = ("task", "repeat", "execution_id", "version", "experiment_version", "success", "partial_credit",
              "learning_role", "cost_usd", "first_effect_boundaries", "active_extensions", "termination_status")
    with path.open("rb") as stream:
        for record in ijson.items(stream, "item", use_float=True):
            yield {**{key: record.get(key) for key in fields},
                   "effects_events": sum(bool(event.get("effects")) for event in record["injections"]),
                   "component_events": sum(event.get("component") == "E" for event in record["injections"]),
                   "tool_calls": len(record["actual_tool_actions"])}


def p0():
    _, frozen = protocol()
    historical = ROOT / "experiments/research"
    output = ROOT / "experiments/ecai_2027/p0"
    output.mkdir(parents=True, exist_ok=True)
    summary = read(historical / "summary.json")
    sources = {}
    for arm in ("global", "gnosis"):
        for path in (historical / arm / "versions").glob("*/manifest.json"):
            for unit in read(path)["units"]:
                if not unit["source"]:
                    continue
                key = fingerprint(unit["source"])
                row = sources.setdefault(key, {"source_fingerprint": key, "components": [], "scopes": [], "versions": [], "arms": []})
                for field, value in [("components", unit["component"]), ("scopes", unit["scope"]),
                                     ("versions", path.parent.name), ("arms", arm)]:
                    if value not in row[field]:
                        row[field].append(value)
    table(output / "historical_source_scopes.csv", list(sources.values()))
    comparisons = []
    for path in sorted((historical / "gnosis/comparisons").glob("*.json"), key=lambda p: int(p.stem)):
        comparison = read(path)
        before = read(historical / "gnosis/versions" / comparison["before_version"] / "manifest.json")["units"]
        after = read(historical / "gnosis/versions" / comparison["after_version"] / "manifest.json")["units"]
        base = list(compact_records(historical / "gnosis/selection" / (comparison["before_version"] + ".json")))
        candidate = list(compact_records(historical / "gnosis/selection" / (comparison["after_version"] + ".json")))
        base_keys = {(r["task"], r["repeat"]): r for r in base}
        flips = sum(r["success"] != base_keys[(r["task"], r["repeat"])]["success"] for r in candidate)
        comparisons.append({"candidate": path.stem, "source": comparison["source_task"],
            "before_version": comparison["before_version"], "after_version": comparison["after_version"],
            "changed_scopes": [unit["scope"] for unit in changed_units(before, after)],
            "record_denominator": len(base), "planned_affected": comparison["executed_count"],
            "reused": comparison["reused_count"], "label_flips": flips, "noise_flips": None,
            "effects_executions": sum(bool(r["effects_events"]) for r in candidate),
            "validation_reference_cny": comparison["validation_cost_usd"] * FX,
            "learning_release_decision": comparison["decision"],
            "independent_noise_or_reuse_audit_available": False,
            "validation_plan": comparison["validation_plan"]})
    table(output / "historical_validation_scope.csv", comparisons)
    heldout = []
    for arm in ("fixed", "ace", "global", "gnosis"):
        for row in compact_records(historical / "heldout" / (arm + ".json")):
            heldout.append({"arm": arm, **row})
    table(output / "historical_heldout.csv", heldout)
    retest = read(historical / "retest.json")
    table(output / "historical_retest.csv", [{"arm": arm, "before_version": data["before_version"],
          "after_version": data["after_version"], "scoped_executions": data["scoped"]["executed_count"],
          "global_executions": data["global"]["executed_count"], "reused": data["scoped"]["reused_count"],
          "decision_consistent": data["consistent_decision"], "execution_saving": data["execution_saving"],
          "reference_saving_cny": data["cost_saving_usd"] * FX,
          "audit_contract": "historical_independent_reruns_not_P1_shared_execution"} for arm, data in retest.items()])
    ledger = (ROOT / "state/research_ledger.sqlite").resolve()
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
        costs_rows = [dict(zip(["phase", "calls", "reference_usd", "cash_estimate_usd", "unsettled_usd", "input_tokens", "output_tokens"], row))
                     for row in db.execute("SELECT phase,COUNT(*),SUM(cost),SUM(COALESCE(billed,cost)),"
                         "SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END),SUM(input_tokens),SUM(output_tokens) FROM calls GROUP BY phase")]
    table(output / "historical_costs.csv", costs_rows)
    result = {"status": "completed", "frozen_protocol": frozen, "historical_experiment_version": summary["experiment_version"],
              "distinct_executable_sources": len(sources), "mature_comparisons": len(comparisons),
              "planned_affected": sum(row["planned_affected"] for row in comparisons),
              "reused": sum(row["reused"] for row in comparisons), "heldout_executions": len(heldout),
              "gnosis_effects_executions": sum(bool(r["effects_events"]) for r in heldout if r["arm"] == "gnosis"),
              "model_calls": 0, "historical_baseline_repeat_available": False}
    save_json(output / "summary.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "frozen_protocol"}), flush=True)


def costs(meter, prefix):
    with meter.connect() as db:
        row = db.execute("SELECT COALESCE(SUM(cost),0),COALESCE(SUM(COALESCE(billed,cost)),0) FROM calls WHERE phase LIKE ?",
                         (prefix + "%",)).fetchone()
    return {"reference_cny": row[0] * FX, "cash_cny": row[1] * FX}


def source_learning_records(source, label, evaluator, attempts):
    records, failures = [], []
    for attempt in attempts:
        if attempt["execution_id"] is None:
            failures.append(attempt)
            continue
        record = read(evaluator.output / "executions" / label / (attempt["execution_id"] + ".json"))
        if (record["task"], record["learning_role"], record["version"], record["experiment_version"]) != (
                source, "learning", attempt["version"], evaluator.version):
            raise ValueError("来源学习断点的任务或执行条件不一致")
        if attempt["success"] != record["success"]:
            raise ValueError("来源成功状态与真实执行记录不一致")
        records.append(record)
    return records, failures


def source_proposal_messages(source, current_units, records, failures):
    return [{"role": "system", "content": REPAIR_INSTRUCTIONS +
             "\nReturn only reason and units using the supplied schema. Repair the fixed source task. "
             "Choose scope freely including GLOBAL. Replace the full group; do not request other tasks."},
            {"role": "user", "content": json.dumps({"source": source, "current_units": current_units,
             "evidence": [evidence_view(r) for r in records], "candidate_failures": failures,
             "schema": Proposal.model_json_schema()}, ensure_ascii=False)}]


async def learn(source, index, workspace, evaluator, model):
    try:
        return await learn_source(source, index, workspace, evaluator, model)
    except (BudgetExceeded, InterruptedProposal, APIConnectionError, APIStatusError,
            ExecutionBlocked, AIFormatError, ValidationError) as error:
        save_json(workspace.root / "candidate.json", {"candidate": "C" + str(index + 1), "source": source,
            "status": "budget_stopped" if isinstance(error, BudgetExceeded) else "recoverable_interruption",
            "version": None, "attempts": read(workspace.root / "attempts.json")
            if (workspace.root / "attempts.json").exists() else [], "error_type": type(error).__name__,
            "stop_reason": str(error), "budget_revision": BUDGET_REVISION["name"]})
        raise


async def learn_source(source, index, workspace, evaluator, model):
    folder = workspace.root
    path = folder / "candidate.json"
    if path.exists() and read(path)["status"] in ("mature", "source_execution_limit"):
        return read(path)
    if workspace.state["frozen"]:
        result = {"candidate": "C" + str(index + 1), "source": source, "status": "mature",
                  "version": workspace.state["frozen"], "attempts": read(folder / "attempts.json")}
        if not result["attempts"][-1]["success"]:
            raise ValueError("冻结版本缺少来源成功证据")
        save_json(path, result)
        return result
    baseline = components_version([])
    attempts_path = folder / "attempts.json"
    attempts = read(attempts_path) if attempts_path.exists() else []
    label = "C" + str(index + 1)
    records, failures = source_learning_records(source, label, evaluator, attempts)
    result = {"candidate": label, "source": source, "status": "learning",
              "version": None, "attempts": attempts, "budget_revision": BUDGET_REVISION["name"]}
    first_success = next((item for item in attempts if item["success"] and item["version"] != baseline), None)
    if first_success is not None:
        workspace.state["frozen"] = first_success["version"]
        workspace.save()
        result.update(status="mature", version=first_success["version"])
        save_json(path, result)
        return result
    save_json(path, result)
    start = max((item["attempt"] for item in attempts), default=-1) + 1
    for attempt in itertools.count(start):
        with evaluator.meter.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM source_runs WHERE arm=?", (label,)).fetchone()[0]
        pending_id = fingerprint(["ecai", source, attempt, workspace.state["working"] if attempt else baseline])
        if count >= 6 and not (evaluator.output / "executions" / label / (pending_id + ".json")).exists():
            result.update(status="source_execution_limit", stop_reason="来源累计真实执行达到六次")
            save_json(path, result)
            return result
        if attempt:
            previous_version = attempts[-1]["version"] if attempts else baseline
            messages = source_proposal_messages(source, workspace.load_version(previous_version), records, failures)
            for retry in range(2):
                try:
                    response = await asyncio.to_thread(model.request, messages,
                        "learning/" + result["candidate"] + "/proposal/" + str(attempt),
                        folder / "requests" / f"{attempt}_{retry}.json",
                        validate=Proposal.model_validate_json, options={"response_format": {"type": "json_object"}})
                    proposal = Proposal.model_validate_json(response.choices[0].message.content)
                    break
                except (ValidationError, AIFormatError) as error:
                    if retry:
                        failure = {"attempt": attempt, "version": previous_version,
                                   "execution_id": None, "success": None,
                                   "units": workspace.load_version(previous_version),
                                   "status": "proposal_format_failure", "error": str(error)}
                        save_json(folder / "proposal_failures" / (str(attempt) + ".json"), failure)
                        failures.append(failure)
                        attempts.append(failure)
                        save_json(attempts_path, attempts)
                        continue
                    messages = [*messages, {"role": "user", "content": "Protocol validation failed: " + str(error)}]
            else:
                continue
            workspace.edit([unit.model_dump() for unit in proposal.units])
        version = workspace.state["working"] if attempt else baseline
        execution_id = fingerprint(["ecai", source, attempt, version])
        failure_path = folder / "source_failures" / (str(attempt) + ".json")
        if failure_path.exists():
            failure = read(failure_path)
            failures.append(failure)
            attempts.append(failure)
            save_json(attempts_path, attempts)
            continue
        launch_token = SOURCE_LAUNCH.set(uuid.uuid4().hex)
        try:
            record = await execute_task(evaluator, workspace, version, source, result["candidate"],
                "learning", "learning", 0, execution_id=execution_id)
        except SourceExecutionLimit as error:
            result.update(status="source_execution_limit", stop_reason=str(error))
            save_json(path, result)
            return result
        except CandidateError as error:
            failure = {"attempt": attempt, "version": version, "execution_id": None, "success": None,
                       "units": workspace.load_version(version), "status": "component_failure", "error": str(error)}
            save_json(failure_path, failure)
            failures.append(failure)
            attempts.append(failure)
            save_json(folder / "attempts.json", attempts)
            continue
        finally:
            SOURCE_LAUNCH.reset(launch_token)
        records.append(record)
        attempts.append({"attempt": attempt, "version": version, "execution_id": record["execution_id"],
                         "success": record["success"], "units": workspace.load_version(version)})
        save_json(folder / "attempts.json", attempts)
        print(json.dumps({"source": source, "attempt": attempt, "success": record["success"]}), flush=True)
        if attempt and record["success"] and version != baseline:
            result.update(status="mature", version=version)
            workspace.state["frozen"] = version
            workspace.save()
            break
    save_json(path, result)
    return result


def execute_task_process(plan, meter, launch_id, args, kwargs):
    SOURCE_LAUNCH.set(launch_id)
    local = ResearchEvaluator(plan, meter)
    return asyncio.run(local.execute(*args, **kwargs))


async def execute_task(evaluator, *args, **kwargs):
    global TASK_POOL
    # 环境在子进程主线程创建，信号处理与各题事件循环均独立。
    if TASK_POOL is None:
        TASK_POOL = ProcessPoolExecutor(max_workers=POLICY["parallel_tasks"],
                                        mp_context=multiprocessing.get_context("spawn"))
    return await asyncio.get_running_loop().run_in_executor(TASK_POOL, execute_task_process,
        evaluator.plan, evaluator.meter, SOURCE_LAUNCH.get(), args, kwargs)


async def matrix(workspace, evaluator, version, label, tasks, selected=None):
    keys = [(task, repeat) for task in tasks for repeat in range(2)]
    if selected is not None:
        keys = [key for key in keys if key in selected]
    for task in tasks:
        evaluator.task(task)
    slots = asyncio.Semaphore(POLICY["parallel_tasks"])

    async def one(task, repeat):
        async with slots:
            return await execute_task(evaluator, workspace, version, task, label, "evaluation", "selection", repeat,
                execution_id=fingerprint(["ecai", label, task, repeat, version]))

    return list(await asyncio.gather(*(one(task, repeat) for task, repeat in keys)))


def decision(base, candidate, tasks):
    sage = paired_selection(base, candidate, tasks, 2)
    _, decide, _ = starharness()
    before = {task: sum(r["success"] for r in base if r["task"] == task) / 2 for task in tasks}
    after = {task: sum(r["success"] for r in candidate if r["task"] == task) / 2 for task in tasks}
    mean_before, mean_after = sum(before.values()) / len(tasks), sum(after.values()) / len(tasks)
    partial_before = sum(r["partial_credit"] for r in base) / len(base)
    partial_after = sum(r["partial_credit"] for r in candidate) / len(candidate)
    improved, reason = decide(mean_after, after, partial_after,
                              {"mean": mean_before, "per_scenario": before, "verifier_rate": partial_before})
    return {"accepted": improved and sage["losses"] == 0, "deterministic_reason": reason,
            "mean_before": mean_before, "mean_after": mean_after,
            "partial_before": partial_before, "partial_after": partial_after,
            "wins": sage["wins"], "losses": sage["losses"], "sage": sage}


async def run():
    review = read(ROOT / "experiments/ecai_2027/static_review.json")
    if review["decision"] != "PASS" or review.get("budget_revision") != fingerprint(BUDGET_REVISION):
        raise RuntimeError("独立静态代码复核尚未 PASS")
    load_dotenv(ROOT / ".env")
    old, frozen = protocol()
    plan = {**old, "selection": frozen["mechanism_tasks"]}
    meter = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=OUT)
    evaluator = ResearchEvaluator(plan, meter)
    audit_output(OUT, evaluator.version)
    model = EcaiModel(meter, evaluator.version)
    for source in SOURCES:
        evaluator.task(source)
    workspaces = [EvolutionWorkspace(OUT / ("C" + str(i + 1)), evaluator.version) for i in range(3)]
    # 三个来源同时学习；任何机制成绩均在全部候选冻结后产生。
    candidates = await asyncio.gather(*(learn(source, i, workspaces[i], evaluator, model)
                                        for i, source in enumerate(SOURCES)))
    save_json(OUT / "candidates.json", candidates)
    baseline = EvolutionWorkspace(OUT / "B0", evaluator.version)
    empty = baseline.state["working"]
    tasks = frozen["mechanism_tasks"]
    base = await matrix(baseline, evaluator, empty, "B0", tasks)
    noise = await matrix(baseline, evaluator, empty, "B0prime", tasks)
    save_json(OUT / "baseline.json", {"B0": base, "B0prime": noise})
    for candidate, workspace in zip(candidates, workspaces, strict=True):
        if candidate["status"] != "mature":
            continue
        after = workspace.load_version(candidate["version"])
        path = workspace.root / "validation_plan.json"
        selected_plan = validation_plan([], after, base, evaluator.version)
        if path.exists():
            if read(path) != selected_plan:
                raise ValueError("已保存的候选选择计划与冻结版本不一致")
        else:
            save_json(path, selected_plan)
        affected = {(item["task"], item["repeat"]) for item in selected_plan if item["affected"]}
        selected = await matrix(workspace, evaluator, candidate["version"], candidate["candidate"], tasks, affected)
        by_key = {(r["task"], r["repeat"]): r for r in selected}
        selective = [by_key[(b["task"], b["repeat"])] if item["affected"] else reuse_evidence(b, after, item)
                     for b, item in zip(base, selected_plan, strict=True)]
        selective_decision = decision(base, selective, tasks)
        save_json(workspace.root / "selective_decision.json", selective_decision)
        remaining = {(t, r) for t in tasks for r in range(2)} - affected
        audit = await matrix(workspace, evaluator, candidate["version"], candidate["candidate"], tasks, remaining)
        by_key.update({(r["task"], r["repeat"]): r for r in audit})
        full = [by_key[(b["task"], b["repeat"])] for b in base]
        save_json(workspace.root / "comparison.json", {"plan": selected_plan,
            "selective": selective_decision, "full": decision(base, full, tasks),
            "records": full, "changes": changed_units([], after)})
        print(json.dumps({"candidate": candidate["candidate"], "affected": len(affected), "audit": len(audit)}), flush=True)
    report(meter)


def report(meter=None):
    candidates = []
    for index, source in enumerate(SOURCES):
        label = "C" + str(index + 1)
        folder = OUT / label
        path = folder / "candidate.json"
        if path.exists():
            candidates.append(read(path))
        else:
            candidates.append({"candidate": label, "source": source, "status": "incomplete", "version": None,
                               "attempts": read(folder / "attempts.json") if (folder / "attempts.json").exists() else []})
    if meter is None:
        meter = CashBudget(ROOT / "state/ecai_p1_ledger.sqlite", None, rates=config()["rate_card"], output=OUT)
    protocol_data = read(OUT / "protocol.json")
    tasks = protocol_data["mechanism_tasks"]
    executions = {}
    for label in ["B0", "B0prime", "C1", "C2", "C3"]:
        executions[label] = [read(path) for path in (OUT / "executions" / label).glob("*.json")
                             if not path.name.endswith(".failure.json")]
    base = sorted(executions["B0"], key=lambda r: (tasks.index(r["task"]), r["repeat"]))
    noise = sorted(executions["B0prime"], key=lambda r: (tasks.index(r["task"]), r["repeat"]))
    complete_baseline = len(base) == len(noise) == 36
    k0 = sum(a["success"] != b["success"] for a, b in zip(base, noise, strict=True)) if complete_baseline else None
    rows, pairs, attempts, scope_rows = [], [], [], []
    for candidate in candidates:
        label = candidate["candidate"]
        for attempt in candidate["attempts"]:
            attempts.append({"candidate": label, "source": candidate["source"], "status": candidate["status"], **attempt})
        path = OUT / label / "comparison.json"
        if not path.exists() or not complete_baseline:
            continue
        comparison = read(path)
        full, selected_plan = comparison["records"], comparison["plan"]
        affected = sum(i["affected"] for i in selected_plan)
        flips = sum(a["success"] != b["success"] for a, b in zip(base, full, strict=True))
        missed = sum(not item["affected"] and a["success"] != b["success"]
                     for a, b, item in zip(base, full, selected_plan, strict=True))
        effects = sum(any(e.get("effects") for e in r["injections"]) for r in full)
        unexpected = sum(not item["affected"] and (bool(r["active_extensions"]) or
                         any(e.get("kind") == "knowledge_injection" for e in r["injections"]))
                         for r, item in zip(full, selected_plan, strict=True))
        deltas = np.array([sum((base[i]["success"] != full[i]["success"]) -
                                  (base[i]["success"] != noise[i]["success"])
                                  for i in range(j, j + 2)) for j in range(0, 36, 2)])
        samples = np.random.default_rng(100).choice(deltas, (200000, 18)).mean(axis=1) * 18
        interval = np.quantile(samples, [0.025, 0.975]).tolist()
        selective_cost = sum(costs(meter, "evaluation/" + label + "/execution/" + r["execution_id"])["cash_cny"]
                             for r, item in zip(full, selected_plan, strict=True) if item["affected"])
        full_cost = costs(meter, "evaluation/" + label + "/")["cash_cny"]
        selected_reference = sum(costs(meter, "evaluation/" + label + "/execution/" + r["execution_id"])["reference_cny"]
                                 for r, item in zip(full, selected_plan, strict=True) if item["affected"])
        full_reference = costs(meter, "evaluation/" + label + "/")["reference_cny"]
        proposal_cost = costs(meter, "learning/" + label + "/proposal/")
        source_cost = costs(meter, "learning/" + label + "/execution/")
        consistent = comparison["selective"]["accepted"] == comparison["full"]["accepted"]
        rows.append({"candidate": label, "source": candidate["source"], "version": candidate["version"],
            "scopes": [u["scope"] for u in comparison["changes"]], "planned_affected": affected,
            "reused": 36 - affected, "label_flips": flips, "noise_flips": k0, "excess_flips": flips - k0,
            "effects_executions": effects, "reuse_label_flips": missed, "decisions_consistent": consistent,
            "text_activation_executions": sum(any(e.get("kind") == "knowledge_injection" for e in r["injections"]) for r in full),
            "unexpected_activation_executions": unexpected, "excess_flips_ci95": interval,
            "selective_accepted": comparison["selective"]["accepted"], "full_accepted": comparison["full"]["accepted"],
            "execution_saving_rate": (36 - affected) / 36, "full_cash_cny": full_cost,
            "selective_cash_cny": selective_cost, "saved_cash_cny": full_cost - selective_cost,
            "full_reference_cny": full_reference, "selective_reference_cny": selected_reference,
            "saved_reference_cny": full_reference - selected_reference,
            "proposal_cash_cny": proposal_cost["cash_cny"], "proposal_reference_cny": proposal_cost["reference_cny"],
            "source_cash_cny": source_cost["cash_cny"], "source_reference_cny": source_cost["reference_cny"],
            "audit_cash_cny": full_cost - selective_cost, "audit_reference_cny": full_reference - selected_reference,
            "total_full_cash_cny": proposal_cost["cash_cny"] + source_cost["cash_cny"] + full_cost,
            "total_selective_cash_cny": proposal_cost["cash_cny"] + source_cost["cash_cny"] + selective_cost})
        for a, n, b, item in zip(base, noise, full, selected_plan, strict=True):
            pairs.append({"candidate": label, "task": a["task"], "repeat": a["repeat"],
                "B0": a["success"], "B0prime": n["success"], "C": b["success"],
                "B0_partial": a["partial_credit"], "B0prime_partial": n["partial_credit"], "C_partial": b["partial_credit"],
                "affected": item["affected"], "first_boundary": item["first_boundary"],
                "B0_id": a["execution_id"], "B0prime_id": n["execution_id"], "C_id": b["execution_id"],
                "first_effect_boundaries": b["first_effect_boundaries"], "tool_calls": len(b["actual_tool_actions"]),
                "text_activation_boundaries": [e.get("boundary_index") for e in b["injections"] if e.get("kind") == "knowledge_injection"],
                "active_extensions": b["active_extensions"],
                "capability_calls": [call for event in b["injections"] for call in event.get("actual_calls", [])]})
        scope_rows.append({"candidate": label, "source": candidate["source"], "maturity": candidate["status"],
            "version": candidate["version"], "changes": comparison["changes"], "validation_plan": selected_plan,
            "selective_decision": comparison["selective"], "full_decision": comparison["full"],
            "execution_ids": [r["execution_id"] for r in full]})
    table(OUT / "p1_candidate_history.csv", attempts)
    source_rows = []
    for candidate in candidates:
        folder = OUT / candidate["candidate"]
        manifests = [read(path) for path in (folder / "versions").glob("*/manifest.json")]
        source_rows.append({"candidate": candidate["candidate"], "source": candidate["source"],
            "status": candidate["status"], "mature_version": candidate["version"], "attempts": candidate["attempts"],
            "all_generated_versions": manifests, "validation_plan": read(folder / "validation_plan.json")
            if (folder / "validation_plan.json").exists() else None,
            "comparison": read(folder / "comparison.json") if (folder / "comparison.json").exists() else None})
        if source_rows[-1]["comparison"] is not None:
            source_rows[-1]["comparison"].pop("records")
            source_rows[-1]["comparison_file"] = str((folder / "comparison.json").relative_to(ROOT))
    table(OUT / "p1_validation_scope.csv", source_rows)
    table(OUT / "p1_candidate_audit.csv", scope_rows)
    core_rows = []
    for candidate in candidates:
        measured = next((row for row in rows if row["candidate"] == candidate["candidate"]), None)
        core_rows.append({"candidate": candidate["candidate"], "source": candidate["source"],
            "maturity": candidate["status"], "planned_affected": None, "reused": None,
            "label_flips": None, "noise_flips": k0, "effects_executions": None, "reuse_label_flips": None,
            "decisions_consistent": None, "saved_cash_cny": None, "saved_reference_cny": None,
            **(measured or {})})
    table(OUT / "p0_scope_effect_noise.csv", core_rows)
    table(OUT / "paired_results.csv", pairs)
    with meter.connect() as db:
        entries = [dict(zip(["call_id", "phase", "reference_usd", "billed_usd", "reserved_usd", "input_tokens", "output_tokens", "cached_tokens"], row))
                   for row in db.execute("SELECT id,phase,cost,billed,reserved,input_tokens,output_tokens,cached_tokens FROM calls")]
    for entry in entries:
        entry.update(dataset_status="valid", ledger="state/ecai_p1_ledger.sqlite", paid_before_snapshot=False)
        entry.update(official_aggregate_covered=entry["call_id"] in meter.covered_live,
                     effective_reserved_usd=0 if entry["call_id"] in meter.covered_live or entry["reference_usd"] is not None
                     else entry["reserved_usd"])
    for archive in meter.archived:
        archive_path = ROOT / archive["ledger"]
        with sqlite3.connect(archive_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            archived_entries = [dict(zip(["call_id", "phase", "reference_usd", "billed_usd", "reserved_usd", "input_tokens", "output_tokens", "cached_tokens"], row))
                               for row in db.execute("SELECT id,phase,cost,billed,reserved,input_tokens,output_tokens,cached_tokens FROM calls")]
        for entry in archived_entries:
            entry.update(dataset_status="void", ledger=archive["ledger"],
                         paid_before_snapshot=entry["call_id"] in meter.snapshot["settled_before_snapshot_calls"].get(archive["ledger"], []))
            entry.update(official_aggregate_covered=meter.billing is not None,
                         effective_reserved_usd=0 if meter.billing or entry["reference_usd"] is not None
                         else entry["reserved_usd"])
        entries.extend(archived_entries)
    if meter.billing:
        entries.append({"call_id": None, "phase": "official_aggregate/unassigned", "dataset_status": "financial_only",
                        "ledger": "state/ecai_billing_reconciliation.json",
                        "official_unassigned_cash_cny": meter.official_unassigned,
                        "official_total_cash_cny": meter.billing["official_cash_cny"],
                        "paid_before_snapshot": False, "effective_reserved_usd": 0,
                        "note": "有效与归档调用共有的官方余项，不伪造逐调用费用或用量"})
    table(OUT / "budget_reconciliation.csv", entries)
    slots = []
    for label in ["B0", "B0prime", "C1", "C2", "C3"]:
        indexed = {(r["task"], r["repeat"]): r for r in executions[label] if r["learning_role"] == "selection"}
        candidate = next((c for c in candidates if c["candidate"] == label), None)
        for task in tasks:
            for repeat in range(2):
                record = indexed.get((task, repeat))
                status = "completed" if record else "not_applicable" if candidate and candidate["status"] in (
                    "immature", "source_execution_limit") else "pending"
                slots.append({"condition": label, "task": task, "repeat": repeat, "status": status,
                              "execution_id": record["execution_id"] if record else None})
    table(OUT / "execution_slots.csv", slots)
    completed = complete_baseline and all(c["status"] in ("mature", "immature", "source_execution_limit")
                                         for c in candidates) and len(rows) == sum(c["status"] == "mature" for c in candidates)
    with meter.connect() as db:
        source_counts = {label: db.execute("SELECT COUNT(*) FROM source_runs WHERE arm=?", (label,)).fetchone()[0]
                         for label in ("C1", "C2", "C3")}
    save_json(OUT / "summary.json", {"completed": completed, "candidates": candidates, "mechanism": rows,
                                     "baseline_count": len(base), "noise_count": len(noise), "budget": meter.state(),
                                     "budget_revision": BUDGET_REVISION, "source_execution_counts": source_counts})
    positive = sum(r["planned_affected"] in range(1, 36) and r["saved_cash_cny"] > 0 and
                   r["decisions_consistent"] and not r["reuse_label_flips"] and
                   not r["unexpected_activation_executions"] for r in rows) >= 2
    if rows:
        from matplotlib import pyplot as plt
        from matplotlib import font_manager
        for font in (Path("/mnt/c/Windows/Fonts")).glob("times*.ttf"):
            font_manager.fontManager.addfont(str(font))
        plt.rcParams.update({"font.family": "Times New Roman", "svg.fonttype": "none",
                             "text.color": "#334155", "axes.labelcolor": "#334155",
                             "axes.edgecolor": "#64748b", "xtick.color": "#64748b", "ytick.color": "#64748b"})
        positions = np.arange(len(core_rows))
        fig, ax = plt.subplots(figsize=(7, 4))
        for offset, key, label, color in [(-0.25, "planned_affected", "Planned affected", "#0072b2"),
                                   (0, "label_flips", "Observed label flips", "#d55e00"),
                                   (0.25, "noise_flips", "Unchanged baseline flips", "#64748b")]:
            ax.bar(positions + offset, [r[key] if r[key] is not None else np.nan for r in core_rows],
                   width=0.25, label=label, color=color)
        ax.set_xticks(positions, [r["candidate"] for r in core_rows])
        for position, row in zip(positions, core_rows, strict=True):
            if row["planned_affected"] is None:
                ax.text(position, 8, "Not mature", ha="center", color="#64748b", fontsize=9)
        ax.set_ylabel("Records (denominator: 36)")
        ax.set_ylim(0, 38)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(OUT / "scope_effect_noise.png", dpi=180)
        fig.savefig(OUT / "scope_effect_noise.svg")
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(7, 4))
        for r, color in zip(rows, ["#0072b2", "#d55e00", "#009e73"], strict=False):
            ax.plot([0, 36], [0, r["full_cash_cny"]], color=color, label=r["candidate"] + " full")
            ax.plot([0, 36], [0, r["selective_cash_cny"]], color=color, linestyle="--", label=r["candidate"] + " selective")
        ax.set_xlabel("Historical records (fixed observed affected fraction)")
        ax.set_ylabel("Validation cash cost (CNY)")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(OUT / "validation_cost_curve.png", dpi=180)
        fig.savefig(OUT / "validation_cost_curve.svg")
        plt.close(fig)
    lines = ["# GNOSIS P1 实验交付：预算调整后续跑", "", "原额度结果保存在 experiments/ecai_2027/p1_original_limits/。本次 P1 累计上限 ¥68，取消独立来源提议额度；原余额快照 ¥171.41 及其已消耗现金保持累计核算。", "",
             "关键假设：各来源从空组件开始，机制任务保持相同初态；局部组件在匹配前不可见。撤掉这些假设，复用正确性与成本比较失效。", "",
             f"P1 实验状态：{'已完成' if completed else '未完成'}。正结果门槛：{('PASS' if positive else 'FAIL') if completed else '待评估'}。来源成熟候选 {sum(c['status'] == 'mature' for c in candidates)}/3，完成审计 {len(rows)}/3；B0′ 标签翻转 {str(k0) + '/36' if k0 is not None else '待完整重复执行后计算'}。", "",
             "|候选|受影响|复用|标签翻转|非空 effects|漏选标签翻转|发布一致|现金节省（元）|",
             "|---|---:|---:|---:|---:|---:|---|---:|"]
    lines.extend(f"|{r['candidate']}|{r['planned_affected']}/36|{r['reused']}|{r['label_flips']}|{r['effects_executions']}|{r['reuse_label_flips']}|{r['decisions_consistent']}|{r['saved_cash_cny']:.6f}|" for r in rows)
    spent = costs(meter, "")
    lines += ["", f"P1 已返回 usage 的固定参照费用 ¥{spent['reference_cny']:.6f}；用量与时段估算 ¥{spent['cash_cny']:.6f}。", "",
              "代码新增独立 P1 入口，复用原评价器、版本管理与激活计划。三来源同时学习，首次严格成功后冻结；受影响执行共用于选择性判断和完整审计，审计仅补充预定复用记录。发布沿用正式实验的严格成功均值改进、partial credit 平局判据及零退化条件，SAGE 独立保存。", "",
              "P4、P2/P3 尚未执行，完整投稿路线裁决等待其机制与收益证据。", "",
              "原始数据：experiments/ecai_2027/mechanism/；断点入口：python -m engine.run_ecai p1。"]
    lines += ["", "来源完成状态："] + [f"- {c['source']}：{c['status']}，累计真实执行 {source_counts[c['candidate']]} 次，保存 {len(c['attempts'])} 条尝试记录。" for c in candidates]
    lines += ["", f"B0 完成 {len(base)}/36，B0′ 完成 {len(noise)}/36；未完成槽位保存于 execution_slots.csv。",
              f"未结算预留 ¥{meter.state()['unsettled_usd'] * FX:.6f}，单列保留。"]
    money = meter.state()
    if meter.billing:
        lines += ["", f"官方导出已核对：有效与归档批次合计 ¥{meter.billing['official_cash_cny']:.8f}，"
                  f"逐调用归属未知的余项 ¥{meter.official_unassigned:.8f}。余项全部计入 P1 现金上界，"
                  "官方覆盖调用的现金预留已经关闭，逐调用 usage 未知状态继续保留。",
                  f"P1 现金责任上界 ¥{money['p1_cash_responsibility_upper_bound_cny']:.8f}。"]
    else:
        lines += ["", "官方导出账单尚未关联。"]
    lines += ["", f"余额快照 ¥{money['balance_snapshot_cny']:.2f}；快照前已结算 ¥{money['paid_before_snapshot_cny']:.6f}，保留累计成本。",
              f"归档批次已结算 ¥{money['archived_cash_cny']:.6f}；归档未结算预留 ¥{money['archived_unsettled_cny']:.6f}。",
              f"快照后的现金与预留责任 ¥{money['future_cash_responsibility_cny']:.6f}；扣除预留后可用 ¥{money['available_after_reservations_cny']:.6f}。"]
    (ROOT / "docs/GNOSIS-P1实验交付-2026-10-09.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"completed": completed, "mature": len(rows), "positive_route": positive, "cost": spent}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["p0", "p1", "report", "prepare_followup", "revise_sources", "offline_followup",
                                            "p4", "p2_p3", "followup", "followup_report"])
    args = parser.parse_args()
    if args.command not in ("p0", "p1", "report"):
        from engine.ecai_followup import main as followup_main
        followup_main(args.command)
        return
    if args.command == "p0":
        p0()
    elif args.command == "p1":
        try:
            asyncio.run(run())
        except BudgetExceeded as error:
            save_json(OUT / "budget_stop.json", {"status": "budget_stopped", "reason": str(error)})
            report()
            raise
        except (InterruptedProposal, APIConnectionError, APIStatusError, ExecutionBlocked, AIFormatError, ValidationError) as error:
            save_json(OUT / "service_stop.json", {"status": "recoverable_interruption", "error_type": type(error).__name__,
                       "reason": str(error), "resume": "p1"})
            report()
            raise
    else:
        report()


if __name__ == "__main__":
    main()
