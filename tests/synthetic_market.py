"""Synthetic market generator for engine calibration (null / planted effect).

Nothing here is real data: symbols are labelled ``SYN01``.., prices are a
seeded random walk, AKD/custody/metadata rows are random numbers shaped like
the real ``PeriodCapture``/``PriceHistoryCapture`` dataclasses, and nothing
is read from ``data/``. Used by ``scripts/calibrate_engine.py`` and
``tests/test_engine_calibration.py`` to check the frozen study engine's
false-positive rate and power (docs/research/engine_calibration.md). The
synthetic returns say nothing about any real hypothesis or market.

Design
------
* Daily log returns ``r[i][t]`` (symbol i, day t) come from a seeded
  one-factor model. Variant ``iid``: independent normal innovations,
  market share of variance ``market_share``. Variant ``dependent``:
  GARCH(1,1) volatility clustering for the market factor and each
  idiosyncratic term, Student-t(6) innovations, weak AR(1) serial
  correlation, and a condition probability that moves with the market
  volatility and with a persistent market-wide latent (so conditioned days
  cluster in time and in high-volatility regimes). Variant ``xsection``
  (added 2026-10-14, TRIAL_LOG T-30, from the real flag-only statistics of
  T-29): returns exactly as ``iid``; the condition input loads on a
  market-wide latent that is i.i.d. over days (no persistence), so flags
  cluster across symbols on the same date (dispersion index ~5, frequency
  ~0.15 per side) but not in time.
* ``open[t] = close[t-1] * exp(GAP_SHARE * r[t])``,
  ``close[t] = close[t-1] * exp(r[t])`` (exact consistency, no adjustment
  boundary, volume non-zero so no bar is a holiday placeholder).
* The engine's forward return is ``close[t+6] / open[t+1] - 1`` (entry at the
  next open, exit 5 tradeable bars later), i.e. ``(1 - GAP_SHARE) * r[t+1] +
  r[t+2] + ... + r[t+6]`` in log terms.
* The AKD-derived condition input is drawn **independently of every return
  after t**: ``x = vwap_proxy_vs_close`` is drawn (frequencies below), then
  broker ``total_cost`` is set so the engine's proxy
  ``sum(size*cost)/sum(size)`` equals ``close * (1 + x)`` up to price
  rounding. Under NULL no later return depends on any flag; under ALT a known
  log-return shift is added to the forward window of flagged events.
* Flags used for planting/oracle statistics are recomputed from the final
  quantized rows with the engine's own Decimal formulas, so they agree
  exactly with what ``fintr.research`` evaluates.

Deterministic: every random draw comes from ``random.Random`` seeded with a
string; the same ``(config, seed)`` gives byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import math
import random

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
    PeriodMetadataDay,
)
from fintr.data.price_history import DailyBar, PriceHistoryCapture, PriceHistoryCaptureMeta


GAP_SHARE = 0.3  # open[t] = close[t-1] * exp(GAP_SHARE * r[t])
HORIZON = 5  # engine primary horizon (trading days); entry bar is t+1
# Forward window of the signal on day t spans r[t+1] (intraday part) .. r[t+6].
FORWARD_LAST_OFFSET = HORIZON + 1

HYPOTHESES = ("H1", "H2", "H3", "H4")
# Frozen-spec thresholds (configs/studies/prereg/h1..h4): copied here only to
# compute truth flags; tests/test_engine_calibration.py checks that the flags
# reproduce the engine's own event counts.
_X_LOW, _X_HIGH = Decimal("0.01"), Decimal("0.10")
_TOP1_MIN = Decimal("0.10")

VARIANTS = ("iid", "dependent", "xsection")
# xsection latent loading, calibrated on flags only (dispersion index ~5 at
# frequency ~0.15; scripts in docs/research/engine_calibration.md "Aday yöntemler")
XSECTION_LOADING = 0.65  # 5 seeds x 20x250: dispersion 5.1/5.2, frequency 0.15, flag/count acf lag1 ~0


@dataclass(frozen=True)
class MarketConfig:
    variant: str = "iid"
    n_symbols: int = 20
    n_days: int = 250
    start: date = date(2025, 10, 1)  # first server date; weekdays only
    n_brokers: int = 6
    # Forward 5-day (6-piece) log-return sd is about 5% (docs/prereg/README.md
    # power assumption sigma_5 ~ 5%): daily sd = 0.05 / sqrt(5.49).
    daily_sigma: float = 0.05 / math.sqrt((1 - GAP_SHARE) ** 2 + 5)
    market_share: float = 0.4  # share of return variance from the market factor
    vwap_scale: float = 0.0078  # sd of x; P(x <= -0.01) = P(x >= 0.01) ~ 10% (iid)
    concentration_prob: float = 0.2  # P(a day has one dominant broker net flow)
    outlier_prob: float = 0.003  # P(|x| > 0.10), exercises the band
    # dependent variant
    garch_market: tuple[float, float] = (0.08, 0.90)  # (alpha, beta), unit variance
    garch_idio: tuple[float, float] = (0.05, 0.93)
    ar1: float = 0.08
    t_df: int = 6
    latent_phi: float = 0.9  # persistence of the market-wide condition latent
    latent_loading: float = 0.35
    symbol_phi: float = 0.3  # within-symbol persistence of the condition input
    # xsection variant: same-date cross-symbol clustering only (latent i.i.d. over days)
    xsection_loading: float = XSECTION_LOADING
    xsection_vwap_scale: float = 0.00965  # P(x >= 0.01) = P(x <= -0.01) ~ 0.15

    def __post_init__(self):
        if self.variant not in VARIANTS:
            raise ValueError(f"unknown variant {self.variant!r}")
        if self.n_days < 30 or self.n_symbols < 1 or self.n_brokers < 2:
            raise ValueError("synthetic market too small")


@dataclass
class SyntheticWorld:
    config: MarketConfig
    seed: str
    symbols: tuple[str, ...]
    dates: tuple[date, ...]
    captures: dict[str, PeriodCapture]
    prices: dict[str, PriceHistoryCapture]
    # flags[h][symbol][t] -> bool, recomputed from the final rows
    flags: dict[str, dict[str, list[bool]]]
    # planted log-return shift per hypothesis (0.0 under NULL)
    planted: dict[str, float]
    # oracle: noise-free conditioned-minus-baseline mean of the planted
    # forward shift over eligible events, per hypothesis (log-return units)
    oracle_difference: dict[str, float] = field(default_factory=dict)
    event_counts: dict[str, int] = field(default_factory=dict)  # eligible flagged events
    distinct_dates: dict[str, int] = field(default_factory=dict)
    frequencies: dict[str, float] = field(default_factory=dict)  # flagged / all in-scope days


def synthetic_dates(start: date, n_days: int) -> tuple[date, ...]:
    days: list[date] = []
    d = start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return tuple(days)


def _std_t(rng: random.Random, df: int) -> float:
    z = rng.gauss(0.0, 1.0)
    chi = rng.gammavariate(df / 2.0, 2.0)
    return z / math.sqrt(chi / df) / math.sqrt(df / (df - 2.0))


def _garch_series(rng: random.Random, n: int, alpha: float, beta: float, df: int | None):
    """Unit-unconditional-variance GARCH(1,1): returns (values, cond_sd)."""
    omega = 1.0 - alpha - beta
    var = 1.0
    values: list[float] = []
    sds: list[float] = []
    for _ in range(n):
        sd = math.sqrt(var)
        z = _std_t(rng, df) if df else rng.gauss(0.0, 1.0)
        value = sd * z
        values.append(value)
        sds.append(sd)
        var = omega + alpha * value * value + beta * var
    return values, sds


def _ar1_gaussian(rng: random.Random, n: int, phi: float) -> list[float]:
    """Unit-variance AR(1)."""
    out: list[float] = []
    prev = rng.gauss(0.0, 1.0)
    scale = math.sqrt(1.0 - phi * phi)
    for _ in range(n):
        prev = phi * prev + scale * rng.gauss(0.0, 1.0)
        out.append(prev)
    return out


def _dec4(value: float) -> Decimal:
    return Decimal(f"{value:.4f}")


def _forward_pieces(shift: float) -> tuple[float, float]:
    """Per-piece log shift added to r[t+1] and to each of r[t+2..t+6] so the
    forward log return shifts by exactly ``shift``."""
    per_piece = shift / FORWARD_LAST_OFFSET
    return per_piece / (1.0 - GAP_SHARE), per_piece


def generate_world(
    config: MarketConfig, seed: int | str, *, planted: dict[str, float] | None = None,
    target_difference: dict[str, float] | None = None,
) -> SyntheticWorld:
    """Build one synthetic panel.

    ``planted`` maps a hypothesis id (``H1``..``H4``) to a log-return shift
    added to the forward window of every event flagged for it. Missing /
    empty means NULL. The random noise is identical for any ``planted``
    (common random numbers), only the shifts differ.

    ``target_difference`` (alternative to ``planted``) asks instead for a
    noise-free conditioned-minus-baseline mean forward shift of exactly the
    given value per hypothesis. Overlapping 5-day windows, clustered flags and
    the baseline containing the events themselves make that difference differ
    from the per-event shift, so the per-event shifts are solved (small
    linear system) for each panel.
    """
    if planted and target_difference:
        raise ValueError("give either planted or target_difference")
    for h in target_difference or {}:
        if h not in HYPOTHESES:
            raise ValueError(f"unknown hypothesis {h!r}")
    planted ={h: float(v) for h, v in (planted or {}).items() if v}
    for h in planted:
        if h not in HYPOTHESES:
            raise ValueError(f"unknown hypothesis {h!r}")
    tag = f"{seed}:{config.variant}"
    dates = synthetic_dates(config.start, config.n_days)
    n, T, K = config.n_symbols, config.n_days, config.n_brokers
    symbols = tuple(f"SYN{i + 1:02d}" for i in range(n))
    dependent = config.variant == "dependent"
    xsection = config.variant == "xsection"

    # ---- market-level series -------------------------------------------------
    rng_m = random.Random(f"{tag}:market")
    if dependent:
        mkt, mkt_sd = _garch_series(rng_m, T, *config.garch_market, config.t_df)
        latent = _ar1_gaussian(rng_m, T, config.latent_phi)
        vol_mult = [min(2.0, max(0.5, sd)) for sd in mkt_sd]
    else:
        mkt = [rng_m.gauss(0.0, 1.0) for _ in range(T)]
        latent = [rng_m.gauss(0.0, 1.0) for _ in range(T)] if xsection else [0.0] * T
        vol_mult = [1.0] * T

    ms = config.market_share
    # ---- per-symbol noise paths (independent of any planted shift) ----------
    log_r: list[list[float]] = []
    x_draw: list[list[float]] = []
    conc_draw: list[list[tuple]] = []
    for i, sym in enumerate(symbols):
        rng = random.Random(f"{tag}:{sym}:ret")
        if dependent:
            idio, _ = _garch_series(rng, T, *config.garch_idio, config.t_df)
        else:
            idio = [rng.gauss(0.0, 1.0) for _ in range(T)]
        innov = [
            config.daily_sigma * (math.sqrt(ms) * mkt[t] + math.sqrt(1.0 - ms) * idio[t])
            for t in range(T)
        ]
        if dependent and config.ar1:
            phi = config.ar1
            norm = math.sqrt(1.0 - phi * phi)
            series, prev = [], 0.0
            for t in range(T):
                prev = phi * prev + norm * innov[t]
                series.append(prev)
            innov = series
        log_r.append(innov)

        rng_x = random.Random(f"{tag}:{sym}:akd")
        w = _ar1_gaussian(rng_x, T, config.symbol_phi) if dependent else [
            rng_x.gauss(0.0, 1.0) for _ in range(T)
        ]
        a = config.latent_loading if dependent else (config.xsection_loading if xsection else 0.0)
        x_scale = config.xsection_vwap_scale if xsection else config.vwap_scale
        xs: list[float] = []
        for t in range(T):
            u = a * latent[t] + math.sqrt(1.0 - a * a) * w[t]
            x = x_scale * vol_mult[t] * u
            if rng_x.random() < config.outlier_prob:
                x = rng_x.choice((-1.0, 1.0)) * rng_x.uniform(0.11, 0.25)
            xs.append(x)
        x_draw.append(xs)
        # broker structure draws (sizes, nets, cost noise), fixed per (symbol, day)
        days_struct: list[tuple] = []
        for t in range(T):
            base = [rng_x.lognormvariate(math.log(2.0e5), 0.8) for _ in range(K)]
            frac = [rng_x.uniform(-0.15, 0.15) for _ in range(K)]
            conc = rng_x.random() < config.concentration_prob
            j = rng_x.randrange(K)
            q = rng_x.uniform(0.14, 0.28)
            net_share = rng_x.uniform(0.75, 1.0)
            sign = rng_x.choice((-1, 1))
            eps = [rng_x.gauss(0.0, 0.002) for _ in range(K)]
            vol_u = rng_x.uniform(0.8, 1.2)
            range_u = (abs(rng_x.gauss(0.0, 0.004)), abs(rng_x.gauss(0.0, 0.004)))
            days_struct.append((base, frac, conc, j, q, net_share, sign, eps, vol_u, range_u))
        conc_draw.append(days_struct)

    # ---- pass 1: flags from the drawn x (decides which windows get a shift) ---
    def band_flags(x: float, conc: bool) -> dict[str, bool]:
        pos = _X_LOW <= Decimal(repr(x)) <= _X_HIGH
        neg = -_X_HIGH <= Decimal(repr(x)) <= -_X_LOW
        return {"H1": neg, "H2": pos, "H3": neg and conc, "H4": pos and conc}

    pass1 = [[band_flags(x_draw[i][t], conc_draw[i][t][2]) for t in range(T)] for i in range(n)]

    def unit_shifts(h: str) -> list[list[float]]:
        """Shift arrays from a unit forward shift on every pass-1 event of ``h``."""
        first, rest = _forward_pieces(1.0)
        arrays = [[0.0] * T for _ in range(n)]
        for i in range(n):
            for t in range(T):
                if not pass1[i][t][h]:
                    continue
                if t + 1 < T:
                    arrays[i][t + 1] += first
                for k in range(2, FORWARD_LAST_OFFSET + 1):
                    if t + k < T:
                        arrays[i][t + k] += rest
        return arrays

    if target_difference:
        hs = list(target_difference)
        units = {h: unit_shifts(h) for h in hs}
        # response matrix: noise-free conditioned-minus-baseline mean forward
        # shift of event class g (row) per unit shift planted in h (column)
        matrix = [
            [_event_mean_difference(units[h], [[f[g] for f in row] for row in pass1])
             for h in hs]
            for g in hs
        ]
        solved = _solve_linear(matrix, [float(target_difference[g]) for g in hs])
        planted = dict(zip(hs, solved))
    shifts = [[0.0] * T for _ in range(n)]
    for h, s in planted.items():
        arrays = unit_shifts(h)
        for i in range(n):
            for t in range(T):
                shifts[i][t] += s * arrays[i][t]

    # ---- build prices, AKD, custody, metadata --------------------------------
    captures: dict[str, PeriodCapture] = {}
    prices: dict[str, PriceHistoryCapture] = {}
    flags: dict[str, dict[str, list[bool]]] = {h: {} for h in HYPOTHESES}
    shift_arrays: dict[str, list[float]] = {}
    codes = tuple(f"B{k + 1:02d}" for k in range(K))
    meta = PriceHistoryCaptureMeta(
        schema="fintr_price_history_capture_v1", source_id="synthetic",
        request_url="synthetic://engine-calibration", accessed_at_utc="2026-10-13T00:00:00Z",
        permission_status="user_asserted_not_independently_verified", unofficial_source=True,
        raw_response_sha256="0" * 64, normalized_sha256="0" * 64,
    )
    for i, sym in enumerate(symbols):
        rng_p = random.Random(f"{tag}:{sym}:level")
        level = math.exp(rng_p.uniform(math.log(20.0), math.log(300.0)))
        total_r = [log_r[i][t] + shifts[i][t] for t in range(T)]
        shift_arrays[sym] = shifts[i]
        close_prev = level
        bars: list[DailyBar] = []
        akd_net: list[DatedCodeValues] = []
        akd_mkt: list[DatedMarketProxyValues] = []
        custody: list[DatedCodeValues] = []
        metadata: list[PeriodMetadataDay] = []
        lots = [rng_p.uniform(5e6, 2e7) for _ in range(K)]
        sym_flags = {h: [False] * T for h in HYPOTHESES}
        for t, day in enumerate(dates):
            base, frac, conc, j, q, net_share, sign, eps, vol_u, range_u = conc_draw[i][t]
            close = close_prev * math.exp(total_r[t])
            open_ = close_prev * math.exp(GAP_SHARE * total_r[t])
            close_q, open_q = _dec4(close), _dec4(open_)
            sizes = [max(1, int(b)) for b in base]
            if conc:
                rest_total = sum(s for k, s in enumerate(sizes) if k != j)
                sizes[j] = max(1, int(rest_total * q / (1.0 - q)))
            nets = [int(sizes[k] * frac[k]) for k in range(K)]
            if conc:
                nets[j] = sign * int(sizes[j] * net_share)
            total = sum(sizes)
            # costs: size-weighted mean of (1 + eps_k - weighted_mean(eps)) is 1
            w_eps = sum(sizes[k] * eps[k] for k in range(K)) / total
            target = close_q * (Decimal(1) + Decimal(repr(x_draw[i][t])))
            cost_rows = [
                DailyMarketProxyRow(
                    codes[k], Decimal(sizes[k]),
                    (target * Decimal(repr(1.0 + eps[k] - w_eps))).quantize(Decimal("0.0001")),
                )
                for k in range(K)
            ]
            akd_mkt.append(DatedMarketProxyValues(day, tuple(cost_rows)))
            akd_net.append(DatedCodeValues(
                day, tuple(CodeValue(codes[k], Decimal(nets[k])) for k in range(K)),
            ))
            vwap = sum((r.total_size * r.total_cost for r in cost_rows), Decimal(0)) / sum(
                (r.total_size for r in cost_rows), Decimal(0))
            x_real = (vwap - close_q) / close_q
            top1 = Decimal(max(abs(v) for v in nets)) / Decimal(total)
            neg = -_X_HIGH <= x_real <= -_X_LOW
            pos = _X_LOW <= x_real <= _X_HIGH
            sym_flags["H1"][t] = neg
            sym_flags["H2"][t] = pos
            sym_flags["H3"][t] = neg and top1 >= _TOP1_MIN
            sym_flags["H4"][t] = pos and top1 >= _TOP1_MIN
            # custody: slow random walk of lots (never a scale jump)
            lots = [max(1.0, v * math.exp(rng_p.gauss(0.0, 0.01))) for v in lots]
            custody.append(DatedCodeValues(
                day, tuple(CodeValue(codes[k], Decimal(int(lots[k]))) for k in range(K)),
            ))
            retail = rng_p.uniform(20.0, 60.0)
            metadata.append(PeriodMetadataDay(
                day, _dec4(retail), _dec4(100.0 - retail), (), investor_count=None,
            ))
            hi = max(open_q, close_q) * (Decimal(1) + Decimal(repr(range_u[0])))
            lo = min(open_q, close_q) * (Decimal(1) - Decimal(repr(range_u[1])))
            bars.append(DailyBar(
                trade_date=day, timestamp_utc=int(day.toordinal()), open=open_q,
                high=hi.quantize(Decimal("0.0001")), low=lo.quantize(Decimal("0.0001")),
                close=close_q, adjusted_close=close_q,
                volume=max(1, int(total / 2.0 * vol_u)),
            ))
            close_prev = close
        for h in HYPOTHESES:
            flags[h][sym] = sym_flags[h]
        captures[sym] = PeriodCapture(
            symbol=sym, start=dates[0], end=dates[-1], captured_at_utc="2026-10-13T00:00:00Z",
            packet_sha256="0" * 64, server_dates=dates, period_akd_net=(),
            daily_akd_net=tuple(akd_net), daily_akd_market=tuple(akd_mkt),
            daily_custody_lots=tuple(custody), custodian_diff_start=dates[0],
            custodian_diff_end=dates[-1], custodian_diff=(), metadata=tuple(metadata),
            brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
        )
        prices[sym] = PriceHistoryCapture(
            meta=meta, symbol=sym, requested_start=dates[0], requested_end=dates[-1],
            date_source="synthetic", dropped_before_start_count=0, dropped_after_end_count=0,
            bars=tuple(bars),
        )

    world = SyntheticWorld(
        config=config, seed=str(seed), symbols=symbols, dates=dates, captures=captures,
        prices=prices, flags=flags, planted=planted,
    )
    _fill_truth(world, shift_arrays)
    return world


def eligible_signal_indices(n_days: int) -> range:
    """Signal-day indices t with a full forward window: entry bar t+1 and exit
    bar t+1+HORIZON must exist (the engine drops the rest as insufficient_bars)."""
    return range(0, n_days - (HORIZON + 1))


def forward_shift(shifts: list[float], t: int) -> float:
    """Planted forward log-return shift of the window opened by a signal on t."""
    return (1.0 - GAP_SHARE) * shifts[t + 1] + sum(
        shifts[t + k] for k in range(2, FORWARD_LAST_OFFSET + 1)
    )


def _event_mean_difference(arrays: list[list[float]], flag_rows: list[list[bool]]) -> float:
    """Noise-free conditioned-minus-baseline mean forward shift over eligible
    events (``arrays[i][t]`` = shift added to r[t] of symbol i)."""
    cond_sum = base_sum = 0.0
    cond_n = base_n = 0
    for shifts, flags in zip(arrays, flag_rows):
        for t in eligible_signal_indices(len(shifts)):
            value = forward_shift(shifts, t)
            base_sum += value
            base_n += 1
            if flags[t]:
                cond_sum += value
                cond_n += 1
    if not cond_n:
        raise ValueError("no eligible event for a planted hypothesis")
    return cond_sum / cond_n - base_sum / base_n


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (tiny systems)."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular planting system")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def _fill_truth(world: SyntheticWorld, shift_arrays: dict[str, list[float]]) -> None:
    T = world.config.n_days
    elig = list(eligible_signal_indices(T))
    base_sum = 0.0
    base_n = 0
    cond_sum = {h: 0.0 for h in HYPOTHESES}
    cond_n = {h: 0 for h in HYPOTHESES}
    date_sets: dict[str, set[int]] = {h: set() for h in HYPOTHESES}
    in_scope_flagged = {h: 0 for h in HYPOTHESES}
    for sym in world.symbols:
        sh = shift_arrays[sym]
        fs = {t: forward_shift(sh, t) for t in elig}
        base_sum += sum(fs.values())
        base_n += len(fs)
        for h in HYPOTHESES:
            row = world.flags[h][sym]
            in_scope_flagged[h] += sum(row)
            for t in elig:
                if row[t]:
                    cond_sum[h] += fs[t]
                    cond_n[h] += 1
                    date_sets[h].add(t)
    base_mean = base_sum / base_n
    for h in HYPOTHESES:
        world.event_counts[h] = cond_n[h]
        world.distinct_dates[h] = len(date_sets[h])
        world.frequencies[h] = in_scope_flagged[h] / (world.config.n_symbols * T)
        world.oracle_difference[h] = (
            cond_sum[h] / cond_n[h] - base_mean if cond_n[h] else float("nan")
        )
