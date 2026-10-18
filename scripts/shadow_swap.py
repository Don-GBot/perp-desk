#!/usr/bin/env python3
"""SHADOW-MODE swap logger. STRICTLY READ-ONLY. No LLM. Run it from plain cron.

Question it answers: "Would closing an open trade early to open a better new setup have paid off?"
It only logs hypothetical decisions. It never closes, opens, cancels or changes anything.
Only GET endpoints are called (positions, balance, tpsl-pending, tickers, instruments, candles).

  python3 scripts/shadow_swap.py --once    # one cycle, then exit (use this from cron)
  python3 scripts/shadow_swap.py           # built-in scheduler, cycles at shadow_swap.slots minutes

Needs BLOFIN_API_KEY / BLOFIN_API_SECRET / BLOFIN_PASSPHRASE (read-only use) and exits if they are missing.

Output: DATA_DIR/logs/shadow_swap.jsonl (one JSON record per decision) and
DATA_DIR/logs/shadow_swap.log (human), each rotating at 5 MB x 3. Each cycle ends with a SUMMARY record.

Assumptions:
 - Fees: taker rate fees.taker_rate per side (config). Closing an open trade costs one
   taker fee on exit notional. Candidate EV = risk_usdt x net R:R (round-trip fees included).
 - size_coin = |contracts| * contractValue.
 - Remaining profit (old) = |target - mark| * size_coin - exit fee (0 if price is beyond target).
 - Old R:R = remaining reward / remaining distance to stop.
 - Standard rule (old stop not yet at break-even):
     PASS iff candEV >= (remaining profit + close fee) * hurdle_mult AND candRR >= oldRR + rr_edge.
 - Break-even rule (old stop at/through entry, R:R undefined):
     PASS iff candEV >= remaining profit * hurdle_mult (R:R rule skipped). Record field rule="BE_EV_ONLY".
 - Price at/through a non-BE stop (risk ~0): not swappable ("old_rr_inf").
 - Candidate R:R is net of round-trip fees (scan.net_rr), ask for longs, bid for shorts.
 - risk_usdt = risk_per_trade_pct of equity (same as execute.py).

Entry filters: a candidate is only scored if it passes the SAME filters as execute.py (helpers
imported from execute.py / scan.py; importing never triggers a trade because both guard their
run code with __name__ == "__main__"):
  1. age <= max_age_min (entry ts, else scan.json ts, else file mtime), else FILTERED "STALE"
  2. stop < entry < target (mirrored for shorts) and net R:R >= min_rr, stop/target rounded to tick
  3. stop distance >= min_stop_atr * atr_tolerance * 1H ATR
  4. 4H trend aligned (scan.py rule)
  5. size >= minSize and a margin fit (leverage range, margin cap incl. margin freed by closing the
     old trade, liquidation well beyond the stop).
  Failures are logged once per cycle as FILTERED. Margin fit is re-checked per pair.
"""
import os, sys, json, time, math, logging, argparse, datetime as dt
from logging.handlers import RotatingFileHandler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config

def _load_json(path):
    with open(path) as fh:
        return json.load(fh)
import execute as EX          # safe: execute.py only acts under __main__; we use pure helpers only
from scan import candles, atr, trend, net_rr

SC = config.CFG["shadow_swap"]
D = config.DATA_DIR; L = config.data_path("logs")
FEE = EX.FEE; RISK = EX.RISK
HURDLE_MULT = float(SC["hurdle_mult"]); RR_EDGE = float(SC["rr_edge"])
SLOTS = tuple(int(x) for x in SC["slots"])
MAX_AGE_MIN = float(SC["max_age_min"]); ATR_MIN = EX.ATR_FLOOR
os.makedirs(L, exist_ok=True)

def _logger(name, fn, fmt):
    lg = logging.getLogger(name); lg.setLevel(logging.INFO); lg.propagate = False
    if not lg.handlers:
        h = RotatingFileHandler(os.path.join(L, fn), maxBytes=5_000_000, backupCount=3)
        fm = logging.Formatter(fmt); fm.converter = time.gmtime; h.setFormatter(fm); lg.addHandler(h)
    return lg
