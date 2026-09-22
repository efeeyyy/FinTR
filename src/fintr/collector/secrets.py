"""Runtime-only secret providers and defensive redaction helpers."""

from __future__ import annotations

from getpass import getpass
import re
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
_INIT_DATA_PATTERN = re.compile(
    r"(?i)(init_data(?:%5B[^%]*%5D)?=)([^&\s'\"]+)"
)


class SecretProvider(Protocol):
    def get_init_data(self) -> str:
        """Return Telegram initData from a runtime-only source."""


class InteractiveSecretProvider:
    """Read initData from a hidden terminal prompt without retaining it."""

    def __init__(self, prompt: str = "Telegram initData: ") -> None:
        self._prompt = prompt

    def get_init_data(self) -> str:
        value = getpass(self._prompt)
        if not value:
            raise ValueError("initData must not be empty")
        return value

    def __repr__(self) -> str:
        return "InteractiveSecretProvider()"


def build_authenticated_url(endpoint: str, init_data: str) -> str:
    """Build the transient connection URL; callers must never log the result."""

    if not init_data:
        raise ValueError("initData must not be empty")
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain query or fragment data")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode({"init_data": init_data}), "")
    )


def redact_url(url: str) -> str:
    """Mask init_data while preserving a useful endpoint representation."""

    try:
        parsed = urlsplit(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return redact_text(url)
    redacted_query = [
        (key, REDACTED if key.lower() == "init_data" else value)
        for key, value in query
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(redacted_query, safe="[]"),
            parsed.fragment,
        )
    )


def redact_text(text: str, *secret_values: str) -> str:
    """Redact known secret values and init_data query parameters from text."""

    redacted = text
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return _INIT_DATA_PATTERN.sub(lambda match: match.group(1) + REDACTED, redacted)
