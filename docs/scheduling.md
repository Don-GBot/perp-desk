# Scheduling

All times UTC. Crypto runs 24/7.

| Job | When | Needs an LLM? |
|---|---|---|
| Alerts scanner (`scanner.py`) | Step 0 of each Executor run. Standalone option: :00 and :30 | No |
| Executor | Every 30 min at :07 and :37 | Yes |
| News analyst | Every 4h | Yes |
| Manager | Daily review, plus a weekly scorecard | Yes |

The Executor runs 7 minutes after the half-hour so the latest 15m candles have closed and the alert pass has data. Agent harnesses often start jobs a few minutes late; the offset absorbs that.

The setup scan (`scan.py`) does not need its own slot. The Executor refreshes it when it is older than 30 minutes.

## Plain cron

Cron starts with an almost empty environment. Use the included `run.sh` wrapper: it changes to the repo root, loads `.env`, and runs the command.

Crontab (`crontab -e`). `CRON_TZ` support varies; the times below assume the system runs in UTC:

```cron
# Optional standalone alerts scanner (only if the Executor does NOT run it as step 0)
# 0,30 * * * *  /path/to/perp-desk/run.sh python3 scripts/scanner.py >> data/cron.log 2>&1

# Agents: call your harness CLI with the role prompt. Replace `your-agent-cli` with yours.
7,37 * * * *  /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/executor.md
0 */4 * * *   /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/news-analyst.md
30 0 * * *    /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/manager.md
```

## systemd timer

Make a oneshot service per job (e.g. the optional standalone `scanner.py`, or the Executor with your harness command) with `WorkingDirectory=/path/to/perp-desk` and `EnvironmentFile=/path/to/perp-desk/.env`, plus a timer such as `OnCalendar=*-*-* *:07,37:00 UTC`, then `systemctl enable --now <name>.timer`.

## Swapping files safely

Replace scripts or config only when nothing is mid-run: `python3 scripts/swapguard.py swap x.py.new x.py` (stage the new file in the same directory). It refuses inside `ops.freeze_windows` (minutes past the hour, box local time; set them around your executor schedule), waits up to `ops.swap_timeout_s` for execute/manage processes and held locks, then skips with exit 3; it never kills anything. Under the lock it rechecks the freeze window, the scanner lock and processes. Each file is replaced atomically, but a multi-file set is not: a mid-set failure is reported as partial (exit 3) and must be fixed by hand. `swapguard.py check` just reports.

## Agent-harness routines

Most harnesses (Grok Bot, Hermes, OpenClaw, Claude Code with a scheduler, and others) have some form of scheduled task, routine or cron job. The pattern is the same everywhere:

1. Create one agent per role. Give it the matching file from `prompts/` as its instructions.
2. Give it shell access to the repo and the environment variables from `.env.example`.
3. Schedule it:
   - Executor: cron `7,37 * * * *`, message "Run the trade check."
   - News analyst: cron `0 */4 * * *`, message "Run the news ratings."
   - Manager: daily cron, message "Run the daily review." Weekly: "Run the weekly scorecard."
4. Keep plain scripts (the optional standalone scanner) out of the harness; run them from cron or systemd so they cost nothing.

If your harness bills per wake, look at `docs/usage-costs.md` before shortening any interval.
