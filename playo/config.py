import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".playo")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CATALOG_FILE = os.path.join(CONFIG_DIR, "catalog.json")

DEFAULTS = {
    "api_id": 0,
    "api_hash": "",
    "bot_token": "",
    "user_session": False,       # True = login with a personal account (no bot)
    "channel": "",               # @username or numeric id like -1001234...
    "download_dir": os.path.join(os.path.expanduser("~"), "Music"),
    "last_msg_id": 0,            # for incremental sync
    "auto_sync": True,           # background sync at startup (every 30s)
    "sort": "title",             # library sort: title | artist | duration | recent
    "seek_back": "seconds",      # ← key: "seconds" (±5s) or "lyric" (prev line)
    "lyrics_pick": False,        # ON = pick from the first 5 lyric results
    "volume": 80,
}


def load():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def session_path(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    return os.path.join(CONFIG_DIR, "playo_session")


def load_catalog():
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_catalog(entries):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
