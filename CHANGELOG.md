# Changelog

## 2026-10-21 (sync 4)

- **Minimum risk floor** (`risk.min_risk_pct`, default 1.0): `fit_size` skips an entry as `below_min_risk` (block line logged) when its fee-inclusive risk would be under 1% of equity after scaling. Shadow swap mirrors it through `fit_size`.
- **One fundamentals freshness setting**: `news_gate.fresh_hours` default 4 -> 4.5; used by the news gate, the opposing-rating check, the ext `no_fundamentals` filter and shadow swap. Cooldown stays `risk.cooldown_hours` (4).
- **R is fee-inclusive**: `R = pnl_net / planned risk incl. fees and entry slip`; `R_price` (price-only) kept alongside. Close lines and registry records carry `size`, `ctVal`, `risk_usdt`, `risk_usdt_allin`. `python3 scripts/closes.py report` recomputes old lines; lines without entry/stop count as `unknown_R`; a missing ctVal is derived from `risk_usdt / |entry - stop|`. Shadow variant stats rescale old rows the same way.
- **Break-even scratch** (0.5R, cooldown exemption) uses fee-inclusive R, with a price-only fallback (then equity) that is logged.
- **Shadow variants**: a post-BE close is cooldown-exempt only if it is a scratch (exit at the BE stop, or R > -0.5); bigger post-BE losses now start the cooldown.
- **New ops script `scripts/swapguard.py`** (`ops:` config): swap files / restart only when idle. Refuses in freeze windows, waits for running execute/manage processes and held locks (2 s polls, 180 s, then skip with exit 3, never kills), rechecks under execute.lock, atomic `os.replace`.

## 2026-10-20 (sync 3)

Entry and sizing rules (all on in `config.example.yaml`):
- **Top-40 universe** (`scan.universe_ext_enabled`, `universe_ext_size: 40`, `scripts/universe.py`): `scan.py` ranks the
  top 40 by 24h quote volume (before the spread filter, stored per scan as `vol_ranks`, `vol_rank`, `universe_tier`).
  Ranks 1–25 (`core`) keep the old rules; ranks 26–40 (`ext`) also need a fresh news-analyst rating, else skip
  `no_fundamentals` (fails closed). `scanner.py` uses the same universe, so its alerts (moves, new pairs, coils, flags)
  now cover the top 40 too; this is intended.
- **5% risk is a maximum** (`risk.scale_down`): instead of skipping, the size is cut (never raised) to the binding limit:
  leverage max 20 / instrument max, margin cap 25% of equity / 95% of free margin, liquidation > 2x the stop distance,
  and the risk-cap headroom. A scaled trade whose round-trip fees exceed 1/3 of its stop-out loss is skipped
  (`fee_heavy`, scaled trades only; full-size trades are never fee_heavy skipped), one below the instrument minimum `below_min_size`. Shared `execute.fit_size`.
- **Risk-based caps** (`risk.risk_caps`, `scripts/riskcap.py`): total open risk (loss at the current stops from the
  mark, incl. exit fee + slippage) + new <= 20% of equity, same-direction bot risk + new <= 15%, scaled to the headroom
  else `cap_risk`; hard backstop 8 positions; a manual position without a stop counts 5%; a stop counts as 0 risk only
  if a stop-out nets >= 0 after entry + exit fees and slippage. Fail closed to the old slot caps (4 / 3 per side) when
  any position's risk can't be computed: missing mark (never substituted by entry), stop orders covering only part of
  the size, bot position without a stop, a bot record that doesn't match the live position, unreadable or full
  (100-row) TP/SL page.
- **Entry slippage** (`fees.entry_slip_frac: 0.0018`): planned stop-out loss in sizing now includes 0.18% expected
  market-entry slippage (= the exit-slip estimate until entry-fill data exists). The hard cut is unchanged (it uses the
  real average price) and the fee_heavy share deliberately keeps its old denominator (no entry-slip term).
- **Unresolved-record block** (`entry.unresolved_record_block`): no entry, either side, on a pair holding a bot record
  with no matching live position or a closed record whose close line isn't logged yet. `registry.put` never overwrites
  another trade's record (the order is then not sent).
- **Adoption from order history** (`bot.adopt_from_history`): `manage.py` rebuilds the missing record of a position
  owned through a bot-prefixed opening order in the exchange history.

Logging (no rule change):
- Every per-setup executor skip is a `blocked_setups.jsonl` line (run-level exits — stale scan, equity unreadable, max
  positions, not net mode — return a gate_reason only) (`logging.log_all_skips`), each setup at most once per run, with
  `universe_tier` / `vol_rank`; sizing gates carry the sizing numbers, `cap_risk` the risk-cap summary.
- `--dry` decision runs of the scheduled routine (RUN_ID = a manage.py run record from the last
  `logging.scheduled_dry_window_s`) log to the live file with mode `scheduled_dry`; ad-hoc dry runs stay in the dry log.
- Plans, bot records, shadow rows carry `scaled_down`, `scaled_by`, `risk_pct_actual`, `fee_share`, `risk_caps`.

Shadows:
- New book **V_RR2**: live rules with gross min R:R `shadow_variants.rr2_min` (2.0); also walks the scan's rejected
  2 <= R:R < 3 setups rebuilt with `scan.geometry`; rows tagged `rr_bucket`. Every book follows the universe tiers,
  risk caps (on the virtual book) and `fit_size`; rows carry `universe_tier`; R uses the planned (entry-slip) loss.
- `shadow_swap.py`: universe tiers, `fit_size` with the live risk-cap headroom, `max_age_min` 35 (= live), `--stats --by universe_tier`.

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
  for `cooldown_hours`, unless it was a manual close. A break-even scratch now needs the exit at/beyond the BE stop
  (minus a slippage tolerance) or a loss under 0.5R; `be_done` alone no longer exempts a loss. Closed-trade lines are appended under a
  lock with fsync; a corrupt line is reported by line number. A line without close_ms uses its log time.
- `execute.py --dry` no longer prunes `bot_positions.json`: a dry run writes no state/live files (its blocks go only to
  the dry log).
- `execute.py` stores the exchange positionId on the bot record, so close matching uses it first.
- `execute.py` writes a provisional bot record BEFORE the order (no record, no order). It stays when the order outcome
  is unknown after a timeout, so the position is adopted as the bot's; it is removed when the order is rejected.
- The direction cap counts a position as the bot's by its record OR a bot-prefixed opening order in the history.
- `manage.py hard-cut`: if a cut close fails, it falls back to the tightest valid stop; one position's error no longer
  skips the others. `ensure-stops` uses the price-only distance for manual positions.
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
