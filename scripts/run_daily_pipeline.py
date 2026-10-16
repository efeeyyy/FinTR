"""CLI: Efe's daily routine in one command (runbook 6g order).

Steps, each a subprocess of this interpreter with the same environment, so
every script keeps its own guards and exit codes:

1. ``collect_daily.py`` (stdio inherited: it prompts for initData itself)
2. ``scan_observations.py --daily-root ... --save --format json`` (only if 1 == 0)
3. ``notify_observations.py --as-of <as_of>`` (only if 2 == 0; dry-run unless
   this script got ``--send``; ``--resend`` is never passed)

Layout v3 (default, ``--layout v3``; Efe's decision of 2026-10-03, the Telegram
signal chat): ONE short message a day. With ``--llm-note`` the order is collect
-> scan -> ``llm_note.py`` -> the single v3 message (``notify_observations.py
--layout v3 --note-assessments-dir``), the accepted note's evidence word going
into each stock's line; there are no separate note messages. If the note step
errors, times out (``LLM_NOTE_TIMEOUT_MARGIN_SECONDS`` over the config's
``max_run_seconds``) or rejects a symbol, the message is still sent with ``—``
for the missing assessments -- the deterministic notice never depends on the
note. Without ``--llm-note`` v3 is sent with ``—`` everywhere. The receipt and
resume rules are the notifier's (``receipt.json``), so a same-day re-run sends
nothing again; when the receipt already exists (``--send``) the note step is
not repeated either (``llm_note: skipped_already_sent``).

``--layout v2`` keeps everything below exactly as it was.

Optional steps 4-5 (v2 only), only with ``--llm-note`` (default off; Efe's decision of
2026-10-02), AFTER the deterministic notice has been sent so it never waits
for the model: 4. ``llm_note.py --as-of <as_of>``; 5. ``notify_observations.py
--llm-note-dir`` = the note as its own Telegram message(s) with its own
resumable receipt (``note_receipt.json``; per-message sent state, like the
notice's). Their failure never changes the already-sent notice or the exit
code; the summary shows ``llm_note: <status>`` (the script's ``LLM_NOTE_STATUS``
line; one model call per symbol, so normally ``ok=A,rejected=B,failed=C``) and
``note_notify: <0|already_sent|partial|content_changed|skipped|failed:exit_N>``.
A symbol with an accepted note for the same as_of is not regenerated on a
same-day re-run. On a same-day re-run a completely sent note is not sent again
(``already_sent``); a partly sent note is completed (only the unsent note
messages go out, ``0``); ``partial`` = this run stopped part-way (re-run to
finish), ``content_changed`` = a partial note receipt exists but the note's
content differs, so nothing was sent (a full resend needs ``notify_observations.py
--llm-note-dir ... --resend`` by hand; this script never passes ``--resend``).
Without ``--llm-note`` the behaviour is exactly as before.

Optional price revision check (``--price-diff``, default off): right after a
successful collect (not with ``--dry-run``/``--skip-collect``) the offline
``diff_price_history.py`` compares each symbol's newest saved price file with
the previous one and writes a report under
``data/interim/price_revisions/<run date>/``. The summary shows
``price_diff: <none|changes:N|failed>``; whatever happens, the pipeline goes on
and its exit code is unchanged (studies and the scanner read prices as before).

No-new-data guard (before step 1; no initData prompt, no connection): the
planner's window end ``E = R - end_offset_days`` is checked offline
(``fintr.collection.new_data_guard``). If no new trading day is expected
(weekend; an official full closure in the approved BIST holiday calendar,
``configs/market/bist_holidays.json``; holiday per the saved price-history
calendar; or every planned symbol's last saved window end is already ``>= E``) and no unsettled day is
pending in the latest saved scan, the pipeline prints ``yeni veri beklenmiyor
(son işlem günü X); toplama atlandı`` and exits 0 -- no collect, scan or
notify, so nothing is sent (an already sent day is never sent again). An
unsettled day (or an unreadable settlement state) keeps the collection, for
custody settlement. ``--force-collect`` bypasses the guard; ``--dry-run`` and
``--skip-collect`` are not guarded. Any guard error falls back to collecting.
If ``E`` lies outside the holiday calendar's coverage (or the calendar cannot be
read), a note goes to stderr and the official-holiday reason is simply not used:
the price-history heuristic decides as before (no silent claim either way).

Optional last step ``--forward-log`` (default off; docs/forward_log.md): after a
sent (or already sent) notice, ``forward_log.py append --as-of <as_of>`` adds the
SHA-256 of the day's scanner/notify/llm_notes files to the hash-chained
``forward_log/manifest.jsonl``. The summary shows ``forward_log: appended|noop|
skipped_dry_run|failed:...``; a failure (e.g. files changed since an earlier
entry: use ``forward_log.py append --revision`` by hand) never changes the
notice or the exit code. Committing the manifest stays a manual step.

Optional accuracy tracking (``--accuracy``, default off; docs/accuracy.md): right after the
scan, ``track_accuracy.py`` writes the walk-forward directional accuracy of the frozen rules
for the development symbols to ``data/interim/accuracy/<run date>/`` (never overwrites; a
same-day re-run reports ``exists``). The summary shows ``accuracy: <saved|exists|failed:...>``;
it prints only the output path (no metric), sends nothing to Telegram and, whatever happens
(failure, timeout), the notice step and the exit code are unchanged.

Missing-close warning (after the steps): when the scan's as_of is older than the
collected window end E, E is a weekday without a close in the saved price
history (not a holiday; Yahoo had not published it, so the price step did not
save the file) and that as_of's notice was already sent before this run (on an
earlier day; a same-day re-run of a sent day exits 0), nothing new could be scanned. The pipeline prints ``yeni gün taranamadı: <E> için
kapanış fiyatı yok ...`` and exits 3 so it is noticed (not with ``--skip-price``).

Any other non-zero exit stops the pipeline with that exit code; nothing is sent.
The scanner's stdout (the observation document) is captured and discarded;
only its stderr (saved path or error line) is read. This script prints no
financial value.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from fintr.collection.new_data_guard import (
    NewDataDecision,
    decide_new_data,
    istanbul_date,
    istanbul_today,
    last_saved_end,
    load_price_calendar,
    missing_close_day,
    scanner_unsettled_pending,
)
from fintr.collection.planner import (
    CollectionPlannerError,
    load_daily_collection_config,
    plan_daily_collection,
    scan_base_capture_ends,
    scan_last_success,
)
from fintr.data.market_calendar import (
    DEFAULT_MARKET_CALENDAR_PATH,
    KIND_FULL_CLOSURE,
    MarketCalendarError,
    OutsideCoverageError,
    load_market_calendar,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DAILY_ROOT = os.path.join("data", "interim", "daily_capture")
SCANNER_DIR = os.path.join("data", "interim", "scanner_daily")
LLM_NOTES_DIR = os.path.join("data", "interim", "llm_notes")
NOTIFY_DIR = os.path.join("data", "interim", "notify")
PRICE_DIR = os.path.join("data", "interim", "price_history")
PRICE_DIFF_DIR = os.path.join("data", "interim", "price_revisions")
PRICE_DIFF_TIMEOUT_SECONDS = 600
PRICE_DIFF_SUMMARY_PREFIX = "PRICE_DIFF: "
DAILY_CONFIG = ROOT / "configs" / "collection" / "daily_miniapp_v1.json"
MARKET_CALENDAR = ROOT / DEFAULT_MARKET_CALENDAR_PATH
LLM_NOTE_CONFIG = ROOT / "configs" / "llm_note" / "config_v1.json"
LLM_NOTE_DEFAULT_RUN_SECONDS = 1800  # the config's max_run_seconds, if the file cannot be read
LLM_NOTE_TIMEOUT_MARGIN_SECONDS = 300
LLM_STATUS_PREFIX = "LLM_NOTE_STATUS: "
FORWARD_LOG_STATUS_PREFIX = "FORWARD_LOG_STATUS: "
FORWARD_LOG_TIMEOUT_SECONDS = 120
ACCURACY_SAVED_PREFIX = "ACCURACY_SAVED: "
ACCURACY_TIMEOUT_SECONDS = 900
ACCURACY_EXIT_EXISTS = 3  # track_accuracy.py: output of this run date already exists
SAVED_PREFIX = "Observations saved:"
EXISTS_PREFIX = "Scan failed: output file already exists (no overwrite):"
RECEIPT_EXISTS_MARK = "receipt zaten var"
NOTE_PARTIAL_MARK = "analiz notu kısmi gönderim ("
NOTE_CHANGED_MARK = "mesaj içeriği ondan farklı"
EXIT_NEW_DAY_NOT_SCANNED = 3
TELEGRAM_ENV = ("FINTR_TELEGRAM_BOT_TOKEN", "FINTR_TELEGRAM_CHAT_ID")


def _child_env() -> dict[str, str]:
    """Children write UTF-8 to the pipes we read; otherwise a Windows child
    uses the ANSI code page (cp1254) and a Turkish letter in an error line
    breaks decoding after the child already ran."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run collect_daily -> scan_observations -> notify_observations "
        "in order, stopping at the first non-zero exit."
    )
    parser.add_argument("--allow-session-takeover", action="store_true",
                        help="passed to collect_daily.py")
    parser.add_argument("--skip-price", action="store_true", help="passed to collect_daily.py")
    parser.add_argument("--run-date", default=None, help="passed to collect_daily.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect_daily.py --dry-run only; scan and notify are skipped")
    parser.add_argument("--skip-collect", action="store_true",
                        help="start at the scan step (re-run scan+notify after a successful collect)")
    parser.add_argument("--send", action="store_true",
                        help="notify step actually sends to Telegram (default: notify dry-run)")
    parser.add_argument("--force-collect", action="store_true",
                        help="skip the no-new-data guard (weekend / holiday / already collected): "
                             "always run collect_daily.py")
    parser.add_argument("--layout", choices=("v2", "v3"), default="v3",
                        help="v3 (default): one short signal message a day; v2: summary + one "
                             "message per stock (the note, if any, as separate messages after it)")
    parser.add_argument("--price-diff", action="store_true",
                        help="after a successful collect, compare each symbol's newest saved price "
                             "file with the previous one (offline, diff_price_history.py) and "
                             "write the report under data/interim/price_revisions/<run date>/. "
                             "Default off; never blocks the notice or changes the exit code; "
                             "summary line price_diff: <none|changes:N|failed>")
    parser.add_argument("--llm-note", action="store_true",
                        help="run the Opus 5.5 analysis note step (llm_note.py). v3: before the "
                             "single message, whose lines carry the accepted evidence words (a "
                             "failed or timed-out note prints the dash and never blocks the "
                             "message); v2: after the notice, sent as its own message(s) with "
                             "its own receipt. Default off; its failure never changes the exit "
                             "code")
    parser.add_argument("--forward-log", action="store_true",
                        help="after a sent (or already sent) notice, append the day's artifact "
                             "hashes to forward_log/manifest.jsonl (scripts/forward_log.py; "
                             "docs/forward_log.md). Default off; the summary shows "
                             "forward_log: appended|noop|skipped_dry_run|failed:...; its failure "
                             "never changes the notice or the exit code")
    parser.add_argument("--accuracy", action="store_true",
                        help="after the scan, run track_accuracy.py (walk-forward directional "
                             "accuracy of the frozen rules, development symbols only; "
                             "docs/accuracy.md) into data/interim/accuracy/<run date>/. Default "
                             "off; sends nothing to Telegram; its failure never changes the "
                             "notice or the exit code; summary line accuracy: "
                             "saved|exists|failed:...")
    return parser


