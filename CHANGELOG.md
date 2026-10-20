# Changelog

## 2026-10-20

Entry rules (both on in `config.example.yaml`; turn off in `config.yaml` if you don't want them):
- **1H trend gate** (`entry.trend_1h`, `scripts/trend1h.py`): longs only in a 1H uptrend, shorts only in a 1H downtrend
  (EMA20/EMA50 and close vs EMA50 on 200 closed 1H candles). Flat blocks both. A data error skips the setup.
  Applied in `execute.py`; `shadow_swap.py` calls the same check.
- **Stock-perp block** (`entry.stock_perp_block`, `scripts/stockperp.py`): no new entries on stock perps (maintained list
  of exchange Stocks/Indices pairs, or assetClass Stocks/Indices for new listings). Checked first, fails closed.
  Open positions are never touched. `stockperp.asset_class()` tags pairs as stock / index / commodity / forex / crypto.

Risk and management rules:
- **Re-entry cooldown** (`risk.cooldown_hours: 4`): after a bot position closes at a loss by its stop-loss, trailing
  stop or hard cut, no new entry on that pair in the same direction for 4h. Opposite direction allowed; wins,
  break-even scratches, manual positions and manual closes don't trigger it. Read from `closed_trades.jsonl`
  (`closes.cooldown_check`), plus closes not logged yet (`closed_pending.json`, fail closed); a read error skips the
  entry. `shadow_swap.py` calls the same check; each shadow book keeps its own cooldown from its own virtual closes.
- **Direction cap 3** (`risk.max_per_direction: 3`, was 4): counts open BOT positions per side only; manual positions
  count toward `max_open` (4) only. A read error skips the entry.
- **Break-even at round-trip fees** (`management.breakeven_fee_legs: 2`, replaces `breakeven_buffer_pct: 0.05`): BE stop =
  entry x (1 +/- 2 x 0.06%), rounded to the tick in the trade's favour; a recorded `be_stop` keeps priority.
- **Slippage buffer** (`fees.slip_frac: 0.0018`): added to the loss at the stop in sizing and in the hard cut
  (loss per coin = |entry - stop| + fee x (entry + stop) + 0.18% x entry). Manual positions keep the price-only cut.
- **Commodities and forex allowed**: `scan.py` no longer limits the universe to assetClass Crypto and `execute.py`
  dropped its crypto-only check; only stock perps are blocked. Every pair is tagged with `asset_class`.
- `manage.py` marks how it closed a position (`close_by`: cut / stop / manual); pruned records wait in
  `closed_pending.json` until their close is logged, so a close seen first by `execute.py` is not lost.

Logging (no rule change):
- `blocked_setups.jsonl`: one line per news-gate, 1h-trend or stock-perp block (`scripts/blocklog.py`, also a CLI for
  the agent). New `execute.py --dry`; dry-run lines go to `logs/blocked_setups_dry.jsonl`, and a dry run never
  overwrites `exec_last.json`.
- Run records: `manage.py` appends one record per invocation to `runs.json` and prints its `run_id`;
  `scripts/runlog.py annotate` lets the agent attach fields instead of hand-writing records.
- Closed trades: `manage.py` logs each closed bot position to `closed_trades.jsonl` (`scripts/closes.py`).
- Funding: `manage.py` records funding paid per bot position from the exchange funding-fee bills (`scripts/funding.py`).
- Transfers: `scripts/transfers.py` logs owner deposits/withdrawals (`add`, `list`, `detect --since` from the exchange
  bills, read-only) so equity changes are not mistaken for PnL.

- **Min R:R is price-only** (gross): (target - entry) / (entry - stop) >= `min_rr` (3), not net of fees. A net-of-fees
  filter mostly removes wide-target trades; fees and slippage are already inside the per-trade risk via sizing.
  `rr_net` is still reported, and `shadow_swap.py` keeps net R:R for its EV.

Fixes:
- Re-entry cooldown also covers closes not logged yet: a pending record on the same pair+side blocks (fail closed)
  for `cooldown_hours`, unless it was a manual close or a break-even scratch. Closed-trade lines are appended under a
  lock with fsync; a corrupt line is reported by line number. A line without close_ms uses its log time.
- `execute.py --dry` no longer prunes `bot_positions.json`: a dry run writes nothing in the data folder.
- `execute.py` stores the exchange positionId on the bot record, so close matching uses it first.
- Shadow books: the hard cut uses equity at entry, and break-even is marked only when the stop actually moves.
- `scanner.py` takes its run lock inside main(), so importing it has no side effects.
- Position / close matching: a recorded position createTime now matches within 120 s instead of by exact string, so a
  createTime reported 1 ms off no longer makes a bot position look manual or leaves its close unmatched. Closes match
  by positionId first, a known opposite side is rejected, and one history row never closes two records.
- `scanner.py`: one pass at a time (`scanner.lock`), skip if a full pass ran in the last 5 min, and if every flag check
  errors the previous `flags_latest.json` is kept and `last_run` is not advanced (the next run retries at once).
  A dedup-only empty pass also keeps the previous flags. State writes are fsync'd and atomic.

Shadow:
- New `scripts/shadow_variants.py` (read-only): virtual books BASELINE, BASELINE_NO1H, V_1H, V_ATR15, V_RR10,
  STOCKS_ONLY, V_SLOWTREND, V_CROWD walk every new scan with their own filters and track exits; `--stats` grades them.
  Pure helpers for the momentum z-score and funding crowding in `scripts/slowcrowd.py`.

Tests: new `tests/test_gates_and_logs.py` (all mocked, no network).

## 2026-10-17

- First public release.
