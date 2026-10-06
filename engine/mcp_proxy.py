import argparse
import asyncio
import json
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from engine.automation_adapter import ScopedInjector
from engine.component_host import CandidateError, HostError
from engine.scoped_units import UnitLibrary, append_record, POLICY, experiment_version, require_harness_ready, harness_blocker

CONTEXT_TOOL = "gnosis_build_context"


def build_proxy(downstream, service, injector, ledger, automationbench=False):
    server = Server("gnosis")
    state = {"task": uuid.uuid4().hex}

    def context_result(value, error=False):
        append_record(ledger, {"session": state["task"], "tool": CONTEXT_TOOL,
                              "success": not error, "kind": value["status"],
                              "version": injector.version, "injections": state.get("injection_events", [])})
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                                    isError=error)

    def finish_context(processed, step):
        if "request" in processed:
            continuation = uuid.uuid4().hex
            state["pending_context"] = {**processed, "continuation": continuation, "step": step,
                                        "version": injector.version}
            return context_result({"status": "model_request", "task": state["task"], "step": step,
                                   "continuation": continuation, "lmRequest": processed["request"]})
        return context_result({"status": "completed", "task": state["task"], "messages": processed["messages"]})

    @server.list_tools()
    async def list_tools():
        tools = await downstream.list_tools()
        return [*tools.tools, types.Tool(name=CONTEXT_TOOL,
                description="Process the complete current authorized conversation through activated GNOSIS context/workflow plugins. Supply the real task instruction and the original message fields, including tool calls and tool results.",
                inputSchema={"type": "object", "required": ["task", "step"],
                "properties": {"task": {"type": "object", "required": ["id", "instruction"],
                    "properties": {"id": {"type": "string"}, "instruction": {"type": "string"}}},
                    "messages": {"type": "array", "items": {"type": "object"}},
                    "continuation": {"type": "string"}, "lmResult": {"type": "string"},
                    "step": {"type": "integer", "minimum": 0, "maximum": POLICY["max_steps"]}},
                "oneOf": [{"required": ["messages"], "not": {"required": ["continuation"]}},
                          {"required": ["continuation", "lmResult"], "not": {"required": ["messages"]}}]})]

    @server.call_tool()
    async def call_tool(name, arguments):
        require_harness_ready(injector.output)
        injector.refresh()
        if name == CONTEXT_TOOL:
            from engine.automation_adapter import restore_messages
            task = arguments["task"]["id"]
            if task != state["task"]:
                injector.close()
                injector.hosts.clear()
                state.clear()
                state["task"] = task
            state["task_instruction"] = arguments["task"]["instruction"]
            step = arguments["step"]
            pending = state.get("pending_context")
            if state.get("final_env_response"):
                return context_result({"status": "stopped", "task": task,
                                       "messages": [m.model_dump(mode="json") for m in state["final_env_response"]]})
            if "continuation" in arguments:
                if pending is None or pending["continuation"] != arguments["continuation"] or pending["step"] != step:
                    return context_result({"status": "invalid_continuation", "task": task}, error=True)
                if pending["version"] != injector.version:
                    state.pop("pending_context")
                    return context_result({"status": "units_changed_during_model_request", "task": task}, error=True)
                if pending["inputs"]["task"] != arguments["task"]:
                    return context_result({"status": "task_changed_during_model_request", "task": task}, error=True)
                messages = pending["messages"]
            else:
                if pending is not None:
                    return context_result({"status": "model_result_required", "task": task}, error=True)
                messages = [m.model_dump(mode="json") for m in restore_messages(arguments["messages"])]
            try:
                if pending is not None:
                    inputs = {**pending["inputs"], "lmResult": arguments["lmResult"],
                              "state": pending["request"].get("state")}
                    result = injector.host_for(pending["unit"]).c_build_context(pending["unit"]["id"], inputs)
                    if "lmRequest" in result:
                        raise CandidateError(pending["unit"]["id"], "C", "每步最多一次上下文模型请求")
                    messages = injector.accept_context(pending["unit"], result, state, inputs)
                    state.pop("pending_context")
                    processed = injector.process_context(messages, state, step, pending["next_component"])
                else:
                    messages = injector.begin_task(messages, state)
                    messages = injector.workflow(messages, state, step)
                    if state.get("final_env_response"):
                        return context_result({"status": "stopped", "task": task,
                                               "messages": [m.model_dump(mode="json") for m in state["final_env_response"]]})
                    processed = injector.process_context(messages, state, step)
            except (CandidateError, HostError) as error:
                state.pop("pending_context", None)
                if getattr(error, "permission_denied", False):
                    note = injector.denial_note(state, error)
                    return context_result({"status": "capability_denied", "task": task,
                                           "messages": [*messages, {"role": "user", "content": note}]}, error=True)
                if isinstance(error, HostError):
                    harness_blocker(injector.output, error, "mcp_context", task)
                return context_result({"status": "harness_or_integration_error" if isinstance(error, HostError)
                                       else "ai_code_or_format_error", "task": task,
                                       "error_type": type(error).__name__, "error": str(error)}, error=True)
            return finish_context(processed, step)
        if state.get("pending_context") or state.get("final_env_response"):
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type="text", text="当前任务已停止" if state.get("final_env_response") else "先提交待处理上下文请求的 lmResult")])
        call_id = uuid.uuid4().hex
        for event in state.get("injection_events", []):
            event.setdefault("next_tool_call", call_id)
        result = await downstream.call_tool(name, arguments)
        if result.isError:
            append_record(ledger, {"session": state["task"], "tool": name, "success": False})
            return result
        # MCP 作用域由显式服务名称和工具 ID 构成，不推断用户意图。
        call_service = service
        endpoint = service + ":" + name
        if automationbench and name == "api_fetch":
            from automationbench.tools.api.fetch import _router_service, _url_to_internal_path
            path, router = _url_to_internal_path(arguments["url"])
            if router is None:
                raise ValueError("成功工具调用没有原装路由")
            call_service = _router_service(router)
            endpoint = call_service + ":" + arguments["method"].upper() + " " + path
        original = result.model_dump(mode="json")
        raw = json.dumps(original, ensure_ascii=False)
        event_count = len(state.get("injection_events", []))
        try:
            rendered = injector.apply(raw, call_service, endpoint, state, call=arguments)
        except (CandidateError, HostError) as error:
            if getattr(error, "permission_denied", False):
                note = injector.denial_note(state, error)
                append_record(ledger, {"session": state["task"], "tool": name,
                                      "success": False, "kind": "capability_denied"})
                return result.model_copy(update={"content": [*result.content, types.TextContent(type="text", text=note)],
                                                 "isError": True})
            if isinstance(error, HostError):
                harness_blocker(injector.output, error, "mcp_observation", state["task"])
            raise
        if rendered != raw:
            if any(e["component"] == "T" and e.get("kind") != "knowledge_injection"
                   for e in state["injection_events"][event_count:]):
                result = result.model_copy(update={"content": [
                    *[item for item in result.content if item.type != "text"],
                    types.TextContent(type="text", text=rendered)]})
            else:
                extra = rendered[len(raw):]
                result = result.model_copy(update={"content": [*result.content, types.TextContent(type="text", text=extra)]})
        append_record(ledger, {"session": state["task"], "call": call_id, "tool": name, "arguments": arguments,
                               "service": call_service,
                               "endpoint": endpoint, "success": True, "version": injector.version,
                               "injections": state.get("injection_events", [])})
        return result

    @server.list_resources()
    async def list_resources():
        return [types.Resource(uri="gnosis://capabilities", name="GNOSIS capability ledger",
                               mimeType="application/json")]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != "gnosis://capabilities":
            raise ValueError("未知能力账本资源")
        injector.refresh()
        summary_path = Path(ledger).parent / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
        return json.dumps({"version": injector.version, "pilot": summary,
                           "active_units": [u["id"] for u in injector.units],
                           "experiment_version": experiment_version(Path(ledger).parent),
                           "units": [{"id": u["id"], "scope": u["scope"],
                                      "status": u["status"], "prediction": u["prediction"],
                                      "evidence": u.get("evidence")}
                                     for u in injector.library.records()]}, ensure_ascii=False)

    return server


async def serve(args):
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "config/runtime.json").read_text(encoding="utf-8"))
    injector = ScopedInjector(UnitLibrary(args.units), cfg, args.candidate)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("需要下游 MCP 服务启动命令")
    params = StdioServerParameters(command=command[0], args=command[1:], cwd=str(root))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                server = build_proxy(session, args.service, injector, args.ledger, args.automationbench)
                async with stdio_server() as (incoming, outgoing):
                    await server.run(incoming, outgoing, server.create_initialization_options())
    finally:
        injector.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--units", default="experiments/pilot/units")
    parser.add_argument("--ledger", default="experiments/pilot/mcp_calls.jsonl")
    parser.add_argument("--automationbench", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
