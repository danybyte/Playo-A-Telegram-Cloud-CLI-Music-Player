import bisect
import os
import queue
import random
import re
import shutil
import sys
import threading
import time

try:
    import msvcrt
except ImportError:
    msvcrt = None

ACCENT, YELLOW, DIM, BOLD, RST = "\x1b[96m", "\x1b[93m", "\x1b[2m", "\x1b[1m", "\x1b[0m"
GREEN = "\x1b[92m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def fmt(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def vis(s):
    return len(ANSI_RE.sub("", s))


def fit(s, w):
    """Truncate to w visible columns, pad to exactly w."""
    i = used = 0
    res = ""
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            res += m.group(0)
            i = m.end()
            continue
        if used >= w:
            break
        res += s[i]
        used += 1
        i += 1
    return res + (RST if vis(s) > w else "") + " " * max(0, w - used)


def clip(s, w):
    if vis(s) <= w:
        return s
    i = used = 0
    res = ""
    while i < len(s) and used < w - 1:
        m = ANSI_RE.match(s, i)
        if m:
            res += m.group(0)
            i = m.end()
            continue
        res += s[i]
        used += 1
        i += 1
    return res + "…"


def tui_strip(s):
    """Strip ANSI, then whitespace — used to detect blank filler rows."""
    return ANSI_RE.sub("", s).strip()


def progress_bar(pos, dur, w=28):
    if dur <= 0:
        return "·" * w
    pos = min(max(0, pos), dur)
    f = int(w * pos / dur)
    return "█" * f + "░" * (w - f)


def volume_bar(v, w=10):
    v = max(0, min(100, int(v)))
    f = int(w * v / 100)
    return "█" * f + "░" * (w - f)


