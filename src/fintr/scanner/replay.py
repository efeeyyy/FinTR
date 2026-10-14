"""Weekly replay reconciliation of saved scanner observations (E3).

Re-runs the frozen scanner (``scripts/scan_observations.py``) on stored data
for earlier as_of dates and compares the result field by field with the saved
``<scanner-dir>/<as_of>/observations.json``. A difference is code drift, a
rule change or a data revision; it is detected and classified here, never
ignored. This module computes no financial figure: it only diffs two
already-serialized scanner documents. See ``docs/scanner.md`` ("Haftalık
replay").

Two replay modes (design: ``docs/scanner.md``):

- ``point-in-time``: the scanner is re-run with ``--data-cutoff-utc`` set to
  the original document's ``generated_at_utc``, so only period/daily captures
  (``captured_at_utc``) and price-history files (``accessed_at_utc``) that
  already existed then are used. Later T+2 custody revisions and metadata
  fills are therefore excluded; for unchanged rules and code, any difference
  is drift (or the original input files were replaced/deleted since).
- ``current``: all data on disk now. Differences are data revisions and/or
  drift; they are separated per symbol by whether the recorded input files
  (path tail + sha256) are the same.

Classification is per symbol and uses only recorded provenance: identical
input set + differing output -> ``code_drift`` (``rule_change`` when
``rules_file_sha256`` also differs); differing input set -> ``data_revision``
in ``current`` mode and ``input_set_mismatch`` in ``point-in-time`` mode
(point-in-time selection could not reproduce the original input set). When
the inputs differ, code drift cannot be separated from the data change in
that symbol.

Volatile fields (``generated_at_utc``, ``python_version``, ``code_commit``),
the replay-only ``data_cutoff_utc`` and the replay's explicit ``as_of_source``
are reported but not counted as differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import tempfile
from typing import Any, Callable, Iterable

from fintr.scanner.report import RESULT_SCHEMA_V2, resolve_code_commit


REPLAY_REPORT_SCHEMA = "fintr_scanner_replay_report_v1"

MODE_POINT_IN_TIME = "point-in-time"
MODE_CURRENT = "current"
MODES = (MODE_POINT_IN_TIME, MODE_CURRENT)

# Reported (original vs replay) but never counted as differences.
VOLATILE_DOCUMENT_FIELDS = ("generated_at_utc", "python_version", "code_commit")
# Present only in a replayed document / only meaningful for the replay run.
REPLAY_ONLY_DOCUMENT_FIELDS = ("data_cutoff_utc",)

EXIT_IDENTICAL = 0
EXIT_ERROR = 1
EXIT_DIFFERENCES = 2

DEFAULT_MAX_DIFFS_PER_UNIT = 500

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCAN_SCRIPT = PROJECT_ROOT / "scripts" / "scan_observations.py"

_AS_OF_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING = object()

CLASS_IDENTICAL = "identical"
CLASS_CODE_DRIFT = "code_drift"
CLASS_RULE_CHANGE = "rule_change"
CLASS_DATA_REVISION = "data_revision"
CLASS_INPUT_SET_MISMATCH = "input_set_mismatch"
CLASS_DOCUMENT_CHANGE = "document_change"

# (exit code, stdout text, stderr text)
ScanRunner = Callable[[list[str]], tuple[int, str, str]]


class ReplayError(Exception):
    """A replay unit could not be produced (reported, never swallowed)."""


@dataclass(frozen=True)
class ReplayConfig:
    scanner_dir: Path
    period_dir: Path
    price_dir: Path
    daily_root: Path
    as_of: tuple[str, ...] = ()      # explicit labels; empty -> last_n
    last_n: int = 5
    modes: tuple[str, ...] = (MODE_POINT_IN_TIME, MODE_CURRENT)
    project_root: Path = PROJECT_ROOT
    max_diffs_per_unit: int = DEFAULT_MAX_DIFFS_PER_UNIT


# --------------------------------------------------------------------------
# saved documents
# --------------------------------------------------------------------------

def list_saved_as_of(scanner_dir: Path) -> list[str]:
    """Ascending ``YYYY-MM-DD`` labels that have an ``observations.json``."""
    if not scanner_dir.is_dir():
        return []
    return sorted(
        entry.name for entry in scanner_dir.iterdir()
        if entry.is_dir() and _AS_OF_DIR_RE.match(entry.name)
        and (entry / "observations.json").is_file()
    )


def select_as_of(config: ReplayConfig) -> list[str]:
    available = list_saved_as_of(config.scanner_dir)
    if config.as_of:
        missing = [label for label in config.as_of if label not in available]
        if missing:
            raise ReplayError(f"no saved observations.json for as_of: {', '.join(missing)}")
        return sorted(set(config.as_of))
    return available[-config.last_n:]


def load_saved_document(scanner_dir: Path, label: str) -> dict:
    path = scanner_dir / label / "observations.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"cannot read {path} ({exc})") from exc
    if not isinstance(document, dict) or not isinstance(document.get("symbols"), dict):
        raise ReplayError(f"{path} is not a scanner observations document")
    return document


# --------------------------------------------------------------------------
# scan runner
# --------------------------------------------------------------------------

def _load_scan_module():
    spec = importlib.util.spec_from_file_location("fintr_replay_scan_observations", SCAN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_scan_script(argv: list[str]) -> tuple[int, str, str]:
    """Run ``scripts/scan_observations.py`` in-process with ``argv`` and
    capture its JSON stdout (the same code path as the saved scans)."""
    module = _load_scan_module()
    stdout = io.BytesIO()
    wrapper = io.TextIOWrapper(stdout, encoding="utf-8", write_through=True)
    stderr = io.StringIO()
    with redirect_stdout(wrapper), redirect_stderr(stderr):
        code = module.main(argv)
    wrapper.flush()
    return code, stdout.getvalue().decode("utf-8"), stderr.getvalue()


# --------------------------------------------------------------------------
# normalization / flattening
# --------------------------------------------------------------------------

def _tail(path: str, parts: int) -> str:
    text = str(path)
    pure = PureWindowsPath(text) if "\\" in text else PurePosixPath(text)
    return "/".join(pure.parts[-parts:])


def _scalar_list(value: Any) -> bool:
    return isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value)


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key in value:
            _flatten(value[key], f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list) and not _scalar_list(value):
        for index, item in enumerate(value):
            _flatten(item, f"{prefix}[{index}]", out)
    else:
        out[prefix] = value  # scalars and scalar lists compared whole


def _entry_strings(entries: list[dict]) -> list[str]:
    return sorted(f"{entry.get('date')}/{entry.get('family')}" for entry in entries)


def _symbol_input_set(block: dict) -> dict[str, str]:
    """Recorded input files of one symbol: ``kind:<symbol dir>/<file>`` -> sha256."""
    files: dict[str, str] = {}
    for kind, info in (block.get("input_files") or {}).items():
        files[f"{kind}:{_tail(info.get('path', ''), 2)}"] = info.get("sha256")
    composition = block.get("composition") or {}
    for info in composition.get("inputs") or []:
        files[f"composition.{info.get('kind')}:{_tail(info.get('path', ''), 2)}"] = info.get("sha256")
    return files


def flatten_document(document: dict) -> dict[tuple[str | None, str | None, str | None, str], Any]:
    """Map ``(symbol, as_of, rule_id, field)`` -> value for every compared leaf.

    Volatile / replay-only fields are excluded (see module docstring); paths
    are reduced to their stable tails so a replay run from another directory
    compares equal.
    """
    out: dict[tuple, Any] = {}

    def put(symbol, as_of, rule, field, value):
        out[(symbol, as_of, rule, field)] = value

    skip = set(VOLATILE_DOCUMENT_FIELDS) | set(REPLAY_ONLY_DOCUMENT_FIELDS) | {"symbols"}
    for key, value in document.items():
        if key in skip:
            continue
        if key == "rules_file":
            value = _tail(value, 3)
        leaves: dict[str, Any] = {}
        _flatten(value, "", leaves)
        for sub, leaf in leaves.items():
            put(None, None, None, f"document.{key}" + (f".{sub}" if sub else ""), leaf)

    for symbol, block in document["symbols"].items():
        for kind, info in (block.get("input_files") or {}).items():
            put(symbol, None, None, f"input_files.{kind}.path", _tail(info.get("path", ""), 2))
            put(symbol, None, None, f"input_files.{kind}.sha256", info.get("sha256"))

        composition = block.get("composition")
        if composition is not None:
            for info in composition.get("inputs") or []:
                name = f"{info.get('kind')}:{_tail(info.get('path', ''), 2)}"
                put(symbol, None, None, f"composition.inputs[{name}].sha256", info.get("sha256"))
            for key, value in composition.items():
                if key == "inputs":
                    continue
                if key in ("unsettled", "revisions"):
                    value = dict(value)
                    value["entries"] = _entry_strings(value.get("entries") or [])
                leaves = {}
                _flatten(value, "", leaves)
                for sub, leaf in leaves.items():
                    put(symbol, None, None, f"composition.{key}" + (f".{sub}" if sub else ""), leaf)

        for scan in block.get("scans") or []:
            as_of = scan.get("as_of")
            leaves = {}
            _flatten(scan.get("freshness") or {}, "", leaves)
            for sub, leaf in leaves.items():
                put(symbol, as_of, None, f"freshness.{sub}", leaf)
            if "data_status" in scan:
                leaves = {}
                _flatten(scan["data_status"], "", leaves)
                for sub, leaf in leaves.items():
                    put(symbol, as_of, None, f"data_status.{sub}", leaf)
            for obs in scan.get("observations") or []:
                rule = obs.get("rule_id")
                for key, value in obs.items():
                    if key in ("rule_id", "features"):
                        continue
                    put(symbol, as_of, rule, f"observation.{key}", value)
                seen: dict[str, int] = {}
                for feature in obs.get("features") or []:
                    name = str(feature.get("name"))
                    seen[name] = seen.get(name, 0) + 1
                    label = name if seen[name] == 1 else f"{name}#{seen[name]}"
                    leaves = {}
                    _flatten({k: v for k, v in feature.items() if k != "name"}, "", leaves)
                    for sub, leaf in leaves.items():
                        put(symbol, as_of, rule, f"observation.features.{label}.{sub}", leaf)
    return out


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _sorted_key(item: tuple) -> tuple:
    symbol, as_of, rule, field = item
    return (symbol or "", as_of or "", rule or "", field)


COMPOSITION_HASH_FIELD = "composition.composition_sha256"


def _raw_input_spellings(block: dict | None) -> list[str] | None:
    """Literal input path strings of a symbol's composition block (they are
    hashed into ``composition_sha256`` as labels)."""
    composition = (block or {}).get("composition")
    if composition is None:
        return None
    return sorted(f"{item.get('kind')}|{item.get('path')}" for item in composition.get("inputs") or [])


def not_comparable_symbols(original: dict, replay: dict) -> list[str]:
    """Symbols whose ``composition_sha256`` cannot be compared: the composer
    hashes the input *path spellings*, so a replay run with differently
    spelled (e.g. absolute vs relative) directories gets a different hash for
    identical content. The per-input sha256 values and every other field are
    still compared; only this derived hash is skipped (and listed)."""
    result = []
    for symbol, block in original["symbols"].items():
        other = replay["symbols"].get(symbol)
        if other is None or "composition" not in block:
            continue
        if _raw_input_spellings(block) != _raw_input_spellings(other):
            result.append(symbol)
    return result


def diff_documents(original: dict, replay: dict) -> list[dict]:
    """Field-level differences (``kind``: changed / missing_in_replay / extra_in_replay)."""
    left = flatten_document(original)
    right = flatten_document(replay)
    skip = {(symbol, None, None, COMPOSITION_HASH_FIELD)
            for symbol in not_comparable_symbols(original, replay)}
    diffs: list[dict] = []
    for key in sorted(set(left) | set(right), key=_sorted_key):
        if key in skip:
            continue
        a = left.get(key, _MISSING)
        b = right.get(key, _MISSING)
        if a is not _MISSING and b is not _MISSING and a == b:
            continue
        symbol, as_of, rule, field = key
        if a is _MISSING:
            kind = "extra_in_replay"
        elif b is _MISSING:
            kind = "missing_in_replay"
        else:
            kind = "changed"
        diffs.append({
            "symbol": symbol, "as_of": as_of, "rule": rule, "field": field, "kind": kind,
            "original": None if a is _MISSING else a,
            "replay": None if b is _MISSING else b,
        })
    return diffs


def volatile_report(original: dict, replay: dict | None) -> dict:
    report: dict[str, dict] = {}
    for field in VOLATILE_DOCUMENT_FIELDS:
        a = original.get(field)
        b = None if replay is None else replay.get(field)
        report[field] = {"original": a, "replay": b, "changed": replay is not None and a != b}
    return report


def input_set_changes(original_block: dict, replay_block: dict) -> dict:
    """Recorded-input comparison for one symbol (path tail + sha256)."""
    before = _symbol_input_set(original_block)
    after = _symbol_input_set(replay_block)
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "sha256_changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


def _inputs_differ(changes: dict) -> bool:
    return bool(changes["added"] or changes["removed"] or changes["sha256_changed"])


def classify_symbols(
    original: dict, replay: dict, diffs: list[dict], *, mode: str, rules_changed: bool,
) -> dict[str, dict]:
    """Per symbol with at least one difference: class + recorded-input changes."""
    by_symbol: dict[str, int] = {}
    for diff in diffs:
        if diff["symbol"] is not None:
            by_symbol[diff["symbol"]] = by_symbol.get(diff["symbol"], 0) + 1
    result: dict[str, dict] = {}
    for symbol, count in sorted(by_symbol.items()):
        orig_block = original["symbols"].get(symbol)
        replay_block = replay["symbols"].get(symbol)
        if orig_block is None or replay_block is None:
            result[symbol] = {
                "class": CLASS_INPUT_SET_MISMATCH if mode == MODE_POINT_IN_TIME else CLASS_DATA_REVISION,
                "diff_count": count, "input_changes": None,
                "note": "symbol present in only one document",
            }
            continue
        changes = input_set_changes(orig_block, replay_block)
        if _inputs_differ(changes):
            klass = CLASS_INPUT_SET_MISMATCH if mode == MODE_POINT_IN_TIME else CLASS_DATA_REVISION
        else:
            klass = CLASS_RULE_CHANGE if rules_changed else CLASS_CODE_DRIFT
        result[symbol] = {"class": klass, "diff_count": count, "input_changes": changes}
    return result


def _count(items: Iterable[str | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item)] = counts.get(str(item), 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def summarize_diffs(diffs: list[dict], symbol_classes: dict[str, dict]) -> dict:
    triples = {(d["symbol"], d["rule"], d["field"]) for d in diffs}
    return {
        "differing_symbol_rule_field_count": len(triples),
        "by_field": _count(d["field"] for d in diffs),
        "by_rule": _count(d["rule"] for d in diffs),
        "by_symbol": _count(d["symbol"] for d in diffs),
        "by_kind": _count(d["kind"] for d in diffs),
        "by_class": _count(info["class"] for info in symbol_classes.values()),
    }


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def _scan_groups(original: dict) -> list[tuple[str, int, list[str]]]:
    """Symbols grouped by (last scan as_of, number of scans) so one scanner run
    reproduces each symbol's own scan window (default-as_of originals may
    differ per symbol)."""
    groups: dict[tuple[str, int], list[str]] = {}
    for symbol, block in original["symbols"].items():
        scans = block.get("scans") or []
        if not scans:
            raise ReplayError(f"{symbol} has no scans in the saved document")
        groups.setdefault((scans[-1]["as_of"], len(scans)), []).append(symbol)
    return [(as_of, n, symbols) for (as_of, n), symbols in sorted(groups.items())]


def build_scan_argv(
    original: dict, config: ReplayConfig, *, mode: str, as_of: str, lookback: int, symbols: list[str],
) -> list[str]:
    rules_file = Path(str(original.get("rules_file", "")).replace("\\", "/"))
    if not rules_file.is_file():  # recorded cwd-relative; fall back to this checkout
        rules_file = config.project_root / rules_file
    argv = [
        "--symbols", ",".join(symbols),
        "--as-of", as_of,
        "--lookback-days", str(lookback),
        "--period-dir", str(config.period_dir),
        "--price-dir", str(config.price_dir),
        "--rules-file", str(rules_file),
        "--format", "json",
    ]
    if original.get("schema") == RESULT_SCHEMA_V2:
        argv += ["--daily-root", str(config.daily_root)]
    if mode == MODE_POINT_IN_TIME:
        generated = original.get("generated_at_utc")
        if not generated:
            raise ReplayError("saved document has no generated_at_utc; cannot replay point-in-time")
        argv += ["--data-cutoff-utc", str(generated)]
    return argv


def rerun_scan(original: dict, config: ReplayConfig, mode: str, runner: ScanRunner) -> dict:
    merged: dict | None = None
    symbols_out: dict[str, dict] = {}
    for as_of, lookback, symbols in _scan_groups(original):
        argv = build_scan_argv(
            original, config, mode=mode, as_of=as_of, lookback=lookback, symbols=symbols,
        )
        code, stdout, stderr = runner(argv)
        if code != 0:
            last = (stderr.strip().splitlines() or ["no message"])[-1]
            raise ReplayError(f"scanner exited {code} for as_of={as_of}: {last}")
        try:
            document = json.loads(stdout)
        except ValueError as exc:
            raise ReplayError(f"scanner output is not JSON ({exc})") from exc
        if merged is None:
            merged = document
        symbols_out.update(document["symbols"])
    assert merged is not None
    merged["symbols"] = {s: symbols_out[s] for s in original["symbols"] if s in symbols_out}
    return merged


def replay_unit(
    label: str, original: dict, config: ReplayConfig, mode: str, runner: ScanRunner,
) -> dict:
    unit: dict[str, Any] = {"as_of": label, "mode": mode}
    try:
        replay = rerun_scan(original, config, mode, runner)
    except ReplayError as exc:
        unit.update({"status": "error", "error": str(exc),
                     "volatile": volatile_report(original, None)})
        return unit

    diffs = diff_documents(original, replay)
    rules_changed = original.get("rules_file_sha256") != replay.get("rules_file_sha256")
    classes = classify_symbols(original, replay, diffs, mode=mode, rules_changed=rules_changed)
    document_diffs = [d for d in diffs if d["symbol"] is None]
    volatile = volatile_report(original, replay)
    cap = config.max_diffs_per_unit
    unit.update({
        "status": "identical" if not diffs else "diff",
        "error": None,
        "diff_count": len(diffs),
        "rules_file_sha256": {
            "original": original.get("rules_file_sha256"),
            "replay": replay.get("rules_file_sha256"),
            "equal": not rules_changed,
        },
        "code_commit": {
            "original": original.get("code_commit"), "replay": replay.get("code_commit"),
            "changed": original.get("code_commit") != replay.get("code_commit"),
        },
        "volatile": volatile,
        "data_cutoff_utc": replay.get("data_cutoff_utc"),
        "document_level_diff_count": len(document_diffs),
        "composition_sha256_not_comparable": not_comparable_symbols(original, replay),
        "symbol_classes": classes,
        "summary": summarize_diffs(diffs, classes),
        "diffs": diffs[:cap],
        "diffs_truncated": max(0, len(diffs) - cap),
    })
    return unit


def run_replay(config: ReplayConfig, runner: ScanRunner = run_scan_script) -> dict:
    """Replay every selected as_of in every requested mode; never raises for a
    single failed unit (it is recorded as ``status: error``)."""
    labels = select_as_of(config)
    if not labels:
        raise ReplayError(f"no saved observations.json under {config.scanner_dir}")
    units: list[dict] = []
    for label in labels:
        try:
            original = load_saved_document(config.scanner_dir, label)
        except ReplayError as exc:
            for mode in config.modes:
                units.append({"as_of": label, "mode": mode, "status": "error", "error": str(exc)})
            continue
        for mode in config.modes:
            units.append(replay_unit(label, original, config, mode, runner))
    totals = {
        "as_of": len(labels),
        "units": len(units),
        "identical": sum(1 for u in units if u["status"] == "identical"),
        "diff": sum(1 for u in units if u["status"] == "diff"),
        "errors": sum(1 for u in units if u["status"] == "error"),
    }
    return {
        "schema": REPLAY_REPORT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_commit": resolve_code_commit(config.project_root),
        "python_version": platform.python_version(),
        "modes": list(config.modes),
        "as_of": labels,
        "scanner_dir": str(config.scanner_dir),
        "totals": totals,
        "units": units,
    }


def exit_code(report: dict) -> int:
    totals = report["totals"]
    if totals["errors"]:
        return EXIT_ERROR
    return EXIT_DIFFERENCES if totals["diff"] else EXIT_IDENTICAL


def summary_line(report: dict) -> str:
    totals = report["totals"]
    return (f"REPLAY: as_of={totals['as_of']} identical={totals['identical']} "
            f"diff={totals['diff']} errors={totals['errors']}")


def unit_line(unit: dict) -> str:
    head = f"{unit['as_of']} {unit['mode']}: {unit['status']}"
    if unit["status"] == "error":
        return f"{head} ({unit['error']})"
    if unit["status"] == "identical":
        return head
    by_class = unit["summary"]["by_class"]
    classes = ", ".join(f"{name}={count}" for name, count in by_class.items()) or "document-level only"
    return (f"{head} (diffs={unit['diff_count']}, symbols: {classes}; "
            f"rules_sha_equal={unit['rules_file_sha256']['equal']}, "
            f"code_commit_changed={unit['code_commit']['changed']})")


# --------------------------------------------------------------------------
# report output (never overwrites)
# --------------------------------------------------------------------------

def check_report_target(path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)


def write_report(path: Path, report: dict) -> Path:
    """Write ``report`` as JSON; raises ``FileExistsError`` instead of overwriting."""
    check_report_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".replay-", suffix=".tmp", dir=path.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)  # fails if the target appeared meanwhile
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
