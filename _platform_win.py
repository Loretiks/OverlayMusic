"""Windows backend: SMTC media controller, Win32 global hotkey, foreground hack.

Imported only on win32. The rest of overlay.py treats this module as opaque —
it must export: MediaController, HotkeyManager, force_foreground, BACKEND_ERR.
"""

from __future__ import annotations

import asyncio
import ctypes
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal


# --- WinRT (preferred) / winsdk (fallback) ---------------------------------
MediaManager = None
PlaybackStatus = None
DataReader = None
Buffer = None
InputStreamOptions = None
BACKEND_ERR: str | None = None

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import (
        DataReader,
        Buffer,
        InputStreamOptions,
    )
except Exception as e_winrt:
    try:
        from winsdk.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )
        from winsdk.windows.storage.streams import (  # type: ignore
            DataReader,
            Buffer,
            InputStreamOptions,
        )
    except Exception as e_winsdk:
        BACKEND_ERR = f"winrt: {e_winrt}; winsdk: {e_winsdk}"


# --------------------------------------------------------------------------
# Win32 hotkey infrastructure
# --------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_USER = 0x0400
WM_QUIT_LOOP = WM_USER + 1

_VK: dict[str, int] = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21,
    "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24, "LEFT": 0x25,
    "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "INSERT": 0x2D,
    "DELETE": 0x2E, "DEL": 0x2E,
}
for c in range(ord("0"), ord("9") + 1):
    _VK[chr(c)] = c
for c in range(ord("A"), ord("Z") + 1):
    _VK[chr(c)] = c
for i in range(1, 25):
    _VK[f"F{i}"] = 0x6F + i

_MODS = {
    "CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT, "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN, "WINDOWS": MOD_WIN, "META": MOD_WIN, "SUPER": MOD_WIN,
}


def force_foreground(hwnd: int) -> None:
    """SetForegroundWindow ограничен — обходим через AttachThreadInput."""
    if not hwnd:
        return
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_tid, cur_tid, False)


def _parse_hotkey(s: str) -> tuple[int, int | None]:
    parts = [p.strip().upper() for p in s.replace("-", "+").split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif p in _VK:
            vk = _VK[p]
    return mods, vk


class HotkeyManager(QObject):
    triggered = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop_event = threading.Event()

    def start(self, hotkey: str) -> bool:
        self.stop()
        mods, vk = _parse_hotkey(hotkey)
        if vk is None:
            return False
        self._stop_event.clear()
        ready = threading.Event()
        result: dict[str, bool] = {"ok": False}

        def worker() -> None:
            self._thread_id = kernel32.GetCurrentThreadId()
            ok = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
            result["ok"] = ok
            ready.set()
            if not ok:
                return
            try:
                msg = wintypes.MSG()
                while not self._stop_event.is_set():
                    ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret in (0, -1):
                        break
                    if msg.message == WM_HOTKEY:
                        self.triggered.emit()
                    elif msg.message == WM_QUIT_LOOP:
                        break
            finally:
                user32.UnregisterHotKey(None, 1)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        ready.wait(timeout=2)
        return result["ok"]

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            if self._thread_id:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT_LOOP, 0, 0)
            self._thread.join(timeout=1)
        self._thread = None
        self._thread_id = None


# --------------------------------------------------------------------------
# SMTC media controller (Windows Runtime)
# --------------------------------------------------------------------------
class MediaController(QObject):
    info_updated = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
        self._pos_baseline: float = 0.0
        self._pos_baseline_at: float = time.monotonic()
        self._last_track_key: tuple | None = None
        self._zero_streak: int = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(250)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            return None

    def poll(self) -> None:
        if MediaManager is None:
            self.info_updated.emit({"available": False, "reason": "winrt не установлен"})
            return
        self._submit(self._poll_async())

    async def _poll_async(self) -> None:
        try:
            mgr = await MediaManager.request_async()
            session = self._pick_session(mgr)
            if session is None:
                self.info_updated.emit({"available": False, "reason": "Откройте трек в плеере"})
                return
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()

            thumb_bytes: bytes | None = None
            if props.thumbnail is not None:
                try:
                    thumb_bytes = await self._read_thumbnail(props.thumbnail)
                except Exception:
                    thumb_bytes = None

            raw_pos = timeline.position.total_seconds() if timeline.position else 0.0
            dur = timeline.end_time.total_seconds() if timeline.end_time else 0.0
            is_playing = int(playback.playback_status) == int(PlaybackStatus.PLAYING)

            now = time.monotonic()
            track_key = (props.title or "", props.artist or "", round(dur, 1))
            track_changed = track_key != self._last_track_key

            if is_playing and raw_pos < 0.5 and not track_changed:
                self._zero_streak += 1
            else:
                self._zero_streak = 0

            position_reliable = self._zero_streak < 3

            if track_changed:
                self._last_track_key = track_key
                self._pos_baseline = raw_pos
                self._pos_baseline_at = now
            elif is_playing and position_reliable:
                predicted = self._pos_baseline + (now - self._pos_baseline_at)
                if abs(raw_pos - predicted) > 3.0:
                    self._pos_baseline = raw_pos
                    self._pos_baseline_at = now
            elif not is_playing:
                if raw_pos > 0.5 or self._pos_baseline < 0.5:
                    self._pos_baseline = raw_pos
                    self._pos_baseline_at = now

            if is_playing:
                effective_pos = self._pos_baseline + (now - self._pos_baseline_at)
                if dur > 0:
                    effective_pos = min(effective_pos, dur)
            else:
                effective_pos = self._pos_baseline

            info = {
                "available": True,
                "title": props.title or "",
                "artist": props.artist or "",
                "album": props.album_title or "",
                "thumbnail": thumb_bytes,
                "is_playing": is_playing,
                "position": effective_pos,
                "duration": dur,
                "position_reliable": position_reliable,
                "source": session.source_app_user_model_id or "",
            }
            self.info_updated.emit(info)
        except Exception as e:
            self.info_updated.emit({"available": False, "reason": f"SMTC: {e}"})

    def _pick_session(self, mgr):
        try:
            sessions = mgr.get_sessions()
            for s in sessions:
                aumid = (s.source_app_user_model_id or "").lower()
                if "spotify" in aumid:
                    return s
        except Exception:
            pass
        try:
            return mgr.get_current_session()
        except Exception:
            return None

    async def _read_thumbnail(self, stream_ref) -> bytes:
        ras = await stream_ref.open_read_async()
        size = ras.size
        if size == 0:
            return b""
        buf = Buffer(size)
        await ras.read_async(buf, size, InputStreamOptions.NONE)
        reader = DataReader.from_buffer(buf)
        out = bytearray(buf.length)
        reader.read_bytes(out)
        return bytes(out)

    def toggle_play(self) -> None:
        self._submit(self._cmd("play_pause"))

    def next_track(self) -> None:
        self._submit(self._cmd("next"))

    def prev_track(self) -> None:
        self._submit(self._cmd("prev"))

    async def _cmd(self, action: str) -> None:
        try:
            mgr = await MediaManager.request_async()
            session = self._pick_session(mgr)
            if session is None:
                return
            if action == "play_pause":
                await session.try_toggle_play_pause_async()
            elif action == "next":
                await session.try_skip_next_async()
            elif action == "prev":
                await session.try_skip_previous_async()
        except Exception:
            pass

    def shutdown(self) -> None:
        try:
            self._poll_timer.stop()
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
