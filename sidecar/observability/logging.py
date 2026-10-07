import logging

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from .redaction import scrub_secrets

REDACT_KEYS = frozenset(
    {
        "prompt",
        "system_prompt",
        "append_system_prompt",
        "delta",
        "final_text",
        "text",
        "args",
        "tool_args",
    }
)

REDACTED = "<redacted>"


def _redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if key in REDACT_KEYS and event_dict[key] not in (None, ""):
            event_dict[key] = REDACTED
    return event_dict


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _scrub_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    # Always on, LOG_PROMPTS or not: CLI stderr, provider errors and exception text are
    # logged for diagnosis and must not carry credentials.
    return {k: _scrub(v) for k, v in event_dict.items()}


def _scrub_arg(value: object) -> object:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, BaseException):
        return scrub_secrets(str(value))
    return value


def _install_stdlib_scrubbing() -> None:
    """Scrub records from plain `logging` too: uvicorn, the Agent SDK, anything else.

    Done where records are created, so it holds whatever handlers uvicorn installs
    later. The message is formatted first and then scrubbed, so a secret split
    between the format string and an argument is caught too; uvicorn's access log
    keeps its arguments (its formatter unpacks them), scrubbed one by one. A
    traceback is formatted and scrubbed up front.
    """
    make_record = logging.getLogRecordFactory()

    def scrub_args(record: logging.LogRecord) -> None:
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v) for k, v in record.args.items()}

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = make_record(*args, **kwargs)
        if record.name == "uvicorn.access":
            scrub_args(record)
        else:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 — a bad format string; logging reports it
                scrub_args(record)
            else:
                record.msg, record.args = scrub_secrets(message), ()
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub_secrets(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)


class _DropSdkLinesQuotingCliOutput(logging.Filter):
    """The Agent SDK quotes the CLI's output — conversation text, past LOG_PROMPTS —
    when a read fails (the turn reports that failure itself, on turn.closed) and, at
    DEBUG, for lines it skips."""

    _PREFIXES = (
        "Fatal error in message reader",
        "Skipping non-JSON line from CLI stdout",
        "Dropping truncated JSON",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self._PREFIXES)


# What every line carries, whether it came from structlog or from plain logging.
_SHARED_PROCESSORS: tuple[Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
)


def _render_processors(*, redact: bool) -> list[Processor]:
    processors: list[Processor] = [_redact_processor] if redact else []
    processors += [_scrub_processor, structlog.processors.JSONRenderer()]
    return processors


def stdlib_formatter(*, redact: bool = True) -> logging.Formatter:
    """Render plain `logging` records (uvicorn, the Agent SDK) like structlog's lines.

    One JSON object per record: the message as `event`, level, timestamp, the bound
    context (turn_id), and a scrubbed traceback under `exception`.
    """
    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            *_render_processors(redact=redact),
        ],
        foreign_pre_chain=list(_SHARED_PROCESSORS),
    )


_configured = False


def configure_logging(*, level: str = "INFO", redact: bool = True) -> None:
    """Configure structlog and plain logging once: every line is one JSON object.

    The first configuration wins: tests build several apps in one process.
    """
    global _configured  # noqa: PLW0603 — the flag is exactly that fact
    if _configured:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(stdlib_formatter(redact=redact))
    logging.basicConfig(handlers=[handler], level=level)
    _install_stdlib_scrubbing()
    if redact:
        for name in (
            "claude_agent_sdk._internal.query",
            "claude_agent_sdk._internal.transport.subprocess_cli",
        ):
            logging.getLogger(name).addFilter(_DropSdkLinesQuotingCliOutput())

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.processors.format_exc_info,
            *_render_processors(redact=redact),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
