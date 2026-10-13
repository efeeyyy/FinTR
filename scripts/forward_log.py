"""CLI: tamper-evident forward log of the daily artifacts (docs/forward_log.md).

Subcommands (all offline, read-only on the data; only the manifest is written):

* ``append --as-of YYYY-MM-DD [--revision]`` -- append the SHA-256 of that day's
  scanner / notify / llm_notes files to the hash-chained manifest. Same hashes
  again = no-op; different hashes = refused (exit 1) unless ``--revision``.
* ``verify [--check-files]`` -- recompute the chain; with ``--check-files`` also
  re-hash the files on disk against each as_of's latest entry. Exit 1 on any break.
* ``backfill --from D --to D`` -- append past dates in order, marked
  ``backfilled``: they prove nothing about timing before their append time.

The manifest holds hashes, paths and sizes only. Nothing financial is printed.
Relative ``--data-root``/``--manifest`` resolve against the repository root.
Exit codes: 0 ok, 1 refused / broken chain / conflict, 2 usage or input error.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

from fintr.forward_log.core import (
    ConflictError,
    ForwardLogError,
    append_as_of,
    list_as_of_dates,
    load_manifest,
    validate_as_of,
    verify_files,
)
from fintr.scanner.report import resolve_code_commit

DEFAULT_DATA_ROOT = "data/interim"
DEFAULT_MANIFEST = "forward_log/manifest.jsonl"
STATUS_PREFIX = "FORWARD_LOG_STATUS: "


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
        p.add_argument("--manifest", default=DEFAULT_MANIFEST)

    append = sub.add_parser("append", help="log one as_of")
    append.add_argument("--as-of", required=True)
    append.add_argument("--revision", action="store_true",
                        help="allow a new entry for an as_of already logged with different hashes")
    common(append)
    verify = sub.add_parser("verify", help="check the chain (and optionally the files)")
    verify.add_argument("--check-files", action="store_true",
                        help="also re-hash the files under --data-root")
    common(verify)
    backfill = sub.add_parser("backfill", help="log existing past as_of dates, marked backfilled")
    backfill.add_argument("--from", dest="start", required=True)
    backfill.add_argument("--to", dest="end", required=True)
    common(backfill)
    return parser


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def cmd_append(args) -> int:
    try:
        result = append_as_of(_path(args.manifest), _path(args.data_root), args.as_of,
                              revision=args.revision,
                              code_commit=resolve_code_commit(ROOT))
    except ConflictError as exc:
        print(f"forward_log: refused: {exc}", file=sys.stderr)
        print(f"{STATUS_PREFIX}refused")
        return 1
    except ForwardLogError as exc:
        print(f"forward_log: {exc}", file=sys.stderr)
        print(f"{STATUS_PREFIX}error")
        return 2
    entry = result.entry
    print(f"forward_log: {result.status} as_of={entry['as_of']} seq={entry['seq']} "
          f"files={len(entry['files'])} entry_hash={entry['entry_hash']}")
    print(f"{STATUS_PREFIX}{result.status}")
    return 0


def cmd_verify(args) -> int:
    manifest = _path(args.manifest)
    entries, problems = load_manifest(manifest)
    file_note = "skipped"
    if args.check_files:
        if problems:
            file_note = "not checked (chain broken)"
        else:
            file_problems = verify_files(entries, _path(args.data_root))
            problems += file_problems
            file_note = "ok" if not file_problems else f"{len(file_problems)} mismatch(es)"
    head = entries[-1].get("entry_hash", "?") if entries and isinstance(entries[-1], dict) else "-"
    print(f"forward_log verify: entries={len(entries)} head={head} files={file_note}")
    for problem in problems:
        print(f"  BREAK: {problem}", file=sys.stderr)
    if problems:
        print(f"forward_log verify: FAILED ({len(problems)} problem(s))", file=sys.stderr)
        return 1
    print("forward_log verify: OK")
    return 0


def cmd_backfill(args) -> int:
    try:
        validate_as_of(args.start)
        validate_as_of(args.end)
        dates = list_as_of_dates(_path(args.data_root), args.start, args.end)
    except ForwardLogError as exc:
        print(f"forward_log: {exc}", file=sys.stderr)
        return 2
    if not dates:
        print("forward_log backfill: no as_of with scanner observations in range", file=sys.stderr)
        return 2
    print("forward_log backfill: entries are marked backfilled; they prove nothing about "
          "when the files existed before this append.")
    commit = resolve_code_commit(ROOT)
    failed = 0
    for as_of in dates:
        try:
            result = append_as_of(_path(args.manifest), _path(args.data_root), as_of,
                                  backfilled=True, code_commit=commit)
        except ForwardLogError as exc:
            failed += 1
            print(f"  {as_of}: not logged: {exc}", file=sys.stderr)
            continue
        print(f"  {as_of}: {result.status} seq={result.entry['seq']}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"append": cmd_append, "verify": cmd_verify, "backfill": cmd_backfill}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
