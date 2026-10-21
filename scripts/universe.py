"""Universe tiers, ONE definition shared by scan.py, execute.py and scanner.py.

config.yaml -> scan:
  universe_ext_enabled false: scan.py ranks the top `universe_size` USDT perps by 24h quote volume.
  universe_ext_enabled true : scan.py ranks the top `universe_ext_size`. Ranks 1..universe_size = tier 'core' (rules
                              unchanged); the rest = tier 'ext', which ALSO need a fresh news-analyst rating
                              (fundamentals.json, younger than news_gate.fresh_hours) or SKIP 'no_fundamentals' (fail closed).
The ranking (by volCurrency24h x last, before the spread filter) is computed once per scan inside scan.scan() and stored in
scan.json as 'vol_ranks' {instId: rank} plus each setup's 'vol_rank' and 'universe_tier', so it is stable per scan.
"""
import json, datetime as dt
import config

S = config.CFG["scan"]
EXT_ENABLED = bool(S["universe_ext_enabled"])
TOP_BASE = int(S["universe_size"]); TOP_EXT = int(S["universe_ext_size"])
CORE, EXT = 'core', 'ext'

def top_n():
    return TOP_EXT if EXT_ENABLED else TOP_BASE

def tier(rank):
    try: return CORE if int(rank) <= TOP_BASE else EXT
    except (TypeError, ValueError): return None

def tier_of(s, scan=None):
    """Setup tier: its own 'universe_tier', else from vol_rank / scan['vol_ranks']; setups from scans without ranks
    (older scans, always the core universe) -> 'core'."""
    t = (s or {}).get('universe_tier')
    if t in (CORE, EXT): return t
    r = (s or {}).get('vol_rank') or ((scan or {}).get('vol_ranks') or {}).get((s or {}).get('instId'))
    return tier(r) if r is not None else CORE

def fund_fresh(inst, now=None, path=None):
    """(True, detail) if fundamentals.json has a rating for inst no older than news_gate.fresh_hours; else (False, why).
    Any read problem -> (False, why): callers fail closed."""
    import news_gate as NG
    now = now or dt.datetime.now(dt.timezone.utc); fresh_h = float(NG.G["fresh_hours"])
    try:
        with open(path or config.data_path('fundamentals.json')) as fh: pairs = (json.load(fh).get('pairs') or {})
    except Exception as e: return False, f'fundamentals unreadable ({str(e)[:80]})'
    r = pairs.get(str(inst).strip().upper())
    if not isinstance(r, dict): return False, 'no rating'
    t = NG._ts(r.get('ts_utc'))
    if t is None: return False, 'rating has no timestamp'
    age = (now - t).total_seconds() / 3600
    if age > fresh_h: return False, f'rating {age:.1f}h old > {fresh_h:g}h'
    return True, f'{r.get("rating")} {r.get("confidence")} {age:.1f}h'

def block_reason(s, scan=None, now=None, path=None):
    """None for core setups; for ext: None if fresh fundamentals else 'no_fundamentals (why)'. Never raises."""
    try:
        if tier_of(s, scan) != EXT: return None
        ok, why = fund_fresh((s or {}).get('instId'), now, path)
        return None if ok else f'no_fundamentals ({why})'
    except Exception as e: return f'no_fundamentals (check error {str(e)[:80]})'
