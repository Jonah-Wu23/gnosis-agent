import asyncio
import json
import os
import sqlite3

from engine.activation import components_version, validation_plan
from engine.ecai_followup import (ABLATION, BASE, HELDOUT, LEDGER, OUT, ROOT, EditProposal,
    StructuredEdit, implementation_files, liabilities, load_components, parse_proposal,
    prepare, proposal_messages, runtime_units)
from engine.ecai_followup_report import contract_audit, exact_difference, holm
from engine.evolution import EvolutionWorkspace
from engine.research_evaluator import ResearchEvaluator
from engine.run_ecai import read
from engine.scoped_units import Budget, POLICY, fingerprint, save_json


def ledger_state(path):
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT id,phase,cost,billed,reserved FROM calls ORDER BY id").fetchall()


async def real_environment(plan, task, output):
    from automationbench.rubric import create_rubric
    from engine.automation_adapter import ScopedAutomationEnv

    evaluator = ResearchEvaluator(plan, Budget(output / "assets.sqlite", None, output=output))
    dataset, row = evaluator.task(task)
    env = ScopedAutomationEnv(dataset=dataset, rubric=create_rubric(), max_turns=POLICY["max_steps"],
                              toolset="api", execution_output=output)
    await env._teardown()
    return evaluator.version, row


def check():
    os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
    original_ledgers = [ROOT / "state/research_ledger.sqlite", ROOT / "state/ecai_p1_ledger.sqlite", LEDGER]
    before = {str(path): ledger_state(path) for path in original_ledgers}
    protocol = prepare()
    assert prepare() == protocol
    folder = ROOT / ".offline_checks/ecai_followup"
    plan = {"tasks": protocol["tasks"], "search": [source["task"] for source in protocol["sources"]],
            "selection": protocol["mechanism_tasks"], "heldout": protocol["heldout_tasks"], "checkpoint": []}
    names = set(plan["search"])
    assert len(names) + len(protocol["missing_source_domains"]) == 6
    assert len(protocol["slots"]) + len(protocol["unavailable_slots"]) == 24
    assert not names & set(plan["selection"]) and not (names | set(plan["selection"])) & set(plan["heldout"])
    domains = {protocol["tasks"][task]["domain"] for task in names} | set(protocol["missing_source_domains"])
    for domain in domains:
        assert sum(protocol["tasks"][task]["domain"] == domain for task in plan["selection"]) == 2
        assert sum(protocol["tasks"][task]["domain"] == domain for task in plan["heldout"]) == 3
    for task in [*plan["search"], *plan["selection"], *plan["heldout"]]:
        version, row = asyncio.run(real_environment(plan, task, folder))
        assert fingerprint(row) == protocol["tasks"][task]["contract"]
        assert version == protocol["experiment_version"]
    for source in protocol["sources"]:
        slots = [slot for slot in protocol["slots"] if slot["source"] == source["task"]]
        payloads = [json.loads(proposal_messages(protocol, slot)[1]["content"]) for slot in slots]
        assert all(payload["current_units"] == [] for payload in payloads)
        assert all(payload["evidence"] == payloads[0]["evidence"] and payload["legal_scope"] == payloads[0]["legal_scope"]
                   and payload["tool_instructions"] == payloads[0]["tool_instructions"] for payload in payloads)
    for block in protocol["blocks"]:
        assert {(slot["arm"], slot["repeat"]) for slot in block["order"]} == {
            (arm, repeat) for arm in protocol["versions"] for repeat in range(2)}
    for arm, saved in protocol["versions"].items():
        workspace = EvolutionWorkspace(folder / arm, protocol["experiment_version"])
        version = workspace.write_version(saved["units"])
        assert version == saved["version"]
        load_components(workspace, version)
    gnosis = protocol["versions"]["gnosis"]["units"]
    assert protocol["versions"]["gnosis_no_e"]["units"] == [unit for unit in gnosis if unit["id"] != "ace_op_vp_guard"]

    # 使用 P1 真实提议检查原始协议、现有激活计划与发布判断；不构造候选或成绩。
    checked, statistics = [], {}
    for label in ("C1", "C2", "C3"):
        candidate = read(BASE / "mechanism" / label / "candidate.json")
        units = read(BASE / "mechanism" / label / "versions" / candidate["version"] / "manifest.json")["units"]
        proposal = EditProposal(action="edit", reason="P1 冻结真实候选", units=units)
        parsed = parse_proposal(proposal.model_dump_json(), "A", {value for unit in units for value in unit["scope"]})
        assert runtime_units(parsed) == units
        assert set(StructuredEdit.model_fields) == set(EditProposal.model_fields)
        assert set(StructuredEdit.model_fields["units"].annotation.__args__[0].model_fields) - set(type(proposal.units[0]).model_fields) == {
            "service", "endpoint_tool", "activation_predicate", "intended_effect", "pre_activation_visibility", "scope_rationale"}
        comparison = read(BASE / "mechanism" / label / "comparison.json")
        baseline = read(BASE / "mechanism/baseline.json")["B0"]
        old_version = baseline[0]["experiment_version"]
        selected = validation_plan([], units, baseline, old_version)
        assert selected == comparison["plan"] and all(item["affected"] for item in selected)
        from engine.ecai_followup import release_decision
        measured = release_decision(baseline, comparison["records"], [row["task"] for row in baseline[::2]], 2)
        assert measured["accepted"] == comparison["full"]["accepted"]
        delta = [sum(comparison["records"][index + repeat]["success"] - baseline[index + repeat]["success"]
                     for repeat in range(2)) / 2 for index in range(0, len(baseline), 2)]
        stats = exact_difference(delta)
        statistics[label] = stats
        assert stats["paired_units"] == 18 and 0 <= stats["p_two_sided"] <= 1
        audit = contract_audit(units, comparison["records"][0])
        # 旧执行缺少新增可见性记录，必须如实返回未知，不能自动通过。
        assert audit["status"] == "UNKNOWN" and audit["missing"]
        checked.append(label)
    adjusted = holm(statistics)
    assert all(row["p_holm"] >= row["p_two_sided"] for row in adjusted.values())
    liability = liabilities()
    assert sum(row["future_cny"] for row in liability) <= 6.47
    assert sum(protocol["allocations_cny"].values()) == 134.27
    after = {str(path): ledger_state(path) for path in original_ledgers}
    assert before == after
    proof = {"status": "PASS", "protocol": fingerprint(protocol), "files": implementation_files(),
        "experiment_version": protocol["experiment_version"], "model_calls": 0,
        "real_tasks_checked": len(plan["search"]) + len(plan["selection"]) + len(plan["heldout"]),
        "real_frozen_versions_loaded": list(protocol["versions"]), "real_p1_candidates_checked": checked,
        "ledgers_unchanged": True, "missing_source_domains": protocol["missing_source_domains"],
        "formal_experiment_ready": protocol["ready"], "checks": ["frozen_task_contracts_and_native_environments", "independent_source_inputs",
            "runtime_conversion", "original_versions_and_exact_no_e", "real_p1_plans_release_statistics",
            "visibility_metadata_missing_remains_unknown", "cash_allocation_and_liability", "idempotent_freeze"]}
    save_json(folder / "result.json", proof)
    print(json.dumps({key: value for key, value in proof.items() if key != "files"}), flush=True)


if __name__ == "__main__":
    check()