JL = _logger("jsonl", "shadow_swap.jsonl", "%(message)s")
HL = _logger("human", "shadow_swap.log", "%(asctime)sZ %(message)s")

def get(fn, *a, tries=4, **k):
    for i in range(tries):
        try:
            r = fn(*a, **k)
            if str(r.get("code")) == "0": return r["data"]
            err = f"code {r.get('code')} {r.get('msg')}"
        except Exception as e: err = str(e)[:200]
        HL.info(f"API error ({err}); retry {i+1}/{tries}"); time.sleep(min(60, 5 * 2 ** i))
    raise RuntimeError(f"API failed after {tries} tries: {err}")
pub = lambda p, q=None: get(bf.public, p, q)
priv = lambda p, q=None: get(bf.private, "GET", p, q)   # GET ONLY

def f(x, d=None):
    try: return float(x)
    except Exception: return d

def _ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None

def load_cands():
    out = []
    for fn in ("scan.json",):
        p = os.path.join(D, fn)
        if os.path.exists(p):
            try:
                j = _load_json(p); s = j.get("setups", j) if isinstance(j, dict) else j
                fts = _ts(j.get("ts")) if isinstance(j, dict) else None
                mts = dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc)
                for c in s or []:
                    c = dict(c); c["_src"] = fn
                    et = _ts(c.get("ts")) or fts
                    c["_ts"] = (et or mts).isoformat(); c["_ts_src"] = "entry" if _ts(c.get("ts")) else ("file_ts" if fts else "mtime")
                    out.append(c)
            except Exception as e: HL.info(f"cannot read {fn}: {e}")
    return out

def trend4h(inst):
    return trend([x[3] for x in candles(inst, config.CFG["entry"]["trend_filter"]["timeframe"])])

def margin_fit(c, equity, free, freed):
    """execute.run sizing/leverage/liq logic; cap = min(25% equity, 95% free + freed)."""
    avail_eff = free + freed / EX.FREE_BUF
    lev, cap = EX.pick_lev(c["_notional"], equity, avail_eff, c["_maxlev"])
    m = dict(freed=round(freed, 4), cap=round(cap, 4), notional=round(c["_notional"], 4), size=c["_size"])
    if lev is None:
        return False, dict(m, ok=False, why=f"no leverage {EX.LEVMIN}-{int(min(EX.LEVMAX, c['_maxlev']))}x fits: margin@max {c['_notional']/min(EX.LEVMAX, c['_maxlev']):.2f} > cap {cap:.2f}")
    liq = EX.est_liq(c["_entry"], c["_eside"], lev, c["_mmr"]); ld = abs(liq - c["_entry"])
    m.update(lev=lev, margin=round(c["_notional"] / lev, 4), liq_est=liq, liq_dist=ld, stop_dist=c["_risk"],
             liq_mult=round(ld / c["_risk"], 3) if c["_risk"] else None)
    if c["_risk"] >= EX.MAX_STOP_TO_LIQ * ld:
        return False, dict(m, ok=False, why=f"liq {liq:.8g} too close at {lev}x: stop dist {c['_risk']:.8g} >= {EX.MAX_STOP_TO_LIQ:g} x liq dist {ld:.8g}")
    return True, dict(m, ok=True, why="fits")

