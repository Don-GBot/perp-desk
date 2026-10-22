"""Config loader shared by all scripts.

Order of precedence (highest first):
  1. Environment variables: DATA_DIR, BOT_CONFIG (path to the YAML file).
  2. config.yaml (path from BOT_CONFIG, else ./config.yaml, else config.yaml in the repo root).
  3. DEFAULTS below (same values as config.example.yaml).

PyYAML is used if installed. Without it, a small built-in parser reads the subset used by
config.example.yaml: nested mappings, scalars, inline [a, b] lists, block lists of scalars
("- item"), and # comments. Anything else raises ConfigError instead of being ignored.

The merged config is validated on load. A bad value raises ConfigError with a clear message.
"""
import os, copy

class ConfigError(ValueError):
    pass

DEFAULTS = {
    "data_dir": "./data",
    "risk": {"risk_per_trade_pct": 5, "hard_cut_pct": 5.5, "max_open": 4, "max_per_direction": 3,
             "one_new_open_per_run": True, "cooldown_hours": 4,
             "scale_down": True, "fee_heavy_max_share": 0.3333333333333333, "min_risk_pct": 1.0,
             "risk_caps": {"enabled": True, "total_pct": 20, "direction_pct": 15, "max_positions": 8, "manual_no_stop_pct": 5}},
    "fees": {"taker_rate": 0.0006, "slip_frac": 0.0025, "entry_slip_frac": 0.0018},
    "bot": {"client_order_prefix": "bfx", "protect_manual_trades": True, "adopt_from_history": True},
    "entry": {"min_rr": 3, "min_stop_atr": 1, "atr_tolerance": 0.95,
              "trend_filter": {"enabled": True, "timeframe": "4H", "ema_fast": 20, "ema_slow": 50},
              "max_scan_age_min": 35,
              "trend_1h": {"enabled": True, "bar": "1H", "bars": 200, "ema_fast": 20, "ema_slow": 50},
              "stock_perp_block": {"enabled": True}, "unresolved_record_block": True,
              "rr_cap_max": 8, "cost_heavy_max_share": 0.12,
              "btc_4h_veto": {"enabled": True, "max_move": 0.01, "stale_min": 15},
              "composite": {"enabled": True, "target_atr_free": 8, "target_atr_pen": 0.25, "gap_full_pct": 3,
                            "gap_over_pen": 0.3333333333333333, "fee_share_free": 0.08, "fee_share_pen": 0.25, "btc_bonus": 0.25}},
    "scan": {"universe_size": 25, "universe_ext_enabled": True, "universe_ext_size": 40, "max_spread_pct": 0.1, "swing_lookback_1h": 12, "swing_buffer_atr": 0.25,
             "target_lookback_4h": 60, "atr_period": 14, "candles_limit": 200},
    "leverage": {"min": 3, "max": 20, "margin_cap_pct": 25, "free_margin_buffer": 0.95,
                 "margin_mode": "isolated", "mmr_default": 0.005, "max_stop_to_liq": 0.5},
    "management": {"trail_activate_r": 1.0, "trail_callback_atr": 1.5, "breakeven_at_r": 0.75,
                   "breakeven_fee_legs": 2, "breakeven_trail_atr": 1.0},
    "news_gate": {"enabled": True, "fresh_hours": 4.5, "block_confidence": ["medium", "high"],
                  "fail_open_after_hours": 5},
    "macro_window": {"enabled": False, "hours_before": 2, "hours_after": 1},
    "scanner": {"move_alert_pct": 5, "realert_min_pts": 3, "realert_frac": 0.25, "memory_hours": 24,
                "coil_ratio": 0.5, "coil_top": 3, "flag_min_quote_vol_usd": 1000000, "flag_bar": "15m"},
    "logging": {"blocked_setups": True, "funding": True, "run_records": True, "log_all_skips": True,
                "scheduled_dry_window_s": 1800},
    "ops": {"freeze_windows": ["05-15", "35-45"], "swap_timeout_s": 180, "swap_poll_s": 2},
}

# ---- minimal YAML subset parser -----------------------------------------------------------------
def _scalar(s):
    s = s.strip()
    if not s: return None
    if s[0] in "\"'":
        if len(s) < 2 or s[-1] != s[0]: raise ConfigError(f"unterminated quoted string: {s!r}")
        return s[1:-1]
    if s.startswith("["):
        if not s.endswith("]"): raise ConfigError(f"unterminated inline list: {s!r}")
        return [_scalar(x) for x in s[1:-1].split(",") if x.strip()]
    if s.startswith("{"): raise ConfigError(f"inline mappings are not supported by the built-in parser: {s!r} (install PyYAML)")
    low = s.lower()
    if low in ("true", "yes", "on"): return True
    if low in ("false", "no", "off"): return False
    if low in ("null", "none", "~"): return None
    try: return int(s)
    except ValueError: pass
    try: return float(s)
    except ValueError: return s

