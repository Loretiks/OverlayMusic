"""macOS backend: MediaRemote (private framework) + NSEvent global hotkey.

Imported only on darwin. Exports the same interface as _platform_win:
MediaController, HotkeyManager, force_foreground, BACKEND_ERR.

Notes:
- MediaRemote.framework is a *private* Apple framework. Stable in practice for
  many years (Sleeve, NepTunes, etc. ship with it) but officially unsupported.
- Global hotkey via NSEvent.addGlobalMonitorForEventsMatchingMask requires
  Accessibility permission. macOS will prompt the user the first time.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

BACKEND_ERR: str | None = None

try:
    import objc  # noqa: F401
    from Foundation import (
        NSBundle,
        NSObject,
        NSDate,
        NSRunLoop,
        NSDefaultRunLoopMode,
    )
    from AppKit import (
        NSApp,
        NSApplication,
        NSEvent,
        NSEventMaskKeyDown,
        NSCommandKeyMask,
        NSAlternateKeyMask,
        NSControlKeyMask,
        NSShiftKeyMask,
    )
    from CoreFoundation import CFRunLoopGetMain, kCFRunLoopDefaultMode
    import dispatch  # noqa: F401  # pyobjc-framework-libdispatch
    PYOBJC_OK = True
except Exception as e:
    BACKEND_ERR = f"pyobjc not available: {e}"
    PYOBJC_OK = False


# --------------------------------------------------------------------------
# MediaRemote private framework setup
# --------------------------------------------------------------------------
_MR_FUNCS: dict = {}


def _load_media_remote() -> bool:
    """Load private MediaRemote.framework and bind needed C functions."""
    global BACKEND_ERR
    if not PYOBJC_OK:
        return False
    if _MR_FUNCS:
        return True
    try:
        bundle = NSBundle.bundleWithPath_(
            "/System/Library/PrivateFrameworks/MediaRemote.framework"
        )
        if bundle is None or not bundle.load():
            BACKEND_ERR = "MediaRemote.framework load failed"
            return False
        # Bind C functions from the bundle.
        objc.loadBundleFunctions(
            bundle,
            _MR_FUNCS,
            [
                # void MRMediaRemoteGetNowPlayingInfo(dispatch_queue_t, void(^)(CFDictionaryRef))
                ("MRMediaRemoteGetNowPlayingInfo", b"v@?"),
                # Bool MRMediaRemoteGetNowPlayingApplicationIsPlaying(dispatch_queue_t, void(^)(Bool))
                ("MRMediaRemoteGetNowPlayingApplicationIsPlaying", b"v@?"),
                # void MRMediaRemoteSendCommand(MRMediaRemoteCommand, NSDictionary*)
                ("MRMediaRemoteSendCommand", b"vI@"),
            ],
        )
        return True
    except Exception as e:
        BACKEND_ERR = f"MediaRemote bind failed: {e}"
        return False


# Command IDs from MRMediaRemote.h (reverse-engineered, stable for years).
MR_CMD_PLAY = 0
MR_CMD_PAUSE = 1
MR_CMD_TOGGLE = 2
MR_CMD_NEXT = 4
MR_CMD_PREV = 5


# --------------------------------------------------------------------------
# Hotkey infra (global event monitor)
# --------------------------------------------------------------------------
# macOS keycodes (subset — extended via lookup tables below).
_KEYCODES: dict[str, int] = {
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7,
    "C": 8, "V": 9, "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16,
    "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "0": 29, "9": 25, "7": 26, "8": 28, "O": 31, "U": 32, "I": 34,
    "P": 35, "L": 37, "J": 38, "K": 40, "N": 45, "M": 46,
    "SPACE": 49, "ENTER": 36, "RETURN": 36, "TAB": 48, "DELETE": 51,
    "ESC": 53, "ESCAPE": 53, "LEFT": 123, "RIGHT": 124, "DOWN": 125, "UP": 126,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}

_MAC_MODS = {
    "CMD": "cmd", "COMMAND": "cmd", "META": "cmd", "WIN": "cmd", "WINDOWS": "cmd",
    "OPT": "opt", "OPTION": "opt", "ALT": "opt",
    "CTRL": "ctrl", "CONTROL": "ctrl",
    "SHIFT": "shift",
}


def _parse_hotkey_mac(s: str) -> tuple[set[str], int | None]:
    parts = [p.strip().upper() for p in s.replace("-", "+").split("+") if p.strip()]
    mods: set[str] = set()
    keycode: int | None = None
    for p in parts:
        if p in _MAC_MODS:
            mods.add(_MAC_MODS[p])
        elif p in _KEYCODES:
            keycode = _KEYCODES[p]
    return mods, keycode


def force_foreground(_hwnd_unused: int = 0) -> None:
    """Bring this app to the front. _hwnd_unused signature compat with Windows."""
    if not PYOBJC_OK:
        return
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


class HotkeyManager(QObject):
    """Global hotkey via NSEvent.addGlobalMonitorForEventsMatchingMask.
    Requires Accessibility permission (System Settings → Privacy → Accessibility).
    """

    triggered = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._monitor = None
        self._wanted_mods: set[str] = set()
        self._wanted_keycode: int | None = None

    def start(self, hotkey: str) -> bool:
        self.stop()
        if not PYOBJC_OK:
            return False
        mods, kc = _parse_hotkey_mac(hotkey)
        if kc is None:
            return False
        self._wanted_mods = mods
        self._wanted_keycode = kc

        def _handler(event):
            try:
                if event.keyCode() != self._wanted_keycode:
                    return
                flags = event.modifierFlags()
                has_cmd = bool(flags & NSCommandKeyMask)
                has_opt = bool(flags & NSAlternateKeyMask)
                has_ctrl = bool(flags & NSControlKeyMask)
                has_shift = bool(flags & NSShiftKeyMask)
                if "cmd" in self._wanted_mods != has_cmd:
                    return
                if ("opt" in self._wanted_mods) != has_opt:
                    return
                if ("ctrl" in self._wanted_mods) != has_ctrl:
                    return
                if ("shift" in self._wanted_mods) != has_shift:
                    return
                self.triggered.emit()
            except Exception:
                pass

        try:
            self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handler
            )
            return self._monitor is not None
        except Exception:
            return False

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                NSEvent.removeMonitor_(self._monitor)
            except Exception:
                pass
            self._monitor = None


# --------------------------------------------------------------------------
# Media controller — MediaRemote-backed
# --------------------------------------------------------------------------
class _NowPlayingFetcher:
    """Wraps MRMediaRemoteGetNowPlayingInfo into a sync-ish blocking call.
    The framework calls the completion block on dispatch queue; we wait briefly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: dict | None = None
        self._is_playing: bool | None = None

    def fetch(self, timeout: float = 0.4) -> tuple[dict | None, bool | None]:
        if not _load_media_remote():
            return None, None

        info_done = threading.Event()
        play_done = threading.Event()
        info_holder: dict = {}
        play_holder: dict = {}

        def info_cb(d):
            try:
                if d is not None:
                    info_holder["d"] = dict(d)
            finally:
                info_done.set()

        def play_cb(playing):
            try:
                play_holder["v"] = bool(playing)
            finally:
                play_done.set()

        try:
            _MR_FUNCS["MRMediaRemoteGetNowPlayingInfo"](None, info_cb)
            _MR_FUNCS["MRMediaRemoteGetNowPlayingApplicationIsPlaying"](None, play_cb)
        except Exception:
            return None, None

        # Pump main loop briefly so completion blocks fire.
        end = time.monotonic() + timeout
        while time.monotonic() < end and (not info_done.is_set() or not play_done.is_set()):
            try:
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    NSDate.dateWithTimeIntervalSinceNow_(0.02),
                )
            except Exception:
                time.sleep(0.02)

        return info_holder.get("d"), play_holder.get("v")

    @staticmethod
    def send_command(cmd: int) -> None:
        if not _load_media_remote():
            return
        try:
            _MR_FUNCS["MRMediaRemoteSendCommand"](cmd, None)
        except Exception:
            pass


