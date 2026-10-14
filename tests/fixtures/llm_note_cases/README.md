# llm_note golden cases

Synthetic negative/positive corpus for `fintr.llm_note.validate.validate_note`
(run by `tests/test_llm_note_cases.py`).

- `packet.json`: one synthetic input packet (symbol `SYNA`, institutions
  `AAA`..`DDD`, five days 2026-01-05..2026-01-09, `as_of` 2026-01-09, rule H2).
  Shape taken from the packet `src/fintr/llm_note/packet.py` builds for the
  synthetic fixtures of `tests/test_llm_note.py`; values were rewritten by hand
  so every day and institution differs (wrong-day / wrong-institution cases
  need distinguishable values). The rule entry carries the repository's own
  frozen H2 text. No market data, nothing from `data/`.
- `cases/NN_<case_id>.json`: `case_id`, `description`, `note` (the model output),
  `expected` (`accept`/`reject`), `expected_reason` (validator category).
  A case the validator gets wrong additionally has `known_gap: true`,
  `known_gap_reason` and `current_result` (`{ok, reason}` it gives today); the
  test pins that behaviour so a change is noticed. When a gap is closed, delete
  the three `known_gap*` keys.
- Invisible/combining/homoglyph characters inside notes are stored as `\uXXXX`
  escapes so the files stay reviewable.

Evaluation data is not used here; the notes are hand-written and are not
training, prompt-tuning or model-selection material.
