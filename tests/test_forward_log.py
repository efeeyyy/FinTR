"""Offline tests for ``fintr.forward_log`` and ``scripts/forward_log.py``.

Synthetic files in temp dirs only; no real data, no network, no git needed
(``code_commit`` is passed explicitly or comes back as ``None``).
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from fintr.forward_log import core

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("forward_log_script", ROOT / "scripts" / "forward_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _now(minute: int) -> datetime:
    return datetime(2026, 10, 10, 12, minute, 0, tzinfo=timezone.utc)


class ForwardLogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "interim"
        self.manifest = self.tmp / "forward_log" / "manifest.jsonl"

    def put(self, rel: str, content: bytes | str) -> Path:
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def make_day(self, as_of: str, tag: str = "a") -> None:
        self.put(f"scanner_daily/{as_of}/observations.json", f'{{"x":"{tag}1"}}')
        self.put(f"notify/{as_of}/receipt.json", f'{{"r":"{tag}2"}}')
        self.put(f"notify/{as_of}/receipt-20261003T202939Z.json", f'{{"r":"{tag}3"}}')
        self.put(f"llm_notes/{as_of}/AAA/attempt-001/note.json", f'{{"n":"{tag}4"}}')

    def append(self, as_of: str, minute: int = 0, **kwargs):
        return core.append_as_of(self.manifest, self.data, as_of, now=_now(minute),
                                 code_commit="c0ffee", **kwargs)

    def lines(self) -> list[str]:
        return self.manifest.read_bytes().decode("utf-8").split("\n")[:-1]

    def rewrite(self, lines: list[str]) -> None:
        self.manifest.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def verify_problems(self) -> list[str]:
        return core.load_manifest(self.manifest)[1]


class AppendTests(ForwardLogTestCase):
    def test_first_entry_shape_and_chain(self) -> None:
        self.make_day("2026-10-02")
        self.make_day("2026-10-09", "b")
        first = self.append("2026-10-02", 0).entry
        second = self.append("2026-10-09", 1).entry
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(first["prev_hash"], core.GENESIS_PREV_HASH)
        self.assertEqual(second["prev_hash"], first["entry_hash"])
        self.assertEqual(first["entry_hash"], core.compute_entry_hash(first))
        self.assertEqual(first["schema"], core.SCHEMA)
        self.assertEqual(first["appended_at_utc"], "2026-10-10T12:00:00Z")
        self.assertEqual(first["code_commit"], "c0ffee")
        self.assertFalse(first["revision"])
        self.assertFalse(first["backfilled"])
        self.assertEqual([item["path"] for item in first["files"]], [
            "llm_notes/2026-10-02/AAA/attempt-001/note.json",
            "notify/2026-10-02/receipt-20261003T202939Z.json",
            "notify/2026-10-02/receipt.json",
            "scanner_daily/2026-10-02/observations.json",
        ])
        self.assertEqual(self.verify_problems(), [])

    def test_hash_is_sha256_of_bytes_and_size(self) -> None:
        self.put("scanner_daily/2026-10-02/observations.json", b"abc")
        entry = self.append("2026-10-02").entry
        self.assertEqual(entry["files"], [{
            "path": "scanner_daily/2026-10-02/observations.json",
            "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            "size": 3,
        }])

    def test_manifest_lines_are_lf_terminated_json(self) -> None:
        self.make_day("2026-10-02")
        self.append("2026-10-02")
        raw = self.manifest.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        self.assertEqual(json.loads(raw)["as_of"], "2026-10-02")

    def test_identical_rerun_is_noop(self) -> None:
        self.make_day("2026-10-02")
        self.append("2026-10-02")
        before = self.manifest.read_bytes()
        result = self.append("2026-10-02", 5)
        self.assertEqual(result.status, "noop")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_changed_files_are_refused_without_revision(self) -> None:
        self.make_day("2026-10-02")
        self.append("2026-10-02")
        before = self.manifest.read_bytes()
        self.put("notify/2026-10-02/receipt.json", '{"r":"changed"}')
        with self.assertRaises(core.ConflictError):
            self.append("2026-10-02", 5)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_added_file_is_also_a_change(self) -> None:
        self.make_day("2026-10-02")
        self.append("2026-10-02")
        self.put("notify/2026-10-02/note_receipt.json", "{}")
        with self.assertRaises(core.ConflictError):
            self.append("2026-10-02", 5)

    def test_revision_appends_new_entry_and_keeps_old_lines(self) -> None:
        self.make_day("2026-10-02")
        first = self.append("2026-10-02").entry
        old_line = self.lines()[0]
        self.put("notify/2026-10-02/receipt.json", '{"r":"changed"}')
        revised = self.append("2026-10-02", 5, revision=True).entry
        self.assertTrue(revised["revision"])
        self.assertEqual(revised["supersedes_seq"], first["seq"])
        self.assertEqual(revised["seq"], 2)
        self.assertEqual(self.lines()[0], old_line)
        self.assertEqual(self.verify_problems(), [])
        # now identical to the revision: no-op even with --revision
        self.assertEqual(self.append("2026-10-02", 6, revision=True).status, "noop")
        self.assertEqual(len(self.lines()), 2)

    def test_revision_without_prior_entry_is_rejected(self) -> None:
        self.make_day("2026-10-02")
        with self.assertRaises(core.ForwardLogError):
            self.append("2026-10-02", revision=True)
        self.assertFalse(self.manifest.exists())

    def test_no_artifacts_and_bad_date(self) -> None:
        with self.assertRaises(core.NoArtifactsError):
            self.append("2026-10-02")
        with self.assertRaises(core.ForwardLogError):
            self.append("2026-13-40")
        with self.assertRaises(core.ForwardLogError):
            self.append("../../etc")

    def test_other_days_files_are_not_included(self) -> None:
        self.make_day("2026-10-02")
        self.make_day("2026-10-09", "b")
        entry = self.append("2026-10-02").entry
        self.assertTrue(all("2026-10-02" in item["path"] for item in entry["files"]))

    def test_append_refuses_on_broken_manifest(self) -> None:
        self.make_day("2026-10-02")
        self.make_day("2026-10-09", "b")
        self.append("2026-10-02")
        self.manifest.write_bytes(self.manifest.read_bytes().replace(b"c0ffee", b"deadbe"))
        before = self.manifest.read_bytes()
        with self.assertRaises(core.ForwardLogError):
            self.append("2026-10-09", 1)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_backfilled_flag_is_recorded(self) -> None:
        self.make_day("2026-10-02")
        entry = self.append("2026-10-02", backfilled=True).entry
        self.assertTrue(entry["backfilled"])
        self.assertEqual(self.verify_problems(), [])


class VerifyTests(ForwardLogTestCase):
    def setUp(self) -> None:
        super().setUp()
        for index, as_of in enumerate(("2026-09-30", "2026-10-02", "2026-10-09")):
            self.make_day(as_of, str(index))
            self.append(as_of, index)

    def test_intact_chain(self) -> None:
        entries, problems = core.load_manifest(self.manifest)
        self.assertEqual((len(entries), problems), (3, []))
        self.assertEqual(core.verify_files(entries, self.data), [])

    def test_edited_line_content_is_detected(self) -> None:
        lines = self.lines()
        lines[1] = lines[1].replace("c0ffee", "deadbe")
        self.rewrite(lines)
        problems = self.verify_problems()
        self.assertTrue(any("entry 2" in item and "entry_hash" in item for item in problems), problems)

    def test_edited_line_with_recomputed_hash_breaks_next_link(self) -> None:
        lines = self.lines()
        entry = json.loads(lines[1])
        entry["code_commit"] = "deadbe"
        entry["entry_hash"] = core.compute_entry_hash(entry)
        lines[1] = core.canonical_json(entry)
        self.rewrite(lines)
        problems = self.verify_problems()
        self.assertTrue(any("entry 3" in item and "prev_hash" in item for item in problems), problems)

    def test_deleted_middle_line_is_detected(self) -> None:
        lines = self.lines()
        self.rewrite([lines[0], lines[2]])
        problems = self.verify_problems()
        self.assertTrue(any("seq" in item for item in problems), problems)
        self.assertTrue(any("prev_hash" in item for item in problems), problems)

    def test_reordered_lines_are_detected(self) -> None:
        lines = self.lines()
        self.rewrite([lines[0], lines[2], lines[1]])
        problems = self.verify_problems()
        self.assertTrue(any("seq" in item for item in problems), problems)
        self.assertTrue(any("prev_hash" in item for item in problems), problems)

    def test_garbage_and_partial_lines_are_detected(self) -> None:
        self.manifest.write_bytes(self.manifest.read_bytes() + b'{"seq": 4')
        problems = self.verify_problems()
        self.assertTrue(any("newline" in item for item in problems), problems)
        self.assertTrue(any("not valid JSON" in item for item in problems), problems)

    def test_second_unrevised_entry_for_same_as_of_is_a_break(self) -> None:
        lines = self.lines()
        entry = json.loads(lines[2])
        extra = dict(entry, seq=4, prev_hash=entry["entry_hash"], appended_at_utc="2026-10-10T12:30:00Z")
        extra["entry_hash"] = core.compute_entry_hash(extra)
        self.rewrite(lines + [core.canonical_json(extra)])
        problems = self.verify_problems()
        self.assertTrue(any("not a revision" in item for item in problems), problems)

    def test_clock_going_backwards_is_a_break(self) -> None:
        entries, _ = core.load_manifest(self.manifest)
        first = json.loads(self.lines()[0])
        first["appended_at_utc"] = "2026-10-11T00:00:00Z"
        first["entry_hash"] = core.compute_entry_hash(first)
        # relink the rest so only the clock rule can fire
        rebuilt = [first]
        for old in entries[1:]:
            new = dict(old, prev_hash=rebuilt[-1]["entry_hash"])
            new["entry_hash"] = core.compute_entry_hash(new)
            rebuilt.append(new)
        self.rewrite([core.canonical_json(item) for item in rebuilt])
        problems = self.verify_problems()
        self.assertTrue(any("earlier than the previous" in item for item in problems), problems)

    def test_tampered_file_on_disk_is_detected(self) -> None:
        self.put("scanner_daily/2026-10-02/observations.json", '{"x":"tampered"}')
        entries, problems = core.load_manifest(self.manifest)
        self.assertEqual(problems, [])
        file_problems = core.verify_files(entries, self.data)
        self.assertEqual(len(file_problems), 1)
        self.assertIn("changed", file_problems[0])

    def test_missing_and_extra_files_on_disk_are_detected(self) -> None:
        (self.data / "notify/2026-10-09/receipt.json").unlink()
        self.put("llm_notes/2026-10-09/ZZZ/attempt-001/note.json", "{}")
        entries, _ = core.load_manifest(self.manifest)
        file_problems = core.verify_files(entries, self.data)
        self.assertTrue(any("missing" in item for item in file_problems), file_problems)
        self.assertTrue(any("not in the log" in item for item in file_problems), file_problems)

    def test_only_latest_revision_is_checked_against_disk(self) -> None:
        self.put("notify/2026-10-09/receipt.json", '{"r":"revised"}')
        self.append("2026-10-09", 10, revision=True)
        entries, problems = core.load_manifest(self.manifest)
        self.assertEqual(problems, [])
        self.assertEqual(core.verify_files(entries, self.data), [])


class BackfillTests(ForwardLogTestCase):
    def test_list_as_of_dates_in_range_sorted(self) -> None:
        for as_of in ("2026-10-09", "2026-09-30", "2026-10-02"):
            self.make_day(as_of)
        (self.data / "scanner_daily" / "not-a-date").mkdir()
        self.assertEqual(core.list_as_of_dates(self.data, "2026-10-01", "2026-10-31"),
                         ["2026-10-02", "2026-10-09"])

    def test_backfill_marks_entries_and_is_idempotent(self) -> None:
        for as_of in ("2026-10-09", "2026-09-30", "2026-10-02"):
            self.make_day(as_of)
        argv = ["backfill", "--from", "2026-09-30", "--to", "2026-10-09",
                "--data-root", str(self.data), "--manifest", str(self.manifest)]
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(argv), 0)
        entries, problems = core.load_manifest(self.manifest)
        self.assertEqual(problems, [])
        self.assertEqual([e["as_of"] for e in entries], ["2026-09-30", "2026-10-02", "2026-10-09"])
        self.assertTrue(all(e["backfilled"] for e in entries))
        self.assertIn("prove nothing", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(argv), 0)
        self.assertEqual(len(core.load_manifest(self.manifest)[0]), 3)

    def test_backfill_conflict_exits_nonzero_without_appending_that_day(self) -> None:
        self.make_day("2026-10-02")
        self.append("2026-10-02")
        self.put("notify/2026-10-02/receipt.json", "changed")
        self.make_day("2026-10-09")
        argv = ["backfill", "--from", "2026-10-01", "--to", "2026-10-31",
                "--data-root", str(self.data), "--manifest", str(self.manifest)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(argv), 1)
        entries, _ = core.load_manifest(self.manifest)
        self.assertEqual([e["as_of"] for e in entries], ["2026-10-02", "2026-10-09"])


class ScriptTests(ForwardLogTestCase):
    def run_script(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = script.main([*argv, "--data-root", str(self.data), "--manifest", str(self.manifest)])
        return code, out.getvalue(), err.getvalue()

    def test_append_status_lines_and_exit_codes(self) -> None:
        self.make_day("2026-10-02")
        code, out, _ = self.run_script("append", "--as-of", "2026-10-02")
        self.assertEqual(code, 0)
        self.assertIn("FORWARD_LOG_STATUS: appended", out)
        code, out, _ = self.run_script("append", "--as-of", "2026-10-02")
        self.assertEqual((code, "FORWARD_LOG_STATUS: noop" in out), (0, True))
        self.put("notify/2026-10-02/receipt.json", "changed")
        code, out, err = self.run_script("append", "--as-of", "2026-10-02")
        self.assertEqual(code, 1)
        self.assertIn("FORWARD_LOG_STATUS: refused", out)
        self.assertIn("--revision", err)
        code, _, _ = self.run_script("append", "--as-of", "2026-10-02", "--revision")
        self.assertEqual(code, 0)
        code, _, _ = self.run_script("append", "--as-of", "2026-10-03")
        self.assertEqual(code, 2)

    def test_verify_exit_codes(self) -> None:
        self.make_day("2026-10-02")
        self.run_script("append", "--as-of", "2026-10-02")
        code, out, _ = self.run_script("verify", "--check-files")
        self.assertEqual(code, 0)
        self.assertIn("entries=1", out)
        self.assertIn("files=ok", out)
        self.put("scanner_daily/2026-10-02/observations.json", "tampered")
        self.assertEqual(self.run_script("verify")[0], 0)  # chain only
        code, _, err = self.run_script("verify", "--check-files")
        self.assertEqual(code, 1)
        self.assertIn("changed", err)
        self.manifest.write_bytes(self.manifest.read_bytes() + b"garbage\n")
        self.assertEqual(self.run_script("verify")[0], 1)

    def test_verify_of_missing_manifest_is_empty_ok(self) -> None:
        code, out, _ = self.run_script("verify")
        self.assertEqual(code, 0)
        self.assertIn("entries=0", out)


if __name__ == "__main__":
    unittest.main()
