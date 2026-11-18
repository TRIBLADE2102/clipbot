#!/bin/zsh
# Cron: every 5 min, start the live clipper for permitted streamers who are live.
cd "$(dirname "$0")"
set -a; source ./.env; set +a
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:$PATH"
python watch.py >> watch.log 2>&1
