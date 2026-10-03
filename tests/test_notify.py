"""Offline tests for ``fintr.notify`` (``format.py``/``telegram.py``) and
``scripts/notify_observations.py``.

No network: every HTTP call is mocked at ``urllib.request.urlopen``.
Observation documents here are synthetic, built directly to the
``fintr_scanner_observations_v1`` shape (``docs/scanner.md``); nothing is
a real capture, a real financial figure or a real Telegram token.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fintr.notify import format as fmt
from fintr.notify.telegram import SendResult, TelegramSendError, send_message
from fintr.scanner.observe import NOTICE_TEXT_TR, VALIDATION_STATUS

from test_symbol_guard import DEVELOPMENT, EXCLUDED, HOLDOUT, GuardEnvMixin


ROOT = Path(__file__).resolve().parents[1]
REAL_RULE_TEXTS_FILE = ROOT / "configs" / "notify" / "rule_texts_v1.json"

FAKE_TOKEN = "123456789:AAFakeNotARealTelegramBotTokenXYZ"
FAKE_CHAT_ID = "555000111"

RULE_TEXTS = {
    "H1": "sentetik H1 açıklaması",
    "H2": "sentetik H2 açıklaması",
    "H3": "sentetik H3 açıklaması",
    "H4": "sentetik H4 açıklaması",
}

_FORBIDDEN_WORDS = (
    "score", "rank", "return", "target", "al", "sat", "öneri", "kesin", "hedef", "stop", "kâr",
)
# Efe decision 2026-10-02: the ONLY buy/sell wording allowed outside the fixed
# disclaimer. Everything else in _FORBIDDEN_WORDS stays forbidden.
_ALLOWED_LABEL_PHRASES = tuple(fmt.DIRECTION_LABELS_TR.values())


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"notify_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notify = _load_script("notify_observations")


def _assert_no_forbidden_words(testcase: unittest.TestCase, text: str) -> None:
    lowered = text.lower()
    for phrase in _ALLOWED_LABEL_PHRASES:
        lowered = lowered.replace(phrase.lower(), "")
    for word in _FORBIDDEN_WORDS:
        pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
        matches = re.findall(pattern, lowered)
        testcase.assertFalse(matches, f"forbidden word {word!r} found in: {text!r}")


def _feature(name: str, value, *, institution_labels: dict | None = None) -> dict:
    return {
        "name": name,
        "params": {},
        "formula": "synthetic formula text, not a real computation",
        "value": value,
        "data_families": ["price"],
        "min_date": "2026-09-25",
        "max_date": "2026-09-25",
        "institution_labels": institution_labels or {},
    }


def _obs(rule_id: str, *, condition_met, suppressed=None, features=()) -> dict:
    return {
        "rule_id": rule_id,
        "family_role": "primary" if rule_id in ("H1", "H2") else "secondary_descriptive",
        "as_of": "2026-09-25",
        "suppressed": suppressed,
        "condition_met": condition_met,
        "not_evaluable_reason": "missing_data" if condition_met is None and suppressed is None else None,
        "features": list(features),
        "validation_status": VALIDATION_STATUS,
    }


def _freshness(**overrides) -> dict:
    base = {
        "last_akd_server_date": "2026-09-25",
        "last_custody_date": "2026-09-23",
        "last_price_date": "2026-09-25",
        "custody_behind_akd_server_dates": 2,
        "custody_flagged_stale_t_plus_2": True,
    }
    base.update(overrides)
    return base


def _document(symbols: dict, *, warning: str | None = NOTICE_TEXT_TR) -> dict:
    return {
        "schema": fmt.OBSERVATIONS_SCHEMA,
        "generated_at_utc": "2026-09-25T12:00:00Z",
        "code_commit": "deadbeef",
        "python_version": "3.11.0",
        "rules_file": "configs/scanner/rules_v1.json",
        "rules_file_sha256": "0" * 64,
        "split_guard": {"schema": "fintr_symbol_guard_report_v1"},
        "warning": warning,
        "symbols": symbols,
    }


def _symbol_block(*, scans: list, composition: dict | None = None) -> dict:
    block = {
        "as_of_source": "explicit",
        "input_files": {
            "period_capture": {"path": "synthetic", "sha256": "0" * 64},
            "price_history": {"path": "synthetic", "sha256": "0" * 64},
        },
        "scans": scans,
    }
    if composition is not None:
        block["composition"] = composition
    return block


def _composition_block(*, gaps=0, unsettled=0, revisions=0, window=...,
                       in_window=(0, 0, 0)) -> dict:
    """Minimal ``composition`` block shape (ADR §4; docs/scanner.md) --
    only the fields ``fintr.notify.format`` actually reads. ``window`` left
    at its default omits the window fields (an older v2 file); ``None`` or a
    ``{"start","end"}`` dict adds them with ``in_window`` = (gaps, unsettled,
    revisions)."""
    block = {
        "composition_sha256": "0" * 64,
        "composer_version": "compose_period_capture_v1",
        "inputs": [{"kind": "base", "path": "synthetic", "sha256": "0" * 64}],
        "gaps": {"count": gaps, "dates": []},
        "unsettled": {"day_count": unsettled, "entries": []},
        "revisions": {"day_count": revisions, "entries": []},
    }
    if window is not ...:
        block["window"] = window
        block["gaps"]["in_window_count"] = in_window[0] if window else None
        block["unsettled"]["in_window_day_count"] = in_window[1] if window else None
        block["revisions"]["in_window_day_count"] = in_window[2] if window else None
    return block


# --- format.py -----------------------------------------------------------


class FormatTests(unittest.TestCase):
    def test_no_positive_observations_when_all_false_or_null(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(),
                "observations": [_obs("H1", condition_met=False), _obs("H2", condition_met=None)],
            }]),
        })
        self.assertFalse(fmt.has_positive_observations(document))

    def test_positive_observations_detected(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.0123")])],
            }]),
        })
        self.assertTrue(fmt.has_positive_observations(document))

    def test_observation_line_includes_rule_text_and_feature_value(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.0123")])],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("AKFIS H1: sentetik H1 açıklaması", text)
        self.assertIn("vwap_proxy_vs_close=-0.0123", text)

    def test_institution_label_included_when_resolved(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H3", condition_met=True, features=[
                    _feature("flow_run_length", "3", institution_labels={
                        "XYZ": {"status": "resolved", "title": "Sentetik Aracı Kurum", "short_title": "SAK"},
                        "ABC": {"status": "unresolved", "title": None, "short_title": None},
                    }),
                ])],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("XYZ Sentetik Aracı Kurum", text)
        self.assertNotIn("ABC", text)

    def test_no_observations_renders_short_notice(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("Bu taramada condition_met=true gözlem yok.", text)

    def test_suppressed_counts_grouped_by_reason(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [
                    _obs("H1", condition_met=None, suppressed="ratio_regime"),
                    _obs("H2", condition_met=None, suppressed="ratio_regime"),
                    _obs("H3", condition_met=None, suppressed="custody_scale_jump"),
                ],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("oran rejimi=2", text)
        self.assertIn("takas ölçek sıçraması=1", text)

    def test_freshness_warnings_for_stale_and_missing_data(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25",
                "freshness": _freshness(last_price_date=None, custody_flagged_stale_t_plus_2=True),
                "observations": [_obs("H1", condition_met=False)],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("T+2 gerisinde", text)
        self.assertIn("eksik veri (fiyat)", text)

    def test_ends_with_fixed_warning_and_validation_status(self):
        document = _document({})
        text = fmt.render_notification_text(document, RULE_TEXTS)
        suffix = "\n".join([NOTICE_TEXT_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"
        self.assertTrue(text.endswith(suffix))

    def test_no_forbidden_words_outside_fixed_disclaimer(self):
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(last_price_date=None),
                "observations": [
                    _obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.0123")]),
                    _obs("H4", condition_met=True, features=[
                        _feature("top1_akd_net_share_of_volume", "0.15"),
                        _feature("vwap_proxy_vs_close", "0.02"),
                    ]),
                ],
            }]),
            "GARAN": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(),
                "observations": [_obs("H1", condition_met=None, suppressed="ratio_regime")],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        suffix = "\n".join([NOTICE_TEXT_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"
        self.assertTrue(text.endswith(suffix))
        body = text[: -len(suffix)]
        _assert_no_forbidden_words(self, body)

    def test_split_message_keeps_short_text_as_one_message(self):
        text = "kısa mesaj"
        self.assertEqual(fmt.split_message(text), [text])

    def test_split_message_splits_on_line_boundaries_within_limit(self):
        lines = [f"satır {i}: " + ("x" * 80) for i in range(200)]
        text = "\n".join(lines)
        self.assertGreater(len(text), fmt.TELEGRAM_MESSAGE_LIMIT)
        chunks = fmt.split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), fmt.TELEGRAM_MESSAGE_LIMIT)
        self.assertEqual("\n".join(chunks), text)

    def test_split_message_never_exceeds_limit_for_a_single_long_line(self):
        text = "y" * 9000
        chunks = fmt.split_message(text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), fmt.TELEGRAM_MESSAGE_LIMIT)
        self.assertEqual("".join(chunks), text)

    # --- ADR §4: --daily-root composition data-status line ---------------

    def test_no_data_status_line_without_composition_block(self):
        # A v1-shaped document (no composition anywhere, as scan_observations.py
        # emits by default) must render exactly as before -- no new line.
        document = _document({
            "AKFIS": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertNotIn("veri durumu", text)

    def test_data_status_line_reports_gap_unsettled_revision_counts(self):
        document = _document({
            "AKFIS": _symbol_block(
                scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=False)],
                }],
                composition=_composition_block(gaps=2, unsettled=3, revisions=1),
            ),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn(
            "AKFIS veri durumu (tüm dönem): boşluk=2, kesinleşmemiş gün=3, revize gün=1", text,
        )

    def _one_symbol_text(self, composition: dict) -> str:
        document = _document({
            "AKFIS": _symbol_block(
                scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=False)],
                }],
                composition=composition,
            ),
        })
        return fmt.render_notification_text(document, RULE_TEXTS)

    def test_data_status_line_uses_in_window_counts_when_window_present(self):
        text = self._one_symbol_text(_composition_block(
            gaps=9, unsettled=300, revisions=8,
            window={"start": "2026-09-08", "end": "2026-09-25"}, in_window=(1, 2, 0),
        ))
        self.assertIn(
            "AKFIS veri durumu (2026-09-08..2026-09-25): boşluk=1, kesinleşmemiş gün=2, "
            "revize gün=0", text,
        )
        self.assertNotIn("300", text)

    def test_data_status_line_null_window_reports_no_window(self):
        text = self._one_symbol_text(_composition_block(gaps=9, unsettled=300, window=None))
        self.assertIn("AKFIS veri durumu: pencere yok (günlük kayıt yok)", text)
        self.assertNotIn("300", text)

    def test_data_status_line_present_per_symbol_only_when_composed(self):
        document = _document({
            "AKFIS": _symbol_block(
                scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=False)],
                }],
                composition=_composition_block(gaps=0, unsettled=0, revisions=0),
            ),
            "GARAN": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }]),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertIn("AKFIS veri durumu (", text)
        self.assertNotIn("GARAN veri durumu", text)

    def test_data_status_line_carries_no_forbidden_words(self):
        document = _document({
            "AKFIS": _symbol_block(
                scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
                }],
                composition=_composition_block(gaps=5, unsettled=4, revisions=3),
            ),
        })
        text = fmt.render_notification_text(document, RULE_TEXTS)
        suffix = "\n".join([NOTICE_TEXT_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"
        self.assertTrue(text.endswith(suffix))
        _assert_no_forbidden_words(self, text[: -len(suffix)])


# --- direction label (Efe decision 2026-10-02) ---------------------------------

DIRECTIONS = {"H1": "negative", "H2": "positive", "H3": "negative", "H4": "positive"}
UP_LABEL = "beklenen yön: yukarı (al yönlü)"
DOWN_LABEL = "beklenen yön: aşağı (sat yönlü)"


def _doc_with(*observations) -> dict:
    return _document({
        "AKFIS": _symbol_block(scans=[{
            "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
            "observations": list(observations),
        }]),
    })


def _line_for(text: str, rule_id: str) -> str:
    matches = [line for line in text.split("\n") if line.startswith(f"AKFIS {rule_id}:")]
    assert len(matches) == 1, matches
    return matches[0]


class DirectionLabelTests(unittest.TestCase):
    def test_positive_maps_to_up_and_negative_to_down(self):
        document = _doc_with(
            _obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "0.02")]),
            _obs("H2", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.02")]),
        )
        text = fmt.render_notification_text(document, RULE_TEXTS, DIRECTIONS)
        self.assertTrue(_line_for(text, "H1").endswith(f" — {DOWN_LABEL}"))
        self.assertTrue(_line_for(text, "H2").endswith(f" — {UP_LABEL}"))

    def test_label_only_on_condition_met_lines(self):
        document = _doc_with(
            _obs("H1", condition_met=False),
            _obs("H2", condition_met=None),
            _obs("H3", condition_met=None, suppressed="ratio_regime"),
        )
        text = fmt.render_notification_text(document, RULE_TEXTS, DIRECTIONS)
        self.assertNotIn("yönlü", text)
        self.assertNotIn(fmt.DIRECTION_UNKNOWN_TR, text)
        self.assertNotIn(fmt.DIRECTION_WARNING_TR, text)

    def test_unresolvable_rule_or_bad_direction_gets_marker_not_label(self):
        bad_values = {"H2": "up", "H3": None, "H4": "Positive"}  # H1 missing entirely
        document = _doc_with(*[_obs(rid, condition_met=True) for rid in ("H1", "H2", "H3", "H4")])
        text = fmt.render_notification_text(document, RULE_TEXTS, bad_values)
        for rid in ("H1", "H2", "H3", "H4"):
            line = _line_for(text, rid)
            self.assertTrue(line.endswith(f" — {fmt.DIRECTION_UNKNOWN_TR}"), line)
        for phrase in _ALLOWED_LABEL_PHRASES:
            self.assertNotIn(phrase, text)

    def test_no_label_means_no_warning_and_original_footer(self):
        document = _doc_with(_obs("H1", condition_met=True))
        text = fmt.render_notification_text(document, RULE_TEXTS, {})
        self.assertIn(fmt.DIRECTION_UNKNOWN_TR, text)
        self.assertNotIn(fmt.DIRECTION_WARNING_TR, text)
        self.assertTrue(text.endswith("\n".join([NOTICE_TEXT_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"))

    def test_warning_present_exactly_once_when_labelled(self):
        document = _doc_with(
            _obs("H1", condition_met=True), _obs("H2", condition_met=True),
            _obs("H3", condition_met=True), _obs("H4", condition_met=True),
        )
        text = fmt.render_notification_text(document, RULE_TEXTS, DIRECTIONS)
        self.assertEqual(text.count(fmt.DIRECTION_WARNING_TR), 1)
        messages = fmt.render_notification(document, RULE_TEXTS, DIRECTIONS)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].count(fmt.DIRECTION_WARNING_TR), 1)

    def test_footer_is_not_contradictory_when_labelled(self):
        document = _doc_with(_obs("H1", condition_met=True))
        text = fmt.render_notification_text(document, RULE_TEXTS, DIRECTIONS)
        self.assertNotIn("al/sat sinyali", text)
        self.assertTrue(text.endswith(
            "\n".join([fmt.NOTICE_TEXT_DIRECTION_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"
        ))
        for must in ("kişisel kullanım", "doğrulanmamış hipotez yönü", "niyet veya manipülasyon iddiası"):
            self.assertIn(must, fmt.NOTICE_TEXT_DIRECTION_TR)

    def test_directions_none_is_the_original_text(self):
        document = _doc_with(
            _obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "0.02")]),
        )
        text = fmt.render_notification_text(document, RULE_TEXTS)
        self.assertEqual(text, fmt.render_notification_text(document, RULE_TEXTS, None))
        self.assertNotIn("yönlü", text)
        self.assertNotIn(fmt.DIRECTION_UNKNOWN_TR, text)
        self.assertNotIn(fmt.DIRECTION_WARNING_TR, text)
        self.assertTrue(text.endswith("\n".join([NOTICE_TEXT_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"))

    def test_every_split_chunk_with_a_label_carries_warning_once_within_limit(self):
        observations = [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "0.02")])] * 1
        symbols = {
            f"SYM{i:03d}": _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": list(observations),
            }])
            for i in range(120)
        }
        messages = fmt.render_notification(_document(symbols), RULE_TEXTS, DIRECTIONS)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), fmt.TELEGRAM_MESSAGE_LIMIT)
            if DOWN_LABEL in message:
                self.assertEqual(message.count(fmt.DIRECTION_WARNING_TR), 1)

    def test_guard_allows_exactly_the_two_labels_and_nothing_else(self):
        document = _doc_with(
            _obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "0.02")]),
            _obs("H2", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.02")]),
        )
        text = fmt.render_notification_text(document, RULE_TEXTS, DIRECTIONS)
        suffix = "\n".join([fmt.NOTICE_TEXT_DIRECTION_TR, f"Durum: {VALIDATION_STATUS}"]) + "\n"
        self.assertTrue(text.endswith(suffix))
        _assert_no_forbidden_words(self, text[: -len(suffix)])

    def test_guard_still_catches_other_buy_sell_or_target_wording(self):
        for bad in (
            "AKFIS H1: al yönlü", "sat yönlü", "al!", "sat!", "kesin yükselir", "hedef 12",
            "stop 10", "kâr bekleniyor", "score 3", "rank 1", "return 2", "target 5", "öneri",
            "beklenen yön: yukarı (al yönlü) ve al",
        ):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                _assert_no_forbidden_words(self, bad)
        for ok in _ALLOWED_LABEL_PHRASES:
            _assert_no_forbidden_words(self, ok)

    def test_directions_loaded_from_frozen_specs_match_expected(self):
        from fintr.notify.direction import load_rule_directions
        directions = load_rule_directions(
            ROOT / "configs" / "scanner" / "rules_v1.json", project_root=ROOT,
        )
        self.assertEqual(directions, DIRECTIONS)

    def test_loader_fails_closed_on_hash_mismatch(self):
        from fintr.notify.direction import load_rule_directions
        from fintr.scanner.rules import ScannerRulesError
        document = json.loads((ROOT / "configs" / "scanner" / "rules_v1.json").read_text(encoding="utf-8"))
        document["rules"][0]["raw_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ScannerRulesError):
                load_rule_directions(path, project_root=ROOT)


class RealRuleTextsFileTests(unittest.TestCase):
    def test_real_rule_texts_file_has_h1_to_h4_no_forbidden_words(self):
        rule_texts = notify.load_rule_texts(REAL_RULE_TEXTS_FILE)
        self.assertEqual(set(rule_texts), {"H1", "H2", "H3", "H4"})
        for rule_id, description in rule_texts.items():
            with self.subTest(rule_id=rule_id):
                _assert_no_forbidden_words(self, description)

    def test_wrong_schema_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rule_texts.json"
            path.write_text(json.dumps({"schema": "other", "rules": {"H1": "x"}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                notify.load_rule_texts(path)


# --- telegram.py -----------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(url="https://api.telegram.org/x", code=code, msg="err",
                                   hdrs=None, fp=io.BytesIO(payload))


class TelegramSendTests(unittest.TestCase):
    def test_empty_token_or_chat_id_rejected(self):
        with self.assertRaises(ValueError):
            send_message("", FAKE_CHAT_ID, "hi")
        with self.assertRaises(ValueError):
            send_message(FAKE_TOKEN, "", "hi")

    def test_success_returns_message_id(self):
        response = _FakeResponse(json.dumps({"ok": True, "result": {"message_id": 42}}).encode("utf-8"))
        with patch("fintr.notify.telegram.urllib.request.urlopen", return_value=response) as urlopen:
            result = send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertEqual(result, SendResult(message_id=42))
        urlopen.assert_called_once()

    def test_429_short_retry_after_retries_once_and_succeeds(self):
        error = _http_error(429, {"ok": False, "parameters": {"retry_after": 5}})
        success = _FakeResponse(json.dumps({"ok": True, "result": {"message_id": 7}}).encode("utf-8"))
        with patch("fintr.notify.telegram.urllib.request.urlopen", side_effect=[error, success]), \
             patch("fintr.notify.telegram.time.sleep") as sleep:
            result = send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertEqual(result.message_id, 7)
        sleep.assert_called_once_with(5.0)

    def test_429_long_retry_after_raises_without_retry(self):
        error = _http_error(429, {"ok": False, "parameters": {"retry_after": 45}})
        with patch("fintr.notify.telegram.urllib.request.urlopen", side_effect=[error]) as urlopen, \
             patch("fintr.notify.telegram.time.sleep") as sleep:
            with self.assertRaises(TelegramSendError):
                send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        sleep.assert_not_called()
        self.assertEqual(urlopen.call_count, 1)

    def test_error_message_never_contains_token_or_url(self):
        error = _http_error(400, {
            "ok": False,
            "description": f"Bad Request: reflecting bot{FAKE_TOKEN} back for debugging",
        })
        with patch("fintr.notify.telegram.urllib.request.urlopen", side_effect=[error]):
            with self.assertRaises(TelegramSendError) as ctx:
                send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_url_error_message_redacted(self):
        with patch("fintr.notify.telegram.urllib.request.urlopen",
                   side_effect=[urllib.error.URLError("connection refused")]):
            with self.assertRaises(TelegramSendError) as ctx:
                send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_incomplete_read_on_success_path_redacted(self):
        # http.client.IncompleteRead is neither an OSError nor a ValueError
        # subclass, so a truncated response body must still be caught and
        # redacted, not escape as a raw, unredacted exception.
        import http.client

        class _TruncatedResponse(_FakeResponse):
            def read(self):
                raise http.client.IncompleteRead(b"", expected=10)

        with patch("fintr.notify.telegram.urllib.request.urlopen",
                   return_value=_TruncatedResponse(b"")):
            with self.assertRaises(TelegramSendError) as ctx:
                send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_incomplete_read_on_http_error_body_redacted(self):
        import http.client

        class _TruncatedHTTPError(urllib.error.HTTPError):
            def read(self, *args, **kwargs):
                raise http.client.IncompleteRead(b"", expected=10)

        error = _TruncatedHTTPError(url=f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
                                     code=400, msg="err", hdrs=None, fp=None)
        with patch("fintr.notify.telegram.urllib.request.urlopen", side_effect=[error]):
            with self.assertRaises(TelegramSendError) as ctx:
                send_message(FAKE_TOKEN, FAKE_CHAT_ID, "merhaba")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


# --- scripts/notify_observations.py ----------------------------------------


class ScriptTests(GuardEnvMixin, unittest.TestCase):
    def _write_observations(self, scanner_dir: Path, as_of: str, document: dict) -> Path:
        target_dir = scanner_dir / as_of
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "observations.json"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return target

    def _base_argv(self, scanner_dir: Path, out_dir: Path, as_of: str) -> list[str]:
        return [
            "--as-of", as_of,
            "--scanner-dir", str(scanner_dir),
            "--out-dir", str(out_dir),
            "--rule-texts", str(REAL_RULE_TEXTS_FILE),
            # These tests pin the v1 (flat single text) behaviour byte for byte;
            # layout v2 is covered by tests/test_notify_layout.py.
            "--layout", "v1",
        ]

    def test_dry_run_never_touches_network(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({
                DEVELOPMENT[0]: _symbol_block(scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
                }]),
            })
            self._write_observations(root / "scanner", "2026-09-25", document)
            with patch("fintr.notify.telegram.urllib.request.urlopen",
                       side_effect=AssertionError("dry-run must never touch the network")):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 0)
            self.assertIn(f"{DEVELOPMENT[0]} H1:", stdout.getvalue())
            self.assertFalse((root / "out").exists())

    def test_send_without_flag_sends_nothing(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            with patch.object(notify, "send_message") as send:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 0)
            send.assert_not_called()

    def test_wrong_schema_rejected(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_observations(root / "scanner", "2026-09-25", {"schema": "not_the_right_schema"})
            with redirect_stderr(io.StringIO()) as errors:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 1)
            self.assertIn("şema", errors.getvalue())

    def test_v1_schema_document_still_accepted(self):
        # An observations.json written before --daily-root existed (or by a
        # run without it) keeps its fintr_scanner_observations_v1 schema and
        # no composition block; the notify tool must still read it exactly
        # as before (ADR §4: old files keep working).
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self.assertEqual(document["schema"], fmt.OBSERVATIONS_SCHEMA_V1)
            self._write_observations(root / "scanner", "2026-09-25", document)
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()) as errors:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 0, errors.getvalue())
            self.assertIn(f"{DEVELOPMENT[0]} H1:", stdout.getvalue())

    def test_v2_schema_document_also_accepted(self):
        # scan_observations.py --daily-root emits fintr_scanner_observations_v2;
        # the notify tool must accept both versions.
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(
                scans=[{
                    "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                    "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
                }],
                composition=_composition_block(gaps=1, unsettled=2, revisions=0),
            )})
            document["schema"] = fmt.OBSERVATIONS_SCHEMA_V2
            self._write_observations(root / "scanner", "2026-09-25", document)
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()) as errors:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 0, errors.getvalue())
            self.assertIn(f"{DEVELOPMENT[0]} H1:", stdout.getvalue())
            self.assertIn(f"{DEVELOPMENT[0]} veri durumu (", stdout.getvalue())

    def test_holdout_symbol_rejected_defense_in_depth(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({HOLDOUT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            with patch.object(notify, "send_message") as send, redirect_stderr(io.StringIO()) as errors:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 1)
            self.assertIn("holdout", errors.getvalue())
            send.assert_not_called()

    def test_excluded_symbol_rejected_defense_in_depth(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({EXCLUDED: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            with patch.object(notify, "send_message") as send, redirect_stderr(io.StringIO()) as errors:
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 1)
            self.assertIn("dışlanan", errors.getvalue())
            send.assert_not_called()

    def test_skip_empty_sends_nothing_when_no_positive_observations(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--skip-empty"]
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = notify.main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_empty_without_skip_empty_prints_short_notice(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=False)],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = notify.main(self._base_argv(root / "scanner", root / "out", "2026-09-25"))
            self.assertEqual(code, 0)
            self.assertIn("gözlem yok", stdout.getvalue())

    def test_send_requires_env_vars(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": "", "FINTR_TELEGRAM_CHAT_ID": ""}), \
                 patch.object(notify, "send_message") as send, redirect_stderr(io.StringIO()) as errors:
                code = notify.main(argv)
            self.assertEqual(code, 1)
            self.assertIn("FINTR_TELEGRAM_BOT_TOKEN", errors.getvalue())
            send.assert_not_called()
            self.assertFalse((root / "out").exists())

    def test_send_success_writes_receipt_without_secrets(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            obs_path = self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=777)) as send:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = notify.main(argv)
            self.assertEqual(code, 0)
            send.assert_called_once()
            receipt_path = root / "out" / "2026-09-25" / "receipt.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema"], "fintr_notify_receipt_v1")
            self.assertEqual(receipt["telegram_message_ids"], [777])
            self.assertEqual(receipt["observations_sha256"],
                              __import__("hashlib").sha256(obs_path.read_bytes()).hexdigest())
            self.assertNotIn("token", json.dumps(receipt).lower())
            self.assertNotIn("chat_id", json.dumps(receipt))
            combined = stdout.getvalue() + stderr.getvalue() + json.dumps(receipt)
            self.assertNotIn(FAKE_TOKEN, combined)

    def test_second_send_without_resend_is_rejected_receipt_not_overwritten(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=1)):
                code = notify.main(argv)
            self.assertEqual(code, 0)
            receipt_path = root / "out" / "2026-09-25" / "receipt.json"
            original_content = receipt_path.read_text(encoding="utf-8")

            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=2)) as send, \
                 redirect_stderr(io.StringIO()) as errors:
                code = notify.main(argv)
            self.assertEqual(code, 1)
            self.assertIn("receipt zaten var", errors.getvalue())
            send.assert_not_called()
            self.assertEqual(receipt_path.read_text(encoding="utf-8"), original_content)

    def test_resend_writes_new_receipt_file_original_untouched(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=1)):
                code = notify.main(argv)
            self.assertEqual(code, 0)
            receipt_dir = root / "out" / "2026-09-25"
            receipt_path = receipt_dir / "receipt.json"
            original_content = receipt_path.read_text(encoding="utf-8")

            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=2)):
                code = notify.main(argv + ["--resend"])
            self.assertEqual(code, 0)
            self.assertEqual(receipt_path.read_text(encoding="utf-8"), original_content)
            other_receipts = [p for p in receipt_dir.iterdir() if p != receipt_path]
            self.assertEqual(len(other_receipts), 1)
            new_receipt = json.loads(other_receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(new_receipt["telegram_message_ids"], [2])
            self.assertTrue(new_receipt["resend"])

    def test_token_redacted_end_to_end_on_network_failure(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _document({DEVELOPMENT[0]: _symbol_block(scans=[{
                "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
                "observations": [_obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.01")])],
            }])})
            self._write_observations(root / "scanner", "2026-09-25", document)
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            error = _http_error(400, {"ok": False, "description": f"reflecting bot{FAKE_TOKEN} back"})
            with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}), \
                 patch("fintr.notify.telegram.urllib.request.urlopen", side_effect=[error]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = notify.main(argv)
            self.assertEqual(code, 1)
            self.assertNotIn(FAKE_TOKEN, stdout.getvalue())
            self.assertNotIn(FAKE_TOKEN, stderr.getvalue())
            self.assertFalse((root / "out").exists())

    def _run_dry(self, root: Path, extra: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + extra
        with patch("fintr.notify.telegram.urllib.request.urlopen",
                   side_effect=AssertionError("dry-run must never touch the network")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = notify.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _two_rule_document(self) -> dict:
        return _document({DEVELOPMENT[0]: _symbol_block(scans=[{
            "as_of": "2026-09-25", "freshness": _freshness(custody_flagged_stale_t_plus_2=False),
            "observations": [
                _obs("H1", condition_met=True, features=[_feature("vwap_proxy_vs_close", "0.02")]),
                _obs("H2", condition_met=True, features=[_feature("vwap_proxy_vs_close", "-0.02")]),
            ],
        }])})

    def test_default_labels_from_real_frozen_specs_with_one_warning(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_observations(root / "scanner", "2026-09-25", self._two_rule_document())
            code, out, _ = self._run_dry(root, [])
            self.assertEqual(code, 0)
            self.assertEqual(out.count(DOWN_LABEL), 1)  # H1 negative
            self.assertEqual(out.count(UP_LABEL), 1)  # H2 positive
            self.assertEqual(out.count(fmt.DIRECTION_WARNING_TR), 1)

    def test_no_direction_label_flag_restores_previous_output(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._two_rule_document()
            self._write_observations(root / "scanner", "2026-09-25", document)
            code, out, _ = self._run_dry(root, ["--no-direction-label"])
            self.assertEqual(code, 0)
            expected = "".join(
                f"--- mesaj {i}/{len(msgs)} (dry-run; ağ kullanılmadı) ---\n{m}\n"
                for msgs in [fmt.render_notification(document, notify.load_rule_texts(REAL_RULE_TEXTS_FILE))]
                for i, m in enumerate(msgs, start=1)
            )
            self.assertEqual(out, expected)
            self.assertNotIn("yönlü", out)
            self.assertNotIn(fmt.DIRECTION_WARNING_TR, out)
            self.assertIn(NOTICE_TEXT_TR, out)

    def test_unverifiable_rules_file_marks_unknown_and_never_labels(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_observations(root / "scanner", "2026-09-25", self._two_rule_document())
            bad = json.loads((ROOT / "configs" / "scanner" / "rules_v1.json").read_text(encoding="utf-8"))
            bad["rules"][0]["raw_sha256"] = "0" * 64
            bad_path = root / "rules.json"
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            code, out, err = self._run_dry(root, ["--rules-file", str(bad_path)])
            self.assertEqual(code, 0)
            self.assertEqual(out.count(fmt.DIRECTION_UNKNOWN_TR), 2)
            self.assertNotIn("yönlü", out)
            self.assertNotIn(fmt.DIRECTION_WARNING_TR, out)
            self.assertIn("yön etiketi kaynağı doğrulanamadı", err)

    def test_send_receipt_hashes_the_labelled_messages_and_resend_unchanged(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_observations(root / "scanner", "2026-09-25", self._two_rule_document())
            argv = self._base_argv(root / "scanner", root / "out", "2026-09-25") + ["--send"]
            env = {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}
            with patch.dict(os.environ, env), \
                 patch.object(notify, "send_message", return_value=SendResult(message_id=5)) as send:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(notify.main(argv), 0)
                    self.assertEqual(notify.main(argv), 1)  # existing receipt: still rejected
                    self.assertEqual(notify.main(argv + ["--resend"]), 0)
            sent = send.call_args_list[0].args[2]
            self.assertIn(UP_LABEL, sent)
            receipt = json.loads((root / "out" / "2026-09-25" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["message_sha256"],
                             [__import__("hashlib").sha256(sent.encode("utf-8")).hexdigest()])
            self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
