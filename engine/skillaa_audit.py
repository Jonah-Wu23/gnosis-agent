import json
import os
import sys
from pathlib import Path
from openai import APIConnectionError, APIStatusError

from engine.scoped_units import AIFormatError, POLICY, ExecutionBlocked, ExecutionTruncated, fingerprint, save_json
from engine.timing_research import (OUT, ROOT, SKILLAA, Prediction, ask, freeze, implementation,
                                    meter_model, read, require_ready, upstream_skillaa)

AUDIT = OUT / "skillaa"
DATA_REVISION = "c1a979068ba118d85467179b704031d113d689cc"


class NativeServiceFailure(BaseException):
    pass


def native():
    upstream_skillaa()
    if str(SKILLAA) not in sys.path:
        sys.path.insert(0, str(SKILLAA))


def prepare():
    native()
    from graphopt.optimizer.skill import load_graph
    from scripts.run_graphskillaa import validate_data

    validate_data("searchqa")
    manifest = read(SKILLAA / "data/searchqa/split_manifest.json")
    if manifest["source"]["revision"] != DATA_REVISION:
        raise ValueError("SearchQA 数据快照发生变化")
    rows = read(SKILLAA / "data/searchqa/train/items.json")
    by_id = {str(row["id"]): row for row in rows}
    groups = manifest["training_group_policy"]["groups"]
    graph_path = SKILLAA / "graphopt/envs/searchqa/initial_skill/best_graph.json"
    graph = load_graph(graph_path)
    history_groups = groups[:3]
    history_ids = [str(case_id) for group in history_groups for case_id in group["train_ids"]]
    test_ids = set(map(str, read(SKILLAA / "data/searchqa/test_ids.json")))
    if len(history_ids) != 12 or set(history_ids) & test_ids:
        raise ValueError("外部审计必须保留原生四题组与测试隔离")
    sources = [str(group["train_ids"][0]) for group in history_groups]
    protocol = {"schema": "gnosis.skillaa.audit.v1", "upstream": upstream_skillaa(),
        "data_revision": DATA_REVISION, "graph": graph.to_dict(), "groups": history_groups,
        "history_ids": history_ids, "source_ids": sources, "items": by_id,
        "test_ids": sorted(test_ids), "repeat_order": history_ids[:8],
        "audit_order": [str(case_id) for group in groups for case_id in group["train_ids"]],
        "native_gate": "select_epoch_affected_cases/summarize_epoch_local_gate/SearchQA.accept_complete_candidate",
        "model": read(OUT / "inventory.json")["model"]}
    freeze(AUDIT / "inventory.json", protocol)
    print(json.dumps({"skillaa_commit": protocol["upstream"], "source_ids": sources, "history_cases": 12,
                      "searchqa_cases": len(rows), "model_calls": 0}))
    return protocol


