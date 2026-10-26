#!/usr/bin/env bash
# Updates Paper (same Minecraft version) + plugins + Python packages.
# ./update.sh --upgrade-minecraft  also moves to a newer Minecraft version (back up first).
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

[[ -x $VENV_PY ]] || die "Run ./setup.sh first."
server_running && die "Stop the server first (/stop in Discord)."
"$VENV_PY" -m pip install -q --upgrade -r requirements.txt
"$VENV_PY" installer.py --update "$@"
ok "Updated. Restart ./start-bot.sh if it's running so it picks up new packages."
