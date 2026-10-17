import argparse
import csv
import io
import json
import sqlite3
from pathlib import Path

import ijson

from engine.activation import activates, components_version
from engine.scoped_units import save_json

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "experiments"
OUT = EXP / "evo_path" / "discovery"
FX = 7.1  # 与 engine/run_ecai.py、engine/timing_analysis.py 同一汇率
EMPTY = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"  # 空组件组基线版本

RESEARCH = Path.home() / ".gnosis" / "research"  # experiments/research 的 WSL 实体；Windows 侧符号链接已断
RESEARCH_LEDGER = Path.home() / ".gnosis" / "research_ledger.sqlite"
SNAPSHOT = EXP / "research.source_20261007"
P1 = EXP / "ecai_2027" / "mechanism"
P4 = EXP / "ecai_2027" / "scope_execution_ab"
P2 = EXP / "ecai_2027" / "heldout_new"
P3 = EXP / "ecai_2027" / "ablation"
TIMING = EXP / "timing_v4"

RESEARCH_ARMS = ("gnosis", "global", "ace", "fixed", "repeat")
P1_CANDIDATES = ("C1", "C2", "C3")
TIMING_ARMS = ("G", "L", "D", "C", "N")

STATE_LABELS = {"no_opportunity": "无适用机会", "not_loaded": "未加载",
                "applicable_not_reached": "已适用未到达", "reached_no_effect": "到达后无影响",
                "effect": "产生影响", "reached_pending_outcome": "已到达待结果分析"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def rel(path):
    # 证据位置统一为项目相对路径；WSL 实体的 research 登记为符号链接的规范形态
    path = Path(path)
    if path.is_relative_to(ROOT):
        return path.relative_to(ROOT).as_posix()
    if path.is_relative_to(RESEARCH):
        return "experiments/research/" + path.relative_to(RESEARCH).as_posix()
    return path.as_posix()


class Ledger:
    """账本只读视图：cost 为闲时参照价（USD），billed 为实际扣费（USD），CNY 按 FX 折算。"""

    def __init__(self, path):
        self.path = Path(path)
        self.by_call = {}
        self.by_execution = {}
        with sqlite3.connect("file:%s?mode=ro" % self.path, uri=True) as db:
            for cid, phase, cost, billed in db.execute("SELECT id, phase, cost, billed FROM calls"):
                self.by_call[cid] = (cost, billed)
                parts = (phase or "").split("/")
                if "execution" in parts and parts[-1] != "execution":
                    slot = self.by_execution.setdefault(parts[-1], {"cost": 0.0, "billed": 0.0, "calls": 0})
                    slot["cost"] += cost or 0.0
                    slot["billed"] += billed or 0.0
                    slot["calls"] += 1

    def call_cost(self, call_id):
        return self.by_call.get(call_id, (None, None))

    def execution_cost(self, execution_id):
        return self.by_execution.get(execution_id, {"cost": 0.0, "billed": 0.0, "calls": 0})


class Ledgers:
    def __init__(self):
        self.members = {
            "research": Ledger(RESEARCH_LEDGER),
            "ecai_p1": Ledger(ROOT / "state" / "ecai_p1_ledger.sqlite"),
            "ecai_followup": Ledger(ROOT / "state" / "ecai_followup_ledger.sqlite"),
            "timing_v4": Ledger(ROOT / "state" / "timing_v4.sqlite"),
        }

    def call_cost(self, call_id):
        for ledger in self.members.values():
            hit = ledger.call_cost(call_id)
            if hit != (None, None):
                return hit
        return None, None

    def execution_cost(self, execution_id):
        hit = {"cost": 0.0, "billed": 0.0, "calls": 0}
        for ledger in self.members.values():
            row = ledger.execution_cost(execution_id)
            for key in ("cost", "billed", "calls"):
                hit[key] += row[key]
        return hit


class Versions:
    """全实验组件版本索引；加载时重算内容哈希，承担版本完整性核对。"""

    def __init__(self):
        self.index = {}
        self.units_cache = {}
        roots = ([RESEARCH / arm for arm in RESEARCH_ARMS]
                 + [SNAPSHOT / arm for arm in ("ace", "fixed", "global", "gnosis", "repeat")]
                 + [P1 / c for c in ("B0",) + P1_CANDIDATES]
                 + [P4 / "B0"] + sorted(P4.glob("candidates/*"))
                 + [P2 / arm for arm in ("fixed", "global", "gnosis")]
                 + [P3 / "gnosis_no_e", TIMING / "common"])
        for workspace in roots:
            versions = workspace / "versions"
            if not versions.is_dir():
                continue
            for entry in versions.iterdir():
                if (entry / "manifest.json").exists():
                    self.index.setdefault(entry.name, entry / "manifest.json")

    def units(self, version):
        if version in self.units_cache:
            return self.units_cache[version]
        record = load_json(self.index[version])
        units = record["units"]
        if record["version"] != version or components_version(units) != version:
            raise ValueError("组件版本摘要不匹配：" + version)
        self.units_cache[version] = units
        return units


def iter_execution_files():
    def records(experiment_id, folder):
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".failure.json"):
                FAILURE_STUBS.append((experiment_id, rel(path)))
                continue
            yield experiment_id, path

    for arm in RESEARCH_ARMS:
        yield from records("research", RESEARCH / "executions" / arm)
    for arm in ("B0", "B0prime") + P1_CANDIDATES:
        yield from records("ecai_p1", P1 / "executions" / arm)
    for arm in ("fixed", "global", "gnosis"):
        yield from records("ecai_p2", P2 / "executions" / arm)
    yield from records("ecai_p3", P3 / "executions" / "gnosis_no_e")
    for group in sorted((P4 / "executions").iterdir()):
        if group.is_dir():
            yield from records("ecai_p4", group)
    for arm in TIMING_ARMS + ("source_G", "source_N"):
        yield from records("timing_v4", TIMING / "executions" / arm)
    for arm in TIMING_ARMS:
        yield from records("timing_v4_preflight", TIMING / "executions" / ("preflight_" + arm))


