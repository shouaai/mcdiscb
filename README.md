# mcdiscb

Discord bot that runs a **Paper 26.x** server + **playit.gg** tunnel on your Arch box, on demand.

```
./start-bot.sh  ──►  bot online in Discord
/start          ──►  Paper + playit start (in a private tmux)
/stop           ──►  world saved, Paper + playit stop, bot stays up
close terminal  ──►  same as /stop, then the bot exits
```

Included: **DiscordSRV** (Discord ⇄ Minecraft chat), **AuthMe Reloaded** (passwords for offline mode),
**SkinsRestorer** (real skins on an offline-mode server), PacketEvents (lets AuthMe hide inventories before login)

## Setup (once)

1. **Two Discord bot apps** at <https://discord.com/developers/applications> → *New Application* → *Bot* → *Reset Token*:
   - **Manager bot** - the one with `/start` `/stop`. No intents needed.
   - **Chat bridge bot** - for DiscordSRV. Turn on **Server Members** + **Message Content** intents.
   Keep them separate: DiscordSRV manages its own bot's slash commands and can overwrite the manager's.
2. Discord → Settings → Advanced → **Developer Mode** on (right-click → *Copy ID*).
3. A free account at <https://playit.gg>.

```bash
git clone https://github.com/shouai/mcdiscb.git ~/mcdiscb
cd ~/mcdiscb && ./setup.sh
```

Update the bot code later with `cd ~/mcdiscb && git pull`. Your `.env`, world, playit claim and logs are
git-ignored, so pulls never touch them.

`setup.sh` installs Java 25 / tmux / playit (AUR `playit-bin`), makes a Python venv, asks for tokens + IDs,
prints invite links for both bots, downloads Paper + plugins (checksum-verified), claims the playit agent,
asks for your tunnel address, and boots the server once to generate the world + configs. Re-run it any time.

## Daily use

```bash
~/mcdiscb/start-bot.sh          # fish shortcut: alias --save mcdiscb ~/mcdiscb/start-bot.sh
```

| Command | Who | What |
|---|---|---|
| `/start` | everyone* | Start Paper + tunnel, posts the join address when ready |
| `/stop` | everyone* | In-game countdown, save, stop both. Non-admins confirm if players are on |
| `/restart` | everyone* | Stop then start |
| `/status` | everyone | Online/offline, players, uptime, tunnel |
| `/resources` | everyone | RAM used out of the allocated `MEMORY`, CPU, TPS, world size |
| `/ip` | everyone | Join address + Minecraft version |
| `/console <cmd>` | admins | Run a console command, shows output (`op Steve`, `whitelist add …`) |
| `/logs [lines]` | admins | Tail `latest.log` |

\* restrict with `ALLOWED_ROLE_ID` / `COMMAND_CHANNEL_ID` in `.env`. Admins = `ADMIN_USER_IDS` + Discord server admins.

Extras: crash detection (posts the error), auto-restart of a dead tunnel, optional
`AUTO_STOP_EMPTY_MINUTES`, adopts a still-running server if the bot is restarted.

## Scripts

| File | Purpose |
|---|---|
| `setup.sh` | One-time install / repair |
| `start-bot.sh` | Run the bot (refuses to start twice) |
| `console.sh [minecraft\|playit\|playitd]` | Watch the live consoles. Detach: `Ctrl+b d` |
| `update.sh` | Update Paper build + plugins (same MC version). `--upgrade-minecraft` to jump versions |
| `playit-setup.sh [--reset]` | Re-claim the playit agent / change tunnel address |
| `.env` | All settings (chmod 600, holds tokens - never committed) |

## How it works

- Everything runs in a private tmux server (`tmux -L mcdiscb`), so your own tmux is untouched.
- playit runs as **your user** (`playit/playitd.sock`, secret in `playit/playit.toml`), only while the
  server is up. `setup.sh` disables + masks the AUR package's 24/7 system service.
  Undo: `sudo systemctl unmask playit && sudo systemctl enable --now playit`.
- Bot tokens are stripped from the environment before Java/playit start.
- Logs: `logs/bot.log`, `server/logs/latest.log`, `playit/playitd.log`.

## Troubleshooting

- `./start-bot.sh --check` validates files and settings.
- **Slash commands missing** → set `GUILD_ID`, restart the bot, re-invite with the `applications.commands` link.
- **Players can't connect** → `./console.sh playit` should show the tunnel running; tunnel must point to `127.0.0.1:25565`.
- **DiscordSRV silent** → chat-bridge bot needs both intents + access to the channel; see `/logs`.
- **Crash on start about Java version** → `sudo pacman -S jre25-openjdk-headless` or set `JAVA_BIN`.
- Autostart the bot with Hyprland: `exec-once = kitty -e ~/mcdiscb/start-bot.sh` (any terminal works).
