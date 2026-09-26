# Eval case examples (synthetic only)

This directory holds a documented, empty-of-real-data **template pair** for
the eval manifest and annotation formats defined in
`src/fintr/evaluation/manifest.py`:

- `manifest.example.json` — one `fintr_eval_manifest_v1` case. Symbol
  `ZZTEST` and dates in 2030 are deliberately obviously synthetic; the
  `source.sha256` is the checksum of the literal bytes
  `synthetic-example-capture-do-not-use-as-real-data`, not a real capture.
- `annotation.example.json` — one matching `fintr_eval_annotation_v1` record,
  frozen, with a checksum computed by
  `fintr.evaluation.manifest.compute_annotation_checksum`. All gold facts and
  allowed calculations are placeholder text.

Real eval cases and their captures must never be committed here. They live
under the git-ignored `data/eval/` tree (see `.gitignore`), referenced from a
manifest file that itself may live under `data/eval/` too. Validate any real
manifest and its annotations with:

```
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts\validate_eval_manifest.py <manifest.json> --annotations <dir>
```

The validator checks structural correctness and runs the leakage guard
(`src/fintr/evaluation/leakage_guard.py`) against the bundled
`src/fintr/evaluation/dev_registry.json`, which lists the known
development-only material (the AKFIS 2026-09-23 pilot and the AKFIS
2026-09-08..2026-09-25 period capture) by symbol and date range — no
financial values. The symbol check is company-level: any case for a symbol
that appears in the registry is rejected regardless of its own observation
period, because custody/settlement data reconciles on a T+2 basis across
weekends, so a same-symbol case dated a few days after a registered
development period can still carry information from it. The registry's
date range is kept only as documentation of what was used, not as a cutoff.
A passing validation run does **not** certify gold quality, human review,
independent permission/license verification, or genuinely held-out status;
see `docs/evaluation_protocol.md` for what remains human work.
