"""Stage 2, step 2: Telegram observation notifications.

Reads the scanner's saved ``fintr_scanner_observations_v1``/``_v2`` document and
sends a plain-text, Turkish observation notice to Efe's own Telegram chat.
This is an observation notice, not a signal: it carries no score, rank,
return or target, and no buy/sell recommendation. No network by default
(``scripts/notify_observations.py`` is dry-run unless ``--send`` is
passed). See ``docs/notify.md``.
"""
