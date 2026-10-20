"""Stock-perp detection: ONE definition for execute.py (entry.stock_perp_block), shadow_variants.py and shadow_swap.py.
Rule: no new entries on stock perps (equities, ETFs/index, tokenized stocks).
A pair is a stock perp if it is in STOCK_PERPS (maintained list) OR Blofin GET /api/v1/market/instruments assetClass is in
STOCK_CLASSES ('Stocks', 'Indices'), so new stock listings are caught. 'Commodities' (oil, gas) and 'Crypto' are not blocked.
STOCK_PERPS snapshot: every assetClass Stocks/Indices instrument on Blofin on 2026-10-20 (108 pairs). Refresh it from
GET /api/v1/market/instruments now and then; the assetClass check catches new listings in the meantime."""
STOCK_CLASSES = frozenset(('Stocks', 'Indices'))
STOCK_PERPS = frozenset((
    'AAOI-USDT', 'AAPL-USDT', 'ADBE-USDT', 'ADI-USDT', 'ALAB-USDT', 'AMAT-USDT', 'AMD-USDT', 'AMZN-USDT',
    'APLD-USDT', 'APP-USDT', 'ARM-USDT', 'ASML-USDT', 'ASTS-USDT', 'AVGO-USDT', 'BE-USDT', 'BMNR-USDT',
    'BRKB-USDT', 'BSP-USDT', 'BX-USDT', 'CBRS-USDT', 'CGNX-USDT', 'COHR-USDT', 'COIN-USDT', 'COST-USDT',
    'CRCL-USDT', 'CRDO-USDT', 'CRWD-USDT', 'CRWV-USDT', 'CSOPSAMSUNG2L-USDT', 'CSOPSKHYNIX2L-USDT', 'CVNA-USDT', 'CYPH-USDT',
    'DELL-USDT', 'DKNG-USDT', 'DRAM-USDT', 'EWJ-USDT', 'EWY-USDT', 'GLW-USDT', 'GOOGL-USDT', 'GS-USDT',
    'HIMS-USDT', 'HOOD-USDT', 'HUT-USDT', 'IBM-USDT', 'INTC-USDT', 'IONQ-USDT', 'IREN-USDT', 'IWM-USDT',
    'JPM-USDT', 'KLAC-USDT', 'KO-USDT', 'LITE-USDT', 'LLY-USDT', 'LRCX-USDT', 'MCD-USDT', 'META-USDT',
    'MRK-USDT', 'MRNA-USDT', 'MRVL-USDT', 'MSFT-USDT', 'MSTR-USDT', 'MSTU-USDT', 'MU-USDT', 'NBIS-USDT',
    'NFLX-USDT', 'NKE-USDT', 'NOW-USDT', 'NVDA-USDT', 'OKLO-USDT', 'ORCL-USDT', 'PATH-USDT', 'PLTR-USDT',
    'QCOM-USDT', 'QNTX-USDT', 'QQQ-USDT', 'RAM-USDT', 'RDDT-USDT', 'RKLB-USDT', 'SAMSUNG-USDT', 'SKHY-USDT',
    'SKHYNIX-USDT', 'SMCI-USDT', 'SMH-USDT', 'SNDK-USDT', 'SNOW-USDT', 'SNPS-USDT', 'SNXX-USDT', 'SOXL-USDT',
    'SOXS-USDT', 'SPCX-USDT', 'SPY-USDT', 'SQQQ-USDT', 'STXX-USDT', 'TEAM-USDT', 'TLT-USDT', 'TMF-USDT',
    'TQQQ-USDT', 'TSLA-USDT', 'TSM-USDT', 'TWLO-USDT', 'TWST-USDT', 'UNH-USDT', 'URNM-USDT', 'UVXY-USDT',
    'VST-USDT', 'WDC-USDT', 'XLE-USDT', 'XOM-USDT',
))
def is_stock(inst, ins=None):
    """(True|False, how). ins = the instrument metadata dict (or None). Raises nothing."""
    if inst in STOCK_PERPS: return True, 'list'
    c = (ins or {}).get('assetClass')
    if c in STOCK_CLASSES: return True, f'assetClass {c}'
    return False, f'assetClass {c}' if c else 'not listed'
def lookup(inst, public):
    """Fetch the instrument via public(path, params) (bf.public) and classify. Raises on a failed/empty read (callers fail closed)
    unless the list already says stock."""
    if inst in STOCK_PERPS: return True, 'list'
    r = public('/api/v1/market/instruments', {'instId': inst})
    rows = [x for x in (r or {}).get('data') or [] if x.get('instId') == inst]
    if not rows: raise RuntimeError(f'instrument metadata for {inst} not found')
    if rows[0].get('assetClass'): _SEEN[inst] = rows[0]['assetClass']
    return is_stock(inst, rows[0])

# ---- asset classes (tagging only; the block above does not depend on it)
# asset_class(pair[, ins]) -> 'stock' | 'index' | 'commodity' | 'forex' | 'crypto'. Maintained lists first (they override Blofin's
# metadata, which labels metals such as XAU-USDT 'Crypto'), then Blofin assetClass (Stocks / Indices / Commodities / Forex),
# else crypto. A pair whose metadata was read by lookup() is remembered for this process (new listings tag correctly).
INDEX_PERPS = frozenset(('QQQ-USDT',))
COMMODITY_PERPS = frozenset(('XAU-USDT', 'XAG-USDT', 'XPT-USDT', 'XPD-USDT', 'XAUT-USDT', 'PAXG-USDT', 'COPPER-USDT',
                             'CL-USDT', 'BZ-USDT', 'NG-USDT', 'NATGAS-USDT', 'WTIOIL-USDT'))
FOREX_PERPS = frozenset(f'{c}-USDT' for c in ('EUR', 'GBP', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD', 'CNH', 'MXN', 'TRY', 'BRL', 'KRW', 'SGD', 'HKD', 'INR', 'ZAR'))
_CLASS_MAP = {'Stocks': 'stock', 'Indices': 'index', 'Commodities': 'commodity', 'Forex': 'forex', 'FX': 'forex', 'Crypto': 'crypto'}
_SEEN = {}
def asset_class(pair, ins=None):
    if pair in INDEX_PERPS: return 'index'
    if pair in COMMODITY_PERPS: return 'commodity'
    if pair in FOREX_PERPS: return 'forex'
    c = (ins or {}).get('assetClass') or _SEEN.get(pair)
    if c in _CLASS_MAP and _CLASS_MAP[c] != 'crypto': return _CLASS_MAP[c]
    if pair in STOCK_PERPS: return 'stock'
    return 'crypto'
