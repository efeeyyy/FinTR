"""CLI: Efe's daily routine in one command (runbook 6g order).

Steps, each a subprocess of this interpreter with the same environment, so
every script keeps its own guards and exit codes:

1. ``collect_daily.py`` (stdio inherited: it prompts for initData itself)
2. ``scan_observations.py --daily-root ... --save --format json`` (only if 1 == 0)
3. ``notify_observations.py --as-of <as_of>`` (only if 2 == 0; dry-run unless
   this script got ``--send``; ``--resend`` is never passed)

Any non-zero exit stops the pipeline with that exit code; nothing is sent.
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
SAVED_PREFIX = "Observations saved:"


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


def notify_command(as_of: str, send: bool) -> list[str]:
    cmd = [sys.executable, _script("notify_observations.py"), "--as-of", as_of,
           "--scanner-dir", SCANNER_DIR]
    if send:
        cmd.append("--send")
    return cmd


def extract_as_of(stderr_text: str) -> tuple[str, str] | None:
    """Return (as_of, saved_path) from the scanner's 'Observations saved:' line."""
    for line in stderr_text.splitlines():
        if line.startswith(SAVED_PREFIX):
            saved = line[len(SAVED_PREFIX):].strip()
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
    print(f"Pipeline stopped: step '{step}' failed with exit code {code}; "
          "later steps not run, nothing sent.")
    _summary(results)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results: list[tuple[str, str]] = []

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
                          stderr=subprocess.PIPE, text=True, encoding="utf-8")
    stderr_text = proc.stderr or ""
    if stderr_text:
        sys.stderr.write(stderr_text)
    if proc.returncode != 0:
        return _fail(results, "scan", proc.returncode)
    found = extract_as_of(stderr_text)
    if found is None:
        print("Scan exited 0 but its saved path (as_of) could not be determined.")
        return _fail(results, "scan", 1)
    as_of, saved = found
    results.append(("scan", "0"))

    code = subprocess.run(notify_command(as_of, args.send), cwd=ROOT).returncode
    if code != 0:
        return _fail(results, "notify", code)
    results.append(("notify", "0"))
    _summary(results, as_of=as_of, saved=saved, sent=args.send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
