"""CLI: Efe's daily routine in one command (runbook 6g order).

Steps, each a subprocess of this interpreter with the same environment, so
every script keeps its own guards and exit codes:

1. ``collect_daily.py`` (stdio inherited: it prompts for initData itself)
2. ``scan_observations.py --daily-root ... --save --format json`` (only if 1 == 0)
3. ``notify_observations.py --as-of <as_of>`` (only if 2 == 0; dry-run unless
   this script got ``--send``; ``--resend`` is never passed)

Optional steps 4-5, only with ``--llm-note`` (default off; Efe's decision of
2026-10-02), AFTER the deterministic notice has been sent so it never waits
for the model: 4. ``llm_note.py --as-of <as_of>``; 5. ``notify_observations.py
--llm-note-dir`` = the note as its own Telegram message(s) with its own
receipt (``note_receipt.json``, same no-overwrite/resend guard). Their
failure never changes the already-sent notice or the exit code; the summary
shows ``llm_note: <status>`` (the script's ``LLM_NOTE_STATUS`` line; one model
call per symbol, so normally ``ok=A,rejected=B,failed=C``) and ``note_notify:
<0|already_sent|skipped|failed:exit_N>``. A symbol with an accepted note for the
same as_of is not regenerated on a same-day re-run. Without ``--llm-note`` the
behaviour is exactly as before.

Any other non-zero exit stops the pipeline with that exit code; nothing is sent.
The scanner's stdout (the observation document) is captured and discarded;
only its stderr (saved path or error line) is read. This script prints no
financial value.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DAILY_ROOT = os.path.join("data", "interim", "daily_capture")
SCANNER_DIR = os.path.join("data", "interim", "scanner_daily")
LLM_NOTES_DIR = os.path.join("data", "interim", "llm_notes")
LLM_STATUS_PREFIX = "LLM_NOTE_STATUS: "
SAVED_PREFIX = "Observations saved:"
EXISTS_PREFIX = "Scan failed: output file already exists (no overwrite):"
RECEIPT_EXISTS_MARK = "receipt zaten var"
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
    parser.add_argument("--llm-note", action="store_true",
                        help="after the deterministic notice: run the Opus 5.5 analysis note step "
                             "(llm_note.py) and send the note as its own message with its own "
                             "receipt; default off; its failure changes neither the notice nor "
                             "the exit code")
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


def llm_note_command(as_of: str) -> list[str]:
    return [sys.executable, _script("llm_note.py"), "--as-of", as_of,
            "--scanner-dir", SCANNER_DIR, "--daily-root", DAILY_ROOT, "--out-dir", LLM_NOTES_DIR]


def notify_command(as_of: str, send: bool, note: bool = False) -> list[str]:
    """``note=True``: the analysis note as its own message(s) with its own
    receipt (``notify_observations.py --llm-note-dir``); the deterministic
    command (``note=False``) is exactly what it was without the note step."""
    cmd = [sys.executable, _script("notify_observations.py"), "--as-of", as_of,
           "--scanner-dir", SCANNER_DIR]
    if send:
        cmd.append("--send")
    if note:
        cmd += ["--llm-note-dir", LLM_NOTES_DIR]
    return cmd


def run_llm_note(as_of: str) -> str:
    """Run the note step; return its status string. Never raises and never
    decides the pipeline's exit code."""
    try:
        proc = subprocess.run(llm_note_command(as_of), cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env())
    except Exception as exc:  # the note must never stop the deterministic notice
        return f"error:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(LLM_STATUS_PREFIX):
            return line[len(LLM_STATUS_PREFIX):].strip() or f"error:exit_{proc.returncode}"
    return f"error:exit_{proc.returncode}"


def run_note_notify(as_of: str, send: bool) -> str:
    """Send (or dry-run) the note as its own Telegram message(s); returns a
    status string. Never raises."""
    try:
        proc = subprocess.run(notify_command(as_of, send, note=True), cwd=ROOT,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", env=_child_env())
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 1 and send and RECEIPT_EXISTS_MARK in (proc.stderr or ""):
        return "already_sent"
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

    proc = subprocess.run(notify_command(as_of, args.send), cwd=ROOT,
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

    if args.llm_note:
        # After the deterministic notice has gone out: nothing below can
        # change it or the exit code; failures only show up in the summary.
        results += run_note_steps(as_of, args.send)

    _summary(results, **summary)
    if already_sent:
        print("  sent: no (already sent earlier; no resend)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
