"""Append one user-verified corporate-action fact to a symbol's registry.

The fact (type, lot multiplier, ex_date, custody_effective_date, source)
must come from a primary source the user read themselves; this project's
tools never fetch kap.org.tr or any other network source for it -- this
script only records the user's assertion and validates its structure via
``fintr.data.corporate_actions``. A local ``--source-file`` is hashed
(sha256) but never read remotely or uploaded anywhere.

Writes ``configs/corporate_actions/<SYMBOL>.json`` atomically (temp file +
rename in the same directory) and never partially overwrites an existing,
valid registry: any validation failure (duplicate action_id, duplicate or
overlapping ex_date, bad multiplier, custody_effective_date before ex_date)
aborts before touching the file. On success the appended entry is printed
for the user to confirm.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from fintr.data.corporate_actions import (
    ACTION_TYPES,
    CorporateActionsError,
    ENTERED_BY_USER,
    REGISTRY_SCHEMA,
    VERIFICATION_USER_ASSERTED,
    action_to_json,
    load_corporate_action_registry,
    parse_corporate_action_registry,
    registry_to_json,
)


DEFAULT_REGISTRY_ROOT = Path("configs/corporate_actions")


def _positive_decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid decimal: {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("--lot-multiplier must be a positive, finite decimal")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append one user-verified corporate-action fact to "
            "configs/corporate_actions/<SYMBOL>.json. Every field is a required "
            "argument (no derived defaults); this tool never fetches kap.org.tr "
            "or any other network source -- the fact must come from a primary "
            "source the user already read."
        ),
    )
    parser.add_argument("--symbol", required=True, help="e.g. AKFIS")
    parser.add_argument(
        "--action-id", required=True,
        help="unique id for this action within the symbol's registry, e.g. AKFIS-2026-bonus-1",
    )
    parser.add_argument("--type", required=True, choices=ACTION_TYPES)
    parser.add_argument(
        "--lot-multiplier", required=True, type=_positive_decimal,
        help="new lots per old lot, e.g. 6 for a 1-for-5 bonus issue",
    )
    parser.add_argument(
        "--ex-date", required=True,
        help="YYYY-MM-DD; first trading date in post-action units (affects AKD and price)",
    )
    parser.add_argument(
        "--custody-effective-date", required=True,
        help="YYYY-MM-DD; first custody date in post-action units (required explicitly, no default)",
    )
    parser.add_argument("--source-url", required=True, help="primary-source URL the user read")
    parser.add_argument("--source-published-date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--source-file", type=Path, default=None, metavar="PATH",
        help="a local file the user downloaded from the primary source; its sha256 is "
             "computed locally and stored -- the file itself is never read remotely or uploaded",
    )
    parser.add_argument("--notes", default="", help="free-text note (optional)")
    parser.add_argument(
        "--registry-root", type=Path, default=DEFAULT_REGISTRY_ROOT,
        help=f"directory holding <SYMBOL>.json registries (default: {DEFAULT_REGISTRY_ROOT})",
    )
    return parser


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    source_file_sha256: str | None = None
    if args.source_file is not None:
        try:
            source_file_sha256 = _file_sha256(args.source_file)
        except OSError as exc:
            print(f"Registration failed: --source-file could not be read: {exc}", file=sys.stderr)
            return 1

    entry = {
        "action_id": args.action_id,
        "type": args.type,
        "lot_multiplier": format(args.lot_multiplier, "f"),
        "ex_date": args.ex_date,
        "custody_effective_date": args.custody_effective_date,
        "source_url": args.source_url,
        "source_published_date": args.source_published_date,
        "source_file_sha256": source_file_sha256,
        "entered_by": ENTERED_BY_USER,
        "entered_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verification": VERIFICATION_USER_ASSERTED,
        "notes": args.notes,
    }

    registry_path = args.registry_root / f"{args.symbol}.json"
    if registry_path.exists():
        try:
            existing = load_corporate_action_registry(registry_path)
        except CorporateActionsError as exc:
            print(f"Registration failed: existing registry is invalid: {exc}", file=sys.stderr)
            return 1
        if existing.symbol != args.symbol:
            print(
                f"Registration failed: {registry_path} already holds actions for symbol "
                f"{existing.symbol!r}, not {args.symbol!r}", file=sys.stderr,
            )
            return 1
        raw_actions = [action_to_json(action) for action in existing.actions]
    else:
        raw_actions = []

    if any(raw["action_id"] == entry["action_id"] for raw in raw_actions):
        print(f"Registration failed: duplicate action_id {entry['action_id']!r}", file=sys.stderr)
        return 1

    candidate_document = {
        "schema": REGISTRY_SCHEMA, "symbol": args.symbol, "actions": raw_actions + [entry],
    }
    try:
        registry = parse_corporate_action_registry(candidate_document)
    except CorporateActionsError as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        return 1

    payload = (
        json.dumps(registry_to_json(registry), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    args.registry_root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".corporate-actions-", suffix=".tmp",
            dir=args.registry_root, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, registry_path)
        temporary = None
    except OSError as exc:
        print(f"Registration failed: could not write {registry_path}: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    saved_action = next(action for action in registry.actions if action.action_id == args.action_id)
    print(f"Registered {args.action_id!r} for {args.symbol} -> {registry_path}", file=sys.stderr)
    print(json.dumps(action_to_json(saved_action), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
