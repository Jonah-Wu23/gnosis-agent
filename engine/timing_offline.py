import asyncio
import inspect
import json
import os
import sqlite3

import ijson

from engine.activation import (KnowledgeTreatment, TaskBoundary, knowledge_validation_tasks, provide_knowledge)
from engine.scoped_units import fingerprint, save_json
from engine.timing_research import (ARMS, CHECK, OUT, ROOT, compact_record, implementation, prepare, read)


def ledger_snapshot():
    result = {}
    for path in sorted((ROOT / "state").glob("*.sqlite")):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "calls" in tables:
                result[str(path)] = db.execute("SELECT id,phase,cost,billed,reserved FROM calls ORDER BY id").fetchall()
    return result


async def load_environment(task, metadata):
    from engine.research_evaluator import ResearchEvaluator
    from engine.automation_adapter import ScopedAutomationEnv
    from engine.scoped_units import Budget, POLICY
    from automationbench.rubric import create_rubric

    meter = Budget(CHECK / "assets.sqlite", None, output=CHECK)
    evaluator = ResearchEvaluator({"tasks": {task: metadata}}, meter)
    dataset, row = evaluator.task(task)
    env = ScopedAutomationEnv(dataset=dataset, rubric=create_rubric(), max_turns=POLICY["max_steps"], toolset="api")
    await env._teardown()
    assert fingerprint(row) == metadata["contract"]


