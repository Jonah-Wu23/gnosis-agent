import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")

BASE_URL = os.environ["MODER_PORVIDER"]
MODEL = os.environ["MODER_NAME"]

# 思考强度固定为 high，调用方无法传入更低档位
REASONING_EFFORT = "high"

client = OpenAI(
    base_url=BASE_URL,
    api_key=os.environ["MODEL_API"],
)


def chat(messages, *, reasoning_effort=REASONING_EFFORT, **params):
    """发一次 Chat Completions 请求，返回原始响应对象。"""
    return client.chat.completions.create(
        model=MODEL,
        messages=messages,
        reasoning_effort=reasoning_effort,
        **params,
    )
