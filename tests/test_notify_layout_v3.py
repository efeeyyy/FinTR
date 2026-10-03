"""Offline tests for notification layout v3 (``fintr.notify.layout_v3``), the
Telegram "signal chat" (Efe's decision of 2026-10-03): the pure renderer and
the v3 paths of ``scripts/notify_observations.py``.

No network: ``send_message`` is patched on the script module. Documents are
synthetic (helpers of ``test_notify``/``test_notify_layout``); nothing is a
real capture or financial figure. The accepted-note integration (real note
files through the fake model CLI) and the pipeline order are in
``tests/test_llm_note.py``. Layout v1/v2 stay pinned by ``test_notify.py`` and
``test_notify_layout.py`` (byte hashes).
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from fintr.notify.guard import find_forbidden_words
from fintr.notify.layout_v3 import (
    ASSESSMENT_WORDS, DIRECTION_SHORT_V3_TR, HEADER_SUFFIX_TR, render_layout_v3,
)
from fintr.notify.telegram import SendResult

from test_notify import DIRECTIONS, FAKE_CHAT_ID, FAKE_TOKEN, REAL_RULE_TEXTS_FILE, ROOT, _composition_block, notify
from test_notify_layout import FREEZE_WINDOW, _block, _doc, _hit, _obs
from test_symbol_guard import DEVELOPMENT, GuardEnvMixin


AS_OF = "2026-10-02"
HEADER = f"📊 02.10 {HEADER_SUFFIX_TR}"
UP, DOWN = DIRECTION_SHORT_V3_TR["positive"], DIRECTION_SHORT_V3_TR["negative"]
ALLOWED = (UP, DOWN)


def _render(document: dict, directions=DIRECTIONS, assessments=None, **kwargs) -> list[str]:
    return render_layout_v3(document, directions, assessments, **kwargs)


def _lines(document: dict, **kwargs) -> list[str]:
    messages = _render(document, **kwargs)
    assert len(messages) == 1, messages
    return messages[0].split("\n")


CLEAN_WINDOW = _composition_block(window=FREEZE_WINDOW, in_window=(0, 0, 0))


def _stock(*rule_ids: str, composition=CLEAN_WINDOW, **block_kwargs) -> dict:
    """A stock hitting ``rule_ids`` with a clean window unless told otherwise."""
    return _block([_hit(rule_id) for rule_id in rule_ids], composition=composition, **block_kwargs)


class FormatTests(unittest.TestCase):
    def test_header_arrows_rule_ids_and_dash_without_a_note(self):
        document = _doc({"AKBNK": _stock("H2"), "BIMAS": _stock("H1")})
        self.assertEqual(_lines(document), [
            "📊 02.10 sinyaller (doğrulanmamış hipotez, tavsiye değil)",
            "AKBNK ↑ al yönlü · H2 · Opus: —",
            "BIMAS ↓ sat yönlü · H1 · Opus: —",
        ])

    def test_direction_comes_only_from_the_frozen_predicted_direction(self):
        document = _doc({"AKBNK": _stock("H2"), "BIMAS": _stock("H2")})
        flipped = {**DIRECTIONS, "H2": "negative"}
        self.assertEqual(_lines(document, directions=flipped)[1:], [
            "AKBNK ↓ sat yönlü · H2 · Opus: —", "BIMAS ↓ sat yönlü · H2 · Opus: —"])
        # a rule that is not exactly positive/negative, or absent, gets no direction part
        for directions in ({"H2": "sideways"}, {"H2": None}, {}, {"H1": "negative"}):
            with self.subTest(directions=directions):
                self.assertEqual(_lines(document, directions=directions)[1:], [
                    "AKBNK · H2 · Opus: —", "BIMAS · H2 · Opus: —"])

    def test_no_direction_label_writes_no_direction_part(self):
        document = _doc({"AKBNK": _stock("H2"), "BIMAS": _stock("H1")})
        lines = _lines(document, directions=None)
        self.assertEqual(lines[0], HEADER)  # the unverified-hypothesis warning stays in the header
        self.assertEqual(lines[1:], ["AKBNK · H2 · Opus: —", "BIMAS · H1 · Opus: —"])
        self.assertNotIn("yönlü", "\n".join(lines))

    def test_same_direction_rules_of_one_stock_share_one_line(self):
        document = _doc({"AKBNK": _stock("H4", "H2"), "BIMAS": _stock("H3", "H1")})
        self.assertEqual(_lines(document)[1:], [
            "AKBNK ↑ al yönlü · H2+H4 · Opus: —",
            "BIMAS ↓ sat yönlü · H1+H3 · Opus: —",
        ])

    def test_different_directions_of_one_stock_get_separate_lines_up_first(self):
        document = _doc({"AKBNK": _stock("H1", "H2", "H3", "H4")})
        self.assertEqual(_lines(document)[1:], [
            "AKBNK ↑ al yönlü · H2+H4 · Opus: —",
            "AKBNK ↓ sat yönlü · H1+H3 · Opus: —",
        ])

    def test_only_condition_met_observations_make_lines(self):
        document = _doc({"AKBNK": _block([_hit("H2"), _obs("H1", condition_met=False),
                                          _obs("H3", condition_met=None)], composition=CLEAN_WINDOW),
                         "BIMAS": _block([_obs("H1", condition_met=False)], composition=CLEAN_WINDOW)})
        self.assertEqual(_lines(document)[1:], ["AKBNK ↑ al yönlü · H2 · Opus: —"])

    def test_multi_day_scan_names_the_day_per_line_and_ranges_the_header(self):
        document = _doc({"AKBNK": {**_stock("H2"), "scans": [
            _block([_hit("H2")], as_of="2026-10-01")["scans"][0],
            _block([_hit("H1")], as_of="2026-10-02")["scans"][0]]}})
        lines = _lines(document)
        self.assertEqual(lines[0], f"📊 01.10–02.10 {HEADER_SUFFIX_TR}")
        self.assertEqual(lines[1:], ["AKBNK (01.10) ↑ al yönlü · H2 · Opus: —",
                                     "AKBNK (02.10) ↓ sat yönlü · H1 · Opus: —"])
        assessments = {("AKBNK", "H2", "2026-10-01"): "destekliyor"}
        self.assertEqual(_lines(document, assessments=assessments)[1:], [
            "AKBNK (01.10) ↑ al yönlü · H2 · Opus: destekliyor",
            "AKBNK (02.10) ↓ sat yönlü · H1 · Opus: —"])


class OpusWordTests(unittest.TestCase):
    DOC = _doc({"AKBNK": _stock("H2", "H4"), "BIMAS": _stock("H1")})

    def test_the_accepted_notes_word_is_shown_for_exactly_that_stock_rule_and_day(self):
        assessments = {("AKBNK", "H2", AS_OF): "destekliyor", ("AKBNK", "H4", AS_OF): "destekliyor",
                       ("BIMAS", "H1", AS_OF): "zayıflatıyor"}
        self.assertEqual(_lines(self.DOC, assessments=assessments)[1:], [
            "AKBNK ↑ al yönlü · H2+H4 · Opus: destekliyor",
            "BIMAS ↓ sat yönlü · H1 · Opus: zayıflatıyor"])
        # another symbol, rule or day never lends its word
        for key in (("BIMAS", "H2", AS_OF), ("AKBNK", "H1", AS_OF), ("AKBNK", "H2", "2026-10-01")):
            with self.subTest(key=key):
                self.assertEqual(_lines(self.DOC, assessments={key: "destekliyor"})[2],
                                 "BIMAS ↓ sat yönlü · H1 · Opus: —")
                self.assertEqual(_lines(self.DOC, assessments={key: "destekliyor"})[1],
                                 "AKBNK ↑ al yönlü · H2+H4 · Opus: —")

    def test_every_allowed_word_and_nothing_else(self):
        for word in ASSESSMENT_WORDS:
            with self.subTest(word=word):
                lines = _lines(self.DOC, assessments={("BIMAS", "H1", AS_OF): word})
                self.assertEqual(lines[2], f"BIMAS ↓ sat yönlü · H1 · Opus: {word}")
        for bad in ("al", "sat", "güçlü", "yükselecek", "DESTEKLIYOR", "", None, 5):
            with self.subTest(bad=bad):
                lines = _lines(self.DOC, assessments={("BIMAS", "H1", AS_OF): bad})
                self.assertEqual(lines[2], "BIMAS ↓ sat yönlü · H1 · Opus: —")

    def test_merged_rules_with_different_words_are_shown_per_rule(self):
        for assessments, expected in (
            ({("AKBNK", "H2", AS_OF): "destekliyor", ("AKBNK", "H4", AS_OF): "belirsiz"},
             "Opus: H2 destekliyor, H4 belirsiz"),
            ({("AKBNK", "H2", AS_OF): "zayıflatıyor"}, "Opus: H2 zayıflatıyor, H4 —"),
            ({("AKBNK", "H4", AS_OF): "belirsiz"}, "Opus: H2 —, H4 belirsiz"),
            ({("AKBNK", "H2", AS_OF): "belirsiz", ("AKBNK", "H4", AS_OF): "belirsiz"}, "Opus: belirsiz"),
            ({}, "Opus: —"),
        ):
            with self.subTest(assessments=assessments):
                self.assertEqual(_lines(self.DOC, assessments=assessments)[1],
                                 f"AKBNK ↑ al yönlü · H2+H4 · {expected}")

    def test_the_note_is_never_needed_for_the_message(self):
        for assessments in (None, {}):
            self.assertEqual(_render(self.DOC, assessments=assessments),
                             _render(self.DOC, assessments={}))


class NoSignalAndDataLineTests(unittest.TestCase):
    def test_no_observation_is_one_line(self):
        document = _doc({"AKBNK": _block([_obs("H1", condition_met=False)]),
                         "BIMAS": _block([_obs("H2", condition_met=None)])})
        self.assertEqual(_render(document), ["📊 02.10 sinyal yok"])
        # also when the window is dirty (single line, no data line) and without any scan
        dirty = _doc({"AKBNK": _block([_obs("H1", condition_met=False)],
                                      composition=_composition_block(window=FREEZE_WINDOW, in_window=(1, 2, 3)))})
        self.assertEqual(_render(dirty), ["📊 02.10 sinyal yok"])
        self.assertEqual(_render(_doc({}), as_of="2026-10-03"), ["📊 03.10 sinyal yok"])

    def test_data_line_only_when_the_window_has_unsettled_gap_or_revised_days(self):
        def lines_for(in_window):
            composition = _composition_block(window=FREEZE_WINDOW, in_window=in_window)
            return _lines(_doc({"AKBNK": _stock("H2", composition=composition)}))
        self.assertEqual(len(lines_for((0, 0, 0))), 2)  # header + the stock only
        self.assertEqual(lines_for((0, 2, 0))[-1], "veri: 2 gün kesinleşmemiş")
        self.assertEqual(lines_for((1, 0, 0))[-1], "veri: 1 gün boşluk")
        self.assertEqual(lines_for((0, 0, 3))[-1], "veri: 3 gün revize")
        self.assertEqual(lines_for((1, 2, 3))[-1], "veri: 2 gün kesinleşmemiş, 1 gün boşluk, 3 gün revize")
        self.assertEqual(len(lines_for((1, 2, 3))), 3)  # one short line, whatever the counts

    def test_data_line_takes_the_largest_count_over_stocks_and_never_adds(self):
        def block(in_window):
            return _stock("H2", composition=_composition_block(window=FREEZE_WINDOW, in_window=in_window))
        document = _doc({"AKBNK": block((0, 2, 0)), "BIMAS": block((0, 2, 1)), "CCOLA": block((0, 1, 0))})
        self.assertEqual(_lines(document)[-1], "veri: 2 gün kesinleşmemiş, 1 gün revize")

    def test_stock_without_window_information_is_marked_unknown(self):
        for composition in (None, _composition_block(window=None)):
            with self.subTest(composition=composition):
                document = _doc({"AKBNK": _stock("H2", composition=composition)})
                self.assertEqual(_lines(document)[-1], "veri: durum bilgisi yok")


class SafetyTests(unittest.TestCase):
    def _full_document(self) -> dict:
        composition = _composition_block(window=FREEZE_WINDOW, in_window=(1, 2, 3))
        return _doc({"AKBNK": _stock("H1", "H2", "H3", "H4", composition=composition),
                     "BIMAS": _stock("H2", composition=composition)})

    def test_no_new_numbers_and_no_forbidden_wording(self):
        assessments = {("AKBNK", "H2", AS_OF): "destekliyor", ("AKBNK", "H4", AS_OF): "belirsiz"}
        text = "\n".join(_render(self._full_document(), assessments=assessments))
        self.assertEqual(find_forbidden_words(text, ALLOWED), [])
        for word in ("getiri", "puan", "hedef", "stop", "pozisyon", "Ne demek", "Kural", "Vade",
                     "%", "Durum:", "kural sürümü", "Kural sürümü", "⚠"):
            self.assertNotIn(word, text)
        # only the date, rule ids and the data line's counts carry digits
        remaining = re.sub(r"\d\d\.\d\d|H\d|veri: .*", "", text)
        self.assertNotRegex(remaining, r"\d")

    def test_text_is_deterministic_and_pure(self):
        document = self._full_document()
        before = json.dumps(document, sort_keys=True)
        first, second = _render(document), _render(document)
        self.assertEqual(first, second)
        self.assertEqual(before, json.dumps(document, sort_keys=True))

    def test_twenty_stocks_worst_case_is_one_message_below_the_telegram_limit(self):
        # Worst case: 20 stocks with the longest symbol name the notifier accepts (16),
        # all four rules in both directions, merged rules with differing words, a dirty window.
        symbols = [f"SYMBOL{index:02d}ABCDEFGH"[:16] for index in range(20)]
        composition = _composition_block(window=FREEZE_WINDOW, in_window=(10, 20, 30))
        document = _doc({symbol: _stock("H1", "H2", "H3", "H4", composition=composition) for symbol in symbols})
        words = {"H1": "destekliyor", "H2": "zayıflatıyor", "H3": "belirsiz", "H4": "destekliyor"}
        assessments = {(symbol, rule, AS_OF): word for symbol in symbols for rule, word in words.items()}
        messages = _render(document, assessments=assessments)
        self.assertEqual(len(messages), 1)
        self.assertLess(len(messages[0]), 4096)
        self.assertEqual(len(messages[0].split("\n")), 1 + 40 + 1)
        # ... and with the multi-day prefix on every line it still fits
        multi = {symbol: {**_stock("H1", "H2", "H3", "H4"), "scans": [
            _block([_hit(rule) for rule in ("H1", "H2", "H3", "H4")], as_of="2026-10-01")["scans"][0],
            _block([_hit(rule) for rule in ("H1", "H2", "H3", "H4")], as_of="2026-10-02")["scans"][0]]}
            for symbol in symbols}
        multi_messages = _render(_doc(multi), assessments=assessments)
        self.assertTrue(all(len(m) <= 4096 for m in multi_messages))

    def test_overlong_input_continues_in_further_messages_that_repeat_the_header(self):
        document = _doc({f"SYM{index:02d}": _stock("H2") for index in range(30)})
        messages = _render(document, limit=300)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), 300)
            self.assertTrue(message.startswith(HEADER))
        rows = [line for message in messages for line in message.split("\n")[1:]]
        self.assertEqual(rows, [f"SYM{index:02d} ↑ al yönlü · H2 · Opus: —" for index in range(30)])


# --- scripts/notify_observations.py, layout v3 ------------------------------------


class ScriptV3Tests(GuardEnvMixin, unittest.TestCase):
    ENV = {"FINTR_TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "FINTR_TELEGRAM_CHAT_ID": FAKE_CHAT_ID}

    def _document(self) -> dict:
        document = _doc({DEVELOPMENT[0]: _stock("H2"), DEVELOPMENT[1]: _stock("H1")})
        document["rules_file_sha256"] = "9" * 64
        return document

    def _prepare(self, document: dict | None = None) -> Path:
        self.make_env()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = root / "scanner" / AS_OF
        target.mkdir(parents=True)
        (target / "observations.json").write_text(
            json.dumps(document or self._document(), ensure_ascii=False), encoding="utf-8")
        return root

    def _run(self, root: Path, *extra: str, send_effects=None):
        argv = ["--as-of", AS_OF, "--scanner-dir", str(root / "scanner"), "--out-dir", str(root / "out"),
                "--rule-texts", str(REAL_RULE_TEXTS_FILE), *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        mock_send = MagicMock(side_effect=send_effects if send_effects is not None else [SendResult(11)] * 4)
        with patch.dict(os.environ, self.ENV), patch.object(notify, "send_message", mock_send), \
                patch.object(notify, "time", MagicMock()), redirect_stdout(stdout), redirect_stderr(stderr):
            code = notify.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), mock_send

    def _receipts(self, root: Path) -> list[Path]:
        directory = root / "out" / AS_OF
        return sorted(directory.iterdir()) if directory.exists() else []

    def test_v3_is_the_default_and_dry_run_prints_the_single_message_without_network(self):
        root = self._prepare()
        with patch("fintr.notify.telegram.urllib.request.urlopen",
                   side_effect=AssertionError("dry-run must never touch the network")):
            code, out, err, send = self._run(root)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, (
            "--- mesaj 1/1 (dry-run; ağ kullanılmadı) ---\n"
            f"{HEADER}\n{DEVELOPMENT[0]} ↑ al yönlü · H2 · Opus: —\n{DEVELOPMENT[1]} ↓ sat yönlü · H1 · Opus: —\n"))
        send.assert_not_called()
        self.assertEqual(self._receipts(root), [])

    def test_no_direction_label_and_unverifiable_rules_file_leave_the_direction_out(self):
        root = self._prepare()
        _code, out, _err, _send = self._run(root, "--no-direction-label")
        self.assertIn(f"{DEVELOPMENT[0]} · H2 · Opus: —", out)
        self.assertNotIn("yönlü", out)
        bad = json.loads((ROOT / "configs" / "scanner" / "rules_v1.json").read_text(encoding="utf-8"))
        bad["rules"][0]["raw_sha256"] = "0" * 64
        bad_path = root / "rules.json"
        bad_path.write_text(json.dumps(bad), encoding="utf-8")
        code, out, err, _send = self._run(root, "--rules-file", str(bad_path))
        self.assertEqual(code, 0)
        self.assertIn(f"{DEVELOPMENT[0]} · H2 · Opus: —", out)
        self.assertNotIn("yönlü", out)
        self.assertIn("yön etiketi kaynağı doğrulanamadı", err)

    def test_empty_day_is_one_line_and_skip_empty_sends_nothing(self):
        document = _doc({DEVELOPMENT[0]: _block([_obs("H1", condition_met=False)])})
        root = self._prepare(document)
        _code, out, _err, _send = self._run(root)
        self.assertEqual(out, "--- mesaj 1/1 (dry-run; ağ kullanılmadı) ---\n📊 02.10 sinyal yok\n")
        code, out, _err, send = self._run(root, "--skip-empty", "--send")
        self.assertEqual((code, out), (0, ""))
        send.assert_not_called()

    def test_send_writes_a_v2_family_receipt_and_a_same_day_rerun_sends_nothing_again(self):
        root = self._prepare()
        code, _out, err, send = self._run(root, "--send")
        self.assertEqual(code, 0, err)
        self.assertEqual(send.call_count, 1)
        sent = send.call_args.args[2]
        self.assertTrue(sent.startswith(HEADER))
        files = self._receipts(root)
        self.assertEqual([f.name for f in files], ["receipt.json"])
        receipt = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "fintr_notify_receipt_v2")
        self.assertEqual((receipt["layout"], receipt["message_count"], receipt["complete"]), ("v3", 1, True))
        self.assertIsNone(receipt["rule_explanations"])
        self.assertNotIn("note_assessments", receipt)
        self.assertNotIn(FAKE_TOKEN, files[0].read_text(encoding="utf-8"))
        # same day again: refused with the "receipt zaten var" mark the pipeline reads; nothing sent
        code, _out, err, send = self._run(root, "--send")
        self.assertEqual(code, 1)
        self.assertIn("receipt zaten var", err)
        send.assert_not_called()
        self.assertEqual([f.name for f in self._receipts(root)], ["receipt.json"])
        # --resend is the only way to send again
        code, _out, _err, send = self._run(root, "--send", "--resend")
        self.assertEqual((code, send.call_count), (0, 1))
        self.assertEqual(len(self._receipts(root)), 2)

    def test_failed_send_writes_no_receipt_and_the_rerun_sends(self):
        root = self._prepare()
        from fintr.notify.telegram import TelegramSendError
        code, _out, err, _send = self._run(root, "--send", send_effects=[TelegramSendError("HTTP 500")])
        self.assertEqual(code, 1)
        self.assertEqual(self._receipts(root), [])
        self.assertNotIn(FAKE_TOKEN, err)
        code, _out, _err, send = self._run(root, "--send")
        self.assertEqual((code, send.call_count), (0, 1))

    def test_note_assessments_dir_without_notes_prints_dashes_and_is_recorded(self):
        root = self._prepare()
        notes = root / "no_notes_here"
        code, out, err, send = self._run(root, "--send", "--note-assessments-dir", str(notes))
        self.assertEqual(code, 0, err)
        self.assertIn(f"{DEVELOPMENT[0]} ↑ al yönlü · H2 · Opus: —", send.call_args.args[2])
        receipt = json.loads(self._receipts(root)[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["note_assessments"], {"dir": str(notes), "shown": 0})

    def test_note_assessments_dir_needs_layout_v3(self):
        root = self._prepare()
        for layout in ("v1", "v2"):
            with self.subTest(layout=layout), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self._run(root, "--layout", layout, "--note-assessments-dir", str(root / "x"))
            self.assertEqual(caught.exception.code, 2)

    def test_layout_choice_and_default(self):
        parser = notify._parser()
        self.assertEqual(parser.parse_args(["--as-of", AS_OF]).layout, "v3")
        for layout in ("v1", "v2", "v3"):
            self.assertEqual(parser.parse_args(["--as-of", AS_OF, "--layout", layout]).layout, layout)


if __name__ == "__main__":
    unittest.main()
