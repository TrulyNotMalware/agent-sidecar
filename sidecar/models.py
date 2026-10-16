import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Both CLIs issue UUIDs (claude v4, codex v7). Anything else is refused: a leading "-"
# is parsed as a flag, and codex also resolves free-form *thread names* globally.
SESSION_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

MAX_OUTPUT_SCHEMA_BYTES = 64 * 1024


class ConverseRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, extra="forbid")

    session_key: str = Field(alias="sessionKey", min_length=1, max_length=256)
    # Generous (~250k tokens) but bounded: the whole body is held in memory.
    prompt: str = Field(min_length=1, max_length=1_000_000)
    # Handed to the CLI as `--resume <id>` / `resume <id>`; see SESSION_ID_PATTERN.
    session_id: str | None = Field(default=None, alias="sessionId", pattern=SESSION_ID_PATTERN)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    append_system_prompt: str | None = Field(default=None, alias="appendSystemPrompt")
    mode: Literal["session", "stateless"] = "session"
    # `text` events as the model generates them, not one per finished text block.
    token_streaming: bool = Field(default=False, alias="tokenStreaming")
    # Run controls, each checked against the operator's setting (ALLOWED_MODELS,
    # MAX_TURNS, MAX_BUDGET_USD, TURN_TIMEOUT_SEC) before the turn starts.
    model: str | None = Field(default=None, min_length=1, max_length=256)
    max_turns: int | None = Field(default=None, alias="maxTurns", ge=1)
    max_budget_usd: float | None = Field(
        default=None, alias="maxBudgetUsd", gt=0, allow_inf_nan=False
    )
    timeout_sec: int | None = Field(default=None, alias="timeoutSec", ge=1)
    # A JSON Schema the final answer must match; it arrives parsed as done.structuredOutput.
    output_schema: dict[str, Any] | None = Field(default=None, alias="outputSchema")

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

    @field_validator("output_schema")
    @classmethod
    def _bounded_schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # claude takes it on argv (--json-schema), where Linux caps one argument at
        # 128 KiB; a real schema is a few KiB.
        if v is not None and len(json.dumps(v)) > MAX_OUTPUT_SCHEMA_BYTES:
            raise ValueError(f"must be at most {MAX_OUTPUT_SCHEMA_BYTES} bytes as JSON")
        return v

    @model_validator(mode="after")
    def _stateless_cannot_resume(self) -> Self:
        if self.mode == "stateless" and self.session_id is not None:
            raise ValueError("sessionId cannot be used with mode=stateless (nothing to resume)")
        return self


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None
