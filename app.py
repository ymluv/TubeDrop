from __future__ import annotations

import io
import json
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
    from PySide6.QtCore import QByteArray, QPointF, QRectF, QTimer, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
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


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    return base / relative_path


def app_state_dir() -> Path:
    return APP_DIR


STATE_DIR = app_state_dir()
APP_ICON_PATH = resource_path("assets/tubedrop.ico")
STATE_FILE = STATE_DIR / "tubedrop_state.json"
HISTORY_THUMB_DIR = STATE_DIR / "history_thumbnails"
SESSION_DIR = STATE_DIR / ".tubedrop_profile"
COOKIE_FILE = SESSION_DIR / "youtube_cookies.txt"
SECURE_BROWSER_ROOT = STATE_DIR / ".tubedrop_secure_browser"
SECURE_BROWSER_CHOICE = SESSION_DIR / "secure_browser.txt"
YOUTUBE_HOME = "https://www.youtube.com/"
TIKTOK_HOME = "https://www.tiktok.com/"
INSTAGRAM_HOME = "https://www.instagram.com/"
PINTEREST_HOME = "https://www.pinterest.com/"
TWITCH_HOME = "https://www.twitch.tv/"
SUPPORTED_PLATFORMS = ["YouTube", "TikTok", "Instagram", "Pinterest", "Twitch"]
SUPPORTED_SITES_TEXT = "YouTube, TikTok, Instagram, Pinterest или Twitch"

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
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_HISTORY_ITEMS = 120
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
    if "twitch.tv" in host:
        return "Twitch"
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
    if "twitch.tv" in host:
        return TWITCH_HOME
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SECURE_BROWSER_ROOT.mkdir(parents=True, exist_ok=True)


def default_app_state() -> dict[str, Any]:
    return {"settings": {}, "history": []}


def load_app_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_app_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_app_state()
    if not isinstance(data, dict):
        return default_app_state()
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    history = data.get("history") if isinstance(data.get("history"), list) else []
    return {"settings": settings, "history": history}


def save_app_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


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


def cookie_file_available() -> bool:
    return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 60


def platform_prefers_login_before_fetch(url: str) -> bool:
    return platform_label_for_url(url) == "Instagram" and not (cookie_file_available() or secure_session_available())


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


def find_download_thumbnail(folder: Path, media_path: Path | None, started_at: float) -> Path | None:
    exact_candidates: list[Path] = []
    if media_path:
        for suffix in THUMBNAIL_EXTENSIONS:
            candidate = media_path.with_suffix(suffix)
            if candidate.exists():
                exact_candidates.append(candidate)
    if exact_candidates:
        return max(exact_candidates, key=lambda item: item.stat().st_mtime)

    recent_candidates: list[Path] = []
    try:
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in THUMBNAIL_EXTENSIONS:
                continue
            try:
                if path.stat().st_mtime >= started_at - 10:
                    recent_candidates.append(path)
            except OSError:
                continue
    except OSError:
        return None
    if not recent_candidates:
        return None
    return max(recent_candidates, key=lambda item: item.stat().st_mtime)


