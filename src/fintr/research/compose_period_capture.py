"""ADR §3: compose one symbol's base + rolling-window period captures into
one in-memory ``PeriodCapture``, with full lineage.

Relationship to E4 (``fintr.research.capture_merge``): E4 merges several
multi-month period captures under a **whole-date** ownership rule (the
single latest-``captured_at_utc`` record owns every family for a date),
because those captures never disagree about scope -- they are simply
consecutive, lightly-overlapping requests for the same long history. This
composer serves a different case (daily rolling windows that deliberately
re-request the same recent days, to catch late-settling custody and
provider revisions) and so relaxes ownership to **per (date, family)**: see
rule 1 below. It reuses E4's canonical-JSON hashing
(``fintr.data.period_capture.canonical_json``) and, for the composed
capture's synthetic ``packet_sha256``, E4's "hash over the inputs' own
packet hashes" idiom -- but it does not call ``merge_period_captures``
or import its module-private helpers: the two ownership rules are not
interchangeable, and mixing them silently would violate rule 1. A saved
capture is never re-parsed or re-hashed here either -- every input must
already be a ``load_period_capture``-validated ``PeriodCapture`` (whose
own ``packet_sha256`` was already checked against its packet by that
loader), which is how "hash uyuşmazlığında red" (reject on hash mismatch)
is satisfied: by reuse, not by a second hash check here.

Rules (docs/research/daily_miniapp_collection_adr.md §3), briefly:

1. Each of the four dated series families (``akd``, ``akd_market``,
   ``custody``, ``metadata``) is selected independently, date by date: the
   input capture with the latest ``captured_at_utc`` that carries that
   date in that family wins the whole row for that date/family.
2. ``server_dates`` is the union of every input capture's own
   ``server_dates`` (not only the winners' -- an input can lose every
   family selection for a date and its ``server_dates`` membership still
   stands). A tradeable-calendar date not covered by any input is never
   synthesized; it is reported in ``gaps`` when a tradeable-dates calendar
   is supplied (optional, mirroring E4's ``tradeable_dates`` parameter).
3. A (date, family) seen in more than one input is a revision when the
   canonical-JSON hash of its row-set differs between the winner and a
   loser; recorded with the two inputs' own whole-capture
   ``packet_sha256`` (not a row-level hash, matching the ADR's schema
   field names) and the count of codes whose value differs (metadata rows
   are not code-keyed: 1 when the two day records differ at all, else no
   entry is recorded).
4. Settlement -- this composer's own operational reading of the ADR text,
   which states the intent but not an algorithm:
   - AKD (``akd``+``akd_market`` jointly, since both come from the same
     provider row -- see ``DailyMarketProxyRow``'s docstring): settled
     once the two temporally-latest inputs that carry the date agree
     (identical canonical-JSON row-set hash); a date seen in only one
     input is unsettled (not yet confirmed by a second observation).
   - Custody (``custody``): settled once some input's own
     ``server_dates`` reaches at least two dates strictly after the
     custody date (T+2) and that input's row-set hash for the date
     matches the winning hash.
   Every unsettled (date, family) pair is listed in ``unsettled``.
5. Period aggregates (``period_akd_net``, ``custodian_diff``) span a
   single requested period and cannot be combined; the composed capture
   carries neither (``period_aggregates_available=False``), the same
   convention E4 already uses for its merged capture (and that
   ``truncate_period_capture`` already drops for any capture, composed or
   not). ``PeriodCapture.__post_init__`` enforces this pairing.
6. ``brokerage_directory`` comes from the single input with the latest
   ``captured_at_utc`` overall (not per-family); which input is recorded
   in ``brokerage_directory_source``.
7. Pure function: no network, clock or randomness. The same set of input
   ``PeriodCapture`` objects, in any order, always produces the same
   composed capture and the same ``composition_sha256`` (inputs are
   sorted into one canonical processing order before anything is built).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
from typing import Sequence

from fintr.data.period_capture import PeriodCapture, canonical_json
from fintr.data.period_composition import (
    COMPOSER_VERSION,
    COMPOSITION_FAMILIES,
    COMPOSITION_SCHEMA,
    CompositionBrokerageSource,
    CompositionInputRecord,
    CompositionLineage,
    CompositionRevisionEntry,
    CompositionSelectionEntry,
    CompositionUnsettledDay,
)


_FAMILY_ATTR = {
    "akd": "daily_akd_net",
    "akd_market": "daily_akd_market",
    "custody": "daily_custody_lots",
    "metadata": "metadata",
}
_CODE_KEYED_FAMILIES = ("akd", "custody")


class CompositionError(ValueError):
    """The given captures cannot be composed without an unstated assumption."""


@dataclass(frozen=True, slots=True)
class CaptureInput:
    """One capture given to the composer, and which kind it is."""

    capture: PeriodCapture
    kind: str  # "base" | "rolling"
    label: str | None = None  # e.g. the source file path, for the report only

    def __post_init__(self) -> None:
        if self.kind not in ("base", "rolling"):
            raise CompositionError(f"invalid_input_kind: {self.kind}")


def _captured_at(capture: PeriodCapture) -> datetime:
    text = capture.captured_at_utc
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise CompositionError(f"unparseable_captured_at_utc: {text}") from None
    if parsed.tzinfo is None:
        raise CompositionError(f"captured_at_utc_without_timezone: {text}")
    return parsed


def _family_days(capture: PeriodCapture, family: str) -> dict[date, object]:
    return {row.date: row for row in getattr(capture, _FAMILY_ATTR[family])}


def _codevalue_payload(day) -> tuple:
    return tuple(
        {"code": row.code, "value": row.value} for row in sorted(day.rows, key=lambda r: r.code)
    )


def _marketproxy_payload(day) -> tuple:
    return tuple(
        {"code": row.code, "total_size": row.total_size, "total_cost": row.total_cost}
        for row in sorted(day.rows, key=lambda r: r.code)
    )


def _metadata_payload(day) -> dict:
    return {
        "retail_pct": day.retail_pct,
        "institutional_pct": day.institutional_pct,
        "null_fields": tuple(sorted(day.null_fields)),
        "investor_count": day.investor_count,
    }


def _family_row_hash(row, family: str) -> str:
    if family in _CODE_KEYED_FAMILIES:
        payload = _codevalue_payload(row)
    elif family == "akd_market":
        payload = _marketproxy_payload(row)
    elif family == "metadata":
        payload = _metadata_payload(row)
    else:
        raise CompositionError(f"invalid_family: {family}")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _row_value_repr(row, family: str) -> str:
    if family == "akd_market":
        return canonical_json({"total_size": row.total_size, "total_cost": row.total_cost})
    return canonical_json({"value": row.value})  # CodeValue: akd | custody


def _differing_code_count(loser_row, winner_row, family: str) -> int:
    if family == "metadata":
        return 0 if _metadata_payload(loser_row) == _metadata_payload(winner_row) else 1
    loser_map = {row.code: _row_value_repr(row, family) for row in loser_row.rows}
    winner_map = {row.code: _row_value_repr(row, family) for row in winner_row.rows}
    codes = set(loser_map) | set(winner_map)
    return sum(1 for code in codes if loser_map.get(code) != winner_map.get(code))


def _akd_settled(ordered: Sequence["CaptureInput"], day: date, family: str) -> bool:
    holders = sorted(
        (item for item in ordered if day in _family_days(item.capture, family)),
        key=lambda item: (_captured_at(item.capture), item.capture.packet_sha256),
    )
    if len(holders) < 2:
        return False
    latest_two = holders[-2:]
    hashes = {
        _family_row_hash(_family_days(item.capture, family)[day], family) for item in latest_two
    }
    return len(hashes) == 1


def _custody_settled(ordered: Sequence["CaptureInput"], day: date, winning_hash: str) -> bool:
    for item in ordered:
        days = _family_days(item.capture, "custody")
        if day not in days:
            continue
        later_server_dates = sum(1 for d in item.capture.server_dates if d > day)
        if later_server_dates >= 2 and _family_row_hash(days[day], "custody") == winning_hash:
            return True
    return False


def _unsettled_days(
    ordered: Sequence["CaptureInput"], composed_series: dict[str, dict[date, object]],
) -> tuple[CompositionUnsettledDay, ...]:
    unsettled: list[CompositionUnsettledDay] = []

    akd_days = sorted(set(composed_series["akd"]) | set(composed_series["akd_market"]))
    for day in akd_days:
        akd_ok = day not in composed_series["akd"] or _akd_settled(ordered, day, "akd")
        market_ok = (
            day not in composed_series["akd_market"] or _akd_settled(ordered, day, "akd_market")
        )
        if not (akd_ok and market_ok):
            unsettled.append(CompositionUnsettledDay(day, "akd"))

    for day, winning_row in sorted(composed_series["custody"].items()):
        winning_hash = _family_row_hash(winning_row, "custody")
        if not _custody_settled(ordered, day, winning_hash):
            unsettled.append(CompositionUnsettledDay(day, "custody"))

    return tuple(unsettled)


def compose_period_capture(
    inputs: Sequence[CaptureInput],
    *,
    tradeable_dates: Sequence[date] | None = None,
) -> PeriodCapture:
    """Compose one symbol's base + rolling captures; see module docstring."""
    if not inputs:
        raise CompositionError("no_captures")
    symbols = {item.capture.symbol for item in inputs}
    if len(symbols) != 1:
        raise CompositionError(f"symbol_mismatch: {','.join(sorted(symbols))}")
    symbol = next(iter(symbols))

    packet_hashes = [item.capture.packet_sha256 for item in inputs]
    if len(set(packet_hashes)) != len(packet_hashes):
        raise CompositionError("duplicate_capture_packet")

    # Canonical processing order, independent of the caller's input order
    # (rule 7): captured-at time, then packet hash as a total-order
    # tiebreaker (two distinct inputs cannot share both, since duplicate
    # packet hashes were just rejected above).
    ordered = tuple(sorted(
        inputs, key=lambda item: (_captured_at(item.capture), item.capture.packet_sha256),
    ))

    selection_entries: list[CompositionSelectionEntry] = []
    revision_entries: list[CompositionRevisionEntry] = []
    composed_series: dict[str, dict[date, object]] = {family: {} for family in COMPOSITION_FAMILIES}

    for family in COMPOSITION_FAMILIES:
        holders: dict[date, list[tuple[CaptureInput, object]]] = {}
        for item in ordered:
            for day, row in _family_days(item.capture, family).items():
                holders.setdefault(day, []).append((item, row))

        for day in sorted(holders):
            # ``ordered`` already sorts by (captured_at, packet_sha256), and
            # dict insertion below follows that same order, so the last
            # entry is the winner without re-sorting.
            entries = holders[day]
            winner_item, winner_row = entries[-1]
            composed_series[family][day] = winner_row
            selection_entries.append(
                CompositionSelectionEntry(day, family, winner_item.capture.packet_sha256)
            )
            winner_hash = _family_row_hash(winner_row, family)
            for loser_item, loser_row in entries[:-1]:
                loser_hash = _family_row_hash(loser_row, family)
                if loser_hash != winner_hash:
                    revision_entries.append(CompositionRevisionEntry(
                        date=day, family=family,
                        superseded_packet_sha256=loser_item.capture.packet_sha256,
                        winning_packet_sha256=winner_item.capture.packet_sha256,
                        differing_code_count=_differing_code_count(loser_row, winner_row, family),
                    ))

    # Rule 2: server_dates = union of every input's own server_dates.
    server_dates = tuple(sorted(set().union(
        *(set(item.capture.server_dates) for item in ordered)
    )))

    start = min(item.capture.start for item in ordered)
    end = max(item.capture.end for item in ordered)

    gaps: tuple[date, ...] = ()
    if tradeable_dates is not None:
        covered = set(server_dates)
        gaps = tuple(sorted(
            day for day in tradeable_dates if start <= day <= end and day not in covered
        ))

    unsettled_entries = _unsettled_days(ordered, composed_series)

    latest_item = max(
        ordered, key=lambda item: (_captured_at(item.capture), item.capture.packet_sha256),
    )
    brokerage_source = CompositionBrokerageSource(
        kind=latest_item.kind, captured_at_utc=latest_item.capture.captured_at_utc,
        packet_sha256=latest_item.capture.packet_sha256,
    )

    input_records = tuple(
        CompositionInputRecord(
            kind=item.kind, packet_sha256=item.capture.packet_sha256,
            captured_at_utc=item.capture.captured_at_utc, label=item.label,
        )
        for item in ordered
    )

    lineage_body = {
        "schema": COMPOSITION_SCHEMA,
        "symbol": symbol,
        "composer_version": COMPOSER_VERSION,
        "inputs": [
            {"kind": r.kind, "packet_sha256": r.packet_sha256,
             "captured_at_utc": r.captured_at_utc, "label": r.label}
            for r in input_records
        ],
        "selection": [
            {"date": e.date.isoformat(), "family": e.family, "packet_sha256": e.packet_sha256}
            for e in selection_entries
        ],
        "gaps": [day.isoformat() for day in gaps],
        "revisions": [
            {"date": r.date.isoformat(), "family": r.family,
             "superseded_packet_sha256": r.superseded_packet_sha256,
             "winning_packet_sha256": r.winning_packet_sha256,
             "differing_code_count": r.differing_code_count}
            for r in revision_entries
        ],
        "unsettled": [
            {"date": u.date.isoformat(), "family": u.family} for u in unsettled_entries
        ],
        "brokerage_directory_source": {
            "kind": brokerage_source.kind, "captured_at_utc": brokerage_source.captured_at_utc,
            "packet_sha256": brokerage_source.packet_sha256,
        },
        "period_aggregates_available": False,
    }
    composition_sha256 = hashlib.sha256(canonical_json(lineage_body).encode("utf-8")).hexdigest()

    lineage = CompositionLineage(
        schema=COMPOSITION_SCHEMA, symbol=symbol, composer_version=COMPOSER_VERSION,
        inputs=input_records, selection=tuple(selection_entries), gaps=gaps,
        revisions=tuple(revision_entries), unsettled=unsettled_entries,
        brokerage_directory_source=brokerage_source,
        period_aggregates_available=False, composition_sha256=composition_sha256,
    )

    composed = replace(
        latest_item.capture,
        start=start, end=end,
        packet_sha256=composition_sha256,
        server_dates=server_dates,
        daily_akd_net=tuple(
            composed_series["akd"][day] for day in sorted(composed_series["akd"])
        ),
        daily_akd_market=tuple(
            composed_series["akd_market"][day] for day in sorted(composed_series["akd_market"])
        ),
        daily_custody_lots=tuple(
            composed_series["custody"][day] for day in sorted(composed_series["custody"])
        ),
        metadata=tuple(
            composed_series["metadata"][day] for day in sorted(composed_series["metadata"])
        ),
        period_akd_net=(), custodian_diff=(),
        custodian_diff_start=start, custodian_diff_end=end,
        composition=lineage,
    )
    return composed
