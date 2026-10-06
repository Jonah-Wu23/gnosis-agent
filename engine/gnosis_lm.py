import json
import os
import time
from dataclasses import dataclass

import openai

from engine.agent_runtime import Usage
from engine.llm_client import chat


@dataclass
class RateCard:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float

    def cost_of(self, input_tokens: int, output_tokens: int, cached_input_tokens: int) -> float:
        non_cached = max(input_tokens - cached_input_tokens, 0)
        return (
            non_cached * self.input_per_mtok
            + cached_input_tokens * self.cache_read_per_mtok
            + output_tokens * self.output_per_mtok
        ) / 1e6


class GnosisLM:
    """自有模型客户端：任务执行与（后续）GEPA 反思共用同一实例。

    对接 vendored agent 的 generate(messages) 接口；同时实现 GEPA reflection_lm
    需要的 __call__ 与 total_cost 属性。usage 全部来自真实 API 响应，不做字符估算。
    """

    def __init__(
        self,
        rate_card: dict,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        seed: int = 100,
        max_retries: int = 5,
        retry_after_n_seconds: float = 15,
    ):
        self.rate_card = RateCard(
            input_per_mtok=rate_card["input_per_mtok"],
            output_per_mtok=rate_card["output_per_mtok"],
            cache_read_per_mtok=rate_card["cache_read_per_mtok"],
        )
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.seed = seed
        self.max_retries = max_retries
        self.retry_after_n_seconds = retry_after_n_seconds
        self._total_input = 0
        self._total_output = 0
        self._total_reasoning = 0
        self._total_cached_input = 0
        self._total_cost = 0.0
        self._log_dir: str | None = None
        self._call_index = 0

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def total_tokens_in(self) -> int:
        return self._total_input

    @property
    def total_tokens_out(self) -> int:
        return self._total_output

    @property
    def total_reasoning_tokens(self) -> int:
        return self._total_reasoning

    def log_calls_to(self, world) -> None:
        self._log_dir = os.path.join(world.output_misc_directory, "lm_calls")
        os.makedirs(self._log_dir, exist_ok=True)
        self._call_index = 0

    def _log_call(self, messages, response, usage: Usage) -> None:
        if self._log_dir is None:
            return
        self._call_index += 1
        record = {
            "call_index": self._call_index,
            "request": {"messages": messages},
            "response": {
                "content": response.choices[0].message.content,
                "model": response.model,
            },
            "usage": usage.__dict__,
        }
        path = os.path.join(self._log_dir, f"{self._call_index:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)

    def _chat_once(self, messages, max_tokens=None):
        return chat(
            messages,
            temperature=self.temperature,
            seed=self.seed,
            max_tokens=max_tokens or self.max_tokens,
        )

    def retry_delay(self, attempt, error):
        from engine.scoped_units import retry_delay

        return retry_delay(attempt, error)

    def generate(self, messages, cache_control_at=None, max_tokens=None) -> dict:
        # cache_control_at 为官方 agent 的接口参数；本网关缓存由服务端自动处理，忽略该参数
        # max_tokens 供 C 组件的 lmRequest 限定摘要子请求的长度，缺省用实例配置
        last_error = None
        for attempt in range(self.max_retries + 1):
            self._retry_attempt = attempt
            try:
                response = self._chat_once(messages, max_tokens=max_tokens)
                break
            except openai.APIConnectionError as e:
                last_error = e
            except openai.APIStatusError as e:
                if e.status_code in (408, 429) or e.status_code >= 500:
                    last_error = e
                else:
                    raise
            if attempt < self.max_retries:
                time.sleep(self.retry_delay(attempt, last_error))
        else:
            raise last_error
        u = response.usage
        cached = 0
        if u.prompt_tokens_details is not None:
            cached = u.prompt_tokens_details.cached_tokens or 0
        reasoning = 0
        if u.completion_tokens_details is not None:
            reasoning = u.completion_tokens_details.reasoning_tokens or 0
        cost = self.rate_card.cost_of(u.prompt_tokens, u.completion_tokens, cached)
        usage = Usage(
            input_tokens=u.prompt_tokens,
            output_tokens=u.completion_tokens,
            cached_input_tokens=cached,
            reasoning_tokens=reasoning,
            cost=cost,
        )
        self._total_input += u.prompt_tokens
        self._total_output += u.completion_tokens
        self._total_cached_input += cached
        self._total_reasoning += reasoning
        self._total_cost += cost
        self._log_call(messages, response, usage)
        content = response.choices[0].message.content or ""
        # drop_reasoning_content=true：reasoning_content 不回投、不进消息历史
        return {
            "role": "assistant",
            "content": content,
            "standardized_usage": usage,
        }

    def __call__(self, prompt) -> str:
        # GEPA reflection_lm 契约：str 或 messages 进，文本出
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = prompt
        return self.generate(messages)["content"]
