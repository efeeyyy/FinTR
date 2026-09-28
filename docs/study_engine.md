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

## Ratio-regime signal-date drop (data quality, pre-freeze)

`docs/prereg/DECISIONS.md` ("Dondurma öncesi veri kalitesi kuralı"): a lot
size vs. Yahoo-volume unit mismatch was found on one development symbol
(BIMAS, ~159 days at ratio exactly 0.5 -- Yahoo's retroactive 1:2 scale
correction against AKD's un-rescaled lots). `fintr.features.price_join.
join_price_history` already detects this as a `ratio_regime` (a maximal run
of consecutive joined days whose `volume_ratio` settles on a constant `1/k`,
integer `k >= 2`, corroborated by `vwap_proxy/k` falling inside the bar's
`[low, high]`; tolerance `ratio_regime_tolerance`, default `0.01`) --
`probable_retroactive_price_adjustment`, a hypothesis, not a verified
corporate action.

`fintr.research.engine.load_symbol_data` now runs this detection per symbol
(`ratio_regime_tolerance`, a `run_study.py` CLI parameter, default
`fintr.features.price_join.DEFAULT_RATIO_REGIME_TOLERANCE` printed in the
result's `parameters` block) and `fintr.research.report.run_study` removes
every signal date that falls inside a detected regime from **both** the
conditioned and the baseline signal-date set for that symbol
(`fintr.research.engine.drop_ratio_regime_signal_dates`) before any event is
built -- the population, not the return formula, changes. Returns come from
Yahoo's own internally consistent series, so the exit window is never
separately excluded for landing inside a regime.

The result's `ratio_regime_quality[SYMBOL]` reports the regimes found
(`start`, `end`, `k`, `day_count`) and how many of that symbol's in-scope
signal dates were dropped; the text summary prints the same per symbol. A
symbol with no detected regime reports an empty list and a zero drop count
-- identical to the engine's behavior before this rule existed.

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
- `--split test`: additionally requires `--test-plan` and `--test-scope`
  (`primary` | `secondary`, no default; see "Test plan" below). The spec
  must be `frozen: true` and declare a non-empty `symbol_holdout` that is a
  subset of the split's holdout symbols. `--test-scope primary` then
  requires `--data` to be exactly the spec's full declared symbol set
  (development + holdout, `symbol_scope="declared"`); `--test-scope
  secondary` requires `--data` to be exactly `symbol_holdout`
  (`symbol_scope="holdout_only"`, unchanged from the pre-test-plan
  behaviour). `--no-split-guard` bypasses these checks (and the test-plan
  scope logic) entirely for non-BIST 30 exploration -- `--test-plan`/
  `--test-scope` are still required to be present, but unused; the result
  falls back to `symbol_scope="declared"`.
- Excluded symbols (`configs/exclusions/symbols.json`), in `--data` or
  anywhere in the spec, are always refused; there is no override.
- The result's `split_guard` block records `guard_bypassed: true`, the
  warning and `bypassed_holdout_symbols` under `--no-split-guard`, and the
  text output starts with a `!!! UYARI` line.

Every result carries `split_guard` (split file, its SHA-256, split id,
exclusions file and its SHA-256) and `symbol_scope`.

## Test plan (`fintr_test_plan_v1`) and the one-shot holdout test

`fintr.research.test_plan.load_test_plan` parses a second, narrower
pre-registration artifact sitting on top of a spec's own `frozen`/
`preregistration` block: the test window (`test_start`..`test_end`), the
minimum post-window tradeable-bar count, the per-side cost, and -- per spec
-- the exact raw file and canonical (`spec_sha256`) hashes recorded at
freeze time (`docs/prereg/FREEZE.md`). `scripts/run_study.py --split test`
refuses to run unless the given `--spec` file matches, byte-for-byte, the
plan's entry for its `spec_id`, and unless `spec.train_test_cutoff` equals
the plan's `test_start`. The result records `test_plan_path`,
`test_plan_sha256` and `test_scope`.

**E3 (window end):** the plan's `test_end` is passed to `fintr.research.
report.run_study` as `test_window_end`; `fintr.research.engine.
signal_dates_for_split`'s optional `window_end` then drops every test-split
signal date after it (train is never affected; `window_end=None`, the
default, reproduces the pre-E3 behaviour exactly). The result records
`test_window_end` (`null` when not given).

`fintr.research.family.evaluate_primary_family` also checks that every
input result shares the same `test_scope`, and only treats `split ==
"test"` as `confirmatory` when `test_scope` is `"primary"` (or absent, for
results predating this field) -- a `secondary` (holdout-only, E5) family
decision is always a robustness check, never confirmatory on its own
(`docs/prereg/DECISIONS.md` 2026-09-28).

`scripts/run_holdout_test.py` is the one-shot executor: `--dry-run`
(default) checks plan/spec hashes, a clean git tree, per-symbol
period-analysis/price-history coverage (E4-merging several period records
whose `start >= test_start`; a pre-window-only record is never used) and
the absence of a lock or a completed manifest, writing nothing.
`--execute` re-checks all of that, atomically creates
`data/interim/studies/test/LOCK` (`O_EXCL`), runs the four specs in both
test scopes via `run_study.py --save`, runs `evaluate_study_family.py` on
the primary H1+H2 results only, and writes `data/interim/studies/test/
manifest.json` (`completed: true` only on full success). See
`docs/prereg/README.md` ("Holdout testi çalıştırma") for the full
procedure and the open question about pre-window price history still being
read by the adjustment-boundary/ratio-regime detection.

## Transaction cost (per side)

`--cost-bps` is a **per-side** assumption charged once at entry and once at
exit: `net_return = gross_return - 2 * cost_bps / 10000`, so the round trip
is `2 x cost_bps` (e.g. `--cost-bps 10` = 20 bps round trip). The result
records it as `parameters.transaction_cost_basis`. It is an assumption, not
a sourced commission figure. Because the same cost is subtracted from
conditioned and baseline events, it does not change `difference_mean`.

## E1: moving block bootstrap by signal date (primary test)

Same-day events across symbols share the market move, and 5-day holding
windows of consecutive signal dates overlap, so the i.i.d. bootstrap below
understates the variance of the difference. A spec may declare

```json
"block_bootstrap": {"method": "moving_block_by_signal_date",
                    "block_length_signal_dates": 5, "resamples": 9999, "seed": 20261001}
```

There is no default: a spec without it gets `block_bootstrap_difference:
null`. For each horizon of the pooled (`overall`) block:

1. The date axis is the sorted distinct signal dates that carry at least one
   kept (non-dropped) conditioned or baseline event (`N` dates).
2. Each replicate draws `ceil(N / L)` block starts with replacement from
   `0..N-L` (`random.Random(seed)`, never global random state), takes `L`
   consecutive dates from each start and cuts the concatenation to `N`
   dates (non-circular moving blocks, Künsch 1989). `N < L` reports
   `status: too_few_signal_dates` instead of a statistic.
3. **Every event of every symbol on a drawn date enters the replicate
   together**, once per draw of that date. The replicate's conditioned mean
   and baseline mean are event-weighted means over the **same drawn dates**;
   `delta*_b = conditioned_mean - baseline_mean`. A replicate whose drawn
   dates hold no conditioned event has no `delta*_b` and is counted in
   `undefined_resamples`.
4. One-sided p for the spec's pre-registered `predicted_direction` only
   (never both tails): negative `p = (1 + #{delta*_b >= 0}) / (B + 1)`,
   positive `p = (1 + #{delta*_b <= 0}) / (B + 1)`; an undefined replicate
   counts as not supporting the prediction. Without a pre-registered
   direction no p is reported.
5. `percentile_05` / `percentile_95`: nearest-rank percentiles
   (`sorted[ceil(q * n) - 1]`) of the defined `delta*_b`, used for the
   SESOI falsification bound.

The result block also records `method`, `block_length_signal_dates`,
`resamples`, `seed`, `distinct_signal_dates`,
`distinct_conditioned_signal_dates` and `observed_difference` (float);
`reproducibility.block_bootstrap` repeats seed/resamples/block length.
Per-symbol blocks carry only the i.i.d. diagnostic (E1 is defined on the
pooled date panel). Formulas: `fintr.research.engine.FORMULAS`.

## Pre-registered family and Holm (`preregistration` block)

When a spec carries a `preregistration` block, the loader validates this
subset of it (the whole block stays in `spec_sha256`): `hypothesis_id`,
`family_id`, `family_role` (`primary` | `secondary_descriptive`),
`primary_family_members`, `family_size` (= number of members),
`multiple_comparison` (`holm`), `familywise_alpha`, `predicted_direction`,
`primary_horizon_trading_days`, `min_events_primary`,
`min_distinct_signal_dates_primary`, `sesoi_difference`. `family_role` is
`primary` exactly when the hypothesis is a listed member, and a spec with a
`preregistration` block must also declare `block_bootstrap`.

`run_study` then adds `hypothesis_test` to the result: the primary-horizon
difference, event and distinct-signal-date counts, whether the minimum-event
rule is met, the E1 one-sided p, the one-sided bound and whether it
excludes the SESOI, and `p_value_for_holm` -- the E1 p for a primary
member, or 1 when the minimum-event rule fails (the family never shrinks);
`null` for a `secondary_descriptive` hypothesis, which never enters Holm.

`scripts/evaluate_study_family.py --result ... --result ...` reads the saved
results of one family (same split and run parameters; every primary member
exactly once, else it refuses) and applies Holm (1979) over the primary
members only (`fintr.research.family`): sort ascending, reject while
`p(i) <= alpha / (m - i + 1)`, stop at the first failure; comparisons are
exact (`fractions.Fraction`). Primary members get an outcome class
(`supported`, `falsified`, `inconclusive`,
`inconclusive_insufficient_events`; confirmatory only for `--split test`);
secondary hypotheses are listed as `descriptive_only` with their unadjusted
p. For `configs/studies/prereg/` the family is H1 + H2 (m = 2, alpha 0.05);
H3 and H4 are `secondary_descriptive` (see `docs/prereg/DECISIONS.md`).

## E4: several period captures per symbol

AKD/custody requests cover ~3 months, so `--data` may repeat a symbol with
several period captures and the same price history path (a different price
path for the same symbol is refused). `fintr.research.capture_merge.
merge_period_captures` builds one timeline:

- precedence: the later `captured_at_utc` wins; capture-time order must
  equal requested-period order (`start`, `end`) and capture times must
  differ, otherwise the merge is refused as ambiguous;
- a date is owned as a whole by the highest-precedence capture carrying it:
  all dated series for that date come from that capture only (this is how a
  later capture replaces an earlier capture's provisional last custody day);
- period aggregates (`period_akd_net`, `custodian_diff`) are not merged (the
  merged capture carries none; no study feature reads them);
- gaps are reported, never filled: per seam the calendar days between the
  requested periods and the tradeable price dates that no capture covers.

The result's `capture_merge[SYMBOL]` block lists every source (label, file
SHA-256, packet SHA-256, capture time, requested period, owned/overridden
date counts), the overlap dates with the series that differ from the owner,
the seams and `any_seam_gap`. Input hashes are recorded as
`SYMBOL:period_capture:<i>` (a single capture keeps `SYMBOL:period_capture`)
and a single capture is used unchanged. Lag features (`custody_change`,
`flow_run_length`) read across a seam gap as if the server dates were
adjacent; check `any_seam_gap` before using them.

## Output

Per horizon: event count, mean/median net return, hit rate, baseline (all
days, same symbols -- a superset of the conditioned days, not a disjoint
complement) comparison, the difference of means with a seeded two-sample
percentile bootstrap confidence interval (i.i.d.; secondary diagnostic
only), the E1 block bootstrap (`block_bootstrap_difference`, see above),
the number of distinct conditioned signal dates, the applied per-side
transaction-cost assumption (bps, see above), and warnings when the event
count is below `min_event_warning_threshold`. Per-symbol breakdown mirrors
the overall block without E1. `ratio_regime_quality` (see above) reports the
per-symbol regime/drop-count data-quality block. The JSON artifact
(`fintr_study_result_v1`, `formula_version: study_engine_v3`) is written
under `data/interim/studies/` without overwriting, and carries a
reproducibility block: code commit, spec SHA-256, per-input-file SHA-256,
the i.i.d. and block bootstrap seeds/resamples, and the Python version.

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

`--split test` is refused unless `--test-plan`/`--test-scope` are given, the
spec's `frozen` is `true`, it matches the plan's recorded hashes, and (for
`--test-scope secondary`) only holdout symbols of the frozen split are
passed (see above). AKFIS is outside the BIST 30 split and not a holdout
symbol, so the train example above passes the guard; a BIST 30 holdout
symbol would not. See "Holdout testi çalıştırma" in `docs/prereg/README.md`
for a full `--split test` example.

## Ön kayıtlı hipotez önerileri

`docs/prereg/` (süreç, H1–H4, yeni feature/motor yeteneği listeleri,
`DECISIONS.md`) ve `configs/studies/prereg/` (eşleşen, `"frozen": false`
spec'ler) henüz dondurulmamış ön kayıt tasarımıdır; tavsiye veya
doğrulanmış bulgu değildir. `tests/test_prereg_specs.py` bu spec'leri
yükleyiciyle, `tests/test_study_prereqs.py` E1/E4/Holm/maliyet
ön koşullarını sentetik veriyle doğrular.
