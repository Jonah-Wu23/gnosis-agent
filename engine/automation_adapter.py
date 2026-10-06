import json
import asyncio
import argparse
import time
import os
from contextvars import ContextVar
from pathlib import Path

from automationbench.runner import AutomationBenchEnv
from automationbench.clients import _record_model_call
from automationbench.tools.api.fetch import _router_service, _url_to_internal_path
from verifiers.clients import OpenAIChatCompletionsClient
from verifiers.types import ToolCall
from verifiers.errors import EmptyModelResponseError, InvalidModelResponseError, ToolError
from verifiers.utils.message_utils import normalize_messages
from openai import APIConnectionError, APIStatusError

from engine.component_host import CandidateError, HostError
from engine.component_sandbox import IsolatedComponentHost
from engine.scoped_units import fingerprint, POLICY, CallJournal, experiment_version, append_record, AIFormatError, TaskCostExceeded, require_model_access, require_harness_ready, ExecutionBlocked

TOOL_RECEIPTS = ContextVar("gnosis_tool_receipts")


def matches(scope, service, endpoint):
    # 作用域匹配决定哪些单元会触发，属于模型输入的一部分；放在本文件以便进版本指纹
    return "GLOBAL" in scope or service in scope or endpoint in scope


def restore_messages(messages):
    # 原装导出器把每个 ToolCall 序列化为 JSON 字符串；用原装 schema 恢复。
    restored = []
    for message in messages:
        if isinstance(message, dict) and message.get("tool_calls"):
            message = {**message, "tool_calls": [
                ToolCall.model_validate_json(tc) if isinstance(tc, str) else tc
                for tc in message["tool_calls"]]}
        restored.append(message)
    return normalize_messages(restored)


class MeteredClient(OpenAIChatCompletionsClient):
    def __init__(self, config, budget, phase):
        super().__init__(config)
        self.budget = budget
        self.phase = phase
        self.client.max_retries = 0
        self.journal = CallJournal(budget.output, experiment_version(budget.output))

    async def to_native_prompt(self, messages):
        native, extra = await super().to_native_prompt(messages)
        for message in native:
            if message.get("reasoning_content") is None:
                message.pop("reasoning_content", None)
        return native, extra

    async def get_response(self, prompt, model, sampling_args, tools=None, **kwargs):
        state = kwargs["state"]
        for attempt in range(POLICY["format_retries"] + 1):
            state["gnosis_format_attempt"] = attempt
            try:
                return await super().get_response(prompt, model, sampling_args, tools=tools, **kwargs)
            except (EmptyModelResponseError, InvalidModelResponseError) as error:
                retry = attempt < POLICY["format_retries"]
                self.journal.failure(self.journal.output / "io" / state["gnosis_last_call"],
                                     "ai_format_error", error, format_attempt=attempt, retry_scheduled=retry)
                if not retry:
                    raise

    async def get_native_response(self, prompt, model, sampling_args, tools=None, **kwargs):
        require_model_access()
        started = time.monotonic()
        state = kwargs["state"]
        initial_call = None
        for attempt in range(POLICY["service_retries"] + 1):
            self.journal.verify_version()
            call_id = self.budget.reserve(self.phase, {"messages": prompt, "tools": tools},
                                          sampling_args["max_tokens"])
            folder = self.journal.request(call_id, self.phase, prompt,
                                          {"sampling_args": sampling_args, "tools": tools,
                                           "service_attempt": attempt, "retry_of": initial_call,
                                           "format_attempt": state["gnosis_format_attempt"]},
                                          task=state.get("task"))
            initial_call = initial_call or call_id
            try:
                response = await super().get_native_response(prompt, model, sampling_args, tools=tools, **kwargs)
            except (APIConnectionError, APIStatusError) as error:
                delay = self.journal.retry_service(folder, error, attempt)
                # 失败尝试都没有取得可计费的响应体，账本一并释放预留
                self.budget.release(call_id, "服务失败未取得计费响应，按未计费释放")
                if delay is None:
                    raise
                await asyncio.sleep(delay)
                continue
            break
        self.journal.response(folder, response)
        cost = self.budget.settle(call_id, response.usage)
        state["gnosis_last_call"] = call_id
        state.setdefault("wire_trace", []).append({"request": prompt, "response": response.choices[0].message.model_dump(mode="json") if response.choices else None,
                                                   "call": call_id})
        if state.get("gnosis_call_kind", "agent") == "agent":
            for event in state.get("injection_events", []):
                event.setdefault("next_agent_call", call_id)
        _record_model_call(state, time.monotonic() - started, response)
        state["gnosis_cost_usd"] = state.get("gnosis_cost_usd", 0.0) + cost
        if state["gnosis_cost_usd"] > POLICY["task_cost_cap_usd"]:
            raise TaskCostExceeded(f"单题费用超过上限 ${POLICY['task_cost_cap_usd']}：已用 ${state['gnosis_cost_usd']:.4f}")
        return response


