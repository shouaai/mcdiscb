# shellcheck shell=bash disable=SC2034
# Shared helpers for mcbot's shell scripts. Sourced, not run.

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

VENV_PY="$PWD/.venv/bin/python"
TMUX_CMD=(tmux -L mcbot)

c_bold=$'\e[1m'; c_green=$'\e[32m'; c_yellow=$'\e[33m'; c_red=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'

step() { printf '\n%s==> %s%s\n' "$c_bold" "$*" "$c_off"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_off" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '%s✗ %s%s\n' "$c_red" "$*" "$c_off" >&2; exit 1; }

# envget KEY  -> value from .env (empty if missing)
envget() {
    "$VENV_PY" - "$1" <<'PY'
import sys
from dotenv import dotenv_values
print(dotenv_values(".env").get(sys.argv[1]) or "")
PY
}

# envset KEY VALUE  -> write/replace KEY in .env
envset() {
    "$VENV_PY" - "$1" "$2" <<'PY'
import sys
from dotenv import set_key
set_key(".env", sys.argv[1], sys.argv[2], quote_mode="always")
PY
    chmod 600 .env
}

server_running() { "${TMUX_CMD[@]}" has-session -t =mcserver 2>/dev/null; }

playit_paths() {
    PLAYITD_BIN=$(envget PLAYITD_BIN); PLAYITD_BIN=${PLAYITD_BIN:-/opt/playit/playitd}
    PLAYIT_CLI_BIN=$(envget PLAYIT_CLI_BIN); PLAYIT_CLI_BIN=${PLAYIT_CLI_BIN:-/opt/playit/agent}
    PLAYIT_DIR="$PWD/playit"
    PLAYIT_SOCK="$PLAYIT_DIR/playitd.sock"
    PLAYIT_SECRET="$PLAYIT_DIR/playit.toml"
}
