"""Input packet for the daily analysis note.

Raw data, not conclusions: for each symbol that has a ``condition_met: true``
observation, the composed rolling-window data (per-institution AKD and
custody rows with institution names, provider metadata), the scanner's
observation entries and feature values, data-status counts and the frozen
rules' own texts. Nothing after the symbol's ``as_of`` enters the packet
(``truncate_period_capture`` plus an explicit date filter); no price series,
return or post-observation price is computed or included (K4); the
observation file's ``generated_at_utc`` and file paths (which carry capture
end dates) are left out. Holdout and excluded symbols are refused with the
same guards the scanner and notifier use.

Numbers are carried as decimal strings (exact; ``format(value, "f")``).
Whole symbols are dropped in sorted-symbol order when the encoded packet
would exceed ``max_packet_bytes``; they are reported in ``omitted`` (never
silently), and are not among ``required_assessments``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from fintr.data.period_capture import PeriodCapture, resolve_brokerage_label
from fintr.llm_note.numbers import ISO_DATE
from fintr.notify.format import SUPPORTED_OBSERVATIONS_SCHEMAS
from fintr.research.features import truncate_period_capture
from fintr.scanner.rules import ScannerRule


PACKET_SCHEMA = "fintr_llm_note_packet_v1"
AKD_COLUMNS = ["net_size", "total_size", "total_cost"]


class PacketError(ValueError):
    """The packet could not be built. ``category`` is a stable slug."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}" if detail else category)
        self.category = category


class GuardRefusal(PacketError):
    """A holdout or excluded symbol was present; nothing may be sent."""


