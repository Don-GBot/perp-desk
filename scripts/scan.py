#!/usr/bin/env python3
"""Setup scanner. The ONLY source of trade setups. Public endpoints only, no API keys needed.

Rules (all values from config.yaml -> scan / entry / fees):
  - Universe: USDT perps of any asset class, state "live", no scheduled offTime (stock perps are blocked later by
    execute.py's stock-perp gate; commodities and forex are allowed)
    (delisting), ranked by 24h quote volume; top universe.top_n() (`universe_size`, or `universe_ext_size` when
    `universe_ext_enabled`), then drop spread > `max_spread_pct`. The volume rank (before the spread filter) is stored
    per scan: scan.json 'vol_ranks', and each setup's 'vol_rank' and 'universe_tier' ('core' | 'ext', universe.py).
  - Only CLOSED candles (confirm == "1") are used for trend, ATR, swing and target.
  - Trend: 4H EMA fast/slow. Up = fast > slow and close > slow. Down = mirror. Flat pairs are skipped.
  - Long: entry = ask. Stop = lower of (lowest low of last N 1H candles - buffer x ATR) and
    (entry - min_stop_atr x ATR). Target = highest high of the last M 4H candles. Short is the mirror.
  - R:R is price-only (gross, see rr): (target - entry) / (entry - stop), mirrored for shorts. Keep setups with
    R:R >= min_rr, best first. Fees are covered by sizing (risk incl. fees + slippage), not by the R:R filter.

Writes DATA_DIR/scan.json (atomic) and prints it.
  python3 scripts/scan.py
"""
import os, sys, json, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config
import universe as UN

C = config.CFG; S = C["scan"]; E = C["entry"]; FEE = float(C["fees"]["taker_rate"])

def ema(v, n):
    k = 2 / (n + 1); e = v[0]; out = [e]
    for x in v[1:]: e = x * k + e * (1 - k); out.append(e)
    return out

def atr(c, n=None):
    n = n or S["atr_period"]
    if len(c) <= n: raise ValueError(f"not enough candles for ATR{n}: {len(c)}")
    tr = [c[0][1] - c[0][2]]
    for i in range(1, len(c)):
        h, l, pc = c[i][1], c[i][2], c[i - 1][3]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[:n]) / n
    for x in tr[n:]: a = (a * (n - 1) + x) / n
    return a

