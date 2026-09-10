import datetime
import threading

try:
    import winsdk.windows.media as wm
    import winsdk.windows.media.playback as pb
except ImportError:
    wm = pb = None

# maps the app's player state -> SMTC playback status
_STATUS = {}
if wm:
    _STATUS = {
        "playing": wm.MediaPlaybackStatus.PLAYING,
        "buffering": wm.MediaPlaybackStatus.PLAYING,
        "paused": wm.MediaPlaybackStatus.PAUSED,
        "stopped": wm.MediaPlaybackStatus.STOPPED,
    }


class SMTCBridge:
    """Windows System Media Transport Controls bridge.

    Shows playo in the system 'now playing' overlay (Win+K flyout, volume
    OSD, lock screen) and routes hardware media keys — the headphone
    play/pause button, next/prev keys — to the player, even when the
    terminal has no focus.

    Uses a MediaPlayer purely as an SMTC provider (pygame does the actual
    audio): MediaPlayer.system_media_transport_controls works from a plain
    desktop process, unlike GetForCurrentView which needs a CoreWindow.

    A poll loop syncs the overlay with the player ~2x/second; button
    events arrive on a WinRT thread and are handed to `on_button` there
    (the app already tolerates cross-thread playback calls — the auto-
    advance monitor does the same).
    """

    POLL = 0.5

    def __init__(self, snapshot, on_button):
        self._snapshot = snapshot      # () -> dict(title, artist, album,
        self._on_button = on_button    #   duration, pos, state) | None
        self._mp = None
        self._smtc = None
        self._last = None              # last pushed snapshot dict
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---------- worker thread ----------
    def _run(self):
        try:
            mp = pb.MediaPlayer()
            mp.command_manager.is_enabled = False
            smtc = mp.system_media_transport_controls
            smtc.is_enabled = True
            for flag in ("is_play_enabled", "is_pause_enabled",
                         "is_next_enabled", "is_previous_enabled",
                         "is_stop_enabled"):
                setattr(smtc, flag, True)
            smtc.add_button_pressed(self._button)
        except Exception:
            return                     # SMTC unavailable — run without it
        self._mp, self._smtc = mp, smtc
        try:
            self._sync()
        except Exception:
            pass
        while not self._stop.wait(self.POLL):
            try:
                self._sync()
            except Exception:
                pass

    # SystemMediaTransportControlsButton -> action. Built ONCE via getattr:
    # some winsdk builds lack members (b10 has no PLAY_PAUSE) and a hard
    # reference in the handler raised AttributeError on the WinRT event
    # thread for EVERY button press — the press never reached the player
    # and the traceback flooded stderr straight through the TUI.
    _BTN_ACTIONS = (("PLAY", "play"), ("PAUSE", "pause"),
                    ("PLAY_PAUSE", "toggle"), ("NEXT", "next"),
                    ("PREVIOUS", "prev"), ("STOP", "stop"))
    _btn_map = None

    @classmethod
    def _button_map(cls):
        if cls._btn_map is None:
            B = wm.SystemMediaTransportControlsButton
            cls._btn_map = {B[n]: a for n, a in cls._BTN_ACTIONS
                            if hasattr(B, n)}
        return cls._btn_map

    def _button(self, sender, args):
        try:
            action = self._button_map().get(args.button)
            if action:
                self._on_button(action)
        except Exception:
            pass        # never let a WinRT-callback error reach the console

    def _sync(self):
        snap = self._snapshot()
        if snap is None:
            snap = {"title": "", "artist": "", "album": "",
                    "duration": 0, "pos": 0, "state": "stopped"}
        last = self._last
        # push metadata only when the track (or album, which loads async)
        # actually changed — property writes cost a shell round-trip
        if last is None or (snap["title"], snap["artist"], snap["album"],
                            snap["duration"]) != (last["title"],
                                                  last["artist"],
                                                  last["album"],
                                                  last["duration"]):
            du = self._smtc.display_updater
            du.type = wm.MediaPlaybackType.MUSIC
            mus = du.music_properties
            mus.title = snap["title"] or "Playo"
            mus.artist = snap["artist"]
            mus.album_title = snap["album"]
            du.update()
        status = _STATUS.get(snap["state"])
        if last is None or status != last["_status"]:
            self._smtc.playback_status = status
        pos = max(0.0, snap["pos"])
        dur = max(0.0, snap["duration"])
        # the system advances the clock itself while PLAYING — re-push the
        # position when paused/seeking/stalled or drifting past 1.5 s
        pos_stale = (last is None or snap["state"] != "playing"
                     or abs(pos - last["_pos"]) > 1.5 + self.POLL)
        if pos_stale and (last is None or abs(pos - last["_pos"]) > 0.25
                          or last["_pos"] != pos or last["_dur"] != dur):
            tp = wm.SystemMediaTransportControlsTimelineProperties()
            tp.position = datetime.timedelta(seconds=pos)
            tp.end_time = datetime.timedelta(seconds=dur)
            tp.min_seek_time = datetime.timedelta(seconds=0)
            tp.max_seek_time = datetime.timedelta(seconds=dur)
            self._smtc.update_timeline_properties(tp)
        self._last = {**snap, "_status": status, "_pos": pos, "_dur": dur}

    # ---------- public ----------
    def close(self):
        self._stop.set()
        if self._smtc:
            try:
                self._smtc.is_enabled = False
                self._smtc.playback_status = wm.MediaPlaybackStatus.STOPPED
            except Exception:
                pass
        self._mp = self._smtc = None
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
