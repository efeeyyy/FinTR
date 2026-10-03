"""Daily analysis note step (Efe's decision of 2026-10-02; see
``docs/prereg/DECISIONS.md``, ``docs/architecture.md`` and ``docs/notify.md``).

For every ``condition_met`` observation of a saved scanner output, a Claude
model (Claude Code CLI, headless, Efe's subscription) writes an
evidence-based *hypothesis assessment* -- the frozen rule's direction is
``destekliyor`` / ``zayıflatıyor`` / ``belirsiz`` according to the raw data
-- never a buy/sell order, target, stop, position size, return forecast or
score. Everything the model returns is fail-closed validated by code
(``fintr.llm_note.validate``): output schema, allowed assessment values,
forbidden wording, every number against the input packet, every evidence
path against the packet. A failed note never blocks the deterministic
notification. The packet is raw data truncated at ``as_of`` (no price
series, no return, nothing after ``as_of``; K4 holds). Raw data leaving the
machine for Anthropic is Efe's decision; the Mini App licence statement was
made for data collection and was not verified to cover third-party
transmission.
"""
