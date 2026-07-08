from __future__ import annotations

import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import imageio_ffmpeg
    import requests
    from PIL import Image, ImageOps
    from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import extract_cookies_from_browser
    from yt_dlp.utils import DownloadCancelled
except ImportError as exc:  # pragma: no cover - shown only before dependencies are installed
    app = QApplication(sys.argv)
    QMessageBox.critical(
        None,
        "Не хватает зависимостей",
        "Установите зависимости командой:\n\n"
        "python -m pip install -r requirements.txt\n\n"
        f"Подробности: {exc}",
    )
    raise SystemExit(1)


APP_TITLE = "TubeDrop"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def app_state_dir() -> Path:
    if getattr(sys, "frozen", False) and os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_TITLE
    return APP_DIR


STATE_DIR = app_state_dir()
SESSION_DIR = STATE_DIR / ".tubedrop_profile"
COOKIE_FILE = SESSION_DIR / "youtube_cookies.txt"
SECURE_BROWSER_ROOT = STATE_DIR / ".tubedrop_secure_browser"
SECURE_BROWSER_CHOICE = SESSION_DIR / "secure_browser.txt"
YOUTUBE_HOME = "https://www.youtube.com/"
TIKTOK_HOME = "https://www.tiktok.com/"
INSTAGRAM_HOME = "https://www.instagram.com/"
PINTEREST_HOME = "https://www.pinterest.com/"
SUPPORTED_SITES_TEXT = "YouTube, TikTok, Instagram или Pinterest"

BG = "#111111"
PANEL = "#191919"
PANEL_2 = "#242424"
PANEL_3 = "#303030"
TEXT = "#f5f5f5"
MUTED = "#a8a8a8"
SUBTLE = "#707070"
BORDER = "#303030"
ACCENT = "#10a37f"
ACCENT_HOVER = "#16b894"
BLUE = "#d4d4d4"
DANGER = "#fb7185"
WARN = "#f59e0b"
LOG_BG = "#0d0d0d"

FONT_STACK = "Segoe UI"
DEFAULT_OUTPUT_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"
WINDOW_RADIUS = 20
MEDIA_EXTENSIONS = {
    ".mp4",
    ".m4a",
    ".mp3",
    ".wav",
    ".webm",
    ".mkv",
    ".mov",
    ".flac",
    ".opus",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MERGED_MP4_VIDEO_AUDIO_FORMAT = (
    "bv[vcodec^=avc1]+ba[acodec^=mp4a]/"
    "bv[ext=mp4]+ba[ext=m4a]/"
    "bv+ba"
)
PROGRESSIVE_VIDEO_AUDIO_FORMAT = (
    "b[ext=mp4][vcodec!=none][acodec!=none]/"
    "best[ext=mp4][vcodec!=none][acodec!=none]/"
    "b[vcodec!=none][acodec!=none]/"
    "best[vcodec!=none][acodec!=none]"
)
UNIVERSAL_VIDEO_AUDIO_FORMAT = f"{MERGED_MP4_VIDEO_AUDIO_FORMAT}/{PROGRESSIVE_VIDEO_AUDIO_FORMAT}"
PROGRESSIVE_FIRST_VIDEO_AUDIO_FORMAT = f"{PROGRESSIVE_VIDEO_AUDIO_FORMAT}/{MERGED_MP4_VIDEO_AUDIO_FORMAT}"


def clean_yt_text(value: Any) -> str:
    text = ANSI_RE.sub("", str(value))
    return " ".join(text.replace("\r", " ").split())


def host_from_url(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    raw = url.strip()
    if raw and "://" not in raw and "." in raw.split("/", 1)[0]:
        return f"https://{raw}"
    return raw


def platform_label_for_url(url: str) -> str:
    host = host_from_url(url)
    if "tiktok.com" in host:
        return "TikTok"
    if "instagram.com" in host:
        return "Instagram"
    if host == "pin.it" or host.endswith(".pin.it") or host.startswith("pinterest.") or ".pinterest." in host:
        return "Pinterest"
    if "youtu.be" in host or "youtube.com" in host:
        return "YouTube"
    return "сайт"


def platform_home_for_url(url: str) -> str:
    host = host_from_url(url)
    if "tiktok.com" in host:
        return TIKTOK_HOME
    if "instagram.com" in host:
        return INSTAGRAM_HOME
    if host == "pin.it" or host.endswith(".pin.it") or host.startswith("pinterest.") or ".pinterest." in host:
        return PINTEREST_HOME
    return YOUTUBE_HOME


def is_tiktok_info(info: dict[str, Any]) -> bool:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if "tiktok" in extractor:
        return True
    source_url = str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "")
    return platform_label_for_url(source_url) == "TikTok"


def is_instagram_url(url: str) -> bool:
    return platform_label_for_url(url) == "Instagram"


def needs_vegas_compatible_mp4(choice: "DownloadChoice", url: str, recode: bool) -> bool:
    return choice.kind == "video" and (recode or is_instagram_url(url))


def make_vegas_compatible_mp4(source: Path, ffmpeg: str) -> Path:
    source = source.resolve()
    target = source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")
    temp = target.with_name(f"{target.stem}.vegas-tmp{target.suffix}")
    temp.unlink(missing_ok=True)

    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-fflags",
        "+genpts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-dn",
        "-vf",
        "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p,setsar=1",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-bf",
        "0",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-r",
        "30",
        "-fps_mode",
        "cfr",
        "-video_track_timescale",
        "30000",
        "-tag:v",
        "avc1",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "-brand",
        "mp42",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "mp4",
        str(temp),
    ]

    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        temp.unlink(missing_ok=True)
        details = clean_yt_text(exc.stderr or exc)
        raise RuntimeError(f"не удалось подготовить MP4 для Vegas: {details}") from exc

    os.replace(temp, target)
    if target != source:
        source.unlink(missing_ok=True)
    return target


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.home()


def ensure_session_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SECURE_BROWSER_ROOT.mkdir(parents=True, exist_ok=True)


def installed_chromium_browsers() -> list[tuple[str, str, Path]]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    candidates = [
        (
            "edge",
            "Microsoft Edge",
            [
                program_files / "Microsoft/Edge/Application/msedge.exe",
                program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
                local / "Microsoft/Edge/Application/msedge.exe",
            ],
        ),
        (
            "chrome",
            "Google Chrome",
            [
                program_files / "Google/Chrome/Application/chrome.exe",
                program_files_x86 / "Google/Chrome/Application/chrome.exe",
                local / "Google/Chrome/Application/chrome.exe",
            ],
        ),
        (
            "brave",
            "Brave",
            [
                program_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
                program_files_x86 / "BraveSoftware/Brave-Browser/Application/brave.exe",
                local / "BraveSoftware/Brave-Browser/Application/brave.exe",
            ],
        ),
    ]
    found = []
    for browser_id, label, paths in candidates:
        for path in paths:
            if path.exists():
                found.append((browser_id, label, path))
                break
    return found


def secure_browser_choice() -> tuple[str, str, Path] | None:
    browsers = installed_chromium_browsers()
    if not browsers:
        return None
    if SECURE_BROWSER_CHOICE.exists():
        saved = SECURE_BROWSER_CHOICE.read_text(encoding="utf-8").strip()
        for browser in browsers:
            if browser[0] == saved:
                return browser
    return browsers[0]


def secure_browser_user_data_dir(browser_id: str) -> Path:
    return SECURE_BROWSER_ROOT / browser_id