FAILURE_STUBS = []


def expression_from_components(record, versions, evidence_path):
    # 表达五态：无适用机会 / 未加载 / 已适用未到达 / 到达后无影响 / 产生影响；
    # 知识单元注入即到达，任务级影响留给 outcome 分析，记"已到达待结果分析"
    rows = []
    units = versions.units(record["version"])
    if not units:
        return rows
    active = record.get("active_extensions") or {}
    effects = record.get("first_effect_boundaries") or {}
    injections = record.get("injections") or []
    by_unit = {}
    for event in injections:
        by_unit.setdefault(event.get("unit"), []).append(event)
    trace = record.get("boundary_trace") or []
    for unit in units:
        uid = unit["id"]
        component = unit.get("component")
        applicable = any(activates(unit, event) for event in trace)
        events = by_unit.get(uid, [])
        loaded = uid in active or bool(events)
        reached = loaded if component is None else bool(events)
        affected = uid in effects or any(event.get("effects") for event in events)
        if not applicable:
            state = "no_opportunity"
        elif not loaded:
            state = "not_loaded"
        elif not reached:
            state = "applicable_not_reached"
        elif affected:
            state = "effect"
        elif component is None:
            state = "reached_pending_outcome"
        else:
            state = "reached_no_effect"
        rows.append({"experiment_id": None, "execution_id": record["execution_id"], "repair_identity": uid,
                     "component": component, "applicable": applicable, "loaded": loaded, "reached": reached,
                     "affected": affected, "state": state, "state_label": STATE_LABELS[state],
                     "first_boundary": active.get(uid), "task": record["task"], "arm": record.get("arm"),
                     "success": record.get("success"), "version": record["version"],
                     "evidence_path": evidence_path})
    return rows


def expression_from_timing(record, evidence_path):
    treatment = record["knowledge_treatment"]
    requests = record.get("knowledge_requests") or []
    reached = any(item.get("parts") for item in requests)
    first = next((item for item in requests if item.get("parts")), None)
    applicable = bool(treatment.get("selected"))
    if not applicable:
        state = "no_opportunity"
    elif reached:
        state = "reached_pending_outcome"
    else:
        state = "applicable_not_reached"
    return [{"experiment_id": "timing_v4", "execution_id": record["execution_id"],
             "repair_identity": treatment["source"], "component": None, "applicable": applicable,
             "loaded": reached, "reached": reached, "affected": None, "state": state,
             "state_label": STATE_LABELS[state],
             "first_boundary": first.get("boundary_index") if first else None,
             "first_visible_call": first.get("call") if first else None,
             "treatment": treatment["treatment"], "task": record["task"], "arm": record.get("arm"),
             "success": record.get("success"), "version": record["version"], "evidence_path": evidence_path}]


