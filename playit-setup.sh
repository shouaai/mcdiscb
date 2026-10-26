#!/usr/bin/env bash
# Claims the playit.gg agent that mcbot runs, and saves your tunnel address.
# Usage: ./playit-setup.sh [--reset]
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

[[ -x $VENV_PY ]] || die "Run ./setup.sh first."
playit_paths
[[ -x $PLAYITD_BIN && -x $PLAYIT_CLI_BIN ]] || die "playit isn't installed - run ./setup.sh"
server_running && die "Stop the server first (/stop in Discord)."
mkdir -p "$PLAYIT_DIR"
chmod 700 "$PLAYIT_DIR"

if [[ ${1:-} == --reset ]]; then
    rm -f "$PLAYIT_SECRET"
    warn "Old agent secret removed - claiming a new agent."
fi

if [[ -s $PLAYIT_SECRET ]]; then
    ok "playit agent already claimed (./playit-setup.sh --reset to claim a new one)"
else
    rm -f "$PLAYIT_SOCK"
    "$PLAYITD_BIN" --socket-path "$PLAYIT_SOCK" --secret-path "$PLAYIT_SECRET" \
        --log-path "$PLAYIT_DIR/playitd.log" &
    daemon=$!
    trap 'kill -INT "$daemon" 2>/dev/null; wait "$daemon" 2>/dev/null || true; rm -f "$PLAYIT_SOCK"' EXIT
    for _ in {1..50}; do [[ -S $PLAYIT_SOCK ]] && break; sleep 0.2; done
    [[ -S $PLAYIT_SOCK ]] || die "playitd didn't start - see playit/playitd.log"

    echo "Open the link playit prints below, log in, and approve this agent."
    "$PLAYIT_CLI_BIN" --socket-path "$PLAYIT_SOCK" setup
    [[ -s $PLAYIT_SECRET ]] || die "Claim didn't finish (no secret saved). Run ./playit-setup.sh again."
    chmod 600 "$PLAYIT_SECRET"
    ok "Agent claimed"
fi

cur=$(envget PLAYIT_ADDRESS)
cat <<EOF

Create the tunnel once (skip if you already have one for this agent):
  1. https://playit.gg/account/tunnels  ->  Add Tunnel
  2. Type: Minecraft Java    Local address: 127.0.0.1    Local port: 25565
  3. Assign it to this agent, then copy the public address (e.g. something.joinmc.link)
EOF
read -rp "Tunnel address${cur:+ [$cur]}: " addr
addr=${addr:-$cur}
addr=${addr//[[:space:]]/}
if [[ -n $addr ]]; then
    envset PLAYIT_ADDRESS "$addr"
    ok "PLAYIT_ADDRESS=$addr"
else
    warn "No address saved - set PLAYIT_ADDRESS in .env later so /ip can show it."
fi
