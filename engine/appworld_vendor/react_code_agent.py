import copy
import json
import os
import re
from collections.abc import Sequence
from typing import Any

from jinja2 import Template

from appworld import AppWorld
from engine.agent_runtime import Usage
from engine.appworld_vendor.agent import Agent, ExecutionIO, Status
from engine.component_host import CandidateError, ComponentHost

# 一轮 buildContext 内允许的 lmRequest 子请求上限（契约 §2）
MAX_LM_REQUESTS_PER_STEP = 2


@Agent.register("simplified_react_code_agent")
class SimplifiedReActCodeAgent(Agent):  # type: ignore[misc]
    """v2 单一路径：W/T/C 都是宿主里的 TS 模块，裁剪职责整体归 C 组件。"""

    def __init__(
        self,
        component_host: ComponentHost,
        candidate_id: str,
        skills_text: str = "",
        ignore_multiple_calls: bool = True,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.component_host = component_host
        self.candidate_id = candidate_id
        self.skills_text = skills_text or ""
        self.ignore_multiple_calls = ignore_multiple_calls
        self.partial_code_regex = r".*```python\n(.*)"
        self.full_code_regex = r"```python\n(.*?)```"
        self.num_instruction_messages = 0
        # 跨步状态，initialize 每任务重置
        self.last_code = ""
        self.last_observation = ""
        self.last_error = ""
        self.workflow_stopped = False
        self._component_log = None

    def initialize(self, world: AppWorld) -> None:
        super().initialize(world)
        self._open_component_log(world)
        self.last_code = ""
        self.last_observation = ""
        self.last_error = ""
        self.workflow_stopped = False
        self.component_host.set_step(0)
        template = self.component_host.w_template(self.candidate_id).lstrip()
        if self.skills_text:
            # v1 的逐字节等价拼接：模板 + "\n\n# Skills\n\n" + 技能段（段自带 "## name" 标题）
            template = template + "\n\n# Skills\n\n" + self.skills_text
        app_descriptions = json.dumps(
            [{"name": k, "description": v} for (k, v) in world.task.app_descriptions.items()],
            indent=1,
        )
        template_params = {
            "instruction": world.task.instruction,
            "main_user": world.task.supervisor,
            "app_descriptions": app_descriptions,
        }
        output_str = Template(template).render(template_params) + "\n\n"
        self.messages = self.text_to_messages(output_str)
        self.num_instruction_messages = len(self.messages)

    # ---------------- 组件调用证据（契约 §7） ----------------

    def _open_component_log(self, world: AppWorld) -> None:
        self.close_component_log()
        path = os.path.join(world.output_misc_directory, "component_calls.jsonl")
        self._component_log = open(path, "a", encoding="utf-8")
        self.component_host.set_log_sink(self._write_component_call)

    def _write_component_call(self, record: dict) -> None:
        self._component_log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._component_log.flush()

    def close_component_log(self) -> None:
        self.component_host.set_log_sink(None)
        if self._component_log is not None:
            self._component_log.close()
            self._component_log = None

    # ---------------- 每步流程 ----------------

    def next_execution_inputs_usage_and_status(
        self, last_execution_outputs: Sequence[ExecutionIO]
    ) -> tuple[Sequence[ExecutionIO], Usage, Status]:
        host = self.component_host
        host.set_step(self.step_number)
        raw_output = None
        if last_execution_outputs:
            assert (
                len(last_execution_outputs) == 1
            ), "React expects exactly one last_execution_output."
            raw_output = last_execution_outputs[0].content
            self.logger.show_message(
                role="environment", content=raw_output, step_number=self.step_number
            )
            # WorkflowState 的 lastObservation/lastError 指"上一次工具执行"，就是刚拿回来的
            # 这份输出，故先把状态更新到位再问工作流
            self.last_observation = raw_output
            self.last_error = raw_output if raw_output.startswith("Execution failed") else ""
        directive = host.w_before_step(self.candidate_id, self.workflow_state())
        action = directive["action"]
        if action == "stop":
            reason = directive.get("reason", "")
            self.workflow_stopped = True
            self.logger.show_message(role="termination", content=f"workflow stop: {reason}")
            return [], Usage(), Status(failed=True, message=f"workflow stop: {reason}")
        if raw_output is not None:
            observation = host.t_format_observation(
                self.candidate_id, raw=raw_output, step=self.step_number, code=self.last_code
            )
            self.messages.append({"role": "user", "content": observation["text"]})
        if action == "inject":
            self.messages.append({"role": "user", "content": directive["message"]})

        messages = self.build_context_messages()
        output = self.language_model.generate(messages=messages, cache_control_at=-1)
        error_message = output.pop("error", None)
        if error_message:
            return [], Usage(), Status(failed=True, message=error_message)
        standardized_usage = output.pop("standardized_usage")
        raw_message = copy.deepcopy(output)
        code, fixed_output_content = self.extract_code_and_fix_content(output["content"] or "")
        output["content"] = fixed_output_content + "\n\n"
        self.messages.append(
            output
        )  # NOTE: output should be appended as is, so Claude's thinking is passed back.
        reasoning_content = output.get("reasoning_content", "").strip()
        self.logger.show_message(
            role="agent",
            content=fixed_output_content,
            reasoning_content=reasoning_content,
            raw_message=raw_message,
            step_number=self.step_number,
        )
        self.last_code = code
        return [ExecutionIO(content=code)], standardized_usage, Status(failed=False)

    def workflow_state(self) -> dict:
        usage = self.usage_tracker.task_usage(self.world.task_id)
        return {
            "step": self.step_number,
            "taskInstruction": self.world.task.instruction,
            "lastCode": self.last_code,
            "lastObservation": self.last_observation,
            "lastError": self.last_error,
            "usage": {"costUsd": usage.cost, "outputTokens": usage.output_tokens},
            "historyLength": len(self.messages),
        }

    # ---------------- C 组件：每轮真实构建请求 ----------------

    def build_context_messages(self) -> list[dict[str, Any]]:
        lm_result = None
        lm_state = None
        granted = 0
        while True:
            result = self.component_host.c_build_context(
                self.candidate_id, self.context_input(lm_result, lm_state)
            )
            if "messages" in result:
                return result["messages"]
            if "lmRequest" not in result:
                raise CandidateError(
                    self.candidate_id,
                    "C",
                    f"buildContext 返回既无 messages 也无 lmRequest：{sorted(result)}",
                    "c.buildContext",
                )
            granted += 1
            if granted > MAX_LM_REQUESTS_PER_STEP:
                raise CandidateError(
                    self.candidate_id,
                    "C",
                    f"一轮 buildContext 的 lmRequest 超过上限 {MAX_LM_REQUESTS_PER_STEP}",
                    "c.buildContext",
                )
            request = result["lmRequest"]
            lm_state = request.get("state")
            output = self.language_model.generate(
                [{"role": "user", "content": request["prompt"]}],
                cache_control_at=-1,
                max_tokens=request.get("maxTokens"),
            )
            error_message = output.pop("error", None)
            if error_message:
                raise RuntimeError(f"buildContext 的子请求失败：{error_message}")
            # 子请求的用量当场入账：主请求失败时 solve_task 不会再加 usage
            self.usage_tracker.add(self.world.task_id, output.pop("standardized_usage"))
            lm_result = output["content"]

    def context_input(self, lm_result: str | None, lm_state: Any) -> dict:
        usage = self.usage_tracker.task_usage(self.world.task_id)
        remaining_cost = None
        if self.usage_tracker.max_cost_per_task is not None:
            remaining_cost = max(self.usage_tracker.max_cost_per_task - usage.cost, 0.0)
        context_input = {
            "task": {"id": self.world.task_id, "instruction": self.world.task.instruction},
            "messages": self.messages,
            "step": self.step_number,
            "budget": {
                "maxSteps": self.max_steps,
                "remainingSteps": max(self.max_steps - self.step_number, 0),
                "remainingCostUsd": remaining_cost,
                "maxOutputTokensPerTask": self.usage_tracker.max_output_tokens_per_task or 0,
            },
        }
        if lm_result is not None:
            context_input["lmResult"] = lm_result
        if lm_state is not None:
            context_input["state"] = lm_state
        return context_input

    # ---------------- 文本与代码解析 ----------------

    def extract_code_and_fix_content(self, text: str) -> tuple[str, str]:
        original_text = text
        output_code = ""
        match_end = 0
        # Handle multiple calls
        for re_match in re.finditer(self.full_code_regex, original_text, flags=re.DOTALL):
            code = re_match.group(1).strip()
            if self.ignore_multiple_calls:
                text = original_text[: re_match.end()]
                return code, text
            output_code += code + "\n"
            match_end = re_match.end()
        # check for partial code match at end (no terminating ```)  following the last match
        partial_match = re.match(
            self.partial_code_regex, original_text[match_end:], flags=re.DOTALL
        )
        if partial_match:
            output_code += partial_match.group(1).strip()
            # terminated due to stop condition. Add stop condition to output.
            if not text.endswith("\n"):
                text = text + "\n"
            text = text + "```"
        if len(output_code) == 0:
            return "", text
        else:
            return output_code, text

    def text_to_messages(self, input_str: str) -> list[dict[str, Any]]:
        messages_json: list[dict[str, Any]] = []
        last_start = 0
        for m in re.finditer("(USER|ASSISTANT|SYSTEM):\n", input_str, flags=re.IGNORECASE):
            last_end = m.span()[0]
            if len(messages_json) == 0:
                if last_end != 0:
                    raise ValueError(
                        f"Start of the prompt has no assigned role: {input_str[:last_end]}"
                    )
            else:
                messages_json[-1]["content"] = input_str[last_start:last_end]
            role = m.group(1).lower()
            messages_json.append({"role": role, "content": None})
            last_start = m.span()[1]
        messages_json[-1]["content"] = input_str[last_start:]
        return messages_json

    def messages_to_text(self, messages: list[dict[str, Any]]) -> str:
        output_str = ""
        for message in messages:
            role = message["role"]
            if role == "system":
                output_str += "SYSTEM:\n" + message["content"]
            if role == "assistant":
                output_str += "ASSISTANT:\n" + message["content"]
            elif role == "user":
                output_str += "USER:\n" + message["content"]
            else:
                raise ValueError(f"Unknown message role {role} in: {message}")
        return output_str