def _ns_to_bytes(ns_data) -> bytes:
    try:
        return bytes(ns_data) if ns_data is not None else b""
    except Exception:
        return b""


class MediaController(QObject):
    info_updated = Signal(dict)

    # Field keys from MRNowPlaying (constants are CFStrings; we use string repr).
    _K_TITLE = "kMRMediaRemoteNowPlayingInfoTitle"
    _K_ARTIST = "kMRMediaRemoteNowPlayingInfoArtist"
    _K_ALBUM = "kMRMediaRemoteNowPlayingInfoAlbum"
    _K_DURATION = "kMRMediaRemoteNowPlayingInfoDuration"
    _K_ELAPSED = "kMRMediaRemoteNowPlayingInfoElapsedTime"
    _K_ARTWORK = "kMRMediaRemoteNowPlayingInfoArtworkData"
    _K_TIMESTAMP = "kMRMediaRemoteNowPlayingInfoTimestamp"
    _K_RATE = "kMRMediaRemoteNowPlayingInfoPlaybackRate"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fetcher = _NowPlayingFetcher()
        self._pos_baseline = 0.0
        self._pos_baseline_at = time.monotonic()
        self._last_track_key: tuple | None = None
        self._zero_streak = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(250)

        if BACKEND_ERR:
            QTimer.singleShot(
                100,
                lambda: self.info_updated.emit(
                    {"available": False, "reason": BACKEND_ERR}
                ),
            )

    def poll(self) -> None:
        threading.Thread(target=self._poll_blocking, daemon=True).start()

    def _poll_blocking(self) -> None:
        info, is_playing = self._fetcher.fetch()
        if info is None:
            self.info_updated.emit({"available": False, "reason": "Откройте трек в плеере"})
            return

        title = str(info.get(self._K_TITLE, "") or "")
        artist = str(info.get(self._K_ARTIST, "") or "")
        album = str(info.get(self._K_ALBUM, "") or "")
        dur = float(info.get(self._K_DURATION, 0) or 0)
        elapsed = float(info.get(self._K_ELAPSED, 0) or 0)
        artwork = info.get(self._K_ARTWORK)
        thumb_bytes = _ns_to_bytes(artwork) if artwork is not None else b""
        playing = bool(is_playing) if is_playing is not None else True

        now = time.monotonic()
        track_key = (title, artist, round(dur, 1))
        track_changed = track_key != self._last_track_key

        if playing and elapsed < 0.5 and not track_changed:
            self._zero_streak += 1
        else:
            self._zero_streak = 0
        position_reliable = self._zero_streak < 3

        if track_changed:
            self._last_track_key = track_key
            self._pos_baseline = elapsed
            self._pos_baseline_at = now
        elif playing and position_reliable:
            predicted = self._pos_baseline + (now - self._pos_baseline_at)
            if abs(elapsed - predicted) > 3.0:
                self._pos_baseline = elapsed
                self._pos_baseline_at = now
        elif not playing:
            if elapsed > 0.5 or self._pos_baseline < 0.5:
                self._pos_baseline = elapsed
                self._pos_baseline_at = now

        if playing:
            effective_pos = self._pos_baseline + (now - self._pos_baseline_at)
            if dur > 0:
                effective_pos = min(effective_pos, dur)
        else:
            effective_pos = self._pos_baseline

        self.info_updated.emit({
            "available": True,
            "title": title,
            "artist": artist,
            "album": album,
            "thumbnail": thumb_bytes if thumb_bytes else None,
            "is_playing": playing,
            "position": effective_pos,
            "duration": dur,
            "position_reliable": position_reliable,
            "source": "macos.mediaremote",
        })

    def toggle_play(self) -> None:
        _NowPlayingFetcher.send_command(MR_CMD_TOGGLE)

    def next_track(self) -> None:
        _NowPlayingFetcher.send_command(MR_CMD_NEXT)

    def prev_track(self) -> None:
        _NowPlayingFetcher.send_command(MR_CMD_PREV)

    def shutdown(self) -> None:
        try:
            self._poll_timer.stop()
        except Exception:
            pass
