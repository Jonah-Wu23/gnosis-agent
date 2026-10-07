import json
import os
import time
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError

from engine.scoped_units import POLICY, CallJournal, AIFormatError, ExecutionTruncated, require_model_access, save_json


def proposal_messages_match(saved_messages, current_messages, allow_budget_drift=False):
    """核对提议断点输入是否一致。

    在多路并发学习与断点续跑时，共享账本中的实时动态计费字段（budget 与 arm_remaining_usd）
    会随其他并发任务产生微小漂移。只要提议的全部核心业务字段（工作版本、已发布版本、组件源码、
    执行证据记录、任务集、上一轮反馈、动作模式等）严格一致，断点即可判定为输入未变并无损复用。
    """
    if saved_messages == current_messages:
        return True
    if len(saved_messages) != len(current_messages):
        return False
    for index, (saved, current) in enumerate(zip(saved_messages, current_messages)):
        if saved == current:
            continue
        if {k: v for k, v in saved.items() if k != "content"} != {k: v for k, v in current.items() if k != "content"}:
            return False
        if not allow_budget_drift or index != 1 or saved.get("role") != "user":
            return False
        if "content" not in saved or "content" not in current:
            return False
        s_content = saved["content"]
        c_content = current["content"]
        try:
            s_data = json.loads(s_content)
            c_data = json.loads(c_content)
            if isinstance(s_data, dict) and isinstance(c_data, dict):
                s_norm = {k: v for k, v in s_data.items() if k not in ("budget", "arm_remaining_usd")}
                c_norm = {k: v for k, v in c_data.items() if k not in ("budget", "arm_remaining_usd")}
                if s_norm == c_norm:
                    continue
        except (json.JSONDecodeError, TypeError):
            pass
        return False
    return True


class ResearchModel:
    def __init__(self, meter, version):
        self.meter = meter
        self.version = version
        self.output = meter.output
        self.journal = CallJournal(meter.output, version)

    def checked(self, response, validate, folder=None):
        """实时路径与断点恢复路径共用的终止状态检查；失败时把真实失败写进调用记录。"""
        choice = response.choices[0] if response.choices else None
        try:
            if choice is None:
                raise AIFormatError("自主修复响应没有候选结果")
            if choice.finish_reason == "length":
                raise ExecutionTruncated("自主修复响应达到输出上限")
            if choice.finish_reason != "stop" or not choice.message.content:
                raise AIFormatError("自主修复响应未正常完成")
            if validate:
                validate(choice.message.content)
        except (AIFormatError, ExecutionTruncated) as error:
            if folder is not None:
                self.journal.failure(folder, "execution_truncated" if isinstance(error, ExecutionTruncated)
                                     else "ai_format_error", error)
            raise

    def finish(self, checkpoint, call_id, response, messages, options):
        folder = self.output / "io" / call_id
        if not (folder / "response.json").exists():
            self.journal.response(folder, response)
        self.meter.settle(call_id, response.usage)
        save_json(checkpoint, {"version": self.version, "messages": messages, "options": options,
                               "status": "complete", "response": response.model_dump(mode="json"),
                               "call": call_id})
        return response

    def resume(self, checkpoint, messages, options, validate=None, allow_budget_drift=False):
        """断点恢复：先核对输入，再以调用记录里的真实响应为准；没有响应记录才返回 None 重发。

        恢复路径与实时路径使用同一套终止状态检查。
        """
        from openai.types.chat import ChatCompletion
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not proposal_messages_match(saved["messages"], messages, allow_budget_drift) or saved["options"] != options or saved["version"] != self.version:
            raise ValueError("提议断点输入发生变化")
        folder = self.output / "io" / saved["call"]
        if saved["status"] == "complete":
            response = ChatCompletion.model_validate(saved["response"])
        else:
            response_path = folder / "response.json"
            if not response_path.exists():
                if os.environ.get("GNOSIS_RESUME_ONLY") == "1":
                    raise RuntimeError("断点续跑模式下断点缺少有效响应记录，禁止重新生成: " + str(checkpoint))
                self.meter.release(saved["call"], "断点恢复时该请求没有保存响应，按未计费释放预留")
                return None
            response = self.finish(checkpoint, saved["call"],
                                   ChatCompletion.model_validate_json(response_path.read_text(encoding="utf-8")),
                                   messages, options)
        self.checked(response, validate, folder)
        return response

    def request(self, messages, phase, checkpoint, validate=None, options=None, allow_budget_drift=False):
        options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 1, **(options or {})}
        checkpoint = checkpoint.resolve()
        resume_mode = os.environ.get("GNOSIS_RESUME_ONLY") == "1"
        if checkpoint.exists():
            response = self.resume(checkpoint, messages, options, validate, allow_budget_drift)
            if response is not None:
                return response
        elif resume_mode:
            raise FileNotFoundError("断点续跑模式下未找到已有提议断点: " + str(checkpoint))
        require_model_access()
        from engine.llm_client import chat, client
        client.max_retries = 0
        for attempt in range(POLICY["service_retries"] + 1):
            self.journal.verify_version()
            call_id = self.meter.reserve(phase, messages, options["max_tokens"])
            folder = self.journal.request(call_id, phase, messages, options)
            # 请求发出前先落断点，进程中断后仍能按调用记录核对这次请求的结果。
            save_json(checkpoint, {"version": self.version, "messages": messages, "options": options,
                                   "status": "inflight", "call": call_id})
            try:
                response = chat(messages, **options)
            except (APIConnectionError, APIStatusError) as error:
                delay = self.journal.retry_service(folder, error, attempt)
                self.meter.release(call_id, "服务失败未取得响应")
                if delay is None:
                    raise
                time.sleep(delay)
                continue
            self.finish(checkpoint, call_id, response, messages, options)
            self.checked(response, validate, folder)
            return response
        raise RuntimeError("服务重试流程未返回响应")


class ACETransport:
    def __init__(self, model, phase, folder):
        self.model = model
        self.phase = phase
        self.folder = folder
        self.role = None
        self.validator = None
        self.sequence = max((int(path.name.split("_", 1)[0]) for path in folder.glob("*.json")
                             if path.name.split("_", 1)[0].isdigit()), default=0)
        self.last_response = None
        # 官方 ACE 接收 OpenAI 客户端协议；全部请求经同一个真实计费入口发送。
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **parameters):
        self.sequence += 1
        response = self.model.request(parameters["messages"], self.phase,
                                      self.folder / (str(self.sequence) + "_" + self.role + ".json"),
                                      validate=self.validator,
                                      options={"response_format": {"type": "json_object"}})
        self.last_response = response
        return response
