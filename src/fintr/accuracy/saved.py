"""Saved scanner predictions and their forward-log provenance.

A saved ``scanner_daily/<as_of>/observations.json`` already holds the live
scanner's prediction for ``as_of``, written by the pipeline before the outcome
existed. It is preferred over a reconstruction (``prediction_source``):

- ``forward_log``: the file's SHA-256 matches the latest hash-chained
  ``forward_log/manifest.jsonl`` entry for that ``as_of`` and that entry was NOT
  ``backfilled`` (a backfilled entry only shows the file existed when it was
  appended, ``docs/forward_log.md``), and the manifest chain verifies;
- ``saved_scan``: the file exists and is a scanner document with the same rules
  hash, but there is no verified, non-backfilled forward-log entry for it.

A file whose ``rules_file_sha256`` differs from the current rules is not used
(the rules changed), and an unreadable file is skipped with a note; both fall
back to reconstruction. Nothing here computes a financial figure.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re

from fintr.accuracy.core import (
    SOURCE_FORWARD_LOG,
    SOURCE_SAVED_SCAN,
    SavedPrediction,
)
from fintr.forward_log.core import load_manifest

_AS_OF_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OBSERVATIONS_NAME = "observations.json"
_SCHEMAS = ("fintr_scanner_observations_v1", "fintr_scanner_observations_v2")


def _latest_entries(manifest_path: Path | None) -> tuple[dict[str, dict], list[str]]:
    """``as_of -> latest manifest entry`` and the chain problems (any problem
    makes the forward log unusable as proof)."""
    if manifest_path is None or not Path(manifest_path).exists():
        return {}, ["manifest_missing"]
    entries, problems = load_manifest(Path(manifest_path))
    latest: dict[str, dict] = {}
    for entry in entries:
        as_of = entry.get("as_of")
        if isinstance(as_of, str) and (as_of not in latest or entry["seq"] > latest[as_of]["seq"]):
            latest[as_of] = entry
    return latest, problems


def load_saved_predictions(
    scanner_dir: Path, *, days: tuple[date, ...], rules_file_sha256: str,
    forward_manifest: Path | None,
) -> tuple[dict, dict]:
    """``(predictions, provenance)`` for the saved scans whose ``as_of`` is in ``days``.

    ``predictions`` maps ``(symbol, rule_id, as_of)`` to ``SavedPrediction``;
    ``provenance`` maps ``as_of`` ISO label to a record of the file, its hash,
    the document's own ``generated_at_utc``, the forward-log entry used (if any)
    and notes on why a saved file was not used.
    """
    scanner_dir = Path(scanner_dir)
    latest, problems = _latest_entries(forward_manifest)
    chain_ok = not problems
    predictions: dict = {}
    provenance: dict = {}
    wanted = {day.isoformat() for day in days}
    if not scanner_dir.is_dir():
        return predictions, provenance

    for directory in sorted(scanner_dir.iterdir()):
        label = directory.name
        if label not in wanted or not directory.is_dir() or not _AS_OF_DIR.match(label):
            continue
        path = directory / OBSERVATIONS_NAME
        if not path.is_file():
            continue
        record: dict = {"file": str(path), "used": False, "notes": []}
        provenance[label] = record
        try:
            raw = path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            record["notes"].append("unreadable_saved_scan")
            continue
        record["sha256"] = hashlib.sha256(raw).hexdigest()
        if not isinstance(document, dict) or document.get("schema") not in _SCHEMAS \
                or not isinstance(document.get("symbols"), dict):
            record["notes"].append("not_a_scanner_document")
            continue
        record["generated_at_utc"] = document.get("generated_at_utc")
        if document.get("rules_file_sha256") != rules_file_sha256:
            record["notes"].append("rules_hash_differs_not_used")
            continue

        source = SOURCE_SAVED_SCAN
        entry = latest.get(label)
        if not forward_manifest:
            record["notes"].append("no_forward_log_manifest_given")
        elif not chain_ok:
            record["notes"].append("forward_log_chain_problem_not_trusted")
        elif entry is None:
            record["notes"].append("no_forward_log_entry")
        else:
            logged = {f.get("path"): f.get("sha256") for f in entry.get("files", [])}
            relative = f"scanner_daily/{label}/{OBSERVATIONS_NAME}"
            record["forward_log"] = {
                "seq": entry.get("seq"), "appended_at_utc": entry.get("appended_at_utc"),
                "backfilled": entry.get("backfilled"), "revision": entry.get("revision"),
                "entry_hash": entry.get("entry_hash"),
            }
            if logged.get(relative) != record["sha256"]:
                record["notes"].append("forward_log_hash_differs_from_file")
            elif entry.get("backfilled"):
                record["notes"].append("forward_log_entry_is_backfilled_not_proof_of_timing")
            else:
                source = SOURCE_FORWARD_LOG
        record["source"] = source
        record["used"] = True

        for symbol, block in document["symbols"].items():
            for scan in block.get("scans", []):
                if scan.get("as_of") != label:
                    continue  # lookback day of a multi-day file: not this day's own scan
                for observation in scan.get("observations", []):
                    rule_id = observation.get("rule_id")
                    if not isinstance(rule_id, str):
                        continue
                    met = observation.get("condition_met")
                    predictions[(symbol, rule_id, date.fromisoformat(label))] = SavedPrediction(
                        source=source,
                        condition_met=met if isinstance(met, bool) else None,
                        suppressed=observation.get("suppressed"),
                    )
    return predictions, provenance
