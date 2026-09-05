import re

import requests

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "Playo/0.1 (github.com/playo)"}
TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def fetch(artist, title, duration=None):
    """Fetch lyrics; returns (text, synced). Prefers the synced version."""
    if not title:
        return None, False
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
        if not results:
            return None, False
        if duration:
            near = [r for r in results
                    if r.get("duration") and abs(r["duration"] - duration) <= 6]
            if near:
                results = near
        best = next((r for r in results if r.get("syncedLyrics")), results[0])
        text = best.get("syncedLyrics") or best.get("plainLyrics")
        return text, bool(best.get("syncedLyrics"))
    except requests.RequestException:
        return None, False


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
