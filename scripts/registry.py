"""Registry of positions the bot opened: DATA_DIR/bot_positions.json, keyed by instId.

A record only counts for a position if it MATCHES that position: same instId and side, and either
the stored position createTime equals the position's createTime, or the position was created within
MATCH_WINDOW_MS of the record's opened_at. Pair + side alone is never enough, so a stale record can't
make a later manual trade on the same pair look like the bot's. prune() drops records that match no
open position; execute.py and manage.py call it at the start of every run.
"""
import os, json, datetime as dt
import config

FILE = "bot_positions.json"
MATCH_WINDOW_MS = 120_000

def load():
    try:
        with open(config.data_path(FILE)) as fh: reg = json.load(fh)
        return reg if isinstance(reg, dict) else {}
    except Exception:
        return {}

def save(reg):
    p = config.data_path(FILE); tmp = p + ".tmp"
    with open(tmp, "w") as fh: json.dump(reg, fh, indent=1, default=str)
    os.replace(tmp, p)

def iso_ms(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
        return int(t.timestamp() * 1000)
    except Exception:
        return None

def matches(p, rec, window_ms=MATCH_WINDOW_MS):
    if not rec or rec.get("instId") != p.get("instId"): return False
    if (rec.get("side") == "buy") != (float(p.get("positions") or 0) > 0): return False
    try: ct = int(p.get("createTime") or 0)
    except Exception: ct = 0
    if not ct: return False
    pct = rec.get("position_createTime")
    if pct and str(pct) == str(ct): return True
    oa = iso_ms(rec.get("opened_at"))
    return bool(oa and abs(ct - oa) <= window_ms)

def match(p, reg=None):
    reg = load() if reg is None else reg
    rec = reg.get(p.get("instId"))
    return rec if matches(p, rec) else None

def prune(positions, reg=None):
    """Drop records with no matching open position. Returns (registry, removed_instIds)."""
    reg = load() if reg is None else reg
    keep = {k: v for k, v in reg.items() if any(matches(p, v) for p in positions)}
    removed = sorted(set(reg) - set(keep))
    if removed: save(keep)
    return keep, removed

def put(rec):
    reg = load(); reg[rec["instId"]] = rec; save(reg)
