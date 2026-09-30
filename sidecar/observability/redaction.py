"""Keep credentials out of what leaves the process: error frames, log lines, spans.

Two layers: the exact values of the credentials this process knows about (registered
at startup, plus the current request's turn token), and patterns for
credential-shaped strings it does not know (an API key echoed back by the provider,
an Authorization header in CLI stderr).
"""

import contextvars
import re

REDACTED = "<redacted>"

_MIN_SECRET_LEN = 8
_known: set[str] = set()
# Per request: the handler registers it, and the turn's task and stream inherit it.
_turn_secrets: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "turn_secrets", default=()
)

_PATTERNS = (
    # Credentials in a URL: https://user:pass@gateway
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), REDACTED + "@"),
    # Anthropic / OpenAI keys (sk-ant-…, sk-proj-…, and the masked "sk-proj-****abcd"
    # providers echo back). A bare "sk-" only when key-length, so "sk-learn" or a
    # sessionKey like "tenant:sk-dashboard" survive.
    (
        re.compile(r"sk-(?:ant|proj|svcacct|admin)-[\w\-*]{4,}|(?<![A-Za-z0-9])sk-[\w\-]{20,}"),
        "sk-" + REDACTED,
    ),
    # Authorization schemes. The token must contain a digit or punctuation, so prose
    # like "missing bearer authentication" is left alone.
    (
        re.compile(
            r"(?i)\b(bearer|basic)([\s:]+[\"']?)(?=[\w.~+/=\-]*[0-9.~+/=\-])[\w.~+/=\-]{8,}"
        ),
        r"\1\2" + REDACTED,
    ),
    # A JWT anywhere (e.g. a turn token in a header dump).
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*"), REDACTED),
    # key=value / "key": "value" / header: value
    (
        re.compile(
            r"(?i)\b(x-api-key|api[_-]?key|access[_-]?token|turn[_-]?token|token|secret|password)"
            r"(\"?\s*[:=]\s*\"?)[^\s\"'&,;]{8,}"
        ),
        r"\1\2" + REDACTED,
    ),
)


def register_secrets(*values: str | None) -> None:
    """Remember credential values so scrub_secrets() replaces them wherever they appear."""
    _known.update(v for v in values if v and len(v) >= _MIN_SECRET_LEN)


def register_turn_secret(value: str | None) -> None:
    """Like register_secrets, for the current request's context only (a turn token)."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _turn_secrets.set((*_turn_secrets.get(), value))


def scrub_secrets(text: str) -> str:
    # Longest first, so a secret that contains another is replaced whole.
    for value in sorted({*_known, *_turn_secrets.get()}, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