def outcome_row(experiment_id, record, evidence_path, formal, ledgers, note=None):
    ledger_cost = ledgers.execution_cost(record["execution_id"])
    return {"kind": "execution", "experiment_id": experiment_id, "execution_id": record["execution_id"],
            "task": record["task"], "task_role": record.get("learning_role"), "arm": record.get("arm"),
            "repeat": record.get("repeat"), "version": record.get("version"),
            "experiment_version": record.get("experiment_version"),
            "strict_success": record.get("success"), "partial_credit": record.get("partial_credit"),
            "termination_status": record.get("termination_status"),
            "execution_error": record.get("execution_error"),
            "cost_usd": record.get("cost_usd"), "validation_cost_usd": record.get("validation_cost_usd"),
            "cost_cny_reference": ledger_cost["cost"] * FX, "cost_cny_billed": ledger_cost["billed"] * FX,
            "ledger_calls": ledger_cost["calls"], "recorded_at": record.get("recorded_at"),
            "in_formal_denominator": formal, "note": note, "evidence_path": evidence_path}


def scan(ledgers):
    """单遍扫描全部执行记录，同时产出 outcome 行、expression 行与版本证据索引。"""
    versions = Versions()
    timing_ids = timing_formal_ids()
    p4_ids = p4_formal_ids()
    outcome_rows, expression_rows = [], []
    evidence_by_version = {}
    ace_records = []
    counts = {}
    for experiment_id, path in iter_execution_files():
        try:
            record = load_json(path)
            base = experiment_id.replace("_preflight", "")
            counts[experiment_id] = counts.get(experiment_id, 0) + 1
            if experiment_id == "timing_v4_preflight":
                formal, note = False, "preflight"
            elif experiment_id == "timing_v4":
                formal = record["execution_id"] in timing_ids
                note = None if formal else "learning"
            elif experiment_id == "ecai_p4":
                formal = record["execution_id"] in p4_ids
                note = None if formal else "source_probe"
            else:
                formal = record.get("learning_role") in ("selection", "heldout", "checkpoint")
                note = "noise_repeat" if record.get("arm") == "B0prime" else None
            outcome_rows.append(outcome_row(experiment_id, record, rel(path), formal, ledgers, note))
            evidence_path = rel(path)
            if record.get("knowledge_treatment"):
                expression_rows += expression_from_timing(record, evidence_path)
            else:
                for row in expression_from_components(record, versions, evidence_path):
                    row["experiment_id"] = base
                    expression_rows.append(row)
            if experiment_id == "research" and record.get("learning_role") in ("learning", "selection"):
                key = record["arm"] + "|" + record["version"]
                evidence_by_version.setdefault(key, []).append(record["execution_id"])
            if experiment_id == "research" and record.get("arm") == "ace":
                ace_records.append((record.get("recorded_at") or 0, record["version"], record["execution_id"]))
        except Exception as error:
            raise ValueError("执行记录解析失败 %s：%r" % (path, error)) from error
    write_jsonl(OUT / "outcome.jsonl", outcome_rows)
    write_jsonl(OUT / "expression.jsonl", expression_rows)
    save_json(OUT / "evidence_map.json", evidence_by_version)
    # ACE 版本谱系：按执行时间排序后相邻去重
    ace_transitions = []
    for _, version, execution_id in sorted(ace_records):
        if not ace_transitions or ace_transitions[-1]["candidate"] != version:
            ace_transitions.append({"parent": ace_transitions[-1]["candidate"] if ace_transitions else None,
                                    "candidate": version, "execution_id": execution_id})
    save_json(OUT / "ace_version_transitions.json", ace_transitions)
    return {"outcome_rows": len(outcome_rows), "expression_rows": len(expression_rows),
            "executions_by_experiment": counts,
            "failure_stubs": {"count": len(FAILURE_STUBS),
                              "paths": [p for _, p in FAILURE_STUBS]}}


