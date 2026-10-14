"""CLI: daily analysis note (Claude Opus 5.5 through the Claude Code CLI).

Reads ``<scanner-dir>/<as-of>/observations.json``; for every symbol with a
``condition_met`` observation builds a raw-data packet (``fintr.llm_note
.packet``; nothing after ``as_of``, no price series, no returns) and makes
ONE model call per symbol, asking for a structured *evidence assessment* of
each frozen rule's direction (``destekliyor``/``zayıflatıyor``/``belirsiz``).
Each answer is validated fail-closed (``fintr.llm_note.validate``). Files go
to ``<out-dir>/<as_of>/<SYMBOL>/attempt-NNN/`` (no overwrite). See
``docs/notify.md``, ``docs/architecture.md`` and AGENTS.md (exception of
2026-10-02): the note never contains an order, target, stop, position,
return forecast or score, and a failure here never blocks the deterministic
notification.

A symbol whose packet exceeds ``max_symbol_packet_bytes`` is never truncated:
it gets "alınamadı (packet_too_large)". Symbols beyond ``max_symbols_per_run``
get "alınamadı (symbol_limit)". After a ``usage_limit``/``cli_not_found`` the
remaining symbols are recorded without a call (same category), and after
``max_run_seconds`` with ``run_time_budget``. A symbol that already has an
accepted note for this ``as_of`` is skipped, so a re-run only retries the rest.

Last stdout line: ``LLM_NOTE_STATUS: <status>``: ``ok=A,rejected=B,failed=C``
(+ ``,already=D`` when D symbols already had an accepted note; "rejected" =
"analiz notu doğrulamadan geçemedi", "failed" = "analiz notu alınamadı"),
``already_noted``, ``no_observations``, ``dry_run``, ``refused:guard`` or
``error:<category>``. Exit 0: all ok / already_noted / no_observations /
dry_run; 3: any rejected or failed; 1: error or guard refusal (nothing was
sent to the model).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fintr.evaluation.live_capture import LivePilotCaptureError, validate_context_contains_no_secrets
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.llm_note import runner
from fintr.llm_note.config import ConfigError, load_config
from fintr.llm_note.loader import load_composed_capture
from fintr.llm_note.packet import GuardRefusal, PacketError, SymbolOutcome, build_symbol_packets, positive_symbols
from fintr.llm_note.record import (
    DETERMINISM_NOTE, RECORD_SCHEMA, json_bytes, latest_accepted, new_attempt_dir, symbol_dir, write_new,
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
STOP_CATEGORIES = ("usage_limit", "cli_not_found")  # persistent: later calls would fail the same way


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily analysis note: evidence assessment of the frozen rules' direction for "
                    "condition_met observations, one model call per symbol, by Claude Opus 5.5 "
                    "through the Claude Code CLI (subscription, no API key). Fail-closed "
                    "validation; never blocks the deterministic notification."
    )
    parser.add_argument("--as-of", type=_date, default=None,
                        help="reads <scanner-dir>/<as-of>/observations.json (required unless "
                             "--probe-isolation)")
    parser.add_argument("--probe-isolation", action="store_true",
                        help="ask the model ONE fixed yes/no question (does its context contain "
                             "user/project instructions, hooks, MCP, skills or plugins?) and record "
                             "the answer under <out-dir>/_probes/; reads no data, sends no data. "
                             "Exit 0 only if the answer is 'hayır' (isolated)")
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
                        help="timeout of each model call; default: the config's timeout_seconds")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and size the per-symbol packets, call nothing, write nothing")
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


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


_ISTANBUL = timezone(timedelta(hours=3))  # Europe/Istanbul is fixed UTC+3 since 2016


def generation_provenance(as_of: date, now_utc: datetime) -> dict:
    """Backfill marker (Efe kararı 2026-10-14): ``generated_late`` is true when the generation
    date (Europe/Istanbul) is more than 1 calendar day after ``as_of`` + 1. Such notes were
    written after the fact and must never feed any study, holdout test or model/prompt choice."""
    local_day = now_utc.astimezone(_ISTANBUL).date()
    return {"generated_on_local": local_day.isoformat(),
            "generated_late": local_day > as_of + timedelta(days=2)}


def _size_summary(outcomes: list[SymbolOutcome]) -> str:
    sizes = [o.size for o in outcomes if o.size]
    if not sizes:
        return "paket boyutu yok"
    return f"en büyük {max(sizes)} bayt, ortanca {int(statistics.median(sizes))} bayt"


class _Run:
    """What is shared by the per-symbol calls of one run."""

    def __init__(self, args, config, *, obs_path: Path, obs_sha: str, cli: str, env: dict,
                 version: str, timeout: float) -> None:
        self.args, self.config = args, config
        self.obs_path, self.obs_sha = obs_path, obs_sha
        self.cli, self.env, self.version, self.timeout = cli, env, version, timeout
        self.started = time.monotonic()
        self.started_utc = datetime.now(timezone.utc)
        self.stop_category: str | None = None

    def base_record(self, symbol: str, attempt: int, status: str, category: str | None,
                    *, inputs: dict) -> dict:
        config = self.config
        return {
            "schema": RECORD_SCHEMA, "as_of": self.args.as_of.isoformat(), "symbol": symbol,
            "attempt": attempt, "status": status, "category": category,
            "started_at_utc": _iso(datetime.now(timezone.utc)),
            "code_commit": resolve_code_commit(PROJECT_ROOT),
            "python_version": platform.python_version(), "platform": platform.platform(),
            "inputs": {"observations_path": self.obs_path.as_posix(),
                       "observations_sha256": self.obs_sha, **inputs},
            "config": {"path": config.path.name, "sha256": config.sha256},
            "prompt": {"path": config.prompt_path.name, "sha256": config.prompt_sha256},
            "output_schema": {"path": config.schema_path.name, "sha256": config.schema_sha256},
            "model_requested": config.model, "effort": config.effort,
            "determinism_note": DETERMINISM_NOTE,
            **generation_provenance(self.args.as_of, datetime.now(timezone.utc)),
        }

    def not_called(self, symbol: str, category: str, *, inputs: dict, detail: str | None = None) -> tuple[str, str]:
        """Record a symbol that gets no model call; status ``unavailable``."""
        parent = symbol_dir(self.args.out_dir, self.args.as_of.isoformat(), symbol)
        attempt_dir, attempt = new_attempt_dir(parent)
        record = self.base_record(symbol, attempt, "unavailable", category, inputs=inputs)
        record["called"] = False
        if detail:
            record["detail"] = detail
        write_new(attempt_dir / "record.json", json_bytes(record))
        return "failed", category

    def call(self, build) -> tuple[str, str | None]:
        """One model call for one symbol; returns (``ok``/``rejected``/``failed``, category)."""
        args, config = self.args, self.config
        as_of_text = args.as_of.isoformat()
        symbol = build.symbol
        packet_sha = hashlib.sha256(build.packet_bytes).hexdigest()
        stdin_text = runner.STDIN_HEADER + build.packet_bytes.decode("utf-8")
        command = runner.build_command(
            self.cli, model=config.model, effort=config.effort, prompt_path=config.prompt_path,
            schema_text=config.schema_text, setting_sources=config.setting_sources,
        )
        parent = symbol_dir(args.out_dir, as_of_text, symbol)
        attempt_dir, attempt = new_attempt_dir(parent)
        write_new(attempt_dir / "packet.json", build.packet_bytes)
        outcome = runner.run_claude(command, stdin_text=stdin_text, timeout=self.timeout, env=self.env)
        if outcome.category in STOP_CATEGORIES:
            self.stop_category = outcome.category

        if not outcome.ok:
            validation, status, category = None, "unavailable", outcome.category
        else:
            validation = (validate_note(outcome.note, build.packet) if outcome.note is not None
                          else Validation(False, "schema", ("response_not_json",)))
            status = "accepted" if validation.ok else "rejected"
            category = None if validation.ok else validation.category

        raw_text = outcome.stdout
        try:
            validate_context_contains_no_secrets(raw_text)
        except LivePilotCaptureError:
            raw_text = "[withheld: sensitive_pattern]"
        if raw_text:
            write_new(attempt_dir / "response_raw.txt", raw_text.encode("utf-8"))
        if validation is not None and validation.ok:
            write_new(attempt_dir / "note.json", json_bytes(outcome.note))

        record = self.base_record(symbol, attempt, status, category, inputs={
            "packet_file": "packet.json", "packet_sha256": packet_sha,
            "packet_bytes": len(build.packet_bytes),
            "required_assessments": len(build.packet["required_assessments"]),
            "stdin_sha256": hashlib.sha256(stdin_text.encode("utf-8")).hexdigest(),
        })
        record.update({
            "called": True, "duration_seconds": round(outcome.duration_seconds, 3),
            "usage": outcome.usage, "model_reported": outcome.model_reported,
            "cli": {"version": self.version, "configured_path": config.cli_path,
                    "removed_env": sorted(set(config.strip_env))},
            "command": runner.record_command(command, config.schema_sha256, _relative(config.prompt_path)),
            "response": {
                "returncode": outcome.returncode,
                "raw_file": "response_raw.txt" if raw_text else None,
                "raw_sha256": hashlib.sha256(outcome.stdout.encode("utf-8")).hexdigest() if outcome.stdout else None,
                "stderr_excerpt": _safe_excerpt(outcome.stderr_excerpt),
            },
            "validation": None if validation is None else {
                "ok": validation.ok, "category": validation.category, "details": list(validation.details),
            },
        })
        try:
            validate_context_contains_no_secrets(record)
        except LivePilotCaptureError:
            return "failed", "record_sensitive_content"
        write_new(attempt_dir / "record.json", json_bytes(record))
        if status == "accepted":
            return "ok", None
        return ("rejected" if status == "rejected" else "failed"), category

    def handle(self, outcome: SymbolOutcome) -> tuple[str, str | None]:
        symbol = outcome.symbol
        if outcome.error is not None:
            inputs = {"detail": str(outcome.error)}
            if outcome.size:
                inputs["packet_bytes"] = outcome.size
            return self.not_called(symbol, outcome.error.category, inputs=inputs)
        build = outcome.build
        try:
            validate_context_contains_no_secrets(build.packet)
        except LivePilotCaptureError:
            return self.not_called(symbol, "packet_sensitive_content", inputs={})
        if self.stop_category is not None:
            return self.not_called(symbol, self.stop_category, inputs={},
                                   detail=f"not called: an earlier call of this run ended with {self.stop_category}")
        if time.monotonic() - self.started > self.config.max_run_seconds:
            return self.not_called(symbol, "run_time_budget", inputs={},
                                   detail=f"not called: max_run_seconds={self.config.max_run_seconds} exceeded")
        return self.call(build)


def run_probe(args, config, cli: str, env: dict, version: str) -> str:
    """The isolation probe: one fixed canary question, no data. Returns
    ``clean`` (YOK), ``not_isolated`` (a config canary named), ``unclear`` or
    ``unavailable:<category>``; the answer is recorded under
    ``<out-dir>/_probes/attempt-NNN/record.json`` (no overwrite)."""
    command = runner.build_command(
        cli, model=config.model, effort=config.effort, prompt_path=config.probe_prompt_path,
        schema_text=None, setting_sources=config.setting_sources,
    )
    started = datetime.now(timezone.utc)
    outcome = runner.run_claude(command, stdin_text=runner.PROBE_STDIN,
                                timeout=args.timeout_seconds or config.timeout_seconds, env=env)
    result = (f"unavailable:{outcome.category}" if not outcome.ok
              else runner.classify_probe_answer(outcome.result_text, config.probe_canaries))
    attempt_dir, attempt = new_attempt_dir(Path(args.out_dir) / "_probes")
    record = {
        "schema": "fintr_llm_note_probe_v1", "attempt": attempt, "result": result,
        "answer_text": _safe_excerpt(outcome.result_text),
        "question": runner.PROBE_STDIN, "started_at_utc": _iso(started),
        "duration_seconds": round(outcome.duration_seconds, 3), "usage": outcome.usage,
        "model_requested": config.model, "model_reported": outcome.model_reported,
        "setting_sources": config.setting_sources,
        "canaries": {"count": len(config.probe_canaries), "sha256": config.probe_canaries_sha256},
        "classification": result,
        "probe_prompt": {"path": config.probe_prompt_path.name, "sha256": config.probe_prompt_sha256},
        "config": {"path": config.path.name, "sha256": config.sha256},
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "cli": {"version": version, "configured_path": config.cli_path},
        "command": runner.record_command(command, config.schema_sha256, _relative(config.probe_prompt_path)),
        "response": {"returncode": outcome.returncode, "stderr_excerpt": _safe_excerpt(outcome.stderr_excerpt)},
    }
    try:
        validate_context_contains_no_secrets(record)
    except LivePilotCaptureError:
        record["answer_text"] = "[withheld: sensitive_pattern]"
        record["response"]["stderr_excerpt"] = "[withheld: sensitive_pattern]"
    write_new(attempt_dir / "record.json", json_bytes(record))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.probe_isolation:
        try:
            config = load_config(_under_root(args.config), project_root=PROJECT_ROOT)
        except ConfigError as exc:
            return _finish("error:config", f"Analiz notu: {exc}", 1)
        cli = runner.resolve_cli(config.cli_path, args.claude_path)
        env = runner.child_env(config.strip_env)
        result = run_probe(args, config, cli, env,
                           runner.cli_version(cli, config.version_timeout_seconds, env))
        return _finish(f"probe:{result}",
                       f"Analiz notu izolasyon sondası: {result} (kayıt: {(args.out_dir / '_probes').as_posix()})",
                       0 if result == "clean" else 3)
    if args.as_of is None:
        parser.error("--as-of is required (unless --probe-isolation)")
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
    try:
        positives = positive_symbols(document)
        pending = [s for s in positives if latest_accepted(symbol_dir(args.out_dir, as_of_text, s)) is None]
    except ValueError as exc:
        return _finish("error:bad_symbol", f"Analiz notu: {exc}", 1)
    already = len(positives) - len(pending)
    if not pending:
        return _finish("already_noted", "Analiz notu: tüm hisseler için doğrulanmış not zaten var; "
                       "model yeniden çağrılmadı", 0)

    try:
        config = load_config(_under_root(args.config), project_root=PROJECT_ROOT)
        rules = load_scanner_rules(_under_root(args.rules_file), project_root=PROJECT_ROOT)
        rule_texts = _load_rule_texts(_under_root(args.rule_texts))
    except (ConfigError, ScannerRulesError, ValueError) as exc:
        return _finish("error:config", f"Analiz notu: {exc}", 1)

    obs_sha = hashlib.sha256(obs_bytes).hexdigest()
    try:
        outcomes = build_symbol_packets(
            as_of=args.as_of, document=document, observations_sha256=obs_sha, rules=rules,
            rule_texts=rule_texts, project_root=PROJECT_ROOT, guards=guards,
            load_capture=lambda symbol: load_composed_capture(
                symbol, base_root=args.base_root, daily_root=args.daily_root, price_dir=args.price_dir,
                as_of=args.as_of),
            max_symbol_packet_bytes=config.max_symbol_packet_bytes,
            max_symbols=config.max_symbols_per_run, fallback_window_days=config.fallback_window_days,
            only_symbols=set(pending),
        )
    except GuardRefusal as exc:
        return _finish("refused:guard", f"Analiz notu reddedildi: {exc}; model çağrılmadı", 1)
    except PacketError as exc:
        return _finish(f"error:{exc.category}", f"Analiz notu: girdi paketi kurulamadı ({exc})", 1)

    for outcome in outcomes:
        if outcome.build is not None:
            print(f"Analiz notu girdi paketi {outcome.symbol}: {outcome.size} bayt, "
                  f"sha256={hashlib.sha256(outcome.build.packet_bytes).hexdigest()}, "
                  f"gözlem={len(outcome.build.packet['required_assessments'])}", file=sys.stderr)
        else:
            print(f"Analiz notu girdi paketi {outcome.symbol}: kurulamadı ({outcome.error.category})",
                  file=sys.stderr)
    print(f"Analiz notu: {len(outcomes)} hisse, {_size_summary(outcomes)}"
          + (f", zaten notlu {already}" if already else ""), file=sys.stderr)
    if args.dry_run:
        return _finish("dry_run", "Analiz notu: --dry-run; model çağrılmadı, dosya yazılmadı", 0)

    cli = runner.resolve_cli(config.cli_path, args.claude_path)
    env = runner.child_env(config.strip_env)
    version = runner.cli_version(cli, config.version_timeout_seconds, env)
    if config.isolation_probe:  # fail-closed: no data leaves unless the child reports no extra context
        probe = run_probe(args, config, cli, env, version)
        if probe != "clean":
            return _finish(f"error:isolation_probe:{probe}",
                           f"Analiz notu: izolasyon sondası '{probe}'; model verisi gönderilmedi", 1)
    run = _Run(args, config, obs_path=obs_path, obs_sha=obs_sha, cli=cli, env=env, version=version,
               timeout=args.timeout_seconds or config.timeout_seconds)
    counts = {"ok": 0, "rejected": 0, "failed": 0}
    for outcome in outcomes:
        result, category = run.handle(outcome)
        counts[result] += 1
        if result == "ok":
            print(f"Analiz notu {outcome.symbol}: kaydedildi", file=sys.stderr)
        elif result == "rejected":
            print(f"Analiz notu {outcome.symbol}: analiz notu doğrulamadan geçemedi ({category})", file=sys.stderr)
        else:
            print(f"Analiz notu {outcome.symbol}: analiz notu alınamadı ({category})", file=sys.stderr)

    status = f"ok={counts['ok']},rejected={counts['rejected']},failed={counts['failed']}"
    if already:
        status += f",already={already}"
    print(f"Analiz notu kayıtları: {(args.out_dir / as_of_text).as_posix()}", file=sys.stderr)
    return _finish(status, None, 0 if not (counts["rejected"] or counts["failed"]) else 3)


if __name__ == "__main__":
    sys.exit(main())
