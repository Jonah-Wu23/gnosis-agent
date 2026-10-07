import importlib.util
import json
import time
from pathlib import Path
from typing import Literal

from openai import APIConnectionError, APIStatusError
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from engine.llm_client import chat, client
from engine.automation_adapter import matches, restore_messages
from engine.scoped_units import AIFormatError, POLICY, CallJournal, experiment_version, fingerprint, ProposalCheckpoint, ProposalInterrupted, save_json, require_model_access


class UnitProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: list[str] = Field(min_length=1)
    content: str = Field(min_length=1)
    source_task: str = Field(min_length=1)
    prediction: str = Field(min_length=1)
    component: Literal["T", "C", "W"] | None
    source: None
    replaces: list[str] = Field(default_factory=list)

    @field_validator("scope")
    @classmethod
    def scope_contract(cls, value):
        from engine.activation import validate_scope
        validate_scope(value)
        return value


class ProposalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: UnitProposal


def load_prompt(path, name):
    spec = importlib.util.spec_from_file_location("gnosis_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def editable(unit):
    return {k: unit[k] for k in UnitProposal.model_fields}


def learning_view(record):
    if record.get("learning_role") != "learning":
        raise PermissionError("执行记录未获得学习授权")
    version = experiment_version(Path(__file__).resolve().parent.parent / "state")
    if record.get("experiment_version") != version:
        raise PermissionError("其他版本的执行记录禁止进入学习入口")
    messages = []
    indices = {}

    def message_id(message):
        key = json.dumps(message, sort_keys=True, ensure_ascii=False)
        if key not in indices:
            indices[key] = len(messages)
            messages.append(message)
        return indices[key]

    # 消息内容只保存一次，prompt、completion 与每次调用通过编号引用。
    prompt = [message_id(m) for m in record["prompt"]]
    completion = [message_id(m) for m in record["completion"]]
    wire_trace = [{**step, "request": [message_id(m) for m in step["request"]],
                   "response": message_id(step["response"]) if step["response"] is not None else None}
                  for step in record.get("wire_trace", [])]
    return {"task": record["task"], "domain": record["domain"],
            "messages": messages, "prompt": prompt, "completion": completion,
            "reward": {"partial_credit": record["partial_credit"], "success": record["success"]},
            "touches": record["touches"], "injections": record["injections"],
            "wire_trace": wire_trace,
            "execution_error": record.get("execution_error"), "record_version": record.get("experiment_version", "legacy")}


def propose(record, library, budget, ace_root, previous_unit=None, failure=None, learning_records=(), scope_records=(), repair_root=None, components=None, checkpoint=None):
    if components is None:
        components = POLICY["default_components"]
    if any(r.get("learning_role") in ("heldout", "checkpoint") for r in scope_records):
        raise PermissionError("留出与中途检查执行记录禁止用于改进使用范围")
    client.max_retries = 0
    output = budget.output
    version = experiment_version(output)
    journal = CallJournal(output, version)
    calls = []
    root = Path(ace_root) / "ace/prompts"
    reflector = load_prompt(root / "reflector.py", "REFLECTOR_PROMPT_NO_GT")
    curator = load_prompt(root / "curator.py", "CURATOR_PROMPT_NO_GT")
    playbook = json.dumps([editable(u) for u in library.units()], ensure_ascii=False)
    messages = restore_messages(record["prompt"])
    completion = restore_messages(record["completion"])
    question = "\n".join(m.content for m in messages if m.role == "user")
    feedback = {"current_execution": learning_view(record),
                "allowed_learning_executions": [learning_view(r) for r in learning_records],
                "previous_unit": editable(previous_unit) if previous_unit else None,
                "actual_failure": failure,
                "scope_matches_on_saved_learning_traces": [{"task": r["task"], "domain": r["domain"],
                    "matching_touches": [t for t in r["touches"] if previous_unit is None or matches(previous_unit["scope"], t["service"], t["endpoint"])]}
                    for r in scope_records]}
    trace = json.dumps(feedback, ensure_ascii=False)
    prediction = completion[-1].content if completion and completion[-1].role == "assistant" else ""
    checkpoint_root = Path(checkpoint) if checkpoint else output / "proposal_checkpoints" / version / fingerprint({
        "feedback": feedback, "playbook": playbook, "components": components, "repair_root": repair_root})

    def invoke(prompt, phase, structured=True, validate=None):
        request = [{"role": "user", "content": prompt}]
        options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 0, "seed": POLICY["seed"]}
        if structured:
            options["response_format"] = {"type": "json_object"}
        saved = ProposalCheckpoint(checkpoint_root / (phase + ".json"), version, request, options)
        calls.extend(saved.state["calls"])
        if saved.state["status"] == "complete":
            return saved.state["result"]
        if saved.state["status"] == "failed":
            if saved.state["category"] == "ai_format_error":
                raise AIFormatError(saved.state["error"])
            raise ProposalInterrupted(saved.state["category"], saved.state["call"])
        while saved.state["format_attempt"] <= POLICY["format_retries"]:
            format_attempt = saved.state["format_attempt"]
            attempt = saved.state["service_attempt"]
            request = saved.state["request"]
            journal.verify_version()
            if saved.state["status"] == "inflight":
                call_id = saved.state["call"]
                folder = output / "io" / call_id
                response_path = folder / "response.json"
                if response_path.exists():
                    response = ChatCompletion.model_validate_json(response_path.read_text(encoding="utf-8"))
                elif (folder / "failure.json").exists():
                    failure_record = json.loads((folder / "failure.json").read_text(encoding="utf-8"))
                    if failure_record["category"] == "service_connection_failure":
                        budget.release(call_id, "恢复已记录的服务失败，按未计费释放")
                    if not failure_record["retry_scheduled"]:
                        saved.save(status="failed", category=failure_record["category"])
                        raise ProposalInterrupted(failure_record["category"], call_id)
                    saved.save(status="ready", service_attempt=attempt + 1, call=None,
                               retry_at=(folder / "failure.json").stat().st_mtime + failure_record["retry_delay_seconds"])
                    continue
                else:
                    raise ProposalInterrupted("pending_response", call_id)
            else:
                require_model_access()
                time.sleep(max(0, saved.state.get("retry_at", 0) - time.time()))
                call_id = budget.reserve("proposal", request, options["max_tokens"])
                retry_of = saved.state["retry_of"]
                saved.start(call_id)
                calls.append(call_id)
                folder = journal.request(call_id, phase, request,
                                         {**options, "reasoning_effort": "high", "service_attempt": attempt,
                                          "format_attempt": format_attempt, "retry_of": retry_of},
                                         task=record["task"], unit=previous_unit["id"] if previous_unit else None)
                try:
                    response = chat(request, **options)
                except (APIConnectionError, APIStatusError) as error:
                    delay = journal.retry_service(folder, error, attempt)
                    budget.release(call_id, "服务失败未取得计费响应，按未计费释放")
                    if delay is None:
                        saved.save(status="failed", category="service_connection_failure")
                        raise
                    saved.save(status="ready", service_attempt=attempt + 1, call=None,
                               retry_at=time.time() + delay)
                    continue
                journal.response(folder, response)
            budget.settle(call_id, response.usage)
            message = response.choices[0].message.content if len(response.choices) == 1 else None
            try:
                if not message or response.choices[0].finish_reason != "stop":
                    raise AIFormatError("AI 未交出完整输出，终止原因与完整响应已保存")
                value = json.loads(message) if structured else message
                if structured and not isinstance(value, dict):
                    raise AIFormatError("已要求返回 JSON 对象")
                result = validate(value) if validate else value
                saved.save(status="complete", result=result)
                return result
            except (json.JSONDecodeError, ValidationError, AIFormatError) as error:
                failed = error if isinstance(error, AIFormatError) else AIFormatError(str(error))
                retry = format_attempt < POLICY["format_retries"]
                if not (folder / "failure.json").exists():
                    journal.failure(folder, "ai_format_error", failed, format_attempt=format_attempt,
                                    retry_scheduled=retry)
                if not retry:
                    saved.save(status="failed", category="ai_format_error", error=str(failed))
                    if failed is error:
                        raise
                    raise failed from error
                request.append({"role": "user", "content": "The previous output failed the declared format. "
                                "Produce a new output satisfying the original schema and output requirements.\n"
                                + json.dumps({"format_error": str(failed), "previous_output": message})})
                saved.save(status="ready", request=request, format_attempt=format_attempt + 1,
                           service_attempt=0, call=None, retry_at=0)

    reflection = invoke(reflector.format(question, trace, prediction,
                                          json.dumps(feedback["current_execution"]["reward"]), playbook), "ace_reflection")
    curation = invoke(curator.format(token_budget=120, current_step=1, total_samples=1,
                                    playbook_stats=f"{len(library.units())} accepted units",
                                    recent_reflection=json.dumps(reflection), current_playbook=playbook,
                                    question_context=question), "ace_curation")
    observed = {t["service"] for r in (record, *learning_records) for t in r["touches"]}
    observed |= {t["endpoint"] for r in (record, *learning_records) for t in r["touches"]}
    scope_values = sorted(observed | {"GLOBAL"})
    component_rule = (
        "You may revise every editable field: knowledge content, scope, expected effect, component type and implementation. "
        "Choose component=null for knowledge only, T for a tool observation wrapper, C for full conversation context, "
        "or W for a scoped workflow directive. No component type is selected by the caller. "
        if components else
        "You may revise every editable field: knowledge content, scope and expected effect. "
        "This stage writes knowledge text only: set component to null and source to null. "
    )
    cost_rule = (
        "Cost you should weigh: a context component may issue lmRequest sub-requests, each costing about $0.0059 "
        "(a 20 KB prompt plus up to 32768 output tokens), and a task is stopped once it spends $0.15, so a component "
        "that asks the model on every step kills its own task. A task also runs at most "
        + str(POLICY["max_steps"]) + " steps. "
    )
    replaces_rule = (
        "replaces lists the ids of existing units this one supersedes; when this unit is admitted they are retired. "
        "Use it to narrow or merge earlier knowledge instead of adding a second unit. Existing units: "
        + json.dumps([u["id"] for u in library.units()]) + " "
    )
    prompt = (
        "Choose the improvement yourself from the original ACE reflection and curation below. "
        + component_rule + cost_rule + replaces_rule
        + "Return exactly this JSON Schema; source is null because source code is requested separately:\n"
        + json.dumps(ProposalEnvelope.model_json_schema())
        + "\nsource_task must identify the current learning execution: " + record["task"]
        + "\nscope values must come from these observed services/endpoints or GLOBAL: " + json.dumps(scope_values)
        + "\nKeep the knowledge explanation at most 120 tokens. "
        "Decide the scope from actual applicability; shared services can occur in other business tasks. "
        "Do not include evaluation assertions or task answers.\n"
        + json.dumps({"reflection": reflection, "curation": curation, "feedback": feedback}, ensure_ascii=False)
    )
    def validate_design(proposal):
        unit = ProposalEnvelope.model_validate(proposal).unit.model_dump()
        if unit["source_task"] != record["task"] or not set(unit["scope"]) <= set(scope_values):
            raise AIFormatError("来源或使用范围违反已明确的输出格式")
        if not components and unit["component"] is not None:
            raise AIFormatError("本阶段只接受纯文字单元，组件类型必须为 null")
        if any(name not in {u["id"] for u in library.records()} for name in unit["replaces"]):
            raise AIFormatError("replaces 指向不存在的单元")
        return unit

    unit = invoke(prompt, "unit_design", validate=validate_design)
    if unit["component"]:
        interface = {
            "T": "export function formatObservation(input) -> {text:string,trace?:object}; input has raw, step and code.",
            "C": "export function buildContext(input) -> {messages:Message[],trace?:object,state?:any}, or {lmRequest:{prompt:string,maxTokens?:number,purpose:string,state?:any}}. Input has the real task instruction, the complete authorized conversation including tool messages/calls, step, budget and optional state/lmResult. Messages use the original role/content/tool_calls/tool_call_id fields. You may organize and compress the entire conversation.",
            "W": "export function beforeStep(input) -> {action:'continue'} or {action:'inject',message:string} or {action:'stop',reason:string}. Input has the real task instruction, step, lastCode, lastObservation, lastError, usage and historyLength.",
        }[unit["component"]]
        source_prompt = (
            "Write the component implementation yourself. Return only a complete TypeScript module as plain source text, "
            "without JSON encoding or Markdown fences. " + interface
            + "\nThe component runs in QuickJS with no filesystem, processes, external modules or network. "
            "Only the authorized input data and its own state are available. Keep errors visible. "
            "A denied capability is returned to the agent as an observation and the task keeps running, so handle it. "
            "Each lmRequest sub-request costs about $0.0059 and the task is stopped once it spends $0.15. "
            "The program appends knowledge once per activation; decide how the component uses it.\n"
            + json.dumps({"design": unit, "reflection": reflection, "actual_previous_failure": failure,
                          "previous_source": previous_unit["source"] if previous_unit else None}, ensure_ascii=False)
        )
        unit["source"] = invoke(source_prompt, "component_implementation", structured=False)
    provenance = {"experiment_version": version, "calls": calls,
                  "parent": previous_unit["id"] if previous_unit else None,
                  "repair_root": repair_root, "proposal_checkpoint": str(checkpoint_root.relative_to(output)),
                  "learning_tasks": [record["task"], *[r["task"] for r in learning_records]]}
    unit_id = fingerprint(unit)[:20]
    if (library.root / unit_id / "unit.json").exists():
        existing = library.read(unit_id)
        if existing.get("provenance") != provenance:
            raise AIFormatError("AI 返回了已存在的同一单元，没有产生新版本")
        result = existing
    else:
        result = library.propose(unit, provenance)
    save_json(checkpoint_root / "unit.json", {"unit": result["id"], "experiment_version": version})
    return result
