"""CLI: find (and optionally set aside) saved price files whose last bar has no close.

Before the incomplete-last-bar guard, a price file whose last bar (the
requested end day) carried ``close = None`` (Yahoo had not published the close
yet) was saved; the files are no-overwrite, so such a file blocks the window
for good and makes the day look like a holiday to the offline calendar.

Default is ``--dry-run``: lists the affected files (symbol, file name, last
bar date) and changes nothing. ``--apply`` moves each one, never deleting it
and never overwriting, to ``<price-dir>/_incomplete/<date>/<SYMBOL>/<file>``;
a later ``collect_daily.py`` / ``fetch_price_history.py --save`` run can then
fetch the window again. The rule is ``fintr.data.price_history.last_bar_incomplete``
(the same one the fetch uses). Prints no price or volume value.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

from fintr.collection.new_data_guard import istanbul_today
from fintr.data.price_history import last_bar_incomplete

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICE_DIR = ROOT / "data" / "interim" / "price_history"
INCOMPLETE_DIRNAME = "_incomplete"
_PRICE_FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json$")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List price_history files whose last bar has close=None; --apply moves "
        "them (no delete) to _incomplete/<date>/<SYMBOL>/."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="list only, change nothing (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="move the listed files to <price-dir>/_incomplete/<date>/<SYMBOL>/")
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR,
                        help="price history root (default: data/interim/price_history)")
    parser.add_argument("--run-date", type=_date, default=None,
                        help="the <date> folder name (default: today, Europe/Istanbul)")
    return parser


def find_incomplete(price_dir: Path) -> tuple[list[tuple[str, Path, str]], int]:
    """([(symbol, path, last bar date)], unreadable file count), sorted."""
    found: list[tuple[str, Path, str]] = []
    unreadable = 0
    if not price_dir.is_dir():
        return found, unreadable
    for symbol_dir in sorted(price_dir.iterdir()):
        if not symbol_dir.is_dir() or symbol_dir.is_symlink() or symbol_dir.name.startswith("_"):
            continue
        for path in sorted(symbol_dir.iterdir()):
            if not path.is_file() or path.is_symlink() or not _PRICE_FILE_NAME.fullmatch(path.name):
                continue
            try:
                normalized = json.loads(path.read_text(encoding="utf-8"))["normalized"]
                if last_bar_incomplete(normalized):
                    found.append((symbol_dir.name, path, normalized["bars"][-1]["date"]))
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                unreadable += 1
    return found, unreadable


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    price_dir = args.price_dir
    found, unreadable = find_incomplete(price_dir)
    mode = "apply" if args.apply else "dry-run"
    print(f"Price dir: {price_dir} ({mode})")
    for symbol, path, last_date in found:
        print(f"  {symbol}  {path.name}  last bar {last_date}: close missing")
    print(f"Incomplete files: {len(found)}; unreadable (skipped): {unreadable}")
    if not args.apply:
        if found:
            print("Nothing moved (dry-run). Re-run with --apply to move them to "
                  f"{INCOMPLETE_DIRNAME}/<date>/<SYMBOL>/.")
        return 0

    label = (args.run_date or istanbul_today()).isoformat()
    moved = 0
    for symbol, path, _last in found:
        target_dir = price_dir / INCOMPLETE_DIRNAME / label / symbol
        target = target_dir / path.name
        if target.exists():
            print(f"  not moved (target exists): {symbol} {path.name}", file=sys.stderr)
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        path.rename(target)
        moved += 1
    print(f"Moved: {moved} of {len(found)} to {price_dir / INCOMPLETE_DIRNAME / label}")
    return 0 if moved == len(found) else 1


if __name__ == "__main__":
    sys.exit(main())