class LiveView:
    """Full-screen dashboard player.

    One screen with everything: library list, lyrics pane, now-playing bar.
    Keyboard shortcuts for every control; 'o' opens the settings overlay
    (sort mode, download folder, channel, volume, shuffle).
    """

    def __init__(self, app):
        self.app = app
        self.flt = ""                 # filter text
        self.searching = False        # ON only after Tab; OFF = player keys
        self.sel = 0                  # selection within hits
        self.show_lyrics = True
        self.overlay = None           # None | "settings" | "picker"
        self.pick = None              # None | set(track idx) — lyric reset
        self._last_vol = 60
        self.hits = list(range(len(app.tracks)))

    # ---------- keys ----------
    def _read_key(self):
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            # full F-key map: F6=sort, F5/F7..F12 used to fall through —
            # sort key looked dead
            return {"H": "up", "P": "down", "K": "left", "M": "right",
                    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
                    "S": "del", "R": "ins", ";": "f1", "<": "f2", "=": "f3",
                    ">": "f4", "?": "f5", "@": "f6", "A": "f7", "B": "f8",
                    "C": "f9", "D": "f10", "W": "f11", "X": "f12",
                    chr(133): "f11", chr(134): "f12",
                    "s": "ctrl_left", "t": "ctrl_right"}.get(ch2, "?")
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            return "esc"
        if ch == "\t":
            return "tab"
        return ch

    def _hits(self):
        if self.flt:
            return self.app._find(self.flt)
        return list(range(len(self.app.tracks)))

    def _set_filter(self, text):
        """Change the filter, but keep the cursor on the SAME track.

        So after searching, picking a song and clearing the search (Esc),
        the selection stays on that song inside the full list."""
        prev = self.hits[self.sel] \
            if self.hits and self.sel < len(self.hits) else None
        self.flt = text
        self.hits = self._hits()
        if prev is not None and prev in self.hits:
            self.sel = self.hits.index(prev)
        else:
            self.sel = 0

    def _keep_anchor(self, prev):
        """Re-place the selection on the SAME track after a resort."""
        if prev is None or not self.hits:
            self.sel = min(self.sel, max(0, len(self.hits) - 1))
            return
        if prev in self.hits:
            self.sel = self.hits.index(prev)

    def _play(self, i):
        if i is not None:
            self.app.play_async(i)

    def _seek_back(self, app):
        """← key: seconds mode seeks -5s; lyric mode ALWAYS goes to the
        previous lyric line's start (predictable — the first press lands
        on the previous line, never restarts the current one)."""
        if app.cfg.get("seek_back") == "lyric" and app.lrc:
            times = [t for t, _ in app.lrc]
            pos = app.player.position_ms()
            idx = max(0, bisect.bisect_right(times, pos) - 1)
            # strictly before the current line's start when we are only a
            # moment into it; otherwise the line before the current one
            if pos > times[idx] and idx > 0:
                target = idx - 1 if pos - times[idx] <= 1500 else idx
            else:
                target = max(0, idx - 1)
            # never land on a whitespace-only line — keep going back
            while target > 0 and not (app.lrc[target][1] or "").strip():
                target -= 1
            app.player.seek(times[target] / 1000)
        else:
            app.player.seek(app.player.position_ms() / 1000 - 5)

    def _seek_fwd(self, app):
        """→ key: seconds mode seeks +5s; lyric mode jumps to the next line."""
        if app.cfg.get("seek_back") == "lyric" and app.lrc:
            times = [t for t, _ in app.lrc]
            pos = app.player.position_ms()
            idx = max(0, bisect.bisect_right(times, pos) - 1)
            nxt = min(len(times) - 1, idx + 1)
            # skip whitespace-only lines forward too
            while nxt < len(times) - 1 and \
                    not (app.lrc[nxt][1] or "").strip():
                nxt += 1
            app.player.seek(times[nxt] / 1000)
        else:
            app.player.seek(app.player.position_ms() / 1000 + 5)

    def _toggle_mute(self):
        app = self.app
        if app.player.volume > 0:
            self._last_vol = int(app.player.volume * 100)
            app.set_volume(0)
        else:
            app.set_volume(self._last_vol)

    def _toggle_shuffle(self):
        app = self.app
        app.toggle_shuffle()
        self._note(f"shuffle {'ON' if app.shuffle else 'OFF'}")

    def _shuffle_play(self):
        """Shuffle play: shuffle ON (if off) + a random track starts now."""
        app = self.app
        if not app.tracks:
            return
        if not app.shuffle:
            app.toggle_shuffle()
        pool = [i for i in app.order if i != app.index] or app.order
        self._play(random.choice(pool))
        self._note("shuffle play — random track started")

    def _toggle_lyrics(self):
        self.show_lyrics = not self.show_lyrics

    def _note(self, text):
        self.app._tui_note = (time.time(), text)

    SEARCH_IDLE_EXIT = 2            # seconds of typing silence, then OFF

    def _search_touch(self):
        """Refresh the 2-second auto-off timer while typing."""
        self._search_last = time.time()

    # ---------- settings overlay ----------
    # yellow context hints shown UNDER the selected row
    SETTINGS_HINTS = {
        "bot_token":  "bots can't read history — only NEW posts. "
                      "for the old songs use add_account",
        "add_account": "personal login: phone + Telegram code, once. "
                       "indexes ALL history (854 songs need this)",
        "user_session": "ON = personal account (full history) · "
                        "OFF = bot only (new posts only)",
        "lyrics_pick": "ON = before showing lyrics, list the first 5 source "
                       "results — ↑↓ + Enter picks, lyrics are saved",
        "lyrics_reset": "deletes every lyric file in ~/.playo/lyrics — "
                        "next plays refetch from the source. To reset only "
                        "some songs: + on them, then Enter",
        "sync_now":   "full re-index — fills gaps the incremental "
                      "auto-sync missed",
    }

    def _settings_rows(self):
        app = self.app
        vol = int(app.player.volume * 100)
        K = app.keys()
        keys_cfg = app.cfg.get("keys") or {}
        rows = [
            ("_sec", "PLAYBACK", ""),
            ("volume",     f"[{volume_bar(vol)}] {vol:3d}%", "←/→"),
            ("shuffle",    "ON " if app.shuffle else "OFF",
                           f"enter · key: {K['shuffle'] or '—'}"),
            ("sort",       app.cfg.get("sort", "title"), "enter cycles"),
            ("seek_back",  app.cfg.get("seek_back", "seconds"),
                           "←/→ · seconds | lyric"),
            ("_sec", "LYRICS", ""),
            ("lyrics_pick", ("ON " if app.cfg.get("lyrics_pick") else "OFF"),
                       "enter · pick from 5 lyric results"),
            ("lyrics_reset", "reset all",
                       "enter · deletes ALL saved lyrics"),
            ("_sec", "KEYS", ""),
        ]
        # rebindable keys (letters are reserved for search — use F-keys/symbols)
        for action in ("next", "prev", "shuffle", "sort", "lyrics", "mute"):
            cur = keys_cfg.get(action, K[action])
            shown = cur if cur else "—"
            rows.append((f"key:{action}", shown,
                         "Enter, then press an F-key"))
        rows += [
            ("_sec", "TELEGRAM / LIBRARY", ""),
            ("auto_sync",  ("ON " if app.cfg.get("auto_sync", True) else "OFF"),
                           "enter"),
            ("sync_now",   ("syncing…" if getattr(self, "_sync_busy", False)
                            else "run now"),
                           "enter — re-index the whole channel"),
            ("add_account", self._account_state(),
                           "enter — personal login (phone + code)"),
            ("channel",    clip(str(app.cfg.get("channel", "")), 24), "Enter edit"),
            ("download_dir", clip(str(app.cfg.get("download_dir", "")), 24), ""),
            ("bot_token",  "<set>" if app.cfg.get("bot_token") else "—", ""),
            ("user_session", ("ON " if app.cfg.get("user_session") else "OFF"),
                           "enter"),
        ]
        return rows

    def _account_state(self):
        """'logged in' when a personal session exists, else a prompt."""
        from . import config as cfgmod
        logged = os.path.exists(cfgmod.session_path(self.app.cfg)
                                + "_user.session")
        return ("logged in ✓" if logged else "add…")

    def _close_overlay(self):
        """Close the settings overlay and restore the library selection."""
        self.overlay = None
        if hasattr(self, "_overlay_sel"):
            self.hits = self._hits()
            self.sel = min(self._overlay_sel, max(0, len(self.hits) - 1))

    def _handle_settings(self, key):
        app = self.app
        rows = self._settings_rows()
        self.sel = min(self.sel, len(rows) - 1)
        # section headers are labels, not rows — never rest on one
        while self.sel < len(rows) - 1 and rows[self.sel][0] == "_sec":
            self.sel += 1
        name = rows[self.sel][0]

        def save():
            from . import config as cfgmod
            cfgmod.save(app.cfg)

        if key == "esc":
            self._close_overlay()
        elif key == "up":
            self.sel = max(0, self.sel - 1)
            while self.sel > 0 and rows[self.sel][0] == "_sec":
                self.sel -= 1
        elif key == "down":
            self.sel = min(len(rows) - 1, self.sel + 1)
            while self.sel < len(rows) - 1 and rows[self.sel][0] == "_sec":
                self.sel += 1
        elif key == "f2" or (key == "left" and name == "sort"):
            if name == "sort":
                keys = list(app.SORTS)
                app.cfg["sort"] = keys[(keys.index(app.cfg["sort"]) - 1) % len(keys)]
                app.apply_sort()
                save()
            elif name == "volume":
                app.set_volume(int(app.player.volume * 100) - 5)
            elif name == "channel":
                self._edit = str(app.cfg.get("channel", ""))
                self._edit_mode = True
        elif key == "left":
            if name == "volume":
                app.set_volume(int(app.player.volume * 100) - 5)
            elif name == "seek_back":
                app.cfg["seek_back"] = ("seconds" if
                                        app.cfg.get("seek_back") == "lyric"
                                        else "lyric")
                save()
            elif name == "channel":
                self._edit = str(app.cfg.get("channel", ""))
                self._edit_mode = True
        elif key == "right":
            if name == "volume":
                app.set_volume(int(app.player.volume * 100) + 5)
            elif name == "sort":
                app.cycle_sort()
            elif name == "seek_back":
                app.cfg["seek_back"] = ("seconds" if
                                        app.cfg.get("seek_back") == "lyric"
                                        else "lyric")
                save()
            elif name == "channel":
                self._edit = str(app.cfg.get("channel", ""))
                self._edit_mode = True
        elif key in ("\r", "\n"):
            if name == "shuffle":
                app.toggle_shuffle()
            elif name == "sync_now":
                self._sync_now()
            elif name == "add_account":
                self._login_flow()
            elif name == "auto_sync":
                app.cfg["auto_sync"] = not app.cfg.get("auto_sync", True)
                save()
            elif name == "lyrics_pick":
                app.cfg["lyrics_pick"] = not app.cfg.get("lyrics_pick", False)
                save()
                app._reset_lyrics()       # re-fetch with the new mode
                self._note("lyric picker "
                           + ("ON — pick from the first 5 results"
                              if app.cfg["lyrics_pick"] else "OFF"))
            elif name == "lyrics_reset":
                from . import lyrics as lyrics_mod
                n = lyrics_mod.clear_all_cache()
                app._reset_lyrics()       # current track refetches
                self._note(f"ALL saved lyrics wiped ({n} files)")
            elif name == "user_session":
                app.cfg["user_session"] = not app.cfg.get("user_session")
                save()
                # restart the background worker so it uses the NEW mode
                # (bot watch vs user polling) — otherwise the old loop
                # keeps spamming bot-restriction errors
                app.stop_auto_sync()
                if app.cfg.get("auto_sync", True) and app._config_ready():
                    app.start_auto_sync()
                self._note("connection mode switched — worker restarted")
            elif name == "sort":
                app.cycle_sort()
            elif name.startswith("key:"):
                self._binding = name[4:]
                self._bind_mode = True
            elif name in ("channel", "download_dir", "bot_token"):
                # editable text fields: Enter opens the inline editor
                self._edit_key = name
                self._edit = str(app.cfg.get(name, ""))
                self._edit_mode = True

    # ---------- multi-select lyric reset ----------
    # '+' marks songs, Enter resets their lyrics. Selection persists
    # across search/filter changes, Esc clears it.
    def _pick_toggle(self):
        if self.pick is None:
            self.pick = set()
        if not self.hits:
            return
        i = self.hits[self.sel]
        if i in self.pick:
            self.pick.discard(i)
            self._note(f"deselected — {len(self.pick)} marked"
                       if self.pick else "selection cleared")
        else:
            self.pick.add(i)
            self._note(f"marked — {len(self.pick)} selected"
                       f"  (Enter = reset their lyrics)")

    def _pick_reset(self):
        app = self.app
        idxs = sorted(self.pick or set())
        tracks = [app.tracks[i] for i in idxs if i < len(app.tracks)]
        n = app.clear_lyrics(tracks)
        self.pick = None
        self._note(f"lyrics reset for {n}/{len(tracks)} songs"
                   f" — will refetch on next play")

    def _pick_maybe_clear(self):
        """Drop selection entries that no longer exist (rescan/resort)."""
        if self.pick:
            self.pick = {i for i in self.pick if i < len(self.app.tracks)}

    def _pick_count(self):
        self._pick_maybe_clear()
        return len(self.pick or ())

    # ---------- manual full sync (settings) ----------
    def _sync_now(self):
        """Re-index the ENTIRE channel in the background (no downloads).

        The incremental auto-sync only fetches messages newer than the last
        known one — if the catalog ever falls behind (missed posts, a fresh
        session), this full pass fills the gap. UI stays interactive."""
        app = self.app
        if getattr(self, "_sync_busy", False):
            return
        if not app._config_ready():
            self._note("sync needs channel + login — check settings")
            return
        self._sync_busy = True
        self._note("syncing… indexing the whole channel (this can take a while)")

        def worker():
            try:
                from . import telegram_sync
                # serialize with the auto-sync poller + stream downloader —
                # one Telethon session file, no 'database is locked'
                with app._sync_lock:
                    entries = telegram_sync.build_catalog_history(
                        app.cfg, interactive=False)
                added = app._merge_catalog(entries)
                self._note(f"sync done — {len(entries)} audio posts"
                           f" ({'catalog updated' if added else 'no changes'})")
            except telegram_sync.LoginRequired:
                self._note("sync needs a personal login — no user session")
            except Exception as e:
                self._note(f"sync failed: {e}")
            finally:
                self._sync_busy = False

        threading.Thread(target=worker, daemon=True).start()

    # ---------- main dispatch ----------
    # Typing letters filters the list directly (search-first design).
    # Commands live on F-keys, Tab, and symbols so nothing collides.
    def _handle(self, key):
        if getattr(self, "_edit_mode", False):
            self._handle_edit(key)
            return
        if getattr(self, "_bind_mode", False):
            self._handle_bind(key)
            return
        if self.overlay == "picker":
            self._handle_picker(key)
            return
        if self.overlay == "settings":
            self._handle_settings(key)
            return

        app = self.app
        K = app.keys()

        # ---- SEARCH MODE: ON after Tab, typing filters, auto-exits ----
        if self.searching:
            if key in ("esc", "tab"):
                self.searching = False        # functional keys take over
            elif key == "\x08":
                self._set_filter(self.flt[:-1])
                self._search_touch()
            elif key in ("\r", "\n"):
                if self.hits:
                    self._play(self.hits[self.sel])
                self.searching = False
            elif key in ("up", "down", "pgup", "pgdn"):
                # browsing the results closes search — player keys return
                self.searching = False
                self._note("search off — player keys active")
                if key == "up":
                    self.sel = max(0, self.sel - 1)
                elif key == "down":
                    self.sel = min(max(0, len(self.hits) - 1), self.sel + 1)
                elif key == "pgup":
                    self.sel = max(0, self.sel - 10)
                else:
                    self.sel = min(max(0, len(self.hits) - 1), self.sel + 10)
            elif isinstance(key, str) and len(key) == 1 and key.isprintable() \
                    and key != " ":
                self._set_filter(self.flt + key)
                self._search_touch()
            return

        # ---- PLAYER MODE: letters are player commands, typing never filters
        K = app.keys()
        if key == "tab" or key == K["search"]:
            self.searching = True
            self._search_touch()
            self._note("search ON — type to filter · 2s idle = off")
        elif key == "esc":
            if self.pick:
                self.pick = None
                self._note("selection cleared")
            elif self.flt:
                self._set_filter("")       # clear filter, cursor stays put
            else:
                raise KeyboardInterrupt
        elif key in ("o", "f3") or key == K["settings"]:
            self._overlay_sel = self.sel     # library selection to restore
            self.overlay = "settings"
            self.sel = 0
        elif key == "=":
            app.set_volume(int(app.player.volume * 100) + 5)
        elif key == "-":
            app.set_volume(int(app.player.volume * 100) - 5)
        elif key in ("m", "del") or (K["mute"] and key == K["mute"]):
            self._toggle_mute()
        elif key in ("s", "tab") or (K["shuffle"] and key == K["shuffle"]):
            self._toggle_shuffle()
        elif key in ("y", "f6") or (K["sort"] and key == K["sort"]):
            self._note(f"sorted by {app.cycle_sort()}")
        elif key == "+":
            self._pick_toggle()
        elif key in ("v", "f4") or (K["lyrics"] and key == K["lyrics"]):
            self._toggle_lyrics()
        elif key in ("n", ">") or (K["next"] and key == K["next"]):
            self._play(app._step(1))
        elif key in ("b", "p", "<") or (K["prev"] and key == K["prev"]):
            self._play(app._step(-1))
        elif key == "z":
            self._shuffle_play()
        elif key == "left":
            self._seek_back(app)
        elif key == "right":
            self._seek_fwd(app)
        elif key == "ctrl_left":
            app.player.seek(app.player.position_ms() / 1000 - 30)
        elif key == "ctrl_right":
            app.player.seek(app.player.position_ms() / 1000 + 30)
        elif key == "r" or key == "f8":
            app.rescan()
            self.hits = self._hits()
            self._note("library rescanned")
        elif key in ("\r", "\n"):
            if self.pick:
                self._pick_reset()
            elif self.hits:
                self._play(self.hits[self.sel])
        elif key == " ":
            if app.player.state == "playing":
                app.player.pause()
            elif app.player.state == "paused":
                app.player.play()
            elif self.hits:
                self._play(self.hits[self.sel])
        elif key == "up":
            self.sel = max(0, self.sel - 1)
        elif key == "down":
            self.sel = min(max(0, len(self.hits) - 1), self.sel + 1)
        elif key == "pgup":
            self.sel = max(0, self.sel - 10)
        elif key == "pgdn":
            self.sel = min(max(0, len(self.hits) - 1), self.sel + 10)
        elif key == "home":
            self.sel = 0
        elif key == "end":
            self.sel = max(0, len(self.hits) - 1)

    # ---------- key binding capture ----------
    # Named keys only — keys() deliberately ignores 1-char overrides
    # (letters/symbols belong to search + player commands), so binding
    # them used to silently produce a DEAD binding
    BINDABLE = {"up", "down", "home", "end", "pgup", "pgdn", "del", "ins",
                "ctrl_left", "ctrl_right",
                "f1", "f2", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
                "f11", "f12"}

    def _handle_bind(self, key):
        """Capture one key press as the new binding for an action."""
        if key == "esc":
            self._bind_mode = False
            return
        if key in self.BINDABLE:
            app = self.app
            cfg_keys = app.cfg.get("keys") or {}
            cfg_keys[self._binding] = key
            app.cfg["keys"] = cfg_keys
            from . import config as cfgmod
            cfgmod.save(app.cfg)
            self._bind_mode = False
            self._close_overlay()        # back to the dashboard so the key is usable
            self._note(f"'{key}' is now {self._binding}")
            return
        self._note("use an F-key or Home/End/PgUp/PgDn/Del — "
                   "letters stay reserved")
        self._bind_mode = False

    # ---------- channel inline edit ----------
    # ---------- personal account login (settings) ----------
    def _login_flow(self):
        """Add the personal account from settings: the phone is asked via
        the inline editor; the code editor opens ONLY when Telegram asks."""
        app = self.app
        if not app.cfg.get("api_id") or not app.cfg.get("api_hash"):
            self._note("login needs api_id/api_hash — run setup.py")
            return
        self._edit_key = "_login_phone"
        self._edit = ""
        self._edit_mode = True
        self._note("type your phone (+98…) then Enter")

    def _login_step2(self, phone):
        app = self.app
        if not phone:
            self._note("login cancelled — no phone")
            return
        code_q = queue.Queue()
        pwd_q = queue.Queue()
        self._login_code_q = code_q
        self._login_pwd_q = pwd_q
        self._note("connecting…")

        def worker():
            try:
                from . import telegram_sync
                res = telegram_sync.personal_login(
                    app.cfg, phone, code_q, pwd_q,
                    status_cb=self._on_code_needed)
                if res == "already":
                    self._note("already logged in — nothing to do")
                else:
                    self._note("account added — indexing the channel…")
                    app.cfg["user_session"] = True
                    from . import config as cfgmod
                    cfgmod.save(app.cfg)
                    app.stop_auto_sync()
                    if app.cfg.get("auto_sync", True) and app._config_ready():
                        app.start_auto_sync()
            except Exception as e:
                self._note(f"login failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_code_needed(self, msg):
        """Telegram asked for the code (or the 2FA password) — open the
        matching editor + a loud, sticky yellow hint (keys now type it)."""
        is_pwd = "password" in (msg or "").lower()
        self._edit_key = "_login_pwd" if is_pwd else "_login_code"
        self._edit = ""
        self._edit_mode = True
        self._tui_note = (time.time() + 30,   # sticky — stays until typed
                          (f"2FA PASSWORD — type it below ·"
                           f" Enter submits · Esc cancels" if is_pwd else
                           "CODE SENT — check Telegram · type it below ·"
                           " Enter submits · Esc cancels"))
        self._note(msg or "")

    def _handle_edit(self, key):
        """Inline editor for any text setting (channel, download_dir, ...)."""
        if key == "esc":
            self._edit_mode = False
            # cancelling a login prompt must UNBLOCK the login worker —
            # it sits on queue.get() forever otherwise
            if getattr(self, "_edit_key", "").startswith("_login"):
                for q in (getattr(self, "_login_code_q", None),
                          getattr(self, "_login_pwd_q", None)):
                    if q:
                        try:
                            q.put_nowait("")
                        except Exception:
                            pass
        elif key == "\x08":
            self._edit = self._edit[:-1]
        elif key in ("\r", "\n"):
            key_name = getattr(self, "_edit_key", "channel")
            val = self._edit.strip()
            if key_name == "_login_phone":
                self._edit_mode = False
                self._login_step2(val)
                return
            if key_name == "_login_code":
                self._edit_mode = False
                q = getattr(self, "_login_code_q", None)
                if q:
                    q.put(val)
                    self._note("code submitted — verifying…")
                return
            if key_name == "_login_pwd":
                self._edit_mode = False
                q = getattr(self, "_login_pwd_q", None)
                if q:
                    q.put(val)
                    self._note("password submitted — verifying…")
                return
            self.app.cfg[key_name] = val
            from . import config as cfgmod
            cfgmod.save(self.app.cfg)
            self._edit_mode = False
            self._note(f"{key_name} saved")
            if key_name == "download_dir":
                os.makedirs(val, exist_ok=True)
                self.app.rescan()
                self.hits = self._hits()
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self._edit += key

    # ---------- data ----------
    def _ensure_lyrics(self):
        app = self.app
        # fetch as soon as now-playing switches — even while the song is
        # still downloading/streaming; lyrics must be ready when audio starts
        if app.current:
            app._load_lyrics_async()
            app._load_album_async()
        self._check_picker()

    def _check_picker(self):
        """Pick mode: when the candidate list lands, open the picker
        overlay (unless settings is open — it opens right after)."""
        app = self.app
        cands = getattr(app, "_lyric_candidates", None)
        if self.overlay == "picker":
            if not cands:
                self._close_overlay()     # track changed / cleared
            return
        if cands and self.overlay is None:
            self._overlay_sel = self.sel  # library selection to restore
            self.overlay = "picker"
            self.sel = 0

    def _picker_apply(self, idx):
        app = self.app
        cands = getattr(app, "_lyric_candidates", None) or []
        if not (0 <= idx < len(cands)):
            return
        from . import lyrics as lyrics_mod
        artist, track, album, dur, text, synced = cands[idx]
        tr = app.current
        if tr:
            lyrics_mod.save_cache(tr.artist, tr.title, text, synced)
            app._apply_lyric_text((tr.title, tr.artist), text, synced)
        self._close_overlay()
        self._note(f"lyrics: {clip(artist, 24)} — {clip(track, 30)}"
                   f"{'  (saved)' if tr else ''}")

    def _handle_picker(self, key):
        app = self.app
        cands = getattr(app, "_lyric_candidates", None) or []
        if key == "esc":
            # auto-apply the best match (synced first) — old behavior
            best = next((i for i, c in enumerate(cands) if c[5]), 0)
            if cands:
                self._picker_apply(best)
            else:
                self._close_overlay()
        elif key == "up":
            self.sel = max(0, self.sel - 1)
        elif key == "down":
            self.sel = min(max(0, len(cands) - 1), self.sel + 1)
        elif key in ("\r", "\n"):
            self._picker_apply(self.sel)
        elif isinstance(key, str) and len(key) == 1 and key in "12345":
            self._picker_apply(int(key) - 1)

    # ---------- render ----------
    def _render(self):
        app = self.app
        cols, rows = shutil.get_terminal_size((100, 30))
        self.hits = self._hits()
        # sel means SETTINGS-ROW index while an overlay is open — clamping
        # it against the track count pinned the cursor at 'seek_back'
        # (row 3) whenever the library had ≤ len(settings rows) hits
        if not self.overlay:
            self.sel = min(self.sel, max(0, len(self.hits) - 1))
            # after a resort, keep the selection on the same track
            stamp = getattr(app, "_sort_stamp", 0)
            if stamp != getattr(self, "_seen_sort_stamp", 0):
                self._seen_sort_stamp = stamp
                self._keep_anchor(getattr(self, "_anchor_track", None))
            self._anchor_track = self.hits[self.sel] if self.hits else None
        w = cols - 1
        bar = self._render_bottom_bar(w)
        body_rows = rows - len(bar)

        L = []
        self._render_dashboard(L, body_rows, w)
        while len(L) < body_rows:
            L.append("")
        L = L[:body_rows]
        L.extend(bar)

        if self.overlay:
            self._render_overlay(L, cols, rows)

        sys.stdout.write("\x1b[H" + "\n\x1b[K".join(L) + "\x1b[K\x1b[J")
        sys.stdout.flush()

    def _row(self, mark, num, title, artist, dur, w, playing=False):
        left = 10                                  # ' > ⇣ 123  '
        right = len(dur) + 1
        artist_s = artist or "?"
        space = w - left - right - 4
        if space < 8:
            title = clip(title, max(4, w - left - right - 6))
            artist_s = ""
        elif vis(title) > space - 4 - vis(artist_s):
            title = clip(title, max(4, space - 4 - vis(artist_s)))
        t_style = BOLD if playing else ""
        line = f" {mark} {DIM}{num}{RST}  {t_style}{title}{RST if t_style else ''}"
        if artist_s:
            line += f"  {DIM}— {artist_s}{RST}"
        pad = " " * max(1, w - vis(line) - len(dur))
        return fit(f"{line}{pad}{DIM}{dur}{RST}", w)

    def _render_dashboard(self, L, rows, w):
        app = self.app
        tr = app.current
        lyr_on = self.show_lyrics and tr is not None

        # FIXED heights. The help is ALWAYS two short lines — a single long
        # line used to wrap on narrower terminals and scrambled the layout.
        H = 2
        # 1) top bars: ALWAYS two lines — a live SEARCH badge (with countdown)
        #    while searching, player-key hints otherwise. Layout never moves.
        note = getattr(app, "_tui_note", None)
        K = app.keys()
        if note and time.time() - note[0] < 4:
            L.append(fit(f" {YELLOW}·{RST} {DIM}{note[1]}{RST}", w))
        elif self.searching:
            left = max(0, int(self.SEARCH_IDLE_EXIT + 0.99
                              - (time.time() - getattr(self, "_search_last", 0))))
            L.append(fit(f" {ACCENT}{BOLD}SEARCH ON{RST} {ACCENT}●{RST}"
                         f"  {DIM}type to filter · ↑↓ browse = off{RST}", w))
            L.append(fit(f"    filter:{RST} {BOLD}{self.flt or '…'}█{RST}"
                         f"  {DIM}off in {left}s — keep typing{RST}", w))
        else:
            L.append(fit(f" {BOLD}PLAYO{RST}  {DIM}Tab{RST} search  "
                         f"{DIM}Space{RST} play/pause  {DIM}Enter{RST} play sel"
                         f"  {DIM}{K['next']}{RST} next  {DIM}{K['prev']}{RST}"
                         f" prev  {DIM}←→{RST} seek", w))
            L.append(fit(f"    {DIM}=−|↑↓{RST} vol  {DIM}+{RST} mark  "
                         f"{DIM}{K['mute']}{RST} mute"
                         f"  {DIM}F3{RST} settings  {DIM}{K['shuffle']}{RST}"
                         f" shuffle  {DIM}z{RST} shuf-play  {DIM}{K['sort']}{RST}"
                         f" sort  {DIM}F4{RST} lyrics"
                         f"  {DIM}Esc²{RST} quit", w))
        if self.flt and not self.searching:
            # filter stays applied after search closes — make that obvious
            L.append(fit(f" {ACCENT}filter:{RST} {BOLD}{self.flt}{RST}"
                         f"  {DIM}(Esc clears){RST}", w))

        B = rows - H
        if self.flt and not self.searching:
            B -= 1                            # the 'filter kept' banner line
        two_col = bool(lyr_on) and w >= 100 and B >= 10
        # 2) panels
        if two_col:
            lw = max(38, (w * 55) // 100)
            rw = w - lw - 1
            left = self._library_panel(B, lw, app)
            right = self._lyrics_panel(B, rw, app)
            for k in range(B):
                a = left[k] if k < len(left) else ""
                b = right[k] if k < len(right) else ""
                # pad the left cell to the full column width so the divider
                # ALWAYS sits at the same column — lyrics never slide left
                a = fit(a, lw)
                L.append(fit(f"{a}{DIM}│{RST}{b}", w))
        else:
            # stacked: library on top, lyrics pinned to the bottom —
            # (1 libhdr + S list + 1 lyrhdr + lyrics = B, no trailing gap)
            L.append(fit(f"{DIM}── {self._lib_hdr_text(app)} ──{RST}", w))
            if lyr_on:
                Ly = max(2, (B - 4) // 2)
                S = max(3, B - Ly - 1)
            else:
                S = max(3, B - 1)
            L.extend(self._library_body(S, w, app))
            if lyr_on:
                tmp = []
                self._render_lyrics(tmp, Ly - 1, w)
                L.append(fit(f"{DIM}──{RST} {GREEN}{BOLD}LYRICS{RST} "
                             f"{DIM}{'─' * max(1, w - 11)}{RST}", w))
                # pin the lyrics block to the bottom: pad ABOVE it
                while len(L) + len(tmp) < rows:
                    L.append("")
                L.extend(tmp)

    def _lib_hdr_text(self, app):
        sort = app.cfg.get("sort", "title")
        hdr = f"LIBRARY  {len(self.hits)}/{len(app.tracks)}  ·  sort: {sort}"
        if app.shuffle:
            hdr += f"  ·  {GREEN}SHUFFLE{RST}"
        n = self._pick_count()
        if n:
            hdr += (f"  ·  {GREEN}{n} MARKED{RST} {DIM}(Enter reset lyrics"
                    f" · Esc cancel){RST}")
        vol = int(app.player.volume * 100)
        if vol == 0:
            hdr += f"  ·  {YELLOW}MUTED{RST}"
        if self.flt:
            mark = ""                     # mode shown in the top bar
            hdr += f"  ·  filter: {BOLD}{self.flt}█{RST}{mark}"
        return hdr

    def _library_body(self, n, w, app):
        """Exactly n lines: scroll-hint slots + track rows."""
        out = []
        if not self.hits:
            if not app.tracks:
                out.append(fit(f"   {DIM}catalog is empty — new channel posts"
                               f" appear here live{RST}", w))
            else:
                out.append(fit(f"   {DIM}no matches for '{self.flt}'{RST}", w))
        else:
            entries = max(1, n - 2)
            lo = max(0, min(self.sel - entries // 2, len(self.hits) - entries))
            hi = min(len(self.hits), lo + entries)
            if lo > 0:
                out.append(fit(f"   {DIM}… {lo} above{RST}", w))
            else:
                out.append("")
            for pos in range(lo, hi):
                i = self.hits[pos]
                t = app.tracks[i]
                if app._dl_status == t.title and \
                        getattr(app, "_dl_progress", None):
                    cur, tot = app._dl_progress
                    pct = f"{min(99, int(cur * 100 / tot))}" if tot else "…"
                    tag = f"{YELLOW}⇣{pct:>2}{RST}"
                elif not t.downloaded:
                    tag = f"{YELLOW}⇣ {RST}"
                elif i == app.index and app.player.state == "playing":
                    tag = f"{ACCENT}♪ {RST}"
                elif i == app.index and app.player.state == "paused":
                    tag = f"{ACCENT}❚❚{RST}"
                else:
                    tag = f"{DIM}· {RST}"
                sel_o = f"{ACCENT}{BOLD}>{RST}" if pos == self.sel else " "
                if self.pick and i in self.pick:
                    tag = f"{GREEN}+ {RST}"
                playing = (i == app.index and app.player.state == "playing")
                out.append(self._row(f"{sel_o}{tag}", f"{i + 1:>4}", t.title,
                                     t.artist, fmt(t.duration), w, playing))
            if hi < len(self.hits):
                out.append(fit(f"   {DIM}… {len(self.hits) - hi} below{RST}", w))
            else:
                out.append("")
        while len(out) < n:
            out.append("")
        return out[:n]

    def _library_panel(self, n, lw, app):
        """Left column: LIBRARY header + body. Exactly n lines."""
        lines = [fit(f"{DIM}── {self._lib_hdr_text(app)} ──{RST}", lw)]
        lines.extend(self._library_body(n - 1, lw, app))
        while len(lines) < n:
            lines.append("")
        return lines[:n]

    def _lyrics_panel(self, n, rw, app):
        """Right column: green LYRICS header + a CENTERED lyrics block.

        The block takes ~55% of the column height with generous blank
        margins above and below, so lyric lines never sit next to the
        list rows (no visual mixing)."""
        lines = [fit(f"{DIM}──{RST} {GREEN}{BOLD}LYRICS{RST} "
                     f"{DIM}{'─' * max(1, rw - 11)}{RST}", rw)]
        body_h = n - 1
        content = max(3, int(body_h * 0.55))
        tmp = []
        self._render_lyrics(tmp, content, rw)
        # trim trailing blank filler rows, then center the block vertically
        while tmp and not tui_strip(tmp[-1]):
            tmp.pop()
        pad_top = max(1, (body_h - len(tmp)) // 2)
        lines.extend([""] * pad_top)
        lines.extend(tmp)
        while len(lines) < n:
            lines.append("")
        return lines[:n]

    def _vol_meter(self, vol, width=14):
        """Stepped meter: ▁▂▃▄▅▆▇ growing with the volume."""
        bars = "▁▂▃▄▅▆▇█"
        n = max(4, width)
        filled = round(n * max(0, min(100, vol)) / 100)
        out = []
        for k in range(n):
            lvl = int((k + 1) / n * (len(bars) - 1))
            ch = bars[lvl] if k < filled else bars[0]
            out.append(ch)
        return "".join(out)

    def _render_lyrics(self, L, n, w):
        app = self.app
        tr = app.current
        if app.lrc:
            lines = app.lrc
            times = [t for t, _ in lines]
            idx = max(0, bisect.bisect_right(times, app.player.position_ms()) - 1)
            lo = max(0, min(idx - n // 2, len(lines) - n))
            hi = min(len(lines), lo + n)
            phase = int(time.time() * 2.5) % 3      # 400ms per step
            for k in range(lo, hi):
                ms, line = lines[k]
                if line and line.strip():
                    txt = line
                elif k == idx:
                    # animated ♪♪♪ loader — only while playback is ON this
                    # line; passed/upcoming interludes stay gray
                    if phase == 0:
                        txt = f"{ACCENT}♪{RST}{DIM}♪♪{RST}"
                    elif phase == 1:
                        txt = f"{DIM}♪{RST}{ACCENT}♪{RST}{DIM}♪{RST}"
                    else:
                        txt = f"{DIM}♪♪{RST}{ACCENT}♪{RST}"
                else:
                    txt = f"{DIM}♪♪♪{RST}"
                if k == idx:
                    # same 3-space indent as every other row — never sticks
                    # to the column separator
                    L.append(fit(f"   {BOLD}{txt}{RST}", w))
                else:
                    L.append(fit(f"   {DIM}{txt}{RST}", w))
        elif app.lrc_plain:
            shown = app.lrc_plain.splitlines()
            if tr and tr.duration > 0:
                frac = min(1.0, max(0.0, app.player.position_ms() / 1000 / tr.duration))
                start = int(frac * max(0, len(shown) - n + 1))
            else:
                start = 0
            for line in shown[start:start + n - 1]:
                L.append(fit(f"   {DIM}{line}{RST}", w))
            L.append(fit(f"   {DIM}(plain — no sync){RST}", w))
        elif getattr(app, "_lyrics_fetching", None):
            L.append(fit(f" {ACCENT}◌{RST} {DIM}searching lyrics…{RST}", w))
        elif not app.lrc_key:
            L.append(fit(f"   {DIM}…{RST}", w))
        elif app.lrc_key and not app.lrc and not app.lrc_plain:
            L.append(fit(f"   {DIM}(no lyrics found for this track){RST}", w))
        else:
            L.append(fit(f"   {DIM}…{RST}", w))

    # bottom bar ─────────────────────────────
    def _render_bottom_bar(self, w):
        """Exactly 2 lines, always — a stable height keeps rows from jumping."""
        app = self.app
        tr = app.current
        state = app.player.state
        status = {"playing": "♪", "paused": "❚❚",
                  "buffering": "◌"}.get(state, "■")
        out = []
        if not tr:
            total = len(app.tracks)
            msg = (f"{DIM}Tab search · Enter play · Space pause{RST}" if total
                   else
                   f"{DIM}catalog empty — new channel posts appear here live{RST}")
            out.append(fit(f" {status} {msg}", w))
            dl = sum(1 for t in app.tracks if t.downloaded)
            out.append(fit(f"   {DIM}{dl} downloaded · {total - dl} cloud"
                           f" · {app.cfg['download_dir']}{RST}", w))
            return out
        # position within the PLAYBACK order — labeled so shuffle numbers
        # don't read as the library row number ('queue 2/848' vs '2/848')
        if app.index is not None and app.index in app.order:
            qpos = app.order.index(app.index) + 1
        else:
            qpos = app.index + 1 if app.index is not None else 0
        label = "queue" if app.shuffle else "track"
        q = f"{DIM}[{label} {qpos}/{len(app.tracks)}]{RST}"
        album = getattr(app, "lrc_album", None)
        album_bit = (f"  {GREEN}· Album: {album}{RST}" if album
                     and album.lower() not in tr.title.lower()
                     else "")
        line = f" {ACCENT}{status}{RST} {BOLD}{tr.title}{RST} " \
               f"{DIM}— {tr.artist or '?'}{RST}{album_bit} {q}"
        out.append(fit(line, w))
        pos = app.player.position_ms() / 1000
        pb = progress_bar(pos, tr.duration, max(10, min(30, w - 40)))
        vol = int(app.player.volume * 100)
        # single stepped volume meter (green), grows/shrinks with volume
        meter = f"{GREEN}{self._vol_meter(vol, 16)}{RST}"
        bits = [f"{pb} {DIM}{fmt(pos)}/{fmt(tr.duration)}{RST}",
                f"{DIM}VOL{RST} {meter} {vol:3d}%"]
        if vol == 0:
            bits.append(f"{YELLOW}MUTE{RST}")
        if app.shuffle:
            bits.append(f"{GREEN}SHUF{RST}")
        dlbit = self._dl_bit()
        if dlbit:
            bits.append(dlbit)
        # a download error shows once, then clears (no permanent '!')
        err = getattr(app, "_dl_error", None)
        if err and err != getattr(self, "_last_err_shown", None):
            self._last_err_shown = err
            bits.append(f"{YELLOW}! {clip(err, 30)}{RST}")
        else:
            app._dl_error = None
            self._last_err_shown = None
        out.append(fit(" " + "  ".join(bits), w))
        return out

    def _dl_bit(self):
        """Live download indicator: '↓ 42% ▓▓▓░░ 4.2/10.0 MB' — the percent
        bar WINS over the plain '◌ buffering…' text so the user always sees
        how far the download really is."""
        app = self.app
        prog = getattr(app, "_dl_progress", None)
        if getattr(app, "_dl_status", None) and prog:
            cur, tot = prog
            if tot:
                pct = int(cur * 100 / tot)
                mb_c, mb_t = cur / 1e6, tot / 1e6
                bw = 10
                filled = int(bw * cur / tot)
                bw_s = "▓" * filled + "░" * (bw - filled)
                return (f"{YELLOW}↓{RST} {pct:3d}% {DIM}{bw_s}"
                        f" {mb_c:.1f}/{mb_t:.1f}MB{RST}")
            cur_s = f"{cur / 1e6:.1f}MB"
            return f"{YELLOW}↓{RST} {DIM}{cur_s}{RST}"
        if getattr(app, "_dl_status", None):
            return f"{YELLOW}↓ starting…{RST}"
        if app.player.state == "buffering":
            return f"{YELLOW}◌ buffering…{RST}"
        tr = app.current
        if tr and not tr.downloaded:
            return f"{YELLOW}↓ cloud{RST}"
        return None

    # overlays ───────────────────────────────
    def _render_overlay(self, L, cols, rows):
        """Modal overlay, drawn INSIDE the panel area (never the whole frame
        — bottom bar stays visible). Box borders always full-width aligned."""
        w = cols - 1
        body_rows = len(L)
        if self.overlay == "picker":
            lines = [f"{GREEN}{BOLD}PICK LYRICS{RST}  "
                     f"{DIM}(↑↓ · 1-5 · Enter apply · Esc = best match){RST}"]
            cands = getattr(self.app, "_lyric_candidates", None) or []
            for k, (artist, track, album, dur, text, synced) in enumerate(cands):
                cur = k == self.sel
                o = f"{ACCENT}{BOLD}>{RST}" if cur else f"{DIM}{k + 1}{RST}"
                style = BOLD if cur else ""
                n_lines = len(text.splitlines())
                label = (f"{artist} — {track}"
                         + (f"  [{album}]" if album else "")
                         + f"  {fmt(dur) if dur else ''}"
                         + (f"  {GREEN}synced{RST}" if synced
                            else f"  {DIM}plain{RST}"))
                lines.append(f" {o} {style}{clip(label, w - 12)}{RST}"
                             f"  {DIM}· {n_lines} lines{RST}")
            if not cands:
                lines.append(f" {DIM}no results{RST}")
        elif self.overlay == "settings":
            lines = [f"{BOLD}SETTINGS{RST}  {DIM}(↑↓ select · ←/→ adjust ·"
                     f" Enter toggle · Esc close){RST}"]
            if getattr(self, "_edit_mode", False):
                key_name = getattr(self, "_edit_key", "channel")
                lines.append(f" {BOLD}{key_name}{RST}: "
                             f"{ACCENT}{self._edit}█{RST}"
                             f"  {DIM}(Enter save · Esc cancel){RST}")
            else:
                rows = self._settings_rows()
                for k, (name, val, hint) in enumerate(rows):
                    if name == "_sec":
                        lines.append(f" {DIM}── {BOLD}{val}{RST}{DIM} "
                                     f"{'─' * max(2, 30 - len(val))}{RST}")
                        continue
                    cur = k == self.sel
                    o = f"{ACCENT}{BOLD}>{RST}" if cur else " "
                    style = BOLD if cur else ""
                    lines.append(f" {o} {style}{name:<14}{RST} {val}"
                                 f"  {DIM}{hint}{RST}")
                # yellow context hint, right UNDER the selected row
                hint = self.SETTINGS_HINTS.get(rows[self.sel][0]) \
                    if rows and 0 <= self.sel < len(rows) else None
                lines.append(f" {YELLOW}{clip(hint, max(20, w - 12))}{RST}"
                             if hint else "")
        # the dashboard (and its note line) is hidden behind the overlay —
        # surface the active note INSIDE the box so login feedback is visible
        note = getattr(self.app, "_tui_note", None)
        if note and time.time() - note[0] < 8:
            lines.append(f" {YELLOW}{clip(note[1], max(20, w - 12))}{RST}")
        else:
            lines.append("")          # reserved line — box height stays put
        # left-align the box: fixed left margin, borders span box_w exactly
        box_w = min(w - 2, max(vis(x) for x in lines) + 4)
        margin = 2
        top = max(1, (body_rows - len(lines) - 2) // 2)
        left = max(0, (w - box_w) // 2)       # center the box horizontally
        out = []
        for k in range(body_rows):
            if k == top - 1 or k == top + len(lines):
                out.append(fit(" " * left + f"{DIM}{'─' * box_w}{RST}", w))
            elif top <= k < top + len(lines):
                line = lines[k - top]
                inner = " " * margin + line
                inner += " " * max(0, box_w - vis(inner) - margin)
                out.append(fit(" " * left
                               + f"{DIM}│{RST}{inner}{DIM}│{RST}", w))
            else:
                out.append("")
        L[:] = out

    # ---------- loop ----------
    def _drain_notes(self):
        app = self.app
        note = getattr(app, "_tui_note", None)
        if not note or time.time() - note[0] >= 4:
            notes = getattr(app, "_notes", None)
            if notes:
                app._tui_note = notes.pop(0)

    def _log_error(self, where, exc):
        import traceback
        from . import config as cfgmod
        try:
            with open(os.path.join(cfgmod.CONFIG_DIR, "error.log"), "a",
                      encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {exc!r}\n")
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        self.app._tui_note = (time.time(), f"! {where}: {exc}")

    def run(self):
        if msvcrt is None:
            print("Live view is only supported on Windows right now.")
            return
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        try:
            while True:
                try:
                    while msvcrt.kbhit():
                        self._handle(self._read_key())
                    # 2 s of typing silence closes search — player keys return
                    if self.searching:
                        last = getattr(self, "_search_last", 0)
                        if time.time() - last > self.SEARCH_IDLE_EXIT:
                            self.searching = False
                            self._note("search OFF (2s idle) — filter kept")
                    self._ensure_lyrics()
                    self._drain_notes()
                    self._render()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self._log_error("ui", e)
                time.sleep(0.08)
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write("\x1b[?25h\x1b[?1049l\x1b[0m")
            sys.stdout.flush()