class NativeTransport:
    def __init__(self, model, prefix):
        self.model = model
        self.prefix = prefix

    def request(self, *, messages, stage="optimizer", **kwargs):
        phase = "skillaa/" + self.prefix + "/" + stage + "/" + fingerprint(messages)
        try:
            response = self.model.request(messages, phase, OUT / "requests" / phase / "response.json")
        except (APIConnectionError, APIStatusError, ExecutionBlocked, ExecutionTruncated) as error:
            # 原生提议器捕获 Exception 后会转入模板路径；服务失败必须终止并保留原始异常。
            raise NativeServiceFailure("SKILLAA 模型传输终止") from error
        usage = response.usage
        return response.choices[0].message.content, {
            "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens}

    def __call__(self, system, user, **kwargs):
        return self.request(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)


def install_transport(model, prefix):
    from graphopt import model as native_model

    native_model.configure_openlux(endpoint=os.environ["MODER_PORVIDER"], api_key=os.environ["MODEL_API"])
    native_model.set_optimizer_deployment("deepseek-flash")
    native_model.set_target_deployment("deepseek-flash")
    native_model.set_reasoning_effort("high")
    transport = NativeTransport(model, prefix)
    # 官方 chat_target/chat_optimizer 保持原协议，唯一替换点为真实计费的模型传输。
    native_model._chat_messages = transport.request
    return transport


def rollout(model, graph, items, prefix):
    from graphopt.optimizer.semantic_convert import graph_to_prompt
    from graphopt.runtime_envs.searchqa.rollout import process_one
    from graphopt.engine.rollout_readout import attach_graph_usage, attach_trajectories, attach_semantic_reasoning_traces
    from graphopt.evolution.trace_attribution import attach_trace_evidence

    folder = AUDIT / "rollouts" / prefix
    request_identity = fingerprint({"graph": graph.to_dict(), "items": items, "model": "deepseek-flash"})
    freeze(folder / "input.json", {"identity": request_identity})
    path = folder / "results.json"
    if path.exists():
        return read(path)
    install_transport(model, prefix)
    skill = graph_to_prompt(graph, environment="searchqa", protocol="case_complete")
    rows = []
    for item in items:
        result_path = folder / "cases" / (fingerprint(str(item["id"])) + ".json")
        if result_path.exists():
            row = read(result_path)
        else:
            # 使用原生 SearchQA 提示与评分；传输失败必须上抛，禁止纳入任务失败率。
            row = process_one(item, str(folder), skill, max_turns=1,
                              max_completion_tokens=POLICY["execution_output_tokens"], request_retries=1)
            if not row["agent_ok"]:
                save_json(result_path.with_suffix(".failure.json"), row)
                raise RuntimeError(row["fail_reason"])
            save_json(result_path, row)
        rows.append(row)
    attach_trajectories(folder, rows)
    attach_semantic_reasoning_traces(folder, rows)
    attach_graph_usage(folder, rows, valid_nodes=set(graph.nodes), valid_edges={edge.id for edge in graph.edges})
    attach_trace_evidence(graph, rows)
    freeze(path, rows)
    return rows


def grouped(protocol):
    from graphopt.engine.grouped_schedule import GroupedBatch, TrainValidationGroup

    groups = [TrainValidationGroup(tuple(map(str, group["train_ids"])), tuple(map(str, group.get("val_ids", []))),
                                  group.get("group_type", "")) for group in protocol["groups"]]
    return GroupedBatch(list(protocol["history_ids"]), [], groups)


def audit_rollout(model, graph, items, prefix, reusable):
    known = {str(row["id"]): row for row in reusable}
    pending = [item for item in items if str(item["id"]) not in known]
    if pending:
        known.update({str(row["id"]): row for row in rollout(model, graph, pending, prefix)})
    rows = [known[str(item["id"])] for item in items]
    freeze(AUDIT / "rollouts" / prefix / "audit_results.json", {
        "graph": fingerprint(graph.to_dict()), "reused_ids": [str(item["id"]) for item in items if str(item["id"]) in {str(row["id"]) for row in reusable}],
        "rows": rows})
    return rows


def gates(graph, patch, batch, before, after):
    from graphopt.engine.trainer import select_epoch_affected_cases, summarize_epoch_local_gate, _hard_transition_summary
    from graphopt.runtime_envs.searchqa.pipeline import accept_complete_candidate

    scope = select_epoch_affected_cases(grouped_batch=batch, base_graph=graph, patch=patch,
                                         reference_train=before, reference_val=[])
    wanted = set(scope["train_case_ids"])
    local = summarize_epoch_local_gate(patch, reference_train=[row for row in before if row["id"] in wanted],
        candidate_train=[row for row in after if row["id"] in wanted], reference_val=[], candidate_val=[])
    complete = _hard_transition_summary(before, after)
    # SearchQA 原生无 validation split：完整 update pool 是 BigGate 的主要判定集合。
    big = accept_complete_candidate(complete, {}, allow_validation_tie=False, use_train_tiebreak=False)
    return {"scope": scope, "local": local, "big": {"accepted": big, "audit": complete}}


def run():
    require_ready()
    from engine.timing_research import require_preflight
    require_preflight()
    native()
    from graphopt.types import GraphPatch, SkillGraph
    from graphopt.evolution.engine import run_evolution_step
    from graphopt.evolution.config import EvolutionConfig
    from graphopt.evolution.materialize import materialize_skill_graph
    from graphopt.optimizer.skill import atomic_edit_groups
    from graphopt.engine.trainer import select_epoch_affected_cases, explain_epoch_local_gate
    from engine.timing_research import Design

    protocol = read(AUDIT / "inventory.json")
    design = Design.model_validate(read(OUT / "design.json"))
    meter, model = meter_model()
    freeze(AUDIT / "implementation.json", implementation())
    graph = SkillGraph.from_dict(protocol["graph"])
    batch = grouped(protocol)
    items = protocol["items"]
    attempts = []
    for source in protocol["source_ids"]:
        folder = AUDIT / "edits" / source
        before = rollout(model, graph, [items[case_id] for case_id in protocol["history_ids"]], source + "/old")
        source_group = next(group for group in batch.groups if source in group.train_ids)
        source_rows = [row for row in before if row["id"] in source_group.train_ids]
        proposal_path = folder / "proposal.json"
        if proposal_path.exists():
            patch = GraphPatch.from_dict(read(proposal_path))
        else:
            transport = install_transport(model, source + "/proposal")
            evolution = run_evolution_step(graph.copy(), source_rows,
                cfg=EvolutionConfig(analyst_workers=1, success_guard_workers=1), chat_fn=transport,
                mode="teacher", step_dir=folder / "native_proposal", update_protocol="case_complete")
            patch = GraphPatch(edits=evolution.patch_edits, reasoning=evolution.reasoning)
            freeze(proposal_path, patch.to_dict())
        atomic = atomic_edit_groups(patch.edits)
        selected_path = folder / "selected.json"
        if not atomic:
            freeze(folder / "status.json", {"status": "no_native_edit", "source": source})
            attempts.append({"source": source, "status": "no_native_edit"})
            continue
        if selected_path.exists():
            single = GraphPatch.from_dict(read(selected_path)["patch"])
        else:
            # 原生 atomic group 是单次知识干预；按原生生成顺序取首个合法编辑。
            single = GraphPatch(edits=[patch.edits[index] for index in atomic[0][1]], reasoning=patch.reasoning)
            materialize_skill_graph(graph, single)
            freeze(selected_path, {"source": source, "atomic_group": atomic[0][0], "patch": single.to_dict()})
        candidate = materialize_skill_graph(graph, single)
        scope = select_epoch_affected_cases(grouped_batch=batch, base_graph=graph, patch=single,
                                            reference_train=before, reference_val=[])
        local_ids = list(scope["train_case_ids"])
        outside = [case_id for case_id in protocol["audit_order"] if case_id not in local_ids]
        # 门禁范围全部保留；新增真实题直到审计池至少半数在 LocalGate 之外。
        pool = list(dict.fromkeys([*local_ids, *outside[:max(6, len(local_ids))]]))
        for case_id in protocol["audit_order"]:
            if len(pool) >= 12:
                break
            if case_id not in pool:
                pool.append(case_id)
        if sum(case_id not in local_ids for case_id in pool) < len(pool) / 2:
            raise ValueError("真实来源池不足以提供至少半数范围外审计任务")
        registration_path = folder / "registration.json"
        if registration_path.exists():
            registration = read(registration_path)
            pool = registration["pool"]
            local_ids = list(registration["scope"]["train_case_ids"])
        else:
            registration = {"source": source, "scope": scope, "pool": pool, "patch": single.to_dict(),
                            "base_graph": fingerprint(protocol["graph"]), "predictor": design.predictor_instructions}
            freeze(registration_path, registration)
        prediction_path = folder / "prediction.json"
        prediction_status_path = folder / "prediction_status.json"
        def validate_prediction(value):
            if (len(value.tasks) != len(local_ids) or len(set(value.tasks)) != len(value.tasks)
                    or not set(value.tasks) <= set(pool) or set(value.latest_boundary) != set(pool)):
                raise ValueError(f"预测必须提供池内 {len(local_ids)} 个不同任务 ID，并为审计池每个任务登记边界")
        if prediction_status_path.exists():
            prediction_status = read(prediction_status_path)
            prediction = (Prediction.model_validate(read(prediction_path))
                          if prediction_status["status"] == "valid" else None)
        else:
            try:
                prediction = ask(model, design.predictor_instructions,
                    {"tasks": {case_id: items[case_id]["question"] for case_id in pool}, "old_evidence": before,
                     "edit": single.to_dict(), "size_limit": len(local_ids)}, Prediction,
                    "skillaa_prediction/" + source, validate_result=validate_prediction)
            except AIFormatError:
                prediction = None
                prediction_status = {"status": "invalid_model_output", "phase": "skillaa_prediction/" + source}
            else:
                freeze(prediction_path, prediction.model_dump(mode="json"))
                prediction_status = {"status": "valid", "phase": "skillaa_prediction/" + source}
            freeze(prediction_status_path, prediction_status)
        after = rollout(model, candidate, [items[case_id] for case_id in protocol["history_ids"]], source + "/new")
        decisions = gates(graph, single, batch, before, after)
        transport = install_transport(model, source + "/local_explanation")
        decisions["local"]["semantic_explanation"] = explain_epoch_local_gate(single, decisions["local"], chat_fn=transport)
        freeze(folder / "native_gates.json", decisions)
        audit_before = audit_rollout(model, graph, [items[case_id] for case_id in pool], source + "/audit_old", before)
        audit_after = audit_rollout(model, candidate, [items[case_id] for case_id in pool], source + "/audit_new", after)
        repeats = rollout(model, graph, [items[case_id] for case_id in protocol["repeat_order"]], source + "/old_repeat")
        regressions = [row["id"] for old, row in zip(audit_before, audit_after, strict=True)
                       if old["hard"] == 1 and row["hard"] == 0 and row["id"] not in local_ids]
        repeat_old = rollout(model, graph, [items[case_id] for case_id in regressions], source + "/regression_old_repeat") if regressions else []
        repeat_new = rollout(model, candidate, [items[case_id] for case_id in regressions], source + "/regression_new_repeat") if regressions else []
        result = {"source": source, "registration": registration,
                  "prediction": prediction.model_dump(mode="json") if prediction is not None else None,
                  "prediction_status": prediction_status,
                  "before": audit_before, "after": audit_after, "old_repeats": repeats, "history_old": before,
                  "repeat_old": repeat_old, "repeat_new": repeat_new, "gates": decisions}
        freeze(folder / "result.json", result)
        attempts.append({"source": source, "status": "audited", "local_accepted": decisions["local"]["accepted"]})
    freeze(AUDIT / "complete.json", attempts)


def analyze():
    native()
    from engine.timing_analysis import capture_interval, expense, ledger_rows

    rows = []
    for path in sorted((AUDIT / "edits").glob("*/result.json")):
        result = read(path)
        source = result["source"]
        local = set(result["registration"]["scope"]["train_case_ids"])
        predicted = set(result["prediction"]["tasks"]) if result["prediction"] is not None else None
        regressed = {new["id"] for old, new in zip(result["before"], result["after"], strict=True)
                     if old["hard"] == 1 and new["hard"] == 0}
        repeated = {new["id"] for old, new in zip(result["repeat_old"], result["repeat_new"], strict=True)
                    if old["hard"] == 1 and new["hard"] == 0}
        history = {row["id"]: row for row in result["history_old"]}
        noise = sum(history[row["id"]]["hard"] != row["hard"] for row in result["old_repeats"])
        ledger = ledger_rows()
        costs = expense([row for row in ledger if row["phase"].startswith("skillaa/" + source + "/")])
        prediction_cost = expense([row for row in ledger if row["phase"] == "skillaa_prediction/" + source])
        per_case = {}
        for task in result["registration"]["pool"]:
            from graphopt.runtime_envs.searchqa.rollout import _build_user
            from engine.timing_research import read as load
            item = load(AUDIT / "inventory.json")["items"][task]
            per_case[task] = [row for row in ledger if row["phase"].startswith(("skillaa/" + source + "/audit_new/", "skillaa/" + source + "/new/"))
                and (OUT / "io" / row["call"] / "request.json").is_file()
                and load(OUT / "io" / row["call"] / "request.json")["messages"][-1]["content"] == _build_user(
                    item, diagnostic_mode=False, diagnostic_instruction="", diagnostic_trace_context="")]
        local_cost = expense([row for task in local for row in per_case.get(task, [])])
        predicted_cost = expense([row for task in predicted for row in per_case.get(task, [])]) if predicted is not None else None
        predicted_total = predicted_cost["billed_cny"] + prediction_cost["billed_cny"] if predicted_cost is not None else None
        local_efficiency = len(regressed & local) / local_cost["billed_cny"] if local_cost["billed_cny"] else None
        predicted_efficiency = len(regressed & predicted) / predicted_total if predicted_total else None
        value = (predicted_efficiency is not None and local_efficiency is not None
                 and predicted_efficiency > local_efficiency) if predicted is not None else None
        rows.append({"source": source, "regressions": sorted(regressed), "outside_regressions": sorted(regressed - local),
            "repeated_outside_regressions": sorted(repeated - local), "no_edit_flips": noise,
            "no_edit_repeat_count": len(result["old_repeats"]), "local": sorted(local),
            "predicted": sorted(predicted) if predicted is not None else None,
            "prediction_status": result["prediction_status"],
            "capture_local": capture_interval(len(regressed & local), len(regressed)),
            "capture_predicted": capture_interval(len(regressed & predicted), len(regressed)) if predicted is not None else None,
            "captured_repeated_outside": sorted(repeated & predicted - local) if predicted is not None else None, "audit_cost": costs,
            "prediction_cost": prediction_cost, "local_execution_cost": local_cost, "predicted_execution_cost": predicted_cost,
            "local_captures_per_cny": local_efficiency, "predicted_captures_per_cny": predicted_efficiency,
            "economic_value": value,
            "local_accepted": result["gates"]["local"]["accepted"], "big_accepted": result["gates"]["big"]["accepted"]})
    qualifying = sum(bool(row["captured_repeated_outside"]) and len(row["repeated_outside_regressions"]) /
        len(read(AUDIT / "edits" / row["source"] / "result.json")["registration"]["pool"]) >
        row["no_edit_flips"] / row["no_edit_repeat_count"] for row in rows)
    economic_sources = sum(bool(row["captured_repeated_outside"]) and row["economic_value"] for row in rows)
    save_json(AUDIT / "analysis.json", {"sources": rows, "repeated_predictable_sources": qualifying,
        "mechanism_threshold_met": qualifying >= 2, "economic_sources": economic_sources,
        "external_threshold_met": qualifying >= 2 and economic_sources >= 2,
        "local_accepts": sum(row["local_accepted"] for row in rows)})
    return rows
