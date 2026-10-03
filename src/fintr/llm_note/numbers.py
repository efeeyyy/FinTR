"""Number extraction and normalisation for the note's number audit.

Every number a model writes must exist in the input packet -- for a reason,
under that reason's own ``evidence_paths`` (``validate.py``). Text is
compared in a normalised form so that formatting differences do not cause
false rejections, but values are never rounded or computed:

- thousand/decimal separators: ``1.234,56`` / ``1,234.56`` / ``1234.56``;
  a lone ``.``/``,`` followed by exactly three digits is read both ways
  (decimal and thousand grouping) and accepted if either reading exists in
  the packet;
- the sign is matched: a ``-``/``−`` directly before the digits (not after a
  letter or digit, so ``10-15`` and ``H2-kural`` carry no sign) makes the
  token negative; a negative packet value matches only a negative token and
  a positive one only an unsigned token. "net satış 5" for a packet value
  ``-5`` is therefore rejected: the model must quote ``-5`` (a documented
  limit: strict sign matching, not semantic reading of the wording);
- a number marked with ``%`` matches the packet value ``p`` exactly, or the
  fraction ``p / 100`` only against a packet value with ``|v| <= 1`` (a
  ratio printed as a percentage);
- rounding is accepted only at the quoted precision: a quoted number with at
  least one decimal place (``10,46``, ``%5,12``) matches a packet value that
  rounds to it at that many decimals (sign included; ``-10.46`` for
  ``-10.4567``, ``5,12%`` for ``0.051234``). Integers stay exact (``5`` does
  not match ``4.6``), so a coarse quote cannot match a wide range of values.
  More than ``MAX_DECIMALS`` decimal places in a quoted number is refused as
  a raw, unrounded value;
- ISO dates are separate: each must be a date string present in the packet;
- spelled-out numbers cannot be audited, so ``find_number_words`` flags the
  common Turkish number words and ``yüzde <word>`` ("bir" is not flagged: it
  is also the indefinite article; fractions such as "yarı" are not either --
  documented limits).

``mask_names`` blanks known identifiers (symbols, rule ids, institution
names) before extraction so ``H2`` or a broker title with a digit is not
read as a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation
import re


ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_PLAIN_NUMBER = re.compile(r"[+\-−]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?")
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER_WORDS = (
    "sıfır", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on", "yirmi", "otuz",
    "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan", "yüz", "bin", "milyon", "milyar",
    "trilyon", "yarım", "çeyrek",
)
_NUMBER_WORD = re.compile(r"(?<!\w)(?:" + "|".join(_NUMBER_WORDS) + r")(?!\w)")
_PERCENT_WORD = re.compile(r"(?<!\w)yüzde\s+\w+")
_TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})


@dataclass(frozen=True, slots=True)
class NumberToken:
    raw: str
    percent: bool
    negative: bool
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
        start = match.start()
        negative = start > 0 and text[start - 1] in "-−" and (start == 1 or not text[start - 2].isalnum())
        tokens.append(NumberToken(match.group(0), percent, negative, _candidates(match.group(0))))
    return tokens


def find_number_words(text: str) -> list[str]:
    """Spelled-out numbers and ``yüzde <word>`` in ``text`` (names already
    masked). Not flagged: "bir" (also the article) -- a documented limit."""
    lowered = text.translate(_TR_LOWER).lower()
    return sorted(set(_NUMBER_WORD.findall(lowered)) | set(_PERCENT_WORD.findall(lowered)))


def normalise(value: Decimal) -> Decimal:
    """Signed canonical form (``5.0`` == ``5``; ``-0`` == ``0``)."""
    return value.normalize()


# Packet fields whose digits are not data: hashes, schema names, rule ids and
# symbols, formula and validation-status prose, column documentation. Their numbers must not widen
# what the model may quote.
SKIP_KEYS = frozenset({"schema", "formula", "validation_status", "columns", "rule_id", "symbol"})


def packet_number_set(
    packet: object, *, mask: list[str] = (),
) -> tuple[set[Decimal], set[str]]:
    """(allowed signed values, allowed ISO date strings) of a packet or subtree.

    Numeric JSON values and pure-number strings (the packet's canonical
    decimal form) are taken exactly, with their sign; any other string or key
    is read with ``extract_numbers`` (all readings allowed) after ``mask``
    names (symbols, rule ids, institution names) are blanked. Values under
    ``SKIP_KEYS`` and any ``*sha256`` key are ignored."""
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
            numbers.update(normalise(-c if token.negative else c) for c in token.candidates)

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


_CTX = Context(prec=120)
MAX_DECIMALS = 4  # more decimal places than this in a quoted number = a raw, unrounded value


def decimals_of(candidate: Decimal) -> int:
    exponent = candidate.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def too_many_decimals(token: NumberToken) -> bool:
    """True if every reading of the token has more than ``MAX_DECIMALS``
    decimal places (a raw 30-digit value pasted into the text)."""
    return bool(token.candidates) and all(decimals_of(c) > MAX_DECIMALS for c in token.candidates)


def _rounds_to(value: Decimal, quoted: Decimal, places: int) -> bool:
    """``value`` rounded to ``places`` decimals (half-up or half-even; the
    sign is kept) equals ``quoted``."""
    unit = Decimal(1).scaleb(-places)
    if abs(value - quoted) > unit:
        return False
    return any(value.quantize(unit, rounding=mode, context=_CTX) == quoted
               for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN))


def number_in_packet(token: NumberToken, allowed: set[Decimal]) -> bool:
    """Exact match of the signed value; or, for a quoted number with at least
    one decimal place, a packet value that rounds to it at the quoted
    precision (sign included). Integers stay exact. With ``%``: ``p`` against
    a packet value (exact/rounded), or ``p / 100`` against a packet value with
    ``|v| <= 1`` -- i.e. ``round(v * 100, places) == p``."""
    for candidate in token.candidates:
        signed = -candidate if token.negative else candidate
        places = decimals_of(candidate)
        if normalise(signed) in allowed:
            return True
        # a ratio printed as a percentage: only against a packet value |v| <= 1
        if token.percent and abs(signed) <= 100 and normalise(signed / 100) in allowed:
            return True
        if places >= 1:
            if any(_rounds_to(value, signed, places) for value in allowed):
                return True
            if token.percent and any(
                abs(value) <= 1 and _rounds_to(value * 100, signed, places) for value in allowed
            ):
                return True
    return False
