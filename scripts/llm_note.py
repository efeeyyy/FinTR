"""CLI: daily analysis note (Claude Opus 5.5 through the Claude Code CLI).

Reads ``<scanner-dir>/<as-of>/observations.json``; for the symbols with a
``condition_met`` observation builds a raw-data packet (``fintr.llm_note
.packet``; nothing after ``as_of``, no price series, no returns), asks the
model for a structured *evidence assessment* of each frozen rule's direction
(``destekliyor``/``zayıflatıyor``/``belirsiz``) and validates the answer
fail-closed (``fintr.llm_note.validate``). Result files go to
``<out-dir>/<as_of>/attempt-NNN/`` (no overwrite). See ``docs/notify.md``,
``docs/architecture.md`` and AGENTS.md (exception of 2026-10-02): the note
never contains an order, target, stop, position, return forecast or score,
and a failure here never blocks the deterministic notification.

Last stdout line: ``LLM_NOTE_STATUS: <status>`` where status is one of
``accepted``, ``already_noted``, ``no_observations``, ``dry_run``,
``rejected:<category>`` ("analiz notu doğrulamadan geçemedi"),
``unavailable:<category>`` ("analiz notu alınamadı") or
``error:<category>``. Exit 0: accepted/already_noted/no_observations/
dry_run; 3: rejected/unavailable; 1: error (nothing was sent to the model).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from fintr.evaluation.live_capture import LivePilotCaptureError, validate_context_contains_no_secrets
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.llm_note import runner
from fintr.llm_note.config import ConfigError, load_config
from fintr.llm_note.loader import load_composed_capture
from fintr.llm_note.packet import GuardRefusal, PacketError, build_packet
from fintr.llm_note.record import (
    DETERMINISM_NOTE, RECORD_SCHEMA, json_bytes, latest_accepted, new_attempt_dir, write_new,
)
from fintr.llm_note.validate import Validation, validate_note
from fintr.notify import format as fmt
from fintr.scanner.report import resolve_code_commit
from fintr.scanner.rules import ScannerRulesError, load_scanner_rules


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCANNER_DIR = Path("data/interim/scanner_daily")
DEFAULT_DAILY_ROOT = Path("data/interim/daily_capture")
DEFAULT_BASE_ROOT = Path("data/interim/period_analysis")
DEFAULT_PRICE_DIR = Path("data/interim/price_history")
DEFAULT_OUT_DIR = Path("data/interim/llm_notes")
DEFAULT_CONFIG = Path("configs/llm_note/config_v1.json")
DEFAULT_RULES_FILE = Path("configs/scanner/rules_v1.json")
DEFAULT_RULE_TEXTS_FILE = Path("configs/notify/rule_texts_v1.json")
STATUS_PREFIX = "LLM_NOTE_STATUS: "


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily analysis note: evidence assessment of the frozen rules' direction for "
                    "condition_met observations, by Claude Opus 5.5 through the Claude Code CLI "
                    "(subscription, no API key). Fail-closed validation; never blocks the "
                    "deterministic notification."
    )
    parser.add_argument("--as-of", type=_date, required=True,
                        help="reads <scanner-dir>/<as-of>/observations.json")
    parser.add_argument("--scanner-dir", type=Path, default=DEFAULT_SCANNER_DIR,
                        help=f"default: {DEFAULT_SCANNER_DIR}")
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT,
                        help=f"default: {DEFAULT_DAILY_ROOT}")
    parser.add_argument("--base-root", type=Path, default=DEFAULT_BASE_ROOT,
                        help=f"base period captures; default: {DEFAULT_BASE_ROOT}")
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR,
                        help="read only for the trading-day calendar (gap detection); "
                             f"default: {DEFAULT_PRICE_DIR}")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"default: {DEFAULT_CONFIG} (resolved against the repo root)")
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE,
                        help=f"default: {DEFAULT_RULES_FILE}")
    parser.add_argument("--rule-texts", type=Path, default=DEFAULT_RULE_TEXTS_FILE,
                        help=f"default: {DEFAULT_RULE_TEXTS_FILE}")
    parser.add_argument("--claude-path", default=None,
                        help="Claude Code CLI executable; default: the config's cli_path "
                             "(Windows: %%USERPROFILE%%\\.local\\bin\\claude.exe)")
    parser.add_argument("--timeout-seconds", type=float, default=None,
                        help="model call timeout; default: the config's timeout_seconds")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and size the packet, call nothing, write nothing")
    return parser


def _finish(status: str, message: str | None, code: int) -> int:
    if message:
        print(message, file=sys.stderr)
    print(f"{STATUS_PREFIX}{status}")
    return code


def _under_root(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_rule_texts(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        rules = document["rules"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("unreadable_rule_texts_file") from None
    if not isinstance(rules, dict):
        raise ValueError("unreadable_rule_texts_file")
    return {str(key): str(value) for key, value in rules.items()}


def _relative(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def _safe_excerpt(text: str) -> str:
    try:
        validate_context_contains_no_secrets(text)
    except LivePilotCaptureError:
        return "[withheld: sensitive_pattern]"
    return text


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    as_of_text = args.as_of.isoformat()
    obs_path = args.scanner_dir / as_of_text / "observations.json"
    try:
        obs_bytes = obs_path.read_bytes()
        document = json.loads(obs_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _finish("error:observations_unreadable", f"Analiz notu: observations okunamadı ({obs_path})", 1)
    if not isinstance(document, dict) or document.get("schema") not in fmt.SUPPORTED_OBSERVATIONS_SCHEMAS:
        return _finish("error:observations_schema", f"Analiz notu: beklenmeyen şema ({obs_path})", 1)

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        return _finish("error:guard", f"Analiz notu: {exc}", 1)
    blocked = sorted(s for s in document.get("symbols", {}) if guards.is_excluded(s) or guards.is_holdout(s))
    if blocked:
        return _finish("refused:guard", "Analiz notu reddedildi: observations dosyasında holdout veya "
                       f"dışlanan hisse var ({','.join(blocked)}); model çağrılmadı", 1)

    if not fmt.has_positive_observations(document):
        return _finish("no_observations", "Analiz notu: condition_met gözlem yok; model çağrılmadı", 0)
    if latest_accepted(args.out_dir, as_of_text) is not None:
        return _finish("already_noted", "Analiz notu: bu as_of için doğrulanmış not zaten var; "
                       "model yeniden çağrılmadı", 0)

    try:
        config = load_config(_under_root(args.config), project_root=PROJECT_ROOT)
        rules = load_scanner_rules(_under_root(args.rules_file), project_root=PROJECT_ROOT)
        rule_texts = _load_rule_texts(_under_root(args.rule_texts))
    except (ConfigError, ScannerRulesError, ValueError) as exc:
        return _finish("error:config", f"Analiz notu: {exc}", 1)

    started = datetime.now(timezone.utc)
    obs_sha = hashlib.sha256(obs_bytes).hexdigest()
    try:
        build = build_packet(
            as_of=args.as_of, document=document, observations_sha256=obs_sha, rules=rules,
            rule_texts=rule_texts, project_root=PROJECT_ROOT, guards=guards,
            load_capture=lambda symbol: load_composed_capture(
                symbol, base_root=args.base_root, daily_root=args.daily_root, price_dir=args.price_dir),
            max_packet_bytes=config.max_packet_bytes, fallback_window_days=config.fallback_window_days,
        )
    except GuardRefusal as exc:
        return _finish("refused:guard", f"Analiz notu reddedildi: {exc}; model çağrılmadı", 1)
    except PacketError as exc:
        if exc.category == "packet_too_large":
            return _unavailable_without_packet(args, config, obs_path, obs_sha, started, exc)
        return _finish(f"error:{exc.category}", f"Analiz notu: girdi paketi kurulamadı ({exc})", 1)

    try:
        validate_context_contains_no_secrets(build.packet)
    except LivePilotCaptureError:
        return _finish("error:packet_sensitive_content",
                       "Analiz notu: girdi paketi hassas içerik denetimini geçemedi; model çağrılmadı", 1)

    packet_sha = hashlib.sha256(build.packet_bytes).hexdigest()
    print(f"Analiz notu girdi paketi: {len(build.packet_bytes)} bayt, sha256={packet_sha}, "
          f"hisse={','.join(build.included_symbols)}, gözlem={len(build.packet['required_assessments'])}"
          + (f", kapsam dışı={','.join(o['symbol'] for o in build.omitted)}" if build.omitted else ""),
          file=sys.stderr)
    if args.dry_run:
        return _finish("dry_run", "Analiz notu: --dry-run; model çağrılmadı, dosya yazılmadı", 0)

    cli = runner.resolve_cli(config.cli_path, args.claude_path)
    env = runner.child_env(config.strip_env)
    timeout = args.timeout_seconds or config.timeout_seconds
    version = runner.cli_version(cli, config.version_timeout_seconds, env)
    command = runner.build_command(
        cli, model=config.model, effort=config.effort, prompt_path=config.prompt_path,
        schema_text=config.schema_text,
    )
    stdin_text = runner.STDIN_HEADER + build.packet_bytes.decode("utf-8")

    attempt_dir, attempt = new_attempt_dir(args.out_dir, as_of_text)
    write_new(attempt_dir / "packet.json", build.packet_bytes)
    outcome = runner.run_claude(command, stdin_text=stdin_text, timeout=timeout, env=env)

    if not outcome.ok:
        validation, status = None, f"unavailable:{outcome.category}"
        message = f"analiz notu alınamadı ({outcome.category})"
    else:
        validation = (validate_note(outcome.note, build.packet) if outcome.note is not None
                      else Validation(False, "schema", ("response_not_json",)))
        status = "accepted" if validation.ok else f"rejected:{validation.category}"
        message = None if validation.ok else f"analiz notu doğrulamadan geçemedi ({validation.category})"

    raw_text = outcome.stdout
    try:
        validate_context_contains_no_secrets(raw_text)
    except LivePilotCaptureError:
        raw_text = "[withheld: sensitive_pattern]"
    if raw_text:
        write_new(attempt_dir / "response_raw.txt", raw_text.encode("utf-8"))
    if validation is not None and validation.ok:
        write_new(attempt_dir / "note.json", json_bytes(outcome.note))

    record = {
        "schema": RECORD_SCHEMA, "as_of": as_of_text, "attempt": attempt,
        "status": "accepted" if status == "accepted" else status.split(":")[0],
        "category": None if status == "accepted" else status.split(":", 1)[1],
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(outcome.duration_seconds, 3),
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "python_version": platform.python_version(), "platform": platform.platform(),
        "inputs": {
            "observations_path": obs_path.as_posix(), "observations_sha256": obs_sha,
            "packet_file": "packet.json", "packet_sha256": packet_sha,
            "packet_bytes": len(build.packet_bytes), "included_symbols": build.included_symbols,
            "omitted": list(build.omitted),
            "required_assessments": len(build.packet["required_assessments"]),
            "stdin_sha256": hashlib.sha256(stdin_text.encode("utf-8")).hexdigest(),
        },
        "config": {"path": config.path.name, "sha256": config.sha256},
        "prompt": {"path": config.prompt_path.name, "sha256": config.prompt_sha256},
        "output_schema": {"path": config.schema_path.name, "sha256": config.schema_sha256},
        "model_requested": config.model, "model_reported": outcome.model_reported,
        "effort": config.effort,
        "cli": {"version": version, "configured_path": config.cli_path,
                "removed_env": sorted(set(config.strip_env))},
        "command": runner.record_command(
            command, config.schema_sha256, _relative(config.prompt_path)),
        "response": {
            "returncode": outcome.returncode,
            "raw_file": "response_raw.txt" if raw_text else None,
            "raw_sha256": hashlib.sha256(outcome.stdout.encode("utf-8")).hexdigest() if outcome.stdout else None,
            "stderr_excerpt": _safe_excerpt(outcome.stderr_excerpt),
        },
        "validation": None if validation is None else {
            "ok": validation.ok, "category": validation.category, "details": list(validation.details),
        },
        "determinism_note": DETERMINISM_NOTE,
    }
    try:
        validate_context_contains_no_secrets(record)
    except LivePilotCaptureError:
        return _finish("error:record_sensitive_content",
                       "Analiz notu: kayıt hassas içerik denetimini geçemedi; kayıt yazılmadı", 1)
    write_new(attempt_dir / "record.json", json_bytes(record))

    if status == "accepted":
        return _finish("accepted", f"Analiz notu kaydedildi: {attempt_dir.as_posix()}", 0)
    return _finish(status, f"Analiz notu: {message}", 3)


def _unavailable_without_packet(args, config, obs_path: Path, obs_sha: str, started, exc: PacketError) -> int:
    """packet_too_large: the model is not called; a record documents it."""
    as_of_text = args.as_of.isoformat()
    if args.dry_run:
        return _finish("unavailable:packet_too_large", f"Analiz notu: {exc}", 3)
    attempt_dir, attempt = new_attempt_dir(args.out_dir, as_of_text)
    record = {
        "schema": RECORD_SCHEMA, "as_of": as_of_text, "attempt": attempt,
        "status": "unavailable", "category": "packet_too_large",
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "inputs": {"observations_path": obs_path.as_posix(), "observations_sha256": obs_sha,
                   "detail": str(exc)},
        "config": {"path": config.path.name, "sha256": config.sha256},
        "determinism_note": DETERMINISM_NOTE,
    }
    write_new(attempt_dir / "record.json", json_bytes(record))
    return _finish("unavailable:packet_too_large", "Analiz notu: analiz notu alınamadı (packet_too_large)", 3)


if __name__ == "__main__":
    sys.exit(main())
