#!/usr/bin/env python3
"""News gate check against DATA_DIR/fundamentals.json (written by the news analyst).

  python3 scripts/news_gate.py SOL-USDT buy     # prints {"block": false, "reason": "..."}
  Side: buy/long or sell/short (any case). Anything else exits with code 2.

Rules (config.yaml -> news_gate):
  - File missing, or no rating younger than fail_open_after_hours  -> fail open (allow).
  - Pair rating older than fresh_hours, or missing                 -> allow.
  - Neutral                                                        -> allow.
  - Opposing (bearish vs long, bullish vs short) with confidence in block_confidence -> BLOCK.
  - Opposing with low confidence                                   -> allow, but note it for the log.
macro_check() applies config macro_window to fundamentals.json macro.events
([{name, ts_utc}]). It is off by default. When on and the file has no events list, it fails open
with a warning, like the news gate.

Exit code 0 for a valid check (allowed or blocked: read the JSON), 2 for invalid arguments.
"""
import os, sys, json, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

def _load_json(path):
    with open(path) as fh:
        return json.load(fh)

G = config.CFG["news_gate"]
ALIASES = {"med": "medium", "mid": "medium", "hi": "high", "lo": "low"}

def _ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None

SIDES = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}

def norm_side(side):
    s = SIDES.get(str(side).strip().lower())
    if not s: raise ValueError(f"invalid side {side!r}: use buy/long or sell/short")
    return s

def check(inst, side, path=None, now=None):
    side = norm_side(side)
    if not inst or not str(inst).strip(): raise ValueError("instId is required, e.g. BTC-USDT")
    inst = str(inst).strip().upper()
    if not G.get("enabled", True): return dict(block=False, reason="news gate disabled")
    now = now or dt.datetime.now(dt.timezone.utc)
    path = path or config.data_path("fundamentals.json")
    if not os.path.exists(path): return dict(block=False, reason="fundamentals.json missing: fail open")
    try: data = _load_json(path)
    except Exception as e: return dict(block=False, reason=f"fundamentals.json unreadable ({e}): fail open")
    pairs = data.get("pairs") or {}
    ages = [(now - t).total_seconds() / 3600 for t in (_ts(v.get("ts_utc")) for v in pairs.values()) if t]
    if not ages or min(ages) > G["fail_open_after_hours"]:
        return dict(block=False, reason=f"no rating under {G['fail_open_after_hours']}h old: fail open")
    r = pairs.get(inst)
    if not r: return dict(block=False, reason="no rating for pair")
    t = _ts(r.get("ts_utc"))
    if not t or (now - t).total_seconds() / 3600 > G["fresh_hours"]:
        return dict(block=False, reason="rating not fresh")
    rating = str(r.get("rating", "")).lower()
    conf = ALIASES.get(str(r.get("confidence", "")).lower(), str(r.get("confidence", "")).lower())
    long_ = side == "buy"
    opposing = (long_ and rating == "bearish") or (not long_ and rating == "bullish")
    if not opposing: return dict(block=False, reason=f"rating {rating or 'none'} does not oppose", rating=rating, confidence=conf)
    blockers = [ALIASES.get(str(c).lower(), str(c).lower()) for c in G["block_confidence"]]
    if conf in blockers: return dict(block=True, reason=f"fresh {rating} rating, {conf} confidence", rating=rating, confidence=conf)
    return dict(block=False, reason=f"opposing {rating} rating but {conf or 'unknown'} confidence: log only", rating=rating, confidence=conf)

def macro_check(now=None, path=None):
    MW = config.CFG["macro_window"]
    if not MW.get("enabled"): return dict(block=False, reason="macro window disabled")
    now = now or dt.datetime.now(dt.timezone.utc)
    path = path or config.data_path("fundamentals.json")
    try: events = (_load_json(path).get("macro") or {}).get("events")
    except Exception: events = None
    if not isinstance(events, list):
        return dict(block=False, warning=True, reason="macro window on but no macro.events in fundamentals.json: fail open")
    before = dt.timedelta(hours=MW["hours_before"]); after = dt.timedelta(hours=MW["hours_after"])
    for e in events:
        t = _ts((e or {}).get("ts_utc"))
        if t and t - before <= now <= t + after:
            return dict(block=True, reason=f"macro window: {e.get('name', 'event')} at {t.isoformat()}")
    return dict(block=False, reason="no macro event in window")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: news_gate.py INST_ID buy|sell|long|short", file=sys.stderr); sys.exit(2)
    try: res = check(sys.argv[1], sys.argv[2])
    except ValueError as e:
        print(json.dumps(dict(error=str(e))), file=sys.stderr); sys.exit(2)
    print(json.dumps(res))
