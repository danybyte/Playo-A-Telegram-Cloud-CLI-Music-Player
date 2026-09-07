import re

import requests

ITUNES = "https://itunes.apple.com/search"
DEEZER = "https://api.deezer.com/search"
DEEZER_ALBUM = "https://api.deezer.com/album/{id}"
HEADERS = {"User-Agent": "Playo/0.1 (album lookup)"}
TIMEOUT = 6
SINGLE_RE = re.compile(r"[-–(]\s*(single|ep)\s*\)?\s*$", re.I)
COMPIL_RE = re.compile(
    r"greatest|best of|very best|\bhits\b|collection|anthology|box set"
    r"|essential|\bicon\b|platinum|\bgold\b|legacy|the story of"
    r"|motion picture|soundtrack|\bost\b|\bsingles\b|\brarities\b"
    r"|\bclassics\b", re.I)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _clean(name):
    """Drop parenthetical editions/pressings:
    'Megadeth (2LP Limited Red Organza)' -> 'Megadeth'."""
    s = re.sub(r"\([^)]*\)", " ", name or "")
    s = re.sub(r"\s+", " ", s).strip(" -–—·")
    return s or None


def _close(a, b):
    a, b = _norm(a), _norm(b)
    return not a or not b or a in b or b in a


def _pick(rows, title, artist, duration):
    """Choose the ORIGINAL album among exact title+artist matches.

    Compilation/box-set/greatest-hits names are dropped, then the
    EARLIEST release wins — compilations always post-date the original
    studio album ('Take No Prisoners' -> Rust In Peace 1990, never the
    2019 'Warheads On Foreheads' comp that stores rank first).
    '- Single' / '- EP' rows are not albums. When the caller knows the
    real duration, same-named songs of a wildly different length are
    dropped first."""
    cands = []
    for row in rows:
        if _norm(row["track"]) != _norm(title):
            continue
        if not _close(artist, row["artist"]):
            continue
        raw = row["album_raw"]
        if not raw or SINGLE_RE.search(raw):
            continue
        if row["compile"] is True or COMPIL_RE.search(raw):
            continue
        cands.append((_clean(raw), row["date"] or "9999", row["dur"]))
    if not cands:
        return None
    if duration and len(cands) > 1:
        near = [c for c in cands if c[2] and abs(c[2] - duration) <= 60]
        if near:
            cands = near
    cands.sort(key=lambda c: c[1])          # oldest release = original
    return cands[0][0]


def fetch_album(artist, title, duration=None):
    """Real ORIGINAL album for (artist, title). iTunes first, Deezer
    fallback. Returns the cleaned album name or None — a wrong album is
    worse than no album."""
    if not title:
        return None
    q = f"{artist} {title}".strip()
    try:
        r = requests.get(ITUNES, params={"term": q, "entity": "song",
                                         "limit": 25},
                         headers=HEADERS, timeout=TIMEOUT)
        rows = [{"track": it.get("trackName"),
                 "artist": it.get("artistName"),
                 "date": (it.get("releaseDate") or "")[:10],
                 "dur": (it.get("trackTimeMillis") or 0) / 1000,
                 "album_raw": it.get("collectionName"),
                 "compile": False}
                for it in (r.json() or {}).get("results", [])]
        album = _pick(rows, title, artist, duration)
        if album:
            return album
    except Exception:
        pass
    try:
        r = requests.get(DEEZER, params={"q": q, "limit": 25},
                         headers=HEADERS, timeout=TIMEOUT)
        data = (r.json() or {}).get("data", [])
        rows = []
        for d in data:
            alb = d.get("album") or {}
            date, rtype = None, None
            if alb.get("id"):
                # search results carry no dates/type — one lookup per
                # candidate (capped) buys the release date we rank on
                try:
                    full = requests.get(DEEZER_ALBUM.format(id=alb["id"]),
                                        headers=HEADERS,
                                        timeout=TIMEOUT).json()
                    date = (full.get("release_date") or "")[:10]
                    rtype = full.get("record_type")
                except Exception:
                    pass
            if rtype in ("compile", "single", "ep"):
                continue
            rows.append({"track": d.get("title"),
                         "artist": (d.get("artist") or {}).get("name"),
                         "date": date or "9999",
                         "dur": d.get("duration"),
                         "album_raw": alb.get("title"),
                         "compile": rtype == "compile"})
            if len(rows) >= 3:
                break
        album = _pick(rows, title, artist, duration)
        if album:
            return album
    except Exception:
        pass
    return None
