"""Windows backend: SMTC media controller, Win32 global hotkey, foreground hack.

Imported only on win32. The rest of overlay.py treats this module as opaque —
it must export: MediaController, HotkeyManager, force_foreground, BACKEND_ERR.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
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

    # Как часто разрешаем повторно запрашивать try_get_media_properties_async —
    # это самая дорогая часть poll-цикла (NPSMSvc сериализует title/artist/
    # album/genres/track-id/обложку и шлёт через IPC). Раньше дёргалось каждые
    # 250 мс; теперь — только при смене длительности трека ИЛИ раз в 5 сек.
    PROPS_REFRESH_S = 5.0

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
        self._pos_baseline: float = 0.0
        self._pos_baseline_at: float = time.monotonic()
        self._last_track_key: tuple | None = None
        self._zero_streak: int = 0
        # Кэши для снижения нагрузки на NPSMSvc.
        self._mgr = None                  # MediaManager singleton
        self._cached_session = None       # последняя session-обёртка
        self._cached_aumid: str = ""      # source_app_user_model_id (IPC при каждом get!)
        self._cached_props = None         # последние media-properties
        self._cached_props_dur: float = -1.0
        self._cached_props_at: float = 0.0
        self._thumb_cache_key: tuple | None = None
        self._thumb_cache_bytes: bytes | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        # 1 секунда — прогресс-бар интерполируется локально через monotonic-
        # baseline, реальная нагрузка нужна только для смены трека / play-pause.
        self._poll_timer.start(1000)

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
            if self._mgr is None:
                self._mgr = await MediaManager.request_async()
            mgr = self._mgr
            session = self._pick_session(mgr)
            if session is None:
                # Сбрасываем кэшированную сессию — иначе при возврате музыки
                # будем держать stale объект.
                self._cached_session = None
                self._cached_aumid = ""
                self._cached_props = None
                self.info_updated.emit({"available": False, "reason": "Откройте трек в плеере"})
                return

            # Эти два get_*_properties — sync-обёртки над IPC, но дёшевые
            # (несколько байт state, без metadata-сериализации).
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()

            raw_pos = timeline.position.total_seconds() if timeline.position else 0.0
            dur = timeline.end_time.total_seconds() if timeline.end_time else 0.0
            is_playing = int(playback.playback_status) == int(PlaybackStatus.PLAYING)
            now = time.monotonic()
            dur_key = round(dur, 1)

            # source_app_user_model_id — каждое чтение тоже IPC. Берём только
            # при смене session-объекта.
            if session is not self._cached_session:
                try:
                    self._cached_aumid = session.source_app_user_model_id or ""
                except Exception:
                    self._cached_aumid = ""
                self._cached_session = session
                # Новая сессия — props-кэш точно невалиден.
                self._cached_props = None

            # Решаем нужно ли обновлять props (самая дорогая операция):
            #  • первый раз
            #  • сменилась длительность (= новый трек)
            #  • прошло PROPS_REFRESH_S сек (страховка для плееров без duration,
            #    например Yandex.Music — title/artist всё-таки обновятся)
            need_props_refresh = (
                self._cached_props is None
                or self._cached_props_dur != dur_key
                or (now - self._cached_props_at) > self.PROPS_REFRESH_S
            )
            if need_props_refresh:
                try:
                    self._cached_props = await session.try_get_media_properties_async()
                    self._cached_props_dur = dur_key
                    self._cached_props_at = now
                except Exception:
                    pass
            props = self._cached_props
            if props is None:
                # Совсем не удалось забрать metadata — отдаём заглушку, не кидаем UI в "нет музыки"
                self.info_updated.emit({"available": False, "reason": "Жду метаданные…"})
                return

            track_key = (props.title or "", props.artist or "", dur_key)
            track_changed = track_key != self._last_track_key

            # Обложка: тащим байты только при смене трека.
            thumb_bytes: bytes | None = self._thumb_cache_bytes
            if self._thumb_cache_key != track_key:
                thumb_bytes = None
                if props.thumbnail is not None:
                    try:
                        thumb_bytes = await self._read_thumbnail(props.thumbnail)
                    except Exception:
                        thumb_bytes = None
                self._thumb_cache_key = track_key
                self._thumb_cache_bytes = thumb_bytes

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
                "source": self._cached_aumid,
            }
            self.info_updated.emit(info)
        except Exception as e:
            # Сбросим кэш менеджера — возможно NPSMSvc перезапустился и наш
            # singleton stale; следующий poll возьмёт новый.
            self._mgr = None
            self.info_updated.emit({"available": False, "reason": f"SMTC: {e}"})

    def _pick_session(self, mgr):
        # get_current_session — 1 IPC. Раньше мы первым делом дёргали
        # get_sessions() и итерировали все — каждая s.source_app_user_model_id
        # это ещё IPC. У пользователя с Discord/GTA/Edge может быть 5+ сессий,
        # это давало 6+ лишних IPC к NPSMSvc на каждом poll-цикле.
        try:
            cur = mgr.get_current_session()
        except Exception:
            cur = None
        if cur is not None:
            return cur
        # Fallback: если current нет — берём первую попавшуюся медиа-сессию.
        try:
            sessions = mgr.get_sessions()
            return sessions[0] if sessions and len(sessions) > 0 else None
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


# --------------------------------------------------------------------------
# NPSMSvc watchdog
# --------------------------------------------------------------------------
# Системная служба «Now Playing Session Manager» (NPSMSvc_<sessionid>) —
# обслуживает SMTC-сессии. У неё есть известный баг: после некорректного
# disconnect какого-то клиента (типичный сценарий — закрытая вкладка Edge с
# активной media-session) служба застревает в high-CPU/high-memory loop —
# до 100% одного ядра и сотни МБ. Лечится только рестартом службы.
#
# Watchdog раз в 15 сек измеряет % CPU процесса службы через GetProcessTimes
# (cheap, без spawning subprocess'а). Если 2 раза подряд видим > порога —
# рестартуем службу (Restart-Service из PowerShell, требует админа — у нас
# есть благодаря scheduled task /RL HIGHEST). После рестарта — cooldown 5 мин
# чтобы не зацикливаться.
#
# CPU нормирован на одно ядро. Threshold 30% = «занимает треть одного ядра» —
# это на порядок выше любой нормальной фоновой активности NPSMSvc, у которой
# в healthy-состоянии CPU = 0.

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


def _ft_to_us(ft: _FILETIME) -> float:
    """FILETIME (100-ns ticks) → microseconds."""
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 10.0


def _get_process_cpu_us(pid: int) -> float | None:
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return None
    try:
        creation, exit_, kernel, user_t = (
            _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        )
        ok = ctypes.windll.kernel32.GetProcessTimes(
            h,
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user_t),
        )
        if not ok:
            return None
        return _ft_to_us(kernel) + _ft_to_us(user_t)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _find_npsm() -> tuple[str, int] | None:
    """Возвращает (имя службы, PID процесса) или None."""
    try:
        out = subprocess.check_output(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$s = Get-CimInstance Win32_Service -Filter \"Name like 'NPSMSvc%'\" | Select-Object -First 1; "
                "if ($s) { Write-Output \"$($s.Name)|$($s.ProcessId)\" }",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).decode("utf-8", errors="ignore").strip()
        if "|" in out:
            name, pid_s = out.split("|", 1)
            return name.strip(), int(pid_s.strip())
    except Exception:
        pass
    return None


def _restart_service(name: str) -> bool:
    try:
        r = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"Restart-Service -Name '{name}' -Force -ErrorAction Stop",
            ],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=15,
            creationflags=0x08000000,
        )
        return r.returncode == 0
    except Exception:
        return False


class NpsmWatchdog(QObject):
    """Перезапускает NPSMSvc если у неё затяжное high-CPU."""

    restarted = Signal(float)  # передаёт измеренный % CPU перед рестартом

    CHECK_INTERVAL_MS = 15_000      # проверяем раз в 15 сек
    CPU_THRESHOLD_PCT = 30.0        # > 30% одного ядра = аномалия
    BREACHES_NEEDED = 2             # 2 проверки подряд → рестарт (~30 сек high-CPU)
    COOLDOWN_S = 300.0              # 5 мин не трогаем после рестарта

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._svc_name: str | None = None
        self._svc_pid: int | None = None
        self._prev_cpu_us: float | None = None
        self._prev_wall: float | None = None
        self._breaches = 0
        self._last_restart_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._refresh_svc()
        self._timer.start()

    def stop(self) -> None:
        self._enabled = False
        self._timer.stop()

    def _refresh_svc(self) -> None:
        info = _find_npsm()
        if info is None:
            self._svc_name, self._svc_pid = None, None
            return
        self._svc_name, self._svc_pid = info
        self._prev_cpu_us = None
        self._prev_wall = None
        self._breaches = 0

    def _check(self) -> None:
        if not self._enabled:
            return
        if self._svc_pid is None:
            self._refresh_svc()
            if self._svc_pid is None:
                return

        now_wall = time.monotonic()
        cpu_us = _get_process_cpu_us(self._svc_pid)
        if cpu_us is None:
            # Процесс пропал (служба остановлена/перезапущена) — освежим.
            self._refresh_svc()
            return

        if self._prev_cpu_us is None or self._prev_wall is None:
            self._prev_cpu_us = cpu_us
            self._prev_wall = now_wall
            return

        d_cpu = cpu_us - self._prev_cpu_us
        d_wall = now_wall - self._prev_wall
        self._prev_cpu_us = cpu_us
        self._prev_wall = now_wall
        if d_wall <= 0:
            return

        # cpu_us — микросекунды CPU-времени. Доля одного ядра = d_cpu / (d_wall*1e6).
        cpu_pct_one_core = (d_cpu / (d_wall * 1_000_000.0)) * 100.0

        if cpu_pct_one_core <= self.CPU_THRESHOLD_PCT:
            self._breaches = 0
            return

        self._breaches += 1
        if self._breaches < self.BREACHES_NEEDED:
            return

        if (now_wall - self._last_restart_at) < self.COOLDOWN_S:
            return  # ещё в cooldown — не дёргаем

        # Поехали — рестартуем службу.
        name = self._svc_name or ""
        if name and _restart_service(name):
            self._last_restart_at = now_wall
            self._breaches = 0
            self.restarted.emit(cpu_pct_one_core)
            # Дадим службе ~2 сек подняться, потом обновим PID.
            QTimer.singleShot(2000, self._refresh_svc)
