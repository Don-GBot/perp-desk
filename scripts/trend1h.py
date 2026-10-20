"""1H trend gate: ONE definition shared by execute.py (live gate), shadow_variants.py and shadow_swap.py.

On the last entry.trend_1h.bars closed candles of entry.trend_1h.bar (default 200 x 1H):
up = EMA fast > EMA slow and close > EMA slow; down = EMA fast < EMA slow and close < EMA slow; else flat.
Longs need up, shorts need down; flat blocks both. Too little data raises (callers fail closed).
"""
import config
from scan import ema

T = config.CFG["entry"]["trend_1h"]
ENABLED = bool(T["enabled"]); BAR = T["bar"]; BARS = int(T["bars"]); FAST = int(T["ema_fast"]); SLOW = int(T["ema_slow"])
MIN_BARS = SLOW

def trend_from_rows(rows):
    """rows = candles [o, h, l, c, ...], oldest first. Returns (trend, ema_fast, ema_slow, close). Raises ValueError on too little data."""
    cl = [float(x[3]) for x in (rows or [])]
    if len(cl) < MIN_BARS: raise ValueError(f'{BAR} data: {len(cl)} bars < {MIN_BARS}')
    ef = ema(cl, FAST)[-1]; es = ema(cl, SLOW)[-1]; p = cl[-1]
    t = 'up' if (ef > es and p > es) else 'down' if (ef < es and p < es) else 'flat'
    return t, ef, es, p

def want(side): return 'up' if side in ('buy', 'long') else 'down'
