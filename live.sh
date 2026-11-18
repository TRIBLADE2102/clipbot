#!/bin/zsh
# Live clipper: ./live.sh <twitch_login> [--dry-run]. Keeps the Mac awake while it runs.
cd "$(dirname "$0")"
set -a; source ./.env; set +a
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:$PATH"
caffeinate -i python -u live.py "$@" 2>&1 | tee -a live.log
