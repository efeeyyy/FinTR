"""Notification layout v2: one summary message, then one message per stock
whose condition was met (``docs/notify.md``).

Pure: no I/O, no network, no clock. Takes a saved
``fintr_scanner_observations_v1``/``_v2`` document and returns a list of
plain-text Turkish messages, each at most ``TELEGRAM_MESSAGE_LIMIT``
characters. Raw feature values never appear in a message (they stay in the
observation file and the receipt's input hash); only the readable percent
conversions below do.

Percent conversion (all four rules read ``vwap_proxy_vs_close``,
``docs/features.md``/``src/fintr/features/period.py``)::

    v       = (vwap_proxy[t] - close[t]) / close[t]        # the saved raw value
    pct     = -v / (1 + v) * 100  = (close - vwap_proxy) / vwap_proxy * 100

``pct`` is how far the close is from the day's average traded price
(``vwap_proxy``, an AKD-derived proxy), as a percent *of that average
price*. ``pct > 0``: the close is above it; ``pct < 0``: below it. The
wording ("üstünde"/"altında") follows the sign of ``v`` itself, never the
rule id. A frozen threshold ``v >= 0.01`` is therefore ``0.99%`` below the
average price and ``v <= -0.01`` is ``1.01%`` above it, which is why rule
lines say "~%1". ``top1_akd_net_share_of_volume`` is already a 0..1 share
and is shown as ``value * 100``. Both are rounded half-up to two decimals
with a Turkish decimal comma.

Direction wording follows ``fintr.notify.format``: only the frozen spec's
``predicted_direction`` (``directions``) may produce one of the two fixed
labels below (Efe's decision of 2026-10-02, personal use); an unresolved
rule gives "yön bilinmiyor", never a guess. ``directions=None`` is the
unlabelled layout (``--no-direction-label``): no arrow, no label, no
hypothesis sentence, no warning.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fintr.notify.explanations import RuleExplanations
from fintr.notify.format import TELEGRAM_MESSAGE_LIMIT, split_message
from fintr.scanner.observe import NOTICE_TEXT_TR, VALIDATION_STATUS


# The only buy/sell wording this layout may emit (Efe, 2026-10-02): the two
# direction labels, capitalised direction word as in the approved layout.
DIRECTION_LABELS_V2_TR = {
    "positive": "beklenen yön: YUKARI (al yönlü)",
    "negative": "beklenen yön: AŞAĞI (sat yönlü)",
}
DIRECTION_ARROWS = {"positive": "▲", "negative": "▼"}
DIRECTION_UNKNOWN_MARK = "•"
DIRECTION_UNKNOWN_TR = "yön bilinmiyor"
SUMMARY_WARNING_TR = (
    "⚠ Yön etiketi doğrulanmamış hipotezdir (test: Nisan 2027); yatırım tavsiyesi değildir."
)
# AGENTS.md (Efe, 2026-10-02): the label is given "in every message with the
# unverified-hypothesis warning". Every stock message that carries a label
# therefore has this fixed line right under its title.
STOCK_WARNING_TR = "⚠ Doğrulanmamış hipotez; yatırım tavsiyesi değildir."

_WEEKDAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
_FRESHNESS_FIELDS_TR = (
    ("last_akd_server_date", "AKD"),
    ("last_custody_date", "takas"),
    ("last_price_date", "fiyat"),
)
_SUPPRESSED_LABELS_TR = (("ratio_regime", "oran rejimi"), ("custody_scale_jump", "takas ölçek sıçraması"))
_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")


# --- conversions -----------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    """A finite ``Decimal`` from a saved feature value (a decimal string), else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def close_vs_vwap_percent(vwap_proxy_vs_close: Decimal) -> Decimal | None:
    """Signed percent of the close relative to ``vwap_proxy``: ``-v/(1+v)*100``
    for ``v = (vwap_proxy - close) / close``. Positive = close above the
    average price. None when ``1 + v <= 0`` (cannot come from a positive
    close and a positive average price)."""
    denominator = 1 + vwap_proxy_vs_close
    if denominator <= 0:
        return None
    return -vwap_proxy_vs_close / denominator * _HUNDRED


def share_percent(share: Decimal) -> Decimal | None:
    """``top1_akd_net_share_of_volume`` (a 0..1 share) as a percent; None outside 0..1."""
    if share < 0 or share > 1:
        return None
    return share * _HUNDRED