def timing_formal_ids():
    ids = set()
    for path in (TIMING / "blocks").glob("*.json"):
        ids.update(load_json(path))
    return ids


def p4_formal_ids():
    ids = set()
    with (P4 / "p4_validation.csv").open(encoding="utf-8-sig") as stream:
        for record in csv.DictReader(stream):
            for key in ("execution_id", "B0_id"):
                if record.get(key):
                    ids.add(record[key])
    return ids


def lineage_row(experiment_id, lineage_id, source_family, attempt_id, parent_version, candidate_version,
                optimizer_version, event_order, evidence_ids, selection_event, retained_version,
                protocol_identity, kind, evidence_path, stats=None):
    return {"experiment_id": experiment_id, "lineage_id": lineage_id, "source_family": source_family,
            "attempt_id": attempt_id, "parent_version": parent_version, "candidate_version": candidate_version,
            "optimizer_version": optimizer_version, "event_order": event_order, "evidence_ids": evidence_ids,
            "selection_event": selection_event, "retained_version": retained_version,
            "protocol_identity": protocol_identity, "kind": kind, "evidence_path": evidence_path,
            "stats": stats or {}}


def research_arm_lineage(arm, evidence_map):
    workspace = RESEARCH / arm
    state = load_json(workspace / "workspace.json")
    optimizer = state["experiment_version"]
    lineage_id = "research/" + arm
    rows = []
    current = EMPTY
    comparisons = workspace / "comparisons"
    for entry in sorted(state["actions"], key=lambda item: item["index"]):
        index = entry["index"]
        action = entry.get("action")
        if action is None:
            rows.append(lineage_row("research", lineage_id, None, str(index), current, None, optimizer, index,
                                    [], "proposal_format_failure", None, optimizer, "proposal",
                                    rel(workspace / "workspace.json"),
                                    {"proposal_error": entry.get("proposal_error")}))
            continue
        kind = action["action"]
        if kind == "edit":
            candidate = components_version(action["units"])
            evidence = sorted(evidence_map.get(arm + "|" + candidate, []))
            rows.append(lineage_row("research", lineage_id, None, str(index), current, candidate, optimizer,
                                    index, evidence, None, None, optimizer, "edit",
                                    rel(workspace / "workspace.json"),
                                    {"reason": action["reason"], "units": [u["id"] for u in action["units"]]}))
            current = candidate
        elif kind == "validate":
            path = comparisons / ("%s.json" % index)
            if not path.exists():
                rows.append(lineage_row("research", lineage_id, None, str(index), current, None, optimizer, index,
                                        [], "missing_comparison_record", None, optimizer, "selection", rel(path)))
                continue
            comparison = load_json(path)
            rows.append(lineage_row(
                "research", lineage_id, comparison.get("source_task"), str(index),
                comparison["before_version"], comparison["after_version"], optimizer, index, [rel(path)],
                comparison["decision"],
                comparison["after_version"] if comparison["decision"] == "accepted" else comparison["before_version"],
                optimizer, "selection", rel(path),
                {"wins": comparison["wins"], "losses": comparison["losses"], "p_value": comparison["p_value"],
                 "starharness_improved": comparison.get("starharness_improved"),
                 "selection_cost_usd": comparison.get("selection_cost_usd"),
                 "validation_cost_usd": comparison.get("validation_cost_usd"),
                 "tasks": comparison.get("tasks")}))
    rows.append(lineage_row("research", lineage_id, None, None, current, None, optimizer, 10**9, [],
                            "final_state", state["released"], optimizer, "final", rel(workspace / "workspace.json"),
                            {"working": state["working"], "released": state["released"], "frozen": state["frozen"],
                             "early_version": state.get("early_version"),
                             "original_experiment_version": state.get("original_experiment_version")}))
    return rows


