"""Rule direction source for the notifier's optional direction label.

Efe's decision of 2026-10-02 (AGENTS.md "Guvenlik sinirlari",
``docs/prereg/DECISIONS.md``; personal use only): the notifier may show
the frozen rule's hypothesised direction. The direction is read ONLY from
each frozen spec's ``preregistration.predicted_direction``, through the
scanner's existing hash-checked loader
(``fintr.scanner.rules.load_scanner_rules``: spec must be ``frozen``, raw
and canonical SHA-256 must match ``docs/prereg/FREEZE.md``). Nothing is
re-implemented here and nothing is guessed: if the rule file or any spec
fails that check, the caller gets ``ScannerRulesError`` and must label no
line (``scripts/notify_observations.py`` then prints "yön bilinmiyor").
"""

from __future__ import annotations

from pathlib import Path

from fintr.scanner.rules import load_scanner_rules


def load_rule_directions(rules_file: Path, *, project_root: Path) -> dict[str, str]:
    """rule_id -> ``predicted_direction`` of the frozen, hash-verified spec.

    Raises ``fintr.scanner.rules.ScannerRulesError`` when the rules file or
    any spec is unreadable, unfrozen or fails its hash check."""
    rules = load_scanner_rules(Path(rules_file), project_root=Path(project_root))
    return {rule.rule_id: rule.spec.preregistration.predicted_direction for rule in rules}


def load_rule_horizons(rules_file: Path, *, project_root: Path) -> dict[str, int]:
    """rule_id -> ``primary_horizon_trading_days`` of the frozen, hash-verified
    spec (layout v2's "Vade" line). Same loader and failure mode as
    ``load_rule_directions``; entries that are not plain positive integers are
    left out rather than guessed."""
    rules = load_scanner_rules(Path(rules_file), project_root=Path(project_root))
    horizons: dict[str, int] = {}
    for rule in rules:
        value = rule.spec.preregistration.primary_horizon_trading_days
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            horizons[rule.rule_id] = value
    return horizons