def _strip_comment(line):
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q: q = None
        elif ch in "\"'": q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"): break
        out.append(ch)
    return "".join(out).rstrip()

def _mini_yaml(text):
    root = {}
    stack = [(-1, root)]          # (indent, container)
    pending = None                # (indent, parent_dict, key) for a "key:" with no value yet
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]: raise ConfigError(f"line {n}: tabs are not allowed for indentation")
        line = _strip_comment(raw)
        if not line.strip(): continue
        ind = len(line) - len(line.lstrip()); body = line.strip()
        if body.startswith("- ") or body == "-":
            item = body[1:].strip()
            if pending and ind > pending[0]:
                pind, pdict, pkey = pending; pdict[pkey] = []; stack.append((pind, pdict[pkey])); pending = None
            while stack and stack[-1][0] >= ind and not isinstance(stack[-1][1], list): stack.pop()
            cont = stack[-1][1]
            if not isinstance(cont, list): raise ConfigError(f"line {n}: list item without a parent key: {raw!r}")
            if ":" in item and not item.startswith(("'", '"')): raise ConfigError(f"line {n}: lists of mappings are not supported by the built-in parser (install PyYAML)")
            cont.append(_scalar(item)); continue
        if ":" not in body: raise ConfigError(f"line {n}: cannot parse {raw!r}")
        key, _, val = body.partition(":")
        key = key.strip()
        if not key or " " in key and not key.startswith(("'", '"')): raise ConfigError(f"line {n}: bad key {key!r}")
        if pending:  # previous "key:" had no children -> empty mapping
            pind, pdict, pkey = pending
            if ind > pind: pdict[pkey] = {}; stack.append((pind, pdict[pkey]))
            else: pdict[pkey] = None
            pending = None
        while stack and (stack[-1][0] >= ind or isinstance(stack[-1][1], list)): stack.pop()
        if not stack: raise ConfigError(f"line {n}: bad indentation")
        parent = stack[-1][1]
        if not isinstance(parent, dict): raise ConfigError(f"line {n}: mapping inside a list is not supported")
        if val.strip() == "": pending = (ind, parent, key)
        else: parent[key] = _scalar(val)
    if pending: pending[1][pending[2]] = None
    return root

def _has_content(text):
    return any(_strip_comment(l).strip() for l in text.splitlines())

# ---- merge + validate ---------------------------------------------------------------------------
def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict): _merge(base[k], v)
        else: base[k] = v
    return base

def _num(cfg, path, lo=None, hi=None, lo_incl=True, hi_incl=True, integer=False):
    cur = cfg
    for p in path.split("."): cur = cur[p]
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        raise ConfigError(f"{path} must be a number, got {cur!r}")
    if integer and int(cur) != cur: raise ConfigError(f"{path} must be a whole number, got {cur!r}")
    if lo is not None and (cur < lo or (cur == lo and not lo_incl)):
        raise ConfigError(f"{path} must be {'>=' if lo_incl else '>'} {lo}, got {cur}")
    if hi is not None and (cur > hi or (cur == hi and not hi_incl)):
        raise ConfigError(f"{path} must be {'<=' if hi_incl else '<'} {hi}, got {cur}")
    return cur

def _bool(cfg, path):
    cur = cfg
    for p in path.split("."): cur = cur[p]
    if not isinstance(cur, bool): raise ConfigError(f"{path} must be true or false, got {cur!r}")

