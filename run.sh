#!/bin/sh
# Wrapper for cron/systemd: run a command from the repo root with .env loaded.
# Usage: ./run.sh python3 scripts/shadow_swap.py --once
cd "$(dirname "$0")" || exit 1
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec "$@"
