"""OverlayMusic — поверх игр, управление через Windows SMTC.

Default hotkey: Ctrl+Alt+S (configurable via tray -> Настройки).
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from pypresence import Presence as _DiscordPresence
    try:
        from pypresence import ActivityType as _ActivityType
    except Exception:
        _ActivityType = None
except Exception:
    _DiscordPresence = None
    _ActivityType = None

# Platform backend (media controls + global hotkey + foreground)
if sys.platform == "darwin":
    from _platform_mac import (  # type: ignore
        MediaController,
        HotkeyManager,
        force_foreground,
        is_accessibility_trusted,
        request_accessibility,
        BACKEND_ERR as _BACKEND_ERR,
    )
else:
    from _platform_win import (  # type: ignore
        MediaController,
        HotkeyManager,
        force_foreground,
        BACKEND_ERR as _BACKEND_ERR,
    )

    def is_accessibility_trusted() -> bool:
        return True

    def request_accessibility(prompt: bool = True) -> bool:
        return True

from PySide6.QtCore import (
    Qt,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QTimer,
    QUrl,
    Signal,
    QObject,
    QPoint,
    QRect,
    QSize,
    QSizeF,
    QSize as _QSize,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
APP_NAME = "OverlayMusic"

# Discord Application ID — зашит дефолтом, поле в настройках можно не трогать.
# Это твоё личное приложение на discord.com/developers (имя "Melanholy").
# Чтобы поменять имя/иконку, которые видны в активности, — открой страницу
# приложения там же и измени Name / App Icon.
DEFAULT_DISCORD_CLIENT_ID = "1499319405752881212"

DEFAULT_SETTINGS = {
    # On macOS, Qt swaps Ctrl/Meta in QKeySequence: "Ctrl+Alt+]" here means
    # the physical Cmd+Opt+] keys.
    "hotkey_toggle": "Ctrl+Alt+]" if sys.platform == "darwin" else "Ctrl+Alt+S",
    "hotkey_prev": "Left",
    "hotkey_next": "Right",
    "hotkey_playpause": "Down",
    "x": None,
    "y": None,
    "discord_enabled": False,
    "discord_client_id": "",
    "browser_provider": "yandex",  # yandex | youtube | spotify | apple
    "transparent_mode": False,
}

BROWSER_PROVIDERS = [
    ("yandex",  "Яндекс.Музыка"),
    ("youtube", "YouTube Music"),
    ("spotify", "Spotify"),
    ("apple",   "Apple Music"),
]


def settings_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    folder = base / APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    data: dict = {}
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    # Migrate old "hotkey" -> "hotkey_toggle"
    if "hotkey" in data and "hotkey_toggle" not in data:
        data["hotkey_toggle"] = data.pop("hotkey")
    return {**DEFAULT_SETTINGS, **data}


def save_settings(data: dict) -> None:
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Browser link builder
# --------------------------------------------------------------------------
class YandexTrackLookup:
    """Public Yandex Music search API → direct track URL.
    Falls back to a search URL if the track isn't found.
    Cached forever per (title, artist) key.
    """

    _cache: dict[tuple[str, str], str | None] = {}
    _lock = threading.Lock()

    @classmethod
    def direct_url(cls, title: str, artist: str) -> str | None:
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title:
            return None
        key = (title.lower(), artist.lower())
        with cls._lock:
            if key in cls._cache:
                return cls._cache[key]

        url: str | None = None
        try:
            term = urllib.parse.quote(f"{artist} {title}".strip())
            api = (
                f"https://api.music.yandex.net/search?text={term}"
                "&type=track&page=0&nocorrect=false"
            )
            req = urllib.request.Request(
                api, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
            tracks = (
                data.get("result", {}).get("tracks", {}).get("results", [])
            ) or []
            if tracks:
                tid = tracks[0].get("id") or tracks[0].get("realId")
                if tid:
                    url = f"https://music.yandex.ru/track/{tid}"
        except Exception:
            url = None

        with cls._lock:
            cls._cache[key] = url
        return url


def make_browser_url(provider: str, title: str, artist: str) -> str:
    q = urllib.parse.quote(f"{artist} {title}".strip())
    if not q:
        return ""
    if provider == "spotify":
        return f"https://open.spotify.com/search/{q}"
    if provider == "youtube":
        return f"https://music.youtube.com/search?q={q}"
    if provider == "apple":
        return f"https://music.apple.com/search?term={q}"
    # Yandex: try direct track URL first, fall back to search.
    direct = YandexTrackLookup.direct_url(title, artist)
    if direct:
        return direct
    return f"https://music.yandex.ru/search?text={q}"


# --------------------------------------------------------------------------
# Cover-art resolver: SMTC bytes → catbox.moe (real cover); iTunes fallback.
# --------------------------------------------------------------------------
def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


class CoverResolver:
    """Returns a public HTTPS URL for the track's cover.
    Primary: upload SMTC bytes to catbox.moe (the actual cover the user sees).
    Fallback: iTunes Search API by title+artist.
    Cached forever per (title, artist) key.
    """

    _cache: dict[tuple[str, str], str | None] = {}
    _lock = threading.Lock()

    @classmethod
    def resolve(cls, title: str, artist: str, image_bytes: bytes | None) -> str | None:
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title:
            return None
        key = (title.lower(), artist.lower())
        with cls._lock:
            if key in cls._cache:
                return cls._cache[key]

        url: str | None = None
        if image_bytes:
            url = cls._upload_catbox(image_bytes)
        if not url:
            url = cls._itunes_lookup(title, artist)

        with cls._lock:
            cls._cache[key] = url
        return url

    @staticmethod
    def _upload_catbox(image_bytes: bytes) -> str | None:
        if not image_bytes:
            return None
        ext = _detect_image_ext(image_bytes)
        boundary = "----overlay" + os.urandom(8).hex()
        sep = f"--{boundary}\r\n".encode()
        end = f"--{boundary}--\r\n".encode()

        body = bytearray()
        body += sep
        body += b'Content-Disposition: form-data; name="reqtype"\r\n\r\nfileupload\r\n'
        body += sep
        body += (
            f'Content-Disposition: form-data; name="fileToUpload"; '
            f'filename="cover.{ext}"\r\n'
            f"Content-Type: image/{ext}\r\n\r\n"
        ).encode()
        body += image_bytes
        body += b"\r\n"
        body += end

        req = urllib.request.Request(
            "https://catbox.moe/user/api.php",
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                # catbox.moe блокирует кастомные UA — прикинемся curl
                "User-Agent": "curl/8.4.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = r.read().decode("utf-8", errors="ignore").strip()
            if txt.startswith("https://"):
                return txt
        except Exception:
            return None
        return None

    @staticmethod
    def _itunes_lookup(title: str, artist: str) -> str | None:
        try:
            term = urllib.parse.quote(f"{artist} {title}".strip())
            api = (
                f"https://itunes.apple.com/search?term={term}"
                "&media=music&entity=song&limit=1"
            )
            req = urllib.request.Request(api, headers={"User-Agent": "MusicOverlay/1.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
            results = data.get("results") or []
            if results:
                art = results[0].get("artworkUrl100") or ""
                if art:
                    return art.replace("100x100bb", "600x600bb")
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------
# Discord Rich Presence
# --------------------------------------------------------------------------
class DiscordPresence:
    """Background Discord IPC. Reconnects lazily; dedups updates by track."""

    def __init__(self) -> None:
        self._client_id = ""
        self._enabled = False
        self._rpc = None
        self._last_key: tuple | None = None
        self._lock = threading.Lock()
        self._busy = threading.Event()

    def configure(self, enabled: bool, client_id: str) -> None:
        client_id = (client_id or "").strip() or DEFAULT_DISCORD_CLIENT_ID
        new_enabled = bool(enabled and _DiscordPresence is not None and client_id)
        with self._lock:
            id_changed = client_id != self._client_id
            self._client_id = client_id
            self._enabled = new_enabled
            if id_changed or not new_enabled:
                self._disconnect_locked()
                self._last_key = None

    def _ensure_connected_locked(self) -> bool:
        if self._rpc is not None:
            return True
        if not self._enabled or not self._client_id:
            return False
        try:
            rpc = _DiscordPresence(self._client_id)
            rpc.connect()
            self._rpc = rpc
            return True
        except Exception:
            self._rpc = None
            return False

    def _disconnect_locked(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.clear()
            except Exception:
                pass
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None

    def push(self, info: dict, browser_provider: str) -> None:
        if not self._enabled:
            return
        if self._busy.is_set():
            return
        if not info or not info.get("available"):
            key = ("__cleared__",)
        else:
            key = (
                info.get("title") or "",
                info.get("artist") or "",
                bool(info.get("is_playing")),
            )
        with self._lock:
            if key == self._last_key:
                return
            self._last_key = key
        self._busy.set()
        threading.Thread(
            target=self._worker,
            args=(dict(info), browser_provider),
            daemon=True,
        ).start()

    def _worker(self, info: dict, browser_provider: str) -> None:
        try:
            with self._lock:
                if not self._ensure_connected_locked():
                    return
                rpc = self._rpc
            if rpc is None:
                return

            if not info.get("available"):
                try:
                    rpc.clear()
                except Exception:
                    with self._lock:
                        self._disconnect_locked()
                return

            title = (info.get("title") or "").strip()[:128] or "Музыка"
            artist = (info.get("artist") or "").strip()[:128]
            album = (info.get("album") or "").strip()[:128]
            playing = bool(info.get("is_playing"))
            pos = float(info.get("position") or 0)
            dur = float(info.get("duration") or 0)

            cover = CoverResolver.resolve(title, artist, info.get("thumbnail"))
            kwargs: dict = {
                "details": title,
                "large_text": album or title,
            }
            if artist:
                kwargs["state"] = artist
            if cover:
                kwargs["large_image"] = cover
            if playing and dur > 0:
                now = int(time.time())
                kwargs["start"] = now - int(pos)
                kwargs["end"] = now + int(max(1, dur - pos))
            else:
                kwargs["small_text"] = "На паузе"
                if cover:
                    kwargs["small_image"] = cover

            if _ActivityType is not None:
                kwargs["activity_type"] = _ActivityType.LISTENING

            url = make_browser_url(browser_provider, title, artist)
            if url:
                btn_labels = {
                    "yandex":  "🎵 Открыть в Я.Музыке",
                    "spotify": "🎵 Открыть в Spotify",
                    "youtube": "🎵 Открыть на YouTube",
                    "apple":   "🎵 Открыть в Apple Music",
                }
                kwargs["buttons"] = [{
                    "label": btn_labels.get(browser_provider, "🎵 Открыть трек"),
                    "url": url,
                }]

            try:
                rpc.update(**kwargs)
            except Exception:
                with self._lock:
                    self._disconnect_locked()
        finally:
            self._busy.clear()

    def shutdown(self) -> None:
        with self._lock:
            self._disconnect_locked()


# --------------------------------------------------------------------------
# UI primitives
# --------------------------------------------------------------------------
ACCENT_DEFAULT = QColor(0x6E, 0x8C, 0xFF)  # cool neutral, used when no cover

# Backwards-compat alias for any leftover refs.
ACCENT = ACCENT_DEFAULT


def extract_accent(image_bytes: bytes | None) -> QColor:
    """Pick a vivid dominant color from a cover image (16x16 sample)."""
    if not image_bytes:
        return ACCENT_DEFAULT
    img = QImage()
    if not img.loadFromData(image_bytes):
        return ACCENT_DEFAULT
    small = img.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    best_score = -1.0
    best = ACCENT_DEFAULT
    for y in range(16):
        for x in range(16):
            c = QColor(small.pixel(x, y))
            h, s, v, _ = c.getHsvF()
            # Prefer saturated, mid-bright pixels (avoid pure black/white).
            score = (s ** 1.4) * (1.0 - abs(v - 0.62)) * (0.4 + 0.6 * v)
            if score > best_score:
                best_score = score
                best = c
    h, s, v, _ = best.getHsvF()
    s = max(0.55, min(1.0, s * 1.25 + 0.05))
    v = max(0.55, min(0.85, v + 0.05))
    return QColor.fromHsvF(h, s, v)


def icon_color_for_bg(bg: QColor) -> QColor:
    """Подбирает цвет иконки (чёрный/белый) под фон по перцептивной яркости.
    Yellow / light cyan → тёмная иконка. Deep blue / red → белая."""
    r = bg.red() / 255.0
    g = bg.green() / 255.0
    b = bg.blue() / 255.0
    # WCAG-ish relative luminance (без линеаризации — этого достаточно для UI).
    L = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if L > 0.62:
        return QColor(18, 18, 22, 248)
    return QColor(255, 255, 255, 248)


def make_backdrop(image_bytes: bytes | None, w: int, h: int) -> QPixmap | None:
    """Produce a soft, blurred cover-as-backdrop pixmap by aggressive
    downscale → upscale (cheap, GPU-accelerated, no extra deps)."""
    if not image_bytes or w <= 0 or h <= 0:
        return None
    img = QImage()
    if not img.loadFromData(image_bytes):
        return None
    # Heavy downscale-then-upscale = pseudo-Gaussian blur.
    tiny = img.scaled(40, 40, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    bumped = tiny.scaled(max(w * 2, 256), max(h * 2, 256),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    final = bumped.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return QPixmap.fromImage(final)


class CoverLabel(QLabel):
    def __init__(self, size: int = 110, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radius = 14
        self._size = size
        self.setFixedSize(size, size)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        pm = QPixmap(self._size, self._size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self._size, self._size, self._radius, self._radius)
        # Нейтральная подложка под цвет карточки + чуть светлее (на тон).
        # Реальная обложка визуально вылетит на этом фоне; плейсхолдер уйдёт назад.
        p.fillPath(path, QColor(0x1A, 0x1A, 0x20))
        p.setPen(QColor(255, 255, 255, 51))  # ~20% белого
        f = QFont("Segoe UI Symbol", int(self._size * 0.34))
        f.setBold(False)
        p.setFont(f)
        p.drawText(QRect(0, 0, self._size, self._size), Qt.AlignCenter, "♪")
        p.end()
        self.setPixmap(pm)

    def set_bytes(self, data: bytes | None) -> None:
        if not data:
            self._show_placeholder()
            return
        img = QImage()
        if not img.loadFromData(data):
            self._show_placeholder()
            return
        scaled = QPixmap.fromImage(img).scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        x = max(0, (scaled.width() - self._size) // 2)
        y = max(0, (scaled.height() - self._size) // 2)
        scaled = scaled.copy(x, y, self._size, self._size)
        out = QPixmap(self._size, self._size)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self._size, self._size, self._radius, self._radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, scaled)
        p.end()
        self.setPixmap(out)


class EqualizerOverlay(QWidget):
    """Маленький эквалайзер 4 столбика — прыгает когда играет, замирает на паузе.
    Рисуется поверх обложки в правом нижнем углу с тёмной pill-подложкой
    для читаемости на любых обложках."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._bar_w = 3
        self._gap = 3
        self._bars = 4
        self._inner_h = 14
        pad_x, pad_y = 6, 4
        w = self._bars * self._bar_w + (self._bars - 1) * self._gap + pad_x * 2
        h = self._inner_h + pad_y * 2
        self.setFixedSize(w, h)
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._t = 0.0
        self._playing = False
        self._timer = QTimer(self)
        self._timer.setInterval(60)  # ~16 fps хватает для глифа 4×14
        self._timer.timeout.connect(self._tick)

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._timer.start()
        else:
            self._timer.stop()
            self.update()

    def _tick(self) -> None:
        self._t += 0.16
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Тёмная pill-подложка для контраста на светлых обложках.
        bg = QColor(0, 0, 0, 130)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        r = self.height() / 2
        p.drawRoundedRect(self.rect(), r, r)

        bar_color = QColor(255, 255, 255, 235)
        p.setBrush(bar_color)
        for i in range(self._bars):
            if self._playing:
                # Разные фазы и частоты — каждый столбик живёт сам по себе.
                phase = i * 1.1
                freq = 1.5 + 0.35 * i
                amp = 0.5 + 0.5 * math.sin(self._t * freq + phase)
                amp = amp ** 0.7
                ratio = 0.22 + 0.78 * amp
            else:
                ratio = 0.22  # короткие столбики на паузе
            bh = max(2, int(self._inner_h * ratio))
            x = self._pad_x + i * (self._bar_w + self._gap)
            y = self._pad_y + (self._inner_h - bh)
            br = self._bar_w / 2.0
            p.drawRoundedRect(QRectF(x, y, self._bar_w, bh), br, br)


class ProgressBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cur = 0.0
        self._max = 1.0
        self._paused = False
        self._accent = ACCENT_DEFAULT
        self.setFixedHeight(4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_progress(self, cur: float, total: float) -> None:
        self._max = max(0.001, total)
        self._cur = max(0.0, min(cur, total))
        self.update()

    def set_paused(self, paused: bool) -> None:
        if self._paused != paused:
            self._paused = paused
            self.update()

    def set_accent(self, color: QColor) -> None:
        self._accent = QColor(color)
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        r = h / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(0, 0, w, h, r, r)
        ratio = self._cur / self._max if self._max > 0 else 0
        fg = int(w * ratio)
        if fg > 0:
            fill = QColor(self._accent)
            if self._paused:
                fill.setAlpha(95)
            p.setBrush(fill)
            p.drawRoundedRect(0, 0, fg, h, r, r)


# --------------------------------------------------------------------------
# Vector media controls (consistent across Windows versions)
# --------------------------------------------------------------------------
class CtrlButton(QPushButton):
    """Кнопка медиаконтрола.
    primary=True → большая заливная круглая кнопка с акцентным цветом.
    primary=False → ghost-стиль (прозрачная, hover-фон)."""

    def __init__(
        self,
        kind: str,
        size: int,
        primary: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind  # "play" | "pause" | "prev" | "next"
        self._size = size
        self._primary = primary
        self._accent = ACCENT_DEFAULT
        self._hover = False
        self._pressed = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("")
        # Через mouseTrack-флаги Qt сам обновит pseudo-state — но мы рисуем
        # сами в paintEvent, поэтому слушаем enter/leave вручную.
        self.setAttribute(Qt.WA_Hover, True)

    def set_kind(self, kind: str) -> None:
        if kind != self.kind:
            self.kind = kind
            self.update()

    def set_accent(self, color: QColor) -> None:
        self._accent = QColor(color)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._pressed = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = False
            self.update()
        super().mouseReleaseEvent(e)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self._size
        cx = cy = s / 2.0
        rect = QRectF(0, 0, s, s)

        if self._primary:
            # Плоская заливная кнопка цвета акцента. Hover/press — мягкая коррекция.
            base = QColor(self._accent)
            if self._pressed:
                base = base.darker(112)
            elif self._hover:
                base = base.lighter(106)
            p.setPen(Qt.NoPen)
            p.setBrush(base)
            p.drawEllipse(rect)
            # Цвет иконки выбираем под яркость фона: жёлтый → чёрная иконка,
            # тёмно-синий → белая. Авто-контраст.
            glyph_color = icon_color_for_bg(base)
        else:
            # Ghost: круглый фон только при hover/press.
            if self._pressed:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 50))
                p.drawEllipse(rect)
            elif self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawEllipse(rect)
            glyph_color = QColor(255, 255, 255, 230)

        # Все иконки — жирные, заливкой, с pill-радиусом у баров.
        if self.kind == "play":
            tw = s * 0.34
            th = s * 0.40
            ox = s * 0.05  # оптический сдвиг вправо для равновесия
            path = QPainterPath()
            path.moveTo(cx - tw * 0.42 + ox, cy - th)
            path.lineTo(cx + tw + ox, cy)
            path.lineTo(cx - tw * 0.42 + ox, cy + th)
            path.closeSubpath()
            p.fillPath(path, glyph_color)
        elif self.kind == "pause":
            # Жирные pill-bars (как на референсе Я.Музыки).
            bw = s * 0.16
            bh = s * 0.46
            gap = s * 0.07
            r = bw / 2.0  # полная "таблетка"
            left = QPainterPath()
            left.addRoundedRect(QRectF(cx - gap - bw, cy - bh / 2, bw, bh), r, r)
            right = QPainterPath()
            right.addRoundedRect(QRectF(cx + gap, cy - bh / 2, bw, bh), r, r)
            p.fillPath(left, glyph_color)
            p.fillPath(right, glyph_color)
        elif self.kind == "prev":
            tw = s * 0.32
            th = s * 0.36
            bw = s * 0.10
            bh = th * 2.0
            r = bw / 2.0  # pill
            tri = QPainterPath()
            tri.moveTo(cx + tw * 0.5, cy - th)
            tri.lineTo(cx - tw * 0.5, cy)
            tri.lineTo(cx + tw * 0.5, cy + th)
            tri.closeSubpath()
            bar = QPainterPath()
            bar.addRoundedRect(
                QRectF(cx - tw * 0.5 - bw - s * 0.03, cy - bh / 2, bw, bh), r, r
            )
            p.fillPath(tri, glyph_color)
            p.fillPath(bar, glyph_color)
        elif self.kind == "next":
            tw = s * 0.32
            th = s * 0.36
            bw = s * 0.10
            bh = th * 2.0
            r = bw / 2.0
            tri = QPainterPath()
            tri.moveTo(cx - tw * 0.5, cy - th)
            tri.lineTo(cx + tw * 0.5, cy)
            tri.lineTo(cx - tw * 0.5, cy + th)
            tri.closeSubpath()
            bar = QPainterPath()
            bar.addRoundedRect(
                QRectF(cx + tw * 0.5 + s * 0.03, cy - bh / 2, bw, bh), r, r
            )
            p.fillPath(tri, glyph_color)
            p.fillPath(bar, glyph_color)
        p.end()