def research_ace_lineage():
    workspace = RESEARCH / "ace"
    state = load_json(workspace / "workspace.json")
    lineage_id = "research/ace"
    rows = []
    previous = None
    order = 0
    transitions = load_json(OUT / "ace_version_transitions.json")  # scan 产出：执行时间序的版本切换
    for item in transitions:
        rows.append(lineage_row("research", lineage_id, None, None, item["parent"], item["candidate"],
                                state["experiment_version"], order, [item["execution_id"]], None, None,
                                state["experiment_version"], "version_transition", "experiments/research/ace"))
        order += 1
        previous = item["candidate"]
    rows.append(lineage_row("research", lineage_id, None, None, previous, None, state["experiment_version"],
                            10**9, [], "final_state", state["released"], state["experiment_version"], "final",
                            "experiments/research/ace/workspace.json",
                            {"working": state["working"], "released": state["released"], "frozen": state["frozen"],
                             "ace_done": state.get("ace_done")}))
    return rows


def p1_lineage():
    rows = []
    for cid in P1_CANDIDATES:
        folder = P1 / cid
        candidate = load_json(folder / "candidate.json")
        source = candidate["source"]
        lineage_id = "ecai_p1/" + cid
        previous = EMPTY
        order = 0
        for attempt in candidate["attempts"]:
            if attempt.get("execution_id") is None:
                rows.append(lineage_row("ecai_p1", lineage_id, source, str(attempt["attempt"]), previous, None,
                                        None, order, [], "proposal_format_failure", None, "gnosis.ecai.p1",
                                        "proposal", rel(folder / "attempts.json")))
            else:
                rows.append(lineage_row("ecai_p1", lineage_id, source, str(attempt["attempt"]), previous,
                                        attempt["version"], None, order, [attempt["execution_id"]], None, None,
                                        "gnosis.ecai.p1", "attempt", rel(folder / "attempts.json"),
                                        {"source_success": attempt["success"],
                                         "units": [u["id"] for u in attempt.get("units", [])]}))
                previous = attempt["version"]
            order += 1
        decision_path = folder / "selective_decision.json"
        if decision_path.exists():
            decision = load_json(decision_path)
            sage = decision["sage"]
            rows.append(lineage_row("ecai_p1", lineage_id, source, "selective", sage["before_version"],
                                    sage["after_version"], sage.get("experiment_version"), order,
                                    [rel(decision_path)],
                                    "accepted" if decision["accepted"] else "rejected",
                                    sage["after_version"] if decision["accepted"] else sage["before_version"],
                                    "gnosis.ecai.p1", "selection", rel(decision_path),
                                    {"mean_before": decision.get("mean_before"), "mean_after": decision.get("mean_after"),
                                     "wins": decision.get("wins"), "losses": decision.get("losses"),
                                     "reason": decision.get("deterministic_reason")}))
            order += 1
        rows.append(lineage_row("ecai_p1", lineage_id, source, None, previous, None, None, 10**9, [],
                                "final_state", candidate["version"] if candidate["status"] == "mature" else EMPTY,
                                "gnosis.ecai.p1", "final", rel(folder / "candidate.json"),
                                {"status": candidate["status"], "version": candidate["version"]}))
    return rows


def p4_lineage():
    rows = []
    frozen = load_json(P4 / "candidates_frozen.json")
    protocol = load_json(P4 / "protocol.json")
    layers = {s["task"]: s["layer"] for s in protocol["sources"]}
    for entry in sorted(frozen, key=lambda item: item["id"]):
        slot = entry["id"]
        folder = P4 / "candidates" / slot
        source = entry["source"]
        lineage_id = "ecai_p4/" + source
        parent = EMPTY
        for name in ("selective_decision", "full_decision"):
            path = folder / (name + ".json")
            if path.exists():
                parent = load_json(path)["sage"]["before_version"]
                break
        order = 0
        rows.append(lineage_row("ecai_p4", lineage_id, source, slot, parent, entry["version"], None, order,
                                [entry["source_execution"]] if entry.get("source_execution") else [], None, None,
                                protocol["schema"], "edit", rel(folder / "candidate.json"),
                                {"interface": entry["interface"], "repeat": entry["repeat"],
                                 "status": entry["status"], "legal": entry.get("legal"),
                                 "source_success": entry.get("source_success"), "scopes": entry.get("scopes"),
                                 "layer": layers.get(source)}))
        order += 1
        for name, stage in (("selective_decision", "selective"), ("full_decision", "full")):
            path = folder / (name + ".json")
            if not path.exists():
                continue
            decision = load_json(path)
            sage = decision["sage"]
            rows.append(lineage_row("ecai_p4", lineage_id, source, slot, sage["before_version"],
                                    sage["after_version"], sage.get("experiment_version"), order, [rel(path)],
                                    "accepted" if decision["accepted"] else "rejected",
                                    sage["after_version"] if decision["accepted"] else sage["before_version"],
                                    protocol["schema"], "selection", rel(path),
                                    {"stage": stage, "mean_before": decision.get("mean_before"),
                                     "mean_after": decision.get("mean_after"), "wins": decision.get("wins"),
                                     "losses": decision.get("losses"), "reason": decision.get("reason")}))
            order += 1
    return rows


