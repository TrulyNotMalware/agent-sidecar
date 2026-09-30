import logging
from typing import Any

import structlog

from .redaction import scrub_secrets

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


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _scrub_processor(_logger, _method, event_dict: dict[str, Any]) -> dict[str, Any]:
    # Always on, LOG_PROMPTS or not: CLI stderr, provider errors and exception text are
    # logged for diagnosis and must not carry credentials.
    return {k: _scrub(v) for k, v in event_dict.items()}


def _scrub_arg(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, BaseException):
        return scrub_secrets(str(value))
    return value


def _install_stdlib_scrubbing() -> None:
    """Scrub records from plain `logging` too: uvicorn, the Agent SDK, anything else.

    Done where records are created, so it holds whatever handlers uvicorn installs
    later. Arguments are scrubbed one by one (uvicorn's access formatter unpacks
    them), and a traceback is formatted and scrubbed up front.
    """
    make_record = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = make_record(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = scrub_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub_secrets(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)


class _DropSdkReaderErrors(logging.Filter):
    """The Agent SDK logs a failed CLI read quoting the CLI's output line — conversation
    text, past LOG_PROMPTS. The turn reports that failure itself (turn.closed)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Fatal error in message reader")


_configured = False


def configure_logging(*, level: str = "INFO", redact: bool = True) -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", level=level)
    _install_stdlib_scrubbing()
    if redact:
        logging.getLogger("claude_agent_sdk._internal.query").addFilter(_DropSdkReaderErrors())

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]
    if redact:
        processors.append(_redact_processor)
    processors.append(_scrub_processor)
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
