#!/usr/bin/env python3
"""
mcdiscb - Discord bot that starts/stops a Paper Minecraft server and a
playit.gg tunnel on demand.

Everything runs inside a private tmux server (`tmux -L mcdiscb`), so you can
peek at the consoles any time with ./console.sh.

Run it with ./start-bot.sh. Closing that terminal (or Ctrl+C) stops the
Minecraft server and the tunnel cleanly, then exits the bot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from dotenv import load_dotenv
from mcstatus import JavaServer

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("mcdiscb")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\r")
DONE_RE = re.compile(r"Done \(\d+[.,]\d+s\)!")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# spark's "Memory usage:" line, e.g. "3.1 GB / 12.0 GB   (26%)"
SPARK_MEM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(bytes|KB|MB|GB|TB)\s*/\s*(\d+(?:[.,]\d+)?)\s*(bytes|KB|MB|GB|TB)")
BYTE_UNITS = {"bytes": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# DISCORDSRV_ONLY_CHAT=true: DiscordSRV only bridges chat + join/leave.
# Everything else it would post or change in Discord is switched off on every /start.
DSRV_QUIET_CONFIG = {
    "ServerWatchdogEnabled": "false",                              # "server hasn't ticked" pings
    "ChannelTopicUpdaterChannelTopicsAtShutdownEnabled": "false",  # channel topic edits
    "DiscordChatChannelListCommandEnabled": "false",               # "playerlist" replies
}
DSRV_QUIET_MESSAGES = [  # set to "" = disabled
    "DiscordChatChannelServerStartupMessage", "DiscordChatChannelServerShutdownMessage",
    "ChannelTopicUpdaterChatChannelTopicFormat", "ChannelTopicUpdaterConsoleChannelTopicFormat",
    "ChannelTopicUpdaterChatChannelTopicAtServerShutdownFormat",
    "ChannelTopicUpdaterConsoleChannelTopicAtServerShutdownFormat",
]
DSRV_QUIET_BLOCKS = ["MinecraftPlayerDeathMessage", "MinecraftPlayerAchievementMessage"]  # Enabled: false

SECRET_ENV_KEYS ={"DISCORD_TOKEN", "DISCORDSRV_BOT_TOKEN"}
TMUX_SOCKET = "mcdiscb"
SESSION = "mcserver"
W_MC, W_PLAYITD, W_PLAYIT_UI = "minecraft", "playitd", "playit-ui"

# Fallback JVM flags (Aikar-style G1 tuning). installer.py normally writes
# PaperMC's own recommended flags for your version to server/jvm-flags.txt.
DEFAULT_JVM_FLAGS = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
]


# ───────────────────────────── config ──────────────────────────────

def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int = 0) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[config] {key} must be a whole number, got {raw!r}") from None


def _env_ids(key: str) -> frozenset[int]:
    ids = set()
    for part in re.split(r"[,\s]+", _env(key)):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"[config] {key}: {part!r} is not a Discord ID")
        ids.add(int(part))
    return frozenset(ids)


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _default_java() -> str:
    jdk25 = Path("/usr/lib/jvm/java-25-openjdk/bin/java")
    return str(jdk25) if jdk25.exists() else "java"


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    status_channel_id: int
    command_channel_id: int
    allowed_role_id: int
    admin_ids: frozenset[int]
    server_dir: Path
    java_bin: str
    memory: str
    start_timeout: int
    stop_timeout: int
    stop_countdown: int
    auto_stop_minutes: int
    playit_enabled: bool
    playit_address: str
    playitd_bin: str
    playit_cli_bin: str
    playit_dir: Path
    dsrv_token: str
    dsrv_chat_channel: str
    dsrv_console_channel: str
    dsrv_only_chat: bool

    @classmethod
    def load(cls) -> Config:
        memory = _env("MEMORY", "4G").upper()
        if not re.fullmatch(r"\d+[MG]", memory):
            raise SystemExit(f"[config] MEMORY must look like 4G or 3072M, got {memory!r}")
        for key in ("DISCORDSRV_CHAT_CHANNEL_ID", "DISCORDSRV_CONSOLE_CHANNEL_ID"):
            if _env(key) and not _env(key).isdigit():
                raise SystemExit(f"[config] {key} must be a channel ID (numbers only)")
        return cls(
            token=_env("DISCORD_TOKEN"),
            guild_id=_env_int("GUILD_ID"),
            status_channel_id=_env_int("STATUS_CHANNEL_ID"),
            command_channel_id=_env_int("COMMAND_CHANNEL_ID"),
            allowed_role_id=_env_int("ALLOWED_ROLE_ID"),
            admin_ids=_env_ids("ADMIN_USER_IDS"),
            server_dir=(BASE_DIR / _env("SERVER_DIR", "server")).resolve(),
            java_bin=_env("JAVA_BIN") or _default_java(),
            memory=memory,
            start_timeout=_env_int("START_TIMEOUT", 300),
            stop_timeout=_env_int("STOP_TIMEOUT", 120),
            stop_countdown=_env_int("STOP_COUNTDOWN", 10),
            auto_stop_minutes=_env_int("AUTO_STOP_EMPTY_MINUTES", 0),
            playit_enabled=_env_bool("PLAYIT_ENABLED", True),
            playit_address=_env("PLAYIT_ADDRESS"),
            playitd_bin=_env("PLAYITD_BIN", "/opt/playit/playitd"),
            playit_cli_bin=_env("PLAYIT_CLI_BIN", "/opt/playit/agent"),
            playit_dir=BASE_DIR / "playit",
            dsrv_token=_env("DISCORDSRV_BOT_TOKEN"),
            dsrv_chat_channel=_env("DISCORDSRV_CHAT_CHANNEL_ID"),
            dsrv_console_channel=_env("DISCORDSRV_CONSOLE_CHANNEL_ID"),
            dsrv_only_chat=_env_bool("DISCORDSRV_ONLY_CHAT", True),
        )


# ────────────────────────────── tmux ───────────────────────────────

@dataclass
class Pane:
    window: str
    dead: bool
    status: int | None
    pid: int


class Tmux:
    """Thin async wrapper around a private tmux server (-L mcdiscb)."""

    def __init__(self, socket: str = TMUX_SOCKET, session: str = SESSION,
                 conf: Path = BASE_DIR / "tmux.conf") -> None:
        self.socket, self.session, self.conf = socket, session, conf

    def _t(self, window: str | None = None) -> str:
        return f"={self.session}" if window is None else f"={self.session}:={window}"

    async def run(self, *args: str) -> tuple[int, str, str]:
        conf = ["-f", str(self.conf)] if self.conf.exists() else []
        # The tmux server (and so Java/playit) inherits this env - keep the bot tokens out of it.
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_KEYS and k != "TMUX"}
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-L", self.socket, *conf, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def check(self, *args: str) -> str:
        rc, out, err = await self.run(*args)
        if rc:
            raise RuntimeError(f"tmux {args[0]} failed: {err.strip() or out.strip()}")
        return out

    async def has_session(self) -> bool:
        return (await self.run("has-session", "-t", self._t()))[0] == 0

    async def panes(self) -> dict[str, Pane]:
        rc, out, _ = await self.run(
            "list-panes", "-s", "-t", self._t(),
            "-F", "#{window_name}|#{pane_dead}|#{pane_dead_status}|#{pane_pid}",
        )
        if rc:
            return {}
        panes: dict[str, Pane] = {}
        for line in out.splitlines():
            name, dead, status, pid = (line.rsplit("|", 3) + ["", "", ""])[:4]
            panes[name] = Pane(
                window=name,
                dead=dead == "1",
                status=int(status) if status.lstrip("-").isdigit() else None,
                pid=int(pid) if pid.isdigit() else 0,
            )
        return panes

    async def created_at(self) -> float | None:
        rc, out, _ = await self.run("display-message", "-p", "-t", f"={self.session}:", "#{session_created}")
        return float(out.strip()) if rc == 0 and out.strip().isdigit() else None

    # Commands are passed as argv lists, so tmux execs them directly: no shell
    # parsing (your login shell can be fish) and pane_pid is the real process.
    async def new_session(self, window: str, cwd: Path, argv: list[str]) -> None:
        await self.check("new-session", "-d", "-s", self.session, "-n", window,
                         "-c", str(cwd), "-x", "200", "-y", "50", *argv)

    async def new_window(self, window: str, cwd: Path, argv: list[str]) -> None:
        await self.check("new-window", "-d", "-t", f"={self.session}:", "-n", window, "-c", str(cwd), *argv)

    async def respawn(self, window: str) -> None:
        await self.check("respawn-pane", "-k", "-t", self._t(window))

    async def send_line(self, window: str, text: str) -> None:
        # -l = literal: nothing in `text` is interpreted as a tmux key name.
        await self.check("send-keys", "-t", self._t(window), "-l", "--", text)
        await self.check("send-keys", "-t", self._t(window), "Enter")

    async def send_keys(self, window: str, *keys: str) -> None:
        await self.check("send-keys", "-t", self._t(window), *keys)

    async def capture(self, window: str, lines: int = 30) -> str:
        rc, out, _ = await self.run("capture-pane", "-p", "-J", "-t", self._t(window), "-S", f"-{lines}")
        return out if rc == 0 else ""

    async def kill_session(self) -> None:
        await self.run("kill-session", "-t", self._t())


# ───────────────────────── server manager ──────────────────────────

class State(StrEnum):
    OFFLINE = "offline"
    STARTING = "starting"
    ONLINE = "online"
    STOPPING = "stopping"


class ServerError(Exception):
    pass


Progress = Callable[[str], Awaitable[None]]


async def _no_progress(_: str) -> None:
    return None


def clean_text(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def one_line(text: str, limit: int = 200) -> str:
    """Strip newlines/control chars so user text can never become a second console command."""
    return CTRL_RE.sub(" ", text).strip()[:limit]


# ─────────────────────────── resources ─────────────────────────────

def dir_size(path: Path) -> int:
    """Total size of all files under path (symlinks not followed)."""
    total, stack = 0, [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def memory_bytes(memory: str) -> int:
    """MEMORY from .env ("12G", "3072M") in bytes."""
    num, unit = int(memory[:-1]), memory[-1].upper()
    return num * (1024**3 if unit == "G" else 1024**2)


def proc_cpu_seconds(pid: int) -> float | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")  # utime + stime
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class Resources:
    online: bool
    allocated: int
    worlds: list[tuple[str, int]] = field(default_factory=list)
    ram_used: int | None = None
    cpu: float | None = None
    tps: float | None = None


@dataclass
class Snapshot:
    state: State
    players_online: int | None = None
    players_max: int | None = None
    names: list[str] = field(default_factory=list)
    version: str | None = None
    tunnel: str = "off"
    uptime: float | None = None


class ServerManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tmux = Tmux()
        self.lock = asyncio.Lock()
        self.state = State.OFFLINE
        self.empty_since: float | None = None
        self.tunnel_restarts = 0
        self._abort = asyncio.Event()
        self._spark_cache: tuple[float, dict] | None = None

    # ── paths & commands ──
    @property
    def jar(self) -> Path:
        return self.cfg.server_dir / "paper.jar"

    @property
    def log_file(self) -> Path:
        return self.cfg.server_dir / "logs" / "latest.log"

    @property
    def playit_sock(self) -> Path:
        return self.cfg.playit_dir / "playitd.sock"

    @property
    def playit_secret(self) -> Path:
        return self.cfg.playit_dir / "playit.toml"

    def prop(self, key: str, default: str = "") -> str:
        props = self.cfg.server_dir / "server.properties"
        try:
            for line in props.read_text().splitlines():
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip() or default
        except OSError:
            pass
        return default

    def port(self) -> int:
        try:
            return int(self.prop("server-port", "25565"))
        except ValueError:
            return 25565

    def world_dirs(self) -> list[Path]:
        level = self.prop("level-name", "world")
        base = self.cfg.server_dir / level
        dirs = [base, base.with_name(f"{base.name}_nether"), base.with_name(f"{base.name}_the_end")]
        return [d for d in dirs if d.is_dir()]

    def installed_versions(self) -> dict:
        try:
            return json.loads((self.cfg.server_dir / "mcbot-versions.json").read_text())
        except (OSError, ValueError):
            return {}

    def jvm_flags(self) -> list[str]:
        f = self.cfg.server_dir / "jvm-flags.txt"
        try:
            lines = [ln.split("#", 1)[0] for ln in f.read_text().splitlines()]
            flags = [x for ln in lines for x in ln.split()]
            return flags or DEFAULT_JVM_FLAGS
        except OSError:
            return DEFAULT_JVM_FLAGS

    def mc_command(self) -> list[str]:
        m = self.cfg.memory
        return [self.cfg.java_bin, f"-Xms{m}", f"-Xmx{m}", *self.jvm_flags(), "-jar", "paper.jar", "--nogui"]

    def playitd_command(self) -> list[str]:
        return [
            self.cfg.playitd_bin,
            "--socket-path", str(self.playit_sock),
            "--secret-path", str(self.playit_secret),
            "--log-path", str(self.cfg.playit_dir / "playitd.log"),
        ]

    def playit_ui_command(self) -> list[str]:
        # Wait for the daemon's socket, then show playit's live status UI.
        script = ('i=0; while [ ! -S "$1" ] && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done; '
                  'exec "$2" --socket-path "$1"')
        return ["/bin/sh", "-c", script, "playit-ui", str(self.playit_sock), self.cfg.playit_cli_bin]

    # ── checks ──
    def _have(self, binary: str) -> bool:
        return Path(binary).is_file() or shutil.which(binary) is not None

    def preflight(self, tunnel: bool) -> list[str]:
        problems = []
        if not shutil.which("tmux"):
            problems.append("tmux is not installed (sudo pacman -S tmux)")
        if not self.jar.exists():
            problems.append("server/paper.jar is missing - run ./setup.sh")
        eula = self.cfg.server_dir / "eula.txt"
        if not eula.exists() or "eula=true" not in eula.read_text():
            problems.append("Minecraft EULA not accepted - run ./setup.sh")
        if not self._have(self.cfg.java_bin):
            problems.append(f"Java not found at {self.cfg.java_bin} (sudo pacman -S jre25-openjdk-headless)")
        if tunnel and self.cfg.playit_enabled:
            for b in (self.cfg.playitd_bin, self.cfg.playit_cli_bin):
                if not self._have(b):
                    problems.append(f"playit binary not found: {b} - run ./setup.sh")
            if not self.playit_secret.exists():
                problems.append("playit agent isn't claimed yet - run ./playit-setup.sh")
        return problems

    def apply_plugin_configs(self) -> list[str]:
        """Push .env values into plugin configs. Safe to run every start."""
        changed = []
        dsrv = self.cfg.server_dir / "plugins" / "DiscordSRV" / "config.yml"
        if dsrv.exists() and self.cfg.dsrv_token:
            text = dsrv.read_text()
            new = re.sub(r"(?m)^BotToken:.*$", lambda _: f'BotToken: "{self.cfg.dsrv_token}"', text)
            if self.cfg.dsrv_chat_channel:
                new = re.sub(r"(?m)^Channels:.*$",
                             lambda _: f'Channels: {{"global": "{self.cfg.dsrv_chat_channel}"}}', new)
            if self.cfg.dsrv_console_channel:
                new = re.sub(r"(?m)^DiscordConsoleChannelId:.*$",
                             lambda _: f'DiscordConsoleChannelId: "{self.cfg.dsrv_console_channel}"', new)
            if self.cfg.dsrv_only_chat:
                for key, value in DSRV_QUIET_CONFIG.items():
                    new = re.sub(rf"(?m)^{key}:.*$", lambda _, k=key, v=value: f"{k}: {v}", new)
            if new != text:
                dsrv.write_text(new)
                changed.append("DiscordSRV config")
            dsrv.chmod(0o600)
        dsrv_msgs = dsrv.with_name("messages.yml")
        if dsrv_msgs.exists() and self.cfg.dsrv_only_chat:
            text = new = dsrv_msgs.read_text()
            for key in DSRV_QUIET_MESSAGES:
                new = re.sub(rf"(?m)^{key}:.*$", lambda _, k=key: f'{k}: ""', new)
            for block in DSRV_QUIET_BLOCKS:
                # first "  Enabled:" line inside the block (not the nested Embed one)
                new = re.sub(rf"(?m)^({block}:[ \t]*\n(?:[ \t]+\S.*\n)*?[ \t]{{2}}Enabled:)[ \t]*\w+",
                             r"\g<1> false", new)
            if new != text:
                dsrv_msgs.write_text(new)
                changed.append("DiscordSRV messages (chat + join/leave only)")
        authme = self.cfg.server_dir / "plugins" / "AuthMe" / "config.yml"
        if authme.exists():
            text = authme.read_text()
            # Every player arrives through the playit tunnel from the same local IP,
            # so AuthMe's default "1 registration per IP" would block everyone but one.
            new = re.sub(r"(?m)^(\s*maxRegPerIp:)\s*\d+\s*$", r"\g<1> 0", text)
            if new != text:
                authme.write_text(new)
                changed.append("AuthMe maxRegPerIp=0")
        for c in changed:
            log.info("Updated plugin config: %s", c)
        return changed

    async def ping(self):
        try:
            server = JavaServer("127.0.0.1", self.port(), timeout=2)
            return await asyncio.wait_for(server.async_status(), 4)
        except Exception:
            return None

    def _log_sig(self) -> tuple[int, int] | None:
        try:
            st = self.log_file.stat()
            return st.st_ino, st.st_size
        except OSError:
            return None

    def _log_says_done(self, before: tuple[int, int] | None) -> bool:
        """True once *this* run's log has the "Done (x.xs)!" line.

        Paper rotates latest.log a moment after the JVM starts; until then the
        previous run's file (with its own "Done") is still there, so only read
        what was written after `before` unless the file has been replaced.
        """
        try:
            st = self.log_file.stat()
            offset = before[1] if before and st.st_ino == before[0] and st.st_size >= before[1] else 0
            with self.log_file.open("rb") as f:
                f.seek(max(offset, st.st_size - 65536))
                return bool(DONE_RE.search(f.read().decode(errors="replace")))
        except OSError:
            return False

    def log_tail(self, lines: int = 20) -> str:
        try:
            with self.log_file.open("rb") as f:
                f.seek(max(0, self.log_file.stat().st_size - 65536))
                text = f.read().decode(errors="replace")
            return clean_text("\n".join(text.splitlines()[-lines:]))
        except OSError:
            return ""

    async def _pane_tail(self, window: str, lines: int = 25) -> str:
        text = clean_text(await self.tmux.capture(window, 300))
        keep = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("Pane is dead")]
        return "\n".join(keep[-lines:]) or self.log_tail(lines)

    async def _wait_dead(self, window: str, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pane = (await self.tmux.panes()).get(window)
            if pane is None or pane.dead:
                return True
            await asyncio.sleep(1)
        return False

    @staticmethod
    def _kill(pid: int, sig: int) -> None:
        try:
            if pid > 0:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    async def is_running(self) -> bool:
        pane = (await self.tmux.panes()).get(W_MC)
        return pane is not None and not pane.dead

    async def adopt(self) -> None:
        """On bot start: take over a server that's still running from a previous bot run."""
        panes = await self.tmux.panes()
        mc = panes.get(W_MC)
        if mc and not mc.dead:
            self.state = State.ONLINE
            log.info("Found a running server in tmux - adopting it.")
        elif panes:
            await self.tmux.kill_session()

    def request_abort(self) -> None:
        self._abort.set()

    # ── lifecycle ──
    async def start(self, progress: Progress = _no_progress, tunnel: bool = True,
                    timeout: int | None = None) -> bool:
        """Returns False if it was already running."""
        timeout = timeout or self.cfg.start_timeout
        async with self.lock:
            if await self.is_running():
                self.state = State.ONLINE
                return False
            problems = self.preflight(tunnel)
            if problems:
                raise ServerError("\n".join(f"• {p}" for p in problems))
            await self.tmux.kill_session()  # leftovers from a crash
            self.apply_plugin_configs()
            self._abort.clear()
            self.state = State.STARTING
            self.tunnel_restarts = 0
            started = time.time()
            old_log = self._log_sig()
            try:
                await progress("Launching Paper…")
                await self.tmux.new_session(W_MC, self.cfg.server_dir, self.mc_command())
                if tunnel and self.cfg.playit_enabled:
                    await progress("Starting playit.gg tunnel…")
                    self.cfg.playit_dir.mkdir(mode=0o700, exist_ok=True)
                    self.playit_sock.unlink(missing_ok=True)
                    await self.tmux.new_window(W_PLAYITD, self.cfg.playit_dir, self.playitd_command())
                    await self.tmux.new_window(W_PLAYIT_UI, self.cfg.playit_dir, self.playit_ui_command())
                last_note = 0.0
                while True:
                    elapsed = time.time() - started
                    if self._abort.is_set():
                        raise ServerError("Start aborted because the bot is shutting down.")
                    pane = (await self.tmux.panes()).get(W_MC)
                    if pane is None or pane.dead:
                        tail = await self._pane_tail(W_MC) if pane else self.log_tail()
                        code = pane.status if pane else "?"
                        raise ServerError(f"Paper exited during startup (exit code {code}).\n{tail}")
                    if self._log_says_done(old_log) or await self.ping():
                        break
                    if elapsed > timeout:
                        raise ServerError(f"Server didn't finish starting within {timeout}s.\n{self.log_tail(10)}")
                    if elapsed - last_note >= 10:
                        await progress(f"Loading world… ({int(elapsed)}s)")
                        last_note = elapsed
                    await asyncio.sleep(2)
            except BaseException:
                await self._teardown(graceful=True)
                raise
            self.state = State.ONLINE
            self.empty_since = None
            return True

    async def stop(self, progress: Progress = _no_progress, reason: str = "",
                   countdown: int = 0) -> bool:
        """Returns False if it was already stopped."""
        async with self.lock:
            if not await self.tmux.has_session():
                self.state = State.OFFLINE
                return False
            self.state = State.STOPPING
            try:
                if await self.is_running() and countdown > 0:
                    status = await self.ping()
                    if status and status.players.online > 0:
                        await self._countdown(countdown, reason, progress)
                await progress("Saving world and stopping Paper…")
                await self._stop_minecraft()
                await progress("Closing playit tunnel…")
                await self._stop_tunnel()
            finally:
                await self.tmux.kill_session()
                self.state = State.OFFLINE
                self.empty_since = None
            return True

    async def _countdown(self, seconds: int, reason: str, progress: Progress) -> None:
        marks = sorted({seconds, *(m for m in (30, 10, 5, 3, 2, 1) if m < seconds)}, reverse=True)
        why = f" ({one_line(reason, 80)})" if reason else ""
        for i, m in enumerate(marks):
            msg = f"say Server stopping in {m}s{why}" if i == 0 else f"say Stopping in {m}s..."
            await self.tmux.send_line(W_MC, msg)
            await progress(f"Warning players… stopping in {m}s")
            nxt = marks[i + 1] if i + 1 < len(marks) else 0
            await asyncio.sleep(m - nxt)

    async def _stop_minecraft(self) -> None:
        pane = (await self.tmux.panes()).get(W_MC)
        if pane is None or pane.dead:
            return
        await self.tmux.send_line(W_MC, "stop")
        if await self._wait_dead(W_MC, self.cfg.stop_timeout):
            return
        log.warning("Paper didn't stop in %ss - sending SIGTERM", self.cfg.stop_timeout)
        self._kill(pane.pid, signal.SIGTERM)  # Paper's shutdown hook still saves the world
        if not await self._wait_dead(W_MC, 45):
            log.error("Paper still alive - SIGKILL")
            self._kill(pane.pid, signal.SIGKILL)
            await self._wait_dead(W_MC, 5)

    async def _stop_tunnel(self) -> None:
        pane = (await self.tmux.panes()).get(W_PLAYITD)
        if pane is None or pane.dead:
            return
        await self.tmux.send_keys(W_PLAYITD, "C-c")
        if not await self._wait_dead(W_PLAYITD, 10):
            self._kill(pane.pid, signal.SIGTERM)
            await self._wait_dead(W_PLAYITD, 5)

    async def _teardown(self, graceful: bool) -> None:
        try:
            if graceful:
                pane = (await self.tmux.panes()).get(W_MC)
                if pane and not pane.dead:
                    await self.tmux.send_line(W_MC, "stop")
                    if not await self._wait_dead(W_MC, 45):
                        self._kill(pane.pid, signal.SIGTERM)
                        await self._wait_dead(W_MC, 20)
            await self._stop_tunnel()
        finally:
            await self.tmux.kill_session()
            self.state = State.OFFLINE

    async def health_check(self) -> dict | None:
        """Called by the watchdog every few seconds. Returns an event to announce, if any."""
        if self.lock.locked() or self.state is not State.ONLINE:
            return None
        panes = await self.tmux.panes()
        mc = panes.get(W_MC)
        if mc is None or mc.dead:
            async with self.lock:
                tail = await self._pane_tail(W_MC, 15) if mc else self.log_tail(15)
                await self._stop_tunnel()
                await self.tmux.kill_session()
                self.state = State.OFFLINE
                self.empty_since = None
            return {"kind": "server_down", "code": mc.status if mc else None, "tail": tail}
        if self.cfg.playit_enabled:
            pd = panes.get(W_PLAYITD)
            if pd is None or pd.dead:
                if self.tunnel_restarts >= 3:
                    if self.tunnel_restarts == 3:
                        self.tunnel_restarts += 1
                        return {"kind": "tunnel_dead", "tail": await self._pane_tail(W_PLAYITD, 10)}
                    return None
                self.tunnel_restarts += 1
                self.playit_sock.unlink(missing_ok=True)
                if pd is None:
                    await self.tmux.new_window(W_PLAYITD, self.cfg.playit_dir, self.playitd_command())
                else:
                    await self.tmux.respawn(W_PLAYITD)
                return {"kind": "tunnel_restarted", "code": pd.status if pd else None}
        return None

    async def snapshot(self) -> Snapshot:
        snap = Snapshot(state=self.state)
        if self.state is State.OFFLINE:
            return snap
        panes = await self.tmux.panes()
        if self.cfg.playit_enabled:
            pd = panes.get(W_PLAYITD)
            snap.tunnel = "up" if pd and not pd.dead else "down"
        created = await self.tmux.created_at()
        snap.uptime = time.time() - created if created else None
        if self.state is State.ONLINE:
            status = await self.ping()
            if status:
                snap.players_online = status.players.online
                snap.players_max = status.players.max
                snap.names = sorted(p.name for p in (status.players.sample or []))
                snap.version = status.version.name
        return snap

    async def console(self, command: str, wait: float = 1.5) -> str:
        if self.state is not State.ONLINE:
            raise ServerError("Server is offline.")
        command = one_line(command, 400).lstrip("/")
        if not command:
            raise ServerError("Empty command.")
        try:
            before = self.log_file.stat().st_size
        except OSError:
            before = 0
        await self.tmux.send_line(W_MC, command)
        await asyncio.sleep(wait)
        try:
            with self.log_file.open("rb") as f:
                f.seek(before)
                out = f.read(16384).decode(errors="replace")
        except OSError:
            out = ""
        return clean_text(out) or "(no output)"

    async def _spark_health(self) -> dict:
        """Heap use + TPS from Paper's built-in spark profiler (`spark health show`). Best effort."""
        if self._spark_cache and time.monotonic() - self._spark_cache[0] < 15:
            return self._spark_cache[1]  # don't spam the console when people spam the command
        try:
            before = self.log_file.stat().st_size
        except OSError:
            before = 0
        await self.tmux.send_line(W_MC, "spark health show")
        found: dict = {}
        for _ in range(24):  # spark answers asynchronously; give it up to ~6s
            await asyncio.sleep(0.25)
            try:
                with self.log_file.open("rb") as f:
                    f.seek(before)
                    text = clean_text(f.read(65536).decode(errors="replace"))
            except OSError:
                break
            lines = [ln.split("]: ", 1)[-1] for ln in text.splitlines()]  # drop "[time] [thread/INFO]: "
            for i, line in enumerate(lines[:-1]):
                nxt = lines[i + 1]
                if "TPS from last" in line:
                    nums = re.findall(r"\d+(?:[.,]\d+)?", nxt)
                    if nums:  # 5s, 10s, 1m, 5m, 15m -> use 1m
                        found["tps"] = float(nums[min(2, len(nums) - 1)].replace(",", "."))
                elif "Memory usage" in line and (m := SPARK_MEM_RE.search(nxt)):
                    found["heap_used"] = int(float(m[1].replace(",", ".")) * BYTE_UNITS[m[2]])
                    found["heap_max"] = int(float(m[3].replace(",", ".")) * BYTE_UNITS[m[4]])
            if "heap_used" in found:
                break
        self._spark_cache = (time.monotonic(), found)
        return found

    async def resources(self) -> Resources:
        res = Resources(online=self.state is State.ONLINE, allocated=memory_bytes(self.cfg.memory))

        async def world_sizes() -> None:
            res.worlds = await asyncio.to_thread(lambda: [(d.name, dir_size(d)) for d in self.world_dirs()])

        async def process_stats() -> None:
            pane = (await self.tmux.panes()).get(W_MC)
            if not pane or pane.dead or not pane.pid:
                return
            t0, c0 = time.monotonic(), proc_cpu_seconds(pane.pid)
            await asyncio.sleep(1)
            c1 = proc_cpu_seconds(pane.pid)
            if c0 is not None and c1 is not None:
                res.cpu = (c1 - c0) / (time.monotonic() - t0) * 100 / (os.cpu_count() or 1)

        async def spark() -> None:
            found = await self._spark_health()
            res.ram_used, res.tps = found.get("heap_used"), found.get("tps")

        jobs = [world_sizes()]
        if self.state is not State.OFFLINE:
            jobs.append(process_stats())
        if res.online:
            jobs.append(spark())
        await asyncio.gather(*jobs)
        return res


