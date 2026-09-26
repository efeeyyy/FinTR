# Offline event-study / backtest engine (`fintr.research`)

Research tooling only: reports historical-outcome statistics for a
pre-registered condition versus a baseline. It never emits a buy/sell
recommendation and never scores a security. See the ADR in
`docs/architecture.md` ("Offline event-study/backtest engine") for the
problem/alternatives/decision/consequences.

## Study spec (`fintr_study_spec_v1`)

A study spec is the pre-registration artifact: it names the symbols, the
train/test time cutoff, an optional symbol hold-out list, entry timing,
horizons, the adjustment-boundary policy, the bootstrap seed, and the
condition itself. `fintr.research.study_spec.load_study_spec` parses and
validates one; see `examples/study_specs/*.json` for three schema examples,
each explicitly marked as an example, not a recommendation or a validated
finding.

### Condition grammar

No free text, no model call: a condition is JSON built only from
comparisons (`gt`/`ge`/`lt`/`le`/`eq`/`ne`), boolean combinators
(`and`/`or`/`not`), decimal constants, and calls to one of four fixed
feature names (`fintr.research.features.FEATURE_FORMULAS`):

- `top1_akd_net_share_of_volume` -- largest-magnitude broker's AKD net as a
  share of the day's `daily_market_proxy.sum_total_size` (itself the
  unverified `derived_proxy_not_official_ohlc` hypothesis from
  `fintr.features.period`).
- `flow_run_length` (`codes`) -- current same-sign run length of the summed
  AKD net over a named code set.
- `custody_change` (`codes`, `lag_days`) -- change in summed custody lot
  count for a named code set over a given number of *server dates* (not
  calendar days). This does not verify any classification (e.g. "foreign
  custodian") of the codes given; it only measures whatever codes the spec
  supplies.
- `vwap_proxy_vs_close` -- relative gap between the AKD-derived VWAP proxy
  and the same day's saved price close.

## Lookahead guard

Every feature is evaluated against a `PeriodCapture` that
`fintr.research.features.truncate_period_capture` has already cut down to
dates `<= t` before the feature runs -- day `t+1` has no representation in
the object the feature receives, regardless of what parameters the
condition supplies. `tests/test_study_engine.py` verifies this directly:
truncating to `t` and to `t+1` produce different feature values, and a
feature can never reach past its own truncated input.

## Entry, exit, holidays, adjustment boundaries

Entry is at the next tradeable bar's open (or close,
`spec.entry_timing == "next_close"`) strictly after the signal date;
exit is `horizon` tradeable bars later. A bar classified `"placeholder"` by
`fintr.features.price_join.classify_price_bar` (the documented shape of an
exchange-holiday marker bar) is never counted as tradeable and never
supplies a price.

If any date in `(entry_date, exit_date]` is one of
`fintr.features.price_join.join_price_history(...).adjustment_boundary_dates`
(computed over the full saved price series), raw open/close are not
comparable across it; the engine either uses `adjusted_close` at both
entry and exit (`adjustment_boundary_policy: "use_adjusted"`) or drops the
event (`"drop"`) -- both outcomes are explicitly counted in the result
(`dropped_adjustment_boundary`, `adjusted_close_used_count`).

## Custody scale jumps

`fintr.research.engine.detect_custody_scale_jumps` flags a server date
where the whole-panel custody lot total (summed only over codes listed on
both that date and the previous listed date) changes by at least
`custody_scale_jump_ratio` in either direction -- a lot-size/unit
redefinition, not one broker's ordinary flow. A conditioned event whose
condition read custody data spanning such a date is dropped and counted
(`dropped_custody_scale_jump`), never silently kept.

## Split and pre-registration gate

`train`: entry dates before `train_test_cutoff`, symbols excluding
`symbol_holdout`. `test`: entry dates on/after the cutoff, every symbol the
spec ever declared (train symbols + hold-out). `fintr.research.report.
run_study` raises rather than build a `test` result for a spec whose
`frozen` is `false`.

### Frozen BIST 30 split guard (`scripts/run_study.py`)

On top of the engine gate, the CLI enforces the active frozen symbol split
(resolved only through `configs/splits/current.json`; see
[evaluation_protocol.md](evaluation_protocol.md#dondurulmuş-bist-30-hisse-ayrımı-ve-dışlama-listesi-kodla-zorunlu))
and the symbol exclusions before any data file is read:

- `--split train`: refused if the spec's `symbols` or any `--data` symbol
  is a holdout symbol of the split.
- `--split test`: refused unless the spec is `frozen: true`, declares a
  non-empty `symbol_holdout` that is a subset of the split's holdout
  symbols, and every `--data` symbol is a holdout symbol. The engine then
  runs with `symbol_scope="holdout_only"`: only `symbol_holdout` is
  evaluated (no development symbol mixed into the one-shot holdout result).
- Excluded symbols (`configs/exclusions/symbols.json`), in `--data` or
  anywhere in the spec, are always refused; there is no override.
- `--no-split-guard` disables only the holdout checks (non-BIST 30
  exploration); the test split then falls back to `symbol_scope="declared"`.
  The result's `split_guard` block records `guard_bypassed: true`, the
  warning and `bypassed_holdout_symbols`, and the text output starts with a
  `!!! UYARI` line.

Every result carries `split_guard` (split file, its SHA-256, split id,
exclusions file and its SHA-256) and `symbol_scope`.

## Output

Per horizon: event count, mean/median net return, hit rate, baseline (all
days, same symbols -- a superset of the conditioned days, not a disjoint
complement) comparison, the difference of means with a seeded two-sample
percentile bootstrap confidence interval, the applied transaction-cost
assumption (bps, a CLI parameter -- see `scripts/run_study.py --cost-bps`),
and warnings when the event count is below `min_event_warning_threshold`.
Per-symbol breakdown mirrors the overall block. The JSON artifact
(`fintr_study_result_v1`) is written under `data/interim/studies/` without
overwriting, and carries a reproducibility block: code commit, spec
SHA-256, per-input-file SHA-256, the bootstrap seed, and the Python
version.

A price-history capture's own `symbol` field is not compared against the
period capture's symbol: the price source echoes its own ticker convention
(e.g. `AKFIS.IS` for a period capture symbol `AKFIS`), the same legitimate
difference `fintr.features.price_join.join_price_history` already leaves
unchecked (see `docs/price_history.md`). The CLI operator is responsible
for pairing the right files under one `--data SYMBOL=...` entry.

## Running it

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\run_study.py `
  --spec examples\study_specs\example_broker_concentration_v1.json `
  --data AKFIS=data\interim\period_analysis\AKFIS\2026-06-29_2026-09-25.json,data\interim\price_history\AKFIS\2026-06-29_2026-09-25.json `
  --split train --cost-bps 10 --save
```

`--split test` is refused unless the spec's `frozen` is `true` and only
holdout symbols of the frozen split are passed (see above). AKFIS is outside
the BIST 30 split and not a holdout symbol, so the train example above passes
the guard; a BIST 30 holdout symbol would not.

## Ön kayıtlı hipotez önerileri

`docs/prereg/` (süreç, H1–H4, yeni feature/motor yeteneği listeleri) ve
`configs/studies/prereg/` (eşleşen, `"frozen": false` spec'ler) Efe'nin
onayını bekleyen önerilerdir; tavsiye veya doğrulanmış bulgu değildir.
`tests/test_prereg_specs.py` bu spec'leri yükleyiciyle doğrular.
