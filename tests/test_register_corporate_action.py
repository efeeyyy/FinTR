"""Offline tests for ``scripts/register_corporate_action.py``.

All symbols, dates, multipliers and URLs are synthetic fixtures. This CLI
never reaches kap.org.tr or any other network source; ``--source-file`` (when
given) is only hashed locally.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fintr.data.corporate_actions import load_corporate_action_registry


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


register = _load_script("register_corporate_action")


_BASE_ARGS = [
    "--symbol", "TEST",
    "--action-id", "TEST-2026-bonus-1",
    "--type", "bonus_issue",
    "--lot-multiplier", "6",
    "--ex-date", "2026-05-04",
    "--custody-effective-date", "2026-05-06",
    "--source-url", "https://example.test/kap-notice",
    "--source-published-date", "2026-05-01",
]


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = register.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class RegisterCorporateActionTests(unittest.TestCase):
    def test_appends_and_prints_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, out, err = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 0, err)
            printed = json.loads(out)
            self.assertEqual(printed["action_id"], "TEST-2026-bonus-1")
            self.assertEqual(printed["lot_multiplier"], "6")
            self.assertIsNone(printed["source_file_sha256"])

            registry = load_corporate_action_registry(root / "TEST.json")
            self.assertEqual(registry.symbol, "TEST")
            self.assertEqual(len(registry.actions), 1)

    def test_duplicate_action_id_is_refused_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _, _ = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 0)
            before = (root / "TEST.json").read_text(encoding="utf-8")

            code, _, err = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("duplicate", err.lower())
            after = (root / "TEST.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_overlapping_second_action_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _, _ = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 0)

            overlapping = [
                "--symbol", "TEST", "--action-id", "TEST-2026-bonus-2",
                "--type", "split", "--lot-multiplier", "2",
                "--ex-date", "2026-05-05", "--custody-effective-date", "2026-05-07",
                "--source-url", "https://example.test/kap-notice-2",
                "--source-published-date", "2026-05-05",
                "--registry-root", str(root),
            ]
            code, _, err = _run(overlapping)
            self.assertEqual(code, 1)
            self.assertIn("overlap", err.lower())

    def test_non_overlapping_second_action_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _, _ = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 0)

            second = [
                "--symbol", "TEST", "--action-id", "TEST-2026-bonus-2",
                "--type", "split", "--lot-multiplier", "2",
                "--ex-date", "2026-08-04", "--custody-effective-date", "2026-08-06",
                "--source-url", "https://example.test/kap-notice-2",
                "--source-published-date", "2026-08-01",
                "--registry-root", str(root),
            ]
            code, out, err = _run(second)
            self.assertEqual(code, 0, err)
            registry = load_corporate_action_registry(root / "TEST.json")
            self.assertEqual([a.action_id for a in registry.actions],
                              ["TEST-2026-bonus-1", "TEST-2026-bonus-2"])

    def test_source_file_hash_is_computed_locally_and_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "kap-notice.txt"
            source_file.write_bytes(b"synthetic notice contents")
            expected_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()

            code, out, err = _run(
                _BASE_ARGS + ["--registry-root", str(root), "--source-file", str(source_file)],
            )
            self.assertEqual(code, 0, err)
            printed = json.loads(out)
            self.assertEqual(printed["source_file_sha256"], expected_hash)

    def test_invalid_multiplier_is_rejected_by_argparse(self):
        args = list(_BASE_ARGS)
        args[args.index("6")] = "0"
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                register.main(args + ["--registry-root", "does-not-matter"])

    def test_symbol_mismatch_against_existing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Pre-seed a file at TEST.json holding a different symbol.
            (root / "TEST.json").write_text(json.dumps({
                "schema": "fintr_corporate_actions_v1", "symbol": "OTHER", "actions": [],
            }), encoding="utf-8")
            code, _, err = _run(_BASE_ARGS + ["--registry-root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("OTHER", err)


if __name__ == "__main__":
    unittest.main()