def entry_filters(c, ins, tk, equity, risk_usdt, now_dt):
    """Filters 1-4 + sizing/min-size; returns (cand_dict_or_None, filters, reason)."""
    i = c["instId"]; side = "long" if c["side"] in ("buy", "long") else "short"; eside = "buy" if side == "long" else "sell"
    flt = dict(passed=False, failed=[])
    age = (now_dt - dt.datetime.fromisoformat(c["_ts"])).total_seconds() / 60
    flt.update(age_min=round(age, 1), age_src=c["_ts_src"], age_ok=age <= MAX_AGE_MIN)
    base = dict(instId=i, side=side, stop=f(c.get("stop")), target=f(c.get("target")), src=c["_src"], scan_ts=c["_ts"])
    if not flt["age_ok"]:
        flt["failed"].append("STALE")
        return base, flt, f"STALE: scan entry age {age:.1f} min > {MAX_AGE_MIN:.0f}"
    meta = ins.get(i)
    if not meta: flt["failed"].append("instrument"); return base, flt, "instrument not found"
    tick = meta["tickSize"]; lot = meta["lotSize"]; cv = f(meta["contractValue"]); mins = f(meta["minSize"])
    t = tk(i); px = f(t["askPrice"]) if side == "long" else f(t["bidPrice"]); sgn = 1 if side == "long" else -1
    stop = float(EX.q(c["stop"], tick)); target = float(EX.q(c["target"], tick))
    rd = sgn * (px - stop); rw = sgn * (target - px)
    geo = EX.check_geometry(eside, px, stop, target)
    rr = net_rr(eside, px, stop, target) if not geo else 0.0
    a1 = atr(candles(i, "1H")); tr, e20, e50, p4 = trend4h(i)
    want = "up" if side == "long" else "down"
    flt.update(live=px, stop_r=stop, target_r=target, rr_live=round(rr, 3), rr_ok=rr >= EX.MINRR,
               atr1h=a1, stop_dist=rd, stop_atr=round(rd / a1, 3) if a1 else None, atr_ok=bool(a1) and rd >= ATR_MIN * a1,
               trend4h=tr, trend_need=want, trend_ok=tr == want, ema20_4h=e20, ema50_4h=e50, close_4h=p4)
    why = []
    if geo: flt["failed"].append("GEOMETRY"); why.append(geo)
    elif not flt["rr_ok"]: flt["failed"].append("RR"); why.append(f"net R:R {rr:.2f} < {EX.MINRR:g}")
    if not flt["atr_ok"]: flt["failed"].append("ATR"); why.append(f"stop {rd/a1 if a1 else 0:.2f}x 1H ATR < {ATR_MIN}")
    if not flt["trend_ok"]: flt["failed"].append("TREND"); why.append(f"4H trend {tr} != {want}")
    size = EX.size_for(equity, px, stop, cv, lot) if rd > 0 else 0.0
    flt.update(size=size, min_size=mins)
    if size < mins: flt["failed"].append("MINSIZE"); why.append(f"size {size:g} < min {mins:g}")
    notional = risk_usdt / rd * px if rd > 0 else 0.0
    ev = risk_usdt * rr   # net R:R already includes round-trip taker fees
    cd = dict(base, live=px, stop=stop, target=target, rr_live=round(rr, 3), ev=round(ev, 4), open_fee=round(notional * FEE, 4),
              _entry=px, _eside=eside, _risk=rd, _notional=size * cv * px, _size=size,
              _maxlev=f(meta.get("maxLeverage")) or EX.LEVMAX, _mmr=f(meta.get("maintenanceMarginRate")) or EX.MMR_DEFAULT)
    return cd, flt, "; ".join(why)

def pub_c(c): return {k: v for k, v in c.items() if not k.startswith("_")}