def timing_lineage():
    rows = []
    protocol = load_json(TIMING / "protocol.json")
    candidates = load_json(TIMING / "candidates.json")
    registered = set()
    for order, candidate in enumerate(candidates):
        source = candidate["source"]
        registered.add(source)
        slot = candidate.get("slot") or {}
        rows.append(lineage_row("timing_v4", "timing_v4/" + source, source, str(candidate["attempt"]),
                                None, None, protocol["experiment_version"], order,
                                [candidate["baseline_execution"], candidate["source_execution"]],
                                "registered", candidate.get("route_version"), protocol["schema"],
                                "repair_adoption", rel(TIMING / "sources" / source / "candidate.json"),
                                {"category": candidate["repair"]["category"],
                                 "slot_category": slot.get("category")}))
    for source_dir in sorted((TIMING / "sources").iterdir()):
        status_path = source_dir / "status.json"
        if not status_path.exists() or source_dir.name in registered:
            continue
        status = load_json(status_path)
        rows.append(lineage_row("timing_v4", "timing_v4/" + source_dir.name, source_dir.name, None,
                                None, None, protocol["experiment_version"], 10**6, [], status.get("status"),
                                None, protocol["schema"], "screening", rel(status_path),
                                {k: v for k, v in status.items() if k != "status"}))
    return rows


def extract_lineage(_args):
    evidence_path = OUT / "evidence_map.json"
    if not evidence_path.exists():
        raise SystemExit("缺少 evidence_map.json：先运行 extract-outcome（scan）生成版本证据索引")
    evidence_map = load_json(evidence_path)
    rows = []
    for arm in ("gnosis", "global"):
        rows += research_arm_lineage(arm, evidence_map)
    rows += research_ace_lineage()
    rows += p1_lineage()
    rows += p4_lineage()
    rows += timing_lineage()
    write_jsonl(OUT / "lineage.jsonl", rows)
    return {"lineage_rows": len(rows)}


def proposal_row(experiment_id, lineage_id, source, proposal_id, attempt, legality, status,
                 costs, calls, evidence_path, stats=None):
    reference_cny = sum(c for c, _ in costs if c is not None) * FX
    billed_cny = sum(b for _, b in costs if b is not None) * FX
    return {"experiment_id": experiment_id, "lineage_id": lineage_id, "source": source,
            "proposal_id": proposal_id, "attempt": attempt, "legality": legality, "status": status,
            "cost_reference_cny": reference_cny, "cost_billed_cny": billed_cny, "calls": calls,
            "evidence_path": evidence_path, "stats": stats or {}}


def request_costs(ledgers, files):
    costs, calls = [], 0
    for path in files:
        record = load_json(path)
        call = record.get("call")
        if call:
            costs.append(ledgers.call_cost(call))
            calls += 1
    return costs, calls


def research_proposals(ledgers, arm):
    workspace = RESEARCH / arm
    state = load_json(workspace / "workspace.json")
    actions = {entry["index"]: entry for entry in state["actions"]}
    lineage_id = "research/" + arm
    rows = []
    for path in sorted((workspace / "requests").glob("*.json")):
        stem = path.stem.split("_")
        index, retry = int(stem[0]), int(stem[1])
        record = load_json(path)
        action = actions.get(index)
        if action is not None and action.get("action") is None:
            legality = "format_failure"
        elif action is not None:
            legality = "legal"
        else:
            legality = "unknown"
        costs, calls = request_costs(ledgers, [path])
        rows.append(proposal_row("research", lineage_id, None, path.stem, index, legality,
                                 record.get("status"), costs, calls, rel(path),
                                 {"retry": retry,
                                  "original_version": record.get("original_version"),
                                  "action": (action.get("action") or {}).get("action") if action else None,
                                  "proposal_error": action.get("proposal_error") if action else None}))
    return rows


