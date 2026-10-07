import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from pydantic_core import to_jsonable_python
from verifiers.types import ClientConfig

from automationbench.domains import get_domain_dataset
from automationbench.rubric import create_rubric

from engine.activation import components_version
from engine.automation_adapter import MeteredClient, ScopedAutomationEnv, ScopedInjector, restore_messages
from engine.evolution import VersionLibrary
from engine.run_pilot import config, execution_failure
from engine.scoped_units import (POLICY, BudgetExceeded, ExecutionBlocked, ExecutionTruncated, require_model_access,
                                 fingerprint, save_json, experiment_version)

ROOT = Path(__file__).resolve().parent.parent
STATE_COLUMNS = ["scope_touches", "injection_events", "_assertion_results", "_perf", "error",
                 "gnosis_cost_usd", "wire_trace", "boundary_trace", "boundary_schema",
                 "termination_status", "actual_tool_actions", "active_extensions",
                 "first_effect_boundaries", "component_invocations", "component_capability_calls", "harness_defects"]


class ResearchEvaluator:
    def __init__(self, plan, meter):
        self.plan = plan
        self.meter = meter
        self.output = meter.output
        self.version = experiment_version(self.output)
        self.datasets = {}
        self.slots = None

    def task(self, name):
        domain = self.plan["tasks"][name]["domain"]
        if domain not in self.datasets:
            self.datasets[domain] = get_domain_dataset(domain)
        dataset = self.datasets[domain]
        index = next(i for i, row in enumerate(dataset) if json.loads(row["info"])["task_name"] == name)
        row = dataset[index]
        if fingerprint(row) != self.plan["tasks"][name]["contract"]:
            raise ValueError("原始任务或评分契约发生变化")
        return dataset.select([index]), row

    async def execute(self, workspace, version, task, arm, bucket="learning", role="learning", repeat=0,
                      execution_id=None, revision=None, task_cost_cap=None, kind="execution"):
        # 同时最多 parallel_tasks 道题在跑；各题环境、客户端与断点文件互相独立，账本用 SQLite 事务串行记账。
        if self.slots is None:
            self.slots = asyncio.Semaphore(POLICY["parallel_tasks"])
        async with self.slots:
            return await self.execute_one(workspace, version, task, arm, bucket, role, repeat,
                                          execution_id, revision, task_cost_cap, kind)

    async def execute_one(self, workspace, version, task, arm, bucket, role, repeat,
                          execution_id, revision, task_cost_cap, kind):
        role_map = {"learning": self.plan["search"], "selection": self.plan["selection"],
                    "checkpoint": self.plan["checkpoint"], "heldout": self.plan["heldout"]}
        if role not in role_map:
            raise PermissionError("未知的执行角色：" + str(role))
        if task not in role_map[role]:
            raise PermissionError("任务不属于该执行角色的固定集合")
        units = workspace.load_version(version)
        execution_id = execution_id or uuid.uuid4().hex
        path = self.output / "executions" / arm / (execution_id + ".json")
        resume_mode = os.environ.get("GNOSIS_RESUME_ONLY") == "1"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["task"], record["version"], record["experiment_version"], record["repeat"], record["learning_role"]) != (task, version, self.version, repeat, role):
                raise ValueError("执行断点输入发生变化")
            return record
        elif resume_mode:
            raise FileNotFoundError("断点续跑模式下未找到执行断点: " + str(path))
        require_model_access()
        load_dotenv(ROOT / ".env")
        dataset, row = self.task(task)
        injector = ScopedInjector(VersionLibrary(workspace, version), config())
        client = MeteredClient(ClientConfig(api_key_var="MODEL_API", api_base_url=os.environ["MODER_PORVIDER"]),
                               self.meter, bucket + "/" + arm + "/" + kind + "/" + execution_id)
        if task_cost_cap is not None:
            client.task_cost_cap = task_cost_cap
        started = time.monotonic()
        try:
            env = ScopedAutomationEnv(dataset=dataset, rubric=create_rubric(), max_turns=POLICY["max_steps"],
                                      toolset="api", injector=injector, execution_output=self.output)
            if revision:
                prompt = restore_messages(row["prompt"])
                extra = {"role": "user", "content": "Review your previous execution and complete the original task in the fresh environment.\n" + json.dumps(revision, ensure_ascii=False)}
                data = dataset.to_list()
                data[0]["prompt"] = [*[m.model_dump(mode="json") for m in prompt], extra]
                from datasets import Dataset
                env = ScopedAutomationEnv(dataset=Dataset.from_list(data), rubric=create_rubric(),
                                          max_turns=POLICY["max_steps"], toolset="api", injector=injector,
                                          execution_output=self.output)
            result = await env.evaluate(client=client, model=os.environ["MODER_NAME"],
                                        sampling_args={"reasoning_effort": "high", "temperature": 1,
                                                       "max_tokens": POLICY["execution_output_tokens"],
                                                       "extra_body": {"thinking": {"type": "enabled"},
                                                                      "max_tokens": POLICY["execution_output_tokens"]}},
                                        num_examples=1, rollouts_per_example=1, max_concurrent=1, state_columns=STATE_COLUMNS)
            raw = result["outputs"][0]
            error = raw.pop("error", None)
            execution_error = None
            if error:
                cause, category = execution_failure(error)
                execution_error = {"category": category, "error_type": type(cause).__name__, "error": str(cause)}
                if category in ("harness_or_integration_error", "execution_blocked", "budget_stop", "service_connection_failure"):
                    save_json(path.with_suffix(".failure.json"), execution_error)
                    if isinstance(cause, BudgetExceeded):
                        # 预算类终止保留原类型：组额度耗尽结束学习，服务窗口用满等待续跑，二者由控制层区分。
                        raise cause
                    # 终止原因统一按接入层口径上抛：控制层只需捕获同一组类型，真实原因保留在记录里。
                    raise ExecutionBlocked(f"执行终止（{category}）：{execution_error['error_type']}: {cause}") from cause
            raw = to_jsonable_python(raw)
            success = bool(raw["metrics"]["task_completed_correctly"]) and execution_error is None
            record = {"task": task, "domain": self.plan["tasks"][task]["domain"], "success": success,
                      "partial_credit": raw["metrics"]["partial_credit"], "assertions": raw["_assertion_results"],
                      "version": components_version(units), "experiment_version": self.version,
                      "task_contract": fingerprint(row), "learning_role": role, "repeat": repeat,
                      "execution_id": execution_id, "recorded_at": time.time(), "arm": arm,
                      "touches": raw["scope_touches"], "injections": raw["injection_events"],
                      "boundary_trace": raw["boundary_trace"], "boundary_schema": raw["boundary_schema"],
                      "actual_tool_actions": raw["actual_tool_actions"], "wire_trace": raw["wire_trace"],
                      "termination_status": raw["termination_status"], "execution_error": execution_error,
                      "active_extensions": raw.get("active_extensions", {}),
                      "first_effect_boundaries": raw.get("first_effect_boundaries", {}),
                      "harness_defects": raw.get("harness_defects", []),
                      "prompt": to_jsonable_python(restore_messages(raw["prompt"])),
                      "completion": to_jsonable_python(restore_messages(raw["completion"])),
                      "cost_usd": raw["gnosis_cost_usd"], "validation_cost_usd": raw["gnosis_cost_usd"],
                      "duration_s": time.monotonic() - started}
            save_json(path, record)
            return record
        finally:
            await client.close()
            injector.close()