PLATFORM_ICON_SVGS = {
    "YouTube": """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
          <rect x="6" y="14" width="52" height="36" rx="11" fill="#ff0033"/>
          <path d="M28 24v16l15-8z" fill="#fff"/>
        </svg>
    """,
    "TikTok": """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
          <path d="M36 8h9c1 8 5 13 13 15v9c-5 0-10-2-14-5v17c0 10-8 17-18 17S8 54 8 44s8-17 18-17c2 0 4 0 6 1v10c-2-1-4-2-6-2-5 0-8 4-8 8s3 8 8 8 8-3 8-8V8z" fill="#fff"/>
          <path d="M32 28v10c-2-1-4-2-6-2-5 0-8 4-8 8 0 2 1 5 3 6-5-1-9-6-9-12 0-7 6-13 14-13 2 0 4 1 6 3z" fill="#25f4ee"/>
          <path d="M45 8c1 8 5 13 13 15v6c-8-1-14-6-17-13h-5V8z" fill="#fe2c55"/>
        </svg>
    """,
    "Instagram": """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
          <defs>
            <linearGradient id="g" x1="10" y1="56" x2="56" y2="8" gradientUnits="userSpaceOnUse">
              <stop offset="0" stop-color="#feda75"/>
              <stop offset=".35" stop-color="#fa7e1e"/>
              <stop offset=".65" stop-color="#d62976"/>
              <stop offset="1" stop-color="#4f5bd5"/>
            </linearGradient>
          </defs>
          <rect x="8" y="8" width="48" height="48" rx="14" fill="url(#g)"/>
          <rect x="19" y="19" width="26" height="26" rx="8" fill="none" stroke="#fff" stroke-width="4"/>
          <circle cx="32" cy="32" r="7" fill="none" stroke="#fff" stroke-width="4"/>
          <circle cx="43" cy="21" r="3" fill="#fff"/>
        </svg>
    """,
    "Pinterest": """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
          <circle cx="32" cy="32" r="28" fill="#e60023"/>
          <path d="M30 44c-2-1-4-2-5-4l-3 12c0 2-3 2-3 0l-1-1c1-6 3-12 4-18-1-2-1-4-1-6 0-8 6-14 14-14 7 0 12 5 12 12 0 9-5 17-12 17-3 0-5-2-5-5l1-4c1 2 2 3 4 3 4 0 7-5 7-11 0-4-3-7-8-7-6 0-9 4-9 9 0 2 1 4 2 5l-1 4c-3-1-5-5-5-9 0-7 6-13 15-13 8 0 14 5 14 12 0 9-6 17-14 17-3 0-5-1-6-3z" fill="#fff"/>
        </svg>
    """,
    "Twitch": """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
          <path d="M13 8h43v30L43 51H32l-8 8h-8v-8H8V19z" fill="#9146ff"/>
          <path d="M18 14v29h10v8l8-8h12l8-8V14z" fill="#fff"/>
          <path d="M42 23h5v13h-5zm-13 0h5v13h-5z" fill="#9146ff"/>
        </svg>
    """,
}


def platform_icon_svg(platform: str) -> str:
    return PLATFORM_ICON_SVGS.get(platform, PLATFORM_ICON_SVGS["YouTube"])


def render_svg_icon(painter: QPainter, svg: str, rect: QRectF) -> None:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    renderer.render(painter, rect)


def app_icon() -> QIcon:
    return QIcon(str(APP_ICON_PATH)) if APP_ICON_PATH.exists() else QIcon()


def set_windows_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        app_id = f"{APP_TITLE}.Desktop"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def reveal_in_file_manager(file_path: Path | None, folder_path: Path | None = None) -> None:
    if os.name == "nt" and file_path and file_path.exists():
        resolved_file = file_path.resolve()
        subprocess.Popen(f'explorer.exe /select,"{resolved_file}"')
        return
    target = folder_path if folder_path and folder_path.exists() else None
    if not target and file_path and file_path.parent.exists():
        target = file_path.parent
    if target:
        os.startfile(str(target.resolve()))  # type: ignore[attr-defined]


def format_history_time(timestamp: float | int | None) -> str:
    try:
        value = float(timestamp or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return ""
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(value))


