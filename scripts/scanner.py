#!/usr/bin/env python3
"""OPTIONAL alerts scanner: big moves, new pairs in the universe, coils, and INFO-ONLY 15m flags.
It never finds setups and never trades. Public endpoints only, no API keys needed.

By default the executor runs this as step 0 of each run, so no separate scanner agent is needed.
Optionally, run it standalone (cron or its own agent) INSTEAD, never both: the dedupe state is shared.

  python3 scripts/scanner.py

Universe: exactly the executor's (scan.universe(): top universe.top_n() by 24h quote volume, i.e. universe_ext_size when
scan.universe_ext_enabled, else universe_size; drop wide spreads).
Alerts (config.yaml -> scanner):
  - Move: |change from 24h open| >= move_alert_pct. Re-alert only on a direction flip or a further
    same-direction move of >= max(realert_min_pts, realert_frac x last reported move). Memory: memory_hours.
  - New in universe: pair not seen in the universe within memory_hours.
  - Coils: 12 x 1H range / median of prior 12-candle ranges < coil_ratio. Top coil_top, new once per memory_hours.
Flags (INFO ONLY, NOT setups, NOT trade triggers): last 2 CLOSED 15m candles per pair (quote vol >=
flag_min_quote_vol_usd): unusual_volume (>=2x avg of prior 20), volume_spike (>=3x), breakout / breakdown,
strong_move, with a strong-close + no-chase quality gate. Deduped on (pair, type, candle_close_time).

Files (DATA_DIR):
  flags_latest.json     latest flags snapshot (atomic write); the executor reads it for info only
  scanner_state.json    alert memory / dedupe
  scanner_log.jsonl     one JSON line per run
Prints a short JSON summary to stdout.

Run guards:
  - One pass at a time: DATA_DIR/scanner.lock is held (flock) until the process exits. If another pass holds it, the
    script prints "SKIP: locked" and exits 0 without writing anything.
  - A full pass within the last 5 min (scanner_state.json last_run) -> "SKIP: ran Ns ago", exit 0 (harnesses double-run).
  - If EVERY flag check errored (e.g. exchange down), flags_latest.json is kept as is, the summary carries scanner_error,
    and last_run is not advanced, so the next run retries at once. A dedup-only empty pass also keeps the previous flags.
    A genuine pass with zero flags still writes [].
"""
import os, sys, json, fcntl, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config

def _load_json(path):
    with open(path) as fh:
        return json.load(fh)
import scan as executor_scan   # side-effect free on import

