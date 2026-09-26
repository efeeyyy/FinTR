"""Offline source-fact construction and strict model-selection tests."""

from datetime import datetime, timezone
import unittest

from fintr.inference.live_context import LiveAnalysisContext, build_live_analysis_context
from fintr.inference.live_facts import build_live_scope_facts, select_live_scope_facts
from test_live_analysis_context import LIMITS, _evidence


class LiveScopeFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_facts_are_code_authored_and_provenance_specific(self):
        context = build_live_analysis_context(
            await _evidence(), generated_at=datetime.now(timezone.utc), limits=LIMITS,
        )
        facts = build_live_scope_facts(context)
        by_id = {fact.fact_id: fact for fact in facts}
        self.assertEqual(set(by_id), set("ABCDEF"))
        self.assertIn("yerel gönderilmiş istek", by_id["A"].text)
        self.assertIn("sunucu tarihi", by_id["B"].text)
        self.assertIn("sunucu yanıtından", by_id["C"].text)
        self.assertIn("sınırlandırılmıştır", by_id["E"].text)
        self.assertNotIn("Ak Fiyat", " ".join(fact.text for fact in facts))

    async def test_akd_request_scope_fact_remains_mandatory_with_zero_rows(self):
        context = build_live_analysis_context(
            await _evidence(), generated_at=datetime.now(timezone.utc), limits=LIMITS,
        )
        data = context.to_dict()
        data["akd"]["total_rows"] = 0
        data["akd"]["included_rows"] = 0
        data["akd"]["rows"] = []
        empty_akd_context = LiveAnalysisContext(data)

        facts = build_live_scope_facts(empty_akd_context)
        self.assertIn("A", {fact.fact_id for fact in facts})
        with self.assertRaisesRegex(ValueError, "mandatory_fact_missing"):
            select_live_scope_facts(
                "B,D,E", facts, generated_tokens=7, max_new_tokens=64,
            )

    async def test_model_can_select_only_known_ids_not_supply_prose(self):
        context = build_live_analysis_context(
            await _evidence(), generated_at=datetime.now(timezone.utc), limits=LIMITS,
        )
        facts = build_live_scope_facts(context)
        selection = select_live_scope_facts(
            "A, B, E", facts, generated_tokens=7, max_new_tokens=64,
        )
        self.assertEqual(selection.fact_ids, ("A", "B", "E"))
        self.assertEqual(
            selection.rendered_lines[0],
            "[A] " + facts[0].text + " [kaynak alanları: akd.source_event, akd.local_sent_request]",
        )
        for bad, reason in (
            ("A,B,Z", "unknown_fact_id"),
            ("A,A,B", "duplicate_fact_id"),
            ("A,B,C. AKD (Aktif Kullanım Değeri)", "invalid_fact_id_format"),
            ("A,B,C\n2026-09-23", "invalid_fact_id_format"),
            ("A,B,C", "mandatory_fact_missing"),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, reason):
                select_live_scope_facts(bad, facts, generated_tokens=20, max_new_tokens=64)
        with self.assertRaisesRegex(ValueError, "generation_token_cap_reached"):
            select_live_scope_facts("A,B,C", facts, generated_tokens=64, max_new_tokens=64)
        for generated_tokens in (0, -1):
            with self.subTest(generated_tokens=generated_tokens):
                with self.assertRaisesRegex(ValueError, "invalid_live_fact_token_count"):
                    select_live_scope_facts(
                        "A, B, E", facts,
                        generated_tokens=generated_tokens, max_new_tokens=64,
                    )


if __name__ == "__main__":
    unittest.main()
