"""Telegram Bot API ``sendMessage``, standard library only (``urllib``).

The bot token lives only inside the request URL for the duration of one
call; it is never logged, printed or returned. Every exception raised here
is redacted with ``fintr.collector.secrets.redact_text`` before it leaves
this module, so a caller that prints an exception can never leak the
token. Plain text only: no ``parse_mode``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.request

from fintr.collector.secrets import redact_text


TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_SECONDS = 15
RETRY_AFTER_MAX_SECONDS = 30


class TelegramSendError(RuntimeError):
    """A ``sendMessage`` call failed; the message text is always redacted
    of the bot token (and of the full request URL, which embeds it)."""


@dataclass(frozen=True, slots=True)
class SendResult:
    message_id: int


def send_message(
    token: str, chat_id: str, text: str, *, timeout: float = HTTP_TIMEOUT_SECONDS,
) -> SendResult:
    """POST one plain-text message to Telegram; on a 429 response whose
    ``retry_after`` is under ``RETRY_AFTER_MAX_SECONDS``, retry exactly
    once after sleeping that long, otherwise raise."""
    if not token:
        raise ValueError("token must not be empty")
    if not chat_id:
        raise ValueError("chat_id must not be empty")
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    return _send(url, payload, token=token, timeout=timeout, allow_retry=True)


def _send(url: str, payload: bytes, *, token: str, timeout: float, allow_retry: bool) -> SendResult:
    request = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception as read_exc:  # e.g. http.client.IncompleteRead: not an
            # OSError/ValueError subclass, so it would otherwise escape every
            # except clause below still carrying the token-bearing ``url`` in
            # its frame.
            raise TelegramSendError(
                redact_text(
                    f"Telegram API hatası (HTTP {exc.code}); yanıt gövdesi okunamadı "
                    f"({read_exc})",
                    token, url,
                )
            ) from None
        if exc.code == 429 and allow_retry:
            retry_after = _retry_after(body_text)
            if retry_after is not None and retry_after < RETRY_AFTER_MAX_SECONDS:
                time.sleep(retry_after)
                return _send(url, payload, token=token, timeout=timeout, allow_retry=False)
        raise TelegramSendError(
            redact_text(f"Telegram API hatası (HTTP {exc.code}): {body_text}", token, url)
        ) from None
    except urllib.error.URLError as exc:
        raise TelegramSendError(
            redact_text(f"Telegram bağlantı hatası: {exc}", token, url)
        ) from None
    except (OSError, ValueError) as exc:
        raise TelegramSendError(
            redact_text(f"Telegram gönderim hatası: {exc}", token, url)
        ) from None
    except Exception as exc:  # defense in depth: any other stdlib exception (for
        # example http.client.IncompleteRead on the success-read path, which is
        # neither an OSError nor a ValueError) must still be redacted before it
        # can propagate with the token-bearing ``url`` anywhere in its message.
        raise TelegramSendError(
            redact_text(f"Telegram gönderim hatası (beklenmeyen): {exc}", token, url)
        ) from None

    if not isinstance(body, dict) or not body.get("ok"):
        raise TelegramSendError(redact_text(f"Telegram API ok=false döndürdü: {body}", token, url))
    result = body.get("result")
    if not isinstance(result, dict) or "message_id" not in result:
        raise TelegramSendError(redact_text(f"Telegram API beklenmeyen yanıt: {body}", token, url))
    return SendResult(message_id=result["message_id"])


def _retry_after(body_text: str) -> float | None:
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    params = parsed.get("parameters")
    if not isinstance(params, dict):
        return None
    value = params.get("retry_after")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
