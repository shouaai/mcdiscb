#!/usr/bin/env bash
# Starts the Discord bot. Keep this terminal open: closing it (or Ctrl+C)
# saves the world, stops the server + playit, then exits the bot.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

[[ -x $VENV_PY ]] || die "Run ./setup.sh first."
exec 9>"$PWD/.bot.lock"
flock -n 9 || die "mcbot is already running in another terminal."
printf '\e]0;mcbot - Minecraft server bot\a'
exec "$VENV_PY" -u bot.py "$@"