def check():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    before = ledger_snapshot()
    inventory = prepare()
    assert inventory["discovery"] and inventory["order"]
    assert not set(inventory["order"]) & set(inventory["excluded_tasks"])
    assert not {value["template"] for value in inventory["tasks"].values()} & set(inventory["excluded_templates"])
    asset = inventory["discovery"][0]
    source = asset["source"]
    paths = sorted((ROOT / "experiments/ecai_2027/mechanism/executions").glob("*/*.json"))
    candidate = next(read(path) for path in sorted((ROOT / "experiments/ecai_2027/mechanism").glob("C*/candidate.json"))
                     if read(path)["source"] == source)
    request_path = next(path for path in paths if path.stem == candidate["attempts"][0]["execution_id"])
    record = compact_record(request_path)
    trace = record["boundary_trace"]
    event = next(event for event in trace if event["hook"] == "after_call" and event["tool"] == "api_fetch")
    delay = TaskBoundary(hook="after_call", tool=event["tool"], service=event["service"], endpoint=event["endpoint"])
    split = asset["body"].find("\n") + 1
    assert 0 < split < len(asset["body"])
    base = {"source": source, "task": source, "selected": True, "route_version": fingerprint("discovery"),
            "contract": asset["body"][:split], "remainder": asset["body"][split:], "delay": delay.model_dump()}
    checks = []
    for arm in ARMS:
        policy = KnowledgeTreatment(treatment=arm, **base)
        early, early_parts = policy.text_at([])
        late, late_parts = policy.text_at(trace)
        assert early == (asset["body"] if arm in ("G", "L") else base["contract"] if arm == "C" else "")
        assert late == ("" if arm == "N" else asset["body"])
        assert late_parts == ([] if arm == "N" else ["contract", "remainder"])
        assert policy.text_at(trace[:event["index"]])[0] == early
        unseen = policy.model_copy(update={"selected": False})
        assert unseen.text_at(trace)[0] == (asset["body"] if arm == "G" else "")
        checks.append("real_repair_" + arm)
    # 重放真实请求的注入边界，不生成模型响应或任务结果。
    with request_path.open("rb") as stream:
        wire = next(ijson.items(stream, "wire_trace.item", use_float=True))
    initial = wire["request"]
    injected = {}
    for arm in ARMS:
        state = {"knowledge_treatment": KnowledgeTreatment(treatment=arm, **base).model_dump(), "boundary_trace": []}
        messages, parts = provide_knowledge(initial, state)
        injected[arm] = messages
        if parts:
            assert messages[0]["role"] == "system" and messages[1:] == initial
        else:
            assert messages == initial
    assert injected["G"] == injected["L"]
    checks.append("same_native_message_container")
    routes = {source: True, inventory["order"][0]: False}
    old = {"route_version": base["route_version"], "contract": base["contract"], "remainder": ""}
    new = {**old, "remainder": base["remainder"]}
    assert knowledge_validation_tasks(old, new, routes, list(routes), list(routes)) == [source]
    assert set(knowledge_validation_tasks(new, {**new, "contract": ""}, routes, list(routes), list(routes))) == set(routes)
    assert set(knowledge_validation_tasks(new, {**new, "route_version": ""}, routes, list(routes), list(routes))) == set(routes)
    assert knowledge_validation_tasks(new, new, routes, list(routes), list(routes)) == []
    checks.append("body_contract_route_dependencies")
    asyncio.run(load_environment(inventory["order"][0], inventory["tasks"][inventory["order"][0]]))
    checks.append("real_automationbench_task_and_strict_rubric")
    from engine.skillaa_audit import native, prepare as prepare_skillaa, grouped
    native()
    external = prepare_skillaa()
    from graphopt.types import SkillGraph
    from graphopt.optimizer.skill import load_graph, atomic_edit_groups
    from graphopt.evolution.materialize import materialize_skill_graph
    from graphopt.engine.trainer import diff_skill_graphs, select_epoch_affected_cases, summarize_epoch_local_gate
    from graphopt.runtime_envs.searchqa.pipeline import accept_complete_candidate
    from graphopt.optimizer.semantic_convert import graph_to_prompt
    from graphopt.evolution.engine import run_evolution_step
    from graphopt.runtime_envs.searchqa.rollout import process_one
    from graphopt.types import GraphPatch
    from engine.timing_research import SKILLAA

    graph = SkillGraph.from_dict(external["graph"])
    published = load_graph(SKILLAA / "graphopt/envs/searchqa/graphopt/best_graph.json")
    diff = diff_skill_graphs(graph, published)
    assert diff.edits
    atomic = atomic_edit_groups(diff.edits)
    single = GraphPatch(edits=[diff.edits[index] for index in atomic[0][1]])
    changed = materialize_skill_graph(graph, single)
    assert fingerprint(changed.to_dict()) != fingerprint(graph.to_dict())
    rendered = graph_to_prompt(graph, environment="searchqa", protocol="case_complete")
    assert rendered and all(node_id in rendered for node_id in graph.nodes)
    assert len(grouped(external).groups) == 3
    assert len(grouped(external).train_ids) == 12
    assert not set(external["history_ids"]) & set(external["test_ids"])
    assert len(external["source_ids"]) == 3 and len(set(external["source_ids"])) == 3
    for function in (select_epoch_affected_cases, summarize_epoch_local_gate, accept_complete_candidate,
                     run_evolution_step, process_one):
        assert function.__module__.startswith("graphopt.")
    assert "reference_train" in inspect.signature(select_epoch_affected_cases).parameters
    checks.append("official_skillaa_host_data_single_edit_gate_imports")
    from engine.timing_analysis import paired_effect
    # 统计只读取已结算的真实旧实验结果；计算检查不把旧样本计入确认统计。
    outcomes = []
    for candidate_path in sorted((ROOT / "experiments/ecai_2027/mechanism").glob("C*/candidate.json")):
        candidate = read(candidate_path)
        if candidate["status"] != "mature":
            continue
        outcomes.append(int(candidate["attempts"][-1]["success"]) - int(candidate["attempts"][0]["success"]))
    assert paired_effect(outcomes)["independent_sources"] == len(outcomes)
    checks.append("statistics_real_historical_outcomes")
    assert before == ledger_snapshot()
    result = {"decision": "PASS", "files": implementation(), "inventory": fingerprint(inventory),
              "checks": checks, "model_calls": 0, "real_ledgers_unchanged": True,
              "skillaa_commit": external["upstream"], "data_revision": external["data_revision"]}
    save_json(CHECK / "result.json", result)
    print(json.dumps({key: result[key] for key in ("decision", "checks", "model_calls", "real_ledgers_unchanged")}, ensure_ascii=False))