def cycle():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ins = {i["instId"]: i for i in pub("/api/v1/market/instruments")}
    tpsl = {}
    bal = priv("/api/v1/account/balance")
    usdt = [d for d in bal.get("details", []) if d.get("currency") == "USDT"]
    equity = f(usdt[0]["equity"]) if usdt else f(bal.get("totalEquity"))
    pos = [p for p in priv("/api/v1/account/positions") if f(p.get("positions"), 0) != 0]
    for p in pos:   # one request per open pair, so a long pending list can't be cut off at the page size
        lst = priv("/api/v1/trade/orders-tpsl-pending", {"instId": p["instId"], "limit": 100}) or []
        withsl = [t for t in lst if f(t.get("slTriggerPrice"))]
        if lst: tpsl[p["instId"]] = (withsl or lst)[0]
    risk_usdt = RISK * equity
    try: st = _load_json(os.path.join(D, "state.json")).get("positions", {})
    except Exception: st = {}
    tick = {}
    def tk(i):
        if i not in tick: tick[i] = pub("/api/v1/market/tickers", {"instId": i})[0]
        return tick[i]
    opens = []
    for p in pos:
        i = p["instId"]; c = f(p["positions"]); side = "long" if c > 0 else "short"
        cv = f(ins.get(i, {}).get("contractValue"), 1.0); coin = abs(c) * cv
        mark = f(p.get("markPrice")); entry = f(p.get("averagePrice"))
        t = tpsl.get(i, {}); s = st.get(i, {})
        stop = f(t.get("slTriggerPrice")) or f(s.get("current_stop")) or f(s.get("initial_stop"))
        tgt = f(t.get("tpTriggerPrice")) or f(s.get("target"))
        src = "api" if t else ("state.json" if s else "none")
        sgn = 1 if side == "long" else -1
        exit_fee = coin * mark * FEE
        rew = max(0.0, sgn * (tgt - mark)) if tgt else 0.0
        rem = rew * coin - exit_fee
        risk_d = sgn * (mark - stop) if stop else None
        be = stop is not None and sgn * (stop - entry) >= 0
        if risk_d is None: rr = None
        elif be or risk_d <= mark * 1e-6: rr = math.inf
        else: rr = rew / risk_d
        o = dict(instId=i, side=side, contracts=c, ctVal=cv, size_coin=coin, entry=entry, mark=mark,
                 stop=stop, target=tgt, tpsl_src=src, margin=f(p.get("margin")) or f(p.get("initialMargin")) or (coin*mark/f(p.get("leverage"),1)), upnl=f(p.get("unrealizedPnl")),
                 stop_at_or_past_be=be, rr_live=rr, remaining_profit=round(rem, 4), close_fee=round(exit_fee, 4))
        opens.append(o)
        HL.info(f"OPEN {i} {side} {coin:g}coin entry {entry:g} mark {mark:g} SL {stop} TP {tgt} ({src}) "
                f"uPnL {(o['upnl'] or 0):.2f} margin {o['margin']:.2f} remain {rem:.2f} RR {rr}")
    openset = {o["instId"] for o in opens}
    now_dt = dt.datetime.fromisoformat(now)
    free = f(usdt[0].get("available"), 0.0) if usdt else 0.0
    max_freed = max([o["margin"] or 0.0 for o in opens] or [0.0])
    def emit(rec):
        rec = dict(ts=now, equity=round(equity, 2), risk_usdt=round(risk_usdt, 2), fee_rate=FEE, **rec)
        JL.info(json.dumps(rec, default=lambda x: str(x)))
        HL.info(f"{rec['decision']} old={rec.get('open_trade',{}).get('instId') if rec.get('open_trade') else '-'} "
                f"cand={rec.get('candidate',{}).get('instId') if rec.get('candidate') else '-'} "
                f"oldRR={rec.get('old_rr')} candRR={rec.get('cand_rr')} oldEV={rec.get('old_ev')} "
                f"candEV={rec.get('cand_ev')} hurdle={rec.get('hurdle')} :: {rec['reason']}")
    cands, seen, nseen, nfilt = [], set(), 0, 0
    for c in load_cands():
        i = c.get("instId")
        if not i or i in openset or i in seen:
            continue
        seen.add(i); nseen += 1
        try:
            cd, flt, why = entry_filters(c, ins, tk, equity, risk_usdt, now_dt)
            if not why:
                ok0, m0 = margin_fit(cd, equity, free, 0.0)
                okx, mx = (ok0, m0) if ok0 else margin_fit(cd, equity, free, max_freed)
                flt["margin_no_swap"] = m0; flt["margin_best_swap"] = mx
                if not okx: flt["failed"].append("MARGIN"); why = f"margin fit fails even with max freed {max_freed:.2f}: {mx['why']}"
        except Exception as e:
            cd = dict(instId=i, src=c.get("_src")); flt = dict(passed=False, failed=["ERROR"]); why = f"filter error: {str(e)[:200]}"
        if why:
            nfilt += 1
            emit(dict(decision="FILTERED", candidate=pub_c(cd), cand_rr=cd.get("rr_live"), cand_ev=cd.get("ev"), filters=flt, reason=why))
        else:
            flt["passed"] = True; cd["_filters"] = flt; cands.append(cd)
    nev = npass = 0
    def summary():
        msg = (f"SUMMARY open={len(opens)} candidates_seen={nseen} filtered={nfilt} filtered_in={len(cands)} "
               f"evaluated={nev} passes={npass}")
        JL.info(json.dumps(dict(ts=now, decision="SUMMARY", open=len(opens), candidates_seen=nseen, filtered=nfilt,
                                filtered_in=len(cands), evaluated=nev, passes=npass)))
        HL.info(msg)
    if not cands:
        emit(dict(decision="NO_CANDIDATES", reason=f"no filtered-in non-duplicate scanner candidates (open: {sorted(openset)}; filtered {nfilt})",
                  open_trades=opens, filters=None)); summary(); return
    if not opens:
        for c in cands:
            ok, m = margin_fit(c, equity, free, 0.0)
            emit(dict(decision="NO_OPEN_TRADES", candidate=pub_c(c), cand_rr=c["rr_live"], cand_ev=c["ev"],
                      filters=dict(c["_filters"], margin=m), reason="nothing to swap"))
        summary(); return
    for o in opens:
        for c in cands:
            nev += 1
            hurdle = (o["remaining_profit"] + o["close_fee"]) * HURDLE_MULT
            orr = o["rr_live"]
            mok, m = margin_fit(c, equity, free, o["margin"] or 0.0)
            flt = dict(c["_filters"], margin=m)
            is_be = bool(o["stop_at_or_past_be"]) and orr is not None and math.isinf(orr)
            if is_be:   # BE positions are swappable on EV only (R:R undefined)
                hurdle = o["remaining_profit"] * HURDLE_MULT   # (remaining reward USDT - close fee) * 1.2
            rule = "BE_EV_ONLY" if is_be else "STANDARD"
            if orr is None: ok, why = False, "old stop unknown; non-swappable"
            elif not mok: ok, why = False, (f"BE position: EV-only rule; " if is_be else "") + f"margin fit fails for this swap (freed {m['freed']:.2f}): {m['why']}"
            elif is_be:
                ok = c["ev"] >= hurdle
                why = (f"BE position: EV-only rule; candEV {c['ev']:.2f} >= hurdle {hurdle:.2f} (RR rule skipped)" if ok else
                       f"BE position: EV-only rule; candEV {c['ev']:.2f} < hurdle {hurdle:.2f} (RR rule skipped)")
            elif math.isinf(orr): ok, why = False, "old_rr_inf: price at/through stop (risk~0), stop not at BE; non-swappable"
            else:
                need = orr + RR_EDGE; e1 = c["ev"] >= hurdle; e2 = c["rr_live"] >= need
                ok = e1 and e2
                why = (f"EV>=hurdle and RR>=oldRR+{RR_EDGE:g}" if ok else
                       "; ".join(x for x in [None if e1 else f"candEV {c['ev']:.2f} < hurdle {hurdle:.2f}",
                                             None if e2 else f"candRR {c['rr_live']:.2f} < {need:.2f}"] if x))
            npass += ok
            emit(dict(decision="PASS" if ok else "FAIL", open_trade=o, candidate=pub_c(c),
                      old_rr=("inf" if orr is not None and math.isinf(orr) else (round(orr, 3) if orr is not None else None)),
                      cand_rr=c["rr_live"], old_ev=o["remaining_profit"], cand_ev=c["ev"], hurdle=round(hurdle, 4),
                      rule=rule, filters=flt, reason=why))
    summary()

def next_slot(now):
    base = now.replace(second=0, microsecond=0)
    for h in range(0, 2):
        for m in SLOTS:
            t = base.replace(minute=m) + dt.timedelta(hours=h)
            if t > now: return t

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); a = ap.parse_args()
    bf.require_keys("shadow_swap.py")
    if a.once:
        cycle(); return
    open(os.path.join(L, "shadow_swap.pid"), "w").write(str(os.getpid()))
    HL.info(f"scheduler start pid {os.getpid()} slots {SLOTS} UTC")
    while True:
        nxt = next_slot(dt.datetime.now(dt.timezone.utc))
        HL.info(f"next cycle {nxt.isoformat()}")
        while (s := (nxt - dt.datetime.now(dt.timezone.utc)).total_seconds()) > 0: time.sleep(min(s, 60))
        try: cycle()
        except Exception as e: HL.info(f"cycle error: {str(e)[:300]}"); time.sleep(30)

if __name__ == "__main__": main()
