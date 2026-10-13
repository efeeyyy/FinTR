"""Tamper-evident forward log of the daily artifacts (research note D3).

An append-only JSONL manifest (``forward_log/manifest.jsonl``, git-tracked)
holds, per ``as_of``, the SHA-256 of the day's scanner observations, notify
receipts and LLM-note files, chained by ``prev_hash``/``entry_hash``. The
manifest contains hashes only, no data content. See ``docs/forward_log.md``.
"""
