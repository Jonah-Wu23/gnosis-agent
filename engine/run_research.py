import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import math
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from engine.activation import components_version, validation_plan, reuse_evidence
from engine.component_host import HostError
from engine.evolution import ActionRejected, EvolutionAction, EvolutionWorkspace, ResearchBudget, paired_selection
from engine.research_plan import ROOT, ARMS, PLAN_PATH, ARM_LEARNING_LIMITS, prepare_plan
from engine.research_upstream import starharness, official_ace, verify_upstreams
from engine.research_model import ResearchModel, ACETransport
from engine.scoped_units import (Budget, BudgetExceeded, ServiceWindowExceeded, POLICY, AIFormatError, ExecutionBlocked,
                                 ExecutionTruncated, save_json, experiment_version, require_model_access,
                                 VERSION_FILES, append_record, fingerprint)

OUT = ROOT / "experiments/research"
OFFLINE_PROOF = ROOT / "state/research_offline_check.json"
RESEARCH_LEDGER = ROOT / "state/research_ledger.sqlite"

REPAIR_INSTRUCTIONS = """You evolve a frozen-weight agent by editing an entire group of reusable components.
Keep partial progress in the working version. Use real task execution to correct your hypotheses repeatedly.
Choose tasks yourself after reading traces. Inspect successful and failed development tasks, and cover domains
according to expected repair value. Selection feedback is available after a source task fully succeeds.
The final holdout is never accessible. Stop when further repair has low value or the remaining budget is exhausted.
Return one JSON object conforming to the supplied action schema. edit replaces the entire working component group;
preserve useful existing components yourself, and freely change, combine, or delete components.
content holds the full reusable experience, summary supports retrieval, source holds executable TypeScript.
scope=['GLOBAL'] is exclusive with all local scopes. Local scopes match actual service, endpoint, or tool metadata.
Before the first matching registered boundary, components are completely absent from prompts, observations and tools.
E exports onBoundary({event,input,task,state,results?,budget}). It returns any of:
{text:string,state}, {messages:Message[],state}, {arguments:object,state},
{tools:[{name,description,parameters}],state}, {calls:[{tool:'api_fetch'|'api_search',arguments}],state},
or {result:any,state,trace:any}. All fields are optional. Unknown fields fail protocol validation.
arguments can change only at before_call; messages only at context/workflow. tools are exposed only AFTER activation.
Calls use the task's original capabilities, permissions and evaluator. Batches return real results to the same function
as results:[{tool,arguments,raw}], together with your state, until you return no calls. Return final operation output in result.
An exposed function is invoked with event.hook='operation' and input={operation:name,arguments}.
Calls you issue yourself return their real results only to you; other E components still see them at the
registered boundary positions, while text knowledge and T/C/W units apply to the tested agent's own calls.
Use these functions for paging, batch reads, exact computation, multi-step operations and write/read confirmation.
Hooks: task_start, tool_discovery, before_call, after_call, context, workflow, operation.
T exports formatObservation({raw,step,code}) -> {text}; C exports buildContext(input) -> {messages,state?};
W exports beforeStep(input) -> {action:'continue'|'inject'|'stop',message?,reason?}.
T/C/W and text units use task_start/after_call activation. E can use all registered hooks.
inspect requests full evidence for an allowed task. execute runs a development task with the working group.
validate compares mature working/released groups on the independent selection tasks listed in validation_tasks,
using the pre-registered symmetric repeats. Holdout tasks are never listed and never accessible.
Model weights, task contracts, scoring, capabilities and output conditions are fixed across arms.
"""


def evidence_view(record):
    """修复器看到的执行证据：完整作答轨迹、断言结果与扩展位置序列。

    wire_trace 逐步重复整段提示（单题可达 19MB），边界与动作里的原始观测也与作答轨迹重复，
    这些只留在磁盘记录里；修复器的输入因此随执行次数线性增长，不再撑爆上下文。
    """
    keep = ("task", "domain", "success", "partial_credit", "assertions", "version", "execution_id",
            "learning_role", "repeat", "termination_status", "execution_error", "harness_defects",
            "active_extensions", "first_effect_boundaries", "prompt", "completion", "cost_usd")
    view = {key: record.get(key) for key in keep}
    view["boundary_trace"] = [{k: e.get(k) for k in ("index", "hook", "service", "endpoint", "tool", "step")}
                              for e in record.get("boundary_trace", [])]
    view["interventions"] = [{k: e.get(k) for k in ("unit", "component", "kind", "hook", "boundary_index",
                                                     "effects", "output", "next_actual_tool_action")}
                             for e in record.get("injections", [])]
    return view


def outcome_view(record):
    return {key: record.get(key) for key in ("task", "repeat", "success", "partial_credit", "assertions",
                                              "termination_status", "execution_error", "execution_id")}


def arm_spent(meter, arm):
    return sum(row["cost_usd"] for row in meter.totals()
               if row["phase"].split("/")[0] in ("learning", "selection")
               and row["phase"].split("/")[1:2] == [arm])


