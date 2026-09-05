import os
from dataclasses import dataclass

from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".wma"}


@dataclass
class Track:
    path: str
    title: str
    artist: str
    duration: float  # seconds
    msg_id: int = 0          # telegram message id (0 = local-only file)
    downloaded: bool = True


def _tags(path):
    title = artist = ""
    duration = 0.0
    try:
        m = MutagenFile(path, easy=True)
        if m is not None:
            duration = float(m.info.length)
            title = (m.get("title") or [""])[0].strip()
            artist = (m.get("artist") or [""])[0].strip()
    except Exception:
        pass
    return title, artist, duration


def _from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem


def scan(directory):
    tracks = []
    if not os.path.isdir(directory):
        return tracks
    for root, _dirs, files in os.walk(directory):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fn)
            title, artist, duration = _tags(path)
            if not title:
                a, t = _from_filename(path)
                title, artist = t, artist or a
            tracks.append(Track(path, title, artist, duration))
    tracks.sort(key=lambda t: os.path.basename(t.path).lower())
    return tracks