def clean_history_items(items: list[Any]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file") or "")
        folder = str(item.get("folder") or "")
        if not file_path and not folder:
            continue
        cleaned.append(
            {
                "id": str(item.get("id") or int(time.time() * 1000)),
                "title": str(item.get("title") or Path(file_path).stem or "Без названия"),
                "url": str(item.get("url") or ""),
                "platform": str(item.get("platform") or platform_label_for_url(str(item.get("url") or ""))),
                "file": file_path,
                "folder": folder or str(Path(file_path).parent),
                "thumbnail": str(item.get("thumbnail") or ""),
                "downloaded_at": item.get("downloaded_at") or 0,
            }
        )
    return cleaned[:MAX_HISTORY_ITEMS]


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


def thumbnail_pixmap_from_file(path: Path, width: int, height: int) -> QPixmap:
    try:
        image = Image.open(path).convert("RGB")
        return make_preview(image, width=width, height=height)
    except Exception:
        return QPixmap()


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


class HistoryPreview(QFrame):
    def __init__(
        self,
        platform: str,
        thumbnail: Path | None = None,
        on_delete: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.platform = platform
        self.on_delete = on_delete
        self.delete_hovered = False
        self._pixmap = thumbnail_pixmap_from_file(thumbnail, 166, 94) if thumbnail else QPixmap()
        self.setObjectName("HistoryPreview")
        self.setFixedSize(166, 94)
        self.setMouseTracking(True)

    def delete_rect(self) -> QRectF:
        return QRectF(self.width() - 37, 7, 28, 28)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.update_delete_hover(event.position())
        super().enterEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.update_delete_hover(event.position())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if self.delete_hovered:
            self.delete_hovered = False
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        if event.button() == Qt.LeftButton and self.delete_rect().contains(event.position()):
            if self.on_delete:
                self.on_delete()
            event.accept()
            return
        super().mousePressEvent(event)

    def update_delete_hover(self, position: QPointF) -> None:
        hovered = self.delete_rect().contains(position)
        if hovered == self.delete_hovered:
            return
        self.delete_hovered = hovered
        if hovered:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        self.update()

    def draw_trash(self, painter: QPainter, rect: QRectF) -> None:
        icon_color = QColor("#ef4444" if self.delete_hovered else "#ffffff")
        icon_color.setAlpha(245 if self.delete_hovered else 150)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80 if self.delete_hovered else 45))
        painter.drawRoundedRect(rect, 11, 11)

        pen = QPen(icon_color, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = rect.center().x()
        top = rect.top() + 8
        painter.drawLine(QPointF(cx - 7, top + 2), QPointF(cx + 7, top + 2))
        painter.drawLine(QPointF(cx - 4, top), QPointF(cx + 4, top))
        body = QRectF(cx - 6, top + 5, 12, 12)
        painter.drawRoundedRect(body, 2, 2)
        painter.drawLine(QPointF(cx - 2.5, top + 8), QPointF(cx - 2.5, top + 14))
        painter.drawLine(QPointF(cx + 2.5, top + 8), QPointF(cx + 2.5, top + 14))

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom clipped preview painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        path = QPainterPath()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rect, 10, 10)
        painter.fillPath(path, QColor("#0b0b0b"))
        painter.setClipPath(path)

        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            left = (self.width() - scaled.width()) // 2
            top = (self.height() - scaled.height()) // 2
            painter.drawPixmap(left, top, scaled)
        else:
            painter.setPen(QColor(SUBTLE))
            font = QFont(FONT_STACK, 12)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self.platform)

        painter.setClipping(False)
        icon_rect = QRectF(9, 8, 28, 28)
        render_svg_icon(painter, platform_icon_svg(self.platform), icon_rect)
        self.draw_trash(painter, self.delete_rect())


