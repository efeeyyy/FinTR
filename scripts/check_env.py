"""CLI: one-command environment check before Efe's morning daily run.

Prints one line per item (``OK`` / ``UYARI`` / ``HATA``) and a summary; exit
code 1 when any item is ``HATA``, else 0. It opens no network connection,
sends nothing, runs no subprocess (the Claude CLI is only looked up, never
executed), writes no file and prompts for nothing. ``FINTR_TELEGRAM_*``
variables are reported only as present/absent -- neither value nor length is
read into any output. Run records, scanner outputs and receipts are reported
by date, fixed status and counts only; no financial content is printed.

The deterministic hash checks reuse the repository's own loaders
(``load_active_guards``, ``load_scanner_rules``, ``load_study_spec``) and
compare against the values recorded in ``docs/prereg/FREEZE.md`` /
``docs/prereg/DECISIONS.md`` and in the frozen specs. The "which window
end E would the pipeline target" line uses the same pure planner as
``collect_daily.py --dry-run`` (``plan_daily_collection``), offline. This
script does not consult the BIST holiday calendar
(``configs/market/bist_holidays.json``; the pipeline's no-new-data guard does),
so whether E is a holiday is not checked here (the line says so); only weekends
and an already-collected E are recognised as "no new data expected".

See ``docs/live_check_runbook.md`` ("Sabah: önce check_env.py").
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Mapping, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.dont_write_bytecode = True  # importing fintr modules must not write __pycache__

PROJECT_ROOT = Path(__file__).resolve().parents[1]

OK, WARN, ERR = "OK", "UYARI", "HATA"

TELEGRAM_ENV = ("FINTR_TELEGRAM_BOT_TOKEN", "FINTR_TELEGRAM_CHAT_ID")
DEFAULT_COLLECTION_CONFIG = Path("configs/collection/daily_miniapp_v1.json")
LLM_NOTE_CONFIG = Path("configs/llm_note/config_v1.json")
DELIVERY_CONFIG = Path("configs/notify/delivery_v1.json")
SCANNER_RULES = Path("configs/scanner/rules_v1.json")
FREEZE_DOC = Path("docs/prereg/FREEZE.md")
DECISIONS_DOC = Path("docs/prereg/DECISIONS.md")
PREREG_DIR = Path("configs/studies/prereg")
TEST_PLAN = PREREG_DIR / "test_plan_v1.json"
SCANNER_DIR = Path("data/interim/scanner_daily")
# Mirrors run_daily_pipeline.NOTIFY_DIR (a test pins the equality; the pipeline
# script is not imported here). Receipts live here, not under scanner_daily.
NOTIFY_DIR = Path("data/interim/notify")
RUNS_SUBDIR = "_runs"

# Mirrors fintr.collection.planner.RUN_RECORD_SCHEMA (a test pins the equality;
# the planner is not imported here so a missing `websockets` cannot hide it).
RUN_RECORD_SCHEMA = "fintr_daily_collection_run_v1"

ISTANBUL_FIXED_OFFSET_HOURS = 3  # Türkiye: UTC+3 year-round since 2016, no DST.
DISK_WARN_BYTES = 1 << 30  # 1 GiB
DISK_ERROR_BYTES = 100 << 20  # 100 MiB
WEEKDAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_:\-]{1,40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FREEZE_RAW_ROW = re.compile(
    r"^\|\s*`([^`]+\.json)`\s*\|\s*`(prereg_[^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE,
)
_FREEZE_CANONICAL_ROW = re.compile(
    r"^\|\s*`([^`]+\.json)`\s*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE,
)
_DECISIONS_TEST_PLAN = re.compile(
    r"`configs/studies/prereg/test_plan_v1\.json`[^\n]*?SHA-256\s*`([0-9a-f]{64})`",
)


class Result(NamedTuple):
    level: str
    label: str
    detail: str


# --- small helpers ---------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_version(text: str) -> tuple[int, ...] | None:
    match = re.match(r"\d+(?:\.\d+)*", text.strip())
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return (left > right) - (left < right)


def _satisfies(version: str, specifier: str) -> bool | None:
    """Whether ``version`` meets a comma-separated ``>=,<=,>,<,==,!=`` specifier.

    ``None`` when a clause cannot be evaluated (the caller reports "not
    verified" rather than a failure).
    """
    current = _parse_version(version)
    if current is None:
        return None
    for clause in (part.strip() for part in specifier.split(",") if part.strip()):
        match = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)", clause)
        if match is None:
            return None
        operator, bound = match.group(1), tuple(int(p) for p in match.group(2).split("."))
        order = _compare(current, bound)
        passed = {
            ">=": order >= 0, "<=": order <= 0, ">": order > 0, "<": order < 0,
            "==": order == 0, "!=": order != 0,
        }[operator]
        if not passed:
            return False
    return True


def _istanbul_today(*, now: datetime | None = None) -> date:
    """Run day R: the Europe/Istanbul calendar date (same rule as collect_daily.py)."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        tzinfo = ZoneInfo("Europe/Istanbul")
    except ZoneInfoNotFoundError:
        tzinfo = timezone(timedelta(hours=ISTANBUL_FIXED_OFFSET_HOURS))
    return now.astimezone(tzinfo).date()


def _zoneinfo_available() -> bool:
    try:
        ZoneInfo("Europe/Istanbul")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _read_json(path: Path) -> dict | None:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _iso_date_or_none(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --- checks: python and packages ------------------------------------------


def _load_pyproject(root: Path) -> dict | None:
    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def check_python(pyproject: dict | None, *, version: str | None = None) -> Result:
    version = version or ".".join(str(part) for part in sys.version_info[:3])
    if pyproject is None:
        return Result(ERR, "python", f"{version}; pyproject.toml okunamadı")
    required = str(pyproject.get("project", {}).get("requires-python", ""))
    verdict = _satisfies(version, required) if required else None
    if verdict is True:
        return Result(OK, "python", f"{version} (gerekli: {required})")
    if verdict is False:
        return Result(ERR, "python", f"{version}; gerekli: {required}")
    return Result(WARN, "python", f"{version}; gerekli sürüm ({required or 'yok'}) doğrulanamadı")


def check_packages(pyproject: dict | None) -> list[Result]:
    if pyproject is None:
        return [Result(ERR, "bağımlılıklar", "pyproject.toml okunamadı")]
    declared = pyproject.get("project", {}).get("dependencies", [])
    if not isinstance(declared, list) or not declared:
        return [Result(ERR, "bağımlılıklar", "pyproject.toml'da dependencies yok")]
    results: list[Result] = []
    for entry in declared:
        match = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*([^;]*)", str(entry))
        if match is None:
            results.append(Result(ERR, "paket", "bağımlılık satırı çözülemedi"))
            continue
        name, specifier = match.group(1), match.group(2).strip()
        label = f"paket {name}"
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            if name.lower() == "tzdata":
                results.append(_missing_tzdata_result(label))
            else:
                wanted = specifier or "herhangi"
                results.append(Result(ERR, label, f"kurulu değil (gerekli: {wanted})"))
            continue
        verdict = _satisfies(installed, specifier) if specifier else True
        if verdict is True:
            results.append(Result(OK, label, f"{installed} ({specifier or 'sürüm kısıtı yok'})"))
        elif verdict is False:
            results.append(Result(ERR, label, f"{installed}; gerekli: {specifier}"))
        else:
            results.append(
                Result(WARN, label, f"{installed}; sürüm kısıtı ({specifier}) doğrulanamadı")
            )
    return results


def _missing_tzdata_result(label: str) -> Result:
    if _zoneinfo_available():
        return Result(WARN, label, (
            "kurulu değil; sistem tz veritabanı Europe/Istanbul'u okuyor, ancak Windows'ta "
            "bulunmaz (pip install tzdata); yoksa UTC+3 sabit yedeği kullanılır"
        ))
    return Result(WARN, label, (
        "kurulu değil; Europe/Istanbul için UTC+3 sabit yedeği kullanılacak "
        "(Türkiye 2016'dan beri UTC+3, doğru sonuç; yine de pip install tzdata önerilir)"
    ))


# --- checks: environment and tools ----------------------------------------


def check_telegram_env(env: Mapping[str, str]) -> list[Result]:
    """Present/absent only; the value (and its length) is never read into output."""
    results = []
    for name in TELEGRAM_ENV:
        present = bool((env.get(name) or "").strip())
        if present:
            results.append(Result(OK, f"ortam {name}", "var"))
        else:
            results.append(
                Result(WARN, f"ortam {name}", "yok (--send gerçek gönderim için gerekir)")
            )
    return results


def _resolve_cli(template: str) -> tuple[str, bool]:
    """(resolved path or name, exists). Lookup only; the CLI is never executed."""
    expanded = os.path.expandvars(os.path.expanduser(template))
    if os.sep in expanded or "/" in expanded or "\\" in expanded:
        return expanded, Path(expanded).is_file()
    found = shutil.which(expanded)
    return (found or expanded), found is not None


def check_claude_cli(root: Path) -> Result:
    document = _read_json(root / LLM_NOTE_CONFIG)
    cli_path = document.get("cli_path") if document else None
    if not isinstance(cli_path, dict):
        return Result(
            ERR, "Claude CLI", f"{LLM_NOTE_CONFIG.as_posix()} okunamadı veya cli_path yok",
        )
    template = cli_path.get("windows") if os.name == "nt" else cli_path.get("default")
    if not isinstance(template, str) or not template:
        return Result(ERR, "Claude CLI", "cli_path içinde bu işletim sistemi için yol yok")
    resolved, exists = _resolve_cli(template)
    if exists:
        return Result(OK, "Claude CLI", f"bulundu: {resolved} (çalıştırılmadı)")
    return Result(WARN, "Claude CLI", (
        f"bulunamadı: {resolved} (yalnız --llm-note adımı için gerekir; çalıştırılmadı)"
    ))


# --- checks: configs and hashes --------------------------------------------


def _guarded(label: str, func) -> Result | list[Result]:
    """Run one check; an import problem or unexpected error becomes one line."""
    try:
        return func()
    except ImportError as exc:
        return Result(WARN, label, f"atlandı: modül yüklenemedi ({type(exc).__name__})")
    except Exception as exc:  # one broken check must not hide the rest of the report
        return Result(ERR, label, f"beklenmeyen hata ({type(exc).__name__})")


def check_collection_config(root: Path, config_path: Path) -> tuple[Result, object | None]:
    from fintr.collection.planner import CollectionPlannerError, load_daily_collection_config

    try:
        config = load_daily_collection_config(root / config_path)
    except CollectionPlannerError as exc:
        return Result(ERR, "config toplama", f"{config_path.as_posix()}: {exc}"), None
    return Result(OK, "config toplama", f"{config_path.as_posix()} okundu"), config


def check_active_guards():
    """(split result, exclusions result, ActiveGuards | None) via the shared loader."""
    from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        return Result(ERR, "split/exclusions", str(exc)), None, None
    split = Result(OK, "split", (
        f"{guards.split.split_id}: {len(guards.split.development_symbols)} geliştirme + "
        f"{len(guards.split.holdout_symbols)} holdout sembol, "
        f"sha256={guards.split.split_sha256[:12]}…"
    ))
    exclusions = Result(OK, "exclusions", (
        f"{len(guards.exclusions.entries)} dışlanan sembol, "
        f"sha256={str(guards.exclusions.file_sha256)[:12]}… "
        "(kayıtlı hash yok; yalnız okunabilirlik)"
    ))
    return split, exclusions, guards


def check_freeze_hashes(root: Path) -> Result:
    """Frozen spec files vs the raw and canonical hashes recorded in FREEZE.md."""
    from fintr.research.study_spec import StudySpecError, load_study_spec

    label = "spec hash (FREEZE.md)"
    try:
        freeze = (root / FREEZE_DOC).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Result(ERR, label, f"{FREEZE_DOC.as_posix()} okunamadı")
    raw_recorded = {name: sha for name, _spec_id, sha in _FREEZE_RAW_ROW.findall(freeze)}
    canonical_recorded = dict(_FREEZE_CANONICAL_ROW.findall(freeze))
    if not raw_recorded or set(raw_recorded) != set(canonical_recorded):
        return Result(ERR, label, "FREEZE.md tablolarında kayıtlı spec hash'i bulunamadı/tutarsız")
    problems: list[str] = []
    for name in sorted(raw_recorded):
        path = root / PREREG_DIR / name
        try:
            actual_raw = _sha256_file(path)
        except OSError:
            problems.append(f"{name}: dosya okunamadı")
            continue
        if actual_raw != raw_recorded[name]:
            problems.append(f"{name}: ham sha256 kayıtlıdan farklı")
        try:
            actual_canonical = load_study_spec(path).spec_sha256
        except (StudySpecError, OSError):
            problems.append(f"{name}: spec yüklenemedi")
            continue
        if actual_canonical != canonical_recorded[name]:
            problems.append(f"{name}: kanonik sha256 kayıtlıdan farklı")
    if problems:
        return Result(ERR, label, "; ".join(problems))
    return Result(
        OK, label, f"{len(raw_recorded)} spec: ham ve kanonik hash'ler FREEZE.md ile eşleşiyor",
    )


def check_scanner_rules(root: Path) -> Result:
    from fintr.scanner.rules import ScannerRulesError, load_scanner_rules

    label = "tarayıcı kuralları"
    try:
        rules = load_scanner_rules(root / SCANNER_RULES, project_root=root)
    except ScannerRulesError as exc:
        return Result(ERR, label, str(exc))
    return Result(OK, label, (
        f"{len(rules)} kural yüklendi; spec hash'leri {SCANNER_RULES.as_posix()} ile eşleşiyor"
    ))


def check_split_hash_in_specs(root: Path, split_sha256: str | None) -> Result:
    """The active split's hash vs ``preregistration.split_file_sha256`` of every frozen spec."""
    label = "split hash (spec kaydı)"
    paths = sorted((root / PREREG_DIR).glob("h*.json"))
    if not paths or split_sha256 is None:
        return Result(ERR, label, "spec dosyası bulunamadı veya aktif split yüklenemedi")
    mismatched = []
    for path in paths:
        document = _read_json(path) or {}
        prereg = document.get("preregistration")
        recorded = prereg.get("split_file_sha256") if isinstance(prereg, dict) else None
        if recorded != split_sha256:
            mismatched.append(path.name)
    if mismatched:
        return Result(
            ERR, label, "aktif split hash'i spec kaydından farklı: " + ", ".join(mismatched),
        )
    return Result(OK, label, (
        f"aktif split sha256, {len(paths)} dondurulmuş spec'te kayıtlı değerle eşleşiyor"
    ))


def check_test_plan_hash(root: Path) -> Result:
    label = "test planı hash (DECISIONS.md)"
    try:
        decisions = (root / DECISIONS_DOC).read_text(encoding="utf-8")
        actual = _sha256_file(root / TEST_PLAN)
    except (OSError, UnicodeDecodeError):
        return Result(
            ERR, label, f"{DECISIONS_DOC.as_posix()} veya {TEST_PLAN.as_posix()} okunamadı",
        )
    match = _DECISIONS_TEST_PLAN.search(decisions)
    if match is None:
        return Result(ERR, label, "DECISIONS.md'de kayıtlı test planı hash'i bulunamadı")
    if match.group(1) != actual:
        return Result(ERR, label, f"{TEST_PLAN.as_posix()} sha256'sı kayıtlıdan farklı")
    return Result(OK, label, f"{TEST_PLAN.as_posix()} sha256'sı kayıtla eşleşiyor")


def check_other_configs(root: Path) -> list[Result]:
    """Readability of the LLM-note and notify-delivery configs (loaders reused)."""
    results: list[Result] = []

    def llm_note() -> Result:
        from fintr.llm_note.config import ConfigError, load_config

        try:
            load_config(root / LLM_NOTE_CONFIG, project_root=root)
        except ConfigError as exc:
            return Result(ERR, "config analiz notu", f"{LLM_NOTE_CONFIG.as_posix()}: {exc}")
        return Result(OK, "config analiz notu", f"{LLM_NOTE_CONFIG.as_posix()} okundu")

    def delivery() -> Result:
        from fintr.notify.delivery import DeliveryConfigError, load_delivery_config

        try:
            load_delivery_config(root / DELIVERY_CONFIG)
        except DeliveryConfigError as exc:
            return Result(ERR, "config bildirim", str(exc))
        return Result(OK, "config bildirim", f"{DELIVERY_CONFIG.as_posix()} okundu")

    results.append(_guarded("config analiz notu", llm_note))
    results.append(_guarded("config bildirim", delivery))
    return results


# --- checks: data/interim state ---------------------------------------------


def _capture_root(root: Path, config: object | None) -> Path:
    relative = getattr(config, "capture_root", None) or "data/interim/daily_capture"
    return root / relative


def check_last_run(runs_dir: Path) -> Result:
    label = "son günlük çalıştırma"
    if not runs_dir.is_dir():
        return Result(WARN, label, "kayıt dizini yok (henüz günlük çalıştırma yapılmamış)")
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        document = _read_json(path)
        if document is None or document.get("schema") != RUN_RECORD_SCHEMA:
            continue
        run_date = _iso_date_or_none(document.get("run_date_local"))
        status = document.get("status")
        symbols = document.get("symbols")
        if run_date is None or not isinstance(status, str) or not isinstance(symbols, list):
            continue
        status = status if _SAFE_TOKEN.fullmatch(status) else "bilinmiyor"
        entries = [entry for entry in symbols if isinstance(entry, dict)]
        saved = sum(1 for entry in entries if entry.get("status") == "saved")
        failed = sum(1 for entry in entries if str(entry.get("status", "")).startswith("failed"))
        detail = (
            f"tarih={run_date.isoformat()}, durum={status}, saved={saved}, failed={failed}, "
            f"toplam={len(entries)}"
        )
        level = OK if status == "completed" and failed == 0 else WARN
        return Result(level, label, detail)
    return Result(WARN, label, "geçerli çalıştırma kaydı bulunamadı")


def _latest_dated_dir(parent: Path, *, require: str | None = None) -> tuple[date, Path] | None:
    if not parent.is_dir():
        return None
    found: list[tuple[date, Path]] = []
    for path in parent.iterdir():
        as_of = _iso_date_or_none(path.name)
        if as_of is not None and path.is_dir() and (require is None or any(path.glob(require))):
            found.append((as_of, path))
    return max(found, key=lambda item: item[0]) if found else None


def check_last_scanner(scanner_dir: Path) -> tuple[Result, date | None]:
    label = "son scanner_daily as_of"
    latest = _latest_dated_dir(scanner_dir, require="observations.json")
    if latest is None:
        return Result(WARN, label, "yok (henüz taranmış gözlem dosyası yok)"), None
    return Result(OK, label, latest[0].isoformat()), latest[0]


def _session_state(session: object) -> tuple[str, bool]:
    """Fixed-format state text of a receipt session and whether it is complete."""
    total = len(session.message_sha256)
    if session.legacy:
        return "tamamlandı (eski biçim)", True
    if session.complete:
        return f"tamamlandı ({len(session.sent)}/{total} mesaj)", True
    return f"KISMİ ({len(session.sent)}/{total} mesaj gönderildi)", False


def _unsent_scan_text(scanner_as_of: date | None, receipt_as_of: date | None) -> str | None:
    if scanner_as_of is None or (receipt_as_of is not None and scanner_as_of <= receipt_as_of):
        return None
    return f"gönderilmemiş tarama var (son scanner as_of={scanner_as_of.isoformat()})"


def check_last_receipt(notify_dir: Path, scanner_as_of: date | None) -> Result:
    """Latest deterministic notify receipt (``receipt*.json``; note receipts excluded)."""
    from fintr.notify.delivery import find_latest_session

    label = "son notify receipt"
    latest = _latest_dated_dir(notify_dir, require="receipt*.json")
    if latest is None:
        unsent = _unsent_scan_text(scanner_as_of, None)
        if unsent:
            return Result(WARN, label, f"yok (henüz gönderim kaydı yok); {unsent}")
        return Result(WARN, label, "yok (henüz gönderim kaydı yok)")
    as_of, directory = latest
    session = find_latest_session(directory)
    if session is None:
        return Result(WARN, label, f"as_of={as_of.isoformat()}: receipt okunamadı")
    state, complete = _session_state(session)
    detail = f"as_of={as_of.isoformat()}, {state}"
    unsent = _unsent_scan_text(scanner_as_of, as_of)
    if unsent:
        return Result(WARN, label, f"{detail}; {unsent}")
    return Result(OK if complete else WARN, label, detail)


def check_last_note_receipt(notify_dir: Path) -> Result | None:
    """Note receipt (``note_receipt*.json``) of the same as_of directory as the
    latest deterministic receipt; None when there is none (nothing to show)."""
    from fintr.notify.delivery import NOTE_RECEIPTS, find_latest_session

    label = "son notify not receipt'i"
    latest = _latest_dated_dir(notify_dir, require="receipt*.json")
    if latest is None:
        return None
    as_of, directory = latest
    if not any(directory.glob("note_receipt*.json")):
        return None
    session = find_latest_session(directory, NOTE_RECEIPTS)
    if session is None:
        return Result(WARN, label, f"as_of={as_of.isoformat()}: receipt okunamadı")
    state, complete = _session_state(session)
    return Result(OK if complete else WARN, label, f"as_of={as_of.isoformat()}, {state}")


def check_window(
    config: object, guards: object, data_root: Path, run_date: date,
) -> Result:
    """Which window end E the pipeline would target (planner, offline)."""
    from fintr.collection.planner import (
        plan_daily_collection, scan_base_capture_ends, scan_last_success,
    )

    label = "pencere sonu (E)"
    capture_root = data_root / config.capture_root
    last_success = scan_last_success(capture_root / RUNS_SUBDIR)
    base_ends: dict[str, date] = {}
    if config.base_capture_root is not None:
        base_ends = scan_base_capture_ends(
            data_root / config.base_capture_root, config.base_capture_filename,
            [s for s in guards.split.development_symbols if not guards.is_excluded(s)],
        )
    plan = plan_daily_collection(
        run_date=run_date, guards=guards, last_success_by_symbol=last_success,
        base_capture_end_by_symbol=base_ends, end_offset_days=config.end_offset_days,
        lookback_calendar_days=config.lookback_calendar_days, max_span_days=config.max_span_days,
    )
    end = plan.window_end
    head = (
        f"R={run_date.isoformat()} ({WEEKDAYS_TR[run_date.weekday()]}), "
        f"E={end.isoformat()} ({WEEKDAYS_TR[end.weekday()]})"
    )
    planned = plan.planned()
    blocked = plan.blocked()
    if blocked:
        names = ", ".join(entry.symbol for entry in blocked)
        return Result(WARN, label, (
            f"{head}; {len(blocked)} sembol max_span_days={plan.max_span_days} sınırını aşıyor "
            f"({names}); çalıştırma karar bekliyor"
        ))
    if end.weekday() >= 5:
        return Result(OK, label, f"{head}: yeni veri beklenmiyor (E hafta sonu)")
    effective_last_ends = []
    for entry in planned:
        known = [d for d in (last_success.get(entry.symbol), base_ends.get(entry.symbol)) if d]
        effective_last_ends.append(max(known) if known else None)
    if planned and all(last is not None and last >= end for last in effective_last_ends):
        return Result(OK, label, f"{head}: yeni veri beklenmiyor (E zaten toplanmış)")
    starts = [entry.window.start for entry in planned]
    catch_up = sum(1 for entry in planned if entry.window.catch_up)
    span = (
        f"S={min(starts).isoformat()}..{max(starts).isoformat()}" if starts
        else "planlanan sembol yok"
    )
    return Result(OK, label, (
        f"{head}: yeni veri bekleniyor; {len(planned)} sembol planlı "
        f"({catch_up} catch-up), {span}; "
        "resmi tatil takvimi yok, E'nin tatil olup olmadığı denetlenmedi"
    ))


def check_disk(path: Path) -> Result:
    label = "diskte boş alan"
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return Result(ERR, label, "disk bilgisi okunamadı")
    gib = free / (1 << 30)
    detail = f"{gib:.1f} GiB boş"
    if free < DISK_ERROR_BYTES:
        return Result(ERR, label, f"{detail} (< {DISK_ERROR_BYTES >> 20} MiB)")
    if free < DISK_WARN_BYTES:
        return Result(WARN, label, f"{detail} (< {DISK_WARN_BYTES >> 30} GiB)")
    return Result(OK, label, detail)


# --- orchestration -----------------------------------------------------------


def collect_results(
    root: Path, *, env: Mapping[str, str], run_date: date,
    config_path: Path = DEFAULT_COLLECTION_CONFIG,
) -> list[Result]:
    results: list[Result] = []
    pyproject = _load_pyproject(root)
    results.append(check_python(pyproject))
    results.extend(check_packages(pyproject))
    results.extend(check_telegram_env(env))
    results.append(check_claude_cli(root))

    config_result = _guarded("config toplama", lambda: check_collection_config(root, config_path))
    config = None
    if not isinstance(config_result, Result):
        config_result, config = config_result
    results.append(config_result)

    guard_outcome = _guarded("split/exclusions", check_active_guards)
    guards = None
    if isinstance(guard_outcome, Result):
        results.append(guard_outcome)
    else:
        split_result, exclusions_result, guards = guard_outcome
        results.append(split_result)
        if exclusions_result is not None:
            results.append(exclusions_result)

    results.append(_guarded("spec hash (FREEZE.md)", lambda: check_freeze_hashes(root)))
    results.append(_guarded("tarayıcı kuralları", lambda: check_scanner_rules(root)))
    results.append(check_split_hash_in_specs(root, guards.split.split_sha256 if guards else None))
    results.append(check_test_plan_hash(root))
    results.extend(check_other_configs(root))

    capture_root = _capture_root(root, config)
    results.append(check_last_run(capture_root / RUNS_SUBDIR))
    scanner_result, scanner_as_of = check_last_scanner(root / SCANNER_DIR)
    results.append(scanner_result)
    results.append(_guarded(
        "son notify receipt", lambda: check_last_receipt(root / NOTIFY_DIR, scanner_as_of),
    ))
    note_result = _guarded("son notify not receipt'i", lambda: check_last_note_receipt(root / NOTIFY_DIR))
    if note_result is not None:
        results.append(note_result)

    if config is not None and guards is not None:
        results.append(_guarded(
            "pencere sonu (E)", lambda: check_window(config, guards, root, run_date),
        ))
    else:
        results.append(Result(
            WARN, "pencere sonu (E)", "atlandı: config veya split/exclusions yüklenemedi",
        ))

    results.append(check_disk(root))
    return results


def render(results: list[Result]) -> list[str]:
    lines = [f"[{result.level:<5}] {result.label}: {result.detail}" for result in results]
    counts = {level: sum(1 for r in results if r.level == level) for level in (OK, WARN, ERR)}
    lines.append(f"Özet: {counts[OK]} OK, {counts[WARN]} UYARI, {counts[ERR]} HATA")
    return lines


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the daily-run environment (offline, read-only); see module docstring.",
    )
    parser.add_argument(
        "--run-date", type=_date, default=None,
        help="Run day R (Europe/Istanbul calendar date); default: today.",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_COLLECTION_CONFIG,
        help=f"daily collection config; default: {DEFAULT_COLLECTION_CONFIG.as_posix()}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # A legacy console code page must not abort the report.
        sys.stdout.reconfigure(errors="replace")
    run_date = args.run_date if args.run_date is not None else _istanbul_today()
    results = collect_results(
        PROJECT_ROOT, env=os.environ, run_date=run_date, config_path=args.config,
    )
    for line in render(results):
        print(line)
    return 1 if any(result.level == ERR for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