def secure_browser_profile_dir(browser_id: str) -> Path:
    return secure_browser_user_data_dir(browser_id) / "Default"


def secure_session_available() -> bool:
    browser = secure_browser_choice()
    if not browser:
        return False
    browser_id, _label, _path = browser
    profile = secure_browser_profile_dir(browser_id)
    cookie_locations = [
        profile / "Cookies",
        profile / "Network" / "Cookies",
        profile / "Default" / "Network" / "Cookies",
    ]
    return profile.exists() and any(path.exists() for path in cookie_locations)


def js_runtime_options() -> dict[str, dict[str, str]]:
    runtimes: dict[str, dict[str, str]] = {}
    node_path = shutil.which("node")
    if not node_path:
        try:
            import nodejs_wheel.executable as nodejs_executable

            candidate = Path(nodejs_executable.ROOT_DIR) / ("node.exe" if os.name == "nt" else "bin/node")
            if candidate.exists():
                node_path = str(candidate)
        except ImportError:
            node_path = None
    if node_path:
        runtimes["node"] = {"path": node_path}
    runtimes["deno"] = {}
    return runtimes


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "длительность неизвестна"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def compact_number(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн"
    if value >= 1_000:
        return f"{value / 1_000:.1f} тыс"
    return str(value)


def format_size(num: float | None) -> str:
    if not num:
        return ""
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return f"{format_size(bytes_per_second)}/с"


def format_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}ч {minutes:02d}м"
    if minutes:
        return f"{minutes}м {secs:02d}с"
    return f"{secs}с"


def sanitize_filename_base(name: str) -> str:
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name)
    base = " ".join(base.split()).strip(" .")
    return base or "video"


def find_new_media_file(folder: Path, started_at: float) -> Path | None:
    candidates: list[Path] = []
    try:
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            try:
                if path.stat().st_mtime >= started_at - 5:
                    candidates.append(path)
            except OSError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


def make_preview(image: Image.Image, width: int = 320, height: int = 180) -> QPixmap:
    source = image.convert("RGB")
    canvas = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    return pil_to_pixmap(canvas)


def is_requested_format_unavailable(exc: Exception) -> bool:
    raw = str(exc).lower()
    return "requested format is not available" in raw or "use --list-formats" in raw


def has_downloadable_av_formats(info: dict[str, Any] | None) -> bool:
    if not info:
        return False
    if info.get("url") and (info.get("vcodec") != "none" or info.get("acodec") != "none"):
        return True
    for fmt in info.get("formats") or []:
        has_video = fmt.get("vcodec") and fmt.get("vcodec") != "none"
        has_audio = fmt.get("acodec") and fmt.get("acodec") != "none"
        if (has_video or has_audio) and fmt.get("url"):
            return True
    return False


class YtdlpLogger:
    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events

    def info(self, msg: str) -> None:
        if "Extracted" in msg and "cookies" in msg:
            self.events.put(("log", clean_yt_text(msg)))

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            return
        if "Merging formats" in msg:
            self.events.put(("status", "Объединяю видео и аудио..."))

    def warning(self, msg: str) -> None:
        self.events.put(("log", f"Предупреждение: {clean_yt_text(msg)}"))

    def error(self, msg: str) -> None:
        self.events.put(("log", f"Ошибка: {clean_yt_text(msg)}"))


def hide_native_border_for(widget: QWidget) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        dwmapi = ctypes.windll.dwmapi
        color_none = ctypes.c_uint(0xFFFFFFFE)
        corner_preference = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color_none), ctypes.sizeof(color_none))
        dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner_preference), ctypes.sizeof(corner_preference))
    except Exception:
        pass


class BorderlessDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, radius: int = WINDOW_RADIUS) -> None:
        super().__init__(parent)
        self.window_radius = radius
        self.drag_offset = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().showEvent(event)
        hide_native_border_for(self)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if self.drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.drag_offset = None
        event.accept()


class PreviewBox(QFrame):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._text = text
        self.setObjectName("PreviewFrame")
        self.setFixedSize(256, 144)

    def setPixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 - Qt-compatible method name
        self._pixmap = pixmap
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt-compatible method name
        self._text = text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom clipped preview painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        path = QPainterPath()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rect, 16, 16)
        painter.fillPath(path, QColor("#0b0b0b"))
        painter.setClipPath(path)

        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            left = (self.width() - scaled.width()) // 2
            top = (self.height() - scaled.height()) // 2
            painter.drawPixmap(left, top, scaled)
            return

        if self._text:
            painter.setPen(QColor(SUBTLE))
            font = QFont(FONT_STACK, 11)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self._text)


class StatusDot(QWidget):
    COLORS = {
        "ok": ACCENT,
        "busy": WARN,
        "warn": WARN,
        "idle": SUBTLE,
        "error": DANGER,
    }

    def __init__(self, state: str = "ok", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setFixedSize(10, 10)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom circular status painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.COLORS.get(self.state, SUBTLE)))
        painter.drawEllipse(QRectF(1, 1, self.width() - 2, self.height() - 2))


class ModernComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Input")
        self.setMinimumHeight(38)
        self.setMaxVisibleItems(8)
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view().setStyleSheet(
            f"""
            QListView {{
                background: {PANEL_2};
                color: {TEXT};
                padding: 6px;
                outline: 0;
                border: none;
                border-radius: 12px;
                selection-background-color: {PANEL_3};
            }}
            QListView::item {{
                min-height: 30px;
                padding: 6px 10px;
                border-radius: 8px;
            }}
            QListView::item:selected {{
                background: {PANEL_3};
                color: {TEXT};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 6px 2px 6px 0;
            }}
            QScrollBar::handle:vertical {{
                background: #505050;
                min-height: 28px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #626262;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
                height: 0;
            }}
            """
        )

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom combo painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        bg = QColor(PANEL_2)
        if self.underMouse():
            bg = QColor(PANEL_3)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)

        text_rect = self.rect().adjusted(12, 0, -36, 0)
        painter.setPen(QColor(TEXT if self.isEnabled() else SUBTLE))
        painter.setFont(self.font())
        text = painter.fontMetrics().elidedText(self.currentText(), Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)

        cx = self.width() - 19
        cy = self.height() / 2 + 1
        pen = QPen(QColor(MUTED if self.isEnabled() else SUBTLE), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2))
        painter.drawLine(QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2))


class WindowControlButton(QPushButton):
    def __init__(self, symbol: str, close_button: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.symbol = symbol
        self.close_button = close_button
        self.hovered = False
        self.setObjectName("CloseButton" if close_button else "WindowButton")
        self.setFixedSize(34, 30)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom centered text painter
        from PySide6.QtGui import QColor, QPainter

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self.hovered:
            painter.setBrush(QColor("#7f1d1d" if self.close_button else PANEL_3))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(self.rect(), 10, 10)
        painter.setPen(QColor("#ffffff" if self.hovered and self.close_button else MUTED))
        font = QFont(FONT_STACK, 14)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, self.symbol)


