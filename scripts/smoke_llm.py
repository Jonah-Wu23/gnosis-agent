import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.llm_client import MODEL, chat


def run(title, messages, **params):
    t0 = time.perf_counter()
    resp = chat(messages, **params)
    elapsed = time.perf_counter() - t0

    msg = resp.choices[0].message
    details = resp.usage.completion_tokens_details
    reasoning_content = getattr(msg, "reasoning_content", None)

    print(f"===== {title} =====")
    print(f"请求模型: {MODEL}")
    print(f"响应 model 字段: {resp.model}")
    print(f"耗时: {elapsed:.2f}s")
    print(f"usage.prompt_tokens: {resp.usage.prompt_tokens}")
    print(f"usage.completion_tokens: {resp.usage.completion_tokens}")
    print(f"usage.total_tokens: {resp.usage.total_tokens}")
    print(f"usage.completion_tokens_details.reasoning_tokens: {details.reasoning_tokens}")
    print(f"message.reasoning_content 长度: {len(reasoning_content) if reasoning_content else 0}")
    print("内容:")
    print(msg.content)
    print()


run(
    "请求 1：连通性与模型 ID",
    [{"role": "user", "content": "用一句话说明你是谁。"}],
    max_tokens=512,
)

run(
    "请求 2：思考强度 high",
    [
        {
            "role": "user",
            "content": "一个笼子里有鸡和兔，共 35 个头、94 只脚。鸡兔各几只？先推理再给答案。",
        }
    ],
    reasoning_effort="high",
    max_tokens=2048,
)
