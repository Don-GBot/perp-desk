"""Pure helpers for the shadow books V_SLOWTREND and V_CROWD (scripts/shadow_variants.py). No orders, no I/O.
tsmom_z = time-series momentum z-score:
    z = mean over k in (168, 720) of  log(C_t / C_{t-k}) / (std_720(log C_i/C_{i-1}) * sqrt(k))     (1H closes; pandas std, ddof=1)
  (blends 7d/30d = 168h/720h); signal when |z| > shadow_variants.slowtrend_z_min (default 1).
crowd: per pair, 3-day average funding rate (fundingTime within the last 72h)."""
import math
KS = (168, 720); STD_N = 720; Z_MIN = 1.0; NEED = max(KS) + STD_N + 1
def tsmom_z(closes):
    """closes oldest first. Raises ValueError if fewer than NEED closes or bad data."""
    c = [float(x) for x in closes]
    if len(c) < NEED: raise ValueError(f'tsmom: {len(c)} 1H closes < {NEED}')
    if min(c[-NEED:]) <= 0: raise ValueError('tsmom: non-positive close')
    lr = [math.log(c[i] / c[i - 1]) for i in range(len(c) - STD_N, len(c))]
    m = sum(lr) / STD_N; sd = math.sqrt(sum((x - m) ** 2 for x in lr) / (STD_N - 1))
    if not sd > 0: raise ValueError('tsmom: zero volatility')
    return sum(math.log(c[-1] / c[-1 - k]) / (sd * math.sqrt(k)) for k in KS) / len(KS)
def aligned(z, side): return z if side == 'buy' else -z
def avg_funding(rows, now_ms, hours=72):
    """rows = Blofin funding-rate-history data. Average fundingRate with now-72h < fundingTime <= now. Raises if none."""
    xs = [float(r['fundingRate']) for r in rows or [] if now_ms - hours * 3_600_000 < int(r['fundingTime']) <= now_ms]
    if not xs: raise ValueError('no funding records in the last 72h')
    return sum(xs) / len(xs)
def crowd_sets(avgs, n=2):
    """avgs: pair -> 3d avg funding. Returns (no_long = n highest, no_short = n lowest)."""
    s = sorted(avgs.items(), key=lambda kv: (kv[1], kv[0]))
    return {k for k, _ in s[-n:]}, {k for k, _ in s[:n]}
def funding_paid(rows, side, notional, t0_ms, t1_ms):
    """Estimated funding PAID (USDT, + = paid, - = received) over (t0, t1]: sum(rate) * notional, longs pay positive rates."""
    r = sum(float(x['fundingRate']) for x in rows or [] if t0_ms < int(x['fundingTime']) <= t1_ms)
    return round((1 if side == 'buy' else -1) * r * notional, 6)