class TitleBar(QFrame):
    def __init__(
        self,
        host_window: QMainWindow,
        auth_dot: QLabel,
        auth_label: QLabel,
        status_dot: QLabel,
        status_label: QLabel,
    ) -> None:
        super().__init__(host_window)
        self.host_window = host_window
        self.drag_offset = None
        self.setObjectName("TitleBar")
        self.setFixedHeight(48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)

        mark = QLabel("▶")
        mark.setObjectName("LogoMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(32, 32)
        layout.addWidget(mark)

        brand = QHBoxLayout()
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(0)
        title = QLabel(APP_TITLE)
        title.setObjectName("WindowTitle")
        brand.addWidget(title)
        layout.addLayout(brand, 1)

        for dot, label in ((auth_dot, auth_label), (status_dot, status_label)):
            chip = QFrame()
            chip.setObjectName("StatusPill")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(10, 0, 10, 0)
            chip_layout.setSpacing(7)
            label.setObjectName("StatusText")
            chip_layout.addWidget(dot)
            chip_layout.addWidget(label)
            layout.addWidget(chip)

        minimize = WindowControlButton("−")
        minimize.clicked.connect(host_window.showMinimized)
        layout.addWidget(minimize)

        close = WindowControlButton("×", close_button=True)
        close.clicked.connect(host_window.close)
        layout.addWidget(close)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.host_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if self.drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.host_window.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.drag_offset = None
        event.accept()


class AuthDialog(BorderlessDialog):
    session_saved = Signal(int)

    def __init__(self, parent: QWidget | None = None, start_url: str = YOUTUBE_HOME) -> None:
        super().__init__(parent)
        ensure_session_dir()
        self.platform_label = platform_label_for_url(start_url)
        self.platform_home = platform_home_for_url(start_url)
        self.setWindowTitle(f"Безопасный вход {self.platform_label}")
        self.setModal(False)
        self.resize(520, 310)
        self.setMinimumSize(500, 300)
        self.setStyleSheet(APP_STYLE)
        self.start_url = start_url or self.platform_home
        self.browser = secure_browser_choice()
        self.process: subprocess.Popen | None = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("DialogPanel")
        outer_layout.addWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(10)

        title = QLabel(f"Вход {self.platform_label}")
        title.setObjectName("DialogTitle")
        subtitle = QLabel("Войдите в выбранном браузере, затем вернитесь в TubeDrop.")
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.browser_combo = ModernComboBox()
        self.browser_items = installed_chromium_browsers()
        for browser_id, label, _path in self.browser_items:
            self.browser_combo.addItem(label, browser_id)
        if self.browser:
            for index, (browser_id, _label, _path) in enumerate(self.browser_items):
                if browser_id == self.browser[0]:
                    self.browser_combo.setCurrentIndex(index)
                    break
        layout.addWidget(QLabel("Браузер TubeDrop"))
        layout.addWidget(self.browser_combo)

        self.state_label = QLabel("")
        self.state_label.setObjectName("Muted")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        steps = QLabel(
            f"1. Открыть вход\n"
            f"2. Войти в {self.platform_label}\n"
            "3. Нажать «Готово», не закрывая браузер"
        )
        steps.setObjectName("Muted")
        steps.setWordWrap(True)
        layout.addWidget(steps)
        layout.addStretch(1)

        actions = QHBoxLayout()
        open_btn = QPushButton("Открыть вход")
        open_btn.setObjectName("SecondaryButton")
        open_btn.clicked.connect(self.open_secure_browser)
        actions.addWidget(open_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("FlatButton")
        close_btn.clicked.connect(self.reject)
        actions.addWidget(close_btn)

        ready_btn = QPushButton("Готово")
        ready_btn.setObjectName("PrimaryButton")
        ready_btn.clicked.connect(self.finish_secure_login)
        actions.addWidget(ready_btn)
        layout.addLayout(actions)
        self.update_state()

    def selected_browser(self) -> tuple[str, str, Path] | None:
        index = self.browser_combo.currentIndex()
        if 0 <= index < len(self.browser_items):
            return self.browser_items[index]
        return None

    def update_state(self, text: str | None = None) -> None:
        browser = self.selected_browser()
        if not browser:
            self.state_label.setText("Не найден Edge/Chrome/Brave. Установите один из них и перезапустите TubeDrop.")
            return
        _browser_id, label, _path = browser
        self.state_label.setText(text or f"Выбран браузер: {label}")

    def open_secure_browser(self) -> None:
        browser = self.selected_browser()
        if not browser:
            show_app_message(self, "Браузер не найден", "Не найден Edge, Chrome или Brave.", "error")
            return
        browser_id, label, executable = browser
        SECURE_BROWSER_CHOICE.write_text(browser_id, encoding="utf-8")
        user_data = secure_browser_user_data_dir(browser_id)
        user_data.mkdir(parents=True, exist_ok=True)
        url = self.start_url if self.start_url.startswith(("http://", "https://")) else self.platform_home
        args = [
            str(executable),
            f"--user-data-dir={user_data}",
            "--profile-directory=Default",
            "--no-first-run",
            "--new-window",
            url,
        ]
        try:
            self.process = subprocess.Popen(args)
        except OSError as exc:
            show_app_message(self, "Не удалось открыть браузер", str(exc), "error")
            return
        self.update_state(f"Открыт {label}. Войдите в {self.platform_label}, затем нажмите «Готово».")

    def finish_secure_login(self) -> None:
        browser = self.selected_browser()
        if not browser:
            return
        browser_id, label, _path = browser
        SECURE_BROWSER_CHOICE.write_text(browser_id, encoding="utf-8")
        self.update_state(f"Сессия {label} отмечена. TubeDrop сохранит cookie и повторит запрос.")
        QTimer.singleShot(500, self.complete_secure_login)

    def complete_secure_login(self) -> None:
        self.session_saved.emit(1)
        self.accept()

    def close_browser_process(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
            else:
                self.process.terminate()
        except OSError:
            pass


class SuccessDialog(BorderlessDialog):
    def __init__(self, parent: QWidget | None, folder: Path, file_path: Path | None) -> None:
        super().__init__(parent)
        self.folder = folder
        self.file_path = file_path if file_path and file_path.exists() else None
        self.setModal(True)
        self.setStyleSheet(APP_STYLE)
        self.setFixedSize(470, 250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("SuccessPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 22, 22, 18)
        panel_layout.setSpacing(12)
        layout.addWidget(panel)

        top = QHBoxLayout()
        top.setSpacing(14)
        check = QLabel("✓")
        check.setObjectName("SuccessCheck")
        check.setAlignment(Qt.AlignCenter)
        check.setFixedSize(54, 54)
        top.addWidget(check)

        text_block = QVBoxLayout()
        text_block.setSpacing(4)
        title = QLabel("Скачивание завершено")
        title.setObjectName("SuccessTitle")
        subtitle = QLabel(
            "Файл и превью сохранены в выбранную папку."
            if self.file_path
            else "Файлы сохранены в выбранную папку."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        text_block.addWidget(title)
        text_block.addWidget(subtitle)
        top.addLayout(text_block, 1)
        panel_layout.addLayout(top)

        if self.file_path:
            path_label = QLabel(self.file_path.name)
            path_label.setObjectName("DoneFileName")
            path_label.setWordWrap(True)
            panel_layout.addWidget(path_label)

        panel_layout.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("FlatButton")
        ok_btn.clicked.connect(self.accept)
        actions.addWidget(ok_btn)

        file_btn = QPushButton("Открыть файл")
        file_btn.setObjectName("SecondaryButton")
        file_btn.setEnabled(bool(self.file_path))
        file_btn.clicked.connect(self.open_file)
        actions.addWidget(file_btn)

        folder_btn = QPushButton("Открыть папку")
        folder_btn.setObjectName("PrimaryButton")
        folder_btn.clicked.connect(self.open_folder)
        actions.addWidget(folder_btn)
        panel_layout.addLayout(actions)

    def open_file(self) -> None:
        if self.file_path:
            os.startfile(str(self.file_path))  # type: ignore[attr-defined]
        self.accept()

    def open_folder(self) -> None:
        os.startfile(str(self.folder))  # type: ignore[attr-defined]
        self.accept()


class AppMessageDialog(BorderlessDialog):
    def __init__(self, parent: QWidget | None, title: str, message: str, kind: str = "info") -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setStyleSheet(APP_STYLE)
        self.setFixedSize(500, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("MessagePanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 22, 22, 18)
        panel_layout.setSpacing(12)
        layout.addWidget(panel)

        top = QHBoxLayout()
        top.setSpacing(14)
        icon = QLabel("!" if kind == "error" else "i")
        icon.setObjectName("ErrorIcon" if kind == "error" else "InfoIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(48, 48)
        top.addWidget(icon)

        text_block = QVBoxLayout()
        text_block.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("DialogTitle")
        message_label = QLabel(message)
        message_label.setObjectName("Muted")
        message_label.setWordWrap(True)
        text_block.addWidget(title_label)
        text_block.addWidget(message_label)
        top.addLayout(text_block, 1)
        panel_layout.addLayout(top)
        panel_layout.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.clicked.connect(self.accept)
        actions.addWidget(ok_btn)
        panel_layout.addLayout(actions)


def show_app_message(parent: QWidget | None, title: str, message: str, kind: str = "info") -> None:
    AppMessageDialog(parent, title, message, kind).exec()


@dataclass
class DownloadChoice:
    label: str
    ytdlp_format: str
    kind: str
    audio_codec: str | None = None
    fallback_format: str | None = None


class TubeDropApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_session_dir()
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(980, 680)
        self.setStyleSheet(APP_STYLE)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.info: dict[str, Any] | None = None
        self.choices: list[DownloadChoice] = []
        self.worker: threading.Thread | None = None
        self.auth_dialog: AuthDialog | None = None
        self.cancel_requested = threading.Event()
        self.pending_retry = "fetch"
        self.pending_download_choice: DownloadChoice | None = None

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Ссылка на YouTube, TikTok, Instagram или Pinterest")
        self.url_input.returnPressed.connect(self.fetch_info)

        self.folder_input = QLineEdit(str(default_download_dir()))
        self.filename_input = QLineEdit()
        self.filename_input.setPlaceholderText("Введите название")
        self.quality_combo = ModernComboBox()
        self.recode_check = QCheckBox("MP4 для Vegas")
        self.playlist_check = QCheckBox("Плейлист целиком")

        self.auth_status_dot = StatusDot("idle")
        self.auth_status_label = QLabel("Вход: не проверен")
        self.status_dot = StatusDot("ok")
        self.status_label = QLabel("Готово")
        self.preview_label = PreviewBox("Превью")
        self.title_label = QLabel("Ссылка не выбрана")
        self.meta_label = QLabel("Ожидание данных")
        self.log = QPlainTextEdit()
        self.progress = QProgressBar()
        self.progress_label = QLabel("Ожидание")
        self.prepare_progress = QProgressBar()
        self.prepare_label = QLabel("Подготовка файла")
        self.download_btn = QPushButton("Скачать")
        self.cancel_btn = QPushButton("Отмена")

        self.build_ui()
        self.update_auth_status("Вход: сохранён" if secure_session_available() else "Вход: не проверен", "ok" if secure_session_available() else "idle")
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_events)
        self.poll_timer.start(120)

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("RootWindow")
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        main.addWidget(
            TitleBar(
                self,
                self.auth_status_dot,
                self.auth_status_label,
                self.status_dot,
                self.status_label,
            )
        )

        url_card = QFrame()
        url_card.setObjectName("Card")
        url_layout = QHBoxLayout(url_card)
        url_layout.setContentsMargins(8, 8, 8, 8)
        url_layout.setSpacing(8)
        self.url_input.setObjectName("UrlInput")
        url_layout.addWidget(self.url_input, 1)

        paste_btn = QPushButton("Вставить")
        paste_btn.setObjectName("SecondaryButton")
        paste_btn.clicked.connect(self.paste_url)
        url_layout.addWidget(paste_btn)

        check_btn = QPushButton("Проверка")
        check_btn.setObjectName("SecondaryButton")
        check_btn.clicked.connect(lambda: self.open_auth_dialog("fetch"))
        url_layout.addWidget(check_btn)

        fetch_btn = QPushButton("Получить")
        fetch_btn.setObjectName("PrimaryButton")
        fetch_btn.clicked.connect(self.fetch_info)
        self.fetch_btn = fetch_btn
        url_layout.addWidget(fetch_btn)
        main.addWidget(url_card)

        body = QHBoxLayout()
        body.setSpacing(10)
        main.addLayout(body, 1)

        content_col = QVBoxLayout()
        content_col.setSpacing(10)
        body.addLayout(content_col, 1)

        media_panel = QFrame()
        media_panel.setObjectName("Card")
        media_panel.setFixedHeight(168)
        media_layout = QHBoxLayout(media_panel)
        media_layout.setContentsMargins(12, 12, 12, 12)
        media_layout.setSpacing(14)
        media_layout.addWidget(self.preview_label)

        info_col = QVBoxLayout()
        info_col.setSpacing(8)
        self.title_label.setObjectName("VideoTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setMaximumHeight(86)
        info_col.addWidget(self.title_label)

        self.meta_label.setObjectName("Muted")
        self.meta_label.setWordWrap(True)
        self.meta_label.setMaximumHeight(64)
        info_col.addWidget(self.meta_label)
        info_col.addStretch(1)
        media_layout.addLayout(info_col, 1)
        content_col.addWidget(media_panel)

        log_panel = QFrame()
        log_panel.setObjectName("Card")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 10, 12, 12)
        log_layout.setSpacing(8)

        log_header = QHBoxLayout()
        log_title = QLabel("Журнал")
        log_title.setObjectName("SectionTitle")
        log_header.addWidget(log_title)
        clear_btn = QPushButton("Очистить")
        clear_btn.setObjectName("FlatButton")
        clear_btn.clicked.connect(self.log.clear)
        log_header.addWidget(clear_btn)
        log_layout.addLayout(log_header)

        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        log_layout.addWidget(self.log, 1)
        content_col.addWidget(log_panel, 1)

        right = QFrame()
        right.setObjectName("Card")
        right.setFixedWidth(320)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(5)
        body.addWidget(right)

        settings_title = QLabel("Настройки")
        settings_title.setObjectName("PanelTitle")
        right_layout.addWidget(settings_title)

        self.add_labeled(right_layout, "Качество", self.quality_combo)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder_input.setObjectName("Input")
        folder_row.addWidget(self.folder_input, 1)
        folder_btn = QPushButton("...")
        folder_btn.setObjectName("IconButton")
        folder_btn.clicked.connect(self.choose_folder)
        folder_row.addWidget(folder_btn)
        self.add_labeled(right_layout, "Папка", folder_row)

        self.filename_input.setObjectName("Input")
        self.add_labeled(right_layout, "Имя файла", self.filename_input)

        right_layout.addSpacing(2)
        right_layout.addWidget(self.recode_check)
        right_layout.addWidget(self.playlist_check)

        session_actions = QHBoxLayout()
        session_actions.setSpacing(8)
        session_label = QLabel("Сессия")
        session_label.setObjectName("FieldLabel")
        session_actions.addWidget(session_label, 1)
        auth_btn = QPushButton("Вход")
        auth_btn.setObjectName("SecondaryButton")
        auth_btn.clicked.connect(lambda: self.open_auth_dialog("fetch"))
        reset_btn = QPushButton("Сброс")
        reset_btn.setObjectName("FlatButton")
        reset_btn.clicked.connect(self.reset_session)
        session_actions.addWidget(auth_btn)
        session_actions.addWidget(reset_btn)
        right_layout.addLayout(session_actions)

        right_layout.addStretch(1)
        download_stage = QLabel("Скачивание")
        download_stage.setObjectName("FieldLabel")
        right_layout.addWidget(download_stage)
        self.progress.setObjectName("DownloadProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        right_layout.addWidget(self.progress)

        self.progress_label.setObjectName("Muted")
        self.progress_label.setWordWrap(True)
        right_layout.addWidget(self.progress_label)

        self.prepare_label.setObjectName("FieldLabel")
        right_layout.addWidget(self.prepare_label)
        self.prepare_progress.setObjectName("DownloadProgress")
        self.prepare_progress.setRange(0, 100)
        self.prepare_progress.setValue(0)
        right_layout.addWidget(self.prepare_progress)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.cancel_btn.setObjectName("DangerButton")
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.cancel_btn.setEnabled(False)
        actions.addWidget(self.cancel_btn)

        self.download_btn.setObjectName("PrimaryButton")
        self.download_btn.clicked.connect(self.download)
        self.download_btn.setEnabled(False)
        actions.addWidget(self.download_btn)
        right_layout.addLayout(actions)

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().showEvent(event)
        hide_native_border_for(self)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().resizeEvent(event)

    def add_labeled(self, layout: QVBoxLayout, label: str, widget_or_layout: QWidget | QHBoxLayout) -> None:
        text = QLabel(label)
        text.setObjectName("FieldLabel")
        layout.addWidget(text)
        if isinstance(widget_or_layout, QHBoxLayout):
            layout.addLayout(widget_or_layout)
        else:
            layout.addWidget(widget_or_layout)

    def paste_url(self) -> None:
        text = QApplication.clipboard().text().strip()
        if text:
            self.url_input.setText(text)
            self.url_input.setFocus()
            self.url_input.setCursorPosition(len(text))

    def choose_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Выберите папку", self.folder_input.text())
        if directory:
            self.folder_input.setText(directory)

    def reset_session(self) -> None:
        if COOKIE_FILE.exists():
            COOKIE_FILE.unlink()
        SECURE_BROWSER_CHOICE.unlink(missing_ok=True)
        if SECURE_BROWSER_ROOT.exists():
            try:
                shutil.rmtree(SECURE_BROWSER_ROOT)
                SECURE_BROWSER_ROOT.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.log_message(f"Не удалось полностью удалить служебный профиль: {exc}", error=True)
        self.log_message("Сессия TubeDrop сброшена. При следующей проверке будет создан чистый профиль.")
        self.progress_label.setText("Сессия сброшена.")
        self.update_auth_status("Вход: не проверен", "idle")

    def open_auth_dialog(self, retry: str = "fetch") -> None:
        self.pending_retry = retry
        if self.auth_dialog and self.auth_dialog.isVisible():
            self.auth_dialog.raise_()
            self.auth_dialog.activateWindow()
            return
        start_url = normalize_url(self.url_input.text()) or YOUTUBE_HOME
        self.auth_dialog = AuthDialog(self, start_url=start_url)
        self.auth_dialog.session_saved.connect(self.on_session_saved)
        self.auth_dialog.show()

    def on_session_saved(self, count: int) -> None:
        platform_label = platform_label_for_url(self.url_input.text().strip())
        self.log_message(f"Безопасная сессия {platform_label} сохранена.")
        if self.refresh_cookie_file_from_secure_browser():
            self.progress_label.setText(f"Сессия {platform_label} сохранена. Повторяю запрос...")
        else:
            self.progress_label.setText("Сессия отмечена. Пробую повторить запрос...")
        self.update_auth_status("Вход: выполнен", "ok")
        if self.pending_retry == "download" and self.info:
            QTimer.singleShot(300, self.download)
        else:
            QTimer.singleShot(300, self.fetch_info)

    def update_auth_status(self, text: str, state: str = "idle") -> None:
        self.auth_status_label.setText(text)
        self.auth_status_dot.set_state(state)

    def set_busy(self, busy: bool, status: str, can_cancel: bool = False) -> None:
        self.status_label.setText(status)
        self.status_dot.set_state("busy" if busy else "ok")
        self.fetch_btn.setEnabled(not busy)
        self.download_btn.setEnabled((not busy) and bool(self.info))
        self.cancel_btn.setEnabled(bool(busy and can_cancel))

    def cancel_download(self) -> None:
        if not self.worker or not self.worker.is_alive():
            return
        self.cancel_requested.set()
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Отмена")
        self.progress_label.setText("Останавливаю скачивание...")
        self.log_message("Запрошена отмена скачивания.")

    def fetch_info(self) -> None:
        url = normalize_url(self.url_input.text())
        if not url:
            show_app_message(self, "Нужна ссылка", f"Вставьте ссылку на видео из {SUPPORTED_SITES_TEXT}.", "info")
            return
        if url != self.url_input.text().strip():
            self.url_input.setText(url)
        if self.worker and self.worker.is_alive():
            return
        self.clear_log()
        self.update_auth_status("Вход: проверка", "idle")
        self.progress.setRange(0, 0)
        self.progress_label.setText("Получаю метаданные и список форматов...")
        self.set_busy(True, "Загрузка")
        noplaylist = not self.playlist_check.isChecked()
        self.worker = threading.Thread(target=self.fetch_info_worker, args=(url, noplaylist), daemon=True)
        self.worker.start()

    def fetch_info_worker(self, url: str, noplaylist: bool) -> None:
        try:
            options: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": noplaylist,
                "skip_download": True,
                "ignore_no_formats_error": True,
                "format": "bestvideo*+bestaudio/best/b",
                "js_runtimes": js_runtime_options(),
                "logger": YtdlpLogger(self.events),
            }
            self.apply_cookie_options(options)
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and "entries" in info and noplaylist:
                entries = [entry for entry in info.get("entries") or [] if entry]
                if entries:
                    info = entries[0]
            if info and not has_downloadable_av_formats(info):
                platform_label = platform_label_for_url(url)
                self.events.put(
                    (
                        "auth_required",
                        {
                            "message": (
                                f"{platform_label} не вернул доступные форматы для скачивания. "
                                f"Обычно это означает, что нужна безопасная сессия {platform_label}."
                            ),
                            "retry": "fetch",
                        },
                    )
                )
                return
            self.events.put(("info", info))
        except Exception as exc:  # noqa: BLE001 - shown to user
            message = self.friendly_error("Не удалось получить данные", exc, platform_label_for_url(url))
            if self.needs_in_app_session(exc):
                self.events.put(("auth_required", {"message": message, "retry": "fetch"}))
            else:
                self.events.put(("error", message))

    def refresh_cookie_file_from_secure_browser(self) -> bool:
        browser = secure_browser_choice()
        if browser:
            browser_id, label, _path = browser
            profile = secure_browser_profile_dir(browser_id)
            if profile.exists():
                try:
                    ensure_session_dir()
                    jar = extract_cookies_from_browser(browser_id, str(profile), YtdlpLogger(self.events))
                    if not len(jar):
                        self.events.put(("log", f"Профиль {label} пока не содержит cookies."))
                        return False
                    temp_cookie_file = COOKIE_FILE.with_suffix(".tmp")
                    jar.save(str(temp_cookie_file), ignore_discard=True, ignore_expires=True)
                    temp_cookie_file.replace(COOKIE_FILE)
                    self.events.put(("log", f"Сессия TubeDrop обновлена из {label}."))
                    return True
                except Exception as exc:  # noqa: BLE001 - cookie refresh should not break fallback
                    self.events.put(
                        (
                            "log",
                            "Не удалось обновить cookies из браузера. "
                            f"Если сервис снова попросит вход, закройте окно входа и повторите запрос. Подробности: {clean_yt_text(exc)}",
                        )
                    )
        return False

    def apply_cookie_options(self, options: dict[str, Any]) -> None:
        refreshed = self.refresh_cookie_file_from_secure_browser()
        if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 60:
            options["cookiefile"] = str(COOKIE_FILE)
            if refreshed:
                self.events.put(("log", "Использую свежую сохранённую сессию TubeDrop."))
            else:
                self.events.put(("log", "Использую последнюю сохранённую сессию TubeDrop."))

    def build_choices(self, info: dict[str, Any]) -> list[DownloadChoice]:
        formats = info.get("formats") or []
        prefer_progressive = is_tiktok_info(info)
        primary_video_format = PROGRESSIVE_FIRST_VIDEO_AUDIO_FORMAT if prefer_progressive else UNIVERSAL_VIDEO_AUDIO_FORMAT
        fallback_video_format = UNIVERSAL_VIDEO_AUDIO_FORMAT
        heights = sorted(
            {
                fmt.get("height")
                for fmt in formats
                if fmt.get("vcodec") != "none" and isinstance(fmt.get("height"), int)
            },
            reverse=True,
        )

        choices = [
            DownloadChoice(
                "Лучшее MP4: видео + аудио",
                primary_video_format,
                "video",
                fallback_format=fallback_video_format,
            ),
            DownloadChoice(
                "Максимум: видео + аудио",
                UNIVERSAL_VIDEO_AUDIO_FORMAT,
                "video",
                fallback_format=PROGRESSIVE_FIRST_VIDEO_AUDIO_FORMAT,
            ),
        ]

        for height in heights:
            fps_values = [
                fmt.get("fps")
                for fmt in formats
                if fmt.get("vcodec") != "none" and fmt.get("height") == height and fmt.get("fps")
            ]
            fps_hint = f" {int(max(fps_values))}fps" if fps_values and max(fps_values) > 30 else ""
            merged_height_format = (
                f"bv[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
                f"bv[height<={height}][ext=mp4]+ba[ext=m4a]/"
                f"bv[height<={height}]+ba"
            )
            progressive_height_format = (
                f"b[height<={height}][ext=mp4][vcodec!=none][acodec!=none]/"
                f"best[height<={height}][ext=mp4][vcodec!=none][acodec!=none]/"
                f"b[height<={height}][vcodec!=none][acodec!=none]/"
                f"best[height<={height}][vcodec!=none][acodec!=none]"
            )
            height_format = (
                f"{progressive_height_format}/{merged_height_format}"
                if prefer_progressive
                else f"{merged_height_format}/{progressive_height_format}"
            )
            choices.append(
                DownloadChoice(
                    f"{height}p{fps_hint} MP4",
                    height_format,
                    "video",
                    fallback_format=fallback_video_format,
                )
            )

        choices.extend(
            [
                DownloadChoice("Только аудио M4A", "ba[ext=m4a]/ba/bestaudio/best/b", "audio", "m4a", "bestaudio/best/b"),
                DownloadChoice(
                    "Только аудио MP3 (192 kbps, Vegas)",
                    "ba[ext=m4a]/ba[acodec^=mp4a]/ba/bestaudio/best/b",
                    "audio",
                    "mp3",
                    "bestaudio/best/b",
                ),
                DownloadChoice("Только аудио WAV", "ba/bestaudio/best/b", "audio", "wav", "bestaudio/best/b"),
            ]
        )
        return choices

    def show_info(self, info: dict[str, Any] | None) -> None:
        if not info:
            self.events.put(("error", "yt-dlp не вернул сведения о видео."))
            return

        self.info = info
        self.choices = self.build_choices(info)
        self.quality_combo.clear()
        for choice in self.choices:
            self.quality_combo.addItem(choice.label)

        title = info.get("title") or "Без названия"
        uploader = info.get("uploader") or info.get("channel") or "автор неизвестен"
        duration = format_duration(info.get("duration"))
        views = compact_number(info.get("view_count"))
        views_text = f" · {views} просмотров" if views else ""
        self.title_label.setText(title)
        self.title_label.setToolTip(title)
        self.meta_label.setText(f"{uploader} · {duration}{views_text}")
        self.meta_label.setToolTip(f"{uploader} · {duration}{views_text}")
        self.download_btn.setEnabled(True)
        if secure_session_available():
            self.update_auth_status("Вход: сохранён", "ok")
        else:
            self.update_auth_status("Вход: не нужен", "ok")

        thumb = info.get("thumbnail")
        if thumb:
            threading.Thread(target=self.load_thumbnail_worker, args=(thumb,), daemon=True).start()
        else:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Превью недоступно")
        self.log_message("Сведения загружены. Выберите качество и папку.")

    def load_thumbnail_worker(self, url: str) -> None:
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            self.events.put(("thumbnail", image))
        except Exception as exc:  # noqa: BLE001 - preview is optional
            self.events.put(("log", f"Не удалось загрузить превью: {exc}"))

    def selected_choice(self) -> DownloadChoice | None:
        index = self.quality_combo.currentIndex()
        if 0 <= index < len(self.choices):
            return self.choices[index]
        return None

    def output_template(self, folder: Path) -> str:
        raw = self.filename_input.text().strip()
        if not raw:
            return str(folder / DEFAULT_OUTPUT_TEMPLATE)

        name = Path(raw).name.strip()
        if "%(" in name:
            template = name if "%(ext)" in name else f"{name}.%(ext)s"
        else:
            path_name = Path(name)
            base = path_name.stem if path_name.suffix else name
            template = f"{sanitize_filename_base(base)}.%(ext)s"
        return str(folder / template)

    def download(self) -> None:
        if not self.info:
            show_app_message(self, "Сначала данные", "Сначала загрузите сведения о видео.", "info")
            return
        choice = self.selected_choice()
        if not choice:
            show_app_message(self, "Выберите формат", "Выберите качество или аудиоформат.", "info")
            return
        folder = Path(self.folder_input.text()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            show_app_message(self, "Папка недоступна", f"Не удалось создать папку:\n{exc}", "error")
            return

        self.pending_download_choice = choice
        url = normalize_url(self.url_input.text())
        output_template = self.output_template(folder)
        noplaylist = not self.playlist_check.isChecked()
        recode = self.recode_check.isChecked()
        self.cancel_requested.clear()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.prepare_progress.setRange(0, 100)
        self.prepare_progress.setValue(0)
        self.prepare_label.setText("Подготовка файла")
        self.progress_label.setText("Начинаю скачивание...")
        self.set_busy(True, "Скачивание", can_cancel=True)
        self.worker = threading.Thread(
            target=self.download_worker,
            args=(choice, folder, url, output_template, noplaylist, recode),
            daemon=True,
        )
        self.worker.start()

    def download_worker(
        self,
        choice: DownloadChoice,
        folder: Path,
        url: str,
        output_template: str,
        noplaylist: bool,
        recode: bool,
    ) -> None:
        try:
            started_at = time.time()
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            postprocessors = []
            if choice.audio_codec:
                audio_quality = "192" if choice.audio_codec == "mp3" else "0"
                postprocessors.append(
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": choice.audio_codec,
                        "preferredquality": audio_quality,
                    }
                )

            def make_options(format_selector: str) -> dict[str, Any]:
                postprocessor_args: dict[str, list[str]] = {}
                options: dict[str, Any] = {
                    "format": format_selector,
                    "outtmpl": output_template,
                    "noplaylist": noplaylist,
                    "windowsfilenames": True,
                    "quiet": True,
                    "no_warnings": False,
                    "js_runtimes": js_runtime_options(),
                    "logger": YtdlpLogger(self.events),
                    "progress_hooks": [self.progress_hook],
                    "postprocessor_hooks": [self.postprocessor_hook],
                    "ffmpeg_location": ffmpeg,
                    "postprocessors": postprocessors,
                    "writethumbnail": True,
                    "write_all_thumbnails": False,
                }
                if choice.audio_codec == "mp3":
                    postprocessor_args["extractaudio+ffmpeg_o"] = [
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                        "-id3v2_version",
                        "3",
                    ]
                if choice.kind == "video":
                    options["merge_output_format"] = "mp4"
                    postprocessor_args["merger+ffmpeg_o"] = [
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                    ]
                if postprocessor_args:
                    options["postprocessor_args"] = postprocessor_args
                self.apply_cookie_options(options)
                return options

            selectors = [choice.ytdlp_format]
            if choice.fallback_format and choice.fallback_format not in selectors:
                selectors.append(choice.fallback_format)

            last_error: Exception | None = None
            for index, selector in enumerate(selectors):
                try:
                    if self.cancel_requested.is_set():
                        raise DownloadCancelled("Отменено пользователем")
                    if index:
                        self.events.put(("log", "Выбранный формат недоступен. Пробую универсальный доступный формат..."))
                    with YoutubeDL(make_options(selector)) as ydl:
                        ydl.download([url])
                    if self.cancel_requested.is_set():
                        raise DownloadCancelled("Отменено пользователем")
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - fallback handles yt-dlp's custom errors
                    last_error = exc
                    if not is_requested_format_unavailable(exc) or index == len(selectors) - 1:
                        raise
            if last_error:
                raise last_error
            final_file = find_new_media_file(folder, started_at)
            if final_file and needs_vegas_compatible_mp4(choice, url, recode):
                self.events.put(("prepare_progress", (-1, "Готовлю совместимый MP4 для Vegas...")))
                self.events.put(("log", "Перекодирую видео в H.264/AAC MP4 для Vegas."))
                final_file = make_vegas_compatible_mp4(final_file, ffmpeg)
                self.events.put(("prepare_progress", (100, "MP4 для Vegas готов")))
            self.events.put(
                (
                    "done",
                    {
                        "folder": str(folder),
                        "file": str(final_file) if final_file else "",
                    },
                )
            )
        except DownloadCancelled:
            self.events.put(("cancelled", None))
        except Exception as exc:  # noqa: BLE001 - shown to user
            message = self.friendly_error("Скачивание остановлено", exc, platform_label_for_url(url))
            if self.needs_in_app_session(exc):
                self.events.put(("auth_required", {"message": message, "retry": "download"}))
            else:
                self.events.put(("error", message))

    def progress_hook(self, data: dict[str, Any]) -> None:
        if self.cancel_requested.is_set():
            raise DownloadCancelled("Отменено пользователем")

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0
            speed = format_speed(data.get("speed"))
            eta = format_eta(data.get("eta"))
            parts = [f"{percent:.1f}%" if total else f"Скачано {format_size(downloaded)}"]
            if speed:
                parts.append(speed)
            if eta:
                parts.append(f"осталось {eta}")
            self.events.put(("progress", (percent, " · ".join(parts))))
        elif status == "finished":
            self.events.put(("progress", (100, "Файл скачан, подготавливаю итоговый формат...")))
            self.events.put(("prepare_progress", (-1, "Ожидаю подготовку файла...")))

    def postprocessor_hook(self, data: dict[str, Any]) -> None:
        if self.cancel_requested.is_set():
            raise DownloadCancelled("Отменено пользователем")

        status = data.get("status")
        if status not in {"started", "finished"}:
            return
        name = str(data.get("postprocessor") or "")
        if status == "finished":
            self.events.put(("prepare_progress", (100, "Подготовка завершена")))
            return
        if name == "ExtractAudio":
            self.events.put(("prepare_progress", (-1, "Готовлю совместимый аудиофайл...")))
        elif name == "Merger":
            self.events.put(("prepare_progress", (-1, "Вшиваю универсальную AAC-аудиодорожку...")))
        elif name:
            self.events.put(("prepare_progress", (-1, "Финальная подготовка файла...")))

    def friendly_error(self, prefix: str, exc: Exception, platform_label: str = "сервис") -> str:
        raw = clean_yt_text(exc)
        if self.needs_in_app_session(exc):
            return (
                f"{prefix}: {platform_label} запросил проверку или вход. "
                "Открою безопасный вход через отдельный профиль браузера. "
                f"Подробности: {raw}"
            )
        return f"{prefix}: {raw}"

    def needs_in_app_session(self, exc: Exception) -> bool:
        raw = str(exc).lower()
        markers = [
            "sign in to confirm",
            "sign in",
            "not a bot",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "login required",
            "log in",
            "logged in",
            "authentication required",
            "use --cookies-from-browser",
            "use --cookies",
            "cookie database",
            "cookies database",
        ]
        return any(marker in raw for marker in markers)

    def poll_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "info":
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.prepare_progress.setRange(0, 100)
                self.prepare_progress.setValue(0)
                self.prepare_label.setText("Подготовка файла")
                self.show_info(payload if isinstance(payload, dict) else None)
                self.set_busy(False, "Готово")
                self.progress_label.setText("Выберите настройки и нажмите «Скачать».")
            elif event == "thumbnail" and isinstance(payload, Image.Image):
                width = max(1, self.preview_label.width() or 312)
                height = max(1, self.preview_label.height() or 172)
                pixmap = make_preview(payload, width=width, height=height)
                self.preview_label.setText("")
                self.preview_label.setPixmap(pixmap)
            elif event == "progress":
                value, text = payload
                self.progress.setRange(0, 100)
                self.progress.setValue(max(0, min(int(float(value)), 100)))
                self.progress_label.setText(str(text))
            elif event == "prepare_progress":
                value, text = payload
                numeric = float(value)
                if numeric < 0:
                    self.prepare_progress.setRange(0, 0)
                else:
                    self.prepare_progress.setRange(0, 100)
                    self.prepare_progress.setValue(max(0, min(int(numeric), 100)))
                self.prepare_label.setText(str(text))
            elif event == "status":
                self.progress_label.setText(str(payload))
            elif event == "log":
                self.log_message(str(payload))
            elif event == "done":
                self.progress.setRange(0, 100)
                self.progress.setValue(100)
                self.prepare_progress.setRange(0, 100)
                self.prepare_progress.setValue(100)
                data = payload if isinstance(payload, dict) else {"folder": str(payload), "file": ""}
                folder = Path(str(data.get("folder") or default_download_dir()))
                file_text = str(data.get("file") or "")
                file_path = Path(file_text) if file_text else None
                self.progress_label.setText("Готово. Файл и превью сохранены.")
                self.log_message(f"Готово. Папка: {folder}")
                self.set_busy(False, "Готово")
                SuccessDialog(self, folder, file_path).exec()
            elif event == "cancelled":
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.prepare_progress.setRange(0, 100)
                self.prepare_progress.setValue(0)
                self.prepare_label.setText("Подготовка файла")
                self.progress_label.setText("Скачивание отменено.")
                self.log_message("Скачивание отменено.")
                self.set_busy(False, "Отменено")
            elif event == "auth_required":
                data = payload if isinstance(payload, dict) else {}
                message = str(data.get("message") or "Сервис запросил проверку.")
                retry = str(data.get("retry") or "fetch")
                self.update_auth_status("Вход: нужен", "warn")
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.prepare_progress.setRange(0, 100)
                self.prepare_progress.setValue(0)
                self.progress_label.setText("Нужен безопасный вход.")
                self.log_message(message, error=True)
                self.set_busy(False, "Проверка")
                self.open_auth_dialog(retry)
            elif event == "error":
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.prepare_progress.setRange(0, 100)
                self.prepare_progress.setValue(0)
                self.progress_label.setText(str(payload))
                self.log_message(str(payload), error=True)
                self.set_busy(False, "Ошибка")
                show_app_message(self, "Ошибка", str(payload), "error")

    def log_message(self, message: str, error: bool = False) -> None:
        prefix = "[!] " if error else ""
        self.log.appendPlainText(prefix + message)

    def clear_log(self) -> None:
        self.log.clear()


APP_STYLE = f"""
* {{
    font-family: {FONT_STACK};
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: transparent;
}}
QWidget {{
    background: {BG};
}}
QWidget#RootWindow {{
    background: {BG};
    border-radius: {WINDOW_RADIUS}px;
}}
QLabel, QFrame, QCheckBox {{
    background: transparent;
    border: none;
    padding: 0;
}}
QFrame#TitleBar {{
    background: #161616;
    border-radius: 18px;
}}
QLabel#LogoMark {{
    background: {ACCENT};
    color: #ffffff;
    border-radius: 16px;
    font-size: 14px;
    font-weight: 900;
}}
QLabel#WindowTitle {{
    font-size: 16px;
    font-weight: 850;
}}
QLabel#TinyMuted {{
    color: {SUBTLE};
    font-size: 10px;
}}
QLabel#DialogTitle, QLabel#PanelTitle {{
    font-size: 18px;
    font-weight: 850;
}}
QLabel#VideoTitle {{
    font-size: 19px;
    font-weight: 850;
}}
QLabel#SectionTitle, QLabel#FieldLabel {{
    font-size: 12px;
    font-weight: 750;
    color: {TEXT};
}}
QLabel#Muted {{
    color: {MUTED};
}}
QFrame#Card {{
    background: {PANEL};
    border-radius: 18px;
}}
QFrame#StatusPill {{
    background: {PANEL_2};
    border-radius: 15px;
    min-height: 30px;
}}
QLabel#StatusText {{
    color: {MUTED};
    font-size: 12px;
    font-weight: 700;
}}
QFrame#PreviewFrame {{
    background: #0b0b0b;
    border-radius: 16px;
}}
QLineEdit, QComboBox, QLineEdit#Input, QComboBox#Input, QLineEdit#UrlInput, QLineEdit#CompactInput {{
    background: {PANEL_2};
    border: none;
    border-radius: 13px;
    padding: 7px 10px;
    min-height: 20px;
    selection-background-color: {ACCENT};
}}
QLineEdit:hover, QComboBox:hover {{
    background: #292929;
}}
QLineEdit:focus, QComboBox:focus {{
    background: #2b2b2b;
}}
QLineEdit#UrlInput {{
    font-size: 14px;
    padding: 10px 13px;
}}
QComboBox::drop-down {{
    width: 0;
    border: none;
}}
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
}}
QComboBox QAbstractItemView {{
    background: {PANEL_2};
    selection-background-color: {PANEL_3};
    outline: 0;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 6px 2px 6px 0;
}}
QScrollBar::handle:vertical {{
    background: #505050;
    min-height: 28px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: #626262;
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
    border: none;
    height: 0;
}}
QPushButton {{
    border: none;
    border-radius: 13px;
    min-height: 22px;
    padding: 7px 13px;
    font-weight: 750;
}}
QPushButton#WindowButton, QPushButton#CloseButton {{
    background: transparent;
    color: {MUTED};
    border-radius: 10px;
    padding: 0;
    font-size: 18px;
    font-weight: 600;
}}
QPushButton#WindowButton:hover {{
    background: {PANEL_3};
    color: {TEXT};
}}
QPushButton#CloseButton:hover {{
    background: #7f1d1d;
    color: #ffffff;
}}
QPushButton#PrimaryButton {{
    background: {ACCENT};
    color: #ffffff;
}}
QPushButton#PrimaryButton:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#PrimaryButton:disabled {{
    background: #282828;
    color: {SUBTLE};
}}
QPushButton#DangerButton {{
    background: #2b1820;
    color: #fda4af;
}}
QPushButton#DangerButton:hover {{
    background: #3b1d28;
}}
QPushButton#DangerButton:disabled {{
    background: #242424;
    color: {SUBTLE};
}}
QPushButton#SecondaryButton {{
    background: {PANEL_2};
    color: {TEXT};
}}
QPushButton#SecondaryButton:hover {{
    background: {PANEL_3};
}}
QPushButton#FlatButton {{
    background: transparent;
    color: {MUTED};
    padding: 7px 10px;
}}
QPushButton#FlatButton:hover {{
    background: {PANEL_2};
}}
QPushButton#IconButton {{
    background: {PANEL_2};
    color: {TEXT};
    min-width: 36px;
    max-width: 36px;
    padding-left: 0;
    padding-right: 0;
}}
QPushButton#IconButton:hover {{
    background: {PANEL_3};
}}
QCheckBox {{
    color: {MUTED};
    spacing: 8px;
    min-height: 24px;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: none;
    border-radius: 6px;
    background: {PANEL_2};
}}
QCheckBox::indicator:hover {{
    background: {PANEL_3};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
}}
QPlainTextEdit#LogBox {{
    background: {LOG_BG};
    border: none;
    border-radius: 14px;
    padding: 9px;
    color: #d7d7d7;
    font-family: Consolas, monospace;
    font-size: 11px;
}}
QProgressBar#DownloadProgress {{
    background: {PANEL_2};
    border: none;
    border-radius: 7px;
    height: 12px;
    text-align: center;
    color: transparent;
}}
QProgressBar#DownloadProgress::chunk {{
    background: {ACCENT};
    border-radius: 6px;
}}
QMessageBox {{
    background: {BG};
}}
QFrame#DialogPanel, QFrame#SuccessPanel, QFrame#MessagePanel {{
    background: {PANEL};
    border-radius: 22px;
}}
QLabel#SuccessCheck {{
    background: #073f32;
    color: #7cf7c7;
    border-radius: 27px;
    font-size: 34px;
    font-weight: 900;
}}
QLabel#SuccessTitle {{
    font-size: 22px;
    font-weight: 850;
}}
QLabel#DoneFileName {{
    background: #0d0d0d;
    border: none;
    border-radius: 12px;
    color: {TEXT};
    padding: 9px 11px;
}}
QLabel#InfoIcon {{
    background: {PANEL_3};
    color: {TEXT};
    border-radius: 24px;
    font-size: 22px;
    font-weight: 900;
}}
QLabel#ErrorIcon {{
    background: #3b1d28;
    color: #fda4af;
    border-radius: 24px;
    font-size: 24px;
    font-weight: 900;
}}
"""


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont("Segoe UI", 10))
    window = TubeDropApp()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