class ScopedInjector:
    def __init__(self, library, host_config, candidate=None):
        self.library = library
        self.candidate = candidate
        self.units = library.units(candidate)
        self.version = fingerprint([{k: v for k, v in u.items() if k not in ("status", "evidence")}
                                    for u in self.units])
        self.host_config = host_config["host"]
        self.hosts = {}
        self.output = library.root.parent

    def close(self):
        for host in self.hosts.values():
            host.close()

    def refresh(self):
        self.units = self.library.units(self.candidate)
        self.version = fingerprint([{k: v for k, v in u.items() if k not in ("status", "evidence")}
                                    for u in self.units])
        obsolete = set(self.hosts) - {u["id"] for u in self.units}
        for unit_id in obsolete:
            self.hosts.pop(unit_id).close()

    def begin_task(self, messages, state):
        global_units = [u for u in self.units if "GLOBAL" in u["scope"]
                        and u["id"] not in state.setdefault("injected_units", set())]
        if not global_units:
            return messages
        messages = [{"role": "system", "content": "\n".join(u["content"] for u in global_units)}, *messages]
        for unit in global_units:
            state["injected_units"].add(unit["id"])
            state.setdefault("injection_events", []).append({
                "unit": unit["id"], "component": unit["component"], "kind": "knowledge_injection",
                "endpoint": "GLOBAL", "step": 0})
            if unit["component"]:
                state.setdefault("active_components", {})[unit["id"]] = "GLOBAL"
        return messages

    def denial_note(self, state, error):
        append_record(self.output / "permission_denials.jsonl", {
            "task": state.get("task"), "kind": "capability_denied",
            "unit": getattr(error, "candidate_id", None), "component": getattr(error, "component", None),
            "error": str(error), "action": "拒绝后继续作答"})
        return ("[GNOSIS 隔离] 组件请求的能力被拒绝：" + str(error)
                + "。继续用当前可用的数据完成任务，不要再请求该能力。")

    def host_for(self, unit):
        if unit["id"] not in self.hosts:
            root = Path(__file__).resolve().parent.parent
            host = IsolatedComponentHost(root / self.host_config["host_dir"], self.host_config["node_bin_dir"],
                                         self.output / "component_calls.jsonl")
            try:
                host.load_unit(unit)
            except Exception:
                host.close()
                raise
            self.hosts[unit["id"]] = host
        return self.hosts[unit["id"]]

    def apply(self, raw, service, endpoint, state, step=0, call=None):
        fired = state.setdefault("injected_units", set())
        evidence = state.setdefault("injection_events", [])
        active = [u for u in self.units if matches(u["scope"], service, endpoint)]
        if not active:
            return raw
        text = raw
        for unit in active:
            if unit["component"]:
                unit_id = unit["id"]
                if unit["component"] == "T":
                    result = self.host_for(unit).t_format_observation(unit_id, text, step, json.dumps(call or {}))
                    if not isinstance(result.get("text"), str):
                        raise CandidateError(unit_id, "T", "formatObservation 必须返回 text 字符串")
                    text = result["text"]
                else:
                    state.setdefault("active_components", {})[unit_id] = endpoint
                    result = None
                if result is not None:
                    evidence.append({"unit": unit_id, "component": unit["component"],
                                     "endpoint": endpoint, "loaded": True, "trace": result.get("trace")})
            if unit["id"] not in fired:
                text += "\n\n[Scoped knowledge " + unit["id"] + "]\n" + unit["content"]
                evidence.append({"unit": unit["id"], "component": unit["component"],
                                 "kind": "knowledge_injection", "endpoint": endpoint,
                                 "step": len(state.get("trajectory", [])), "tool_call": call})
            fired.add(unit["id"])
        return text

    def context_inputs(self, messages, state, step):
        return {"task": {"id": state["task"], "instruction": state["task_instruction"]},
                "messages": messages, "step": step,
                "budget": {"maxSteps": POLICY["max_steps"], "remainingSteps": POLICY["max_steps"] - step,
                           "remainingCostUsd": None, "maxOutputTokensPerTask": POLICY["execution_output_tokens"]}}

    def process_context(self, messages, state, step, start=0):
        text = messages
        for index, unit in enumerate(self.units):
            if index < start:
                continue
            if unit["id"] not in state.get("active_components", {}) or unit["component"] != "C":
                continue
            inputs = self.context_inputs(text, state, step)
            inputs["state"] = state.setdefault("component_state", {}).get(unit["id"])
            result = self.host_for(unit).c_build_context(unit["id"], inputs)
            if "lmRequest" in result:
                request = result["lmRequest"]
                if not isinstance(request, dict) or not isinstance(request.get("prompt"), str):
                    raise CandidateError(unit["id"], "C", "lmRequest 必须包含 prompt 字符串")
                max_tokens = request.get("maxTokens", POLICY["execution_output_tokens"])
                if type(max_tokens) is not int or not 1 <= max_tokens <= POLICY["execution_output_tokens"]:
                    raise CandidateError(unit["id"], "C", "lmRequest 输出额度违反固定上限")
                return {"unit": unit, "inputs": inputs, "request": {**request, "maxTokens": max_tokens},
                        "messages": text, "next_component": index + 1}
            text = self.accept_context(unit, result, state, inputs)
        return {"messages": text}

    def accept_context(self, unit, result, state, inputs):
        try:
            messages = restore_messages(result["messages"])
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateError(unit["id"], "C", f"buildContext 返回消息格式错误：{error}") from error
        if not messages:
            raise CandidateError(unit["id"], "C", "buildContext 返回空消息列表")
        state.setdefault("component_state", {})[unit["id"]] = result.get("state")
        state.setdefault("injection_events", []).append({
            "unit": unit["id"], "component": "C", "loaded": True,
            "endpoint": state["active_components"][unit["id"]], "in_messages": len(inputs["messages"]),
            "out_messages": len(messages), "trace": result.get("trace")})
        return [m.model_dump(mode="json") for m in messages]

    def workflow(self, messages, state, step):
        for unit in self.units:
            if unit["id"] not in state.get("active_components", {}) or unit["component"] != "W":
                continue
            last = messages[-1]
            result = self.host_for(unit).w_before_step(unit["id"], {
                "step": step, "taskInstruction": state["task_instruction"], "lastCode": state.get("last_tool_call", ""),
                "lastObservation": last.get("content", ""), "lastError": state.get("last_tool_error", ""),
                "usage": {"costUsd": state.get("gnosis_cost_usd", 0), "outputTokens": state.get("output_tokens", 0)},
                "historyLength": len(messages)})
            if result.get("action") == "inject" and isinstance(result.get("message"), str):
                messages = [*messages, {"role": "user", "content": result["message"]}]
            elif result.get("action") == "stop" and isinstance(result.get("reason"), str):
                state["final_env_response"] = restore_messages([{"role": "assistant", "content": result["reason"]}])
            elif result.get("action") != "continue":
                raise CandidateError(unit["id"], "W", "beforeStep 返回动作格式错误")
            state.setdefault("injection_events", []).append({"unit": unit["id"], "component": "W", "loaded": True,
                                                            "endpoint": state["active_components"][unit["id"]], "action": result["action"]})
            if result["action"] == "stop":
                break
        return messages


