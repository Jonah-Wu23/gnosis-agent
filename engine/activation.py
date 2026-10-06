from copy import deepcopy

from engine.scoped_units import fingerprint

HOOKS = ("task_start", "tool_discovery", "before_call", "after_call", "context", "workflow", "operation")
TRACE_SCHEMA = "gnosis.boundaries.v1"


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
        result.append({"task": record["task"], "affected": affected,
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