def audit_output(output, version):
    proof = json.loads(OFFLINE_PROOF.read_text(encoding="utf-8"))
    if proof["status"] != "passed" or proof["experiment_version"] != version:
        raise ExecutionBlocked("当前版本尚未通过无模型调用的离线测试")
    # 准入证明的模式与覆盖范围如实写进运行记录，付费实验的证据基础可事后核对。
    save_json(output / "workflow_audit.json", {"script_status": "clear", "experiment_version": version,
              "offline_proof_mode": proof.get("mode"),
              "offline_tool_replay_tasks": proof.get("tool_replay_tasks"),
              "offline_harness_defects": proof.get("harness_defects"),
              "reviewed_files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in VERSION_FILES}})


def research_meter(output):
    """DeepSeek 官方 API 的独立账本：不设总额、窗口与单题上限，只记账。"""
    from engine.run_pilot import config
    meter = Budget(RESEARCH_LEDGER, limit=None, rates=config()["rate_card"], output=output)
    return ResearchBudget(meter, 0.0)


def make_adapter(plan, workspace, evaluator, arm):
    BenchmarkAdapter, decide, promotion_review = starharness()

    class Adapter(BenchmarkAdapter):
        name = "gnosis"
        editable_dirs = [str(workspace.root.relative_to(ROOT) / "versions")]
        out_of_scope = set()
        protected_files = set(VERSION_FILES) | {str(PLAN_PATH.relative_to(ROOT))}
        agent_env = {"GNOSIS_DISABLE_MODEL_CALLS": os.environ.get("GNOSIS_DISABLE_MODEL_CALLS", "0")}
        dev_scenarios = plan["search"]
        prompts_dir = ROOT / "docs"
        surfaces_path = ROOT / "docs/GNOSIS-先导后计划.md"
        domain_spec_path = surfaces_path

        def selection_scenarios(self):
            return plan["selection"]

        def held_out_scenarios(self):
            return plan["heldout"]

        def validate(self, repo):
            self.validate_scenario_sets(self.dev_scenarios, self.selection_scenarios(), self.held_out_scenarios())
            verify_upstreams()
            workspace.load_version(workspace.state["working"])

        def run_eval(self, name, scenarios, repeats, max_turns, concurrency, unit_timeout, repo, python, eval_env, log_path):
            if max_turns != POLICY["max_steps"] or concurrency != 1:
                raise ValueError("接入契约必须使用固定执行条件")
            async def run():
                rows = []
                for task in scenarios:
                    for repeat in range(repeats):
                        rows.append(await evaluator.execute(workspace, workspace.state["working"], task, arm,
                                    repeat=repeat, execution_id=fingerprint([name, task, repeat])))
                save_json(Path(log_path), {"records": rows})
            asyncio.run(run())
            return 0

        def read_summary(self, name, runs_dir, expected_scenarios=None):
            rows = json.loads((Path(runs_dir) / name).read_text(encoding="utf-8"))["records"]
            if expected_scenarios is not None and {r["task"] for r in rows} != set(expected_scenarios):
                raise ValueError("接入契约缺少任务执行")
            per = {task: sum(r["success"] for r in rows if r["task"] == task) / sum(r["task"] == task for r in rows)
                   for task in {r["task"] for r in rows}}
            return sum(per.values()) / len(per), per, sum(r["partial_credit"] for r in rows) / len(rows)

        def import_check(self, repo, python, eval_env):
            # 上游循环用自己的 import_ok 检查评测模块；这里复用同一目标：接入模块必须可导入。
            workspace.load_version(workspace.state["working"])
            importlib.import_module("automationbench.runner")
            return True

        def gather_traces(self, run_name, scenarios, runs_dir, trace_chars):
            permitted = set(plan["search"] + plan["selection"])
            if not set(scenarios) <= permitted:
                raise PermissionError("最终留出执行禁止进入提议器")
            # 完整证据始终保留，检索摘要不会替代轨迹。
            # 每道题给最近三次执行的完整作答证据，更早的执行只给结果；完整原始记录保留在磁盘。
            rows = [r for r in workspace.records() if r["task"] in scenarios]
            return json.dumps([evidence_view(r) if i >= len(rows) - 3 else outcome_view(r)
                               for i, r in enumerate(rows)], ensure_ascii=False)

        def render_task(self, iteration, n_iterations, frontier, traces):
            return REPAIR_INSTRUCTIONS + "\n" + json.dumps({"frontier": frontier, "traces": traces}, ensure_ascii=False)

    adapter = Adapter()
    adapter.validate(ROOT)
    return adapter, decide, promotion_review


class RepairController:
    def __init__(self, plan, workspace, evaluator, model, arm, baseline):
        self.plan, self.workspace, self.evaluator, self.model, self.arm = plan, workspace, evaluator, model, arm
        self.adapter, self.upstream_decide, self.upstream_gate = make_adapter(plan, workspace, evaluator, arm)
        self.baseline = baseline
        self.state = workspace.state
        self.state.setdefault("early_version", None)
        workspace.save()
        if not workspace.records():
            for record in baseline:
                workspace.observe(record)

    def feedback(self):
        records = self.workspace.records()
        return {"working_version": self.state["working"], "released_version": self.state["released"],
                "editable_implementation": self.workspace.load_version(self.state["working"]),
                "available_records": [{key: record.get(key) for key in ("task", "domain", "success", "partial_credit",
                       "version", "execution_id", "termination_status", "execution_error")} for record in records],
                "development_tasks": [{"task": t, "domain": self.plan["tasks"][t]["domain"]} for t in self.plan["search"]],
                "validation_tasks": [{"task": t, "domain": self.plan["tasks"][t]["domain"]} for t in self.plan["selection"]],
                "last_execution_feedback": self.state.get("last_feedback"),
                "budget": self.model.meter.state(), "arm_remaining_usd": ARM_LEARNING_LIMITS[self.arm] - arm_spent(self.model.meter, self.arm),
                "action_schema": EvolutionAction.model_json_schema()}

    def choose(self, index):
        messages = [{"role": "system", "content": REPAIR_INSTRUCTIONS},
                    {"role": "user", "content": json.dumps(self.feedback(), ensure_ascii=False)}]
        for attempt in range(POLICY["format_retries"] + 1):
            checkpoint = self.workspace.root / "requests" / (str(index) + "_" + str(attempt) + ".json")
            try:
                response = self.model.request(messages, "learning/" + self.arm + "/proposal", checkpoint,
                                               validate=EvolutionAction.model_validate_json,
                                               options={"response_format": {"type": "json_object"}})
                return EvolutionAction.model_validate_json(response.choices[0].message.content)
            except (ValidationError, AIFormatError) as error:
                if attempt == POLICY["format_retries"]:
                    raise
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                messages = [*messages, {"role": "assistant", "content": saved["response"]["choices"][0]["message"]["content"] or ""},
                            {"role": "user", "content": "Protocol validation failed: " + str(error)}]
        raise RuntimeError("自主修复选择没有返回操作")

    async def validate(self, source, index):
        self.workspace.require_mature()
        before_version, after_version = self.state["released"], self.state["working"]
        if before_version == after_version:
            raise ActionRejected("工作版本尚未产生改动")
        before = self.workspace.load_version(before_version)
        after = self.workspace.load_version(after_version)
        repeats = self.plan["selection_repeats"]
        path = self.workspace.root / "selection" / (before_version + ".json")
        selection_cost = 0.0
        if path.exists():
            base_records = json.loads(path.read_text(encoding="utf-8"))
        else:
            base_records = list(await asyncio.gather(*(
                self.evaluator.execute(self.workspace, before_version, task, self.arm, "selection", "selection", repeat,
                    execution_id=fingerprint([self.arm, "selection_base", before_version, task, repeat]))
                for task in self.plan["selection"] for repeat in range(repeats))))
            for record in base_records:
                self.workspace.observe(record)
                selection_cost += record["cost_usd"]
            save_json(path, base_records)
        plans = validation_plan(before, after, base_records, self.workspace.experiment_version,
                                global_validation=self.arm == "global")
        async def candidate(base, plan):
            if not plan["affected"]:
                # 未进入影响位置的证据直接复用，只有受影响的执行才重新计费。
                return reuse_evidence(base, after, plan)
            # 复测规则事先固定：两侧使用相同的题目与次数，缓存的选择证据作为对照侧。
            return await self.evaluator.execute(self.workspace, after_version, base["task"], self.arm, "selection", "selection", base["repeat"],
                    execution_id=fingerprint([self.arm, index, "selection_after", base["task"], base["repeat"]]))
        candidate_records = list(await asyncio.gather(*(candidate(b, p) for b, p in zip(base_records, plans, strict=True))))
        cost_usd = 0.0
        for record, plan in zip(candidate_records, plans, strict=True):
            if plan["affected"]:
                cost_usd += record["cost_usd"]
                self.workspace.observe(record)
        comparison = paired_selection(base_records, candidate_records, self.plan["selection"], repeats)
        per = {task: sum(r["success"] for r in candidate_records if r["task"] == task) / repeats for task in self.plan["selection"]}
        old_per = {task: sum(r["success"] for r in base_records if r["task"] == task) / repeats for task in self.plan["selection"]}
        mean = sum(per.values()) / len(per)
        old_mean = sum(old_per.values()) / len(old_per)
        vrate = sum(r["partial_credit"] for r in candidate_records) / len(candidate_records)
        old_vrate = sum(r["partial_credit"] for r in base_records) / len(base_records)
        improved, reason = self.upstream_decide(mean, per, vrate,
                           {"mean": old_mean, "per_scenario": old_per, "verifier_rate": old_vrate})
        promote, gate_reason, gate = self.upstream_gate(self.adapter, iter_log_dir=self.workspace.root / "comparisons",
                           model="", timeout_s=0, candidate_record={"mean": mean, "per_scenario": per, "verifier_rate": vrate},
                           frontier={"mean": old_mean, "per_scenario": old_per, "verifier_rate": old_vrate,
                                     "baseline_per_scenario": old_per},
                           patch_path=self.workspace.root / "comparisons" / (str(index) + ".json"),
                           candidate_run=after_version, test_flip_result=None)
        comparison.update(starharness_improved=improved, starharness_reason=reason,
                          starharness_gate={"promote": promote, "reason": gate_reason, "detail": gate},
                          validation_plan=plans, executed_count=sum(p["affected"] for p in plans),
                          reused_count=sum(not p["affected"] for p in plans), validation_cost_usd=cost_usd,
                          selection_cost_usd=selection_cost)
        # 事先固定的配对统计照常计算并完整记录；发布推进采用同一批证据上的确定性改进判定加零回退约束。
        solved = sum(1 for task in self.plan["selection"]
                     if all(row["success"] for row in base_records if row["task"] == task))
        regression_rate = comparison["losses"] / solved if solved else 0.0
        comparison["source_task"] = source
        comparison["paired_sage"] = {"significant": comparison["decision"] == "accepted",
                                     "wins": comparison["wins"], "losses": comparison["losses"],
                                     "p_value": comparison["p_value"], "alpha": POLICY["sage_alpha"],
                                     "penalty": POLICY["sage_penalty"], "regression_rate": regression_rate,
                                     "solved_tasks": solved, "repeats": repeats}
        accepted = improved and promote and regression_rate <= POLICY["sage_regression_cap"]
        comparison["decision"] = "accepted" if accepted else "abstained"
        comparison["decision_basis"] = "deterministic_gate_no_regression" if accepted else "rejected"
        self.workspace.publish(comparison)
        save_json(self.workspace.root / "selection" / (after_version + ".json"), candidate_records)
        save_json(self.workspace.root / "comparisons" / (str(index) + ".json"), comparison)
        return {"comparison": {k: v for k, v in comparison.items() if k != "validation_plan"},
                "selection_feedback": [outcome_view(r) for r in candidate_records]}

    async def checkpoint(self):
        if self.state.get("checkpoint_complete"):
            return
        if arm_spent(self.model.meter, self.arm) < ARM_LEARNING_LIMITS[self.arm] * self.plan["checkpoint_learning_fraction"]:
            return
        early = self.state.get("early_version")
        current = self.state["working"]
        if early is None or early == current:
            # 还没有任何版本达到成熟条件，或者当前工作版本与早期版本相同，比较没有意义。
            return
        tasks = {}
        pairs = [(task, label, version) for task in self.plan["checkpoint"]
                 for label, version in (("early", early), ("current", current))]
        done = await asyncio.gather(*(self.evaluator.execute(self.workspace, version, task, self.arm, "evaluation", "heldout",
                            execution_id=fingerprint([self.arm, "checkpoint", label, task]), kind="checkpoint")
                                      for task, label, version in pairs))
        for (task, label, version), record in zip(pairs, done, strict=True):
            tasks.setdefault(task, {})[label] = {"version": version, "success": record["success"],
                                                 "partial_credit": record["partial_credit"], "cost_usd": record["cost_usd"]}
        def rate(label, key):
            return sum(rows[label][key] for rows in tasks.values()) / len(tasks)
        records = {"early_version": early, "current_version": current, "tasks": tasks,
                   "early_strict_pass_rate": rate("early", "success"),
                   "current_strict_pass_rate": rate("current", "success"),
                   "early_partial_credit": rate("early", "partial_credit"),
                   "current_partial_credit": rate("current", "partial_credit"),
                   "cost_usd": sum(rows[label]["cost_usd"] for rows in tasks.values() for label in ("early", "current"))}
        save_json(self.workspace.root / "checkpoint.json", records)
        self.state["checkpoint_complete"] = True
        self.state["checkpoint"] = records
        self.workspace.save()

    async def run(self):
        while not self.state["frozen"]:
            index = len(self.state["actions"])
            pending = self.state["pending_action"]
            try:
                action = EvolutionAction.model_validate(pending["action"]) if pending else await asyncio.to_thread(self.choose, index)
            except (AIFormatError, ExecutionTruncated, ValidationError) as error:
                # 修复器输出不合协议或推理被截断：失败本身就是反馈，下一轮换新的请求序号重新选择。
                # 组额度兜底这条路径，反复失败的修复器最终以额度耗尽结束学习。
                self.state["actions"].append({"index": index, "action": None,
                                              "proposal_error": type(error).__name__})
                self.state["last_feedback"] = {"proposal_error": str(error), "error_type": type(error).__name__}
                self.workspace.save()
                continue
            except BudgetExceeded as error:
                if isinstance(error, ServiceWindowExceeded):
                    raise
                self.state["stop"] = {"error_type": type(error).__name__, "error": str(error)}
                self.workspace.freeze()
                break
            if not pending:
                self.state["pending_action"] = {"action": action.model_dump(), "index": index}
                self.workspace.save()
            try:
                if action.action == "stop":
                    feedback = {"reason": action.reason}
                elif action.action == "edit":
                    feedback = {"working_version": self.workspace.edit([u.model_dump() for u in action.units])}
                elif action.action == "inspect":
                    feedback = json.loads(self.adapter.gather_traces("", [action.task], self.workspace.root, 0))
                elif action.action == "execute":
                    if action.task not in self.plan["search"]:
                        raise ActionRejected("来源执行只能使用开发任务")
                    feedback = await self.evaluator.execute(self.workspace, self.state["working"], action.task, self.arm,
                                      execution_id=fingerprint([self.arm, index, "source"]))
                    self.workspace.observe(feedback)
                    if feedback["success"]:
                        self.workspace.source_succeeds(action.task, self.state["working"])
                        if self.state.get("early_version") is None:
                            # 早期能力取首次达到成熟条件的版本，避免与空版本做恒等比较。
                            self.state["early_version"] = self.state["working"]
                    feedback = evidence_view(feedback)
                else:
                    if action.task not in self.plan["selection"]:
                        raise ActionRejected("成熟版本验证只能使用独立选择任务")
                    feedback = await self.validate(action.task, index)
            except (ActionRejected, PermissionError) as error:
                # 协议拒绝退回修复器重新选择；文件损坏、版本冲突与基础设施错误照原样暴露。
                feedback = {"protocol_error": str(error), "error_type": type(error).__name__}
            except BudgetExceeded as error:
                # 组额度耗尽是学习的正常终点：冻结发布版本交给最终评估。
                # 服务窗口用满与接入层故障照原样上抛，pending_action 保留，重新运行从断点续跑。
                if isinstance(error, ServiceWindowExceeded):
                    raise
                self.state["pending_action"] = None
                self.state["stop"] = {"error_type": type(error).__name__, "error": str(error)}
                self.workspace.freeze()
                break
            self.state["actions"].append({"index": index, "action": action.model_dump()})
            self.state["last_feedback"] = feedback
            self.state["pending_action"] = None
            self.workspace.save()
            await self.checkpoint()
            if action.action == "stop":
                self.workspace.freeze()
                break
        return self.state["frozen"]


async def train_ace(plan, workspace, evaluator, model):
    if workspace.state.get("frozen"):
        # 已冻结的工作区不再接受学习，继续执行只做留出评估。
        return workspace.state["frozen"]
    Reflector, Curator, update_counts, stats, next_id = official_ace()
    from engine.llm_client import MODEL
    transport = ACETransport(model, "learning/ace/memory", workspace.root / "ace_requests")
    reflector = Reflector(transport, "sambanova", MODEL, max_tokens=POLICY["execution_output_tokens"])
    curator = Curator(transport, "sambanova", MODEL, max_tokens=POLICY["execution_output_tokens"])
    state = workspace.state
    state.setdefault("ace_playbook", "")
    state.setdefault("ace_done", [])
    for task in plan["search"]:
        if task in state["ace_done"]:
            continue
        try:
            record = await evaluator.execute(workspace, state["working"], task, "ace",
                                            execution_id=fingerprint(["ace", "source", task]))
            workspace.observe(record)
            question = "\n".join(m["content"] for m in record["prompt"] if m["role"] == "user")
            transport.role = "reflector"
            transport.validator = lambda value: json.loads(value)["bullet_tags"]
            log_dir = workspace.root / "ace_logs" / task
            log_dir.mkdir(parents=True, exist_ok=True)
            def official():
                with (log_dir / "official.log").open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log):
                    reflection, tags, _ = reflector.reflect(question, json.dumps(record["completion"]),
                                       json.dumps(record["completion"][-1:]), None,
                                       json.dumps({"success": record["success"], "assertions": record["assertions"]}),
                                       state["ace_playbook"], use_ground_truth=False, use_json_mode=True,
                                       call_id="gnosis_reflect_" + task, log_dir=str(log_dir))
                    playbook = update_counts(state["ace_playbook"], tags)
                    transport.role = "curator"
                    transport.validator = curator._extract_and_validate_operations
                    playbook, _, _, _ = curator.curate(playbook, reflection, question,
                               len(state["ace_done"]), len(plan["search"]), 32768, stats(playbook),
                               use_ground_truth=False, use_json_mode=True,
                               call_id="gnosis_curate_" + task, log_dir=str(log_dir), next_global_id=next_id(playbook))
                return playbook
            playbook = await asyncio.to_thread(official)
            state["ace_playbook"] = playbook
            if playbook:
                units = [{"id": "official_ace", "scope": ["GLOBAL"], "hooks": ["task_start"],
                          "summary": "Official ACE playbook", "content": playbook, "component": None, "source": None}]
                workspace.edit(units)
                state["released"] = state["working"]
            state["ace_done"].append(task)
            workspace.save()
        except BudgetExceeded as error:
            if isinstance(error, ServiceWindowExceeded):
                raise
            state["stop"] = {"error_type": type(error).__name__, "error": str(error)}
            workspace.save()
            break
        except (AIFormatError, ExecutionTruncated, ValueError, KeyError, TypeError) as error:
            # 官方 ACE 的空响应、解析错误与更新失败都要留下状态，同时照原样上抛。
            state.setdefault("ace_failures", []).append(
                {"task": task, "error_type": type(error).__name__, "error": str(error)})
            workspace.save()
            raise
    return workspace.freeze()


def phase_costs(meter):
    """按 用途/实验组/阶段 汇总真实账本费用，未结算的预留一并计入。"""
    costs = {}
    for row in meter.totals():
        parts = row["phase"].split("/")
        key = (parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
        costs[key] = costs.get(key, 0.0) + row["cost_usd"] + row["unsettled_usd"]
    return costs


def arm_costs(costs, arm):
    learning = sum(value for (bucket, name, _), value in costs.items()
                   if bucket in ("learning", "selection") and name == arm)
    checkpoint = costs.get(("evaluation", arm, "checkpoint"), 0.0)
    return {"learning_and_selection_usd": learning, "checkpoint_usd": checkpoint,
            "heldout_usd": costs.get(("evaluation", arm, "execution"), 0.0),
            "retest_usd": costs.get(("evaluation", arm, "retest"), 0.0),
            "development_usd": learning + checkpoint,
            "total_usd": learning + checkpoint + costs.get(("evaluation", arm, "execution"), 0.0)
                         + costs.get(("evaluation", arm, "retest"), 0.0)}


def termination_counts(rows):
    """每组的真实终止原因分布，预算触顶、截断与服务失败都要看得见。"""
    counts = {}
    for row in rows:
        key = (row.get("execution_error") or {}).get("category") or row.get("termination_status") or "completed"
        counts[key] = counts.get(key, 0) + 1
    return counts


def amortized_comparison(plan, gnosis_rows, repeat_rows, gnosis_total_usd):
    """按实际评估任务数比较两组累计摊销成本，并核对等美元规则是否真的成立。

    两组在留出评估上花掉的钱都来自真实账本：GNOSIS 组把美元前置花在学习与验证，
    重复执行组把同样的美元花在同一道题的反复执行上。交叉点由两条实测曲线给出，
    同时报告两组的总费用差与通过率差，避免只有摊销口径一个读数。
    """
    tasks = len(plan["heldout"])
    counts = []
    for count in range(1, tasks + 1):
        gnosis = (gnosis_total_usd - sum(row["cost_usd"] for row in gnosis_rows[count:])) / count
        repeat = sum(row.get("total_cost_usd", row["cost_usd"]) for row in repeat_rows[:count]) / count
        counts.append({"evaluation_tasks": count, "gnosis_amortized_usd": gnosis, "repeat_amortized_usd": repeat})
    crossover = next((row["evaluation_tasks"] for row in counts if row["gnosis_amortized_usd"] <= row["repeat_amortized_usd"]), None)
    gnosis_spent = sum(row["cost_usd"] for row in gnosis_rows)
    repeat_spent = sum(row.get("total_cost_usd", row["cost_usd"]) for row in repeat_rows)
    return {"gnosis_total_usd": gnosis_total_usd, "heldout_tasks": tasks,
            "break_even_tasks": crossover,
            "budget_parity": {"gnosis_usd": gnosis_total_usd, "repeat_usd": repeat_spent,
                              "difference_usd": gnosis_total_usd - repeat_spent,
                              "difference_ratio": (gnosis_total_usd - repeat_spent) / gnosis_total_usd if gnosis_total_usd else None,
                              "gnosis_heldout_usd": gnosis_spent, "repeat_attempts_usd": repeat_spent},
            "curve": counts}


async def retest_arms(plan, version, evaluator, meter):
    """同一批冻结候选上的全局重测与按影响范围重测。

    每组取最后一次成熟版本比较的两个整组版本，接受与否都纳入：候选本身已冻结，
    两种重测机制的判断一致性与执行数量就在这对版本上测得。
    """
    from engine.research_retest import retest_comparison
    repeats = plan["selection_repeats"]
    results = {}
    for arm in ("global", "gnosis"):
        workspace = EvolutionWorkspace(OUT / arm, version)
        comparisons = workspace.state["comparisons"]
        if not comparisons:
            results[arm] = {"status": "no_mature_comparison"}
            continue
        comparison = comparisons[-1]
        before_version, after_version = comparison["before_version"], comparison["after_version"]
        records = json.loads((workspace.root / "selection" / (before_version + ".json")).read_text(encoding="utf-8"))

        async def execute(task, repeat, label, workspace=workspace, arm=arm, after_version=after_version):
            return await evaluator.execute(workspace, after_version, task, arm, "evaluation", "selection", repeat,
                                           execution_id=fingerprint([arm, "retest", label, after_version, task, repeat]),
                                           kind="retest")

        results[arm] = {"decision_in_learning": comparison["decision"], "before_version": before_version,
                        "after_version": after_version,
                        **await retest_comparison(workspace.load_version(before_version),
                                                  workspace.load_version(after_version),
                                                  records, version, plan["selection"], repeats, execute)}
    save_json(OUT / "retest.json", results)
    return results


async def run_baseline(plan, evaluator, workspace):
    """B1：修正执行条件后的固定 Agent 在 30 道开发题上的基线，按断点复用。"""
    baseline = list(await asyncio.gather(*(evaluator.execute(workspace, workspace.state["working"], task, "fixed",
                                        execution_id=fingerprint(["baseline", task])) for task in plan["search"])))
    gate = baseline_gate(baseline)
    if not workspace.state["frozen"]:
        workspace.freeze()
    return baseline, gate


async def baseline():
    require_model_access()
    load_dotenv(ROOT / ".env")
    plan = prepare_plan()
    version = experiment_version(OUT)
    audit_output(OUT, version)
    meter = research_meter(OUT)
    from engine.research_evaluator import ResearchEvaluator
    _, gate = await run_baseline(plan, ResearchEvaluator(plan, meter), EvolutionWorkspace(OUT / "fixed", version))
    print(json.dumps({**gate, "ledger": meter.state()}, ensure_ascii=False, indent=2))


def baseline_gate(baseline):
    """修正执行条件的门槛：截断仍占失败的一半以上时，学习不开跑。"""
    failures = [r for r in baseline if not r["success"]]
    truncated = [r for r in failures if r.get("termination_status") == "truncated"
                 or (r.get("execution_error") or {}).get("category") == "execution_truncated"]
    gate = {"tasks": len(baseline), "strict_pass_rate": sum(r["success"] for r in baseline) / len(baseline),
            "failures": len(failures), "truncated_failures": len(truncated),
            "passed": 2 * len(truncated) < max(1, len(failures)),
            "cost_usd": sum(r["cost_usd"] for r in baseline)}
    save_json(OUT / "baseline_gate.json", gate)
    if not gate["passed"]:
        raise ExecutionBlocked("修正基线后截断仍是主要失败类型，先修执行条件再开学习")
    return gate


async def research_once():
    require_model_access()
    load_dotenv(ROOT / ".env")
    plan = prepare_plan()
    version = experiment_version(OUT)
    audit_output(OUT, version)
    meter = research_meter(OUT)
    from engine.research_evaluator import ResearchEvaluator
    evaluator = ResearchEvaluator(plan, meter)
    model = ResearchModel(meter, version)
    workspaces = {arm: EvolutionWorkspace(OUT / arm, version) for arm in ARMS}
    # 学习阶段：组额度耗尽在各组内部结束学习；接入层与服务故障上抛，重新运行按断点续跑。
    baseline, gate = await run_baseline(plan, evaluator, workspaces["fixed"])
    # 三个学习组互相独立：工作区、组额度与断点各自分开，同时运行，共用 8 路任务名额。
    await asyncio.gather(train_ace(plan, workspaces["ace"], evaluator, model),
                         *(RepairController(plan, workspaces[arm], evaluator, model, arm, baseline).run()
                           for arm in ("global", "gnosis")))
    stopping, retest = None, None
    results = {}
    try:
        for arm in ARMS[:-1]:
            workspace = workspaces[arm]
            rows = list(await asyncio.gather(*(evaluator.execute(workspace, workspace.state["frozen"], task, arm, "evaluation", "heldout",
                                             execution_id=fingerprint([arm, "heldout", task])) for task in plan["heldout"])))
            results[arm] = rows
            save_json(OUT / "heldout" / (arm + ".json"), rows)
        retest = await retest_arms(plan, version, evaluator, meter)
    except BudgetExceeded as error:
        if isinstance(error, ServiceWindowExceeded):
            raise
        stopping = {"stage": "heldout", "error_type": type(error).__name__, "error": str(error)}
    # 等美元组只看到自身前次过程，选择最后一次，评分不反馈给受测模型。
    # 每道留出题的额度 = GNOSIS 全部费用（学习、验证、中途检查、留出）摊到每题；评估池剩余不足时以剩余为准。
    repeat_workspace = workspaces["repeat"]
    if not repeat_workspace.state["frozen"]:
        repeat_workspace.freeze()
    gnosis_cost = arm_costs(phase_costs(meter), "gnosis")
    gnosis_total = gnosis_cost["total_usd"] - gnosis_cost["retest_usd"]
    allowance = gnosis_total / len(plan["heldout"])
    async def repeat_task(task):
        spent, revision, rows = 0.0, None, []
        while not rows or allowance - spent >= POLICY["min_attempt_usd"]:
            record = await evaluator.execute(repeat_workspace, repeat_workspace.state["frozen"], task,
                    "repeat", "evaluation", "heldout", repeat=len(rows), revision=revision,
                    execution_id=fingerprint(["repeat", task, len(rows)]),
                    task_cost_cap=max(allowance - spent, POLICY["min_attempt_usd"]))
            rows.append(record)
            spent += record["cost_usd"]
            if record["cost_usd"] <= 0 or (record.get("execution_error") or {}).get("category") == "task_cost_cap":
                break
            revision = {"completion": record["completion"], "actual_tool_actions": record["actual_tool_actions"]}
        return rows, spent

    repeat_rows, repeat_attempts = [], []
    if stopping is None:
        for rows, spent in await asyncio.gather(*(repeat_task(task) for task in plan["heldout"])):
            repeat_attempts.extend(rows)
            repeat_rows.append({**rows[-1], "attempts": len(rows), "total_cost_usd": spent, "allowance_usd": allowance})
    results["repeat"] = repeat_rows
    save_json(OUT / "heldout/repeat.json", repeat_rows)
    costs = phase_costs(meter)
    summary = {"experiment_version": version, "plan_identity": plan["identity"], "baseline": gate,
               "arms": {}, "budget": meter.state(), "stopping": stopping,
               "retest": retest,
               "baseline_note": "固定 Agent 基线覆盖 30 道开发题；各组通过率在 30 道最终留出题上计算"}
    for arm, rows in results.items():
        if not rows:
            continue
        cost = sum(r.get("total_cost_usd", r["cost_usd"]) for r in rows)
        arm_cost = arm_costs(costs, arm)
        development = arm_cost["development_usd"] if arm in ARM_LEARNING_LIMITS else 0.0
        comparisons = workspaces[arm].state.get("comparisons", [])
        summary["arms"][arm] = {"strict_pass_rate": sum(r["success"] for r in rows) / len(rows),
                                "partial_credit": sum(r["partial_credit"] for r in rows) / len(rows),
                                "evaluation_tasks": len(rows), "evaluation_cost_usd": cost,
                                "learning_and_selection_cost_usd": arm_cost["learning_and_selection_usd"],
                                "checkpoint_cost_usd": arm_cost["checkpoint_usd"],
                                "retest_cost_usd": arm_cost["retest_usd"],
                                "development_cost_usd": development,
                                "amortized_cost_per_task_usd": (cost + development) / len(rows),
                                "termination_counts": termination_counts(rows),
                                "effective_evolutions": sum(c["decision"] == "accepted" for c in comparisons),
                                "comparisons": len(comparisons),
                                "validation_executed": sum(c.get("executed_count", 0) for c in comparisons),
                                "validation_reused": sum(c.get("reused_count", 0) for c in comparisons),
                                "stop": workspaces[arm].state.get("stop"),
                                "frozen_units": len(workspaces[arm].load_version(workspaces[arm].state["frozen"])),
                                "checkpoint": workspaces[arm].state.get("checkpoint")}
    if results.get("gnosis") and repeat_rows:
        summary["amortized_comparison"] = amortized_comparison(plan, results["gnosis"], repeat_rows, gnosis_total)
    summary["repeat_attempt_terminations"] = termination_counts(repeat_attempts)
    save_json(OUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def research():
    """服务窗口用满时等窗口释放后整体续跑；全部执行与修复操作按断点复用，不重复计费。"""
    while True:
        try:
            return await research_once()
        except ServiceWindowExceeded as error:
            delay = max(60.0, research_meter(OUT).window_release_seconds() + 1.0)
            print(f"[research] {error}；等待 {delay:.0f} 秒后续跑", flush=True)
            await asyncio.sleep(delay)


async def retest():
    """单独执行固定候选重测比较；research 已包含这一步，结果按断点复用。"""
    require_model_access()
    load_dotenv(ROOT / ".env")
    plan = prepare_plan()
    version = experiment_version(OUT)
    audit_output(OUT, version)
    meter = research_meter(OUT)
    from engine.research_evaluator import ResearchEvaluator
    results = await retest_arms(plan, version, ResearchEvaluator(plan, meter), meter)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="GNOSIS 连续修复与激活边界研究入口")
    parser.add_argument("action", choices=("prepare", "baseline", "run", "offline", "unittests", "retest"))
    parser.add_argument("--replay", action="store_true", help="离线验证同时重放全部 30 道先导轨迹")
    args = parser.parse_args()
    if args.action == "prepare":
        plan = prepare_plan()
        print(json.dumps({"search_tasks": len(plan["search"]), "selection_tasks": len(plan["selection"]),
                          "heldout_tasks": len(plan["heldout"]), "selection_repeats": plan["selection_repeats"],
                          "arms": plan["arms"], "output_tokens": POLICY["execution_output_tokens"]}))
    elif args.action == "unittests":
        from engine.research_offline import unit_only
        asyncio.run(unit_only())
    elif args.action == "offline":
        from engine.research_offline import run
        asyncio.run(run(full_replay=args.replay))
    elif args.action == "retest":
        asyncio.run(retest())
    elif args.action == "baseline":
        asyncio.run(baseline())
    else:
        asyncio.run(research())


if __name__ == "__main__":
    main()