class ScopedAutomationEnv(AutomationBenchEnv):
    def __init__(self, *args, injector=None, **kwargs):
        self.injector = injector
        super().__init__(*args, **kwargs)

    def execution_guard(self):
        output = self.injector.output if self.injector else Path(__file__).resolve().parent.parent / "experiments/pilot"
        require_harness_ready(output)

    def _should_stop_for_error(self, error):
        return isinstance(error, ExecutionBlocked) or super()._should_stop_for_error(error)

    @staticmethod
    def _denied(error):
        return bool(getattr(error, "permission_denied", False))

    def denial_note(self, state, error):
        """越界请求被拒绝后把拒绝信息交回受测 AI，作答继续到测试结束。"""
        return self.injector.denial_note(state, error)

    async def setup_state(self, state, **kwargs):
        self.execution_guard()
        state = await super().setup_state(state, **kwargs)
        state["scope_touches"] = []
        state["injected_units"] = set()
        state["injection_events"] = []
        state["gnosis_cost_usd"] = 0.0
        state["wire_trace"] = []
        state["task"] = state["info"]["task_name"]
        state["task_instruction"] = "\n".join(m.content for m in restore_messages(state["prompt"]) if m.role == "user")
        if self.injector:
            encoded = [m.model_dump(mode="json") for m in restore_messages(state["prompt"])]
            state["prompt"] = restore_messages(self.injector.begin_task(encoded, state))
        return state

    async def get_prompt_messages(self, state):
        self.execution_guard()
        messages = await super().get_prompt_messages(state)
        if not self.injector:
            return messages
        encoded = [m.model_dump(mode="json") for m in restore_messages(messages)]
        step = len(state["trajectory"])
        try:
            encoded = self.injector.workflow(encoded, state, step)
            if state.get("final_env_response") is not None:
                return restore_messages(encoded)
            processed = self.injector.process_context(encoded, state, step)
        except (CandidateError, HostError) as error:
            if not self._denied(error):
                raise ToolError(str(error)) from error
            return restore_messages([*encoded, {"role": "user", "content": self.denial_note(state, error)}])
        while "request" in processed:
            request = processed["request"]
            encoded = processed["messages"]
            max_tokens = request["maxTokens"]
            sampling = {**state["sampling_args"], "max_tokens": max_tokens}
            state["gnosis_call_kind"] = "context_component"
            try:
                response = await super().get_model_response(state, restore_messages([{"role": "user", "content": request["prompt"]}]),
                                                             tool_defs=[], sampling_args=sampling)
            finally:
                state["gnosis_call_kind"] = "agent"
            inputs = {**processed["inputs"], "lmResult": response.message.content, "state": request.get("state")}
            try:
                result = self.injector.host_for(processed["unit"]).c_build_context(processed["unit"]["id"], inputs)
                if "lmRequest" in result:
                    raise CandidateError(processed["unit"]["id"], "C", "每步最多一次上下文模型请求")
                encoded = self.injector.accept_context(processed["unit"], result, state, inputs)
                processed = self.injector.process_context(encoded, state, step, processed["next_component"])
            except (CandidateError, HostError) as error:
                if not self._denied(error):
                    raise ToolError(str(error)) from error
                return restore_messages([*encoded,
                                         {"role": "user", "content": self.denial_note(state, error)}])
        return restore_messages(processed["messages"])

    async def call_tool(self, tool_name, tool_args, tool_call_id, **kwargs):
        self.execution_guard()
        result = await super().call_tool(tool_name, tool_args, tool_call_id, **kwargs)
        if tool_name == "api_fetch":
            TOOL_RECEIPTS.get()[tool_call_id] = {k: v for k, v in tool_args.items() if k != "world"}
        return result

    async def env_response(self, messages, state, **kwargs):
        self.execution_guard()
        receipts = {}
        token = TOOL_RECEIPTS.set(receipts)
        try:
            result = await super().env_response(messages, state, **kwargs)
        finally:
            TOOL_RECEIPTS.reset(token)
        for message in result:
            if message.tool_call_id not in receipts:
                continue
            args = receipts[message.tool_call_id]
            path, router = _url_to_internal_path(args["url"])
            payload = json.loads(message.content)
            state["last_tool_call"] = json.dumps(args)
            if router is None or _router_service(router) not in state["world"].meta.allowed_services:
                append_record(Path(__file__).resolve().parent.parent / "experiments/pilot/permission_denials.jsonl",
                              {"task": state["task"], "kind": "unauthorized_tool", "call": args,
                               "result": message.content})
            if isinstance(payload, dict) and "error" in payload:
                state["last_tool_error"] = json.dumps(payload["error"])
            if router is None or isinstance(payload, dict) and "error" in payload:
                continue
            service = _router_service(router)
            endpoint = service + ":" + args["method"].upper() + " " + path
            state["scope_touches"].append({"service": service, "endpoint": endpoint})
            if self.injector:
                try:
                    message.content = self.injector.apply(message.content, service, endpoint, state,
                                                          len(state["trajectory"]), args)
                except (CandidateError, HostError) as error:
                    if not self._denied(error):
                        raise ToolError(str(error)) from error
                    message.content = message.content + "\n\n" + self.denial_note(state, error)
        return result


async def serve_task(domain, task_name):
    from automationbench.domains import get_domain_dataset
    from automationbench.rubric import create_rubric
    from automationbench.runner import compute_allowed_services, strip_none_values
    from automationbench.schema.world import WorldState
    from automationbench.tools.api import API_TOOLS
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    dataset = get_domain_dataset(domain)
    row = next(row for row in dataset if json.loads(row["info"])["task_name"] == task_name)
    info = strip_none_values(json.loads(row["info"]))
    world = WorldState(**info["initial_state"])
    world.meta.allowed_services = compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"])
    env = AutomationBenchEnv(dataset=dataset, rubric=create_rubric(), toolset="api")
    functions = {f.__name__: f for f in API_TOOLS}
    server = Server("automationbench")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
                for t in env._all_tool_defs]

    @server.call_tool()
    async def call_tool(name, arguments):
        function = functions[name]
        args = env.update_tool_args(name, arguments, [], {"world": world})
        result = function(**args)
        failed = name == "api_fetch" and "error" in json.loads(result)
        return types.CallToolResult(content=[types.TextContent(type="text", text=result)], isError=failed)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    asyncio.run(serve_task(args.domain, args.task))
