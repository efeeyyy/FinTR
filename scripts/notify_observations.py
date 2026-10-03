"""CLI: Telegram observation notifications from a saved scanner output.

Reads ``<scanner-dir>/<as-of>/observations.json`` (schema
``fintr_scanner_observations_v1`` or ``_v2``, written by
``scripts/scan_observations.py``; v2 only when it was run with
``--daily-root``) and sends a plain-text, Turkish observation notice to
Efe's own Telegram chat via the Bot API (``fintr.notify.telegram``, stdlib
``urllib`` only).
This is an observation notice: no score, rank, return, target, stop,
position size or free-form buy/sell recommendation. One narrow exception
(Efe's decision of 2026-10-02, personal use; AGENTS.md "Guvenlik
sinirlari"): each condition_met line carries the frozen rule's
hypothesised direction label ("beklenen yön: yukarı (al yönlü)" / "aşağı
(sat yönlü)", read from the hash-checked frozen spec's
``predicted_direction``), with an unverified-hypothesis warning;
``--no-direction-label`` restores the previous wording. See
``docs/notify.md`` and ``docs/scanner.md``.

Safe defaults: dry-run unless ``--send`` is passed (no network call, the
message(s) are only printed to stdout); the token and chat id are read
only from ``FINTR_TELEGRAM_BOT_TOKEN``/``FINTR_TELEGRAM_CHAT_ID``, never
from a CLI argument, and are never written to a file, log, receipt or
exception message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.llm_note import packet as llm_packet, render as llm_render
from fintr.notify import format as fmt
from fintr.notify.direction import load_rule_directions
from fintr.notify.telegram import TelegramSendError, send_message
from fintr.scanner.rules import ScannerRulesError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCANNER_DIR = Path("data/interim/scanner")
DEFAULT_OUT_DIR = Path("data/interim/notify")
DEFAULT_RULE_TEXTS_FILE = Path("configs/notify/rule_texts_v1.json")
DEFAULT_RULES_FILE = Path("configs/scanner/rules_v1.json")

RULE_TEXTS_SCHEMA = "fintr_notify_rule_texts_v1"
RECEIPT_SCHEMA = "fintr_notify_receipt_v1"


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a plain-text Turkish observation notice for one saved scanner "
                     "output to Efe's own Telegram chat. Dry-run by default (no network); "
                     "--send is required to actually call the Telegram Bot API.",
    )
    parser.add_argument("--as-of", type=_date, required=True,
                         help="reads <scanner-dir>/<as-of>/observations.json")
    parser.add_argument("--scanner-dir", type=Path, default=DEFAULT_SCANNER_DIR,
                         help=f"default: {DEFAULT_SCANNER_DIR}")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--rule-texts", type=Path, default=DEFAULT_RULE_TEXTS_FILE,
                         help=f"default: {DEFAULT_RULE_TEXTS_FILE}")
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE,
                         help="frozen scanner rule file the direction label is read from "
                              f"(resolved against the repo root); default: {DEFAULT_RULES_FILE}")
    parser.add_argument(
        "--no-direction-label", action="store_true",
        help="do not label observations with the frozen rule's hypothesised direction; "
             "restores the wording used before Efe's 2026-10-02 decision",
    )
    parser.add_argument(
        "--llm-note-dir", type=Path, default=None,
        help="default: off. Root of the analysis notes written by scripts/llm_note.py "
             "(<dir>/<as-of>/attempt-NNN/); a verified note is added as separate message(s) "
             "after the deterministic notice, otherwise one status line (docs/notify.md)",
    )
    parser.add_argument(
        "--skip-empty", action="store_true",
        help="send nothing (not even the short 'no observation' notice) when there is no "
             "condition_met=true observation",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="actually call the Telegram Bot API; default is dry-run (stdout only, no network)",
    )
    parser.add_argument(
        "--resend", action="store_true",
        help="send again, under a new receipt file, even though a receipt already exists "
             "for this as_of",
    )
    return parser


def load_rule_texts(path: Path) -> dict[str, str]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable_rule_texts_file ({path})") from exc
    if not isinstance(document, dict) or document.get("schema") != RULE_TEXTS_SCHEMA:
        raise ValueError(f"unsupported_rule_texts_schema ({path})")
    rules = document.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise ValueError(f"rule_texts file has no rules ({path})")
    return {str(key): str(value) for key, value in rules.items()}


def _receipt_target(receipt_dir: Path, *, resend: bool) -> Path:
    """The receipt path to write to; never an existing file. Without
    ``--resend``, only the primary ``receipt.json`` is ever offered, and
    the caller must treat its existence as a rejection."""
    primary = receipt_dir / "receipt.json"
    if not primary.exists():
        return primary
    if not resend:
        raise FileExistsError(primary)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = receipt_dir / f"receipt-{stamp}.json"
    suffix = 2
    while candidate.exists():
        candidate = receipt_dir / f"receipt-{stamp}-{suffix}.json"
        suffix += 1
    return candidate


def _write_receipt(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    obs_path = args.scanner_dir / args.as_of.isoformat() / "observations.json"
    try:
        obs_bytes = obs_path.read_bytes()
    except OSError:
        print(f"Notify failed: observations dosyası okunamadı ({obs_path})", file=sys.stderr)
        return 1
    try:
        document = json.loads(obs_bytes)
    except json.JSONDecodeError:
        print(f"Notify failed: observations dosyası geçersiz JSON ({obs_path})", file=sys.stderr)
        return 1
    if not isinstance(document, dict) or document.get("schema") not in fmt.SUPPORTED_OBSERVATIONS_SCHEMAS:
        print(
            f"Notify failed: beklenmeyen şema ({obs_path}); gerekli: "
            f"{' veya '.join(fmt.SUPPORTED_OBSERVATIONS_SCHEMAS)}",
            file=sys.stderr,
        )
        return 1

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        print(f"Notify failed: {exc}", file=sys.stderr)
        return 1
    symbols = sorted(document.get("symbols", {}))
    blocked = sorted(
        symbol for symbol in symbols if guards.is_excluded(symbol) or guards.is_holdout(symbol)
    )
    if blocked:
        print(
            f"Notify failed: observations dosyasında holdout veya dışlanan hisse var "
            f"({','.join(blocked)}); gönderim reddedildi",
            file=sys.stderr,
        )
        return 1

    try:
        rule_texts = load_rule_texts(args.rule_texts)
    except ValueError as exc:
        print(f"Notify failed: {exc}", file=sys.stderr)
        return 1

    positive = fmt.has_positive_observations(document)
    if not positive and args.skip_empty:
        print(
            "Notify: bu taramada gözlem yok; --skip-empty verildi, hiçbir şey gönderilmiyor.",
            file=sys.stderr,
        )
        return 0

    directions: dict[str, str] | None = None
    if not args.no_direction_label:
        try:
            directions = load_rule_directions(args.rules_file, project_root=PROJECT_ROOT)
        except ScannerRulesError as exc:
            # Fail-safe: never guess a direction; every line says "yön bilinmiyor".
            print(
                f"Notify: yön etiketi kaynağı doğrulanamadı ({exc}); satırlar 'yön bilinmiyor' "
                "ile gösterilecek",
                file=sys.stderr,
            )
            directions = {}

    messages = fmt.render_notification(document, rule_texts, directions)
    note_messages: list[str] = []
    if args.llm_note_dir is not None:
        note_messages = llm_render.note_messages(
            args.llm_note_dir, args.as_of.isoformat(), directions=directions,
            has_observations=positive, expected_symbols=llm_packet.positive_symbols(document),
        )
        messages = messages + note_messages

    if not args.send:
        for index, message in enumerate(messages, start=1):
            print(f"--- mesaj {index}/{len(messages)} (dry-run; ağ kullanılmadı) ---")
            print(message)
        return 0

    receipt_dir = args.out_dir / args.as_of.isoformat()
    try:
        receipt_target = _receipt_target(receipt_dir, resend=args.resend)
    except FileExistsError:
        print(
            f"Notify failed: bu as_of için receipt zaten var ({receipt_dir / 'receipt.json'}); "
            "--resend ile yeniden gönder",
            file=sys.stderr,
        )
        return 1

    token = os.environ.get("FINTR_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("FINTR_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "Notify failed: FINTR_TELEGRAM_BOT_TOKEN ve FINTR_TELEGRAM_CHAT_ID ortam "
            "değişkenleri gerekli (bkz. docs/notify.md)",
            file=sys.stderr,
        )
        return 1

    sent_ids: list[int] = []
    try:
        for message in messages:
            result = send_message(token, chat_id, message)
            sent_ids.append(result.message_id)
    except TelegramSendError as exc:
        print(f"Notify failed: {exc}", file=sys.stderr)
        return 1

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "as_of": args.as_of.isoformat(),
        "sent_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "observations_path": str(obs_path),
        "observations_sha256": hashlib.sha256(obs_bytes).hexdigest(),
        "message_sha256": [hashlib.sha256(message.encode("utf-8")).hexdigest() for message in messages],
        "telegram_message_ids": sent_ids,
        "resend": bool(args.resend),
    }
    if args.llm_note_dir is not None:
        receipt["llm_note_messages"] = len(note_messages)
    receipt_payload = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        _write_receipt(receipt_target, receipt_payload)
    except FileExistsError:
        print(
            f"Notify: mesaj(lar) gönderildi ama receipt yazılamadı (çakışma): {receipt_target}",
            file=sys.stderr,
        )
        return 1
    print(f"Notify: {len(messages)} mesaj gönderildi; receipt: {receipt_target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