def format_percent_tr(value: Decimal) -> str:
    """``Decimal('1.5110')`` -> ``'%1,51'`` (two decimals, half-up, Turkish comma)."""
    rounded = abs(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    return "%" + f"{rounded:f}".replace(".", ",")


def format_date_tr(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _parse_date(text: object) -> date | None:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


def _date_text_tr(iso: str) -> str:
    parsed = _parse_date(iso)
    return format_date_tr(parsed) if parsed is not None else str(iso)


def _vwap_fragment(feature: dict) -> str:
    value = _decimal(feature.get("value"))
    pct = close_vs_vwap_percent(value) if value is not None else None
    if pct is None:
        return "kapanış–ortalama fiyat farkı okunamadı"
    if pct == 0:
        return "kapanış ortalama fiyata eşit"
    where = "üstünde" if pct > 0 else "altında"
    return f"kapanış ortalama fiyatın {format_percent_tr(pct)} {where}"


def _share_fragment(feature: dict) -> str:
    value = _decimal(feature.get("value"))
    pct = share_percent(value) if value is not None else None
    if pct is None:
        return "tek kurum net payı okunamadı"
    return f"tek kurum net payı {format_percent_tr(pct)} (günlük toplama oranı)"


_FEATURE_FRAGMENTS = (
    ("vwap_proxy_vs_close", _vwap_fragment),
    ("top1_akd_net_share_of_volume", _share_fragment),
)


# --- observation collection ------------------------------------------------


def _hits(document: dict) -> dict[tuple[str, str], list[dict]]:
    """(symbol, as_of) -> its ``condition_met is True`` observations, rule-id ordered."""
    hits: dict[tuple[str, str], list[dict]] = {}
    for symbol in sorted(document.get("symbols", {})):
        for scan in document["symbols"][symbol].get("scans", []):
            for obs in scan.get("observations", []):
                if obs.get("condition_met") is True:
                    hits.setdefault((symbol, scan.get("as_of", "")), []).append(obs)
    for observations in hits.values():
        observations.sort(key=lambda obs: str(obs.get("rule_id", "?")))
    return dict(sorted(hits.items()))


def _as_of_values(document: dict) -> list[str]:
    return sorted({
        scan["as_of"]
        for block in document.get("symbols", {}).values()
        for scan in block.get("scans", [])
        if scan.get("as_of")
    })


def _stock_direction(observations: list[dict], directions: dict | None) -> str | None:
    """``positive``/``negative`` when every hit resolves to the same frozen
    direction, else None (unknown or conflicting -- never guessed)."""
    resolved = set()
    for obs in observations:
        direction = directions.get(obs.get("rule_id", "?")) if directions is not None else None
        if direction not in DIRECTION_LABELS_V2_TR:
            return None
        resolved.add(direction)
    return resolved.pop() if len(resolved) == 1 else None


# --- summary message -------------------------------------------------------


def _header(document: dict) -> str:
    values = _as_of_values(document)
    if not values:
        return "FinTR · tarih bilinmiyor"
    parsed = [_parse_date(value) for value in values]
    if len(values) == 1:
        day = parsed[0]
        if day is None:
            return f"FinTR · {values[0]}"
        return f"FinTR · {format_date_tr(day)} ({_WEEKDAYS_TR[day.weekday()]} kapanışı)"
    return f"FinTR · {_date_text_tr(values[0])}–{_date_text_tr(values[-1])} ({len(values)} günlük tarama)"


def _data_signature(composition: dict | None) -> tuple:
    if composition is None:
        return ("none",)
    if "window" not in composition:
        return (
            "counts", composition.get("gaps", {}).get("count", 0),
            composition.get("unsettled", {}).get("day_count", 0),
            composition.get("revisions", {}).get("day_count", 0), True,
        )
    if composition["window"] is None:
        return ("nowindow",)
    return (
        "counts", composition.get("gaps", {}).get("in_window_count", 0),
        composition.get("unsettled", {}).get("in_window_day_count", 0),
        composition.get("revisions", {}).get("in_window_day_count", 0), False,
    )


def _signature_text(signature: tuple) -> str:
    if signature[0] == "none":
        return "durum bilgisi yok (günlük birleştirme olmadan tarandı)"
    if signature[0] == "nowindow":
        return "pencere yok (günlük kayıt yok)"
    _kind, gaps, unsettled, revisions, whole_period = signature
    parts = ["tam (boşluk 0)" if gaps == 0 else f"eksik (boşluk {gaps})"]
    parts.append(f"{unsettled} gün kesinleşmemiş" if unsettled else "kesinleşmemiş gün yok")
    if revisions:
        parts.append(f"{revisions} gün revize")
    if whole_period:
        parts.append("tüm dönem sayımı")
    return " · ".join(parts)


def _data_lines(document: dict) -> list[str]:
    """Aggregated data-status lines: one line when every stock has the same
    counts; otherwise the largest group on the first line and each differing
    group (symbols listed) on its own line."""
    symbols = sorted(document.get("symbols", {}))
    if not symbols:
        return []
    signatures = {
        symbol: _data_signature(document["symbols"][symbol].get("composition")) for symbol in symbols
    }
    groups = Counter(signatures.values())
    # Largest group first; ties broken by the signature text so output is deterministic.
    ordered = sorted(groups, key=lambda sig: (-groups[sig], _signature_text(sig)))
    main = ordered[0]
    if len(ordered) == 1:
        scope = "1 hisse" if len(symbols) == 1 else f"{len(symbols)} hissenin hepsi"
        return [f"Veri: {scope} {_signature_text(main)}"]
    lines = [f"Veri: {groups[main]}/{len(symbols)} hisse {_signature_text(main)}"]
    for signature in ordered[1:]:
        members = ", ".join(symbol for symbol in symbols if signatures[symbol] == signature)
        lines.append(f"Farklı: {members} — {_signature_text(signature)}")
    return lines


def _extra_lines(document: dict) -> list[str]:
    """Lines that appear only when there is something to report: missing
    freshness fields, suppressed observations, not-evaluable observations."""
    missing: list[str] = []
    suppressed: Counter = Counter()
    not_evaluable = 0
    for symbol in sorted(document.get("symbols", {})):
        for scan in document["symbols"][symbol].get("scans", []):
            freshness = scan.get("freshness", {})
            labels = [label for key, label in _FRESHNESS_FIELDS_TR if freshness.get(key) is None]
            if labels:
                suffix = f" {_date_text_tr(scan['as_of'])}" if scan.get("as_of") else ""
                missing.append(f"{symbol}{suffix} ({', '.join(labels)})")
            for obs in scan.get("observations", []):
                if obs.get("suppressed") in dict(_SUPPRESSED_LABELS_TR):
                    suppressed[obs["suppressed"]] += 1
                elif obs.get("condition_met") is None:
                    not_evaluable += 1
    lines: list[str] = []
    if missing:
        lines.append("Eksik veri: " + ", ".join(missing))
    if suppressed:
        lines.append("Bastırılan gözlem: " + ", ".join(
            f"{label} {suppressed[key]}" for key, label in _SUPPRESSED_LABELS_TR if suppressed[key]
        ))
    if not_evaluable:
        lines.append(f"Değerlendirilemeyen gözlem: {not_evaluable}")
    return lines


def _count_line(entries: int, up: int, down: int, unknown: int, *, labelled: bool, multi_day: bool) -> str:
    unit = "hisse-gün" if multi_day else "hisse"
    line = f"Koşulu sağlanan: {entries} {unit}"
    if not labelled:
        return line
    parts = []
    if up:
        parts.append(f"{up} yukarı")
    if down:
        parts.append(f"{down} aşağı")
    if unknown:
        parts.append(f"{unknown} yön bilinmiyor")
    return line + " — " + ", ".join(parts)


def _summary_message(document: dict, hits: dict, directions: dict | None) -> str:
    header = _header(document)
    data_lines = _data_lines(document)
    extra_lines = _extra_lines(document)
    if not hits:
        lines = [f"{header}: bugün hiçbir hissede koşul sağlanmadı", *data_lines, *extra_lines]
        return "\n".join(lines)
    stock_directions = [_stock_direction(observations, directions) for observations in hits.values()]
    up = stock_directions.count("positive")
    down = stock_directions.count("negative")
    unknown = len(stock_directions) - up - down
    multi_day = len(_as_of_values(document)) > 1
    labelled = directions is not None
    lines = [
        header,
        _count_line(len(hits), up, down, unknown, labelled=labelled, multi_day=multi_day),
        *data_lines,
        *extra_lines,
    ]
    if up or down:
        lines.append(SUMMARY_WARNING_TR)
    else:
        # No direction label anywhere (unlabelled layout, or every rule
        # unresolved): the scanner's own fixed notice, as in layout v1.
        lines.append(document.get("warning") or NOTICE_TEXT_TR)
        lines.append(f"Durum: {VALIDATION_STATUS}")
    return "\n".join(lines)


# --- per-stock message -----------------------------------------------------


def _today_fragments(observations: list[dict]) -> list[str]:
    """Readable fragments for the "Bugün" line, one per known feature, taken
    from the stock's hits (a feature shared by several rules shows once per
    distinct rendering)."""
    fragments: list[str] = []
    for name, render in _FEATURE_FRAGMENTS:
        for obs in observations:
            for feature in obs.get("features", []):
                if feature.get("name") == name:
                    text = render(feature)
                    if text not in fragments:
                        fragments.append(text)
    return fragments


def _stock_message(
    symbol: str, as_of: str, observations: list[dict], *, document: dict, rule_texts: dict,
    directions: dict | None, explanations: RuleExplanations | None, horizons: dict | None,
    multi_day: bool,
) -> str:
    direction = _stock_direction(observations, directions)
    where = f" ({_date_text_tr(as_of)})" if multi_day else ""
    if direction is not None:
        title = f"{DIRECTION_ARROWS[direction]} {symbol}{where} — {DIRECTION_LABELS_V2_TR[direction]}"
    elif directions is not None:
        title = f"{DIRECTION_UNKNOWN_MARK} {symbol}{where} — {DIRECTION_UNKNOWN_TR}"
    else:
        title = f"{symbol}{where} — koşul sağlandı"
    lines = [title]
    if direction is not None:
        lines.append(STOCK_WARNING_TR)
    rule_lines = explanations.rule_lines if explanations is not None else {}
    for obs in observations:
        rule_id = str(obs.get("rule_id", "?"))
        description = rule_lines.get(rule_id) or rule_texts.get(rule_id, rule_id)
        lines.append(f"Kural {rule_id}: {description}")
    fragments = _today_fragments(observations)
    lines.append("Bugün: " + (" · ".join(fragments) if fragments else "değer okunamadı"))
    version = str(document.get("rules_file_sha256", "bilinmiyor"))[:8]
    if direction is not None:
        meanings = explanations.meanings if explanations is not None else {}
        shown = [(str(obs.get("rule_id", "?")), meanings.get(str(obs.get("rule_id", "?"))))
                 for obs in observations]
        shown = [(rule_id, text) for rule_id, text in shown if text]
        for rule_id, text in shown:
            label = "Ne demek" if len(observations) == 1 else f"Ne demek ({rule_id})"
            lines.append(f"{label}: {text}")
        days = sorted({(horizons or {}).get(str(obs.get("rule_id", "?"))) for obs in observations} - {None})
        if len(days) == 1:
            lines.append(f"Vade: {days[0]} işlem günü · kural sürümü {version}")
        else:
            lines.append(f"Kural sürümü: {version}")
    else:
        lines.append(f"Kural sürümü: {version}")
    return "\n".join(lines)


# --- entry point -----------------------------------------------------------


def render_layout_v2(
    document: dict, rule_texts: dict, directions: dict | None = None,
    explanations: RuleExplanations | None = None, horizons: dict | None = None,
) -> list[str]:
    """Pure: ``document`` -> [summary, one message per stock with a met
    condition, in symbol order]. Order carries no meaning (no ranking).

    ``directions``: ``None`` = unlabelled layout; otherwise rule_id ->
    ``predicted_direction`` (an empty mapping marks every stock
    "yön bilinmiyor"). ``explanations``: fixed texts; a rule without a
    ``meaning`` just has no "Ne demek" line. ``horizons``: rule_id ->
    primary horizon in trading days (frozen spec), omitted when unknown."""
    hits = _hits(document)
    multi_day = len(_as_of_values(document)) > 1
    messages = [_summary_message(document, hits, directions)]
    for (symbol, as_of), observations in hits.items():
        messages.append(_stock_message(
            symbol, as_of, observations, document=document, rule_texts=rule_texts,
            directions=directions, explanations=explanations, horizons=horizons, multi_day=multi_day,
        ))
    result: list[str] = []
    for message in messages:
        result.extend(split_message(message, TELEGRAM_MESSAGE_LIMIT))
    return result
