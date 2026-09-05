"""Playo setup — checks prerequisites, installs them if needed, and saves
the Telegram config. Run it once from the project folder:

    python setup.py          (or just double-click setup.bat on Windows)
"""

import importlib.util
import json
import os
import subprocess
import sys

# ---------------------------------------------------------------- constants
PKGS = [
    ("telethon", "telethon>=1.34"),
    ("pygame", "pygame>=2.5"),
    ("mutagen", "mutagen>=1.46"),
    ("requests", "requests>=2.31"),
    ("colorama", "colorama>=0.4.6"),
]

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".playo")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "api_id": 0,
    "api_hash": "",
    "bot_token": "",
    "user_session": True,
    "channel": "",
    "download_dir": os.path.join(os.path.expanduser("~"), "Music"),
    "last_msg_id": 0,
    "auto_sync": True,
    "sort": "title",
    "seek_back": "seconds",
    "volume": 80,
}

CYAN, YELLOW, DIM, RST = "\x1b[36m", "\x1b[33m", "\x1b[2m", "\x1b[0m"

BANNER = r"""
           d8b
           88P
          d88
?88,.d88b,888   d888b8b  ?88   d8P  d8888b
`?88'  ?88?88  d8P' ?88  d88   88  d8P' ?88
  88b  d8P 88b 88b  ,88b ?8(  d88  88b  d88
  888888P'  88b`?88P'`88b`?88P'?8b `?8888P'
  88P'                          )88
 d88                           ,d8P
 ?8P                        `?888P'
"""

# ---------------------------------------------------------------- helpers


def head(msg):
    print(f"\n{CYAN}== {msg} =={RST}")


def ok(msg):
    print(f"  [ok] {msg}")


def warn(msg):
    print(f"  {YELLOW}[!]{RST} {msg}")


def check_python():
    head("Python")
    if sys.version_info < (3, 9):
        warn(f"Python {sys.version.split()[0]} found — 3.9 or newer is required.")
        warn("Install it from https://www.python.org/downloads/ and re-run setup.")
        sys.exit(1)
    ok(f"Python {sys.version.split()[0]}")


def missing_pkgs():
    out = []
    for mod, req in PKGS:
        if importlib.util.find_spec(mod) is None:
            out.append(req)
    return out


def install_packages():
    head("Dependencies")
    missing = missing_pkgs()
    if not missing:
        ok("all packages already installed")
        return
    for req in missing:
        print(f"  installing {req} ...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", req])
        if r.returncode != 0:
            warn(f"could not install {req} — check your internet/pip, then re-run.")
            sys.exit(1)
    ok("all packages installed")


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
            print(f"\n{DIM}existing config found: {CONFIG_FILE}{RST}")
        except Exception:
            pass
    return cfg


def ask(prompt, default):
    suffix = f" [{default}]" if default not in ("", None) else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def configure():
    head("Telegram settings")
    print("  1) api_id / api_hash  ->  https://my.telegram.org  "
          "(API development tools)")
    print("  2) channel            ->  the channel whose music you want"
          " (e.g. @some_music)")

    cfg = load_config()

    while True:
        raw = ask("api_id", cfg.get("api_id") or "")
        if str(raw).isdigit():
            cfg["api_id"] = int(raw)
            break
        warn("api_id must be a number")

    cfg["api_hash"] = ask("api_hash", cfg.get("api_hash"))
    if not cfg["api_hash"]:
        warn("api_hash is required — get it from my.telegram.org")
        sys.exit(1)

    cfg["bot_token"] = ask(
        "bot_token (optional — from @BotFather, empty = personal account)",
        cfg.get("bot_token"))
    if not cfg["bot_token"]:
        cfg["user_session"] = True
        print(f"  {DIM}-> personal-account mode: the first sync asks for"
              f" your phone + Telegram code{RST}")

    cfg["channel"] = ask("channel", cfg.get("channel"))
    if not cfg["channel"]:
        warn("channel is required (e.g. @some_music or -1001234567890)")
        sys.exit(1)

    cfg["download_dir"] = ask("download folder", cfg.get("download_dir"))
    os.makedirs(cfg["download_dir"], exist_ok=True)

    head("Saving")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    ok(f"config saved -> {CONFIG_FILE}")


def main():
    print(f"{CYAN}{BANNER}{RST}")
    print(f"{DIM}        your musics from telegram{RST}")
    check_python()
    install_packages()
    configure()
    head("Done")
    print("  run the player with:")
    print(f"    {CYAN}playo{RST}        (if installed with pip: pip install -e .)")
    print(f"    {CYAN}python -m playo{RST}   (from this folder)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nsetup cancelled.")
        sys.exit(1)
