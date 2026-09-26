"""Offline, lookahead-safe event-study/backtest research tooling.

This package never issues buy/sell recommendations. It only reports
statistics about historical outcomes of pre-registered, transparent
conditions declared in a versioned study spec (``fintr_study_spec_v1``,
see ``fintr.research.study_spec``). See ``docs/study_engine.md`` and the
architecture ADR in ``docs/architecture.md``.
"""
