import hashlib
import os
import re
import time

import requests

from . import config as cfgmod

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "Playo/0.1 (github.com/playo)"}
TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# every found lyric (user-picked OR auto-picked) is saved here, so the
# next play of the same song needs no network at all
LYRICS_DIR = os.path.join(cfgmod.CONFIG_DIR, "lyrics")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _cache_file(artist, title, ext):
    """Readable, Windows-safe name, disambiguated by a stable hash."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "",
                  f"{artist or 'Unknown'} - {title or 'Unknown'}").strip(" .")
    h = hashlib.sha1(f"{_norm(artist)}|{_norm(title)}".encode("utf-8")
                     ).hexdigest()[:10]
    return os.path.join(LYRICS_DIR, f"{(base or 'lyrics')[:80]}.{h}{ext}")


def load_cache(artist, title):
    """Saved lyrics for this track — (text, synced), or (None, False)."""
    for ext, synced in (".lrc", True), (".txt", False):
        try:
            with open(_cache_file(artist, title, ext), encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                return text, synced
        except OSError:
            continue
    return None, False


def save_cache(artist, title, text, synced):
    if not (text or "").strip():
        return
    try:
        os.makedirs(LYRICS_DIR, exist_ok=True)
        ext = ".lrc" if synced else ".txt"
        with open(_cache_file(artist, title, ext), "w",
                  encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def clear_cache(artist, title):
    """Delete saved lyrics for one track — True if a file was removed."""
    removed = False
    for ext in (".lrc", ".txt"):
        try:
            os.remove(_cache_file(artist, title, ext))
            removed = True
        except OSError:
            pass
    return removed


def clear_all_cache():
    """Wipe the whole lyric cache — returns files removed."""
    n = 0
    try:
        for name in os.listdir(LYRICS_DIR):
            try:
                os.remove(os.path.join(LYRICS_DIR, name))
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def search_all(artist, title, duration=None, limit=5):
    """Raw LRCLIB candidates:
    [(artist, track, album, dur, text, synced), ...] — source order."""
    if not title:
        return []
    try:
        results = requests.get(
            f"{API}/search",
            params={"track_name": title, "artist_name": artist or ""},
            headers=HEADERS, timeout=10,
        ).json() or []
        if not results and artist:
            results = requests.get(
                f"{API}/search", params={"q": f"{artist} {title}"},
                headers=HEADERS, timeout=10,
            ).json() or []
        out = []
        for r in results[:limit]:
            text = r.get("syncedLyrics") or r.get("plainLyrics")
            if text:
                out.append((r.get("artistName") or artist or "",
                            r.get("trackName") or title,
                            r.get("albumName") or "",
                            float(r.get("duration") or 0),
                            text, bool(r.get("syncedLyrics"))))
        return out
    except requests.RequestException:
        return []


def _by_duration(cands, duration):
    """Durations within ±6 s of the track float to the top."""
    if not duration:
        return cands
    near = [c for c in cands if c[3] and abs(c[3] - duration) <= 6]
    far = [c for c in cands if c not in near]
    return near + far


def fetch(artist, title, duration=None):
    """Fetch lyrics; returns (text, synced). Prefers the synced version.
    Cache first (~/.playo/lyrics) — LRCLIB is hit only for unknown tracks,
    and every find is written back to the cache."""
    if not title:
        return None, False
    text, synced = load_cache(artist, title)
    if text:
        return text, synced
    cands = _by_duration(search_all(artist, title, duration, limit=20),
                         duration)
    if not cands:
        return None, False
    best = next((c for c in cands if c[5]), cands[0])
    save_cache(artist, title, best[4], best[5])
    return best[4], best[5]


def parse_lrc(text):
    """LRC text -> [(ms, line), ...] sorted by time."""
    out = []
    for line in (text or "").splitlines():
        times = TIME_RE.findall(line)
        if not times:
            continue
        content = TIME_RE.sub("", line).strip(" -–—")
        for m, s, frac in times:
            ms = int(m) * 60000 + int(s) * 1000 + int((frac or "0").ljust(3, "0")[:3])
            out.append((ms, content))
    out.sort(key=lambda x: x[0])
    return out