SC = config.CFG["scanner"]
STATE = config.data_path('scanner_state.json'); LOG = config.data_path('scanner_log.jsonl')
FLAGS_LATEST = config.data_path('flags_latest.json')
def main():
    """One scanner pass. The run lock and the 5-min skip live here, so importing this module has no side effects."""
    MIN_GAP_S = 300
    _lock_fd = os.open(config.data_path('scanner.lock'), os.O_CREAT | os.O_RDWR, 0o644)
    try: fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: print('SKIP: locked'); sys.exit(0)
    st = _load_json(STATE) if os.path.exists(STATE) else {}
    now = dt.datetime.now(dt.timezone.utc)
    try: _last = dt.datetime.fromisoformat(str(st.get('last_run')).replace('Z', '+00:00'))
    except Exception: _last = None
    if _last is not None:
        if _last.tzinfo is None: _last = _last.replace(tzinfo=dt.timezone.utc)
        _age = (now - _last).total_seconds()
        if 0 <= _age < MIN_GAP_S: print(f'SKIP: ran {int(_age)}s ago (<5 min)'); sys.exit(0)
    now_iso = now.isoformat()
    cutoff = (now - dt.timedelta(hours=SC["memory_hours"])).isoformat()
    errors = []

    # ---- PAIR UNIVERSE: identical to the executor's ----
    liquid, tick = executor_scan.universe()
    top = []
    for _qv, i, *_ in liquid:
        x = tick[i]; last = float(x['last'] or 0); op = float(x['open24h'] or 0)
        top.append(dict(inst=i, qv=float(x['volCurrency24h'] or 0) * last, last=last, chg=(last / op - 1) * 100 if op > 0 else None))
        if op <= 0: errors.append(dict(pair=i, error='open24h missing; move check skipped'))

    # ---- Move alerts ----
    mv2 = {k: v for k, v in st.get('moves_v2', {}).items() if v['ts'] >= cutoff}
    moves = []
    for r in top:
        i = r['inst']; c = r['chg']
        if c is None or abs(c) < SC["move_alert_pct"]: continue
        prev = mv2.get(i)
        need = max(SC["realert_min_pts"], SC["realert_frac"] * abs(prev['pct'])) if prev else 0
        if prev is None or (prev['pct'] * c < 0) or (c > 0 and c - prev['pct'] >= need) or (c < 0 and prev['pct'] - c >= need):
            moves.append(dict(inst=i, chg=round(c, 2), last=r['last'])); mv2[i] = dict(pct=c, ts=now_iso)

    # ---- New in universe (not on the very first run) ----
    seen = {k: v for k, v in st.get('universe_seen', {}).items() if v >= cutoff}
    first_run = 'universe_seen' not in st
    newtop = []
    for r in top:
        if r['inst'] not in seen and not first_run: newtop.append(r['inst'])
        seen[r['inst']] = now_iso

    # ---- Coils (1H range compression) ----
    coils = []
    for r in top:
        i = r['inst']
        try: h1 = executor_scan.candles(i, '1H', 200)
        except Exception as e: errors.append(dict(pair=i, error='coil candles: ' + str(e)[:200])); continue
        rng = [max(x[1] for x in h1[k - 12:k]) - min(x[2] for x in h1[k - 12:k]) for k in range(12, len(h1) + 1)]
        med = sorted(rng[:-1])[len(rng[:-1]) // 2]; ratio = rng[-1] / med if med else 9
        if ratio < SC["coil_ratio"]:
            hi = max(x[1] for x in h1[-12:]); lo = min(x[2] for x in h1[-12:])
            coils.append(dict(inst=i, ratio=round(ratio, 2), hi=hi, lo=lo))
    coils.sort(key=lambda c: c['ratio']); coils = coils[:SC["coil_top"]]
    cseen = {k: v for k, v in st.get('coils_seen', {}).items() if v >= cutoff}
    new_coils = [c for c in coils if c['inst'] not in cseen]
    for c in new_coils: cseen[c['inst']] = now_iso

    # ---- INFO-ONLY FLAGS (closed candles only). NOT setups, NOT trade triggers. ----
    def _f_ema(v, n):  # SMA-seeded EMA
        k = 2 / (n + 1); e = sum(v[:n]) / n
        for x in v[n:]: e = x * k + e * (1 - k)
        return e
    def _f_atr(c, n=14):  # Wilder ATR14 on o,h,l,c rows
        tr = [c[0][1] - c[0][2]] + [max(c[i][1] - c[i][2], abs(c[i][1] - c[i - 1][3]), abs(c[i][2] - c[i - 1][3])) for i in range(1, len(c))]
        a = sum(tr[:n]) / n
        for x in tr[n:]: a = (a * (n - 1) + x) / n
        return a
    BAR = SC["flag_bar"]; BAR_MIN = int(BAR[:-1]) * (60 if BAR.endswith('H') else 1)
    def compute_flags(inst):
        # 121 candles: enough closed history for the last 2 signal candles (each needs >= 60 incl. itself)
        raw = bf.public('/api/v1/market/candles', {'instId': inst, 'bar': BAR, 'limit': 121})['data']
        # newest-first from the API; keep closed candles only (confirm == "1"); [ts, o, h, l, c, quote_vol]
        rows = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])] for x in reversed(raw) if str(x[8]) == '1']
        if len(rows) < 61: return []
        out = []
        for sig_i in (len(rows) - 2, len(rows) - 1):
            hist = rows[:sig_i + 1]  # no future bars
            if len(hist) < 60: continue
            ts, o, h, l, c, v = hist[-1]; prev = hist[-21:-1]; cl = [r[4] for r in hist]
            avg = sum(r[5] for r in prev) / 20
            if avg <= 0: continue
            vr = v / avg
            A = _f_atr([r[1:5] for r in hist[:-1]])  # prior ATR14, excludes the signal candle
            e20 = _f_ema(cl, 20)
            hi = max(r[2] for r in prev); lo = min(r[3] for r in prev)
            change = (c / o - 1) * 100
            body = abs(c - o) / A if A else 0; ext = abs(c - e20) / A if A else 9
            cpos = (c - l) / (h - l) if h > l else .5
            brk = 'breakout' if c > hi else 'breakdown' if c < lo else None
            direction = 'up' if change > 0 else 'down' if change < 0 else 'flat'
            strong_close = (cpos >= .65 if change > 0 else cpos <= .35) and change != 0
            no_chase = ext <= 4 and body <= 4 and (not brk or (A > 0 and abs(c - (hi if brk == 'breakout' else lo)) / A <= 2))
            gate = strong_close and no_chase
            types = []
            if vr >= 2: types.append('unusual_volume')
            if vr >= 3: types.append('volume_spike')
            if gate and brk == 'breakout' and change >= 0.5 and vr >= 2 and body >= .75: types.append('breakout')
            if gate and brk == 'breakdown' and -change >= 0.5 and vr >= 2 and body >= .75: types.append('breakdown')
            if gate and abs(change) >= 1.0 and vr >= 2 and body >= 1: types.append('strong_move')
            close_time = dt.datetime.fromtimestamp((ts + BAR_MIN * 60 * 1000) / 1000, dt.timezone.utc).isoformat()
            out += [dict(ts=now_iso, pair=inst, type=t, direction=direction, candle_close_time=close_time, price=c,
                         volume_multiple=round(vr, 2), move_pct=round(change, 3), body_atr=round(body, 2), ext_atr=round(ext, 2),
                         close_pos=round(cpos, 2)) for t in types]
        return out

    flag_key_cutoff = (now - dt.timedelta(hours=2)).isoformat()
    flag_keys = {k: v for k, v in st.get('flag_keys', {}).items() if v >= flag_key_cutoff}
    flags = []; flag_errors = []; n_dedup = 0; n_checks = 0
    for r in top:
        if r['qv'] < SC["flag_min_quote_vol_usd"]: continue
        n_checks += 1
        try:
            for f in compute_flags(r['inst']):
                k = '%s|%s|%s' % (f['pair'], f['type'], f['candle_close_time'])
                if k in flag_keys: n_dedup += 1; continue
                flag_keys[k] = f['candle_close_time']
                flags.append(f)
        except Exception as e: flag_errors.append(dict(pair=r['inst'], error=str(e)[:200]))
    FLAGS_NOTE = 'INFO ONLY - NOT setups, NOT trade triggers'
    all_failed = n_checks > 0 and len(flag_errors) == n_checks
    scanner_error = f'all {n_checks} flag checks failed; flags_latest kept' if all_failed else None
    flags_latest_action = 'written'
    if all_failed: flags_latest_action = 'kept (all flag checks failed)'
    elif not flags and n_dedup > 0 and os.path.exists(FLAGS_LATEST): flags_latest_action = f'kept (dedup-only empty pass: {n_dedup})'
    else:
        try:
            _tmp = FLAGS_LATEST + '.tmp'
            with open(_tmp, 'w') as _f:
                json.dump(dict(ts=now_iso, note=FLAGS_NOTE, bar=BAR, closed_candles_only=True, flags=flags, errors=flag_errors), _f, indent=1); _f.flush(); os.fsync(_f.fileno())
            os.replace(_tmp, FLAGS_LATEST)
        except Exception as e: flag_errors.append(dict(pair=None, error='flags_latest write failed: ' + str(e)[:200])); flags_latest_action = 'write failed'
    errors += flag_errors

    st.update(universe=[r['inst'] for r in top], moves_v2=mv2, universe_seen=seen, coils_seen=cseen, flag_keys=flag_keys)
    if not all_failed: st['last_run'] = now_iso   # last FULL pass only
    _tmp = STATE + '.tmp'
    with open(_tmp, 'w') as _f: json.dump(st, _f, indent=1); _f.flush(); os.fsync(_f.fileno())
    os.replace(_tmp, STATE)
    brief = [{k: f[k] for k in ('pair', 'type', 'direction', 'candle_close_time', 'price', 'volume_multiple')} for f in flags]
    with open(LOG, 'a') as f:
        f.write(json.dumps(dict(ts=now_iso, pairs=[r['inst'] for r in top], moves=moves, newtop=newtop, coils=new_coils, flags=brief, errors=errors, **({'scanner_error': scanner_error} if scanner_error else {}))) + '\n')

    print(json.dumps(dict(ts=now_iso, pairs=len(top), moves=moves, newtop=newtop, new_coils=new_coils,
                          flags=dict(note=FLAGS_NOTE, items=brief), flags_latest=flags_latest_action, errors=errors,
                          **({'scanner_error': scanner_error} if scanner_error else {})), indent=1))
    os.close(_lock_fd)

if __name__ == "__main__":
    main()
