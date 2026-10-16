"""Walk-forward directional accuracy tracking for the frozen H1-H4 rules.

Descriptive monitoring only (Efe's decision of 2026-10-16,
``docs/prereg/DECISIONS.md``; ``docs/accuracy.md``): for development symbols,
did the realized direction after a ``condition_met`` observation match the
frozen rule's ``predicted_direction``, compared with the unconditional base
rate. It is not a study, not a validation and not a signal; it changes no
rule, threshold or spec and sends nothing to Telegram.
"""
