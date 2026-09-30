from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Both CLIs issue UUIDs (claude v4, codex v7). Anything else is refused: a leading "-"
# is parsed as a flag, and codex also resolves free-form *thread names* globally.
SESSION_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ConverseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_key: str = Field(alias="sessionKey", min_length=1, max_length=256)
    # Generous (~250k tokens) but bounded: the whole body is held in memory.
    prompt: str = Field(min_length=1, max_length=1_000_000)
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

    @field_validator("system_prompt", "append_system_prompt")  # constrained ones: pydantic
    @classmethod
    def _encodable(cls, v: str | None) -> str | None:
        # JSON can carry a lone surrogate ("\ud800"); it cannot be written to the CLI's
        # stdin, argv or a file, so it would fail the turn as an internal error.
        if v is not None:
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("must be valid Unicode text (no lone surrogates)") from None
        return v

    @model_validator(mode="after")
    def _stateless_cannot_resume(self) -> "ConverseRequest":
        if self.mode == "stateless" and self.session_id is not None:
            raise ValueError("sessionId cannot be used with mode=stateless (nothing to resume)")
        return self


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None