def research_ace_proposals(ledgers):
    rows = []
    for path in sorted((RESEARCH / "ace" / "ace_requests").glob("*.json")):
        stem = path.stem.split("_")
        record = load_json(path)
        costs, calls = request_costs(ledgers, [path])
        rows.append(proposal_row("research", "research/ace", None, path.stem, int(stem[0]), "legal",
                                 record.get("status"), costs, calls, rel(path), {"role": stem[1]}))
    return rows


def p1_proposals(ledgers):
    rows = []
    for cid in P1_CANDIDATES:
        folder = P1 / cid
        candidate = load_json(folder / "candidate.json")
        attempts = {a["attempt"]: a for a in candidate["attempts"]}
        for path in sorted((folder / "requests").glob("*.json")):
            stem = path.stem.split("_")
            attempt = int(stem[0])
            linked = attempts.get(attempt)
            legality = "format_failure" if linked and linked.get("execution_id") is None else "legal"
            costs, calls = request_costs(ledgers, [path])
            rows.append(proposal_row("ecai_p1", "ecai_p1/" + cid, candidate["source"], path.stem, attempt,
                                     legality, candidate["status"] if linked else "not_recorded", costs, calls,
                                     rel(path), {"retry": int(stem[1]) if len(stem) > 1 else 0}))
    return rows


def p4_proposals(ledgers):
    rows = []
    with (P4 / "p4_candidates.csv").open(encoding="utf-8-sig") as stream:
        costs_by_slot = {}
        for record in csv.DictReader(stream):
            value = record.get("proposal_cost") or ""
            costs_by_slot[record["id"]] = json.loads(value) if value.startswith("{") else {}
    for slot_dir in sorted((P4 / "candidates").iterdir()):
        candidate = load_json(slot_dir / "candidate.json")
        slot = candidate["id"]
        files = sorted((slot_dir / "requests").glob("*.json")) if (slot_dir / "requests").is_dir() else []
        costs, calls = request_costs(ledgers, files)
        csv_cost = costs_by_slot.get(slot, {})
        rows.append(proposal_row("ecai_p4", "ecai_p4/" + candidate["source"], candidate["source"], slot,
                                 candidate["repeat"], "legal" if candidate["legal"] else "invalid_proposal",
                                 candidate["status"], costs, calls, rel(slot_dir / "proposal.json"),
                                 {"interface": candidate["interface"],
                                  "csv_proposal_cost_cny": csv_cost.get("cash_cny"),
                                  "csv_reference_cny": csv_cost.get("reference_cny")}))
    return rows


def timing_proposals(ledgers):
    rows = []
    for candidate in load_json(TIMING / "candidates.json"):
        source = candidate["source"]
        folder = TIMING / "requests" / "proposal" / source
        files = sorted(folder.glob("*/*.json")) if folder.is_dir() else []
        costs, calls = request_costs(ledgers, files)
        rows.append(proposal_row("timing_v4", "timing_v4/" + source, source, source, candidate["attempt"],
                                 "legal", "registered", costs, calls,
                                 rel(TIMING / "sources" / source / ("proposal_%s.json" % candidate["attempt"])),
                                 {"category": candidate["repair"]["category"], "request_files": len(files)}))
    return rows


def extract_proposal(_args):
    ledgers = Ledgers()
    rows = []
    for arm in ("gnosis", "global"):
        rows += research_proposals(ledgers, arm)
    rows += research_ace_proposals(ledgers)
    rows += p1_proposals(ledgers)
    rows += p4_proposals(ledgers)
    rows += timing_proposals(ledgers)
    write_jsonl(OUT / "proposal.jsonl", rows)
    return {"proposal_rows": len(rows)}


