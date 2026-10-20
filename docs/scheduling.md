# Scheduling

All times UTC. Crypto runs 24/7.

| Job | When | Needs an LLM? |
|---|---|---|
| Alerts scanner (`scanner.py`) | Step 0 of each Executor run. Standalone option: :00 and :30 | No |
| Executor | Every 30 min at :07 and :37 | Yes |
| News analyst | Every 4h | Yes |
| Shadow swap logger (`shadow_swap.py --once`) | Every 15 min at :09, :24, :39, :54 | No |
| Manager | Daily review, plus a weekly scorecard | Yes |

The Executor runs 7 minutes after the half-hour so the latest 15m candles have closed and the alert pass has data. Agent harnesses often start jobs a few minutes late; the offset absorbs that.

The setup scan (`scan.py`) does not need its own slot. The Executor refreshes it when it is older than 30 minutes.

## Plain cron

Cron starts with an almost empty environment. Use the included `run.sh` wrapper: it changes to the repo root, loads `.env`, and runs the command.

Crontab (`crontab -e`). `CRON_TZ` support varies; the times below assume the system runs in UTC:

```cron
# Shadow swap logger: plain script, no LLM
9,24,39,54 * * * *  /path/to/perp-desk/run.sh python3 scripts/shadow_swap.py --once >> data/cron.log 2>&1
*/5 * * * *         /path/to/perp-desk/run.sh python3 scripts/shadow_variants.py --once >> data/cron.log 2>&1

# Optional standalone alerts scanner (only if the Executor does NOT run it as step 0)
# 0,30 * * * *  /path/to/perp-desk/run.sh python3 scripts/scanner.py >> data/cron.log 2>&1

# Agents: call your harness CLI with the role prompt. Replace `your-agent-cli` with yours.
7,37 * * * *  /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/executor.md
0 */4 * * *   /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/news-analyst.md
30 0 * * *    /path/to/perp-desk/run.sh your-agent-cli --prompt-file prompts/manager.md
```

## systemd timer

`/etc/systemd/system/shadow-swap.service`:

```ini
[Unit]
Description=Shadow swap logger (read-only)

[Service]
Type=oneshot
WorkingDirectory=/path/to/perp-desk
EnvironmentFile=/path/to/perp-desk/.env
ExecStart=/usr/bin/python3 scripts/shadow_swap.py --once
```

`/etc/systemd/system/shadow-swap.timer`:

```ini
[Unit]
Description=Shadow swap logger every 15 min

[Timer]
OnCalendar=*-*-* *:09,24,39,54:00 UTC
Persistent=false

[Install]
WantedBy=timers.target
```

Enable with `systemctl enable --now shadow-swap.timer`. Make an Executor service and timer the same way with `OnCalendar=*-*-* *:07,37:00 UTC` and your harness command.

Alternative: `python3 scripts/shadow_swap.py` with no flags runs its own scheduler loop. Use that under a process supervisor if you prefer one long-running process.

## Agent-harness routines

Most harnesses (Grok Bot, Hermes, OpenClaw, Claude Code with a scheduler, and others) have some form of scheduled task, routine or cron job. The pattern is the same everywhere:

1. Create one agent per role. Give it the matching file from `prompts/` as its instructions.
2. Give it shell access to the repo and the environment variables from `.env.example`.
3. Schedule it:
   - Executor: cron `7,37 * * * *`, message "Run the trade check."
   - News analyst: cron `0 */4 * * *`, message "Run the news ratings."
   - Manager: daily cron, message "Run the daily review." Weekly: "Run the weekly scorecard."
4. Keep the shadow logger out of the harness. It is a plain script; run it from cron or systemd so it costs nothing.

If your harness bills per wake, look at `docs/usage-costs.md` before shortening any interval.
