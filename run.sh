#!/bin/zsh
# Loads keys from .env (never printed) and runs one pass. Cron calls this.
cd "$(dirname "$0")"
set -a; source ./.env; set +a
export PATH="/opt/homebrew/bin:$PATH"
.venv/bin/python clipbot.py "$@" >> clipbot.log 2>&1
