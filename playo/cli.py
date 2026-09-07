import bisect
import os
import random
import shlex
import shutil
import sys
import threading
import time

from colorama import just_fix_windows_console

from . import config as cfgmod, lyrics as lyrics_mod, metadata as metadata_mod, telegram_sync
from .library import scan, Track
from .player import Player

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

TAGLINE = "your musics from telegram"

HELP = """
  sync              index the channel into the catalog (no downloads)
  watch             live-index new channel posts (Ctrl+C to quit)
                    (auto: runs in the background at startup)
  list | ls [q]     list catalog + downloaded songs (with search)
  play [n|query]    play — downloads on demand if needed
  pause / resume    pause / resume
  stop              stop completely
  next / prev       next / previous
  seek <s|+s|-s>    seek (e.g. seek 90 or +15)
  vol [0-100]       volume
  lyrics [live]     show lyrics (live = live sync)
  now               current playback status
  shuffle           toggle shuffle on/off
  sort [key]        sort library: title | artist | duration | recent
  ui | tui          full-screen player — control everything with the keyboard
  set <key> <val>   change setting (channel, download_dir, bot_token,
                     user_session, auto_sync)
  open              open the download folder in Explorer
  setup             initial setup (guided)
  rescan            rescan the folder
  help              this help
  exit | quit       quit
"""


