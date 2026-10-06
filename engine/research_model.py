import json
import time
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError

from engine.scoped_units import POLICY, CallJournal, AIFormatError, ExecutionTruncated, require_model_access, save_json


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

    def resume(self, checkpoint, messages, options, validate=None):
        """断点恢复：先核对输入，再以调用记录里的真实响应为准；没有响应记录才返回 None 重发。

        恢复路径与实时路径使用同一套终止状态检查。
        """
        from openai.types.chat import ChatCompletion
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved["messages"] != messages or saved["options"] != options or saved["version"] != self.version:
            raise ValueError("提议断点输入发生变化")
        folder = self.output / "io" / saved["call"]
        if saved["status"] == "complete":
            response = ChatCompletion.model_validate(saved["response"])
        else:
            response_path = folder / "response.json"
            if not response_path.exists():
                self.meter.release(saved["call"], "断点恢复时该请求没有保存响应，按未计费释放预留")
                return None
            response = self.finish(checkpoint, saved["call"],
                                   ChatCompletion.model_validate_json(response_path.read_text(encoding="utf-8")),
                                   messages, options)
        self.checked(response, validate, folder)
        return response

    def request(self, messages, phase, checkpoint, validate=None, options=None):
        require_model_access()
        from engine.llm_client import chat, client
        client.max_retries = 0
        options = {"max_tokens": POLICY["proposal_output_tokens"], "temperature": 1, **(options or {})}
        checkpoint = checkpoint.resolve()
        if checkpoint.exists():
            response = self.resume(checkpoint, messages, options, validate)
            if response is not None:
                return response
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
        require_model_access()
        self.sequence += 1
        response = self.model.request(parameters["messages"], self.phase,
                                      self.folder / (str(self.sequence) + "_" + self.role + ".json"),
                                      validate=self.validator,
                                      options={"response_format": {"type": "json_object"}})
        self.last_response = response
        return response
