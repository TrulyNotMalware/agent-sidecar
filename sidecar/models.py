from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConverseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_key: str = Field(alias="sessionKey", min_length=1, max_length=256)
    prompt: str = Field(min_length=1)
    session_id: str | None = Field(default=None, alias="sessionId")
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    append_system_prompt: str | None = Field(default=None, alias="appendSystemPrompt")
    mode: Literal["session", "stateless"] = "session"


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None


class ApiErrorBody(BaseModel):
    code: str
    message: str