def candles(inst, bar, limit=None):
    """Closed candles only (confirm == "1"), as [o, h, l, c], oldest first."""
    r = bf.public('/api/v1/market/candles', {'instId': inst, 'bar': bar, 'limit': limit or S["candles_limit"]})
    if str(r.get('code')) != '0': raise RuntimeError(f"candles {inst} {bar}: code {r.get('code')} {r.get('msg')}")
    return [[float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in reversed(r['data']) if str(x[8]) == '1']

def trend(closes):
    tf = E["trend_filter"]
    e1 = ema(closes, tf["ema_fast"])[-1]; e2 = ema(closes, tf["ema_slow"])[-1]; p = closes[-1]
    t = 'up' if (e1 > e2 and p > e2) else 'down' if (e1 < e2 and p < e2) else 'flat'
    return t, e1, e2, p

def rr(side, entry, stop, target):
    """Price-only reward:risk, the min_rr filter: (target - entry) / (entry - stop) for longs, mirrored for shorts."""
    risk = (entry - stop) if side == 'buy' else (stop - entry); reward = (target - entry) if side == 'buy' else (entry - target)
    return reward / risk if risk > 0 else 0.0

def net_rr(side, entry, stop, target, fee=FEE):
    """Reward:risk after round-trip taker fees (entry fee + exit fee at target or stop)."""
    if side == 'buy': risk = entry - stop; reward = target - entry
    else: risk = stop - entry; reward = entry - target
    risk_net = risk + fee * (entry + stop); reward_net = reward - fee * (entry + target)
    return reward_net / risk_net if risk > 0 and risk_net > 0 else 0.0

def tradable_instruments():
    """instId -> instrument for live USDT perps (any asset class) with no scheduled offTime."""
    r = bf.public('/api/v1/market/instruments')
    if str(r.get('code')) != '0': raise RuntimeError(f"instruments: code {r.get('code')} {r.get('msg')}")
    return {x['instId']: x for x in r['data']
            if x.get('state') == 'live' and not x.get('offTime')
            and x['instId'].endswith('-USDT')}

def universe(top=None, max_spread_pct=None):
    """Top N (default universe.top_n()) tradable USDT perps by 24h quote volume, then drop wide spreads.
    Returns (rows, tickers_by_inst); rows = [(quote_vol, instId, last, bid, ask, spread_frac)]."""
    rows, t, _ = universe_ranked(top, max_spread_pct)
    return rows, t

def universe_ranked(top=None, max_spread_pct=None):
    """Like universe() plus {instId: volume rank} over the top N before the spread filter."""
    top = top or UN.top_n()
    max_spread = (S["max_spread_pct"] if max_spread_pct is None else max_spread_pct) / 100.0
    ok = tradable_instruments()
    t = bf.public('/api/v1/market/tickers')['data']
    rows = []
    for x in t:
        if x['instId'] not in ok: continue
        last = float(x['last'] or 0); bid = float(x['bidPrice'] or 0); ask = float(x['askPrice'] or 0)
        if last <= 0 or bid <= 0 or ask <= 0: continue
        qv = float(x['volCurrency24h'] or 0) * last
        rows.append((qv, x['instId'], last, bid, ask, (ask - bid) / last))
    rows.sort(reverse=True)
    ranks = {r[1]: k for k, r in enumerate(rows[:top], 1)}
    return [r for r in rows[:top] if r[5] <= max_spread], {x['instId']: x for x in t}, ranks

def geometry(inst, bid, ask, h1, h4):
    """The scan's setup for one pair from bid/ask and closed 1H / trend-timeframe rows. Returns (setup, None) or
    (None, note)."""
    tf = E["trend_filter"]; k_atr = E["min_stop_atr"]
    nsw = S["swing_lookback_1h"]; buf = S["swing_buffer_atr"]; ntg = S["target_lookback_4h"]
    try:
        a1 = atr(h1)
        if len(h4) < tf["ema_slow"] + 1 or len(h1) < nsw or len(h4) < ntg: raise ValueError("not enough closed candles")
        tr, e1, e2, _ = trend([x[3] for x in h4])
    except Exception as e:
        return None, f'{inst}: data error {str(e)[:120]}'
    if not tf["enabled"]:
        tr = 'up' if e1 >= e2 else 'down'  # filter off: side from EMA order only, no flat skip
    if tr == 'flat': return None, f'{inst}: {tf["timeframe"]} flat'
    if tr == 'up':
        entry = ask
        swing = min(x[2] for x in h1[-nsw:])
        stop = min(swing - buf * a1, entry - k_atr * a1)
        target = max(x[1] for x in h4[-ntg:])
        risk = entry - stop; side = 'buy'
    else:
        entry = bid
        swing = max(x[1] for x in h1[-nsw:])
        stop = max(swing + buf * a1, entry + k_atr * a1)
        target = min(x[2] for x in h4[-ntg:])
        risk = stop - entry; side = 'sell'
    r_ = rr(side, entry, stop, target)
    return dict(instId=inst, side=side, trend4h=tr, entry=entry, stop=stop, target=target, atr1h=a1,
                stop_atr=risk / a1 if a1 else None, rr=round(r_, 2), rr_net=round(net_rr(side, entry, stop, target), 2)), None

def scan(top=None):
    top = top or UN.top_n()
    liquid, _, ranks = universe_ranked(top)
    tf = E["trend_filter"]; min_rr = E["min_rr"]
    setups = []; notes = []
    for qv, inst, last, bid, ask, spread in liquid:
        try: h1 = candles(inst, '1H'); h4 = candles(inst, tf["timeframe"])
        except Exception as e:
            notes.append(f'{inst}: data error {str(e)[:120]}'); continue
        s, note = geometry(inst, bid, ask, h1, h4)
        if s is None: notes.append(note); continue
        s.update(vol_usd=round(qv), spread=spread, vol_rank=ranks[inst], universe_tier=UN.tier(ranks[inst]))
        r_ = rr(s['side'], s['entry'], s['stop'], s['target'])
        if r_ >= min_rr: setups.append(s)
        else: notes.append(f"{inst}: {s['side']} R:R {r_:.2f} <{min_rr:g}")
    setups.sort(key=lambda s: -s['rr'])
    return dict(ts=dt.datetime.now(dt.timezone.utc).isoformat(), fee_rate=FEE, universe=[r[1] for r in liquid],
                setups=setups, rejected=notes, top_n=top, vol_ranks=ranks, universe_tiers={i: UN.tier(k) for i, k in ranks.items()})

def write_atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)

if __name__ == '__main__':
    r = scan(); write_atomic(config.data_path('scan.json'), r); print(json.dumps(r, indent=1))