@dataclass(frozen=True, slots=True)
class PacketBuild:
    packet: dict
    packet_bytes: bytes
    omitted: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def included_symbols(self) -> list[str]:
        return sorted(self.packet["symbols"])


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def encode_packet(packet: dict) -> bytes:
    return (json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _positive_scans(block: dict) -> list[dict]:
    return [
        scan for scan in block.get("scans", [])
        if any(obs.get("condition_met") is True for obs in scan.get("observations", []))
    ]


def _observation_entry(obs: dict) -> dict:
    features = []
    for feature in obs.get("features", []):
        entry = {
            key: feature.get(key)
            for key in ("name", "formula", "value", "data_families", "min_date", "max_date")
        }
        if feature.get("institution_labels"):
            entry["institution_labels"] = feature["institution_labels"]
        features.append(entry)
    return {
        "rule_id": obs.get("rule_id"), "family_role": obs.get("family_role"),
        "as_of": obs.get("as_of"), "suppressed": obs.get("suppressed"),
        "condition_met": obs.get("condition_met"),
        "not_evaluable_reason": obs.get("not_evaluable_reason"),
        "features": features, "validation_status": obs.get("validation_status"),
    }


def _window_start(composition: dict | None, capture: PeriodCapture, cutoff: date,
                  fallback_days: int) -> date:
    window = (composition or {}).get("window")
    if isinstance(window, dict) and isinstance(window.get("start"), str):
        return date.fromisoformat(window["start"])
    dates = [d for d in capture.server_dates if d <= cutoff]
    return dates[-fallback_days] if len(dates) >= fallback_days else (dates[0] if dates else cutoff)


def _composition_summary(composition: dict | None, start: date, cutoff: date) -> dict | None:
    if composition is None:
        return None

    def in_window(text: str) -> bool:
        return start <= date.fromisoformat(text) <= cutoff

    gaps = [d for d in composition.get("gaps", {}).get("dates", []) if in_window(d)]
    unsettled = [e for e in composition.get("unsettled", {}).get("entries", []) if in_window(e["date"])]
    revisions = [e for e in composition.get("revisions", {}).get("entries", []) if in_window(e["date"])]
    return {
        "composition_sha256": composition.get("composition_sha256"),
        "gap_dates_in_window": gaps,
        "unsettled_in_window": [{"date": e["date"], "family": e["family"]} for e in unsettled],
        "revised_in_window": [{"date": e["date"], "family": e["family"]} for e in revisions],
    }


def _symbol_block(symbol: str, block: dict, capture: PeriodCapture, cutoff: date,
                  fallback_days: int) -> dict:
    truncated = truncate_period_capture(capture, cutoff)
    composition = block.get("composition")
    start = _window_start(composition, capture, cutoff, fallback_days)

    net = {day.date: day.by_code() for day in truncated.daily_akd_net}
    market = {day.date: day.by_code() for day in truncated.daily_akd_market}
    custody = {day.date: day.by_code() for day in truncated.daily_custody_lots}
    days: dict[str, dict] = {}
    codes: set[str] = set()
    for day in sorted(set(net) | set(market) | set(custody)):
        if not start <= day <= cutoff:
            continue
        akd_rows: dict[str, list] = {}
        for code in sorted(set(net.get(day, {})) | set(market.get(day, {}))):
            proxy = market.get(day, {}).get(code)
            akd_rows[code] = [
                _num(net.get(day, {}).get(code)),
                _num(proxy.total_size) if proxy else None,
                _num(proxy.total_cost) if proxy else None,
            ]
        custody_rows = {code: _num(value) for code, value in sorted(custody.get(day, {}).items())}
        codes.update(akd_rows, custody_rows)
        days[day.isoformat()] = {"akd": akd_rows, "custody": custody_rows}

    metadata = {
        day.date.isoformat(): {
            "retail_pct": _num(day.retail_pct), "institutional_pct": _num(day.institutional_pct),
            "investor_count": day.investor_count, "null_fields": list(day.null_fields),
        }
        for day in truncated.metadata if start <= day.date <= cutoff
    }
    institutions: dict[str, dict] = {}
    for code in sorted(codes):
        label = resolve_brokerage_label(capture.brokerage_directory, code)
        institutions[code] = (
            {"title": label["title"], "short_title": label["short_title"]}
            if label["title"] else {"status": label["status"]}
        )

    scans = [
        {
            "as_of": scan["as_of"], "freshness": scan.get("freshness"),
            "data_status": scan.get("data_status"),
            "observations": [_observation_entry(obs) for obs in scan.get("observations", [])],
        }
        for scan in _positive_scans(block)
    ]
    return {
        "cutoff": cutoff.isoformat(), "scans": scans, "days": days,
        "metadata_by_day": metadata, "institutions": institutions,
        "composition": _composition_summary(composition, start, cutoff),
    }


def _rule_entries(rule_ids: set[str], rules: tuple[ScannerRule, ...], rule_texts: dict,
                  project_root: Path) -> dict:
    by_id = {rule.rule_id: rule for rule in rules}
    entries: dict[str, dict] = {}
    for rule_id in sorted(rule_ids):
        rule = by_id.get(rule_id)
        if rule is None:
            raise PacketError("unknown_rule", rule_id)
        spec_json = json.loads((project_root / rule.spec_path).read_text(encoding="utf-8"))
        entries[rule_id] = {
            "description": rule_texts.get(rule_id), "family_role": rule.family_role,
            "predicted_direction": rule.spec.preregistration.predicted_direction,
            "condition": spec_json.get("condition"),
            # dates in the frozen texts (e.g. the freeze date) are masked so no
            # date after ``as_of`` is in the packet
            "spec_description": ISO_DATE.sub("[tarih]", rule.spec.description),
        }
    return entries


def build_packet(
    *, as_of: date, document: dict, observations_sha256: str,
    rules: tuple[ScannerRule, ...], rule_texts: dict, project_root: Path, guards,
    load_capture: Callable[[str], PeriodCapture],
    max_packet_bytes: int, fallback_window_days: int,
) -> PacketBuild:
    """See the module docstring. Raises ``PacketError`` (``GuardRefusal`` for
    a holdout/excluded symbol)."""
    if not isinstance(document, dict) or document.get("schema") not in SUPPORTED_OBSERVATIONS_SCHEMAS:
        raise PacketError("observations_schema")
    all_symbols = document.get("symbols", {})
    blocked = sorted(s for s in all_symbols if guards.is_excluded(s) or guards.is_holdout(s))
    if blocked:
        raise GuardRefusal("guard_symbol", ",".join(blocked))

    candidates: dict[str, dict] = {}
    for symbol in sorted(all_symbols):
        scans = _positive_scans(all_symbols[symbol])
        if not scans:
            continue
        scan_dates = [date.fromisoformat(scan["as_of"]) for scan in scans]
        if max(scan_dates) > as_of:
            raise PacketError("scan_after_as_of", symbol)
        candidates[symbol] = {"block": all_symbols[symbol], "cutoff": max(scan_dates)}
    if not candidates:
        raise PacketError("no_observations")

    blocks: dict[str, dict] = {}
    required: dict[str, list[dict]] = {}
    for symbol, info in candidates.items():
        try:
            capture = load_capture(symbol)
        except ValueError as exc:
            raise PacketError("data_load_failed", str(exc)) from None
        blocks[symbol] = _symbol_block(symbol, info["block"], capture, info["cutoff"], fallback_window_days)
        required[symbol] = sorted(
            (
                {"symbol": symbol, "rule_id": obs["rule_id"], "as_of": scan["as_of"]}
                for scan in _positive_scans(info["block"]) for obs in scan["observations"]
                if obs.get("condition_met") is True
            ),
            key=lambda item: (item["as_of"], item["rule_id"]),
        )

    included = sorted(blocks)
    omitted: list[dict] = []
    while True:
        rule_ids = {item["rule_id"] for symbol in included for item in required[symbol]}
        packet = {
            "schema": PACKET_SCHEMA, "as_of": as_of.isoformat(),
            "observation_file": {
                "schema": document["schema"], "sha256": observations_sha256,
                "rules_file_sha256": document.get("rules_file_sha256"),
            },
            "columns": {
                "akd": AKD_COLUMNS,
                "custody": "lot_count per institution code (custody balance)",
            },
            "rules": _rule_entries(rule_ids, rules, rule_texts, project_root),
            "required_assessments": [item for symbol in included for item in required[symbol]],
            "symbols": {symbol: blocks[symbol] for symbol in included},
        }
        payload = encode_packet(packet)
        if len(payload) <= max_packet_bytes:
            return PacketBuild(packet, payload, tuple(omitted))
        if len(included) <= 1:
            raise PacketError("packet_too_large", f"{len(payload)} > {max_packet_bytes}")
        dropped = included.pop()
        omitted.append({"symbol": dropped, "reason": "packet_size_limit"})
