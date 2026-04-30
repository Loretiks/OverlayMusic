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

import base64
import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


_LOG_PATH = Path.home() / "Library" / "Logs" / "OverlayMusic.log"


def _dlog(msg: str) -> None:
    """Best-effort diagnostic log to ~/Library/Logs/OverlayMusic.log."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

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
# Accessibility trust (required for global hotkey via NSEvent monitor)
# --------------------------------------------------------------------------
_AX_LIB = None
_CF_LIB = None


def _load_ax() -> bool:
    """Lazy-load ApplicationServices + CoreFoundation via ctypes."""
    global _AX_LIB, _CF_LIB
    if _AX_LIB is not None and _CF_LIB is not None:
        return True
    try:
        ax_path = ctypes.util.find_library("ApplicationServices")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ax_path or not cf_path:
            return False
        _AX_LIB = ctypes.cdll.LoadLibrary(ax_path)
        _CF_LIB = ctypes.cdll.LoadLibrary(cf_path)
        _AX_LIB.AXIsProcessTrusted.restype = ctypes.c_bool
        _AX_LIB.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        _AX_LIB.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        _CF_LIB.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        _CF_LIB.CFDictionaryCreate.restype = ctypes.c_void_p
        _CF_LIB.CFRelease.argtypes = [ctypes.c_void_p]
        _CF_LIB.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        _CF_LIB.CFStringCreateWithCString.restype = ctypes.c_void_p
        _CF_LIB.CFBooleanGetTypeID.restype = ctypes.c_long
        return True
    except Exception:
        return False


def is_accessibility_trusted() -> bool:
    """True iff this process has Accessibility permission."""
    if not _load_ax():
        return False
    try:
        return bool(_AX_LIB.AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility(prompt: bool = True) -> bool:
    """Check trust and (optionally) trigger the system prompt to grant it.
    Returns current trust status. The prompt only appears when missing."""
    if not _load_ax():
        return False
    try:
        # kAXTrustedCheckOptionPrompt CFString -> kCFBooleanTrue
        key = _CF_LIB.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", 0x08000100  # kCFStringEncodingUTF8
        )
        # kCFBooleanTrue is exported as a global; grab via dlsym.
        cf_true = ctypes.c_void_p.in_dll(_CF_LIB, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        vals = (ctypes.c_void_p * 1)(cf_true.value)
        # kCFTypeDictionaryKeyCallBacks/ValueCallBacks are also dlsym'd globals.
        key_cb = ctypes.addressof(
            ctypes.c_void_p.in_dll(_CF_LIB, "kCFTypeDictionaryKeyCallBacks")
        )
        val_cb = ctypes.addressof(
            ctypes.c_void_p.in_dll(_CF_LIB, "kCFTypeDictionaryValueCallBacks")
        )
        opts = _CF_LIB.CFDictionaryCreate(
            None, keys, vals, 1, key_cb, val_cb
        )
        try:
            return bool(_AX_LIB.AXIsProcessTrustedWithOptions(opts if prompt else None))
        finally:
            if opts:
                _CF_LIB.CFRelease(opts)
            if key:
                _CF_LIB.CFRelease(key)
    except Exception:
        # Fallback to non-prompting check.
        try:
            return bool(_AX_LIB.AXIsProcessTrusted())
        except Exception:
            return False


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
    "]": 30, "[": 33, "RIGHTBRACKET": 30, "LEFTBRACKET": 33,
    "BRACKETRIGHT": 30, "BRACKETLEFT": 33,
    "-": 27, "=": 24, "MINUS": 27, "EQUAL": 24, "PLUS": 24,
    ";": 41, "'": 39, ",": 43, ".": 47, "/": 44, "\\": 42, "`": 50,
    "SEMICOLON": 41, "APOSTROPHE": 39, "COMMA": 43, "PERIOD": 47,
    "SLASH": 44, "BACKSLASH": 42,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}

# Qt swaps Ctrl/Meta on macOS in QKeySequence strings: a saved "Ctrl" means
# the physical Cmd key, a saved "Meta" means the physical Ctrl key. We honor
# Qt's convention here so settings round-trip correctly with QKeySequenceEdit.
# The CMD/COMMAND/OPT/OPTION aliases are kept for hand-edited settings files.
_MAC_MODS = {
    "CMD": "cmd", "COMMAND": "cmd", "WIN": "cmd", "WINDOWS": "cmd",
    "CTRL": "cmd", "CONTROL": "cmd",
    "META": "ctrl",
    "OPT": "opt", "OPTION": "opt", "ALT": "opt",
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
    """Global hotkey via Carbon's RegisterEventHotKey.

    Carbon hotkeys are system-wide and do NOT require Accessibility permission.
    They're delivered by the OS to our app's event target regardless of focus.
    The Carbon Event Manager APIs are deprecated but still fully functional —
    every Spotlight-style utility on macOS uses them.
    """

    triggered = Signal()

    # Carbon modifier bit masks (these are NOT NSEvent flags).
    _CMD_KEY = 1 << 8       # 0x0100
    _SHIFT_KEY = 1 << 9     # 0x0200
    _OPTION_KEY = 1 << 11   # 0x0800
    _CONTROL_KEY = 1 << 12  # 0x1000

    # kEventClassKeyboard = 'keyb', kEventHotKeyPressed = 5
    _EVENT_CLASS_KEYBOARD = 0x6B657962  # b'keyb'
    _EVENT_HOTKEY_PRESSED = 5

    _carbon = None
    _handler_installed = False
    _hotkey_ref = ctypes.c_void_p(0)
    _handler_ref = ctypes.c_void_p(0)
    _hotkey_id_value = 0x53504F31  # 'SPO1'
    _last_fire_at: float = 0.0

    @classmethod
    def _load_carbon(cls) -> bool:
        if cls._carbon is not None:
            return True
        try:
            path = ctypes.util.find_library("Carbon")
            if not path:
                _dlog("Carbon framework not found")
                return False
            cls._carbon = ctypes.cdll.LoadLibrary(path)
            cls._carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            cls._carbon.RegisterEventHotKey.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint64,  # EventHotKeyID is {OSType signature; UInt32 id} packed
                ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            cls._carbon.RegisterEventHotKey.restype = ctypes.c_int32
            cls._carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
            cls._carbon.UnregisterEventHotKey.restype = ctypes.c_int32
            cls._carbon.InstallEventHandler.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            cls._carbon.InstallEventHandler.restype = ctypes.c_int32
            return True
        except Exception as e:
            _dlog(f"Carbon load failed: {e}")
            return False

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._wanted_mods: set[str] = set()
        self._wanted_keycode: int | None = None
        # Keep refs alive — ctypes callbacks get GC'd otherwise.
        self._handler_proc = None
        self._event_type = None

    def _emit_safe(self) -> None:
        now = time.monotonic()
        if now - HotkeyManager._last_fire_at < 0.30:
            return
        HotkeyManager._last_fire_at = now
        self.triggered.emit()

    def start(self, hotkey: str) -> bool:
        self.stop()
        if not self._load_carbon():
            return False
        mods, kc = _parse_hotkey_mac(hotkey)
        _dlog(f"start({hotkey!r}) -> parsed mods={mods} keycode={kc}")
        if kc is None:
            return False
        self._wanted_mods = mods
        self._wanted_keycode = kc

        carbon_mods = 0
        if "cmd" in mods:
            carbon_mods |= self._CMD_KEY
        if "opt" in mods:
            carbon_mods |= self._OPTION_KEY
        if "ctrl" in mods:
            carbon_mods |= self._CONTROL_KEY
        if "shift" in mods:
            carbon_mods |= self._SHIFT_KEY

        # Install event handler once per process.
        if not HotkeyManager._handler_installed:
            EventHandlerProc = ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )

            def _proc(_call_ref, _event, _user_data):
                try:
                    self._emit_safe()
                except Exception as e:
                    _dlog(f"hotkey proc exception: {e}")
                return 0  # noErr

            self._handler_proc = EventHandlerProc(_proc)

            class EventTypeSpec(ctypes.Structure):
                _fields_ = [("eventClass", ctypes.c_uint32),
                            ("eventKind", ctypes.c_uint32)]

            self._event_type = EventTypeSpec(
                self._EVENT_CLASS_KEYBOARD, self._EVENT_HOTKEY_PRESSED
            )
            target = self._carbon.GetApplicationEventTarget()
            err = self._carbon.InstallEventHandler(
                target,
                ctypes.cast(self._handler_proc, ctypes.c_void_p),
                1,
                ctypes.byref(self._event_type),
                None,
                ctypes.byref(HotkeyManager._handler_ref),
            )
            if err != 0:
                _dlog(f"InstallEventHandler failed: {err}")
                return False
            HotkeyManager._handler_installed = True

        # Pack EventHotKeyID {signature: OSType, id: UInt32} into UInt64.
        # Carbon expects this struct passed by VALUE in registers — packed as 8 bytes.
        hk_id = (self._hotkey_id_value << 32) | 1
        ref = ctypes.c_void_p(0)
        err = self._carbon.RegisterEventHotKey(
            kc, carbon_mods, hk_id, self._carbon.GetApplicationEventTarget(),
            0, ctypes.byref(ref),
        )
        if err != 0:
            _dlog(f"RegisterEventHotKey failed: {err} (mods={hex(carbon_mods)} kc={kc})")
            return False
        HotkeyManager._hotkey_ref = ref
        _dlog(f"hotkey registered: ref={ref.value} mods={hex(carbon_mods)} kc={kc}")
        return True

    def stop(self) -> None:
        if HotkeyManager._hotkey_ref and HotkeyManager._hotkey_ref.value:
            try:
                self._carbon.UnregisterEventHotKey(HotkeyManager._hotkey_ref)
            except Exception:
                pass
            HotkeyManager._hotkey_ref = ctypes.c_void_p(0)


# --------------------------------------------------------------------------
# Media controller — `media-control` CLI backend
# --------------------------------------------------------------------------
# macOS 15.4 closed MediaRemote.framework to third-party apps. The `ungive/
# media-control` Homebrew package works around this and exposes the same
# now-playing info via a CLI/JSON contract. We shell out to it.

_MEDIA_CONTROL_BIN: str | None = None


def _find_media_control() -> str | None:
    global _MEDIA_CONTROL_BIN
    if _MEDIA_CONTROL_BIN:
        return _MEDIA_CONTROL_BIN
    candidates = []
    # 1) Bundled inside the .app: <bundle>/Contents/Resources/media-control/bin/media-control
    import sys as _sys
    exe_dir = os.path.dirname(_sys.executable)  # .../Contents/MacOS
    bundle_root = os.path.dirname(exe_dir)       # .../Contents
    candidates.append(os.path.join(bundle_root, "Resources", "media-control", "bin", "media-control"))
    # 2) Homebrew installs as a fallback during dev/local runs.
    candidates += [
        "/opt/homebrew/bin/media-control",
        "/usr/local/bin/media-control",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            _MEDIA_CONTROL_BIN = c
            return c
    found = shutil.which("media-control")
    if found:
        _MEDIA_CONTROL_BIN = found
    return _MEDIA_CONTROL_BIN


class _NowPlayingFetcher:
    """Subprocess wrapper around `media-control get`."""

    def fetch(self, timeout: float = 1.0) -> tuple[dict | None, bool | None]:
        binary = _find_media_control()
        if not binary:
            return None, None
        try:
            proc = subprocess.run(
                [binary, "get"],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception as e:
            _dlog(f"media-control fetch failed: {e}")
            return None, None
        if proc.returncode != 0:
            return None, None
        out = proc.stdout.strip()
        if not out or out == "null":
            return None, None
        try:
            data = json.loads(out)
        except Exception:
            return None, None
        # Normalize into the dict shape expected by MediaController.
        info: dict = {}
        if "title" in data:
            info["title"] = str(data.get("title") or "")
        if "artist" in data:
            info["artist"] = str(data.get("artist") or "")
        if "album" in data:
            info["album"] = str(data.get("album") or "")
        if "duration" in data:
            info["duration"] = float(data.get("duration") or 0)
        if "elapsedTime" in data:
            info["elapsed"] = float(data.get("elapsedTime") or 0)
        artwork_b64 = data.get("artworkData")
        if artwork_b64:
            try:
                info["artwork"] = base64.b64decode(artwork_b64)
            except Exception:
                info["artwork"] = b""
        is_playing = data.get("playing")
        return info, (bool(is_playing) if is_playing is not None else None)

    @staticmethod
    def send_command(cmd: str) -> None:
        binary = _find_media_control()
        if not binary:
            return
        try:
            subprocess.run(
                [binary, cmd],
                capture_output=True, text=True, timeout=1.0,
            )
        except Exception as e:
            _dlog(f"media-control send_command({cmd!r}) failed: {e}")


def _ns_to_bytes(ns_data) -> bytes:
    try:
        return bytes(ns_data) if ns_data is not None else b""
    except Exception:
        return b""


class MediaController(QObject):
    info_updated = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fetcher = _NowPlayingFetcher()
        self._pos_baseline = 0.0
        self._pos_baseline_at = time.monotonic()
        self._last_track_key: tuple | None = None
        self._zero_streak = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(500)

        if not _find_media_control():
            QTimer.singleShot(
                100,
                lambda: self.info_updated.emit({
                    "available": False,
                    "reason": "Не найден media-control. Установи: brew install media-control",
                }),
            )

    def poll(self) -> None:
        threading.Thread(target=self._poll_blocking, daemon=True).start()

    def _poll_blocking(self) -> None:
        info, is_playing = self._fetcher.fetch()
        if info is None:
            self.info_updated.emit({"available": False, "reason": "Откройте трек в плеере"})
            return

        title = info.get("title", "")
        artist = info.get("artist", "")
        album = info.get("album", "")
        dur = float(info.get("duration", 0) or 0)
        elapsed = float(info.get("elapsed", 0) or 0)
        thumb_bytes = info.get("artwork") or b""
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
            "source": "macos.media-control",
        })

    def toggle_play(self) -> None:
        _NowPlayingFetcher.send_command("toggle-play-pause")

    def next_track(self) -> None:
        _NowPlayingFetcher.send_command("next-track")

    def prev_track(self) -> None:
        _NowPlayingFetcher.send_command("previous-track")

    def shutdown(self) -> None:
        try:
            self._poll_timer.stop()
        except Exception:
            pass
