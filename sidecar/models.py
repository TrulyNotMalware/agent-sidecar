from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Both CLIs issue UUIDs (claude v4, codex v7). Anything else is refused: a leading "-"
# is parsed as a flag, and codex also resolves free-form *thread names* globally.
SESSION_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ConverseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_key: str = Field(alias="sessionKey", min_length=1, max_length=256)
    prompt: str = Field(min_length=1)
    # Handed to the CLI as `--resume <id>` / `resume <id>`; see SESSION_ID_PATTERN.
    session_id: str | None = Field(default=None, alias="sessionId", pattern=SESSION_ID_PATTERN)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    append_system_prompt: str | None = Field(default=None, alias="appendSystemPrompt")
    mode: Literal["session", "stateless"] = "session"

    @field_validator("session_id", mode="before")
    @classmethod
    def _empty_session_id_means_none(cls, v: object) -> object:
        # Clients without omitempty send "" for "start fresh"; treat it like null.
        # Lowercase so the same session is always looked up by the same id.
        if v == "":
            return None
        return v.lower() if isinstance(v, str) else v


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None


class ApiErrorBody(BaseModel):
    code: str
    message: str