# ──────────────────────────── discord ──────────────────────────────

GREEN, RED, YELLOW, GREY = 0x3BA55D, 0xED4245, 0xFAA61A, 0x747F8D


def make_embed(title: str, desc: str = "", color: int = GREY) -> discord.Embed:
    return discord.Embed(title=title, description=desc[:4000], color=color)


def code_block(text: str, limit: int = 1500) -> str:
    text = text.replace("```", "'''")
    if len(text) > limit:
        text = "…" + text[-limit:]
    return f"```\n{text}\n```" if text else ""


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_uptime(seconds: float | None) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


class ConfirmStop(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("That button isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stop anyway", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content=None, embed=make_embed("⏳ Stopping…", color=YELLOW), view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled - server keeps running.", embed=None, view=None)
        self.stop()


class McBot(discord.Client):
    def __init__(self, cfg: Config, mgr: ServerManager) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # This bot never joins voice; skip discord.py's "voice not supported" warnings.
        discord.VoiceClient.warn_nacl = False
        if hasattr(discord.VoiceClient, "warn_dave"):
            discord.VoiceClient.warn_dave = False
        super().__init__(intents=intents)
        self.cfg, self.mgr = cfg, mgr
        self.tree = app_commands.CommandTree(self)
        self.last_channel: discord.abc.Messageable | None = None
        self._presence = ""
        register_commands(self)

    async def setup_hook(self) -> None:
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
            log.warning("GUILD_ID not set - global commands can take a while to appear.")
        log.info("Synced %d slash commands.", len(synced))
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def on_ready(self) -> None:
        log.info("Logged in as %s. Commands: /start /stop /restart /status /resources /ip /console /logs", self.user)
        log.info("Close this terminal or press Ctrl+C to stop everything.")

    # ── helpers ──
    def is_admin(self, inter: discord.Interaction) -> bool:
        if inter.user.id in self.cfg.admin_ids:
            return True
        return isinstance(inter.user, discord.Member) and inter.user.guild_permissions.administrator

    def control_denied(self, inter: discord.Interaction) -> str | None:
        if self.is_admin(inter):
            return None
        if self.cfg.command_channel_id and inter.channel_id != self.cfg.command_channel_id:
            return f"Use this in <#{self.cfg.command_channel_id}>."
        if self.cfg.allowed_role_id:
            roles = getattr(inter.user, "roles", [])
            if not any(r.id == self.cfg.allowed_role_id for r in roles):
                return f"You need the <@&{self.cfg.allowed_role_id}> role for that."
        return None

    def address_line(self) -> str:
        if not self.cfg.playit_enabled:
            return "Local/LAN only (playit disabled)"
        return f"`{self.cfg.playit_address}`" if self.cfg.playit_address else "*(PLAYIT_ADDRESS not set in .env)*"

    def mc_version(self) -> str:
        return self.mgr.installed_versions().get("minecraft", "?")

    async def announce(self, embed: discord.Embed) -> None:
        channel = self.get_channel(self.cfg.status_channel_id) if self.cfg.status_channel_id else None
        channel = channel or self.last_channel
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Couldn't post announcement")

    async def set_presence(self, text: str, status: discord.Status) -> None:
        if text == self._presence:
            return
        self._presence = text
        try:
            await self.change_presence(status=status, activity=discord.Game(text))
        except Exception:
            pass

    async def _watchdog(self) -> None:
        await self.wait_until_ready()
        tick = 0
        while not self.is_closed():
            try:
                event = await self.mgr.health_check()
                if event:
                    await self._announce_event(event)
                if tick % 6 == 0:
                    await self._presence_and_autostop()
            except Exception:
                log.exception("watchdog error")
            tick += 1
            await asyncio.sleep(5)

    async def _announce_event(self, event: dict) -> None:
        kind = event["kind"]
        if kind == "server_down":
            code = event.get("code")
            if code == 0:
                e = make_embed("🔴 Server stopped", "Paper shut down on its own (someone ran `stop` in-game "
                               "or in the console). Tunnel closed too. Use `/start` to bring it back.", RED)
            else:
                e = make_embed("💥 Server crashed", f"Paper exited with code `{code}`. Tunnel closed. "
                               "Use `/start` to try again.\n" + code_block(event.get("tail", ""), 900), RED)
            log.warning("Server went down (exit code %s)", code)
        elif kind == "tunnel_restarted":
            e = make_embed("⚠️ playit tunnel restarted", "The tunnel process died and was restarted. "
                           "Players may have been disconnected briefly.", YELLOW)
        else:
            e = make_embed("⚠️ playit tunnel keeps crashing", "Gave up restarting it. Run `./console.sh playitd` "
                           "on the host to see why.\n" + code_block(event.get("tail", ""), 600), RED)
        await self.announce(e)

    async def _presence_and_autostop(self) -> None:
        if self.mgr.state is not State.ONLINE:
            label = {State.STARTING: "Starting…", State.STOPPING: "Stopping…"}.get(self.mgr.state, "Offline · /start")
            await self.set_presence(label, discord.Status.idle)
            return
        status = await self.mgr.ping()
        online = status.players.online if status else 0
        await self.set_presence(f"Online · {online}/{status.players.max if status else '?'} players",
                                discord.Status.online)
        mins = self.cfg.auto_stop_minutes
        if mins <= 0 or not status:
            return
        if online > 0:
            self.mgr.empty_since = None
            return
        self.mgr.empty_since = self.mgr.empty_since or time.time()
        if time.time() - self.mgr.empty_since >= mins * 60 and not self.mgr.lock.locked():
            log.info("Server empty for %s min - auto-stopping", mins)
            await self.mgr.stop(reason="auto-stop: empty")
            await self.announce(make_embed("💤 Auto-stopped", f"Nobody was online for {mins} minutes.", GREY))


def register_commands(bot: McBot) -> None:
    tree, mgr, cfg = bot.tree, bot.mgr, bot.cfg

    async def run_start(inter: discord.Interaction) -> None:
        async def progress(text: str) -> None:
            await inter.edit_original_response(embed=make_embed(f"⏳ {text}", color=YELLOW))

        try:
            fresh = await mgr.start(progress)
        except ServerError as e:
            await inter.edit_original_response(embed=make_embed("❌ Couldn't start the server", str(e)[:1800], RED))
            return
        title = "🟢 Server is online!" if fresh else "🟢 Server was already online"
        e = make_embed(title, f"**Join:** {bot.address_line()}\n**Version:** Java Edition {bot.mc_version()}\n"
                       "New here? You'll be asked to register a password when you join (offline-mode auth).",
                       GREEN)
        await inter.edit_original_response(embed=e)
        if fresh:
            log.info("Server started by %s", inter.user)

    async def run_stop(inter: discord.Interaction, reason: str) -> None:
        async def progress(text: str) -> None:
            await inter.edit_original_response(content=None, embed=make_embed(f"⏳ {text}", color=YELLOW), view=None)

        stopped = await mgr.stop(progress, reason=reason, countdown=cfg.stop_countdown)
        e = make_embed("🔴 Server stopped" if stopped else "Server was already offline",
                       "World saved, tunnel closed." if stopped else "", RED if stopped else GREY)
        await inter.edit_original_response(content=None, embed=e, view=None)
        if stopped:
            log.info("Server stopped by %s", inter.user)

    async def busy(inter: discord.Interaction) -> bool:
        if mgr.lock.locked():
            await inter.response.send_message(f"Busy - the server is currently **{mgr.state.value}**. "
                                              "Try again in a moment.", ephemeral=True)
            return True
        return False

    @tree.command(name="start", description="Start the Minecraft server and playit tunnel")
    @app_commands.guild_only()
    async def start_cmd(inter: discord.Interaction) -> None:
        if (msg := bot.control_denied(inter)) or await busy(inter):
            if msg:
                await inter.response.send_message(msg, ephemeral=True)
            return
        bot.last_channel = inter.channel
        await inter.response.defer(thinking=True)
        await run_start(inter)

    @tree.command(name="stop", description="Save the world and stop the server + tunnel")
    @app_commands.guild_only()
    async def stop_cmd(inter: discord.Interaction) -> None:
        if (msg := bot.control_denied(inter)) or await busy(inter):
            if msg:
                await inter.response.send_message(msg, ephemeral=True)
            return
        bot.last_channel = inter.channel
        if mgr.state is State.OFFLINE and not await mgr.tmux.has_session():
            await inter.response.send_message("Server is already offline.", ephemeral=True)
            return
        snap = await mgr.snapshot()
        if snap.players_online and not bot.is_admin(inter):
            names = ", ".join(snap.names) or f"{snap.players_online} player(s)"
            view = ConfirmStop(inter.user.id)
            await inter.response.send_message(f"**{snap.players_online}** online right now ({names}). Stop anyway?",
                                              view=view)
            timed_out = await view.wait()
            if not view.confirmed:
                if timed_out:
                    await inter.edit_original_response(content="No answer - stop cancelled.", view=None)
                return
        else:
            await inter.response.defer(thinking=True)
        await run_stop(inter, reason=f"/stop by {inter.user.display_name}")

    @tree.command(name="restart", description="Restart the server (stop, then start)")
    @app_commands.guild_only()
    async def restart_cmd(inter: discord.Interaction) -> None:
        if (msg := bot.control_denied(inter)) or await busy(inter):
            if msg:
                await inter.response.send_message(msg, ephemeral=True)
            return
        bot.last_channel = inter.channel
        await inter.response.defer(thinking=True)

        async def progress(text: str) -> None:
            await inter.edit_original_response(embed=make_embed(f"⏳ {text}", color=YELLOW))

        await mgr.stop(progress, reason=f"restart by {inter.user.display_name}", countdown=cfg.stop_countdown)
        await run_start(inter)

    @tree.command(name="status", description="Is the server up? Who's online?")
    @app_commands.guild_only()
    async def status_cmd(inter: discord.Interaction) -> None:
        await inter.response.defer()
        snap = await mgr.snapshot()
        color = {State.ONLINE: GREEN, State.OFFLINE: RED}.get(snap.state, YELLOW)
        e = make_embed(f"Minecraft server: {snap.state.value}", color=color)
        if snap.state is State.ONLINE:
            players = "—" if snap.players_online is None else f"{snap.players_online}/{snap.players_max}"
            e.add_field(name="Players", value=players)
            e.add_field(name="Uptime", value=fmt_uptime(snap.uptime))
            e.add_field(name="Tunnel", value=snap.tunnel)
            if snap.names:
                e.add_field(name="Online now", value=discord.utils.escape_markdown(", ".join(snap.names))[:1000],
                            inline=False)
            e.set_footer(text=snap.version or f"Java Edition {bot.mc_version()}")
        elif snap.state is State.OFFLINE:
            e.description = "Use `/start` to launch it."
        await inter.followup.send(embed=e)

    @tree.command(name="resources", description="RAM, CPU and world size of the Minecraft server")
    @app_commands.guild_only()
    async def resources_cmd(inter: discord.Interaction) -> None:
        await inter.response.defer()
        r = await mgr.resources()
        e = make_embed("Server resources", color=GREEN if r.online else GREY)
        allocated = fmt_bytes(r.allocated)
        if r.online:
            if r.ram_used is not None:
                ram = f"{fmt_bytes(r.ram_used)} / {allocated} ({r.ram_used * 100 // r.allocated}%)"
            else:
                ram = f"? / {allocated} (couldn't read usage)"
            e.add_field(name="RAM", value=ram)
            if r.cpu is not None:
                e.add_field(name="CPU", value=f"{r.cpu:.0f}% of {os.cpu_count()} threads")
            if r.tps is not None:
                e.add_field(name="TPS (1m)", value=f"{r.tps:.1f} / 20")
        else:
            e.description = "Server is offline - RAM and CPU usage show up after `/start`."
            e.add_field(name="RAM", value=f"{allocated} allocated")
        if r.worlds:
            e.add_field(name="World size", value=fmt_bytes(sum(size for _, size in r.worlds)), inline=False)
        else:
            e.add_field(name="World size", value="No world yet - it's created on the first `/start`.", inline=False)
        await inter.followup.send(embed=e)

    @tree.command(name="ip", description="Show the server address")
    @app_commands.guild_only()
    async def ip_cmd(inter: discord.Interaction) -> None:
        e = make_embed("Server address", f"{bot.address_line()}\nJava Edition **{bot.mc_version()}**\n"
                       f"Currently **{mgr.state.value}**" + (" - use `/start`" if mgr.state is State.OFFLINE else ""))
        await inter.response.send_message(embed=e)

    @tree.command(name="console", description="(Admin) Run a server console command")
    @app_commands.describe(command="e.g. list, op Steve, whitelist add Steve, authme unregister Steve")
    @app_commands.guild_only()
    async def console_cmd(inter: discord.Interaction, command: str) -> None:
        if not bot.is_admin(inter):
            await inter.response.send_message("Admins only.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            out = await mgr.console(command)
        except ServerError as e:
            await inter.followup.send(str(e), ephemeral=True)
            return
        log.info("%s ran console command: %s", inter.user, one_line(command))
        await inter.followup.send(f"`> {one_line(command, 100)}`\n" + code_block(out, 1800), ephemeral=True)

    @tree.command(name="logs", description="(Admin) Show the last lines of the server log")
    @app_commands.describe(lines="How many lines (default 20)")
    @app_commands.guild_only()
    async def logs_cmd(inter: discord.Interaction, lines: app_commands.Range[int, 1, 60] = 20) -> None:
        if not bot.is_admin(inter):
            await inter.response.send_message("Admins only.", ephemeral=True)
            return
        await inter.response.send_message(code_block(mgr.log_tail(lines), 1900) or "(log is empty)", ephemeral=True)

    @tree.error
    async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Command error", exc_info=error)
        msg = f"Something went wrong: `{type(getattr(error, 'original', error)).__name__}` (details in bot.log)"
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ───────────────────────────── main ────────────────────────────────

def setup_logging() -> None:
    logging.raiseExceptions = False  # never crash if the terminal is gone (SIGHUP)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(BASE_DIR / "logs" / "bot.log", maxBytes=1_000_000,
                                                        backupCount=3)
    stream = logging.StreamHandler(sys.stdout)
    for h in (file_handler, stream):
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [file_handler, stream]
    logging.getLogger("discord.http").setLevel(logging.WARNING)


async def run_bot(cfg: Config) -> int:
    if not cfg.token:
        log.error("DISCORD_TOKEN is empty in .env - run ./setup.sh first.")
        return 1
    mgr = ServerManager(cfg)
    await mgr.adopt()
    bot = McBot(cfg, mgr)

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    hits = 0

    def on_signal(sig: signal.Signals) -> None:
        nonlocal hits
        hits += 1
        if hits == 1:
            log.info("Got %s - stopping the server safely, please wait…", sig.name)
            mgr.request_abort()
            shutdown.set()
        elif hits == 2:
            log.warning("Still saving the world. Press Ctrl+C once more to force-quit "
                        "(the server would keep running in tmux).")
        else:
            os._exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, on_signal, sig)

    async def connect() -> None:
        # Right after boot the network may not be up yet: wait instead of crashing.
        warned = False
        while True:
            try:
                await loop.getaddrinfo("discord.com", 443)
                break
            except OSError:
                if not warned:
                    log.warning("Can't reach discord.com yet - waiting for the network…")
                    warned = True
                await asyncio.sleep(5)
        await bot.start(cfg.token)

    bot_task = asyncio.create_task(connect())
    wait_task = asyncio.create_task(shutdown.wait())
    done, _ = await asyncio.wait({bot_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if bot_task in done and bot_task.exception():
        err = bot_task.exception()
        if isinstance(err, discord.LoginFailure):
            log.error("Discord rejected DISCORD_TOKEN - check .env.")
        else:
            log.error("Bot crashed: %r", err)
        exit_code = 1

    if await mgr.tmux.has_session():
        if bot.is_ready():
            await bot.announce(make_embed("🟡 Host is shutting down", "Saving the world and stopping the server…",
                                          YELLOW))
        await mgr.stop(reason="host shutting down", countdown=min(cfg.stop_countdown, 5))
        log.info("Server and tunnel stopped.")
        if bot.is_ready():
            await bot.announce(make_embed("🔴 Server stopped", "The host went offline.", RED))
            await bot.set_presence("Offline · host is off", discord.Status.idle)

    wait_task.cancel()
    await bot.close()
    if not bot_task.done():
        bot_task.cancel()
    log.info("Bye.")
    return exit_code


async def first_boot(cfg: Config) -> int:
    """Boot once (no tunnel) so Paper and the plugins generate their files, then stop."""
    mgr = ServerManager(cfg)
    if await mgr.tmux.has_session():
        print("The server is already running - stop it first.")
        return 1

    async def progress(text: str) -> None:
        print(f"  … {text}", flush=True)

    print("First boot: generating world + plugin configs (can take a few minutes)…")
    try:
        await mgr.start(progress, tunnel=False, timeout=max(cfg.start_timeout, 600))
    except ServerError as e:
        print(f"\n✗ Server failed to start:\n{e}")
        return 1
    print("  … Server came up, stopping it again.")
    await mgr.stop(progress)
    changed = mgr.apply_plugin_configs()
    try:
        log_text = mgr.log_file.read_text(errors="replace")
    except OSError:
        log_text = ""
    ok = True
    for plugin in ("DiscordSRV", "AuthMe"):
        loaded = f"Enabling {plugin}" in log_text
        ok &= loaded
        print(f"  {'✓' if loaded else '✗'} {plugin} {'loaded' if loaded else 'NOT loaded - check server/logs/latest.log'}")
    for c in changed:
        print(f"  ✓ configured {c}")
    if not cfg.dsrv_token:
        print("  ! DISCORDSRV_BOT_TOKEN is empty - Discord chat bridge won't connect until you set it.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--first-boot", action="store_true", help="boot once to generate configs, then stop")
    parser.add_argument("--check", action="store_true", help="validate config and files, then exit")
    args = parser.parse_args()

    cfg = Config.load()
    if args.first_boot:
        logging.basicConfig(level=logging.WARNING)
        return asyncio.run(first_boot(cfg))
    if args.check:
        problems = ServerManager(cfg).preflight(tunnel=True)
        if not cfg.token:
            problems.insert(0, "DISCORD_TOKEN is empty")
        print("\n".join(f"✗ {p}" for p in problems) or "✓ Everything looks ready.")
        return 1 if problems else 0
    setup_logging()
    return asyncio.run(run_bot(cfg))


if __name__ == "__main__":
    sys.exit(main())