def validate(cfg):
    for sec, d in DEFAULTS.items():
        if isinstance(d, dict) and not isinstance(cfg.get(sec), dict):
            raise ConfigError(f"section {sec!r} must be a mapping, got {cfg.get(sec)!r}")
    r = _num(cfg, "risk.risk_per_trade_pct", 0, 10, lo_incl=False)
    h = _num(cfg, "risk.hard_cut_pct", 0, 100, lo_incl=False)
    if h < r: raise ConfigError(f"risk.hard_cut_pct ({h}) must be >= risk.risk_per_trade_pct ({r})")
    _num(cfg, "risk.max_open", 1, integer=True); _num(cfg, "risk.max_per_direction", 1, integer=True)
    _bool(cfg, "risk.one_new_open_per_run"); _num(cfg, "risk.cooldown_hours", 0)
    if _num(cfg, "risk.min_risk_pct", 0) > cfg["risk"]["risk_per_trade_pct"]: raise ConfigError("risk.min_risk_pct must be <= risk.risk_per_trade_pct")
    _bool(cfg, "risk.scale_down"); _num(cfg, "risk.fee_heavy_max_share", 0, 1, lo_incl=False)
    if not isinstance(cfg["risk"].get("risk_caps"), dict): raise ConfigError("risk.risk_caps must be a mapping")
    _bool(cfg, "risk.risk_caps.enabled")
    rt = _num(cfg, "risk.risk_caps.total_pct", 0, 100, lo_incl=False); rd = _num(cfg, "risk.risk_caps.direction_pct", 0, 100, lo_incl=False)
    if rd > rt: raise ConfigError("risk.risk_caps.direction_pct must be <= total_pct")
    _num(cfg, "risk.risk_caps.max_positions", 1, integer=True); _num(cfg, "risk.risk_caps.manual_no_stop_pct", 0, 100)
    _num(cfg, "fees.taker_rate", 0, 0.01); _num(cfg, "fees.slip_frac", 0, 0.05); _num(cfg, "fees.entry_slip_frac", 0, 0.05)
    p = cfg["bot"]["client_order_prefix"]
    if not isinstance(p, str) or not p.isalnum() or not 1 <= len(p) <= 8:
        raise ConfigError(f"bot.client_order_prefix must be 1-8 letters/digits, got {p!r}")
    _bool(cfg, "bot.protect_manual_trades"); _bool(cfg, "bot.adopt_from_history")
    _num(cfg, "entry.min_rr", 0, lo_incl=False); _num(cfg, "entry.min_stop_atr", 0)
    _num(cfg, "entry.atr_tolerance", 0, 1, lo_incl=False); _num(cfg, "entry.max_scan_age_min", 0, lo_incl=False)
    _bool(cfg, "entry.trend_1h.enabled"); _bool(cfg, "entry.stock_perp_block.enabled"); _bool(cfg, "entry.unresolved_record_block")
    _num(cfg, "entry.rr_cap_max", 0); _num(cfg, "entry.cost_heavy_max_share", 0, 1)
    if not isinstance(cfg["entry"].get("btc_4h_veto"), dict): raise ConfigError("entry.btc_4h_veto must be a mapping")
    _bool(cfg, "entry.btc_4h_veto.enabled"); _num(cfg, "entry.btc_4h_veto.max_move", 0, 1, lo_incl=False)
    _num(cfg, "entry.btc_4h_veto.stale_min", 0, 240, lo_incl=False)
    if not isinstance(cfg["entry"].get("composite"), dict): raise ConfigError("entry.composite must be a mapping")
    _bool(cfg, "entry.composite.enabled")
    for k in ("target_atr_free", "target_atr_pen", "gap_over_pen", "fee_share_pen", "btc_bonus"): _num(cfg, "entry.composite." + k, 0)
    _num(cfg, "entry.composite.gap_full_pct", 0, lo_incl=False); _num(cfg, "entry.composite.fee_share_free", 0, 1)
    t1 = cfg["entry"]["trend_1h"]
    a1 = _num(cfg, "entry.trend_1h.ema_fast", 1, integer=True); b1 = _num(cfg, "entry.trend_1h.ema_slow", 1, integer=True)
    if a1 >= b1: raise ConfigError("entry.trend_1h.ema_fast must be smaller than ema_slow")
    if _num(cfg, "entry.trend_1h.bars", 1, 1440, integer=True) < b1: raise ConfigError("entry.trend_1h.bars must be >= ema_slow")
    if t1["bar"] not in ("15m", "30m", "1H", "2H", "4H"): raise ConfigError(f"entry.trend_1h.bar not supported: {t1['bar']!r}")
    for k in ("blocked_setups", "funding", "run_records", "log_all_skips"): _bool(cfg, "logging." + k)
    _num(cfg, "logging.scheduled_dry_window_s", 0)
    tf = cfg["entry"]["trend_filter"]
    _bool(cfg, "entry.trend_filter.enabled")
    f = _num(cfg, "entry.trend_filter.ema_fast", 1, integer=True); s = _num(cfg, "entry.trend_filter.ema_slow", 1, integer=True)
    if f >= s: raise ConfigError("entry.trend_filter.ema_fast must be smaller than ema_slow")
    if tf["timeframe"] not in ("1H", "2H", "4H", "6H", "8H", "12H", "1D"): raise ConfigError(f"entry.trend_filter.timeframe not supported: {tf['timeframe']!r}")
    us = _num(cfg, "scan.universe_size", 1, integer=True); _bool(cfg, "scan.universe_ext_enabled")
    if _num(cfg, "scan.universe_ext_size", 1, integer=True) < us: raise ConfigError("scan.universe_ext_size must be >= scan.universe_size")
    _num(cfg, "scan.max_spread_pct", 0, lo_incl=False)
    _num(cfg, "scan.swing_lookback_1h", 1, integer=True); _num(cfg, "scan.swing_buffer_atr", 0)
    _num(cfg, "scan.target_lookback_4h", 1, integer=True); _num(cfg, "scan.atr_period", 1, integer=True)
    _num(cfg, "scan.candles_limit", 60, 1440, integer=True)
    lmin = _num(cfg, "leverage.min", 1, integer=True); lmax = _num(cfg, "leverage.max", 1, integer=True)
    if lmin > lmax: raise ConfigError("leverage.min must be <= leverage.max")
    _num(cfg, "leverage.margin_cap_pct", 0, 100, lo_incl=False); _num(cfg, "leverage.free_margin_buffer", 0, 1, lo_incl=False)
    if cfg["leverage"]["margin_mode"] not in ("isolated", "cross"): raise ConfigError("leverage.margin_mode must be isolated or cross")
    _num(cfg, "leverage.mmr_default", 0, 1); _num(cfg, "leverage.max_stop_to_liq", 0, 1, lo_incl=False)
    for k in ("trail_activate_r", "trail_callback_atr", "breakeven_at_r", "breakeven_trail_atr"): _num(cfg, "management." + k, 0, lo_incl=False)
    _num(cfg, "management.breakeven_fee_legs", 0, 10)
    _bool(cfg, "news_gate.enabled"); _num(cfg, "news_gate.fresh_hours", 0, lo_incl=False); _num(cfg, "news_gate.fail_open_after_hours", 0, lo_incl=False)
    bc = cfg["news_gate"]["block_confidence"]
    if not isinstance(bc, list) or not bc: raise ConfigError("news_gate.block_confidence must be a non-empty list, e.g. [medium, high]")
    bad = [x for x in bc if str(x).lower() not in ("low", "medium", "high", "med")]
    if bad: raise ConfigError(f"news_gate.block_confidence has unknown values {bad}; use low, medium, high")
    _bool(cfg, "macro_window.enabled"); _num(cfg, "macro_window.hours_before", 0); _num(cfg, "macro_window.hours_after", 0)
    for k in ("move_alert_pct", "realert_min_pts", "memory_hours", "coil_ratio"): _num(cfg, "scanner." + k, 0, lo_incl=False)
    _num(cfg, "scanner.realert_frac", 0); _num(cfg, "scanner.coil_top", 0, integer=True); _num(cfg, "scanner.flag_min_quote_vol_usd", 0)
    if not str(cfg["scanner"]["flag_bar"]).endswith(("m", "H")): raise ConfigError("scanner.flag_bar must look like 15m or 1H")
    _num(cfg, "ops.swap_timeout_s", 0); _num(cfg, "ops.swap_poll_s", 0, lo_incl=False)
    fw = cfg["ops"]["freeze_windows"]
    if not isinstance(fw, list) or any(not isinstance(x, str) or len(x.split("-")) != 2 or not all(p.isdigit() and 0 <= int(p) <= 60 for p in x.split("-")) or int(x.split("-")[0]) >= int(x.split("-")[1]) for x in fw):
        raise ConfigError('ops.freeze_windows must be a list of "MM-MM" minute ranges, e.g. ["05-15", "35-45"]')
    return cfg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _find_config():
    """BOT_CONFIG, else <repo root>/config.yaml, else built-in defaults."""
    p = os.environ.get("BOT_CONFIG")
    if p:
        if not os.path.exists(p): raise ConfigError(f"BOT_CONFIG points to a missing file: {p}")
        return p
    c = os.path.join(REPO_ROOT, "config.yaml")   # the working directory is never searched
    return c if os.path.exists(c) else None

def parse_text(text):
    """Parse config text with PyYAML if available, else the built-in parser. Never silently returns {}."""
    try:
        import yaml  # optional
        data = yaml.safe_load(text)
    except ImportError:
        data = _mini_yaml(text)
    if data is None: data = {}
    if not isinstance(data, dict): raise ConfigError("config file must be a mapping at the top level")
    if not data and _has_content(text): raise ConfigError("config file has content but nothing could be parsed")
    return data

def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    p = path or _find_config()
    if p:
        with open(p) as fh: _merge(cfg, parse_text(fh.read()))
    validate(cfg)
    dd = os.path.expanduser(os.environ.get("DATA_DIR") or cfg["data_dir"])
    cfg["data_dir"] = os.path.abspath(dd if os.path.isabs(dd) else os.path.join(REPO_ROOT, dd))   # relative = repo root
    os.makedirs(cfg["data_dir"], exist_ok=True)
    cfg["_config_path"] = p
    return cfg

CFG = load()
DATA_DIR = CFG["data_dir"]

def data_path(*parts):
    return os.path.join(DATA_DIR, *parts)
