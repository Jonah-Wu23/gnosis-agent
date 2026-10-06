import os
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field

from engine.scoped_units import ExecutionBlocked


class Quota(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remaining_usd: float = Field(ge=0)
    reset_at: float
    observed_at: float


def live_quota():
    endpoint = os.environ.get("GNOSIS_QUOTA_URL")
    if not endpoint:
        raise ExecutionBlocked("未配置实时服务额度接口 GNOSIS_QUOTA_URL")
    # 接口返回已归一化的剩余额度与更新时间，响应只在内存用于请求准入。
    with httpx.Client(timeout=30) as client:
        response = client.get(endpoint, headers={"Authorization": "Bearer " + os.environ["MODEL_API"]})
        response.raise_for_status()
        value = Quota.model_validate(response.json())
    if time.time() - value.observed_at > 60 or value.observed_at > time.time() + 5:
        raise ExecutionBlocked("实时服务额度记录已过期")
    return value.model_dump()
