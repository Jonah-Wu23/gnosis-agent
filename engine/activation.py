from copy import deepcopy

from engine.scoped_units import fingerprint
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

HOOKS = ("task_start", "tool_discovery", "before_call", "after_call", "context", "workflow", "operation")
TRACE_SCHEMA = "gnosis.boundaries.v1"


class TaskBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hook: Literal["before_call", "after_call", "tool_discovery"]
    tool: str | None = None
    service: str | None = None
    endpoint: str | None = None
    arguments: dict = Field(default_factory=dict)
    occurrence: int = Field(default=1, ge=1)

    def locate(self, trace):
        matches = [event for event in trace if event["hook"] == self.hook
                   and all(value is None or event.get(key) == value
                           for key, value in (("tool", self.tool), ("service", self.service),
                                              ("endpoint", self.endpoint)))
                   and all(event.get("arguments") is not None and event["arguments"].get(key) == value
                           for key, value in self.arguments.items())]
        return matches[self.occurrence - 1] if len(matches) >= self.occurrence else None


class KnowledgeTreatment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["gnosis.timing.v1"] = "gnosis.timing.v1"
    source: str
    treatment: Literal["G", "L", "D", "C", "N"]
    task: str
    selected: bool
    route_version: str
    contract: str
    remainder: str
    delay: TaskBoundary

    def text_at(self, trace):
        if self.treatment == "N" or (self.treatment != "G" and not self.selected):
            return "", []
        early = self.treatment in ("G", "L")
        delayed = self.delay.locate(trace) is not None
        parts = []
        if self.contract and (early or self.treatment == "C" or delayed):
            parts.append(("contract", self.contract))
        if self.remainder and (early or delayed):
            parts.append(("remainder", self.remainder))
        return "".join(value for _, value in parts), [key for key, _ in parts]


def provide_knowledge(prompt, state):
    treatment = KnowledgeTreatment.model_validate(state["knowledge_treatment"])
    text, parts = treatment.text_at(state["boundary_trace"])
    return ([{"role": "system", "content": text}, *prompt] if text else prompt), parts


def knowledge_validation_tasks(before, after, routes, contract_consumers, route_consumers):
    # 未入选任务独立运行且正文无其他读取路径；撤掉该假设，局部正文更新必须扩大重测集合。
    affected = set()
    selected = {task for task, enabled in routes.items() if enabled}
    if before["route_version"] != after["route_version"]:
        affected.update(route_consumers)
    if before["contract"] != after["contract"]:
        affected.update(contract_consumers)
        affected.update(selected)
    if before["remainder"] != after["remainder"]:
        affected.update(routes if "G" in (before.get("treatment"), after.get("treatment")) else selected)
    if before.get("treatment") != after.get("treatment"):
        affected.update(routes if "G" in (before.get("treatment"), after.get("treatment")) else selected)
    return sorted(affected)


def response_termination(response):
    return "truncated" if response.choices and response.choices[0].finish_reason == "length" else "completed"


def validate_scope(scope):
    if not isinstance(scope, list) or not scope or any(not isinstance(s, str) or not s for s in scope):
        raise ValueError("作用域必须使用非空字符串列表")
    if len(scope) != len(set(scope)):
        raise ValueError("作用域禁止重复")
    if "GLOBAL" in scope and scope != ["GLOBAL"]:
        raise ValueError("GLOBAL 与任何局部作用域互斥")


def unit_hooks(unit):
    if "hooks" in unit:
        return unit["hooks"]
    return ["task_start", "after_call"]


def activates(unit, event):
    validate_scope(unit["scope"])
    if event["hook"] not in unit_hooks(unit):
        return False
    scope = unit["scope"]
    return "GLOBAL" in scope or any(value in scope for value in (
        event.get("service"), event.get("endpoint"), event.get("tool")))


def components_version(units):
    return fingerprint([{k: v for k, v in unit.items() if k not in ("status", "evidence")}
                        for unit in units])


def boundary(state, hook, service=None, endpoint=None, tool=None, arguments=None, raw=None):
    if hook not in HOOKS:
        raise ValueError("未登记的扩展位置")
    trace = state.setdefault("boundary_trace", [])
    event = {"index": len(trace), "hook": hook, "service": service, "endpoint": endpoint,
             "tool": tool, "step": len(state.get("trajectory", [])),
             "arguments": deepcopy(arguments), "raw": raw}
    trace.append(event)
    state["boundary_schema"] = TRACE_SCHEMA
    return event


def changed_units(before, after):
    old = {u["id"]: u for u in before}
    new = {u["id"]: u for u in after}
    changes = [u for key, u in old.items() if key not in new or u != new[key]] + [
        u for key, u in new.items() if key not in old or u != old[key]]
    # 同一组组件的相对顺序也是执行依赖，交换顺序必须重新验证。
    if [u["id"] for u in before if u["id"] in new] != [u["id"] for u in after if u["id"] in old]:
        changes.extend(u for u in before if u["id"] in new)
    return changes


def validation_plan(before, after, records, experiment_version, global_validation=False):
    changes = changed_units(before, after)
    global_scope = any(u["scope"] == ["GLOBAL"] for u in changes)
    result = []
    for record in records:
        if record["experiment_version"] != experiment_version or record["version"] != components_version(before):
            raise ValueError("历史证据与待比较的程序或组件版本不一致")
        trace = record.get("boundary_trace", [])
        reusable = record.get("boundary_schema") == TRACE_SCHEMA
        first = next((event for event in trace if any(activates(u, event) for u in changes)), None)
        affected = bool(changes) and (global_validation or global_scope or not reusable or first is not None)
        # 当前 AutomationBench 接入层没有完整恢复模型与环境前缀的接口，受影响执行从头运行。
        result.append({"task": record["task"], "repeat": record.get("repeat", 0), "affected": affected,
                       "first_boundary": first["index"] if first else None,
                       "restart": "task_start" if affected else None,
                       "reason": "global_validation" if global_validation else "global_scope" if global_scope else
                       "missing_boundary_trace" if not reusable else "activation" if first else "unchanged_prefix",
                       "evidence_execution": record["execution_id"]})
    return result


def reuse_evidence(record, after, plan):
    if plan["affected"] or plan["evidence_execution"] != record["execution_id"]:
        raise ValueError("受影响或不对应的证据禁止复用")
    return {**deepcopy(record), "version": components_version(after),
            "reused_from": record["execution_id"], "validation_cost_usd": 0.0}
