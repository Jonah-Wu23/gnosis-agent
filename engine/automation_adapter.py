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
from verifiers.types import ToolCall, Tool
from verifiers.errors import EmptyModelResponseError, InvalidModelResponseError, ToolError
from verifiers.utils.message_utils import normalize_messages
from openai import APIConnectionError, APIStatusError

from engine.component_host import CandidateError, HostError
from engine.component_sandbox import IsolatedComponentHost
from engine.scoped_units import fingerprint, POLICY, CallJournal, experiment_version, append_record, AIFormatError, TaskCostExceeded, require_model_access, require_harness_ready, ExecutionBlocked, ExecutionTruncated
from engine.activation import boundary, activates, unit_hooks, response_termination
from engine.extension_protocol import validate_extension
from jsonschema import Draft202012Validator
from pydantic import ValidationError

EXECUTION_STATE = ContextVar("gnosis_execution_state")


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
        self.task_cost_cap = POLICY["task_cost_cap_usd"]
        self.journal = CallJournal(budget.output, experiment_version(budget.output))

    def contingency_reason(self, attempt):
        if attempt:
            return "service_failure_retry"
        return "task_cost_fluctuation" if getattr(self.budget, "cost_fluctuation", False) else None

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
            def task_guard(db, amount, reason):
                # 额度按已结算费用判定：输出上限 393216 的最坏预留会让额度内的任务提前停下。
                if self.task_cost_cap is not None and state.get("gnosis_cost_usd", 0.0) >= self.task_cost_cap:
                    raise TaskCostExceeded(f"单题额度已用完：${state.get('gnosis_cost_usd', 0.0):.4f}")
            call_id = self.budget.reserve(self.phase, {"messages": prompt, "tools": tools},
                                          sampling_args["max_tokens"], guard=task_guard,
                                          contingency_reason=self.contingency_reason(attempt))
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
        choice = response.choices[0] if response.choices else None
        state.setdefault("wire_trace", []).append({"request": prompt, "response": choice.message.model_dump(mode="json") if choice else None,
                                                   "finish_reason": choice.finish_reason if choice else None, "call": call_id})
        if state.get("gnosis_call_kind", "agent") == "agent":
            for event in state.get("injection_events", []):
                event.setdefault("next_agent_call", call_id)
        _record_model_call(state, time.monotonic() - started, response)
        state["gnosis_cost_usd"] = state.get("gnosis_cost_usd", 0.0) + cost
        state["termination_status"] = response_termination(response)
        if choice and choice.finish_reason == "length":
            self.journal.failure(folder, "execution_truncated", ExecutionTruncated("推理输出达到单次上限"))
            raise ExecutionTruncated("推理输出达到单次上限")
        if self.task_cost_cap is not None and state["gnosis_cost_usd"] > self.task_cost_cap:
            raise TaskCostExceeded(f"单题费用超过上限 ${self.task_cost_cap}：已用 ${state['gnosis_cost_usd']:.4f}")
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
        self.output = getattr(library, "output", library.root.parent)

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
        event = boundary(state, "task_start")
        global_units = [u for u in self.units if "GLOBAL" in u["scope"]
                        and u["component"] != "E"
                        and "task_start" in unit_hooks(u)
                        and u["id"] not in state.setdefault("injected_units", set())]
        if not global_units:
            return messages
        messages = [{"role": "system", "content": "\n".join(u["content"] for u in global_units)}, *messages]
        for unit in global_units:
            state["injected_units"].add(unit["id"])
            state.setdefault("injection_events", []).append({
                "unit": unit["id"], "component": unit["component"], "kind": "knowledge_injection",
                "endpoint": "GLOBAL", "step": 0, "boundary_index": event["index"]})
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

    def apply(self, raw, service, endpoint, state, step=0, call=None, tool=None):
        event = boundary(state, "after_call", service, endpoint, tool, arguments=call, raw=raw)
        fired = state.setdefault("injected_units", set())
        evidence = state.setdefault("injection_events", [])
        active = [u for u in self.units if activates(u, event) and u["component"] != "E"]
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
                                 "step": len(state.get("trajectory", [])), "tool_call": call,
                                 "boundary_index": event["index"]})
            fired.add(unit["id"])
        return text

    def context_inputs(self, messages, state, step):
        return {"task": {"id": state["task"], "instruction": state["task_instruction"]},
                "messages": messages, "step": step,
                "budget": {"maxSteps": POLICY["max_steps"], "remainingSteps": POLICY["max_steps"] - step,
                           "remainingCostUsd": None, "maxOutputTokensPerTask": POLICY["execution_output_tokens"]}}

    def process_context(self, messages, state, step, start=0):
        boundary(state, "context", state.get("current_service"), state.get("current_endpoint"))
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
        boundary(state, "workflow", state.get("current_service"), state.get("current_endpoint"))
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
    def __init__(self, *args, injector=None, execution_output=None, **kwargs):
        self.injector = injector
        self.execution_output = execution_output
        super().__init__(*args, **kwargs)

    def execution_guard(self):
        output = self.execution_output or (self.injector.output if self.injector else Path(__file__).resolve().parent.parent / "experiments/pilot")
        require_harness_ready(output)

    async def extension(self, unit, event, state, value):
        inputs = {"event": event, "input": value,
                  "task": {"id": state["task"], "instruction": state["task_instruction"]},
                  "budget": {"remainingCostUsd": None if POLICY["task_cost_cap_usd"] is None else
                             POLICY["task_cost_cap_usd"] - state.get("gnosis_cost_usd", 0),
                             "maxCalls": POLICY["max_steps"]},
                  "state": state.setdefault("component_state", {}).get(unit["id"])}
        while True:
            state["component_invocations"] = state.get("component_invocations", 0) + 1
            if state["component_invocations"] > POLICY["max_steps"] * POLICY["max_steps"]:
                raise CandidateError(unit["id"], "E", "组件执行超过任务操作额度")
            raw = self.injector.host_for(unit).e_on_boundary(unit["id"], inputs)
            try:
                result = validate_extension(raw)
            except (ValidationError, ValueError) as error:
                raise CandidateError(unit["id"], "E", str(error)) from error
            state["component_state"][unit["id"]] = result.state
            for tool in result.tools:
                name = "gnosis_" + unit["id"] + "_" + tool.name
                registered = state.setdefault("operation_tools", {})
                definition = Tool(name=name, description=tool.description, parameters=tool.parameters)
                if name in registered and registered[name]["definition"] != definition:
                    raise CandidateError(unit["id"], "E", "已登记操作函数的定义发生变化")
                if name not in registered:
                    registered[name] = {"unit": unit, "operation": tool.name, "definition": definition}
                    state["tool_defs"] = [*state["tool_defs"], definition]
            effects = []
            if result.tools:
                effects.append("tool_catalog")
            if result.calls:
                effects.append("capability_calls")
            if result.arguments is not None and result.arguments != value.get("arguments"):
                effects.append("call_arguments")
            if result.text is not None and result.text != value.get("text"):
                effects.append("observation")
            if result.messages is not None and result.messages != value.get("messages"):
                effects.append("context")
            if effects:
                state.setdefault("first_effect_boundaries", {}).setdefault(unit["id"], event["index"])
            state.setdefault("injection_events", []).append({
                "unit": unit["id"], "component": "E", "boundary_index": event["index"],
                "hook": event["hook"], "input": value, "output": raw,
                "actual_calls": [call.model_dump() for call in result.calls], "effects": effects})
            if not result.calls:
                return result
            receipts = []
            for call in result.calls:
                state["component_capability_calls"] = state.get("component_capability_calls", 0) + 1
                if state["component_capability_calls"] > POLICY["max_steps"]:
                    raise CandidateError(unit["id"], "E", "组件调用超过任务操作额度")
                if call.tool not in ("api_fetch", "api_search"):
                    raise CandidateError(unit["id"], "E", "组件仅可调用当前任务的原装 API 能力")
                args = self.update_tool_args(call.tool, call.arguments, [], state)
                response = await self.execute_capability(call.tool, args,
                                                         "component_" + str(state["component_invocations"]), state, unit)
                receipts.append({"tool": call.tool, "arguments": call.arguments, "raw": response.content})
            inputs = {**inputs, "state": result.state, "results": receipts}

    async def extensions(self, event, state, value, exclude=None):
        if not self.injector:
            return value
        for unit in self.injector.units:
            if unit["component"] != "E" or not activates(unit, event) or unit["id"] == exclude:
                continue
            active = state.setdefault("active_extensions", {})
            if unit["id"] not in active:
                active[unit["id"]] = event["index"]
            try:
                result = await self.extension(unit, event, state, value)
            except (CandidateError, HostError) as error:
                if not self._denied(error):
                    raise
                value = {**value, "text": (value.get("text") or "") + "\n" + self.denial_note(state, error)}
                continue
            if result.arguments is not None:
                if event["hook"] != "before_call":
                    raise CandidateError(unit["id"], "E", "arguments 只允许在调用前返回")
                value = {**value, "arguments": result.arguments}
            if result.text is not None:
                if event["hook"] in ("context", "workflow"):
                    value = {**value, "messages": [*value["messages"], {"role": "user", "content": result.text}]}
                else:
                    value = {**value, "text": result.text}
            if result.messages is not None:
                if event["hook"] not in ("context", "workflow"):
                    raise CandidateError(unit["id"], "E", "messages 只允许在上下文扩展位置返回")
                value = {**value, "messages": [m.model_dump(mode="json") for m in restore_messages(result.messages)]}
        return value

    @staticmethod
    def call_location(tool_name, arguments):
        """定位调用位置：任何参数形态都必须给出结果，未匹配路由时保留真实 URL。"""
        if tool_name != "api_fetch":
            return None, tool_name
        url = str(arguments.get("url") or "")
        method = str(arguments.get("method") or "").upper()
        path, router = _url_to_internal_path(url)
        service = _router_service(router) if router else None
        return service, (service or "unknown") + ":" + method + " " + (path or url)

    @staticmethod
    def parse_arguments(value):
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def rejected_location(self, call, state):
        """被拒观测按真实参数字典解析位置，解析不出再退化成工具名。"""
        parsed = self.parse_arguments(call.arguments)
        if parsed is None:
            return None, call.name
        return self.location(call.name, parsed, state)

    def location_discoveries(self, payload, state):
        try:
            return self.discovered_endpoints(payload)
        except Exception as error:
            state.setdefault("harness_defects", []).append(
                {"tool": "api_search", "error_type": type(error).__name__, "error": str(error)})
            return []

    def location(self, tool_name, arguments, state):
        """接入层自己的定位计算：出错就是基础设施故障，不冒充环境结果。"""
        try:
            return self.call_location(tool_name, arguments)
        except Exception as error:
            state.setdefault("harness_defects", []).append(
                {"tool": tool_name, "error_type": type(error).__name__, "error": str(error)})
            raise HostError(f"接入层调用位置计算失败（{tool_name}）：{type(error).__name__}: {error}") from error

    @staticmethod
    def discovered_endpoints(payload):
        """按搜索实际返回的接口元数据登记发现位置，不猜测查询意图。"""
        entries = []
        for entry in payload.get("results", []):
            url = str(entry.get("url") or "")
            method = str(entry.get("method") or "").upper()
            path, router = _url_to_internal_path(url)
            service = _router_service(router) if router else None
            entries.append((service, (service or "unknown") + ":" + method + " " + (path or url),
                            json.dumps(entry)))
        return entries

    async def execute_capability(self, tool_name, tool_args, tool_call_id, state, unit):
        """组件发起的原装能力调用：与受测 AI 直接调用走同一批扩展位置。

        发起组件本身不接收自己这批调用的扩展事件，避免它在等待结果时重入自己的协议循环。
        """
        public_args = {k: v for k, v in tool_args.items() if k != "world"}
        service, endpoint = self.location(tool_name, public_args, state)
        event = boundary(state, "before_call", service, endpoint, tool_name, public_args)
        prepared = await self.extensions(event, state, {"arguments": public_args}, exclude=unit["id"])
        args = self.update_tool_args(tool_name, prepared["arguments"], [], state)
        try:
            result = await super().call_tool(tool_name, args, tool_call_id)
        except Exception as error:
            raised = json.dumps({"raised": type(error).__name__, "message": str(error)}, ensure_ascii=False)
            state["termination_status"] = "tool_execution_failed"
            state.setdefault("actual_tool_actions", []).append({"tool": tool_name, "arguments": public_args,
                                                               "raw": raised, "failed": True,
                                                               "tool_call_id": tool_call_id, "component_call": True})
            boundary(state, "after_call", service, endpoint, tool_name, public_args, raised)
            if service:
                state["scope_touches"].append({"service": service, "endpoint": endpoint})
            raise
        payload = json.loads(result.content) if tool_name in ("api_fetch", "api_search") else None
        failed = isinstance(payload, dict) and "error" in payload
        if failed:
            state["termination_status"] = "tool_execution_failed"
        state.setdefault("actual_tool_actions", []).append({"tool": tool_name, "arguments": public_args,
                                                           "raw": result.content, "failed": failed,
                                                           "tool_call_id": tool_call_id, "component_call": True})
        event = boundary(state, "after_call", service, endpoint, tool_name, public_args, result.content)
        value = await self.extensions(event, state, {"text": result.content, "arguments": public_args},
                                      exclude=unit["id"])
        result.content = value["text"]
        if service:
            state["scope_touches"].append({"service": service, "endpoint": endpoint})
        if tool_name == "api_search" and isinstance(payload, dict):
            for discovered_service, discovered_endpoint, raw in self.location_discoveries(payload, state):
                event = boundary(state, "tool_discovery", discovered_service, discovered_endpoint, tool_name, raw=raw)
                value = await self.extensions(event, state, {"text": result.content}, exclude=unit["id"])
                result.content = value["text"]
        return result

    def _should_stop_for_error(self, error):
        return isinstance(error, (ExecutionBlocked, ExecutionTruncated, CandidateError, HostError)) or super()._should_stop_for_error(error)

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
        state["boundary_trace"] = []
        state["actual_tool_actions"] = []
        state["termination_status"] = "completed"
        state["task"] = state["info"]["task_name"]
        state["task_instruction"] = "\n".join(m.content for m in restore_messages(state["prompt"]) if m.role == "user")
        if self.injector:
            encoded = [m.model_dump(mode="json") for m in restore_messages(state["prompt"])]
            encoded = self.injector.begin_task(encoded, state)
            state["prompt"] = restore_messages(encoded)
            event = state["boundary_trace"][-1]
            value = await self.extensions(event, state, {"text": ""})
            if value.get("text"):
                state["prompt"] = restore_messages([{"role": "system", "content": value["text"]}, *encoded])
        else:
            boundary(state, "task_start")
        event = boundary(state, "tool_discovery", tool="catalog")
        value = await self.extensions(event, state, {"text": ""})
        if value.get("text"):
            state["prompt"] = restore_messages([{"role": "system", "content": value["text"]},
                                                *[m.model_dump(mode="json") for m in state["prompt"]]])
        return state

    async def get_prompt_messages(self, state):
        self.execution_guard()
        messages = await super().get_prompt_messages(state)
        if not self.injector:
            boundary(state, "workflow", state.get("current_service"), state.get("current_endpoint"))
            boundary(state, "context", state.get("current_service"), state.get("current_endpoint"))
            return messages
        encoded = [m.model_dump(mode="json") for m in restore_messages(messages)]
        step = len(state["trajectory"])
        try:
            encoded = self.injector.workflow(encoded, state, step)
            extended = await self.extensions(state["boundary_trace"][-1], state, {"messages": encoded})
            encoded = extended["messages"]
            if state.get("final_env_response") is not None:
                return restore_messages(encoded)
            processed = self.injector.process_context(encoded, state, step)
            if "request" not in processed:
                processed = await self.extensions(state["boundary_trace"][-1], state, processed)
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
        state = EXECUTION_STATE.get()
        if tool_name in state.get("operation_tools", {}):
            registered = state["operation_tools"][tool_name]
            Draft202012Validator(registered["definition"].parameters).validate(tool_args)
            event = boundary(state, "operation", state.get("current_service"), state.get("current_endpoint"), tool_name, tool_args)
            from verifiers.types import ToolMessage
            try:
                result = await self.extension(registered["unit"], event, state,
                                              {"operation": registered["operation"], "arguments": tool_args})
            except (CandidateError, HostError) as error:
                if not self._denied(error):
                    raise
                return ToolMessage(role="tool", content=self.denial_note(state, error), tool_call_id=tool_call_id)
            if result.text is not None or result.messages is not None or result.arguments is not None:
                raise CandidateError(registered["unit"]["id"], "E", "操作函数只返回 result")
            return ToolMessage(role="tool", content=json.dumps(result.result, ensure_ascii=False), tool_call_id=tool_call_id)
        public = {k: v for k, v in tool_args.items() if k != "world"}
        service, endpoint = self.location(tool_name, public, state)
        event = boundary(state, "before_call", service, endpoint, tool_name, public)
        value = await self.extensions(event, state, {"arguments": public})
        args = self.update_tool_args(tool_name, value["arguments"], [], state)
        actual = {k: v for k, v in args.items() if k != "world"}
        service, endpoint = self.location(tool_name, actual, state)
        state["last_tool_call"] = json.dumps(actual)
        state["current_service"], state["current_endpoint"] = service, endpoint
        if service:
            state["scope_touches"].append({"service": service, "endpoint": endpoint})
        try:
            result = await super().call_tool(tool_name, args, tool_call_id, **kwargs)
        except Exception as error:
            # 原装工具直接抛出异常时同样留下调用边界与真实动作，异常照原样上抛。
            raised = json.dumps({"raised": type(error).__name__, "message": str(error)}, ensure_ascii=False)
            state["last_tool_error"] = raised
            state["termination_status"] = "tool_execution_failed"
            state["actual_tool_actions"].append({"tool": tool_name, "arguments": actual, "raw": raised,
                                                 "failed": True, "tool_call_id": tool_call_id,
                                                 "boundary_index": event["index"]})
            boundary(state, "after_call", service, endpoint, tool_name, actual, raised)
            raise
        payload = json.loads(result.content) if tool_name in ("api_fetch", "api_search") else None
        failed = isinstance(payload, dict) and "error" in payload
        state["last_tool_error"] = json.dumps(payload["error"]) if failed else ""
        if failed:
            state["termination_status"] = "tool_execution_failed"
        state["actual_tool_actions"].append({"tool": tool_name, "arguments": actual, "raw": result.content,
                                             "failed": failed, "tool_call_id": tool_call_id,
                                             "boundary_index": event["index"]})
        raw = result.content
        if value.get("text"):
            raw += "\n\n" + value["text"]
        if self.injector:
            raw = self.injector.apply(raw, service, endpoint, state, len(state["trajectory"]), actual, tool_name)
            event = state["boundary_trace"][-1]
        else:
            event = boundary(state, "after_call", service, endpoint, tool_name, actual, raw)
        value = await self.extensions(event, state, {"text": raw, "arguments": actual})
        result.content = value["text"]
        for intervention in state.get("injection_events", []):
            intervention.setdefault("next_actual_tool_action", len(state["actual_tool_actions"]) - 1)
        if tool_name == "api_search":
            # 激活只根据实际返回的接口元数据，不猜测查询意图。
            event = boundary(state, "tool_discovery", tool=tool_name, raw=raw)
            value = await self.extensions(event, state, {"text": result.content})
            result.content = value["text"]
            for discovered_service, discovered_endpoint, entry in self.location_discoveries(payload, state):
                event = boundary(state, "tool_discovery", discovered_service, discovered_endpoint, tool_name, raw=entry)
                value = await self.extensions(event, state, {"text": result.content})
                result.content = value["text"]
        return result

    async def env_response(self, messages, state, **kwargs):
        self.execution_guard()
        token = EXECUTION_STATE.set(state)
        try:
            records = await super().env_response(messages, state, **kwargs)
        finally:
            EXECUTION_STATE.reset(token)
        return await self.record_unrouted(messages, records, state)

    async def record_unrouted(self, messages, records, state):
        """受测 AI 看到的每条工具观测都要经过 after_call。

        verifiers 在工具参数无法解析时直接补一条错误消息，不经过 call_tool；
        这里为这些观测补记边界与动作，作用域证据才不会漏掉模型实际看到的内容。
        """
        calls = {}
        for message in reversed(messages):
            for call in getattr(message, "tool_calls", None) or []:
                calls[call.id] = call
            if calls:
                break
        observed = {action.get("tool_call_id") for action in state.get("actual_tool_actions", [])}
        for record in records:
            call = calls.get(getattr(record, "tool_call_id", None))
            if call is None or call.id in observed:
                continue
            parsed = self.parse_arguments(call.arguments)
            arguments = parsed if parsed is not None else {"requested": call.arguments}
            service, endpoint = self.rejected_location(call, state)
            event = boundary(state, "after_call", service, endpoint, call.name, arguments, record.content)
            value = await self.extensions(event, state, {"text": record.content, "arguments": arguments})
            record.content = value["text"]
            state.setdefault("actual_tool_actions", []).append(
                {"tool": call.name, "arguments": arguments, "raw": value["text"], "failed": True,
                 "rejected": True, "tool_call_id": call.id, "boundary_index": event["index"]})
        return records


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
