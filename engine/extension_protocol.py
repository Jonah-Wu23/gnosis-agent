from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field


class OperationTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict


class CapabilityCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: dict


class ExtensionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Any = None
    text: str | None = None
    arguments: dict | None = None
    messages: list[dict] | None = None
    tools: list[OperationTool] = Field(default_factory=list)
    calls: list[CapabilityCall] = Field(default_factory=list)
    result: Any = None
    trace: Any = None


def validate_extension(value):
    result = ExtensionResult.model_validate(value)
    for tool in result.tools:
        if not tool.name.isidentifier():
            raise ValueError("操作函数名必须是有效标识符")
        Draft202012Validator.check_schema(tool.parameters)
        if tool.parameters.get("type") != "object":
            raise ValueError("操作函数参数必须使用 object schema")
    return result
