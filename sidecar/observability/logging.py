import logging
from typing import Any

import structlog

REDACT_KEYS = frozenset({
    "prompt",
    "system_prompt",
    "append_system_prompt",
    "delta",
    "final_text",
    "text",
    "args",
    "tool_args",
})

REDACTED = "<redacted>"


def _redact_processor(_logger, _method, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in event_dict:
        if key in REDACT_KEYS and event_dict[key] not in (None, ""):
            event_dict[key] = REDACTED
    return event_dict


_configured = False


def configure_logging(*, level: str = "INFO", redact: bool = True) -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", level=level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if redact:
        processors.append(_redact_processor)
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