class IconButton(QPushButton):
    """Маленькая ghost-кнопка с глифом (⚙, ×) и круглым hover-фоном."""

    def __init__(self, glyph: str, size: int = 28, parent: QWidget | None = None) -> None:
        super().__init__(glyph, parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        f = QFont("Segoe UI", int(size * 0.50))
        f.setWeight(QFont.Medium)
        self.setFont(f)
        r = size // 2
        self.setStyleSheet(
            f"""
            IconButton {{
                background: transparent;
                color: rgba(255,255,255,0.65);
                border: none;
                border-radius: {r}px;
                padding: 0px;
            }}
            IconButton:hover {{
                background: rgba(255,255,255,0.10);
                color: #ffffff;
            }}
            IconButton:pressed {{
                background: rgba(255,255,255,0.18);
            }}
            """
        )


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки — OverlayMusic")
        self.setMinimumWidth(420)
        root = QVBoxLayout(self)
        root.setSpacing(6)

        global_lbl = QLabel("<b>Глобальные</b> — работают всегда:")
        root.addWidget(global_lbl)

        global_form = QFormLayout()
        global_form.setSpacing(6)
        self.toggle_edit = QKeySequenceEdit(QKeySequence(settings["hotkey_toggle"]))
        global_form.addRow("Показать / скрыть оверлей:", self.toggle_edit)
        root.addLayout(global_form)

        root.addSpacing(8)
        local_lbl = QLabel("<b>В оверлее</b> — когда он открыт и в фокусе:")
        root.addWidget(local_lbl)

        local_form = QFormLayout()
        local_form.setSpacing(6)
        self.prev_edit = QKeySequenceEdit(QKeySequence(settings["hotkey_prev"]))
        self.next_edit = QKeySequenceEdit(QKeySequence(settings["hotkey_next"]))
        self.play_edit = QKeySequenceEdit(QKeySequence(settings["hotkey_playpause"]))
        local_form.addRow("Предыдущий трек:", self.prev_edit)
        local_form.addRow("Следующий трек:", self.next_edit)
        local_form.addRow("Пауза / воспроизведение:", self.play_edit)
        root.addLayout(local_form)

        hint = QLabel(
            "Esc всегда скрывает оверлей.\n"
            "Глобальный хоткей должен содержать модификатор (Ctrl/Alt/Shift).",
            self,
        )
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        root.addSpacing(6)
        root.addWidget(hint)

        root.addSpacing(10)
        discord_lbl = QLabel("<b>Discord</b> — статус «слушаю музыку»:")
        root.addWidget(discord_lbl)

        discord_form = QFormLayout()
        discord_form.setSpacing(6)
        self.discord_enabled = QCheckBox("Включить Rich Presence")
        self.discord_enabled.setChecked(bool(settings.get("discord_enabled")))
        discord_form.addRow("", self.discord_enabled)

        # Client ID row: input + "Создать ↗" button.
        # Скрываем поле полностью, если дефолтный ID зашит в коде.
        self.discord_id_edit = QLineEdit(settings.get("discord_client_id", "") or "")
        if DEFAULT_DISCORD_CLIENT_ID:
            self.discord_id_edit.setPlaceholderText(
                "(используется встроенный; впиши свой, чтобы заменить)"
            )
        else:
            self.discord_id_edit.setPlaceholderText("Application ID")

        id_row = QHBoxLayout()
        id_row.setSpacing(6)
        id_row.addWidget(self.discord_id_edit, 1)
        create_btn = QPushButton("Создать ↗")
        create_btn.setToolTip("Открыть discord.com/developers/applications в браузере")
        create_btn.setCursor(Qt.PointingHandCursor)
        create_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://discord.com/developers/applications")
            )
        )
        id_row.addWidget(create_btn)
        discord_form.addRow("Client ID:", id_row)

        self.browser_combo = QComboBox()
        for key, label in BROWSER_PROVIDERS:
            self.browser_combo.addItem(label, key)
        cur = settings.get("browser_provider", "yandex")
        idx = max(0, self.browser_combo.findData(cur))
        self.browser_combo.setCurrentIndex(idx)
        discord_form.addRow("Кнопка ведёт на:", self.browser_combo)
        root.addLayout(discord_form)

        if DEFAULT_DISCORD_CLIENT_ID:
            d_hint = QLabel(
                "Client ID уже зашит в код — настройка не нужна. Просто включи галочку.",
                self,
            )
        else:
            d_hint = QLabel(
                "Один раз настроить (≈30 сек):\n"
                "  1. Нажми «Создать ↗» → New Application → имя (например «Яндекс Музыка»)\n"
                "  2. На странице приложения скопируй «Application ID»\n"
                "  3. Вставь в поле выше — больше никогда не потребуется",
                self,
            )
        d_hint.setStyleSheet("color: #888; font-size: 9pt;")
        d_hint.setWordWrap(True)
        root.addWidget(d_hint)

        if _DiscordPresence is None:
            warn = QLabel(
                "⚠ pypresence не установлен. Discord-статус работать не будет.\n"
                "Установите: pip install pypresence",
                self,
            )
            warn.setStyleSheet("color: #c9a227; font-size: 9pt;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        root.addSpacing(10)
        appearance_lbl = QLabel("<b>Внешний вид</b>:")
        root.addWidget(appearance_lbl)
        self.transparent_check = QCheckBox(
            "Полностью прозрачный режим (без подложки и затемнения)"
        )
        self.transparent_check.setChecked(bool(settings.get("transparent_mode")))
        root.addWidget(self.transparent_check)
        t_hint = QLabel(
            "Скрывает фон оверлея — остаются только обложка, текст и кнопки. "
            "Удобно поверх тёмных игр; на светлом фоне текст может быть плохо читаем.",
            self,
        )
        t_hint.setStyleSheet("color: #888; font-size: 9pt;")
        t_hint.setWordWrap(True)
        root.addWidget(t_hint)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        bb.button(QDialogButtonBox.Ok).setText("Сохранить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def values(self) -> dict:
        return {
            "hotkey_toggle": self.toggle_edit.keySequence().toString(),
            "hotkey_prev": self.prev_edit.keySequence().toString(),
            "hotkey_next": self.next_edit.keySequence().toString(),
            "hotkey_playpause": self.play_edit.keySequence().toString(),
            "discord_enabled": self.discord_enabled.isChecked(),
            "discord_client_id": self.discord_id_edit.text().strip(),
            "browser_provider": self.browser_combo.currentData() or "yandex",
            "transparent_mode": self.transparent_check.isChecked(),
        }


# --------------------------------------------------------------------------
# Overlay window
# --------------------------------------------------------------------------
class OverlayWindow(QWidget):
    request_settings = Signal()
    request_quit = Signal()
    hide_requested = Signal()
    hidden_after_fade = Signal()

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self._settings = settings
        self._dragging = False
        self._drag_offset = QPoint()
        self._backdrop: QPixmap | None = None
        self._cover_bytes: bytes | None = None
        self._accent: QColor = ACCENT_DEFAULT
        self._last_thumb_hash: int = 0
        self._transparent_mode: bool = bool(settings.get("transparent_mode"))

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(480, 168)

        self._save_pos_timer = QTimer(self)
        self._save_pos_timer.setSingleShot(True)
        self._save_pos_timer.setInterval(500)
        self._save_pos_timer.timeout.connect(self._persist_position)

        self._build_ui()
        self._apply_style()
        self._build_shortcuts()
        self._build_animations()

        x, y = settings.get("x"), settings.get("y")
        if x is not None and y is not None:
            self.move(int(x), int(y))
        else:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.top() + 60)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 18, 18)
        outer.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(18)

        self.cover = CoverLabel(96)
        top.addWidget(self.cover, 0, Qt.AlignTop)

        # Эквалайзер поверх обложки — правый нижний угол с небольшим отступом.
        self.equalizer = EqualizerOverlay(self.cover)
        eq_inset = 6
        self.equalizer.move(
            self.cover.width() - self.equalizer.width() - eq_inset,
            self.cover.height() - self.equalizer.height() - eq_inset,
        )
        self.equalizer.hide()

        right = QVBoxLayout()
        right.setSpacing(0)

        # Header: title (растягивается) + ⚙ + ×
        header = QHBoxLayout()
        header.setSpacing(2)
        header.setContentsMargins(0, 0, 0, 0)

        self.title_lbl = QLabel("Музыка не играет")
        self.title_lbl.setObjectName("title")
        self.title_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        header.addWidget(self.title_lbl, 1, Qt.AlignVCenter)

        self.settings_btn = IconButton("⚙", 24)
        self.settings_btn.setToolTip("Настройки")
        self.settings_btn.clicked.connect(self.request_settings.emit)
        header.addWidget(self.settings_btn, 0, Qt.AlignVCenter)

        self.close_btn = IconButton("✕", 24)
        self.close_btn.setToolTip("Скрыть")
        self.close_btn.clicked.connect(self.hide_requested.emit)
        header.addWidget(self.close_btn, 0, Qt.AlignVCenter)
        right.addLayout(header)

        self.artist_lbl = QLabel("Откройте трек в плеере")
        self.artist_lbl.setObjectName("artist")
        right.addWidget(self.artist_lbl)

        right.addStretch(1)

        # Progress bar
        self.progress = ProgressBar()
        right.addWidget(self.progress)

        right.addSpacing(4)

        # Bottom row: tCur · · · prev play next · · · tDur
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.setContentsMargins(0, 0, 0, 0)

        self.time_cur = QLabel("0:00")
        self.time_cur.setObjectName("time")
        self.time_cur.setMinimumWidth(36)
        self.time_dur = QLabel("0:00")
        self.time_dur.setObjectName("time")
        self.time_dur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_dur.setMinimumWidth(36)

        self.prev_btn = CtrlButton("prev", 32, primary=False)
        self.prev_btn.setToolTip("Предыдущий трек")
        self.play_btn = CtrlButton("play", 42, primary=True)
        self.play_btn.setToolTip("Пауза / воспроизведение")
        self.play_btn.set_accent(self._accent)
        self.next_btn = CtrlButton("next", 32, primary=False)
        self.next_btn.setToolTip("Следующий трек")

        bottom.addWidget(self.time_cur, 0, Qt.AlignVCenter)
        bottom.addStretch(1)
        bottom.addWidget(self.prev_btn, 0, Qt.AlignVCenter)
        bottom.addWidget(self.play_btn, 0, Qt.AlignVCenter)
        bottom.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        bottom.addStretch(1)
        bottom.addWidget(self.time_dur, 0, Qt.AlignVCenter)
        right.addLayout(bottom)

        top.addLayout(right, 1)
        outer.addLayout(top)

    def _build_shortcuts(self) -> None:
        # Window-local shortcuts (only when overlay is focused).
        self.sc_prev = QShortcut(QKeySequence(), self)
        self.sc_prev.setContext(Qt.WindowShortcut)
        self.sc_next = QShortcut(QKeySequence(), self)
        self.sc_next.setContext(Qt.WindowShortcut)
        self.sc_play = QShortcut(QKeySequence(), self)
        self.sc_play.setContext(Qt.WindowShortcut)
        # Esc always hides — non-configurable convenience.
        self.sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.sc_esc.setContext(Qt.WindowShortcut)
        self.sc_esc.activated.connect(self.hide_requested.emit)

    def set_local_hotkeys(self, prev: str, nxt: str, play: str) -> None:
        self.sc_prev.setKey(QKeySequence(prev))
        self.sc_next.setKey(QKeySequence(nxt))
        self.sc_play.setKey(QKeySequence(play))

    def set_transparent_mode(self, enabled: bool) -> None:
        if self._transparent_mode == bool(enabled):
            return
        self._transparent_mode = bool(enabled)
        self.update()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QLabel#title {
                color: #ffffff;
                font-family: "Segoe UI";
                font-size: 15pt;
                font-weight: 600;
                letter-spacing: -0.2px;
            }
            QLabel#artist {
                color: rgba(255,255,255,0.72);
                font-family: "Segoe UI";
                font-size: 11pt;
            }
            QLabel#time {
                color: rgba(255,255,255,0.55);
                font-family: "Cascadia Mono", "Consolas", "Segoe UI";
                font-size: 9pt;
                font-weight: 500;
            }
            QLabel#time[paused="true"] {
                color: rgba(255,255,255,0.30);
            }
            """
        )

    def _build_animations(self) -> None:
        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(220)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.OutCubic)

        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(160)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.InCubic)
        self._fade_out.finished.connect(self._after_fade_out)

    def show_animated(self) -> None:
        # На случай повторного вызова во время скрытия — остановить out-анимацию.
        self._fade_out.stop()
        self._fade_in.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self._fade_in.start()

    def hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._fade_in.stop()
        self._fade_out.stop()
        # Стартуем с текущей непрозрачности (на случай прерывания fade-in).
        self._fade_out.setStartValue(self.windowOpacity())
        self._fade_out.start()

    def _after_fade_out(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)
        self.hidden_after_fade.emit()

    # --- painting (rounded acrylic with cover-backdrop) ------------------
    def paintEvent(self, _) -> None:
        if self._transparent_mode:
            # Полностью прозрачный режим: фон, подложку и тень не рисуем —
            # видны только обложка, текст и кнопки.
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        radius = 20.0
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)

        # Soft outer glow (drawn outside the clip).
        for i, alpha in enumerate((8, 14, 22, 32)):
            offs = 4 - i
            r = rect.adjusted(-offs, -offs, offs, offs)
            glow = QPainterPath()
            glow.addRoundedRect(QRectF(r), radius + offs, radius + offs)
            p.setPen(QPen(QColor(0, 0, 0, alpha), 1))
            p.drawPath(glow)

        p.save()
        p.setClipPath(path)

        # 1) Backdrop — blurred cover, pre-rasterized at rect.size() in
        # _rebuild_backdrop(). Drawn 1:1; rescale only if size briefly mismatches
        # (e.g. between resize event and rebuild).
        if self._backdrop is not None and not self._backdrop.isNull():
            if self._backdrop.size() == rect.size():
                p.drawPixmap(rect.topLeft(), self._backdrop)
            else:
                p.drawPixmap(rect, self._backdrop, QRectF(self._backdrop.rect()))
        else:
            # Subtle neutral fill leaning on accent for non-cover state.
            base = QColor(self._accent)
            base = base.darker(220)
            base.setAlpha(255)
            p.fillRect(rect, base if self._backdrop is None else QColor(20, 20, 25))

        # 2) Dark gradient overlay for text legibility (acrylic feel).
        grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        grad.setColorAt(0.0, QColor(8, 8, 14, 215))
        grad.setColorAt(1.0, QColor(8, 8, 14, 195))
        p.fillRect(rect, grad)

        p.restore()

    # --- dragging --------------------------------------------------------
    # Используем нативный startSystemMove() — окном двигает Windows, Qt не
    # перерисовывает кадры во время drag. Это убирает провисание frameless+
    # translucent окна при быстром перемещении. Позиция сохраняется в
    # debounce-таймере по moveEvent.
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            wh = self.windowHandle()
            if wh is not None and wh.startSystemMove():
                e.accept()
                return
            # Fallback (Qt < 5.15 или платформа без поддержки).
            self._dragging = True
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e) -> None:
        if self._dragging:
            self._dragging = False
            # Позицию из fallback-пути сохранит moveEvent → _save_pos_timer.

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        self._save_pos_timer.start()

    def _persist_position(self) -> None:
        self._settings["x"] = self.x()
        self._settings["y"] = self.y()
        save_settings(self._settings)

    def _set_paused_state(self, paused: bool) -> None:
        self.progress.set_paused(paused)
        for lbl in (self.time_cur, self.time_dur):
            lbl.setProperty("paused", "true" if paused else "false")
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

    def _apply_cover(self, thumb: bytes | None) -> None:
        """Recompute backdrop + accent + thumbnail. Called only when bytes change."""
        h = hash(thumb) if thumb else 0
        if h == self._last_thumb_hash:
            return
        self._last_thumb_hash = h
        self._cover_bytes = thumb
        self.cover.set_bytes(thumb)
        self._rebuild_backdrop()
        self._accent = extract_accent(thumb)
        self.progress.set_accent(self._accent)
        self.play_btn.set_accent(self._accent)
        self.update()

    def _rebuild_backdrop(self) -> None:
        """Render backdrop pixmap exactly at the painted rect size, so paintEvent
        can drawPixmap() with no per-frame SmoothTransformation rescale (this was
        the cause of the drag lag — every mouse move triggered a full smooth
        rescale of the blurred cover)."""
        if not self._cover_bytes:
            self._backdrop = None
            return
        w = max(self.width() - 4, 16)
        h = max(self.height() - 4, 16)
        self._backdrop = make_backdrop(self._cover_bytes, w, h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rebuild_backdrop()

    # --- info update -----------------------------------------------------
    def apply_info(self, info: dict) -> None:
        if not info.get("available"):
            self.title_lbl.setText("Музыка не играет")
            self.artist_lbl.setText(info.get("reason", "Откройте трек в плеере"))
            self._apply_cover(None)
            self.progress.set_progress(0, 1)
            self.time_cur.setText("0:00")
            self.time_dur.setText("0:00")
            self.play_btn.set_kind("play")
            self._set_paused_state(True)
            self.equalizer.hide()
            self.equalizer.set_playing(False)
            return

        title = info.get("title") or "—"
        artist = info.get("artist") or ""
        if len(title) > 42:
            title = title[:41] + "…"
        if len(artist) > 50:
            artist = artist[:49] + "…"
        self.title_lbl.setText(title)
        self.artist_lbl.setText(artist)
        self._apply_cover(info.get("thumbnail"))

        pos = float(info.get("position") or 0)
        dur = float(info.get("duration") or 0)
        reliable = bool(info.get("position_reliable", True))
        self.progress.set_progress(pos, dur if dur > 0 else 1)
        self.time_cur.setText(_fmt(pos))
        self.time_cur.setToolTip(
            "" if reliable else
            "Плеер не сообщает позицию — отсчёт ведётся локально от смены трека"
        )
        self.time_dur.setText(_fmt(dur))
        playing = bool(info.get("is_playing"))
        self.play_btn.set_kind("pause" if playing else "play")
        self._set_paused_state(not playing)
        self.equalizer.show()
        self.equalizer.set_playing(playing)
        self.equalizer.raise_()


def _fmt(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Tray icon
# --------------------------------------------------------------------------
def make_tray_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(ACCENT)
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QPen(QColor("white"), 4, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(14, 18, 36, 28, 0 * 16, 180 * 16)
    p.drawArc(20, 24, 24, 18, 0 * 16, 180 * 16)
    p.drawArc(26, 30, 12, 8, 0 * 16, 180 * 16)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------
# App orchestration
# --------------------------------------------------------------------------
class App(QObject):
    def __init__(self, qapp: QApplication) -> None:
        super().__init__()
        self.qapp = qapp
        self.settings = load_settings()
        self._prev_hwnd: int = 0

        self.window = OverlayWindow(self.settings)
        self.window.set_transparent_mode(self.settings.get("transparent_mode", False))
        self.window.prev_btn.clicked.connect(lambda: self.media.prev_track())
        self.window.play_btn.clicked.connect(lambda: self.media.toggle_play())
        self.window.next_btn.clicked.connect(lambda: self.media.next_track())
        self.window.request_settings.connect(self.open_settings)
        self.window.hide_requested.connect(self._hide_window)
        self.window.sc_prev.activated.connect(lambda: self.media.prev_track())
        self.window.sc_next.activated.connect(lambda: self.media.next_track())
        self.window.sc_play.activated.connect(lambda: self.media.toggle_play())
        self._apply_local_hotkeys()

        self.media = MediaController()
        self.media.info_updated.connect(self.window.apply_info)
        self.media.info_updated.connect(self._on_media_info)

        self.discord = DiscordPresence()
        self.discord.configure(
            self.settings.get("discord_enabled", False),
            self.settings.get("discord_client_id", ""),
        )

        self.hotkey = HotkeyManager()
        self.hotkey.triggered.connect(self.toggle_window)
        if sys.platform == "darwin":
            try:
                from _platform_mac import _dlog as _mac_dlog  # type: ignore
                _mac_dlog(f"app startup: hotkey_setting={self.settings['hotkey_toggle']!r}")
            except Exception:
                pass
        # Carbon RegisterEventHotKey doesn't need any permission — register directly.
        ok = self.hotkey.start(self.settings["hotkey_toggle"])

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(f"OverlayMusic ({self.settings['hotkey_toggle']})")
        menu = QMenu()
        menu.addAction("Показать / скрыть").triggered.connect(self.toggle_window)
        menu.addAction("Настройки…").triggered.connect(self.open_settings)
        menu.addSeparator()
        menu.addAction("Выход").triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        if _BACKEND_ERR:
            self.tray.showMessage(
                "OverlayMusic",
                "Backend SMTC не загружен. Управление недоступно.",
                QSystemTrayIcon.Warning,
                5000,
            )
        elif not ok:
            self.tray.showMessage(
                "OverlayMusic",
                f"Не удалось зарегистрировать хоткей «{self.settings['hotkey_toggle']}».\n"
                "Откройте настройки через значок в трее.",
                QSystemTrayIcon.Warning,
                5000,
            )
        else:
            self.tray.showMessage(
                "OverlayMusic",
                f"Запущен. Нажмите {self.settings['hotkey_toggle']} для показа.",
                QSystemTrayIcon.Information,
                3000,
            )

    def _apply_local_hotkeys(self) -> None:
        self.window.set_local_hotkeys(
            self.settings["hotkey_prev"],
            self.settings["hotkey_next"],
            self.settings["hotkey_playpause"],
        )

    def _on_media_info(self, info: dict) -> None:
        self.discord.push(info, self.settings.get("browser_provider", "yandex"))

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_window()

    def toggle_window(self) -> None:
        if self.window.isVisible():
            self._hide_window()
        else:
            self._show_window()

    def _show_window(self) -> None:
        # На Win запоминаем прошлый foreground, на Mac — backend сам активирует app.
        if sys.platform != "darwin":
            try:
                import ctypes
                prev = ctypes.windll.user32.GetForegroundWindow()
                if prev and prev != int(self.window.winId()):
                    self._prev_hwnd = prev
            except Exception:
                self._prev_hwnd = 0
        self.window.show_animated()
        self.window.raise_()
        self.window.activateWindow()
        try:
            hwnd = int(self.window.winId())
            force_foreground(hwnd)
        except Exception:
            pass
        self.window.setFocus(Qt.OtherFocusReason)

    def _hide_window(self) -> None:
        self.window.hide_animated()
        if sys.platform != "darwin" and self._prev_hwnd:
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(self._prev_hwnd)
            except Exception:
                pass
            self._prev_hwnd = 0

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.values()

        if not (new.get("hotkey_toggle") or "").strip():
            self.tray.showMessage(
                "OverlayMusic",
                "Глобальный хоткей не задан.",
                QSystemTrayIcon.Warning, 3000)
            return

        self.settings.update(new)
        save_settings(self.settings)
        self._apply_local_hotkeys()
        self.window.set_transparent_mode(self.settings.get("transparent_mode", False))
        self.discord.configure(
            self.settings.get("discord_enabled", False),
            self.settings.get("discord_client_id", ""),
        )

        ok = self.hotkey.start(new["hotkey_toggle"])
        self.tray.setToolTip(f"OverlayMusic ({new['hotkey_toggle']})")
        if not ok:
            self.tray.showMessage(
                "OverlayMusic",
                f"Хоткей «{new['hotkey_toggle']}» уже занят системой или другим приложением.",
                QSystemTrayIcon.Warning, 4000)
        else:
            self.tray.showMessage(
                "OverlayMusic", "Настройки сохранены.",
                QSystemTrayIcon.Information, 2000)

    def quit(self) -> None:
        try:
            self.hotkey.stop()
            self.media.shutdown()
            self.discord.shutdown()
        finally:
            self.qapp.quit()


def main() -> int:
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setApplicationName(APP_NAME)
    qapp.setOrganizationName(APP_NAME)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # Without tray we'd be unkillable. Show overlay anyway.
        pass

    app = App(qapp)
    # Show overlay briefly so user sees it works on first launch.
    app.window.show_animated()
    QTimer.singleShot(2500, app.window.hide_animated)

    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
