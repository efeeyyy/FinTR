"""Offline tests for notification layout v2 (``fintr.notify.layout``,
``explanations``, ``delivery``, ``guard``) and the layout-v2 paths of
``scripts/notify_observations.py``.

No network: ``send_message`` is patched on the script module. Observation
documents are synthetic (built with the helpers of ``test_notify``); nothing
is a real capture, a real financial figure or a real Telegram token. Layout
v1 (the previous single flat text) is pinned by ``tests/test_notify.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from fintr.notify import delivery
from fintr.notify import format as fmt
from fintr.notify.explanations import (
    RULE_EXPLANATIONS_SCHEMA, RuleExplanationsError, load_rule_explanations,
)
from fintr.notify.guard import find_forbidden_words
from fintr.notify.layout import (
    DIRECTION_LABELS_V2_TR, STOCK_WARNING_TR, SUMMARY_WARNING_TR, format_percent_tr,
    render_layout_v2, share_percent, vwap_vs_close_percent,
)
from fintr.notify.telegram import SendResult, TelegramSendError
from fintr.scanner.observe import NOTICE_TEXT_TR

from test_notify import (
    DIRECTIONS, FAKE_CHAT_ID, FAKE_TOKEN, REAL_RULE_TEXTS_FILE, RULE_TEXTS, ROOT,
    _composition_block, _document, _feature, _freshness, _obs, _symbol_block, notify,
)
from test_symbol_guard import DEVELOPMENT, GuardEnvMixin


EXPLANATIONS_FILE = ROOT / "configs" / "notify" / "rule_explanations_v1.json"
DELIVERY_FILE = ROOT / "configs" / "notify" / "delivery_v1.json"
EXPLANATIONS = load_rule_explanations(EXPLANATIONS_FILE)
HORIZONS = {"H1": 5, "H2": 5, "H3": 5, "H4": 5}
LABELS = tuple(DIRECTION_LABELS_V2_TR.values())
RULES_HASH = "42fca88a" + "0" * 56
FREEZE_WINDOW = {"start": "2026-09-11", "end": "2026-10-02"}


def _vwap(value) -> dict:
    return _feature("vwap_proxy_vs_close", value)


def _hit(rule_id: str, value="0.0153407002341234", share=None) -> dict:
    features = [_vwap(value)]
    if share is not None:
        features.insert(0, _feature("top1_akd_net_share_of_volume", share))
    return _obs(rule_id, condition_met=True, features=features)


def _scan(observations: list, as_of: str = "2026-10-02") -> dict:
    return {"as_of": as_of, "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
            "observations": observations}


def _block(observations: list, *, as_of: str = "2026-10-02", composition=...) -> dict:
    if composition is ...:
        composition = _composition_block(window=FREEZE_WINDOW, in_window=(0, 2, 0))
    return _symbol_block(scans=[_scan(observations, as_of)], composition=composition)


def _doc(symbols: dict) -> dict:
    document = _document(symbols)
    document["rules_file_sha256"] = RULES_HASH
    return document


def _render(document: dict, directions=DIRECTIONS, explanations=EXPLANATIONS, horizons=HORIZONS):
    return render_layout_v2(document, RULE_TEXTS, directions, explanations, horizons)


def _five_stock_doc() -> dict:
    return _doc({
        "AKBNK1": _block([_hit("H2", "0.0153407002341234")]),
        "BIMAS1": _block([_hit("H1", "-0.02")]),
        "CCOLA1": _block([_hit("H2", "0.03")]),
        "DOAS1": _block([_hit("H1", "-0.0123")]),
        "EREG1": _block([_hit("H2", "0.0101")]),
        "FROTO1": _block([_obs("H1", condition_met=False)]),
    })


# --- conversions -------------------------------------------------------------


class PercentConversionTests(unittest.TestCase):
    def test_percent_is_the_frozen_feature_times_100(self):
        # v = (vwap - close) / close shown as-is: close=100, vwap=101.5 -> v=0.015 -> 1.5%.
        pct = vwap_vs_close_percent(Decimal("0.015"))
        self.assertEqual(pct, Decimal("1.5"))
        self.assertEqual(format_percent_tr(pct), "%1,50")
        pct = vwap_vs_close_percent(Decimal("-0.02"))
        self.assertEqual(pct, Decimal("-2"))
        self.assertEqual(format_percent_tr(pct), "%2,00")
        # the example of Efe's decision: 0.01534 -> %1,53 (no re-basing by 1+v)
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("0.01534"))), "%1,53")
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("-0.01534"))), "%1,53")

    def test_frozen_thresholds_are_exactly_one_percent_on_both_sides(self):
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("0.01"))), "%1,00")
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("-0.01"))), "%1,00")
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("0.10"))), "%10,00")
        self.assertEqual(format_percent_tr(vwap_vs_close_percent(Decimal("-0.10"))), "%10,00")

    def test_zero_and_impossible_values(self):
        self.assertEqual(vwap_vs_close_percent(Decimal(0)), 0)
        self.assertIsNone(vwap_vs_close_percent(Decimal(-1)))
        self.assertIsNone(vwap_vs_close_percent(Decimal(-2)))

    def test_format_percent_is_half_up_two_decimals_turkish_comma_unsigned(self):
        self.assertEqual(format_percent_tr(Decimal("1.005")), "%1,01")
        self.assertEqual(format_percent_tr(Decimal("1.004")), "%1,00")
        self.assertEqual(format_percent_tr(Decimal("-1.5110")), "%1,51")
        self.assertEqual(format_percent_tr(Decimal("12.3456")), "%12,35")
        self.assertEqual(format_percent_tr(Decimal(0)), "%0,00")

    def test_share_percent(self):
        self.assertEqual(format_percent_tr(share_percent(Decimal("0.1234"))), "%12,34")
        self.assertIsNone(share_percent(Decimal("-0.01")))
        self.assertIsNone(share_percent(Decimal("1.5")))


class SignAndDirectionPerRuleTests(unittest.TestCase):
    """The subject is the VWAP proxy. H1/H3: v < 0, VWAP below the close
    ("altında"), frozen direction negative (▼ aşağı). H2/H4: v > 0, VWAP above
    the close ("üstünde"), frozen direction positive (▲ yukarı)."""

    def _stock_message(self, rule_id, value, share=None):
        document = _doc({"KCHOL1": _block([_hit(rule_id, value, share)])})
        return _render(document)[1]

    def test_h1_vwap_below_close_down_label(self):
        message = self._stock_message("H1", "-0.0153407002341234")
        self.assertIn("▼ KCHOL1 — beklenen yön: aşağı (sat yönlü)", message)
        self.assertIn("Bugün: Ortalama fiyat (VWAP proxy) kapanışın %1,53 altında", message)

    def test_h2_vwap_above_close_up_label(self):
        message = self._stock_message("H2", "0.0153407002341234")
        self.assertIn("▲ KCHOL1 — beklenen yön: yukarı (al yönlü)", message)
        self.assertIn("Bugün: Ortalama fiyat (VWAP proxy) kapanışın %1,53 üstünde", message)

    def test_h3_below_with_share(self):
        message = self._stock_message("H3", "-0.0153407002341234", share="0.123456789")
        self.assertIn("▼ KCHOL1 — beklenen yön: aşağı (sat yönlü)", message)
        self.assertIn("Ortalama fiyat (VWAP proxy) kapanışın %1,53 altında · tek kurum net payı %12,35", message)

    def test_h4_above_with_share(self):
        message = self._stock_message("H4", "0.0153407002341234", share="0.1")
        self.assertIn("▲ KCHOL1 — beklenen yön: yukarı (al yönlü)", message)
        self.assertIn("Ortalama fiyat (VWAP proxy) kapanışın %1,53 üstünde · tek kurum net payı %10,00", message)

    def test_example_0_01534_is_1_53_in_both_signs(self):
        self.assertIn("kapanışın %1,53 üstünde", self._stock_message("H2", "0.01534"))
        self.assertIn("kapanışın %1,53 altında", self._stock_message("H1", "-0.01534"))

    def test_threshold_values_show_exactly_1_00_percent(self):
        self.assertIn("Ortalama fiyat (VWAP proxy) kapanışın %1,00 üstünde", self._stock_message("H2", "0.01"))
        self.assertIn("Ortalama fiyat (VWAP proxy) kapanışın %1,00 altında", self._stock_message("H1", "-0.01"))
        self.assertIn("kapanışın %1,00 üstünde", self._stock_message("H4", "0.01", share="0.1"))
        self.assertIn("kapanışın %1,00 altında", self._stock_message("H3", "-0.01", share="0.1"))

    def test_direction_label_is_the_exact_lowercase_agents_md_text(self):
        self.assertEqual(DIRECTION_LABELS_V2_TR, {
            "positive": "beklenen yön: yukarı (al yönlü)",
            "negative": "beklenen yön: aşağı (sat yönlü)",
        })
        text = "\n".join(_render(_five_stock_doc()))
        self.assertNotIn("YUKARI", text)
        self.assertNotIn("AŞAĞI", text)
        self.assertNotIn("kapanış ortalama fiyatın", text)

    def test_wording_follows_the_value_sign_not_the_rule_id(self):
        # Never produced by the scanner (the frozen condition would be false),
        # but the percent wording must come from the value itself.
        message = self._stock_message("H2", "-0.02")
        self.assertIn("kapanışın %2,00 altında", message)

    def test_unreadable_values_are_marked_not_guessed(self):
        for bad in (None, "", "abc", "NaN", "Infinity", "-1", "-5"):
            with self.subTest(bad=bad):
                message = self._stock_message("H2", bad)
                self.assertIn("Ortalama fiyat (VWAP proxy)–kapanış farkı okunamadı", message)
                self.assertNotIn("%", message.split("Bugün:")[1].split("\n")[0])

    def test_raw_values_never_in_any_message(self):
        document = _doc({
            "KCHOL1": _block([_hit("H4", "0.0153407002341234", share="0.123456789012")]),
            "THYAO1": _block([_hit("H1", "-0.0200123456789")]),
        })
        text = "\n".join(_render(document))
        for raw in ("0.0153407", "0,0153", "0.123456", "0.0200123", "-0.02", "vwap_proxy_vs_close",
                    "top1_akd_net_share_of_volume"):
            self.assertNotIn(raw, text)


# --- messages ----------------------------------------------------------------


class SummaryAndStockMessageTests(unittest.TestCase):
    def test_five_stock_layout_matches_the_approved_shape(self):
        messages = _render(_five_stock_doc())
        self.assertEqual(len(messages), 1 + 5)
        self.assertEqual(messages[0].split("\n"), [
            "FinTR · 02.10.2026 (Cuma kapanışı)",
            "Koşulu sağlanan: 5 hisse — 3 yukarı, 2 aşağı",
            "Veri: 6 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş",
            SUMMARY_WARNING_TR,
        ])
        self.assertEqual(
            SUMMARY_WARNING_TR,
            "⚠ Yön etiketi doğrulanmamış hipotezdir (test: Nisan 2027); yatırım tavsiyesi değildir.",
        )
        first = messages[1].split("\n")
        self.assertEqual(first[0], "▲ AKBNK1 — beklenen yön: yukarı (al yönlü)")
        self.assertEqual(first[1], STOCK_WARNING_TR)
        self.assertEqual(first[2], "Kural H2: " + EXPLANATIONS.rule_lines["H2"])
        self.assertEqual(first[3], "Bugün: Ortalama fiyat (VWAP proxy) kapanışın %1,53 üstünde")
        self.assertEqual(first[4], "Ne demek: " + EXPLANATIONS.meanings["H2"])
        self.assertEqual(first[5], "Vade: 5 işlem günü · kural sürümü 42fca88a")
        self.assertEqual(len(first), 6)

    def test_every_labelled_message_carries_a_warning_once_and_stocks_are_in_symbol_order(self):
        messages = _render(_five_stock_doc())
        self.assertEqual(messages[0].count(SUMMARY_WARNING_TR), 1)
        for message in messages[1:]:
            self.assertEqual(message.count(STOCK_WARNING_TR), 1)
            self.assertEqual(message.split("\n")[1], STOCK_WARNING_TR)  # right under the label
            self.assertEqual(find_forbidden_words(STOCK_WARNING_TR, LABELS), [])
        titles = [message.split("\n")[0].split(" — ")[0][2:] for message in messages[1:]]
        self.assertEqual(titles, ["AKBNK1", "BIMAS1", "CCOLA1", "DOAS1", "EREG1"])  # symbol order, no ranking

    def test_label_never_appears_without_a_warning_even_without_explanations(self):
        for explanations in (None, EXPLANATIONS, load_rule_explanations_from({"H1": {"rule_line": "x"}})):
            messages = _render(_five_stock_doc(), explanations=explanations)
            for message in messages:
                if any(label in message for label in LABELS):
                    self.assertTrue(STOCK_WARNING_TR in message or SUMMARY_WARNING_TR in message, message)

    def test_unlabelled_and_unknown_direction_messages_have_no_stock_warning(self):
        for directions in (None, {}):
            text = "\n".join(_render(_five_stock_doc(), directions=directions))
            self.assertNotIn(STOCK_WARNING_TR, text)

    def test_worst_case_stock_message_fits_telegram_limit(self):
        # All rules at once for a long multi-day scan: each (symbol, day) is its own message.
        document = _doc({"KCHOL1": _symbol_block(scans=[
            _scan([_hit("H1", "-0.02"), _hit("H3", "-0.02", share="0.2")], f"2026-10-0{day}")
            for day in range(1, 6)
        ])})
        for message in _render(document):
            self.assertLessEqual(len(message), 4096)

    def test_single_stock(self):
        messages = _render(_doc({"KCHOL1": _block([_hit("H1", "-0.02")])}))
        self.assertEqual(len(messages), 2)
        self.assertIn("Koşulu sağlanan: 1 hisse — 1 aşağı", messages[0])
        self.assertIn("Veri: 1 hisse tam (boşluk 0)", messages[0])

    def test_no_condition_met_is_one_short_message_with_data_line_and_no_warning(self):
        document = _doc({
            "KCHOL1": _block([_obs("H1", condition_met=False), _obs("H2", condition_met=False)]),
            "THYAO1": _block([_obs("H1", condition_met=False)]),
        })
        messages = _render(document)
        self.assertEqual(messages, [
            "FinTR · 02.10.2026 (Cuma kapanışı): bugün hiçbir hissede koşul sağlanmadı\n"
            "Veri: 2 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş"
        ])

    def test_two_rules_for_one_stock_are_one_message(self):
        document = _doc({"KCHOL1": _block([_hit("H4", "0.02", share="0.2"), _hit("H2", "0.02")])})
        messages = _render(document)
        self.assertEqual(len(messages), 2)
        lines = messages[1].split("\n")
        self.assertEqual(lines[0], "▲ KCHOL1 — beklenen yön: yukarı (al yönlü)")
        self.assertEqual([line.split(":")[0] for line in lines[1:]],
                         [STOCK_WARNING_TR, "Kural H2", "Kural H4", "Bugün", "Ne demek (H2)", "Ne demek (H4)", "Vade"])
        self.assertEqual(lines[4], "Bugün: Ortalama fiyat (VWAP proxy) kapanışın %2,00 üstünde · tek kurum net payı %20,00 (günlük toplama oranı)")
        self.assertIn("Koşulu sağlanan: 1 hisse — 1 yukarı", messages[0])

    def test_rule_without_explanation_skips_the_line_not_invents_it(self):
        reduced = load_rule_explanations_from({
            "H1": {"rule_line": "H1 sabit satır"},  # no meaning
            "H2": {"rule_line": "H2 sabit satır", "meaning": "H2 sabit açıklama"},
        })
        document = _doc({"AAA1": _block([_hit("H1", "-0.02")]), "BBB1": _block([_hit("H2")]),
                         "CCC1": _block([_hit("H3", "-0.02", share="0.2")])})
        messages = _render(document, explanations=reduced)
        self.assertNotIn("Ne demek", messages[1])
        self.assertIn("Kural H1: H1 sabit satır", messages[1])
        self.assertIn("Ne demek: H2 sabit açıklama", messages[2])
        # H3 has no entry at all: rule line falls back to the v1 rule text, no Ne demek.
        self.assertIn(f"Kural H3: {RULE_TEXTS['H3']}", messages[3])
        self.assertNotIn("Ne demek", messages[3])

    def test_vade_omitted_when_horizon_unknown(self):
        messages = _render(_doc({"KCHOL1": _block([_hit("H2")])}), horizons={})
        self.assertNotIn("Vade:", messages[1])
        self.assertTrue(messages[1].endswith("Kural sürümü: 42fca88a"))

    def test_unknown_direction_marks_the_stock_and_shows_no_hypothesis_text(self):
        messages = _render(_doc({"KCHOL1": _block([_hit("H2")])}), directions={})
        self.assertIn("Koşulu sağlanan: 1 hisse — 1 yön bilinmiyor", messages[0])
        self.assertNotIn(SUMMARY_WARNING_TR, messages[0])
        self.assertIn(NOTICE_TEXT_TR, messages[0])  # the scanner's own fixed notice
        self.assertTrue(messages[1].startswith("• KCHOL1 — yön bilinmiyor\n"))
        for forbidden in ("yönlü", "Ne demek", "Vade", "Ön kayıtlı"):
            self.assertNotIn(forbidden, "\n".join(messages))

    def test_unlabelled_layout_has_no_label_arrow_hypothesis_or_warning(self):
        messages = _render(_five_stock_doc(), directions=None)
        text = "\n".join(messages)
        for forbidden in ("yönlü", "▲", "▼", "Ne demek", "Vade", "Ön kayıtlı", SUMMARY_WARNING_TR):
            self.assertNotIn(forbidden, text)
        self.assertEqual(messages[0].split("\n")[1], "Koşulu sağlanan: 5 hisse")
        self.assertIn(NOTICE_TEXT_TR, messages[0])
        self.assertTrue(messages[1].startswith("AKBNK1 — koşul sağlandı\nKural H2: "))
        self.assertIn("Bugün: Ortalama fiyat (VWAP proxy) kapanışın %1,53 üstünde", messages[1])
        self.assertTrue(messages[1].endswith("Kural sürümü: 42fca88a"))

    def test_conflicting_directions_for_one_stock_are_unknown(self):
        document = _doc({"KCHOL1": _block([_hit("H1", "-0.02"), _hit("H2", "0.02")])})
        messages = _render(document)
        self.assertIn("1 yön bilinmiyor", messages[0])
        self.assertTrue(messages[1].startswith("• KCHOL1 — yön bilinmiyor"))

    def test_multi_day_scan_header_and_dates_in_titles(self):
        document = _doc({"KCHOL1": _symbol_block(scans=[
            _scan([_hit("H2")], "2026-10-01"), _scan([_hit("H2", "0.03")], "2026-10-02"),
        ])})
        messages = _render(document)
        self.assertEqual(messages[0].split("\n")[0], "FinTR · 01.10.2026–02.10.2026 (2 günlük tarama)")
        self.assertIn("Koşulu sağlanan: 2 hisse-gün", messages[0])
        self.assertTrue(messages[1].startswith("▲ KCHOL1 (01.10.2026) — "))
        self.assertTrue(messages[2].startswith("▲ KCHOL1 (02.10.2026) — "))


def load_rule_explanations_from(rules: dict):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "x.json"
        path.write_text(json.dumps({"schema": RULE_EXPLANATIONS_SCHEMA, "rules": rules}), encoding="utf-8")
        return load_rule_explanations(path)


class DataStatusTests(unittest.TestCase):
    def _data_lines(self, symbols: dict) -> list[str]:
        document = _doc(symbols)
        return [line for line in _render(document)[0].split("\n")
                if line.startswith(("Veri:", "Farklı:"))]

    def _comp(self, gaps=0, unsettled=2, revisions=0):
        return _composition_block(window=FREEZE_WINDOW, in_window=(gaps, unsettled, revisions))

    def test_all_identical_is_one_line(self):
        symbols = {f"S{i:02d}A": _block([_obs("H1", condition_met=False)], composition=self._comp())
                   for i in range(20)}
        self.assertEqual(self._data_lines(symbols),
                         ["Veri: 20 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş"])

    def test_one_stock_differs_and_is_listed_separately(self):
        symbols = {f"S{i:02d}A": _block([_obs("H1", condition_met=False)], composition=self._comp())
                   for i in range(19)}
        symbols["ODDA"] = _block([_obs("H1", condition_met=False)],
                                 composition=self._comp(gaps=1, unsettled=3, revisions=1))
        self.assertEqual(self._data_lines(symbols), [
            "Veri: 19/20 hisse tam (boşluk 0) · 2 gün kesinleşmemiş",
            "Farklı: ODDA — eksik (boşluk 1) · 3 gün kesinleşmemiş · 1 gün revize",
        ])

    def test_differing_unsettled_counts_alone_are_listed(self):
        symbols = {
            "AAA1": _block([_obs("H1", condition_met=False)], composition=self._comp()),
            "BBB1": _block([_obs("H1", condition_met=False)], composition=self._comp()),
            "CCC1": _block([_obs("H1", condition_met=False)], composition=self._comp(unsettled=0)),
        }
        self.assertEqual(self._data_lines(symbols), [
            "Veri: 2/3 hisse tam (boşluk 0) · 2 gün kesinleşmemiş",
            "Farklı: CCC1 — tam (boşluk 0) · kesinleşmemiş gün yok",
        ])

    def test_no_composition_and_no_window_and_whole_period_fallback(self):
        quiet = [_obs("H1", condition_met=False)]
        self.assertEqual(self._data_lines({"AAA1": _block(quiet, composition=None)}),
                         ["Veri: 1 hisse durum bilgisi yok (günlük birleştirme olmadan tarandı)"])
        self.assertEqual(self._data_lines({"AAA1": _block(quiet, composition=_composition_block(window=None))}),
                         ["Veri: 1 hisse pencere yok (günlük kayıt yok)"])
        self.assertEqual(
            self._data_lines({"AAA1": _block(quiet, composition=_composition_block(gaps=4, unsettled=3, revisions=0))}),
            ["Veri: 1 hisse eksik (boşluk 4) · 3 gün kesinleşmemiş · tüm dönem sayımı"],
        )

    def test_extra_lines_only_when_there_is_something_to_report(self):
        clean = _render(_doc({"AAA1": _block([_hit("H1", "-0.02")])}))[0]
        for absent in ("Eksik veri", "Bastırılan", "Değerlendirilemeyen"):
            self.assertNotIn(absent, clean)
        document = _doc({
            "AAA1": _symbol_block(scans=[{
                "as_of": "2026-10-02", "freshness": _freshness(last_price_date=None),
                "observations": [_obs("H1", condition_met=None, suppressed="ratio_regime"),
                                 _obs("H2", condition_met=None)],
            }]),
        })
        summary = _render(document)[0]
        self.assertIn("Eksik veri: AAA1 02.10.2026 (fiyat)", summary)
        self.assertIn("Bastırılan gözlem: oran rejimi 1", summary)
        self.assertIn("Değerlendirilemeyen gözlem: 1", summary)


class LengthLimitTests(unittest.TestCase):
    def test_summary_over_the_limit_is_split_on_line_boundaries_without_losing_content(self):
        symbols = {
            f"SYM{i:03d}": _block(
                [_obs("H1", condition_met=False)],
                composition=_composition_block(window=FREEZE_WINDOW, in_window=(i + 1, i, 0)),
            )
            for i in range(300)
        }
        messages = _render(_doc(symbols))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), fmt.TELEGRAM_MESSAGE_LIMIT)
        joined = "\n".join(messages)
        # The largest group (here a one-symbol tie, SYM000) is only counted on
        # the "Veri:" line; every differing group lists its symbols.
        for i in range(1, 300):
            self.assertIn(f"SYM{i:03d}", joined)

    def test_every_message_of_a_large_document_is_within_the_limit(self):
        symbols = {f"SYM{i:03d}": _block([_hit("H4", "0.02", share="0.2"), _hit("H2", "0.02")])
                   for i in range(60)}
        messages = _render(_doc(symbols))
        self.assertEqual(len(messages), 61)
        self.assertTrue(all(len(message) <= fmt.TELEGRAM_MESSAGE_LIMIT for message in messages))


# --- wording guard -------------------------------------------------------------


class ForbiddenWordingTests(unittest.TestCase):
    def test_guard_allows_exactly_the_two_labels_and_nothing_else(self):
        self.assertEqual(find_forbidden_words(LABELS[0], LABELS), [])
        self.assertEqual(find_forbidden_words(LABELS[1], LABELS), [])
        for bad in ("al!", "sat!", "hedef 12", "stop 10", "kâr bekleniyor", "kesin yükselir", "öneri",
                    "score 3", "rank 1", "return 2", "target 5", "al yönlü", "sat yönlü"):
            with self.subTest(bad=bad):
                self.assertTrue(find_forbidden_words(bad, LABELS), bad)
        self.assertTrue(find_forbidden_words(f"{LABELS[0]} ve al", LABELS))
        # Whole words only: Turkish words that merely contain "al"/"sat" are fine.
        self.assertEqual(find_forbidden_words("alıcı satıcı satış alış kapanış"), [])

    def test_every_rendered_message_is_clean_labelled_unknown_and_unlabelled(self):
        document = _doc({
            "AAA1": _block([_hit("H1", "-0.02")]), "BBB1": _block([_hit("H2")]),
            "CCC1": _block([_hit("H3", "-0.02", share="0.2")]),
            "DDD1": _block([_hit("H4", "0.02", share="0.2")]),
            "EEE1": _block([_hit("H4", "0.02", share="0.2"), _hit("H2")]),
        })
        for directions in (DIRECTIONS, {}, None):
            for message in _render(document, directions=directions):
                with self.subTest(directions=bool(directions), message=message[:30]):
                    text = message.replace(NOTICE_TEXT_TR, "")  # the scanner's fixed disclaimer
                    self.assertEqual(find_forbidden_words(text, LABELS), [], message)
        empty = _render(_doc({"AAA1": _block([_obs("H1", condition_met=False)])}))
        self.assertEqual(find_forbidden_words(empty[0], LABELS), [])

    def test_only_two_label_phrases_appear_across_the_labelled_layout(self):
        text = "\n".join(_render(_five_stock_doc()))
        self.assertIn(LABELS[0], text)
        self.assertIn(LABELS[1], text)
        self.assertEqual(text.count("yönlü)"), text.count(LABELS[0]) + text.count(LABELS[1]))

    def test_summary_warning_and_footer_texts_are_clean(self):
        self.assertEqual(find_forbidden_words(SUMMARY_WARNING_TR, LABELS), [])


# --- explanations file ---------------------------------------------------------


class ExplanationsFileTests(unittest.TestCase):
    def _write(self, directory: str, document: dict) -> Path:
        path = Path(directory) / "x.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def test_real_file_has_h1_to_h4_and_pins_its_hash(self):
        self.assertEqual(set(EXPLANATIONS.rule_lines), {"H1", "H2", "H3", "H4"})
        self.assertEqual(set(EXPLANATIONS.meanings), {"H1", "H2", "H3", "H4"})
        self.assertEqual(EXPLANATIONS.sha256, hashlib.sha256(EXPLANATIONS_FILE.read_bytes()).hexdigest())
        for text in (*EXPLANATIONS.rule_lines.values(), *EXPLANATIONS.meanings.values()):
            self.assertEqual(find_forbidden_words(text), [])
        for meaning in EXPLANATIONS.meanings.values():
            self.assertIn("henüz doğrulanmamış hipotez", meaning)
            self.assertLessEqual(meaning.count(". "), 1)  # 1-2 sentences

    def test_rule_lines_match_the_frozen_spec_conditions(self):
        """Direction word, thresholds and the data-quality band come from the
        frozen spec's ``condition`` and ``predicted_direction``, not from memory."""
        for rule_id in ("H1", "H2", "H3", "H4"):
            (spec_path,) = (ROOT / "configs" / "studies" / "prereg").glob(f"{rule_id.lower()}_*.json")
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            terms = spec["condition"]["terms"]
            vwap = [(t["op"], Decimal(t["right"]["const"])) for t in terms
                    if t["left"]["feature"] == "vwap_proxy_vs_close"]
            share = [(t["op"], Decimal(t["right"]["const"])) for t in terms
                     if t["left"]["feature"] == "top1_akd_net_share_of_volume"]
            line = EXPLANATIONS.rule_lines[rule_id]
            with self.subTest(rule=rule_id):
                lower = max(c for op, c in vwap if op == "ge")
                upper = min(c for op, c in vwap if op == "le")
                below = upper < 0  # v <= -0.01 and v >= -0.10: the VWAP proxy is below the close
                self.assertEqual(("altında" in line), below)
                self.assertEqual(("üstünde" in line), not below)
                self.assertEqual(abs(upper if below else lower), Decimal("0.01"))
                self.assertEqual(abs(lower if below else upper), Decimal("0.10"))
                self.assertTrue(line.startswith("ortalama fiyat (VWAP proxy)") or "ve ortalama fiyat (VWAP proxy)" in line)
                self.assertIn("kapanışın en az %1, en çok %10 ", line)
                self.assertNotIn("~", line)
                if share:
                    self.assertEqual(share, [("ge", Decimal("0.10"))])
                    self.assertIn("en az %10'u", line)
                    self.assertIn("net payı", line)
                else:
                    self.assertNotIn("%10'u", line)
                expected_direction = "negative" if below else "positive"
                self.assertEqual(spec["preregistration"]["predicted_direction"], expected_direction)
                self.assertEqual(spec["preregistration"]["primary_horizon_trading_days"], 5)

    def test_rejects_wrong_schema_unreadable_empty_and_non_object(self):
        with tempfile.TemporaryDirectory() as directory:
            for document in ({"schema": "other", "rules": {"H1": {"meaning": "x"}}},
                             {"schema": RULE_EXPLANATIONS_SCHEMA, "rules": {}},
                             {"schema": RULE_EXPLANATIONS_SCHEMA, "rules": {"H1": "text"}}):
                with self.subTest(document=document), self.assertRaises(RuleExplanationsError):
                    load_rule_explanations(self._write(directory, document))
            with self.assertRaises(RuleExplanationsError):
                load_rule_explanations(Path(directory) / "missing.json")

    def test_rejects_forbidden_wording_and_overlong_text(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in ("kesin yükselir", "hedef fiyat", "al sinyali", "x" * 700, "  "):
                with self.subTest(text=text[:20]), self.assertRaises(RuleExplanationsError):
                    load_rule_explanations(self._write(directory, {
                        "schema": RULE_EXPLANATIONS_SCHEMA, "rules": {"H1": {"meaning": text}},
                    }))


# --- delivery (pure parts) -----------------------------------------------------


def _v2_receipt(path: Path, hashes: list[str], sent: dict[int, int], obs_sha: str = "o" * 64) -> None:
    path.write_text(json.dumps({
        "schema": delivery.RECEIPT_SCHEMA_V2, "observations_sha256": obs_sha,
        "message_sha256": hashes,
        "message_status": [
            {"index": i, "status": "sent", "telegram_message_id": sent[i]} if i in sent
            else {"index": i, "status": "not_sent"} for i in range(1, len(hashes) + 1)
        ],
    }), encoding="utf-8")


class DeliveryPlanTests(unittest.TestCase):
    MESSAGES = ["a", "b", "c"]
    HASHES = [delivery.sha256_text(text) for text in MESSAGES]

    def test_no_session_sends_everything_to_the_primary_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = delivery.plan_delivery(Path(directory) / "none", self.MESSAGES, "o" * 64, resend=False)
            self.assertEqual(plan.to_send, (1, 2, 3))
            self.assertEqual(plan.target.name, "receipt.json")

    def test_complete_session_is_rejected_unless_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _v2_receipt(root / "receipt.json", self.HASHES, {1: 10, 2: 11, 3: 12})
            with self.assertRaises(FileExistsError):
                delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False)
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=True)
            self.assertEqual(plan.to_send, (1, 2, 3))
            self.assertTrue(plan.resend)
            self.assertNotEqual(plan.target.name, "receipt.json")
            self.assertFalse(plan.target.exists())

    def test_incomplete_session_resumes_only_unsent_into_a_part_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _v2_receipt(root / "receipt.json", self.HASHES, {1: 10})
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False)
            self.assertEqual(plan.to_send, (2, 3))
            self.assertEqual(plan.already_sent, {1: 10})
            self.assertEqual(plan.target.name, "receipt.part-002.json")
            self.assertEqual(plan.continues, "receipt.json")
            _v2_receipt(plan.target, self.HASHES, {1: 10, 2: 11})
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False)
            self.assertEqual(plan.to_send, (3,))
            self.assertEqual(plan.target.name, "receipt.part-003.json")

    def test_content_mismatch_refuses_to_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _v2_receipt(root / "receipt.json", self.HASHES, {1: 10})
            with self.assertRaises(delivery.ResumeMismatch):
                delivery.plan_delivery(root, ["a", "b", "CHANGED"], "o" * 64, resend=False)
            with self.assertRaises(delivery.ResumeMismatch):
                delivery.plan_delivery(root, self.MESSAGES, "different-observations", resend=False)

    def test_legacy_v1_receipt_counts_as_a_complete_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "receipt.json").write_text(json.dumps({"schema": "fintr_notify_receipt_v1"}), encoding="utf-8")
            with self.assertRaises(FileExistsError):
                delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False)

    def _note_receipt(self, path: Path, hashes: list[str], sent: dict[int, int]) -> None:
        _v2_receipt(path, hashes, sent)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema"] = delivery.NOTE_RECEIPT_SCHEMA_V2
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_note_family_has_its_own_files_and_never_matches_the_notice_glob(self):
        family = delivery.NOTE_RECEIPTS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(list(root.glob("receipt*.json")))
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False, family=family)
            self.assertEqual(plan.target.name, "note_receipt.json")
            self._note_receipt(root / "note_receipt.json", self.HASHES, {1: 10})
            self._note_receipt(root / "note_receipt.part-002.json", self.HASHES, {1: 10, 2: 11})
            self.assertEqual(list(root.glob("receipt*.json")), [])  # the notice's glob sees nothing
            self.assertIsNone(delivery.find_latest_session(root))
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False, family=family)
            self.assertEqual((plan.to_send, plan.target.name, plan.continues),
                             ((3,), "note_receipt.part-003.json", "note_receipt.json"))
            with self.assertRaises(delivery.ResumeMismatch):
                delivery.plan_delivery(root, ["a", "b", "CHANGED"], "o" * 64, resend=False, family=family)

    def test_note_family_resend_head_and_complete_rejection(self):
        family = delivery.NOTE_RECEIPTS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._note_receipt(root / "note_receipt.json", self.HASHES, {1: 1, 2: 2, 3: 3})
            with self.assertRaises(FileExistsError):
                delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False, family=family)
            plan = delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=True, family=family)
            self.assertRegex(plan.target.name, r"^note_receipt-\d{8}T\d{6}Z\.json$")
            self._note_receipt(root / "note_receipt-20261003T101010Z.json", self.HASHES, {1: 4})
            session = delivery.find_latest_session(root, family)
            self.assertEqual(session.head.name, "note_receipt-20261003T101010Z.json")
            self.assertEqual(session.sent, {1: 4})

    def test_legacy_v1_note_receipt_counts_as_a_complete_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note_receipt.json").write_text(
                json.dumps({"schema": "fintr_notify_note_receipt_v1"}), encoding="utf-8")
            with self.assertRaises(FileExistsError):
                delivery.plan_delivery(root, self.MESSAGES, "o" * 64, resend=False, family=delivery.NOTE_RECEIPTS)

    def test_the_two_families_do_not_read_each_others_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _v2_receipt(root / "note_receipt.json", self.HASHES, {1: 1})  # notice schema in a note file
            self.assertTrue(delivery.find_latest_session(root, delivery.NOTE_RECEIPTS).legacy)

    def test_latest_session_is_the_newest_resend_even_with_a_collision_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _v2_receipt(root / "receipt.json", self.HASHES, {1: 1, 2: 2, 3: 3})
            _v2_receipt(root / "receipt-20261003T101010Z.json", self.HASHES, {1: 4, 2: 5, 3: 6})
            _v2_receipt(root / "receipt-20261003T101010Z-2.json", self.HASHES, {1: 7})
            session = delivery.find_latest_session(root)
            self.assertEqual(session.head.name, "receipt-20261003T101010Z-2.json")
            self.assertEqual(session.sent, {1: 7})
            self.assertFalse(session.complete)

    def test_load_delivery_config(self):
        delay, sha = delivery.load_delivery_config(DELIVERY_FILE)
        self.assertEqual(delay, 1.0)
        self.assertEqual(sha, hashlib.sha256(DELIVERY_FILE.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            for document in ({"schema": "other", "inter_message_delay_seconds": 1},
                             {"schema": delivery.DELIVERY_SCHEMA, "inter_message_delay_seconds": -1},
                             {"schema": delivery.DELIVERY_SCHEMA, "inter_message_delay_seconds": "1"},
                             {"schema": delivery.DELIVERY_SCHEMA, "inter_message_delay_seconds": True}):
                path = Path(directory) / "d.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(document=document), self.assertRaises(delivery.DeliveryConfigError):
                    delivery.load_delivery_config(path)


# --- scripts/notify_observations.py, layout v2 ---------------------------------


class ScriptV2Tests(GuardEnvMixin, unittest.TestCase):
    ENV = {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}

    def _document(self, value_a="0.0153407002341234") -> dict:
        document = _doc({
            DEVELOPMENT[0]: _block([_hit("H2", value_a)]),
            DEVELOPMENT[1]: _block([_hit("H1", "-0.02")]),
        })
        document["rules_file_sha256"] = "9" * 64
        return document

    def _prepare(self, document: dict | None = None) -> Path:
        self.make_env()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self._write(root, document or self._document())
        return root

    def _write(self, root: Path, document: dict) -> Path:
        target = root / "scanner" / "2026-10-02"
        target.mkdir(parents=True, exist_ok=True)
        path = target / "observations.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def _argv(self, root: Path, *extra: str) -> list[str]:
        return ["--as-of", "2026-10-02", "--scanner-dir", str(root / "scanner"),
                "--out-dir", str(root / "out"), "--rule-texts", str(REAL_RULE_TEXTS_FILE), *extra]

    def _run(self, root: Path, *extra: str, send_effects=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        mock_send = MagicMock(side_effect=send_effects)
        mock_time = MagicMock()
        with patch.dict(os.environ, self.ENV), patch.object(notify, "send_message", mock_send), \
                patch.object(notify, "time", mock_time), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = notify.main(self._argv(root, *extra))
        return code, stdout.getvalue(), stderr.getvalue(), mock_send, mock_time

    def _receipts(self, root: Path) -> list[Path]:
        directory = root / "out" / "2026-10-02"
        return sorted(directory.iterdir()) if directory.exists() else []

    def test_default_is_layout_v2_dry_run_without_network(self):
        root = self._prepare()
        stdout = io.StringIO()
        with patch("fintr.notify.telegram.urllib.request.urlopen",
                   side_effect=AssertionError("dry-run must never touch the network")), \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = notify.main(self._argv(root))
        out = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("--- mesaj "), 3)
        self.assertIn("--- mesaj 1/3 (dry-run; ağ kullanılmadı) ---\nFinTR · 02.10.2026 (Cuma kapanışı)\n", out)
        self.assertIn("Koşulu sağlanan: 2 hisse — 1 yukarı, 1 aşağı", out)
        self.assertIn(f"▲ {DEVELOPMENT[0]} — beklenen yön: yukarı (al yönlü)", out)
        self.assertIn(f"▼ {DEVELOPMENT[1]} — beklenen yön: aşağı (sat yönlü)", out)
        self.assertIn("kural sürümü 99999999", out)
        self.assertIn("Vade: 5 işlem günü", out)  # from the real frozen specs
        self.assertEqual(out.count(SUMMARY_WARNING_TR), 1)
        self.assertFalse((root / "out").exists())

    def test_both_observation_schemas_render_in_v2(self):
        for schema in (fmt.OBSERVATIONS_SCHEMA_V1, fmt.OBSERVATIONS_SCHEMA_V2):
            with self.subTest(schema=schema):
                document = self._document()
                document["schema"] = schema
                root = self._prepare(document)
                code, out, err, _send, _time = self._run(root)
                self.assertEqual(code, 0, err)
                self.assertIn("Koşulu sağlanan: 2 hisse — 1 yukarı, 1 aşağı", out)

    def test_layout_v1_flag_is_the_previous_single_flat_text(self):
        root = self._prepare()
        document = self._document()
        code, out, _err, send, _time = self._run(root, "--layout", "v1")
        self.assertEqual(code, 0)
        send.assert_not_called()
        directions = {"H1": "negative", "H2": "positive", "H3": "negative", "H4": "positive"}
        expected = "".join(
            f"--- mesaj {i}/{len(msgs)} (dry-run; ağ kullanılmadı) ---\n{m}\n"
            for msgs in [fmt.render_notification(document, notify.load_rule_texts(REAL_RULE_TEXTS_FILE), directions)]
            for i, m in enumerate(msgs, start=1)
        )
        self.assertEqual(out, expected)

    def test_layout_v1_output_is_pinned_to_the_pre_change_text(self):
        # Digests taken from the v1 renderer of the parent commit (before the
        # percent / label wording change of layout v2); v1 must not move.
        pinned = {
            True: "4228844759fd405baea3fb4b160d3d84e41dae7404871ec509147d310edc2d1d",
            False: "574571dee83a65481eb0badb6748385a1c6c03a21d6cd04fe49aa386032e71c9",
        }
        for labelled, digest in pinned.items():
            with self.subTest(labelled=labelled):
                messages = fmt.render_notification(
                    _five_stock_doc(), RULE_TEXTS, DIRECTIONS if labelled else None)
                self.assertEqual(hashlib.sha256("\n\x00".join(messages).encode()).hexdigest(), digest)

    def test_no_direction_label_flag_gives_the_unlabelled_v2_layout(self):
        root = self._prepare()
        code, out, _err, _send, _time = self._run(root, "--no-direction-label")
        self.assertEqual(code, 0)
        for forbidden in ("yönlü", "▲", "▼", "Ne demek", "Ön kayıtlı", SUMMARY_WARNING_TR):
            self.assertNotIn(forbidden, out)
        self.assertIn(f"{DEVELOPMENT[0]} — koşul sağlandı", out)
        self.assertIn(NOTICE_TEXT_TR, out)

    def test_unverifiable_rules_file_never_labels_in_v2(self):
        root = self._prepare()
        bad = json.loads((ROOT / "configs" / "scanner" / "rules_v1.json").read_text(encoding="utf-8"))
        bad["rules"][0]["raw_sha256"] = "0" * 64
        bad_path = root / "rules.json"
        bad_path.write_text(json.dumps(bad), encoding="utf-8")
        code, out, err, _send, _time = self._run(root, "--rules-file", str(bad_path))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("yön bilinmiyor"), 3)  # summary count + two stocks
        for forbidden in ("yönlü", "Ne demek", "Vade", SUMMARY_WARNING_TR):
            self.assertNotIn(forbidden, out)
        self.assertIn("yön etiketi kaynağı doğrulanamadı", err)

    def test_missing_or_bad_explanations_file_fails_closed(self):
        root = self._prepare()
        code, out, err, send, _time = self._run(root, "--rule-explanations", str(root / "missing.json"))
        self.assertEqual(code, 1)
        self.assertIn("Notify failed", err)
        self.assertEqual(out, "")
        send.assert_not_called()

    def test_send_success_sends_in_order_with_pause_and_writes_a_full_receipt(self):
        root = self._prepare()
        code, _out, err, send, mock_time = self._run(
            root, "--send", send_effects=[SendResult(101), SendResult(102), SendResult(103)])
        self.assertEqual(code, 0)
        texts = [item.args[2] for item in send.call_args_list]
        self.assertEqual(len(texts), 3)
        self.assertTrue(texts[0].startswith("FinTR · 02.10.2026"))
        self.assertTrue(texts[1].startswith(f"▲ {DEVELOPMENT[0]}"))
        self.assertTrue(texts[2].startswith(f"▼ {DEVELOPMENT[1]}"))
        self.assertEqual(mock_time.sleep.call_args_list, [call(1.0), call(1.0)])  # between messages only
        files = self._receipts(root)
        self.assertEqual([path.name for path in files], ["receipt.json"])
        receipt = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "fintr_notify_receipt_v2")
        self.assertEqual(receipt["layout"], "v2")
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["message_count"], 3)
        self.assertEqual(receipt["message_sha256"], [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts])
        self.assertEqual(receipt["telegram_message_ids"], [101, 102, 103])
        self.assertEqual([item["status"] for item in receipt["message_status"]], ["sent"] * 3)
        self.assertEqual(receipt["rules_file_sha256"], "9" * 64)
        self.assertEqual(receipt["rule_explanations"]["sha256"], EXPLANATIONS.sha256)
        self.assertEqual(receipt["delivery_config"]["inter_message_delay_seconds"], 1.0)
        self.assertIsNone(receipt["failed_at_index"])
        dumped = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn("token", dumped.lower())
        self.assertNotIn("chat_id", dumped)
        self.assertNotIn(FAKE_TOKEN, dumped + err)
        self.assertNotIn(FAKE_CHAT_ID, dumped)

    def test_message_delay_override_and_zero_means_no_sleep(self):
        root = self._prepare()
        _code, _out, _err, _send, mock_time = self._run(
            root, "--send", "--message-delay-seconds", "0",
            send_effects=[SendResult(1), SendResult(2), SendResult(3)])
        mock_time.sleep.assert_not_called()

    def test_second_send_without_resend_is_rejected_with_the_pipeline_marker(self):
        root = self._prepare()
        self._run(root, "--send", send_effects=[SendResult(1), SendResult(2), SendResult(3)])
        before = (root / "out" / "2026-10-02" / "receipt.json").read_text(encoding="utf-8")
        code, _out, err, send, _time = self._run(root, "--send")
        self.assertEqual(code, 1)
        self.assertIn("receipt zaten var", err)  # scripts/run_daily_pipeline.py RECEIPT_EXISTS_MARK
        send.assert_not_called()
        self.assertEqual((root / "out" / "2026-10-02" / "receipt.json").read_text(encoding="utf-8"), before)

    def test_resend_sends_everything_again_under_a_new_receipt(self):
        root = self._prepare()
        self._run(root, "--send", send_effects=[SendResult(1), SendResult(2), SendResult(3)])
        code, _out, _err, send, _time = self._run(
            root, "--send", "--resend", send_effects=[SendResult(4), SendResult(5), SendResult(6)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 3)
        files = self._receipts(root)
        self.assertEqual(len(files), 2)
        newest = json.loads(files[1].read_text(encoding="utf-8")) if files[1].name != "receipt.json" \
            else json.loads(files[0].read_text(encoding="utf-8"))
        self.assertTrue(newest["resend"])
        self.assertEqual(newest["telegram_message_ids"], [4, 5, 6])

    def test_partial_send_is_recorded_and_the_rerun_sends_only_the_unsent_messages(self):
        root = self._prepare()
        failure = TelegramSendError(f"Telegram API hatası (HTTP 500): reflecting bot{FAKE_TOKEN} chat {FAKE_CHAT_ID}")
        code, _out, err, send, _time = self._run(
            root, "--send", send_effects=[SendResult(11), SendResult(12), failure])
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 3)
        self.assertNotIn(FAKE_TOKEN, err)
        self.assertNotIn(FAKE_CHAT_ID, err)
        self.assertIn("kısmi gönderim (2/3 mesaj; 3. mesajda hata)", err)
        first_texts = [item.args[2] for item in send.call_args_list]
        files = self._receipts(root)
        self.assertEqual([path.name for path in files], ["receipt.json"])
        partial = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["failed_at_index"], 3)
        self.assertEqual(partial["error_kind"], "TelegramSendError")
        self.assertEqual([item["status"] for item in partial["message_status"]], ["sent", "sent", "not_sent"])
        self.assertEqual(partial["telegram_message_ids"], [11, 12])
        self.assertEqual(partial["message_count"], 3)
        dumped = json.dumps(partial, ensure_ascii=False)
        self.assertNotIn(FAKE_TOKEN, dumped)
        self.assertNotIn(FAKE_CHAT_ID, dumped)
        self.assertNotIn("token", dumped.lower())

        # Re-run for the same as_of, no --resend: only message 3 goes out.
        code, _out, err, send, mock_time = self._run(root, "--send", send_effects=[SendResult(13)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args_list[0].args[2], first_texts[2])
        mock_time.sleep.assert_not_called()  # nothing before the first send of a run
        self.assertIn("kısmi gönderim sürdürülüyor (2/3", err)
        files = self._receipts(root)
        self.assertEqual([path.name for path in files], ["receipt.json", "receipt.part-002.json"])
        # The first receipt is never rewritten.
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), partial)
        part = json.loads(files[1].read_text(encoding="utf-8"))
        self.assertTrue(part["complete"])
        self.assertEqual(part["continues"], "receipt.json")
        self.assertEqual(part["telegram_message_ids"], [13])
        self.assertEqual([item["telegram_message_id"] for item in part["message_status"]], [11, 12, 13])
        self.assertEqual(part["message_sha256"], partial["message_sha256"])

        # A third run is now the ordinary "already sent" rejection.
        code, _out, err, send, _time = self._run(root, "--send")
        self.assertEqual(code, 1)
        self.assertIn("receipt zaten var", err)
        send.assert_not_called()

    def test_repeated_partial_failures_keep_resuming_without_duplicates(self):
        root = self._prepare()
        boom = TelegramSendError("boom")
        self._run(root, "--send", send_effects=[SendResult(1), boom])            # 1/3 sent
        code, _o, _e, send, _t = self._run(root, "--send", send_effects=[SendResult(2), boom])  # 2/3
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 2)
        code, _o, _e, send, _t = self._run(root, "--send", send_effects=[SendResult(3)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        names = [path.name for path in self._receipts(root)]
        self.assertEqual(names, ["receipt.json", "receipt.part-002.json", "receipt.part-003.json"])
        last = json.loads((root / "out" / "2026-10-02" / "receipt.part-003.json").read_text(encoding="utf-8"))
        self.assertEqual([item["telegram_message_id"] for item in last["message_status"]], [1, 2, 3])
        self.assertTrue(last["complete"])

    def test_v1_layout_send_error_is_redacted_of_token_and_chat_id(self):
        root = self._prepare()
        failure = TelegramSendError(f"HTTP 400: bot{FAKE_TOKEN} chat {FAKE_CHAT_ID}")
        code, _out, err, _send, _time = self._run(root, "--layout", "v1", "--send", send_effects=[failure])
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_TOKEN, err)
        self.assertNotIn(FAKE_CHAT_ID, err)

    def test_ctrl_c_mid_send_still_records_what_was_sent(self):
        root = self._prepare()
        code, _out, err, send, _time = self._run(
            root, "--send", send_effects=[SendResult(21), KeyboardInterrupt()])
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 2)
        receipt = json.loads((root / "out" / "2026-10-02" / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual([item["status"] for item in receipt["message_status"]], ["sent", "not_sent", "not_sent"])
        self.assertEqual(receipt["error_kind"], "KeyboardInterrupt")
        self.assertNotIn(FAKE_TOKEN, err)
        code, _out, _err, send, _time = self._run(root, "--send", send_effects=[SendResult(22), SendResult(23)])
        self.assertEqual((code, send.call_count), (0, 2))  # messages 2 and 3 only, no duplicate of 1

    def test_unwritable_receipt_after_sending_is_reported_without_a_traceback(self):
        root = self._prepare()
        stderr = io.StringIO()
        with patch.dict(os.environ, self.ENV), \
                patch.object(notify, "send_message", MagicMock(side_effect=[SendResult(1), SendResult(2), SendResult(3)])), \
                patch.object(notify, "time", MagicMock()), \
                patch.object(notify, "_write_receipt", side_effect=PermissionError("disk")), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = notify.main(self._argv(root, "--send"))
        self.assertEqual(code, 1)
        self.assertIn("receipt yazılamadı (PermissionError)", stderr.getvalue())
        self.assertIn("gönderilen mesaj sayısı: 3", stderr.getvalue())

    def test_failure_on_the_first_message_writes_no_receipt_and_the_rerun_sends_all(self):
        root = self._prepare()
        code, _out, err, send, _time = self._run(
            root, "--send", send_effects=[TelegramSendError(f"bot{FAKE_TOKEN} down")])
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_TOKEN, err)
        self.assertEqual(send.call_count, 1)
        self.assertFalse((root / "out").exists())
        code, _out, _err, send, _time = self._run(
            root, "--send", send_effects=[SendResult(1), SendResult(2), SendResult(3)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 3)

    def test_resume_is_refused_when_the_content_changed(self):
        root = self._prepare()
        self._run(root, "--send", send_effects=[SendResult(1), TelegramSendError("boom")])
        self._write(root, self._document(value_a="0.05"))  # same as_of, different observations
        code, _out, err, send, _time = self._run(root, "--send")
        self.assertEqual(code, 1)
        self.assertIn("farklı", err)
        self.assertIn("--resend", err)
        send.assert_not_called()
        self.assertEqual([path.name for path in self._receipts(root)], ["receipt.json"])

    def test_resend_after_a_partial_session_starts_a_fresh_full_send(self):
        root = self._prepare()
        self._run(root, "--send", send_effects=[SendResult(1), TelegramSendError("boom")])
        code, _out, _err, send, _time = self._run(
            root, "--send", "--resend", send_effects=[SendResult(7), SendResult(8), SendResult(9)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 3)

    def test_legacy_v1_layout_receipt_blocks_a_v2_resend_without_the_flag(self):
        root = self._prepare()
        self._run(root, "--layout", "v1", "--send", send_effects=[SendResult(1)])
        code, _out, err, send, _time = self._run(root, "--send")
        self.assertEqual(code, 1)
        self.assertIn("receipt zaten var", err)
        send.assert_not_called()

    def test_skip_empty_still_sends_nothing_without_positive_observations(self):
        quiet = _doc({DEVELOPMENT[0]: _block([_obs("H1", condition_met=False)])})
        root = self._prepare(quiet)
        code, _out, err, send, _time = self._run(root, "--send", "--skip-empty")
        self.assertEqual(code, 0)
        self.assertIn("--skip-empty", err)
        send.assert_not_called()

    def test_empty_day_sends_one_short_message(self):
        quiet = _doc({DEVELOPMENT[0]: _block([_obs("H1", condition_met=False)])})
        root = self._prepare(quiet)
        code, _out, _err, send, mock_time = self._run(root, "--send", send_effects=[SendResult(1)])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        self.assertIn("bugün hiçbir hissede koşul sağlanmadı", send.call_args_list[0].args[2])
        mock_time.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
