#!/usr/bin/env bash
# One-time setup for mcbot on Arch Linux. Safe to re-run any time.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

[[ $EUID -ne 0 ]] || die "Run this as your normal user (it calls sudo when needed)."
command -v pacman >/dev/null || die "This script is for Arch Linux (pacman not found)."

step "1/7 System packages"
sudo pacman -S --needed --noconfirm python tmux jre25-openjdk-headless git base-devel
ok "python, tmux, Java 25"

step "2/7 playit.gg agent"
if [[ -x /opt/playit/playitd && -x /opt/playit/agent ]]; then
    ok "playit already installed ($(/opt/playit/agent version 2>/dev/null || echo '?'))"
elif command -v yay >/dev/null; then
    yay -S --needed --noconfirm playit-bin
elif command -v paru >/dev/null; then
    paru -S --needed --noconfirm playit-bin
else
    tmp=$(mktemp -d)
    git clone --depth 1 https://aur.archlinux.org/playit-bin.git "$tmp/playit-bin"
    (cd "$tmp/playit-bin" && makepkg -si --noconfirm)
    rm -rf "$tmp"
fi
# The AUR package turns on a 24/7 system service. mcbot runs playit itself only
# while the server is up, so switch that off. Undo: sudo systemctl unmask playit
sudo systemctl disable --now playit.service >/dev/null 2>&1 || true
sudo systemctl mask playit.service >/dev/null 2>&1 || true
ok "playit ready (system service disabled - mcbot starts/stops its own)"

step "3/7 Python environment"
[[ -x .venv/bin/python ]] || python -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "virtualenv ready (.venv)"

step "4/7 Settings (.env)"
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

# ask KEY "prompt" [secret] [default]
ask() {
    local key=$1 prompt=$2 secret=${3:-} def=${4:-} val
    if [[ -n $(envget "$key") ]]; then ok "$key already set"; return; fi
    if [[ -n $secret ]]; then
        read -rsp "$prompt: " val; echo
    else
        read -rp "$prompt${def:+ [$def]}: " val
    fi
    val=${val:-$def}
    val=${val//[[:space:]]/}
    if [[ -n $val ]]; then envset "$key" "$val"; ok "$key saved"; else warn "$key left empty"; fi
}

echo "${c_dim}Discord Developer Portal: https://discord.com/developers/applications"
echo "You need TWO bot applications: one for this manager bot, one for DiscordSRV (chat bridge)."
echo "Enable Developer Mode in Discord (Settings > Advanced) to copy IDs with right-click.${c_off}"
ask DISCORD_TOKEN "Manager bot token (the /start /stop bot, input hidden)" secret
ask GUILD_ID "Your Discord server ID"
ask ADMIN_USER_IDS "Your Discord user ID (admin commands)"
ask STATUS_CHANNEL_ID "Channel ID for crash/shutdown notices (Enter to skip)"
ask DISCORDSRV_BOT_TOKEN "Chat-bridge bot token (2nd bot, for DiscordSRV, input hidden)" secret
ask DISCORDSRV_CHAT_CHANNEL_ID "Channel ID for Minecraft <-> Discord chat"
ask CONTACT "Contact email/URL (PaperMC's download API asks for one)" "" "$(git config --get user.email 2>/dev/null || true)"

if [[ -n $(envget DISCORD_TOKEN) && $(envget DISCORD_TOKEN) == "$(envget DISCORDSRV_BOT_TOKEN)" ]]; then
    envset DISCORDSRV_BOT_TOKEN ""
    die "Both tokens are the same bot. DiscordSRV needs its own bot application - rerun ./setup.sh."
fi

app_id() {
    "$VENV_PY" - "$1" <<'PY'
import base64, sys
part = sys.argv[1].split(".")[0]
try:
    print(base64.b64decode(part + "=" * (-len(part) % 4)).decode())
except Exception:
    pass
PY
}
mgr_id=$(app_id "$(envget DISCORD_TOKEN)")
dsrv_id=$(app_id "$(envget DISCORDSRV_BOT_TOKEN)")
echo
echo "Invite links (open each once, pick your server):"
[[ -n $mgr_id ]] && echo "  Manager bot : https://discord.com/oauth2/authorize?client_id=$mgr_id&scope=bot+applications.commands&permissions=84992"
[[ -n $dsrv_id ]] && echo "  DiscordSRV  : https://discord.com/oauth2/authorize?client_id=$dsrv_id&scope=bot&permissions=117760"
warn "For the DiscordSRV bot: Developer Portal > Bot > enable 'Server Members' + 'Message Content' intents."

step "5/7 Paper + plugins"
"$VENV_PY" installer.py

step "6/7 playit.gg tunnel"
playit_paths
if [[ -s $PLAYIT_SECRET && -n $(envget PLAYIT_ADDRESS) ]]; then
    ok "playit agent claimed, address: $(envget PLAYIT_ADDRESS)"
else
    ./playit-setup.sh
fi

step "7/7 First boot"
if [[ -d server/world ]]; then
    ok "World already exists - skipping"
else
    read -rp "Boot the server once now to generate the world + plugin configs? [Y/n] " yn
    if [[ ${yn,,} != n* ]]; then
        "$VENV_PY" bot.py --first-boot || warn "First boot had problems - check server/logs/latest.log"
    fi
fi

echo
"$VENV_PY" bot.py --check || true
echo
ok "${c_bold}Setup done.${c_off} Start the bot any time with:  ./start-bot.sh"