class HistoryCard(QFrame):
    def __init__(
        self,
        item: dict[str, Any],
        on_open_file: Any,
        on_open_folder: Any,
        on_delete: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.on_open_file = on_open_file
        self.on_open_folder = on_open_folder
        self.on_delete = on_delete
        self.setObjectName("HistoryCard")
        self.setFixedSize(188, 222)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        thumbnail = Path(str(item.get("thumbnail") or "")) if item.get("thumbnail") else None
        if thumbnail and not thumbnail.exists():
            thumbnail = None
        platform = str(item.get("platform") or "сайт")
        item_id = str(item.get("id") or "")
        layout.addWidget(HistoryPreview(platform, thumbnail, lambda: self.on_delete(item_id)), 0, Qt.AlignCenter)

        title = QLabel(str(item.get("title") or "Без названия"))
        title.setObjectName("HistoryTitle")
        title.setWordWrap(True)
        title.setMaximumHeight(38)
        title.setToolTip(title.text())
        layout.addWidget(title)

        downloaded_at = format_history_time(item.get("downloaded_at"))
        meta = QLabel(downloaded_at or platform)
        meta.setObjectName("HistoryMeta")
        meta.setToolTip(str(item.get("file") or ""))
        layout.addWidget(meta)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        file_text = str(item.get("file") or "")
        folder_text = str(item.get("folder") or "")
        file_path = Path(file_text) if file_text else Path("__missing_file__")
        folder_path = Path(folder_text) if folder_text else Path("__missing_folder__")

        file_btn = QPushButton("Открыть")
        file_btn.setObjectName("HistoryButton")
        file_btn.setToolTip("Открыть файл")
        file_btn.setEnabled(file_path.exists())
        file_btn.clicked.connect(lambda: self.on_open_file(file_path))
        actions.addWidget(file_btn, 1)

        folder_btn = QPushButton("Папка")
        folder_btn.setObjectName("HistoryButton")
        folder_btn.setToolTip("Открыть папку")
        folder_btn.setEnabled(folder_path.exists() or (bool(file_text) and file_path.parent.exists()))
        folder_btn.clicked.connect(lambda: self.on_open_folder(folder_path, file_path))
        actions.addWidget(folder_btn, 1)
        layout.addLayout(actions)


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
                width: 12px;
                margin: 8px 3px 8px 3px;
            }}
            QScrollBar::handle:vertical {{
                background: #585858;
                min-height: 38px;
                border-radius: 6px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #767676;
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


class PlatformIcon(QWidget):
    def __init__(self, platform: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.platform = platform
        self.setFixedSize(22, 22)
        self.setToolTip(platform)

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 - custom SVG icon painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        render_svg_icon(painter, platform_icon_svg(self.platform), QRectF(1, 1, self.width() - 2, self.height() - 2))


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

        source_row = QHBoxLayout()
        source_row.setContentsMargins(0, 0, 4, 0)
        source_row.setSpacing(7)
        for platform in SUPPORTED_PLATFORMS:
            source_row.addWidget(PlatformIcon(platform))
        layout.addLayout(source_row)

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
        self.update_state(f"Сессия {label} отмечена. Жду сохранения cookies и повторю запрос.")
        QTimer.singleShot(1800, self.complete_secure_login)

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
        reveal_in_file_manager(self.file_path, self.folder)
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
        self.app_state = load_app_state()
        self.settings = self.app_state.get("settings", {})
        self.download_history = clean_history_items(self.app_state.get("history", []))
        self._loading_settings = True
        self.setWindowTitle(APP_TITLE)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(980, 680)
        self.setStyleSheet(APP_STYLE)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.info: dict[str, Any] | None = None
        self.info_url = ""
        self.is_busy = False
        self.choices: list[DownloadChoice] = []
        self.worker: threading.Thread | None = None
        self.auth_dialog: AuthDialog | None = None
        self.cancel_requested = threading.Event()
        self.pending_retry = "fetch"
        self.pending_download_choice: DownloadChoice | None = None
        self.current_thumbnail_image: Image.Image | None = None
        self.recent_messages: list[str] = []
        self.preferred_quality_label = str(self.settings.get("quality_label") or "")

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Вставьте ссылку...")
        self.url_input.returnPressed.connect(self.primary_action)

        self.folder_input = QLineEdit(str(self.settings.get("folder") or default_download_dir()))
        self.filename_input = QLineEdit()
        self.filename_input.setPlaceholderText("Введите название")
        self.filename_input.setText(str(self.settings.get("filename") or ""))
        self.quality_combo = ModernComboBox()
        self.recode_check = QCheckBox("MP4 для Vegas")
        self.recode_check.setChecked(bool(self.settings.get("recode_for_vegas", False)))
        self.playlist_check = QCheckBox("Плейлист целиком")
        self.playlist_check.setChecked(bool(self.settings.get("playlist_whole", False)))

        self.auth_status_dot = StatusDot("idle")
        self.auth_status_label = QLabel("Вход: не проверен")
        self.status_dot = StatusDot("ok")
        self.status_label = QLabel("Готово")
        self.preview_label = PreviewBox("Превью")
        self.title_label = QLabel("Ссылка не выбрана")
        self.meta_label = QLabel("Ожидание данных")
        self.progress = QProgressBar()
        self.progress_label = QLabel("Ожидание")
        self.prepare_progress = QProgressBar()
        self.prepare_label = QLabel("Подготовка файла")
        self.download_btn = QPushButton("Получить")
        self.cancel_btn = QPushButton("Отмена")

        self.build_ui()
        self.connect_settings_signals()
        self._loading_settings = False
        self.refresh_history()
        self.update_primary_action()
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

        history_panel = QFrame()
        history_panel.setObjectName("Card")
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(12, 10, 12, 12)
        history_layout.setSpacing(8)

        history_header = QHBoxLayout()
        history_title = QLabel("История скачиваний")
        history_title.setObjectName("SectionTitle")
        history_header.addWidget(history_title)
        history_header.addStretch(1)
        history_layout.addLayout(history_header)

        self.history_scroll = QScrollArea()
        self.history_scroll.setObjectName("HistoryScroll")
        self.history_scroll.setFrameShape(QFrame.NoFrame)
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_viewport = QWidget()
        self.history_viewport.setObjectName("HistoryViewport")
        self.history_grid = QGridLayout(self.history_viewport)
        self.history_grid.setContentsMargins(0, 0, 4, 0)
        self.history_grid.setHorizontalSpacing(8)
        self.history_grid.setVerticalSpacing(8)
        for column in range(3):
            self.history_grid.setColumnStretch(column, 1)
        self.history_scroll.setWidget(self.history_viewport)
        history_layout.addWidget(self.history_scroll, 1)
        content_col.addWidget(history_panel, 1)

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
        self.download_btn.clicked.connect(self.primary_action)
        self.download_btn.setEnabled(False)
        actions.addWidget(self.download_btn)
        right_layout.addLayout(actions)

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().showEvent(event)
        hide_native_border_for(self)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt event type differs by binding version
        self.save_current_settings()
        super().closeEvent(event)

    def add_labeled(self, layout: QVBoxLayout, label: str, widget_or_layout: QWidget | QHBoxLayout) -> None:
        text = QLabel(label)
        text.setObjectName("FieldLabel")
        layout.addWidget(text)
        if isinstance(widget_or_layout, QHBoxLayout):
            layout.addLayout(widget_or_layout)
        else:
            layout.addWidget(widget_or_layout)

    def connect_settings_signals(self) -> None:
        self.url_input.textChanged.connect(self.on_url_changed)
        self.folder_input.textChanged.connect(self.save_current_settings)
        self.filename_input.textChanged.connect(self.save_current_settings)
        self.quality_combo.currentTextChanged.connect(self.save_current_settings)
        self.recode_check.toggled.connect(self.save_current_settings)
        self.playlist_check.toggled.connect(self.save_current_settings)

    def current_url(self) -> str:
        return normalize_url(self.url_input.text())

    def info_matches_current_url(self) -> bool:
        current = self.current_url()
        return bool(current and self.info and self.info_url == current)

    def update_primary_action(self) -> None:
        if self.is_busy:
            self.download_btn.setEnabled(False)
            return
        if self.info_matches_current_url():
            self.download_btn.setText("Скачать")
            self.download_btn.setEnabled(True)
            return
        self.download_btn.setText("Получить")
        self.download_btn.setEnabled(bool(self.current_url()))

    def on_url_changed(self, text: str) -> None:
        current = normalize_url(text)
        if current != self.info_url:
            self.info = None
            self.choices = []
            self.quality_combo.blockSignals(True)
            self.quality_combo.clear()
            self.quality_combo.blockSignals(False)
            self.pending_download_choice = None
            self.current_thumbnail_image = None
            self.preview_label.setPixmap(QPixmap())
            if current:
                self.title_label.setText("Ссылка вставлена")
                self.meta_label.setText("Нажмите «Получить», чтобы загрузить форматы.")
                self.progress_label.setText("Ссылка готова к проверке.")
            else:
                self.title_label.setText("Ссылка не выбрана")
                self.meta_label.setText("Ожидание данных")
                self.progress_label.setText("Ожидание")
        self.update_primary_action()

    def primary_action(self) -> None:
        if self.is_busy:
            return
        if self.info_matches_current_url():
            self.download()
            return
        self.fetch_info()

    def save_current_settings(self, *args: Any) -> None:  # noqa: ARG002 - Qt passes signal payloads
        if self._loading_settings:
            return
        self.settings = {
            "folder": self.folder_input.text().strip(),
            "filename": self.filename_input.text().strip(),
            "quality_label": self.quality_combo.currentText().strip(),
            "recode_for_vegas": self.recode_check.isChecked(),
            "playlist_whole": self.playlist_check.isChecked(),
        }
        self.preferred_quality_label = self.settings["quality_label"]
        self.app_state["settings"] = self.settings
        self.persist_app_state()

    def persist_app_state(self) -> None:
        self.app_state["history"] = self.download_history[:MAX_HISTORY_ITEMS]
        try:
            save_app_state(self.app_state)
        except OSError as exc:
            self.recent_messages.append(f"[!] Не удалось сохранить настройки: {exc}")

    def clear_grid_layout(self, layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def refresh_history(self) -> None:
        self.clear_grid_layout(self.history_grid)
        if not self.download_history:
            empty_label = QLabel("История пока пустая")
            empty_label.setObjectName("HistoryEmpty")
            empty_label.setAlignment(Qt.AlignCenter)
            self.history_grid.addWidget(empty_label, 0, 0, 1, 3)
            return

        for index, item in enumerate(self.download_history[:MAX_HISTORY_ITEMS]):
            row = index // 3
            column = index % 3
            card = HistoryCard(item, self.open_history_file, self.open_history_folder, self.remove_history_item)
            self.history_grid.addWidget(card, row, column, Qt.AlignTop | Qt.AlignHCenter)

    def remove_history_item(self, item_id: str) -> None:
        for entry in self.download_history:
            if str(entry.get("id") or "") != item_id:
                continue
            thumbnail_text = str(entry.get("thumbnail") or "")
            if thumbnail_text:
                thumbnail = Path(thumbnail_text)
                try:
                    if thumbnail.exists() and thumbnail.resolve().parent == HISTORY_THUMB_DIR.resolve():
                        thumbnail.unlink(missing_ok=True)
                except OSError:
                    pass
            break
        self.download_history = [entry for entry in self.download_history if str(entry.get("id") or "") != item_id]
        self.persist_app_state()
        self.refresh_history()

    def open_history_file(self, file_path: Path) -> None:
        if not file_path.exists():
            show_app_message(self, "Файл не найден", "Этот файл уже удален или перемещен.", "info")
            self.refresh_history()
            return
        os.startfile(str(file_path))  # type: ignore[attr-defined]

    def open_history_folder(self, folder_path: Path, file_path: Path) -> None:
        target = folder_path if folder_path.exists() else file_path.parent
        if not file_path.exists() and not target.exists():
            show_app_message(self, "Папка не найдена", "Папка уже удалена или перемещена.", "info")
            self.refresh_history()
            return
        reveal_in_file_manager(file_path if file_path.exists() else None, target)

    def save_current_thumbnail_for_history(self, item_id: str) -> str:
        if not self.current_thumbnail_image:
            return ""
        try:
            HISTORY_THUMB_DIR.mkdir(parents=True, exist_ok=True)
            target = HISTORY_THUMB_DIR / f"{item_id}.png"
            image = self.current_thumbnail_image.copy().convert("RGB")
            image.thumbnail((640, 360), Image.Resampling.LANCZOS)
            image.save(target, format="PNG")
            return str(target)
        except OSError:
            return ""

    def add_history_entry(self, folder: Path, file_path: Path | None, thumbnail_path: str = "") -> None:
        if not file_path:
            return
        info = self.info or {}
        url = str(info.get("webpage_url") or info.get("original_url") or self.url_input.text().strip())
        title = str(info.get("title") or file_path.stem or "Без названия")
        item_id = str(int(time.time() * 1000))
        thumbnail = thumbnail_path if thumbnail_path and Path(thumbnail_path).exists() else self.save_current_thumbnail_for_history(item_id)
        item = {
            "id": item_id,
            "title": title,
            "url": url,
            "platform": platform_label_for_url(url),
            "file": str(file_path),
            "folder": str(folder),
            "thumbnail": thumbnail,
            "downloaded_at": int(time.time()),
        }
        existing = [entry for entry in self.download_history if str(entry.get("file") or "") != str(file_path)]
        self.download_history = [item, *existing][:MAX_HISTORY_ITEMS]
        self.persist_app_state()
        self.refresh_history()

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
        retry = self.pending_retry
        self.progress_label.setText(f"Сохраняю сессию {platform_label}...")
        self.update_auth_status("Вход: сохранение", "idle")
        self.set_busy(True, "Вход")
        threading.Thread(target=self.session_refresh_worker, args=(retry,), daemon=True).start()

    def session_refresh_worker(self, retry: str) -> None:
        refreshed = self.refresh_cookie_file_from_secure_browser(attempts=10, delay=0.8)
        self.events.put(("session_ready", {"retry": retry, "refreshed": refreshed}))

    def update_auth_status(self, text: str, state: str = "idle") -> None:
        self.auth_status_label.setText(text)
        self.auth_status_dot.set_state(state)

    def set_busy(self, busy: bool, status: str, can_cancel: bool = False) -> None:
        self.is_busy = busy
        self.status_label.setText(status)
        self.status_dot.set_state("busy" if busy else "ok")
        self.cancel_btn.setEnabled(bool(busy and can_cancel))
        if busy:
            if can_cancel:
                label = "Скачиваю..."
            elif status == "Вход":
                label = "Сохраняю..."
            else:
                label = "Получаю..."
            self.download_btn.setText(label)
            self.download_btn.setEnabled(False)
        else:
            self.update_primary_action()

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
        if platform_prefers_login_before_fetch(url):
            platform_label = platform_label_for_url(url)
            self.pending_retry = "fetch"
            self.update_auth_status("Вход: нужен", "warn")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress_label.setText(f"Для {platform_label} нужен вход. Открываю безопасный вход...")
            self.open_auth_dialog("fetch")
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
            self.events.put(("info", {"url": url, "info": info}))
        except Exception as exc:  # noqa: BLE001 - shown to user
            message = self.friendly_error("Не удалось получить данные", exc, platform_label_for_url(url))
            if self.needs_in_app_session(exc):
                self.events.put(("auth_required", {"message": message, "retry": "fetch"}))
            else:
                self.events.put(("error", message))

    def refresh_cookie_file_from_secure_browser(self, attempts: int = 1, delay: float = 0.0) -> bool:
        browser = secure_browser_choice()
        if not browser:
            return False
        browser_id, label, _path = browser
        profile = secure_browser_profile_dir(browser_id)
        last_error = ""
        total_attempts = max(1, attempts)
        for attempt in range(total_attempts):
            if profile.exists():
                try:
                    ensure_session_dir()
                    jar = extract_cookies_from_browser(browser_id, str(profile), YtdlpLogger(self.events))
                    if len(jar):
                        temp_cookie_file = COOKIE_FILE.with_suffix(".tmp")
                        jar.save(str(temp_cookie_file), ignore_discard=True, ignore_expires=True)
                        temp_cookie_file.replace(COOKIE_FILE)
                        self.events.put(("log", f"Сессия TubeDrop обновлена из {label}."))
                        return True
                    last_error = f"Профиль {label} пока не содержит cookies."
                except Exception as exc:  # noqa: BLE001 - cookie refresh should not break fallback
                    last_error = clean_yt_text(exc)
            if attempt < total_attempts - 1 and delay > 0:
                time.sleep(delay)
        if last_error:
            self.events.put(
                (
                    "log",
                    "Не удалось обновить cookies из браузера. "
                    f"Если сервис снова попросит вход, закройте окно входа и повторите запрос. Подробности: {last_error}",
                )
            )
        return False

    def apply_cookie_options(self, options: dict[str, Any]) -> None:
        refreshed = self.refresh_cookie_file_from_secure_browser()
        if cookie_file_available():
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

    def show_info(self, info: dict[str, Any] | None, source_url: str = "") -> None:
        if not info:
            self.events.put(("error", "yt-dlp не вернул сведения о видео."))
            return

        self.info = info
        self.info_url = source_url or self.current_url()
        self.choices = self.build_choices(info)
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for choice in self.choices:
            self.quality_combo.addItem(choice.label)
        if self.preferred_quality_label:
            for index, choice in enumerate(self.choices):
                if choice.label == self.preferred_quality_label:
                    self.quality_combo.setCurrentIndex(index)
                    break
        self.quality_combo.blockSignals(False)
        self.save_current_settings()

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

        self.current_thumbnail_image = None
        thumb = info.get("thumbnail")
        if thumb:
            threading.Thread(target=self.load_thumbnail_worker, args=(thumb,), daemon=True).start()
        else:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Превью недоступно")
        self.log_message("Сведения загружены. Выберите качество и папку.")
        self.update_primary_action()

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
            thumbnail_file = find_download_thumbnail(folder, final_file, started_at)
            self.events.put(
                (
                    "done",
                    {
                        "folder": str(folder),
                        "file": str(final_file) if final_file else "",
                        "thumbnail": str(thumbnail_file) if thumbnail_file else "",
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
                source_url = ""
                info_payload = payload if isinstance(payload, dict) else None
                if isinstance(info_payload, dict) and "info" in info_payload:
                    source_url = str(info_payload.get("url") or "")
                    info_payload = info_payload.get("info") if isinstance(info_payload.get("info"), dict) else None
                if source_url and source_url != self.current_url():
                    self.set_busy(False, "Готово")
                    self.progress_label.setText("Ссылка изменилась. Нажмите «Получить» для новой ссылки.")
                    continue
                self.show_info(info_payload if isinstance(info_payload, dict) else None, source_url)
                self.set_busy(False, "Готово")
                self.progress_label.setText("Выберите настройки и нажмите «Скачать».")
            elif event == "thumbnail" and isinstance(payload, Image.Image):
                self.current_thumbnail_image = payload.copy()
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
                thumbnail_path = str(data.get("thumbnail") or "")
                self.progress_label.setText("Готово. Файл и превью сохранены.")
                self.add_history_entry(folder, file_path, thumbnail_path)
                self.set_busy(False, "Готово")
                SuccessDialog(self, folder, file_path).exec()
            elif event == "session_ready":
                data = payload if isinstance(payload, dict) else {}
                retry = str(data.get("retry") or "fetch")
                refreshed = bool(data.get("refreshed"))
                self.set_busy(False, "Готово")
                if refreshed:
                    self.update_auth_status("Вход: выполнен", "ok")
                    self.progress_label.setText("Сессия сохранена. Повторяю запрос...")
                else:
                    self.update_auth_status("Вход: проверить", "warn")
                    self.progress_label.setText("Сессия отмечена, но cookies пока не видны. Пробую запрос еще раз...")
                if retry == "download" and self.info_matches_current_url():
                    QTimer.singleShot(300, self.download)
                else:
                    QTimer.singleShot(300, self.fetch_info)
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
        text = prefix + clean_yt_text(message)
        self.recent_messages.append(text)
        self.recent_messages = self.recent_messages[-120:]
        if error:
            self.progress_label.setText(text)

    def clear_log(self) -> None:
        self.recent_messages.clear()


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
QFrame#HistoryCard {{
    background: #202020;
    border-radius: 14px;
}}
QFrame#HistoryPreview {{
    background: #0b0b0b;
    border-radius: 10px;
}}
QWidget#HistoryViewport {{
    background: transparent;
}}
QScrollArea#HistoryScroll {{
    background: transparent;
    border: none;
}}
QLabel#HistoryTitle {{
    color: {TEXT};
    font-size: 12px;
    font-weight: 760;
}}
QLabel#HistoryMeta, QLabel#HistoryEmpty {{
    color: {MUTED};
    font-size: 11px;
}}
QLabel#HistoryEmpty {{
    padding-top: 42px;
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
    width: 12px;
    margin: 8px 3px 8px 3px;
}}
QScrollBar::handle:vertical {{
    background: #585858;
    min-height: 38px;
    border-radius: 6px;
}}
QScrollBar::handle:vertical:hover {{
    background: #767676;
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
QPushButton#HistoryButton {{
    background: {PANEL_2};
    color: {TEXT};
    border-radius: 9px;
    min-height: 24px;
    padding: 4px 6px;
    font-size: 11px;
}}
QPushButton#HistoryButton:hover {{
    background: {PANEL_3};
}}
QPushButton#HistoryButton:disabled {{
    background: #242424;
    color: {SUBTLE};
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
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setFont(QFont("Segoe UI", 10))
    window = TubeDropApp()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
