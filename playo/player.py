import os
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import threading
import time

import pygame


class Player:
    def __init__(self, volume=0.8):
        self.volume = volume
        self.state = "stopped"        # stopped | playing | paused | buffering
        self.on_end = None
        self.on_stall = None          # mixer ran dry mid-stream (suppress_end)
        self.suppress_end = False     # True while streaming a growing file
        self._base_ms = 0
        self._tick = 0.0
        self._stop_evt = threading.Event()
        self._thread = None

    def _ensure(self):
        if not pygame.mixer.get_init():
            pygame.mixer.pre_init(44100, -16, 2, 1024)
            pygame.mixer.init()

    def load(self, path):
        self._ensure()
        pygame.mixer.music.set_volume(self.volume)
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        self._base_ms = 0
        self._tick = time.monotonic()
        self.state = "playing"
        self._start_monitor()

    def play(self):
        if self.state == "paused":
            pygame.mixer.music.unpause()
            self._tick = time.monotonic()
            self.state = "playing"

    def pause(self):
        if self.state == "playing":
            self._base_ms = self.position_ms()
            pygame.mixer.music.pause()
            self.state = "paused"

    def toggle(self):
        self.pause() if self.state == "playing" else self.play()

    def stop(self):
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            try:
                # release the file handle — SDL keeps it open after stop(),
                # which blocks renames/rewrites of the file on Windows
                pygame.mixer.music.unload()
            except Exception:
                pass
        self.state = "stopped"
        self._base_ms = 0
        self.suppress_end = False

    def freeze(self):
        """Release the mixer's file handle while keeping the displayed
        position — used mid-stream so the UI never jumps to 0:00."""
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
        self.state = "buffering"
        self.suppress_end = True

    def load_at(self, path, sec):
        """Load and start from `sec` seconds in one go (seamless swaps)."""
        self._ensure()
        pygame.mixer.music.set_volume(self.volume)
        pygame.mixer.music.load(path)
        try:
            pygame.mixer.music.play(start=max(0.0, sec))
        except Exception:
            pygame.mixer.music.play()
        self._base_ms = int(max(0.0, sec) * 1000)
        self._tick = time.monotonic()
        self.state = "playing"
        self._start_monitor()

    def resume_buffering(self):
        """Continue playback after a download filled in the rest of the file."""
        if self.state == "buffering":
            sec = self._base_ms / 1000
            try:
                pygame.mixer.music.play(start=sec)
                self.state = "playing"
                self._tick = time.monotonic()
            except Exception:
                pass
        self.suppress_end = False

    def set_volume(self, v):  # 0..100
        self.volume = max(0.0, min(1.0, v / 100.0))
        if pygame.mixer.get_init():
            pygame.mixer.music.set_volume(self.volume)

    def seek(self, sec):
        # buffering = stream not loaded yet — only the logical position
        # moves; touching the mixer here raises 'music not loaded'
        if self.state in ("stopped", "buffering") or not pygame.mixer.get_init():
            return
        sec = max(0.0, sec)
        pygame.mixer.music.set_pos(sec)
        self._base_ms = int(sec * 1000)
        self._tick = time.monotonic()
        if self.state == "paused":
            pygame.mixer.music.unpause()
            pygame.mixer.music.pause()

    def position_ms(self):
        if self.state == "playing":
            return self._base_ms + int((time.monotonic() - self._tick) * 1000)
        return self._base_ms

    @property
    def busy(self):
        return bool(pygame.mixer.get_init()) and pygame.mixer.music.get_busy()

    def _start_monitor(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self):
        while not self._stop_evt.wait(0.4):
            if self.state == "playing" and not self.busy:
                if self.suppress_end and self.on_stall:
                    # mid-stream the mixer ran dry: the buffered audio caught
                    # up with the download, or the song is over. Freeze the
                    # clock and let the app decide — going to 'stopped' here
                    # used to kill the stream AND the auto-advance silently
                    self._base_ms = self.position_ms()
                    self.state = "buffering"
                    try:
                        self.on_stall()
                    except Exception:
                        pass
                else:
                    self.state = "stopped"
                    if self.on_end:
                        try:
                            self.on_end()
                        except Exception:
                            pass

    def close(self):
        self._stop_evt.set()
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