def fmt(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def bar(pos, dur, w=30):
    if dur <= 0:
        return "·" * w
    pos = min(pos, dur)
    return "█" * int(w * pos / dur) + "·" * (w - int(w * pos / dur))


def volbar(v, w=12):
    v = max(0, min(100, int(v)))
    f = int(w * v / 100)
    return "█" * f + "░" * (w - f)


class PlayoApp:
    def __init__(self):
        self.cfg = cfgmod.load()
        self.tracks = []
        self.index = None          # index of current song in self.tracks
        self.shuffle = bool(self.cfg.get("shuffle", False))
        self.order = []            # playback order (identity when shuffle off)
        self.lrc = None            # [(ms, line)]
        self.lrc_plain = None
        self.lrc_synced = False
        self.lrc_key = None        # (title, artist) whose lyrics are loaded
        self.lrc_album = None      # album name for the now-playing bar
        self._album_key = None     # (title, artist) whose album is loaded
        self._album_fails = {}     # lookup back-off: key -> retry-after
        self.player = Player(volume=self.cfg.get("volume", 80) / 100.0)
        self.player.on_end = self._auto_next
        self.player.on_stall = self._stream_stall
        self._stream_ctx = None       # (gen, state, part) of the live stream
        self._auto_advance = False    # True while the queue picks the track
        self._auto_fails = 0          # consecutive failed auto-advances
        self._sync_lock = threading.Lock()
        self._auto_stop = threading.Event()
        self._auto_thread = None
        self.catalog = cfgmod.load_catalog()   # songs seen in the channel (no files)
        self._dl_status = None                 # title while a TUI download runs
        self._dl_error = None
        self._dl_progress = None               # (cur_bytes, total_bytes)
        self._stream_dest = None
        self._notes = []                       # thread-safe UI notifications
        self._tui_note = None
        self.rescan()

    # ---------- library ----------
    def rescan(self):
        cur = self.current.path if self.current else None
        old_paths = [t.path for t in self.tracks]
        old_order = list(self.order)
        dd = self.cfg["download_dir"]
        cat_by = {}
        for e in self.catalog:
            cat_by[os.path.normcase(os.path.join(dd, e["file"]))] = e
        tracks = []
        seen = set()
        for t in scan(dd):
            key = os.path.normcase(t.path)
            seen.add(key)
            e = cat_by.get(key)
            if e:
                # a local file only counts as downloaded when it is complete;
                # keep msg_id either way so 'recent' sort stays correct
                fsize = e.get("size") or 0
                complete = (fsize <= 0) or os.path.getsize(t.path) >= fsize * 0.98
                t = Track(t.path, t.title, t.artist, t.duration,
                          msg_id=e["msg_id"], downloaded=complete)
            tracks.append(t)
        for key, e in cat_by.items():
            if key in seen:
                continue
            dest = os.path.join(dd, e["file"])
            base, ext = os.path.splitext(e["file"])
            # duplicate filenames exist in the channel — make cloud paths unique
            n = 2
            dest_u = dest
            while os.path.normcase(dest_u) in {os.path.normcase(t.path)
                                               for t in tracks}:
                dest_u = os.path.join(dd, f"{base} ({n}){ext}")
                n += 1
            tracks.append(Track(dest_u,
                                e.get("title") or os.path.splitext(e["file"])[0],
                                e.get("artist") or "",
                                float(e.get("duration") or 0),
                                msg_id=e["msg_id"], downloaded=False))
        self.tracks = tracks
        self.apply_sort()
        self.index = None
        if cur:
            c = os.path.normcase(cur)
            for k, t in enumerate(self.tracks):
                if os.path.normcase(t.path) == c:
                    self.index = k
                    break
        # preserve the play order across rescans (a shuffle queue must
        # never reshuffle just because a rescan happened). On the FIRST
        # scan there is nothing to preserve — old_order is empty — and
        # blindly rebuilding here would ERASE the shuffle apply_sort just
        # made (shuffle flag ON but playback walks the library in order)
        if old_order:
            new_pos = {os.path.normcase(t.path): k
                       for k, t in enumerate(self.tracks)}
            order = [new_pos[os.path.normcase(old_paths[k])] for k in old_order
                     if k < len(old_paths)
                     and os.path.normcase(old_paths[k]) in new_pos]
            seen = set(order)
            order += [k for k in range(len(self.tracks)) if k not in seen]
            self.order = order

    SORTS = ("title", "artist", "duration", "recent")

    DEFAULT_KEYS = {
        "search": "tab", "settings": "f3",
        "next": "n", "prev": "p", "shuffle": "s",
        "sort": "y", "lyrics": "v", "mute": "m",
    }

    def keys(self):
        """Effective key bindings (config overrides, "" = unbound).

        One-character overrides are ignored: printable keys belong to
        search, so stale letter bindings from old configs can never
        shadow the defaults."""
        k = dict(self.DEFAULT_KEYS)
        for a, v in (self.cfg.get("keys") or {}).items():
            if v and not (len(v) == 1 and v.isprintable()):
                k[a] = v
            elif len(v) == 1:
                k[a] = ""                       # dead binding — unbound
        return k

    def apply_sort(self):
        """Sort self.tracks by the configured key; keep the playing track
        AND the current selection anchored to the same tracks."""
        key = self.cfg.get("sort", "title")
        cur = self.current.path if self.current else None
        base = lambda t: os.path.basename(t.path).lower()
        if key == "artist":
            f = lambda t: ((t.artist or "~").lower(), t.title.lower())
        elif key == "duration":
            f = lambda t: (t.duration, t.title.lower())
        elif key == "recent":
            f = lambda t: (0 if t.msg_id else 1, -t.msg_id, base(t))
        else:
            f = lambda t: (t.title.lower(), (t.artist or "").lower())
        self.tracks.sort(key=f)
        self.index = None
        if cur:
            c = os.path.normcase(cur)
            for k, t in enumerate(self.tracks):
                if os.path.normcase(t.path) == c:
                    self.index = k
                    break
        self.rebuild_order()
        # tell the view where the playing track landed (selection anchor)
        self._sort_stamp = getattr(self, "_sort_stamp", 0) + 1

    def cycle_sort(self):
        i = self.SORTS.index(self.cfg.get("sort", "title"))
        self.cfg["sort"] = self.SORTS[(i + 1) % len(self.SORTS)]
        cfgmod.save(self.cfg)
        self.apply_sort()
        return self.cfg["sort"]

    # ---------- catalog ----------
    def notify(self, text):
        """Queue a message for the active UI (printed in the REPL instead)."""
        if getattr(self, "_tui", False):
            self._notes.append((time.time(), text))
        else:
            print(f"  {DIM}{text}{RST}")

    def _catalog_by_msg(self, msg_id):
        for e in self.catalog:
            if e["msg_id"] == msg_id:
                return e
        return None

    def _catalog_add(self, entry):
        if self._catalog_by_msg(entry["msg_id"]):
            return False
        self.catalog.append(entry)
        cfgmod.save_catalog(self.catalog)
        self.rescan()
        self.notify(f"+ {entry['title']}  — ready to play")
        return True

    def _merge_catalog(self, entries):
        changed = False
        for e in entries:
            if not self._catalog_by_msg(e["msg_id"]):
                self.catalog.append(e)
                changed = True
        if changed:
            cfgmod.save_catalog(self.catalog)
        self.rescan()
        return changed

    def rebuild_order(self, preserve=False):
        """Build the play order. preserve=True keeps the existing sequence
        for surviving tracks and appends new ones — rescans must NOT
        reshuffle an active shuffle queue."""
        self.order = list(range(len(self.tracks)))
        if self.shuffle and not preserve:
            random.shuffle(self.order)
            if self.index is not None and self.index in self.order:
                self.order.remove(self.index)
                self.order.insert(0, self.index)

    def _find(self, q):
        """Space-insensitive search: 'song4' matches 'Song 4'.

        Typing has no space key (Space = play/pause), so the query is
        matched with spaces squeezed out on both sides."""
        q = q.lower().replace(" ", "")
        if not q:
            return list(range(len(self.tracks)))
        return [i for i, t in enumerate(self.tracks)
                if q in t.title.lower().replace(" ", "")
                or q in t.artist.lower().replace(" ", "")
                or q in os.path.basename(t.path).lower().replace(" ", "")]

    @property
    def current(self):
        if self.index is None or self.index >= len(self.tracks):
            return None
        return self.tracks[self.index]

    # ---------- playback ----------
    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self.cfg["shuffle"] = self.shuffle
        cfgmod.save(self.cfg)
        self.rebuild_order()
        return self.shuffle

    def _step(self, direction=1):
        """Next/prev respecting shuffle order; returns track index or None."""
        if not self.tracks:
            return None
        if self.index is None or self.index not in self.order:
            return self.order[0]
        pos = self.order.index(self.index)
        return self.order[(pos + direction) % len(self.order)]

    def set_volume(self, v):
        self.player.set_volume(v)
        self.cfg["volume"] = int(self.player.volume * 100)
        cfgmod.save(self.cfg)

    # ---------- download on demand ----------
    @staticmethod
    def _dl_progress(name, cur, tot):
        pct = int(cur * 100 / tot) if tot else 0
        sys.stdout.write(f"\r  ↓ {name[:44]:<44} {pct:3d}%")
        sys.stdout.flush()
        if cur >= tot:
            sys.stdout.write("\n")

    def _download_blocking(self, entries, progress=None):
        """All user-session Telethon work goes through _sync_lock — one
        session file, one client at a time (SQLite locks otherwise)."""
        try:
            with self._sync_lock:
                dl = self._bot_only()
                return telegram_sync.download_entries(self.cfg, entries,
                                                      progress, None, dl=dl)
        except telegram_sync.LoginRequired:
            self.notify("personal login needed — add it from settings"
                        " (add_account) or run: sync")
            return []

    def play_path(self, path):
        for j, t in enumerate(self.tracks):
            if os.path.normcase(t.path) == os.path.normcase(path):
                self.play_index(j)
                return

    def play_index(self, i, auto=False):
        auto_pending = auto or getattr(self, "_auto_advance", False)
        self._auto_advance = False
        self._stream_gen = getattr(self, "_stream_gen", 0) + 1
        prev = getattr(self, "_stream_cancel", None)
        if prev:
            prev.set()                             # cancel any in-flight stream
        self._stream_ctx = None
        self._dl_status = None
        self._dl_progress = None
        if not self.tracks:
            if not getattr(self, "_tui", False):
                print("Catalog is empty — waiting for channel posts (or run sync).")
            return
        i %= len(self.tracks)
        tr = self.tracks[i]
        if not tr.downloaded and tr.msg_id:
            entry = self._catalog_by_msg(tr.msg_id)
            if not entry:
                if auto_pending:
                    self._stream_fail_retry(self._stream_gen)
                return
            if not getattr(self, "_tui", False):
                print(f"{DIM}↓ {tr.title} — downloading...{RST}")
            try:
                self._download_blocking([entry],
                                        None if getattr(self, "_tui", False)
                                        else self._dl_progress)
            except KeyboardInterrupt:
                if not getattr(self, "_tui", False):
                    print("\nCancelled.")
                return
            except Exception as e:
                if not getattr(self, "_tui", False):
                    print(f"Download failed: {e}")
                if auto_pending:
                    self._stream_fail_retry(self._stream_gen)
                return
            self.rescan()
            self.play_path(tr.path)          # re-locate and play the file on disk
            if self.player.state != "playing" and auto_pending:
                self._stream_fail_retry(self._stream_gen)
            return
        try:
            self.player.load(tr.path)
        except Exception as e:
            if not getattr(self, "_tui", False):
                print(f"Error playing {tr.title}: {e}")
            else:
                self._dl_error = f"load: {e}"
            if auto_pending:
                self._stream_fail_retry(self._stream_gen)
            return
        self.index = i
        self._auto_fails = 0
        self._reset_lyrics()
        self._dl_error = None
        if not getattr(self, "_tui", False):
            print(f"{CYAN}▶ {tr.title}  —  {tr.artist or 'unknown'}{RST}")

    def play_async(self, i):
        """TUI: download in a background thread, then play.

        Interrupts any in-flight stream/download — the user's pick always
        wins, so next/prev/Enter always respond instantly."""
        auto = getattr(self, "_auto_advance", False)
        self._auto_advance = False
        if i is None or not self.tracks or i >= len(self.tracks):
            return
        tr = self.tracks[i]
        if tr.downloaded or not tr.msg_id:
            self.play_index(i, auto=auto)
            return
        entry = self._catalog_by_msg(tr.msg_id)
        if not entry:
            if auto:
                self._stream_fail_retry(self._stream_gen)
            return
        self.play_streaming(i, entry, auto=auto)

    def play_streaming(self, i, entry, auto=False):
        """Play the song while it downloads, Windows-safe.

        The downloader appends to <file>.part (lock-free); the mixer plays a
        separate <file>.play copy and 'hops' to a fresh copy whenever playback
        nears the copied edge. A new selection cancels the in-flight stream
        (generation counter + cancel event); stale workers exit silently."""
        tr = self.tracks[i]
        self._stream_gen = getattr(self, "_stream_gen", 0) + 1
        gen = self._stream_gen
        # cancel the previous stream's download (frees the dl session)
        prev = getattr(self, "_stream_cancel", None)
        if prev:
            prev.set()
        cancel = threading.Event()
        self._stream_cancel = cancel
        # NOW PLAYING switches to the target track IMMEDIATELY (no waiting
        # for the buffer): the user sees the new title + live download
        # percent instead of the old song sitting there
        self.player.stop()
        self.index = i
        self._reset_lyrics()
        self.player.state = "buffering"
        self._dl_status = tr.title
        self._dl_progress = (0, entry.get("size") or 0)
        self._dl_error = None
        dest = os.path.join(self.cfg["download_dir"], entry["file"])
        part = dest + ".part"
        app = self
        state = {"started": False, "copied": 0, "hopping": False}
        self._stream_ctx = (gen, state, part)

        def on_progress(cur, tot):
            if app._stream_gen != gen:
                return
            app._dl_progress = (cur, tot)
            app._stream_tick(part, dest, i, state, cur, tot)

        def worker():
            try:
                # user mode: the auto-sync poller shares the SAME Telethon
                # session file — serialize Telethon usage or SQLite dies
                # with 'database is locked' and the stream never starts
                self._sync_lock.acquire()
                try:
                    path = telegram_sync.download_streaming(
                        self.cfg, entry, on_progress, cancel=cancel)
                finally:
                    self._sync_lock.release()
                if app._stream_gen != gen:
                    return                     # user picked another song
                if path is None:
                    app._stream_ctx = None     # cancelled
                    return
                if state["started"]:
                    # seamless finish: release the mixer (frees .play),
                    # promote the .part, reload the final file in place
                    if path.endswith(".part"):
                        # the mixer holds .play, not .part — the rename is
                        # safe while playing; retry: AV/indexer handles can
                        # hold the fresh file briefly on Windows
                        for _ in range(6):
                            try:
                                os.replace(path, dest)
                                break
                            except OSError as e:
                                app._dl_error = f"promote: {e}"
                                time.sleep(0.4)
                    if app._stream_gen != gen:
                        return        # ended / re-picked during the waits
                    # capture AFTER the retries — the song kept playing
                    # while they ran, so an earlier stamp would rewind it
                    pos_ms = app.player.position_ms()
                    app.player.stop()
                    app.rescan()
                    for j, t in enumerate(app.tracks):
                        if os.path.normcase(t.path) == os.path.normcase(dest):
                            app.index = j
                            break
                    # if the promotion failed, play the complete .part —
                    # going silent here killed the queue in the past
                    src = dest if os.path.exists(dest) else path
                    app.player.load_at(src, pos_ms / 1000)
                    app.player.suppress_end = False
                    app._stream_ctx = None
                    try:
                        pf = getattr(app, "_stream_playfile", None)
                        if pf and os.path.exists(pf):
                            os.remove(pf)
                    except OSError:
                        pass
                else:
                    if path.endswith(".part"):
                        try:
                            os.replace(path, dest)
                        except OSError as e:
                            app._dl_error = f"promote: {e}"
                    app.rescan()
                    app.play_path(dest)
                    if app.player.state != "playing" and auto:
                        # play_path silently bailed after an auto-advance —
                        # keep the queue moving (play_path bumped the gen,
                        # so pass the CURRENT one or the timer bails)
                        app._stream_fail_retry(app._stream_gen)
                    app._stream_ctx = None
            except Exception as e:
                if app._stream_gen != gen:
                    return
                app._dl_error = str(e)
                # never leave the player stuck in 'buffering' — playback
                # never started, so show a plain stopped state
                if not state["started"]:
                    app.player.state = "stopped"
                app._stream_ctx = None
                import traceback
                from . import config as _c
                try:
                    with open(os.path.join(_c.CONFIG_DIR, "error.log"), "a",
                              encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]"
                                f" stream: {e!r}\n{traceback.format_exc()}\n")
                except Exception:
                    pass
                # auto-advance picked this track and the stream died —
                # keep the queue moving instead of going silent
                app._stream_fail_retry(gen, auto)
            finally:
                if app._stream_gen == gen:
                    app._dl_status = None
                    app._dl_progress = None

        threading.Thread(target=worker, daemon=True).start()

    def _stream_tick(self, part, dest, i, state, cur, tot):
        """Progress callback: start playback early, hop before the edge."""
        if not state["started"]:
            self._maybe_start_stream(part, dest, i, state)
            return
        if state.get("hopping") or self.player.state not in ("playing", "paused",
                                                             "buffering"):
            return
        try:
            part_size = os.path.getsize(part)
        except OSError:
            return
        tr = self.tracks[i] if i is not None and i < len(self.tracks) else None
        dur = tr.duration if tr else 0
        pos_sec = self.player.position_ms() / 1000
        if dur > 0 and tot > 0:
            buffered = (state["copied"] / tot) * dur - pos_sec
            need = buffered < 10                    # < 10 s of audio ahead
        else:
            need = (part_size - state["copied"]) < 512 * 1024
        if need or (tot > 0 and part_size >= tot):
            self._stream_hop(part, state)

    def _stream_hop(self, part, state):
        """Swap to a fresh copy of the grown download, resume seamlessly."""
        if state.get("hopping") or not getattr(self, "_stream_playfile", None):
            return
        state["hopping"] = True
        try:
            pos_ms = self.player.position_ms()
            self.player.freeze()
            shutil.copyfile(part, self._stream_playfile)
            state["copied"] = os.path.getsize(self._stream_playfile)
            self.player.load_at(self._stream_playfile, pos_ms / 1000)
            self.player.suppress_end = True
        except Exception as e:
            import traceback
            self._dl_error = f"hop: {e}"
            try:
                from . import config as _c
                with open(os.path.join(_c.CONFIG_DIR, "error.log"), "a",
                          encoding="utf-8") as f:
                    f.write(f"[hop] {e!r}\n{traceback.format_exc()}\n")
            except Exception:
                pass
        finally:
            state["hopping"] = False

    def _maybe_start_stream(self, part, dest, i, state):
        """Start playback once the .part file holds enough audio.

        pygame rejects a partially-written MP3 while its header/tags are
        incomplete, so we retry as the file grows until it parses."""
        if state["started"] or self.player.state in ("playing", "paused"):
            return
        size = 0
        try:
            if not os.path.exists(part):
                return
            size = os.path.getsize(part)
            if size < 768 * 1024:
                return
            playfile = dest + ".play"
            shutil.copyfile(part, playfile)       # mixer plays the copy
            self._stream_playfile = playfile
            self.player.load(playfile)
            self.player.suppress_end = True
            self._auto_fails = 0
            # index was already set at pick time; keep it pointed at the
            # streaming track (a rescan in between could have moved it)
            for j, t in enumerate(self.tracks):
                if os.path.normcase(t.path) == os.path.normcase(dest):
                    self.index = j
                    break
            # lyrics were already fetched while buffering — do NOT reset them
            state["started"] = True
            state["copied"] = os.path.getsize(playfile)
        except Exception as e:
            # header not parsable yet — wait for more bytes and retry
            last = getattr(self, "_stream_retry_size", 0)
            if size >= last + 512 * 1024:
                self._stream_retry_size = size
                self._dl_error = None
            if size > 8 * 1024 * 1024:
                self._dl_error = f"stream start: {e}"

    def _auto_next(self):
        i = self._step(1)
        if i is None:
            return
        tr = self.tracks[i]
        if getattr(self, "_tui", False):
            self._auto_advance = True
            self.play_async(i)
            return
        if not tr.downloaded and tr.msg_id:
            print(f"{DIM}[auto] ↓ next: {tr.title} — downloading...{RST}")
        self._auto_advance = True
        self.play_index(i)

    def _stream_fail_retry(self, gen, auto):
        """Auto-advance hit a dead track (download failure, corrupt file).

        Move to the NEXT track after a short pause — bounded, so a broken
        catalog can't spin the whole queue in a loop. Manual picks never
        come here."""
        if not auto:
            return
        app = self

        def retry():
            if app._stream_gen != gen:
                return
            app._auto_fails += 1
            if app._auto_fails > 5:
                app.notify("auto-advance: too many failures in a row — stopped")
                app._auto_fails = 0
                return
            app._auto_next()

        t = threading.Timer(2.0, retry)
        t.daemon = True
        t.start()

    def _stall_giveup(self, gen):
        """The stream never produced more audio after a stall — end the
        track like a normal finish so the queue keeps moving."""
        if gen != self._stream_gen or self.player.state != "buffering":
            return                     # resumed or the user picked something
        self._stream_ctx = None
        self._auto_next()

    def _stream_stall(self):
        """Mixer ran dry mid-stream (suppressed end): the buffer caught up
        with the download — resume when more audio lands — or the song is
        really over — advance to the next track. on_end never fires while
        suppress_end is set, so this is the only way the queue moves on."""
        ctx = getattr(self, "_stream_ctx", None)
        if ctx is None or ctx[0] != self._stream_gen:
            # stream bookkeeping is gone (worker failed/cleared): the
            # track is over — old code sat in 'stopped' until n was pressed
            self._stream_ctx = None
            self._auto_next()
            return
        gen, state, part = ctx
        tot = self._dl_progress[1] if self._dl_progress else 0
        tr = self.current
        dur = tr.duration if tr else 0
        pos_sec = self.player.position_ms() / 1000
        if tot > 0 and dur > 0:
            buffered = (state["copied"] / tot) * dur
            remaining = dur - pos_sec
            if buffered < remaining - 1.0:
                # more audio is still coming — wait for the downloader;
                # _stream_tick hops and resumes as chunks land. If nothing
                # lands, the give-up timer ends the track instead of
                # hanging in 'buffering' forever
                t = threading.Timer(12.0, self._stall_giveup, args=(gen,))
                t.daemon = True
                t.start()
                return
        # the rest of the song is (nearly) downloaded: it played to the end
        self._stream_ctx = None
        self._auto_fails = 0
        self._auto_next()

    # ---------- lyrics ----------
    def _reset_lyrics(self):
        self.lrc = self.lrc_plain = self.lrc_key = None
        self.lrc_album = None
        self._album_key = None
        self._lyrics_fetching = None
        self._lyrics_gen = getattr(self, "_lyrics_gen", 0) + 1

    def _load_lyrics_async(self):
        """Fetch lyrics in a background thread — never blocks the UI loop.

        A failed lookup is remembered for 60 s so the UI doesn't hammer
        LRCLIB every frame (which made 'searching…' flash forever)."""
        tr = self.current
        if not tr:
            return
        key = (tr.title, tr.artist)
        now = time.time()
        if self.lrc_key == key or \
                getattr(self, "_lyrics_fetching", None) == key:
            return
        fails = getattr(self, "_lyrics_fails", None)
        if fails is None:
            fails = self._lyrics_fails = {}
        if now < fails.get(key, 0):
            return
        self._lyrics_fetching = key
        gen = getattr(self, "_lyrics_gen", 0)

        def worker():
            try:
                text, synced = lyrics_mod.fetch(tr.artist, tr.title, tr.duration)
            except Exception:
                fails[key] = now + 60          # back off before retrying
                self._lyrics_fetching = None
                return
            # a newer track may have started while we were fetching
            if getattr(self, "_lyrics_gen", 0) != gen:
                self._lyrics_fetching = None
                return
            if synced:
                self.lrc = lyrics_mod.parse_lrc(text) or None
                self.lrc_plain = None
            else:
                self.lrc = None
                self.lrc_plain = text
            self.lrc_synced = synced
            self.lrc_key = key
            self._lyrics_fetching = None

        threading.Thread(target=worker, daemon=True).start()

    def _load_album_async(self):
        """Fetch the real album name (iTunes → Deezer) in the background.

        Independent of lyrics — the album shows even when LRCLIB has
        nothing for this track. A failed lookup backs off for 10 min."""
        tr = self.current
        if not tr:
            return
        key = (tr.title, tr.artist)
        if self._album_key == key:
            return
        now = time.time()
        if now < self._album_fails.get(key, 0):
            return
        gen = getattr(self, "_lyrics_gen", 0)

        def worker():
            try:
                album = metadata_mod.fetch_album(
                    tr.artist, tr.title, tr.duration)
            except Exception:
                album = None
            if getattr(self, "_lyrics_gen", 0) != gen:
                return          # another track started meanwhile
            self._album_key = key
            self.lrc_album = album
            if album is None:
                self._album_fails[key] = time.time() + 600

        threading.Thread(target=worker, daemon=True).start()

    def _load_lyrics(self, force=False, quiet=False):
        tr = self.current
        if not tr:
            if not quiet:
                print("Nothing is playing.")
            return
        key = (tr.title, tr.artist)
        if force or self.lrc_key != key:
            if not quiet:
                print(f"{DIM}Fetching lyrics from LRCLIB...{RST}")
            text, synced = lyrics_mod.fetch(tr.artist, tr.title, tr.duration)
            self.lrc = lyrics_mod.parse_lrc(text) if (text and synced) else None
            self.lrc_plain = None if synced else text
            self.lrc_synced = synced
            self.lrc_key = key
            if not quiet:
                if not text:
                    print("Lyrics not found.")
                elif not synced:
                    print("Only plain lyrics (no sync) found:")
                    print(text)

    def lyrics_static(self):
        if self.lrc_key is None:
            self._load_lyrics()
        tr = self.current
        if not tr:
            print("Nothing is playing.")
            return
        if not self.lrc:
            if self.lrc_plain:
                print(f"\n{YELLOW}♪ {tr.title} — {tr.artist or '?'}{RST}"
                      f"  {DIM}(plain — no sync){RST}\n")
                print(self.lrc_plain)
            else:
                print("No lyrics found.")
            return
        print(f"\n{YELLOW}♪ {tr.title} — {tr.artist or '?'}{RST}\n")
        for ms, line in self.lrc:
            print(f"  {DIM}[{fmt(ms / 1000)}]{RST} {line}")

    def lyrics_live(self):
        if self.lrc_key is None:
            self._load_lyrics()
        if not self.lrc:
            return
        tr = self.current
        lines = self.lrc
        times = [t for t, _ in lines]
        printed = 0
        print(f"{DIM}Live — Ctrl+C to exit{RST}")
        try:
            while True:
                if self.player.state == "stopped" or self.current is not tr:
                    break
                pos = self.player.position_ms()
                idx = max(0, bisect.bisect_right(times, pos) - 1)
                if pos > times[-1] + 4000 and not self.player.busy:
                    break
                if printed:
                    sys.stdout.write(f"\x1b[{printed}A")
                sys.stdout.write("\x1b[J")
                print(f"{YELLOW}♪ {tr.title}{RST}   {fmt(pos/1000)}")
                lo, hi = max(0, idx - 1), min(len(lines), idx + 3)
                printed = 1 + (hi - lo)
                for k in range(lo, hi):
                    ms, line = lines[k]
                    if k == idx and line:
                        print(f"  {CYAN}▶ {line}{RST}")
                    else:
                        print(f"  {DIM}{line or '...'}{RST}")
                sys.stdout.flush()
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass

    # ---------- commands ----------
    def cmd_now(self):
        tr = self.current
        if not tr or self.player.state == "stopped":
            print("Nothing is playing.")
            return
        pos = self.player.position_ms() / 1000
        status = {"playing": "▶", "paused": "❚❚"}.get(self.player.state, "■")
        print(f"{status} {tr.title}  —  {tr.artist or 'unknown'}")
        print(f"[{bar(pos, tr.duration)}] {fmt(pos)} / {fmt(tr.duration)}"
              f"   vol [{volbar(int(self.player.volume * 100))}] "
              f"{int(self.player.volume * 100)}%"
              f"   shuffle: {'ON' if self.shuffle else 'OFF'}")

    def cmd_sync(self):
        if not self._config_ready():
            print("Run setup first.")
            return
        if self._bot_only():
            print("Bots can't read channel history (Telegram restriction).")
            print("Only NEW channel posts land in the catalog automatically.")
            print("To index the songs already in the channel (no API registration):")
            print("  set user_session on   →   sync   (asks phone + Telegram code once)")
            return
        if not self.stop_auto_sync():
            print("Warning: background job still finishing; results may repeat.")
        try:
            print("Indexing channel history (no downloads)...")
            try:
                entries = telegram_sync.build_catalog_history(self.cfg)
            except KeyboardInterrupt:
                print("\nCancelled.")
                return
            except Exception as e:
                print(f"Telegram error: {e}")
                print("If this says RECAPTCHA, Telegram blocked this app credential for"
                      " code delivery — a personal api_id from my.telegram.org is needed.")
                return
            changed = self._merge_catalog(entries)
            print(f"Catalog: {len(entries)} audio posts found, "
                  f"{len(self.catalog)} total ({'updated' if changed else 'no changes'}).")
        finally:
            if self.cfg.get("auto_sync", True) and self._config_ready():
                self.start_auto_sync()

    AUTO_SYNC_INTERVAL = 30  # seconds between background sync passes

    def _bot_only(self):
        return not self.cfg.get("user_session") and bool(self.cfg.get("bot_token"))

    def _config_ready(self):
        return bool(self.cfg["api_id"] and self.cfg["api_hash"] and self.cfg["channel"]
                    and (self.cfg["user_session"] or self.cfg["bot_token"]))

    def start_auto_sync(self):
        """Bot mode: live watch in background (event-driven, instant).
        User mode: poll the channel every AUTO_SYNC_INTERVAL seconds."""
        if self._auto_thread and self._auto_thread.is_alive():
            return "already"
        self._auto_stop = threading.Event()
        if self._bot_only():
            self._auto_thread = threading.Thread(target=self._watch_background, daemon=True)
        else:
            self._auto_thread = threading.Thread(target=self._auto_sync_loop, daemon=True)
        self._auto_thread.start()
        return "watch" if self._bot_only() else "sync"

    def stop_auto_sync(self, timeout=10):
        if self._auto_thread and self._auto_thread.is_alive():
            if self._bot_only():
                return False   # background watch can't be interrupted cleanly
            self._auto_stop.set()
            self._auto_thread.join(timeout=timeout)
            return not self._auto_thread.is_alive()
        return True

    def _on_auto_entry(self, entry):
        self._catalog_add(entry)

    def _watch_background(self):
        try:
            telegram_sync.run_watch(self.cfg, self._on_auto_entry)
        except Exception as e:
            self.notify(f"[auto-watch] stopped: {e}")

    def _auto_sync_loop(self):
        """User-session mode: refresh the catalog periodically (index only).
        Incremental — only messages newer than the last indexed one.
        The poll takes the SAME lock as stream downloads — one Telethon
        session file, never two concurrent clients on it."""
        stop = self._auto_stop
        hinted = False
        while not stop.is_set():
            with self._sync_lock:
                if stop.is_set():
                    break
                try:
                    min_id = max((e["msg_id"] for e in self.catalog), default=0)
                    entries = telegram_sync.build_catalog_history(
                        self.cfg, interactive=False, min_id=min_id)
                    if self._merge_catalog(entries) or entries:
                        hinted = False
                    # yield the lock to the downloader even when the poll
                    # found nothing — wait OUTSIDE the lock
                    stop.wait(0.05)
                except telegram_sync.LoginRequired:
                    if not hinted:
                        self.notify("[auto-sync] personal login needed — run: sync")
                        hinted = True
                except Exception as e:
                    self.notify(f"[auto-sync] {e}")
            stop.wait(self.AUTO_SYNC_INTERVAL)

    def cmd_watch(self):
        if self._bot_only():
            if self._auto_thread and self._auto_thread.is_alive():
                print("Auto-watch is already running in the background (bot mode).")
                return
        else:
            if not self.stop_auto_sync():
                print("Warning: background sync is still finishing; watch may conflict.")
        try:
            telegram_sync.run_watch(self.cfg, self._on_auto_entry)
        except KeyboardInterrupt:
            print("\nExited watch.")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            if self.cfg.get("auto_sync", True) and self._config_ready() and not self._bot_only():
                self.start_auto_sync()

    def cmd_set(self, key, *value):
        val = " ".join(value)
        if key not in ("channel", "download_dir", "bot_token", "api_hash",
                       "api_id", "user_session", "auto_sync"):
            print("Valid keys: channel | download_dir | bot_token | api_hash | api_id | user_session | auto_sync")
            return
        if key in ("user_session", "auto_sync"):
            val = val.lower() in ("1", "true", "yes", "on")
        if key == "api_id":
            val = int(val)
        self.cfg[key] = val
        cfgmod.save(self.cfg)
        print(f"{key} = {val}")
        if key == "download_dir":
            os.makedirs(val, exist_ok=True)
            self.rescan()
            print(f" rescanned: {len(self.tracks)} tracks")

    def cmd_setup(self):
        c = self.cfg
        print(f"{YELLOW}-- Playo Setup --{RST}")
        print("1) api_id and api_hash from my.telegram.org → API development tools")
        print("2) bot_token from @BotFather (the bot must be an admin of the channel)")
        print("   or leave it empty and set user_session=true → log in with your own account\n")
        while True:
            raw = input(f"api_id [{c['api_id']}]: ").strip() or str(c["api_id"] or "")
            if raw.isdigit():
                c["api_id"] = int(raw)
                break
            print("  api_id must be a number")
        c["api_hash"] = input(f"api_hash [{c['api_hash']}]: ") or c["api_hash"]
        c["bot_token"] = input(f"bot_token [{c['bot_token'] or '—'}]: ") or c["bot_token"]
        if not c["bot_token"]:
            c["user_session"] = True
            print("  → Personal-account mode enabled (first sync: asks for phone number + code)")
        c["channel"] = input(f"channel [{c['channel']}]: ") or c["channel"]
        c["download_dir"] = input(f"download_dir [{c['download_dir']}]: ") or c["download_dir"]
        cfgmod.save(c)
        os.makedirs(c["download_dir"], exist_ok=True)
        self.rescan()
        print(f"Saved to: {cfgmod.CONFIG_FILE}")

    # ---------- dispatch ----------
    def dispatch(self, line):
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("exit", "quit"):
            self.player.close()
            return False
        elif cmd == "help":
            print(HELP)
        elif cmd == "setup":
            self.cmd_setup()
        elif cmd == "sync":
            self.cmd_sync()
        elif cmd == "watch":
            self.cmd_watch()
        elif cmd in ("list", "ls"):
            q = " ".join(args)
            idxs = self._find(q) if q else list(range(len(self.tracks)))
            if not idxs:
                print("No matches found.")
            for i in idxs:
                tr = self.tracks[i]
                mark = "▶" if i == self.index and self.player.state != "stopped" else " "
                dl = "" if tr.downloaded else f"  {DIM}[cloud]{RST}"
                print(f" {mark} {i+1:>3}. {tr.title}  {DIM}—  {tr.artist or '?'} "
                      f"[{fmt(tr.duration)}]{RST}{dl}")
        elif cmd == "play":
            if args and args[0].isdigit():
                self.play_index(int(args[0]) - 1)
            elif args:
                hits = self._find(" ".join(args))
                if not hits:
                    print("Not found.")
                elif len(hits) == 1:
                    self.play_index(hits[0])
                else:
                    for i in hits[:10]:
                        tr = self.tracks[i]
                        print(f"   {i+1:>3}. {tr.title}")
                    print(f"{DIM}Play by number: play {hits[0]+1}{RST}")
            elif self.player.state == "paused":
                self.player.play()
            else:
                self.play_index(0)
        elif cmd == "pause":
            self.player.pause()
            self.cmd_now()
        elif cmd == "resume":
            self.player.play()
            self.cmd_now()
        elif cmd == "stop":
            self.player.stop()
            print("■ Stopped.")
        elif cmd == "next":
            self.play_index(self._step(1) or 0)
        elif cmd == "prev":
            self.play_index(self._step(-1) or 0)
        elif cmd == "seek":
            if not args:
                print("Example: seek 90 | seek +15 | seek -30")
                return True
            try:
                v = float(args[0])
            except ValueError:
                print("Enter a number.")
                return True
            cur = self.player.position_ms() / 1000
            self.player.seek(cur + v if args[0].startswith(("+", "-")) else v)
            self.cmd_now()
        elif cmd == "vol":
            if args and args[0].isdigit():
                self.set_volume(int(args[0]))
            print(f"vol [{volbar(int(self.player.volume * 100))}] "
                  f"{int(self.player.volume * 100)}%")
        elif cmd == "shuffle":
            on = self.toggle_shuffle()
            print(f"Shuffle: {'ON' if on else 'OFF'}")
        elif cmd == "sort":
            key = args[0] if args else None
            if key in self.SORTS:
                self.cfg["sort"] = key
                cfgmod.save(self.cfg)
                self.apply_sort()
                print(f"Sorted by: {key}")
            else:
                print(f"sort: {' | '.join(self.SORTS)} (current: {self.cfg.get('sort', 'title')})")
        elif cmd in ("ui", "tui"):
            from .tui import LiveView
            self._tui = True
            try:
                LiveView(self).run()
            finally:
                self._tui = False
        elif cmd == "lyrics":
            if args and args[0] == "live":
                self.lyrics_live()
            else:
                self.lyrics_static()
        elif cmd == "now":
            self.cmd_now()
        elif cmd == "rescan":
            self.rescan()
            print(f"{len(self.tracks)} tracks.")
        elif cmd == "open":
            os.makedirs(self.cfg["download_dir"], exist_ok=True)
            os.startfile(self.cfg["download_dir"])  # Windows
        elif cmd == "set":
            if len(args) < 2:
                print("Example: set channel @my_music | set download_dir D:\\Music")
            else:
                self.cmd_set(args[0], *args[1:])
        else:
            print(f"Unknown command: {cmd}  ({DIM}help{RST})")
        return True


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    just_fix_windows_console()
    app = PlayoApp()
    print(f"{CYAN}{BANNER}{RST}")
    print(f"{DIM}        {TAGLINE}{RST}")
    print()
    config_ok = app._config_ready()
    if not config_ok:
        print(f"{YELLOW}Configuration incomplete — run setup first.{RST}")
    elif app.cfg.get("auto_sync", True):
        if app._bot_only():
            app.start_auto_sync()
            print(f"{DIM}[auto-watch] bot is listening live — new posts land in the "
                  f"catalog; play one to download it{RST}")
            print(f"{DIM}Existing songs? index them: set user_session on → sync"
                  f"  (no API registration, one Telegram code){RST}")
        elif os.path.exists(cfgmod.session_path(app.cfg) + "_user.session"):
            app.start_auto_sync()
            print(f"{DIM}[auto-watch] running in background (live catalog updates){RST}")
        else:
            # first login must happen in the main thread (phone/code prompts)
            print(f"{DIM}First login: indexing channel...{RST}")
            app.cmd_sync()
            app.start_auto_sync()
            print(f"{DIM}[auto-watch] now running in background{RST}")
    n_dl = sum(1 for t in app.tracks if t.downloaded)
    n_cloud = len(app.tracks) - n_dl
    print(f"{n_dl} downloaded + {n_cloud} in catalog  |  folder: {app.cfg['download_dir']}")
    # the dashboard IS the app: run it, and quit when the user leaves it
    if config_ok:
        try:
            from .tui import LiveView
            app._tui = True
            try:
                LiveView(app).run()
            finally:
                app._tui = False
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"ui error: {e}")
        finally:
            app.player.close()
