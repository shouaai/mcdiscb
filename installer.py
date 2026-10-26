#!/usr/bin/env python3
"""
Downloads/updates Paper + plugins for mcbot.

  fresh install : picks the newest *stable* Paper version that DiscordSRV and
                  AuthMe both support (or MC_VERSION from .env)
  --update      : keeps your current Minecraft version, updates the Paper build
                  and plugins only (safe for your world)
  --upgrade-minecraft : also move to a newer Minecraft version (one-way for worlds!)

Run through ./setup.sh or ./update.sh rather than directly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

SERVER = (BASE / (os.getenv("SERVER_DIR") or "server")).resolve()
PLUGINS_DIR = SERVER / "plugins"
MANIFEST = SERVER / "mcbot-versions.json"
FILL = "https://fill.papermc.io/v3/projects/paper"
MODRINTH = "https://api.modrinth.com/v2"
CONTACT = (os.getenv("CONTACT") or "").strip() or "no-contact-set"
UA = f"mcbot-setup/1.0 (self-hosted Paper server manager; contact: {CONTACT})"


@dataclass(frozen=True)
class Plugin:
    slug: str      # Modrinth project slug
    name: str
    required: bool


PLUGINS = [
    Plugin("discordsrv", "DiscordSRV", True),
    Plugin("authmereloaded", "AuthMeReloaded", True),
    # Optional ones are skipped (with a warning) if there's no build for your version yet.
    Plugin("packetevents", "PacketEvents", False),    # lets AuthMe hide inventories before login
    Plugin("skinsrestorer", "SkinsRestorer", False),  # real skins on an offline-mode server
]

FALLBACK_FLAGS = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
]


def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"\n✗ {msg}", file=sys.stderr)
    sys.exit(1)


# ───────────────────────────── http ────────────────────────────────

def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        die(f"HTTP {e.code} from {url}")
    except urllib.error.URLError as e:
        die(f"Network error talking to {urllib.parse.urlparse(url).netloc}: {e.reason}")


def download(url: str, dest: Path, sha256: str | None = None, sha512: str | None = None) -> None:
    tmp = dest.with_name(dest.name + ".part")
    h256, h512 = hashlib.sha256(), hashlib.sha512()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h256.update(chunk)
                h512.update(chunk)
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        die(f"Download failed ({url}): {e}")
    if (sha256 and h256.hexdigest() != sha256.lower()) or (sha512 and h512.hexdigest() != sha512.lower()):
        tmp.unlink(missing_ok=True)
        die(f"Checksum mismatch for {dest.name} - download corrupted or tampered with. Try again.")
    tmp.replace(dest)


# ───────────────────────── version logic ───────────────────────────

def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def family(v: str) -> str:
    return ".".join(v.split(".")[:2])


def paper_versions() -> list[str]:
    data = get_json(FILL) or {}
    raw = data.get("versions", {})
    flat = [v for group in raw.values() for v in group] if isinstance(raw, dict) else list(raw)
    return sorted({v for v in flat if re.fullmatch(r"\d+(\.\d+)+", v)}, key=vkey, reverse=True)


def paper_build(version: str, allow_experimental: bool) -> dict | None:
    data = get_json(f"{FILL}/versions/{version}/builds")
    builds = data if isinstance(data, list) else (data or {}).get("builds", [])
    builds = sorted(builds, key=lambda b: b.get("id", 0), reverse=True)
    channels = {"STABLE", "RECOMMENDED"} | ({"BETA", "ALPHA", "EXPERIMENTAL"} if allow_experimental else set())
    for b in builds:
        if str(b.get("channel", "")).upper() in channels and "server:default" in b.get("downloads", {}):
            return b
    return None


def paper_java_info(version: str) -> tuple[int, list[str]]:
    data = get_json(f"{FILL}/versions/{version}") or {}
    java = (data.get("version") or {}).get("java") or data.get("java") or {}
    minimum = int(((java.get("version") or {}).get("minimum")) or 25)
    flags = list(((java.get("flags") or {}).get("recommended")) or [])
    return minimum, flags


_plugin_cache: dict[str, list[dict]] = {}


def plugin_versions(slug: str) -> list[dict]:
    if slug not in _plugin_cache:
        q = urllib.parse.urlencode({"loaders": json.dumps(["paper"])})
        vs = get_json(f"{MODRINTH}/project/{slug}/version?{q}") or []
        _plugin_cache[slug] = sorted(vs, key=lambda v: v.get("date_published", ""), reverse=True)
    return _plugin_cache[slug]


def jar_plugin_name(path: Path) -> str | None:
    """The `name:` a plugin jar declares in plugin.yml / paper-plugin.yml."""
    try:
        with zipfile.ZipFile(path) as z:
            for meta in ("paper-plugin.yml", "plugin.yml"):
                if meta in z.namelist():
                    for line in z.read(meta).decode(errors="replace").splitlines():
                        m = re.match(r"name:\s*['\"]?([^'\"#\s]+)", line)
                        if m:
                            return m[1].lower()
    except (OSError, zipfile.BadZipFile, KeyError):
        pass
    return None


def move_duplicates(keep: Path) -> None:
    """Move other jars of the same plugin (e.g. one you added by hand) out of the way:
    Paper refuses to load two copies of one plugin."""
    name = jar_plugin_name(keep)
    if not name:
        return
    for other in PLUGINS_DIR.glob("*.jar"):
        if other != keep and jar_plugin_name(other) == name:
            dest = PLUGINS_DIR / ".replaced"
            dest.mkdir(exist_ok=True)
            other.replace(dest / other.name)
            say(f"  moved duplicate {other.name} to plugins/.replaced/")


def pick_plugin(slug: str, mc: str) -> dict | None:
    def supports(v: dict) -> bool:
        gvs = set(v.get("game_versions", []))
        return mc in gvs or family(mc) in gvs

    candidates = [v for v in plugin_versions(slug) if supports(v)]
    for wanted in ("release", "beta"):
        for v in candidates:
            if v.get("version_type") == wanted:
                return v
    return None


def choose_version(requested: str, allow_experimental: bool) -> tuple[str, dict]:
    required = [p for p in PLUGINS if p.required]
    if requested and requested != "auto":
        build = paper_build(requested, allow_experimental)
        if not build:
            die(f"No stable Paper build for {requested}. (Use --allow-experimental to accept alpha/beta builds.)")
        missing = [p.name for p in required if not pick_plugin(p.slug, requested)]
        if missing:
            die(f"{', '.join(missing)} has no release for Minecraft {requested} yet. "
                "Pick another MC_VERSION in .env or set MC_VERSION=auto.")
        return requested, build

    say("Looking for the newest Paper version that has a stable build AND plugin support…")
    for v in paper_versions()[:12]:
        build = paper_build(v, allow_experimental)
        if not build:
            say(f"  {v:<9} skip - no stable Paper build yet")
            continue
        missing = [p.name for p in required if not pick_plugin(p.slug, v)]
        if missing:
            say(f"  {v:<9} skip - no {', '.join(missing)} release for it yet")
            continue
        say(f"  {v:<9} ✓")
        return v, build
    die("Couldn't find a Paper version supported by all required plugins.")
    raise AssertionError  # unreachable


# ─────────────────────────── java / flags ──────────────────────────

def java_bin() -> str:
    jb = (os.getenv("JAVA_BIN") or "").strip()
    if jb:
        return jb
    jdk25 = Path("/usr/lib/jvm/java-25-openjdk/bin/java")
    return str(jdk25) if jdk25.exists() else "java"


def java_major(java: str) -> int | None:
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30).stderr
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)', out)
    return int(m.group(1)) if m else None


def flags_work(java: str, flags: list[str]) -> bool:
    try:
        r = subprocess.run([java, "-Xms1G", "-Xmx1G", *flags, "-version"], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def write_jvm_flags(java: str, recommended: list[str]) -> None:
    clean = [f for f in recommended if not re.match(r"-Xm[sx]", f)]
    for label, flags in (("PaperMC-recommended", clean), ("Aikar fallback", FALLBACK_FLAGS),
                         ("minimal", ["-XX:+UseG1GC"])):
        if flags and flags_work(java, flags):
            (SERVER / "jvm-flags.txt").write_text(f"# {label} JVM flags (memory comes from MEMORY in .env)\n"
                                                  + "\n".join(flags) + "\n")
            say(f"✓ JVM flags: {label}")
            return
    say("! Couldn't validate JVM flags with your Java - is Java 25 installed?")


# ─────────────────────────── server files ──────────────────────────

def set_properties(path: Path, force: dict[str, str], defaults: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else ["#Minecraft server properties"]
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in force:
            lines[i] = f"{key}={force[key]}"
            seen.add(key)
        elif key in defaults:
            seen.add(key)
    for key, val in {**defaults, **force}.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n")


def handle_eula(accept: bool) -> None:
    eula = SERVER / "eula.txt"
    if eula.exists() and "eula=true" in eula.read_text():
        return
    if not accept:
        say("\nMinecraft's EULA: https://aka.ms/MinecraftEULA")
        try:
            answer = input("Do you agree to the Minecraft EULA? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            die("You need to accept the EULA to run a Minecraft server.")
    stamp = dt.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
    eula.write_text(f"#Accepted via mcbot setup on {stamp}\neula=true\n")
    say("✓ EULA accepted")


def server_running() -> bool:
    return subprocess.run(["tmux", "-L", "mcbot", "has-session", "-t", "=mcserver"],
                          capture_output=True).returncode == 0


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


# ────────────────────────────── main ───────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="update Paper build + plugins, keep MC version")
    ap.add_argument("--upgrade-minecraft", action="store_true", help="allow moving to a newer MC version")
    ap.add_argument("--allow-experimental", action="store_true", help="accept alpha/beta Paper builds")
    ap.add_argument("--accept-eula", action="store_true")
    args = ap.parse_args()

    if CONTACT == "no-contact-set":
        say("! CONTACT is empty in .env - PaperMC asks API users for an email/URL in the User-Agent.")
    if server_running():
        die("The server is running. Stop it with /stop (or close the bot) first.")

    SERVER.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(exist_ok=True)
    manifest = load_manifest()
    installed_mc = manifest.get("minecraft")

    requested = (os.getenv("MC_VERSION") or "auto").strip()
    if installed_mc and not args.upgrade_minecraft:
        if requested not in ("auto", installed_mc):
            say(f"! MC_VERSION={requested} but this world runs {installed_mc}. "
                f"Keeping {installed_mc}; use ./update.sh --upgrade-minecraft to move.")
        requested = installed_mc
    mc, build = choose_version(requested, args.allow_experimental)
    if installed_mc and mc != installed_mc:
        if vkey(mc) < vkey(installed_mc):
            die(f"Refusing to downgrade {installed_mc} → {mc}; that corrupts worlds.")
        say(f"\n⚠ Upgrading Minecraft {installed_mc} → {mc}. Worlds can't be downgraded afterwards.")
        say("  Make a backup of the server/world* folders first if you care about them.")
        try:
            answer = input("  Continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            die("Cancelled.")

    # Paper
    dl = build["downloads"]["server:default"]
    jar = SERVER / "paper.jar"
    if manifest.get("paper_build") == build.get("id") and manifest.get("minecraft") == mc and jar.exists():
        say(f"✓ Paper {mc} build {build.get('id')} already installed")
    else:
        say(f"↓ Paper {mc} build {build.get('id')} ({build.get('channel')})")
        if jar.exists():
            jar.replace(SERVER / "paper.jar.bak")
        download(dl["url"], jar, sha256=(dl.get("checksums") or {}).get("sha256"))

    # Plugins
    new_plugins: dict[str, dict] = {}
    for p in PLUGINS:
        v = pick_plugin(p.slug, mc)
        if not v:
            if p.required:
                die(f"No {p.name} release for {mc}")
            say(f"- {p.name}: no build for {mc} yet, skipping (optional)")
            continue
        files = v.get("files", [])
        f = next((x for x in files if x.get("primary")), files[0] if files else None)
        if not f:
            die(f"{p.name} {v.get('version_number')} has no downloadable file")
        old = (manifest.get("plugins") or {}).get(p.slug, {})
        target = PLUGINS_DIR / f["filename"]
        if old.get("file") == f["filename"] and target.exists():
            say(f"✓ {p.name} {v.get('version_number')} already installed")
        else:
            say(f"↓ {p.name} {v.get('version_number')}")
            download(f["url"], target, sha512=(f.get("hashes") or {}).get("sha512"))
            if old.get("file") and old["file"] != f["filename"]:
                (PLUGINS_DIR / old["file"]).unlink(missing_ok=True)
        move_duplicates(target)
        new_plugins[p.slug] = {"version": v.get("version_number"), "file": f["filename"]}

    # Java
    java = java_bin()
    minimum, flags = paper_java_info(mc)
    major = java_major(java)
    if major is None:
        say(f"! Couldn't run {java}. Install Java {minimum}: sudo pacman -S jre{minimum}-openjdk-headless")
    elif major < minimum:
        say(f"! {java} is Java {major}, but Paper {mc} needs Java {minimum}+.\n"
            f"  sudo pacman -S jre{minimum}-openjdk-headless  (then set JAVA_BIN in .env if needed)")
    else:
        say(f"✓ Java {major} at {java}")
    if major:
        write_jvm_flags(java, flags)

    # server.properties: offline mode for AuthMe, keep everything else as-is
    set_properties(SERVER / "server.properties",
                   force={"online-mode": "false", "enforce-secure-profile": "false"},
                   defaults={"motd": "A Discord-managed Paper server", "server-port": "25565",
                             "max-players": "20", "spawn-protection": "0"})
    say("✓ server.properties (online-mode=false)")

    handle_eula(args.accept_eula)

    MANIFEST.write_text(json.dumps({
        "minecraft": mc, "paper_build": build.get("id"), "plugins": new_plugins,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=2) + "\n")
    say(f"\n✓ Server files ready: Paper {mc} + " + ", ".join(
        f"{k} {v['version']}" for k, v in new_plugins.items()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Cancelled.")