def _script(name: str) -> str:
    return str(SCRIPTS / name)


def collect_command(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, _script("collect_daily.py")]
    if args.allow_session_takeover:
        cmd.append("--allow-session-takeover")
    if args.skip_price:
        cmd.append("--skip-price")
    if args.run_date:
        cmd += ["--run-date", args.run_date]
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def scan_command() -> list[str]:
    return [sys.executable, _script("scan_observations.py"), "--daily-root", DAILY_ROOT,
            "--out-dir", SCANNER_DIR, "--save", "--format", "json"]


def price_diff_command(out_path: str) -> list[str]:
    return [sys.executable, _script("diff_price_history.py"), "--price-dir", PRICE_DIR,
            "--out", out_path]


def run_price_diff(run_date_arg: str | None, now: datetime | None = None) -> str:
    """Offline revision check of the saved price files (docs/price_history.md).

    Returns ``none`` | ``changes:N`` | ``failed``; never raises and never
    blocks the pipeline. The report goes to
    ``data/interim/price_revisions/<run date>/price_diff_<UTC stamp>.json``
    (the stamp keeps a same-day re-run from hitting the no-overwrite rule).
    Only counts and the report path are printed, no price value."""
    moment = datetime.now(timezone.utc) if now is None else now
    day = run_date_arg or istanbul_today().isoformat()
    out_path = os.path.join(PRICE_DIFF_DIR, day, f"price_diff_{moment.strftime('%Y%m%dT%H%M%SZ')}.json")
    try:
        proc = subprocess.run(price_diff_command(out_path), cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env(), timeout=PRICE_DIFF_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return "failed"
    if proc.returncode == 0:
        return "none"
    if proc.returncode != 2:
        return "failed"
    line = next((ln for ln in (proc.stdout or "").splitlines()
                 if ln.startswith(PRICE_DIFF_SUMMARY_PREFIX)), "")
    fields = dict(part.split("=", 1) for part in line[len(PRICE_DIFF_SUMMARY_PREFIX):].split()
                  if "=" in part)
    if not fields.get("changes", "").isdigit():
        return "failed"
    print(f"UYARI: kayıtlı fiyat dosyalarında geriye dönük değişiklik: {fields['changes']} tarih "
          f"(yüksek {fields.get('high', '?')}, orta {fields.get('medium', '?')}, "
          f"bilgi {fields.get('info', '?')}); rapor: {out_path}")
    return f"changes:{fields['changes']}"


def llm_note_command(as_of: str) -> list[str]:
    return [sys.executable, _script("llm_note.py"), "--as-of", as_of,
            "--scanner-dir", SCANNER_DIR, "--daily-root", DAILY_ROOT, "--out-dir", LLM_NOTES_DIR]


def notify_command(as_of: str, send: bool, note: bool = False, *, layout: str = "v2",
                   assessments: bool = False) -> list[str]:
    """``note=True``: the analysis note as its own message(s) with its own
    receipt (``notify_observations.py --llm-note-dir``); the deterministic
    command (``note=False``) is what it was without the note step, with the
    layout now explicit (the notifier's default is v3). ``assessments=True``
    (layout v3): the accepted notes' evidence words go into the message lines."""
    cmd = [sys.executable, _script("notify_observations.py"), "--as-of", as_of,
           "--scanner-dir", SCANNER_DIR, "--layout", layout]
    if send:
        cmd.append("--send")
    if note:
        cmd += ["--llm-note-dir", LLM_NOTES_DIR]
    if assessments:
        cmd += ["--note-assessments-dir", LLM_NOTES_DIR]
    return cmd


def llm_note_timeout_seconds() -> int:
    """Wall-clock limit for the v3 note step: the note config's whole-run
    budget plus a margin, so a hung child can never keep the message back."""
    try:
        run_seconds = json.loads(LLM_NOTE_CONFIG.read_text(encoding="utf-8"))["max_run_seconds"]
        if isinstance(run_seconds, bool) or not isinstance(run_seconds, int) or run_seconds <= 0:
            raise ValueError("max_run_seconds")
    except (OSError, ValueError, KeyError, TypeError):
        run_seconds = LLM_NOTE_DEFAULT_RUN_SECONDS
    return run_seconds + LLM_NOTE_TIMEOUT_MARGIN_SECONDS


def v3_notice_exists(as_of: str) -> bool:
    """The v3 message of ``as_of`` already has its (session-head) receipt."""
    return (ROOT / NOTIFY_DIR / as_of / "receipt.json").exists()


def v3_notice_sent_on(as_of: str, run_date_arg: str | None) -> bool:
    """The receipt of ``as_of`` was written on the run date (Istanbul day): a
    same-day re-run, not a later day whose scan lags behind. Unreadable = no."""
    try:
        run_date = date.fromisoformat(run_date_arg) if run_date_arg else istanbul_today()
        receipt = json.loads((ROOT / NOTIFY_DIR / as_of / "receipt.json").read_text(encoding="utf-8"))
        sent = datetime.fromisoformat(str(receipt["sent_at_utc"]).replace("Z", "+00:00"))
        return istanbul_date(sent) == run_date
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_llm_note(as_of: str, timeout: float | None = None) -> str:
    """Run the note step; return its status string. Never raises and never
    decides the pipeline's exit code. ``timeout`` (v3): seconds after which the
    child is killed and the status is ``error:timeout``."""
    try:
        proc = subprocess.run(llm_note_command(as_of), cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env(),
                              **({} if timeout is None else {"timeout": timeout}))
    except subprocess.TimeoutExpired:
        return "error:timeout"
    except Exception as exc:  # the note must never stop the deterministic notice
        return f"error:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(LLM_STATUS_PREFIX):
            return line[len(LLM_STATUS_PREFIX):].strip() or f"error:exit_{proc.returncode}"
    return f"error:exit_{proc.returncode}"


def accuracy_command(run_date_arg: str | None) -> list[str]:
    cmd = [sys.executable, _script("track_accuracy.py"), "--daily-root", DAILY_ROOT,
           "--scanner-dir", SCANNER_DIR]
    if run_date_arg:
        cmd += ["--run-date", run_date_arg]
    return cmd


def run_accuracy(run_date_arg: str | None) -> str:
    """Run the accuracy tracker; return ``saved``, ``exists`` or ``failed:...``.
    Never raises and never decides the pipeline's exit code. Only the output
    path is printed (the tracker's counts and metrics stay in its files)."""
    try:
        proc = subprocess.run(accuracy_command(run_date_arg), cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env(), timeout=ACCURACY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "failed:timeout"
    except Exception as exc:  # the tracker must never stop or change the notice
        return f"failed:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == ACCURACY_EXIT_EXISTS:
        return "exists"
    if proc.returncode != 0:
        return f"failed:exit_{proc.returncode}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith(ACCURACY_SAVED_PREFIX):
            print(f"accuracy output: {line[len(ACCURACY_SAVED_PREFIX):].strip()}")
            return "saved"
    return "failed:no_output_path"


def run_forward_log(as_of: str) -> str:
    """Append the day's artifact hashes to the forward log; return a status
    string (``appended``, ``noop`` or ``failed:...``). Never raises and never
    decides the pipeline's exit code."""
    try:
        proc = subprocess.run([sys.executable, _script("forward_log.py"), "append", "--as-of", as_of],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", env=_child_env(),
                              timeout=FORWARD_LOG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "failed:timeout"
    except Exception as exc:  # the log must never stop or change the notice
        return f"failed:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return f"failed:exit_{proc.returncode}"
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(FORWARD_LOG_STATUS_PREFIX):
            return line[len(FORWARD_LOG_STATUS_PREFIX):].strip() or "failed:no_status"
    return "failed:no_status"


def run_note_notify(as_of: str, send: bool) -> str:
    """Send (or dry-run) the note as its own Telegram message(s); returns a
    status string (see the module docstring). Never raises."""
    try:
        proc = subprocess.run(notify_command(as_of, send, note=True), cwd=ROOT,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env())
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    note_err = proc.stderr or ""
    if proc.returncode == 1 and send:
        if RECEIPT_EXISTS_MARK in note_err:
            return "already_sent"
        if NOTE_CHANGED_MARK in note_err:
            print("Note receipt for this as_of is incomplete but the note content changed; "
                  "nothing sent. To send the whole note again run notify_observations.py "
                  "--llm-note-dir ... --resend by hand.")
            return "content_changed"
        if NOTE_PARTIAL_MARK in note_err:
            print("Note was only partly sent; re-running the same command sends only the "
                  "note messages not yet sent (see note_receipt.json).")
            return "partial"
    return "0" if proc.returncode == 0 else f"failed:exit_{proc.returncode}"


def run_note_steps(as_of: str, send: bool) -> list[tuple[str, str]]:
    """The note step and the separate note notice; results for the summary."""
    status = run_llm_note(as_of)
    results = [("llm_note", status)]
    if status == "no_observations":
        results.append(("note_notify", "skipped"))
    else:
        results.append(("note_notify", run_note_notify(as_of, send)))
    return results


def extract_as_of(stderr_text: str, prefix: str = SAVED_PREFIX) -> tuple[str, str] | None:
    """Return (as_of, path) from the scanner's 'Observations saved:' line (or,
    with ``EXISTS_PREFIX``, from its no-overwrite refusal naming the existing file)."""
    for line in stderr_text.splitlines():
        if line.startswith(prefix):
            saved = line[len(prefix):].strip()
            as_of = Path(saved.replace("\\", "/")).parent.name
            if as_of:
                return as_of, saved
    return None


def official_holiday_name(window_end: date) -> str | None:
    """The BIST calendar's closure name when ``window_end`` is a listed full closure on a
    weekday, else ``None``. Outside the calendar's coverage, or when the calendar cannot be
    read, it says so on stderr and answers ``None`` (the price-history heuristic decides)."""
    try:
        calendar = load_market_calendar(MARKET_CALENDAR)
        entry = calendar.listed_day(window_end)
    except OutsideCoverageError:
        print(f"BIST holiday calendar does not cover {window_end.isoformat()}; "
              "official-holiday check skipped (price-history heuristic only).", file=sys.stderr)
        return None
    except MarketCalendarError as exc:
        print(f"BIST holiday calendar unusable ({exc}); official-holiday check skipped "
              "(price-history heuristic only).", file=sys.stderr)
        return None
    if entry is not None and entry.kind == KIND_FULL_CLOSURE:
        return entry.name_tr
    return None


def new_data_decision(run_date_arg: str | None) -> NewDataDecision | None:
    """The offline no-new-data check; ``None`` = could not decide (collect).

    Uses the same config, active guards and planner as ``collect_daily.py``;
    reads local files only (run records, price history, latest scan)."""
    try:
        run_date = date.fromisoformat(run_date_arg) if run_date_arg else istanbul_today()
        config = load_daily_collection_config(DAILY_CONFIG)
        guards = load_active_guards()
        capture_root = ROOT / config.capture_root
        development = [
            symbol for symbol in guards.split.development_symbols if not guards.is_excluded(symbol)
        ]
        base_ends = {}
        if config.base_capture_root is not None:
            base_ends = scan_base_capture_ends(
                ROOT / config.base_capture_root, config.base_capture_filename, development,
            )
        last_success = scan_last_success(capture_root / "_runs")
        plan = plan_daily_collection(
            run_date=run_date, guards=guards, last_success_by_symbol=last_success,
            base_capture_end_by_symbol=base_ends, end_offset_days=config.end_offset_days,
            lookback_calendar_days=config.lookback_calendar_days, max_span_days=config.max_span_days,
        )
        if plan.blocked():
            return None  # collect_daily reports the decision the plan needs
        planned = [entry.symbol for entry in plan.planned()]
        return decide_new_data(
            window_end=plan.window_end,
            calendar=load_price_calendar(ROOT / PRICE_DIR, planned),
            last_saved=last_saved_end(planned, last_success, base_ends),
            unsettled_pending=scanner_unsettled_pending(ROOT / SCANNER_DIR),
            official_holiday_name=official_holiday_name(plan.window_end),
        )
    except (CollectionPlannerError, SymbolGuardError, OSError, ValueError) as exc:
        print(f"No-new-data check could not run ({type(exc).__name__}); collecting.", file=sys.stderr)
        return None


def missing_close_window_end(run_date_arg: str | None, as_of: str) -> date | None:
    """The window end E when the scan could not reach it for lack of a close
    (see ``missing_close_day``); ``None`` = no gap or could not decide (the
    warning is advisory, so an unreadable input never changes the exit code)."""
    try:
        run_date = date.fromisoformat(run_date_arg) if run_date_arg else istanbul_today()
        config = load_daily_collection_config(DAILY_CONFIG)
        guards = load_active_guards()
        window_end = run_date - timedelta(days=config.end_offset_days)
        development = [
            symbol for symbol in guards.split.development_symbols if not guards.is_excluded(symbol)
        ]
        calendar = load_price_calendar(ROOT / PRICE_DIR, development)
        if missing_close_day(window_end=window_end, calendar=calendar, as_of=date.fromisoformat(as_of)):
            return window_end
    except (CollectionPlannerError, SymbolGuardError, OSError, ValueError) as exc:
        print(f"Missing-close check could not run ({type(exc).__name__}).", file=sys.stderr)
    return None


def _summary(results, as_of=None, saved=None, sent=None) -> None:
    print("Pipeline summary:")
    for step, code in results:
        print(f"  {step}: {code}")
    if as_of:
        print(f"  as_of: {as_of}")
    if saved:
        print(f"  scanner output: {saved}")
    if sent is not None:
        print(f"  sent: {'yes' if sent else 'no (notify dry-run)'}")


def _fail(results: list[tuple[str, str]], step: str, code: int) -> int:
    results.append((step, str(code)))
    if step == "notify":
        # Layout v2 sends several messages: some may already be out. The
        # notifier's receipt records them; a re-run sends only the rest.
        print(f"Pipeline stopped: step '{step}' failed with exit code {code}; later steps not run. "
              "If the notice was partly sent, re-running the same command sends only the "
              "messages not yet sent (see the notify receipt).")
    else:
        print(f"Pipeline stopped: step '{step}' failed with exit code {code}; "
              "later steps not run, nothing sent.")
    _summary(results)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results: list[tuple[str, str]] = []

    if args.send and not args.dry_run:
        missing = [name for name in TELEGRAM_ENV if not os.environ.get(name)]
        if missing:
            # Checked before collect so a missing variable does not cost a
            # whole collection run; only the names are printed, never values.
            print(f"Pipeline not started: --send needs {', '.join(missing)} "
                  "in this shell (see docs/notify.md); nothing collected or sent.",
                  file=sys.stderr)
            return 2

    if not (args.skip_collect or args.dry_run or args.force_collect):
        decision = new_data_decision(args.run_date)
        if decision is not None and not decision.expect_new_data:
            last = decision.last_trading_day.isoformat() if decision.last_trading_day else "bilinmiyor"
            print(f"yeni veri beklenmiyor (son işlem günü {last}); toplama atlandı")
            results += [("collect", "skipped_no_new_data"), ("scan", "skipped"), ("notify", "skipped")]
            _summary(results)
            print(f"  reason: {decision.reason} (window end {decision.window_end.isoformat()})")
            if decision.official_holiday_name is not None:
                print(f"  resmî tatil: {decision.official_holiday_name}")
            return 0

    if args.skip_collect:
        if args.dry_run:
            print("--skip-collect with --dry-run: nothing to plan.", file=sys.stderr)
            return 2
        results.append(("collect", "skipped"))
    else:
        code = subprocess.run(collect_command(args), cwd=ROOT).returncode
        if code != 0:
            return _fail(results, "collect", code)
        results.append(("collect", "0"))
        if args.dry_run:
            print("Steps scan and notify skipped: --dry-run collected no new data.")
            results += [("scan", "skipped"), ("notify", "skipped")]
            _summary(results)
            return 0
        if args.price_diff:
            results.append(("price_diff", run_price_diff(args.run_date)))

    proc = subprocess.run(scan_command(), cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", env=_child_env())
    stderr_text = proc.stderr or ""
    if stderr_text:
        sys.stderr.write(stderr_text)
    if proc.returncode == 1 and (existing := extract_as_of(stderr_text, EXISTS_PREFIX)):
        as_of, saved = existing
        print(f"Scan output for as_of {as_of} already exists (not overwritten); "
              "treating step 'scan' as already scanned.")
        results.append(("scan", "already_scanned"))
    elif proc.returncode != 0:
        return _fail(results, "scan", proc.returncode)
    else:
        found = extract_as_of(stderr_text)
        if found is None:
            print("Scan exited 0 but its saved path (as_of) could not be determined.")
            return _fail(results, "scan", 1)
        as_of, saved = found
        results.append(("scan", "0"))

    if args.accuracy:
        # Right after the scan, before the notice; bounded by a timeout and
        # never able to stop or alter the notice (any status is only reported).
        results.append(("accuracy", run_accuracy(args.run_date)))

    v3_note = args.layout == "v3" and args.llm_note
    if v3_note:
        # Before the single message, which never waits on the outcome: any
        # status (error, timeout, rejected symbols) just leaves dashes.
        if args.send and v3_notice_exists(as_of):
            status = "skipped_already_sent"
        else:
            status = run_llm_note(as_of, timeout=llm_note_timeout_seconds())
        results.append(("llm_note", status))

    sent_before = v3_notice_exists(as_of)  # before this run's notify can create the receipt
    proc = subprocess.run(notify_command(as_of, args.send, layout=args.layout, assessments=v3_note), cwd=ROOT,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", env=_child_env())
    notify_err = proc.stderr or ""
    if notify_err:
        sys.stderr.write(notify_err)
    already_sent = proc.returncode == 1 and args.send and RECEIPT_EXISTS_MARK in notify_err
    if already_sent:
        print(f"Notice for as_of {as_of} was already sent (receipt exists); nothing resent.")
        results.append(("notify", "already_sent"))
        summary = dict(as_of=as_of, saved=saved)
    elif proc.returncode != 0:
        return _fail(results, "notify", proc.returncode)
    else:
        results.append(("notify", "0"))
        summary = dict(as_of=as_of, saved=saved, sent=args.send)

    if args.llm_note and args.layout == "v2":
        # After the deterministic notice has gone out: nothing below can
        # change it or the exit code; failures only show up in the summary.
        results += run_note_steps(as_of, args.send)

    if args.forward_log:
        # Last, so the logged file set is the day's final one. A dry-run notify
        # leaves no receipt, so logging then would only force a revision later.
        if args.send or already_sent:
            results.append(("forward_log", run_forward_log(as_of)))
        else:
            results.append(("forward_log", "skipped_dry_run"))

    _summary(results, **summary)
    if already_sent:
        print("  sent: no (already sent earlier; no resend)")
    if sent_before and not args.skip_price and not v3_notice_sent_on(as_of, args.run_date):
        missing = missing_close_window_end(args.run_date, as_of)
        if missing is not None:
            print(f"UYARI: yeni gün taranamadı: {missing.isoformat()} için kapanış fiyatı yok "
                  "(Yahoo henüz yayınlamadı); daha sonra `--skip-collect` ile tekrar çalıştırın")
            return EXIT_NEW_DAY_NOT_SCANNED
    return 0


if __name__ == "__main__":
    sys.exit(main())
