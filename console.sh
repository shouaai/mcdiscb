#!/usr/bin/env bash
# Peek at the live consoles. Detach with Ctrl+b then d (everything keeps running).
# Usage: ./console.sh [minecraft|playit|playitd]
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

case ${1:-minecraft} in
    mc | minecraft | server) win=minecraft ;;
    playit | tunnel) win=playit-ui ;;
    playitd | daemon) win=playitd ;;
    *) die "Usage: ./console.sh [minecraft|playit|playitd]" ;;
esac
server_running || die "Server isn't running. Start it with /start in Discord."
"${TMUX_CMD[@]}" select-window -t "=mcserver:=$win" 2>/dev/null || die "No '$win' window right now."
echo "Detach: Ctrl+b then d   Next window: Ctrl+b then n   (Ctrl+c here kills that process!)"
sleep 1.5
unset TMUX
exec "${TMUX_CMD[@]}" attach -t =mcserver
