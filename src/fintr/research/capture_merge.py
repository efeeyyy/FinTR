"""E4: merge several saved period captures of one symbol into one timeline.

AKD/custody requests cover ~3 months each, so a one-year study needs several
captures per symbol. Rules (narrowest reading of docs/prereg/README.md E4):

- Precedence: the capture with the later ``captured_at_utc`` wins. It must
  also be the later capture by requested period (``start``, ``end``); when
  capture-time order and period order disagree, "later capture" is
  ambiguous and the merge is refused rather than guessed.
- A date is owned as a whole by the highest-precedence capture that carries
  it (in ``server_dates`` or in any dated series); every dated row
  (``daily_akd_net``, ``daily_akd_market``, ``daily_custody_lots``,
  ``metadata``) for that date comes from the owner only -- rows are never
  mixed code-by-code across captures. This lets a later capture replace the
  provisional last custody day (T+2 revision) of an earlier one.
- Period aggregates (``period_akd_net``, ``custodian_diff``) span a single
  requested period and cannot be combined; the merged capture carries none
  (no research feature reads them -- ``truncate_period_capture`` drops them
  too).
- Nothing is interpolated: a gap between captures stays a gap and is
  reported per seam (requested-period calendar gap, and, when a price
  calendar is supplied, the tradeable price dates with no server date).
- Every input's packet SHA-256 (and file SHA-256 when given) is kept in the
  report; the merged ``packet_sha256`` is a SHA-256 over those, in
  precedence order, not a provider packet hash.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
from typing import Sequence

from fintr.data.period_capture import PeriodCapture, canonical_json


MERGE_FORMULA_VERSION = "period_capture_merge_v1"

_DATED_SERIES = ("daily_akd_net", "daily_akd_market", "daily_custody_lots", "metadata")


class CaptureMergeError(ValueError):
    """The captures cannot be merged without an unstated assumption."""


@dataclass(frozen=True, slots=True)
class CaptureSource:
    capture: PeriodCapture
    label: str | None = None  # e.g. the file path, for the report only
    file_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MergedCapture:
    capture: PeriodCapture
    report: dict


def _captured_at(capture: PeriodCapture) -> datetime:
    text = capture.captured_at_utc
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise CaptureMergeError(f"unparseable_captured_at_utc: {text}") from None
    if parsed.tzinfo is None:
        raise CaptureMergeError(f"captured_at_utc_without_timezone: {text}")
    return parsed


def _covered_dates(capture: PeriodCapture) -> set[date]:
    dates = set(capture.server_dates)
    for name in _DATED_SERIES:
        dates.update(row.date for row in getattr(capture, name))
    return dates


def _series_rows(capture: PeriodCapture, name: str, day: date):
    for row in getattr(capture, name):
        if row.date == day:
            return row
    return None


def merge_period_captures(
    sources: Sequence[CaptureSource], *, tradeable_dates: Sequence[date] | None = None,
) -> MergedCapture:
    """Merge one symbol's captures; see the module docstring for the rules.

    A single source is returned unchanged (with a report), so a one-capture
    study is byte-for-byte the pre-E4 behaviour.
    """
    if not sources:
        raise CaptureMergeError("no_captures")
    symbols = {source.capture.symbol for source in sources}
    if len(symbols) != 1:
        raise CaptureMergeError(f"symbol_mismatch: {','.join(sorted(symbols))}")
    packet_hashes = [source.capture.packet_sha256 for source in sources]
    if len(set(packet_hashes)) != len(packet_hashes):
        raise CaptureMergeError("duplicate_capture_packet")

    ordered = sorted(sources, key=lambda source: _captured_at(source.capture))
    for earlier, later in zip(ordered, ordered[1:]):
        if _captured_at(earlier.capture) == _captured_at(later.capture):
            raise CaptureMergeError("equal_captured_at_utc: precedence undefined")
        if (earlier.capture.start, earlier.capture.end) > (later.capture.start, later.capture.end):
            raise CaptureMergeError(
                "capture_time_and_period_order_disagree: "
                f"{earlier.capture.start}..{earlier.capture.end} captured before "
                f"{later.capture.start}..{later.capture.end}"
            )

    # Highest precedence (latest) first when resolving each date's owner.
    covered = [_covered_dates(source.capture) for source in ordered]
    all_dates = sorted(set().union(*covered))
    owner_index: dict[date, int] = {}
    for day in all_dates:
        owner_index[day] = max(i for i, dates in enumerate(covered) if day in dates)

    overlap_dates = []
    for day in all_dates:
        holders = [i for i, dates in enumerate(covered) if day in dates]
        if len(holders) < 2:
            continue
        owner = owner_index[day]
        revised = sorted({
            name for i in holders if i != owner for name in _DATED_SERIES
            if _series_rows(ordered[i].capture, name, day)
            != _series_rows(ordered[owner].capture, name, day)
        })
        overlap_dates.append({
            "date": day.isoformat(),
            "captures": [ordered[i].label for i in holders],
            "owner": ordered[owner].label,
            "series_differing_from_owner": revised,
        })

    seams = []
    for earlier, later in zip(ordered, ordered[1:]):
        prev_cap, next_cap = earlier.capture, later.capture
        prev_last = prev_cap.server_dates[-1] if prev_cap.server_dates else None
        next_first = next_cap.server_dates[0] if next_cap.server_dates else None
        seam = {
            "previous": earlier.label,
            "next": later.label,
            "previous_requested_end": prev_cap.end.isoformat(),
            "next_requested_start": next_cap.start.isoformat(),
            "calendar_days_between_requested_periods": max(0, (next_cap.start - prev_cap.end).days - 1),
            "requested_periods_contiguous_or_overlapping": (next_cap.start - prev_cap.end).days <= 1,
            "previous_last_server_date": None if prev_last is None else prev_last.isoformat(),
            "next_first_server_date": None if next_first is None else next_first.isoformat(),
            "missing_tradeable_dates": None,
        }
        if tradeable_dates is not None and prev_last is not None and next_first is not None:
            seam["missing_tradeable_dates"] = [
                d.isoformat() for d in tradeable_dates
                if prev_last < d < next_first and d not in owner_index
            ]
        seam["gap_detected"] = (
            not seam["requested_periods_contiguous_or_overlapping"]
            or bool(seam["missing_tradeable_dates"])
        )
        seams.append(seam)

    source_rows = []
    for rank, source in enumerate(ordered):
        capture = source.capture
        source_rows.append({
            "precedence_rank": rank,  # higher wins
            "label": source.label,
            "file_sha256": source.file_sha256,
            "packet_sha256": capture.packet_sha256,
            "captured_at_utc": capture.captured_at_utc,
            "requested_start": capture.start.isoformat(),
            "requested_end": capture.end.isoformat(),
            "server_date_count": len(capture.server_dates),
            "owned_date_count": sum(1 for i in owner_index.values() if i == rank),
            "overridden_date_count": sum(
                1 for day in covered[rank] if owner_index[day] != rank
            ),
        })

    if len(ordered) == 1:
        merged = ordered[0].capture
    else:
        latest = ordered[-1].capture
        server_dates = tuple(sorted(
            day for day in all_dates if day in ordered[owner_index[day]].capture.server_dates
        ))
        series = {
            name: tuple(
                row for day in all_dates
                if (row := _series_rows(ordered[owner_index[day]].capture, name, day)) is not None
            )
            for name in _DATED_SERIES
        }
        start = min(source.capture.start for source in ordered)
        end = max(source.capture.end for source in ordered)
        merged = replace(
            latest,
            start=start,
            end=end,
            packet_sha256=hashlib.sha256(canonical_json({
                "formula_version": MERGE_FORMULA_VERSION,
                "packet_sha256_in_precedence_order": [s.capture.packet_sha256 for s in ordered],
            }).encode("utf-8")).hexdigest(),
            server_dates=server_dates,
            period_akd_net=(),
            custodian_diff_start=start,
            custodian_diff_end=end,
            custodian_diff=(),
            **series,
        )

    report = {
        "formula_version": MERGE_FORMULA_VERSION,
        "symbol": next(iter(symbols)),
        "capture_count": len(ordered),
        "precedence_rule": (
            "later captured_at_utc wins a whole date (all dated series); capture-time "
            "order must equal requested-period order"
        ),
        "sources": source_rows,
        "overlap_dates": overlap_dates,
        "seams": seams,
        "any_seam_gap": any(seam["gap_detected"] for seam in seams),
        "merged_start": merged.start.isoformat(),
        "merged_end": merged.end.isoformat(),
        "merged_server_date_count": len(merged.server_dates),
        "merged_packet_sha256": merged.packet_sha256,
        "period_aggregates_dropped": len(ordered) > 1,
    }
    return MergedCapture(merged, report)