def comparison_row(experiment_id, source, stage, decision, evidence_path):
    sage = decision["sage"]
    return {"kind": "comparison", "experiment_id": experiment_id, "execution_id": None, "task": None,
            "task_role": "selection", "arm": None, "repeat": None, "version": sage["after_version"],
            "experiment_version": sage.get("experiment_version"), "strict_success": None,
            "partial_credit": None, "termination_status": None, "execution_error": None,
            "cost_usd": None, "validation_cost_usd": None, "cost_cny_reference": None, "cost_cny_billed": None,
            "ledger_calls": 0, "recorded_at": None, "in_formal_denominator": True, "note": stage,
            "evidence_path": evidence_path,
            "source": source, "before_version": sage["before_version"], "after_version": sage["after_version"],
            "accepted": decision.get("accepted"),
            "mean_before": decision.get("mean_before"), "mean_after": decision.get("mean_after"),
            "wins": decision.get("wins"), "losses": decision.get("losses")}


def comparison_rows():
    rows = []
    for arm in ("gnosis", "global"):
        for path in sorted((RESEARCH / arm / "comparisons").glob("*.json")):
            decision = load_json(path)
            decision = dict(decision)
            decision["accepted"] = decision.get("decision") == "accepted"
            decision["sage"] = {"after_version": decision["after_version"],
                                "before_version": decision["before_version"],
                                "experiment_version": decision.get("experiment_version")}
            row = comparison_row("research", decision.get("source_task"), "paired", decision, rel(path))
            row["stats_note"] = {"p_value": decision.get("p_value"),
                                 "starharness_improved": decision.get("starharness_improved")}
            rows.append(row)
    for cid in P1_CANDIDATES:
        path = P1 / cid / "selective_decision.json"
        if path.exists():
            rows.append(comparison_row("ecai_p1", load_json(P1 / cid / "candidate.json")["source"],
                                       "selective", load_json(path), rel(path)))
    for slot_dir in sorted((P4 / "candidates").iterdir()):
        candidate = load_json(slot_dir / "candidate.json")
        for name, stage in (("selective_decision", "selective"), ("full_decision", "full")):
            path = slot_dir / (name + ".json")
            if path.exists():
                rows.append(comparison_row("ecai_p4", candidate["source"], stage, load_json(path), rel(path)))
    return rows


def extract_outcome(_args):
    ledgers = Ledgers()
    summary = scan(ledgers)
    rows = read_jsonl(OUT / "outcome.jsonl")
    rows += comparison_rows()
    write_jsonl(OUT / "outcome.jsonl", rows)
    summary["outcome_rows"] = len(rows)
    return summary


def snapshot_check(_args):
    # 快照身份核对：research.source_20261007 的执行 ID 必须全部回到 WSL 完整件
    full_ids = set()
    for arm in RESEARCH_ARMS:
        folder = RESEARCH / "executions" / arm
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not path.name.endswith(".failure.json"):
                    full_ids.add(path.stem)
    snapshot_ids = set()
    for arm in ("ace", "fixed", "global", "gnosis", "repeat"):
        jsonl = SNAPSHOT / arm / "executions.jsonl"
        if jsonl.exists():
            with jsonl.open("rb") as stream:
                for line in stream:
                    for prefix, event, value in ijson.parse(io.BytesIO(line)):
                        if prefix == "execution_id" and event == "string":
                            snapshot_ids.add(value)
                            break
        folder = SNAPSHOT / "executions" / arm
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not path.name.endswith(".failure.json"):
                    snapshot_ids.add(path.stem)
    missing = sorted(snapshot_ids - full_ids)
    result = {"snapshot_ids": len(snapshot_ids), "full_ids": len(full_ids),
              "missing_in_full": missing, "subset": not missing}
    save_json(OUT / "snapshot_check.json", result)
    return result


def extract_all(_args):
    summary = {}
    summary.update(extract_outcome(None))
    summary.update(extract_lineage(None))
    summary.update(extract_proposal(None))
    summary["snapshot_check"] = snapshot_check(None)
    save_json(OUT / "extraction_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["extract", "extract-lineage", "extract-proposal",
                                            "extract-outcome", "extract-expression", "snapshot-check"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    command = {"extract": extract_all, "extract-lineage": extract_lineage,
               "extract-proposal": extract_proposal, "extract-outcome": extract_outcome,
               "extract-expression": lambda a: scan(Ledgers()), "snapshot-check": snapshot_check}[args.command]
    result = command(args)
    if args.command != "extract":
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
