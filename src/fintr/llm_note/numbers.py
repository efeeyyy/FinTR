"""Number extraction and normalisation for the note's number audit.

Every number a model writes must exist in the input packet. Text is
compared in a normalised form so that formatting differences do not cause
false rejections, but values are never rounded or computed:

- thousand/decimal separators: ``1.234,56`` / ``1,234.56`` / ``1234.56``;
  a lone ``.``/``,`` followed by exactly three digits is read both ways
  (decimal and thousand grouping) and accepted if either reading exists in
  the packet;
- sign is ignored (the magnitude must exist; ``-5`` and ``5`` match);
- a number marked with ``%`` matches the packet value ``v`` or ``v / 100``
  (a fraction printed as a percentage), exactly -- no rounding tolerance;
- ISO dates are separate: each must be a date string present in the packet.

``mask_names`` blanks known identifiers (symbols, rule ids, institution
names) before extraction so ``H2`` or a broker title with a digit is not
read as a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_PLAIN_NUMBER = re.compile(r"[+\-−]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?")
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass(frozen=True, slots=True)
class NumberToken:
    raw: str
    percent: bool
    candidates: tuple[Decimal, ...]  # empty: unparseable


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _candidates(raw: str) -> tuple[Decimal, ...]:
    if "." not in raw and "," not in raw:
        value = _dec(raw)
        return (value,) if value is not None else ()
    has_dot, has_comma = "." in raw, "," in raw
    found: list[Decimal] = []
    if has_dot and has_comma:
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousand_sep = "," if decimal_sep == "." else "."
        head, _, tail = raw.rpartition(decimal_sep)
        if thousand_sep in tail:
            return ()
        groups = head.split(thousand_sep)
        if not all(len(group) == 3 for group in groups[1:]) or not groups[0]:
            return ()
        value = _dec("".join(groups) + "." + tail)
        return (value,) if value is not None else ()
    sep = "." if has_dot else ","
    parts = raw.split(sep)
    grouped = all(len(part) == 3 for part in parts[1:])
    if len(parts) > 2:
        if not grouped:
            return ()
        value = _dec("".join(parts))
        return (value,) if value is not None else ()
    decimal_reading = _dec(parts[0] + "." + parts[1])
    if decimal_reading is not None:
        found.append(decimal_reading)
    if grouped:
        grouped_reading = _dec("".join(parts))
        if grouped_reading is not None:
            found.append(grouped_reading)
    return tuple(found)


def mask_names(text: str, names: list[str]) -> str:
    """Replace each known name (longest first, case-insensitive) by a space."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def extract_dates(text: str) -> tuple[list[str], str]:
    """(ISO dates found, text with them replaced by spaces)."""
    dates = ISO_DATE.findall(text)
    return dates, ISO_DATE.sub(" ", text)


def extract_numbers(text: str) -> list[NumberToken]:
    """Every number in ``text`` (dates must already be removed)."""
    tokens: list[NumberToken] = []
    for match in _TOKEN.finditer(text):
        before = text[:match.start()].rstrip("+-−").rstrip()
        after = text[match.end():match.end() + 3].lstrip()
        percent = before.endswith("%") or after.startswith("%")
        tokens.append(NumberToken(match.group(0), percent, _candidates(match.group(0))))
    return tokens


def normalise(value: Decimal) -> Decimal:
    return abs(value).normalize()


# Packet fields whose digits are not data: hashes, schema names, rule ids and
# symbols, formula and validation-status prose, column documentation. Their numbers must not widen
# what the model may quote.
SKIP_KEYS = frozenset({"schema", "formula", "validation_status", "columns", "rule_id", "symbol"})


def packet_number_set(
    packet: object, *, mask: list[str] = (),
) -> tuple[set[Decimal], set[str]]:
    """(allowed magnitudes, allowed ISO date strings) of one packet.

    Numeric JSON values and pure-number strings (the packet's canonical
    decimal form) are taken exactly; any other string or key is read with
    ``extract_numbers`` (all readings allowed) after ``mask`` names (symbols,
    rule ids, institution names) are blanked. Values under ``SKIP_KEYS`` and
    any ``*sha256`` key are ignored."""
    numbers: set[Decimal] = set()
    dates: set[str] = set()

    def add_text(text: str) -> None:
        if _PLAIN_NUMBER.fullmatch(text):
            value = _dec(text.replace("−", "-"))
            if value is not None:
                numbers.add(normalise(value))
            return
        found, rest = extract_dates(text)
        dates.update(found)
        for token in extract_numbers(mask_names(rest, list(mask))):
            numbers.update(normalise(c) for c in token.candidates)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in SKIP_KEYS or str(key).endswith("sha256"):
                    continue
                add_text(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, Decimal)):
            numbers.add(normalise(Decimal(node)))
        elif isinstance(node, float):
            value = _dec(repr(node))
            if value is not None:
                numbers.add(normalise(value))
        elif isinstance(node, str):
            add_text(node)

    walk(packet)
    return numbers, dates


def number_in_packet(token: NumberToken, allowed: set[Decimal]) -> bool:
    for candidate in token.candidates:
        magnitude = normalise(candidate)
        if magnitude in allowed:
            return True
        if token.percent and normalise(candidate / 100) in allowed:
            return True
    return False
