#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
速映 FlashPlay — 一个单文件的下载与播放图形界面（VLC 风格深色主题）
================================================================

零依赖：只用 Python 标准库（tkinter），单文件即可运行、即可发布。
界面参考 VLC media player 的布局：顶部工具条 / 中部任务列表 / 底部进度条与状态栏。

下载通道：网址下载（yt-dlp，B站/YouTube 等 1800+ 网站）+ 磁力下载（BitTorrent/aria2c）。
内置播放器：mpv 内核嵌入窗口，下载完直接播放。

功能
----
* 粘贴链接加入下载队列，自动获取标题、UP 主、时长、封面等信息
* 画质选择：最佳 / 1080P / 720P / 480P / 仅音频(mp3)
* 实时进度：百分比、速度、剩余时间、合并/转码状态
* 队列串行下载、暂停/继续、取消、失败自动重试
* 内核全自动：yt-dlp / ffmpeg / mpv / aria2c 首次使用时自动获取（国内镜像加速）
* 内核自维护：设置里一键检查更新 yt-dlp；ffmpeg / mpv / aria2c 首次使用时自动获取
* B 站等站点遇到不可达 CDN 节点时自动重试（换节点）
* 完成后双击任务即可打开文件 / 右键菜单打开所在文件夹
* 配置持久化：画质、下载目录、cookies 浏览器、代理等

用法
----
普通用户：双击 速映.exe 即可（无需安装；首次下载时自动准备所需内核）

开发者（需要 Python 3.8+）：
    python sy.py                # 启动图形界面
    python sy.py --selftest     # 无界面自检（真实下载一个短视频验证）
    python sy.py "链接"         # 启动并把链接加入队列

作者：可自由修改与再发布（MIT License 建议）
"""

import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import zlib
from tkinter import filedialog, messagebox, ttk

try:      # 仅 Windows 需要：用 PeekNamedPipe 判断管道里有没有数据可读
    from ctypes import wintypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PeekNamedPipe = _kernel32.PeekNamedPipe
    _PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD)]
    _PeekNamedPipe.restype = wintypes.BOOL
except Exception:            # 非 Windows 平台：退化为直接阻塞读
    _PeekNamedPipe = None

APP_NAME = "速映 FlashPlay"        # 换图标的同时改名字（图标方案 03_速映）
VERSION = "1.0.0"
CONFIG_NAME = "sy_config.json"

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
# 打包成 exe 后，资源要相对 exe 所在目录找，而不是 PyInstaller 的临时解包目录
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# 配色（VLC 风格：深灰底 + 橙色强调）
# ----------------------------------------------------------------------------
BG          = "#1B1B1D"   # 窗口底色
PANEL       = "#232326"   # 面板
TOOLBAR     = "#2A2A2D"   # 工具条
CARD        = "#2E2E32"   # 卡片/表头
HOVER       = "#3A3A40"   # 悬停
BORDER      = "#3A3A40"
TEXT        = "#E9E9EC"
TEXT_DIM    = "#9A9AA2"
ACCENT      = "#FF8800"   # VLC 橙
ACCENT_DARK = "#CC6D00"
OK_GREEN    = "#4EC9A0"
ERR_RED     = "#F0616D"
WARN_YELLOW = "#E8C547"
INPUT_BG    = "#1F1F22"
SEL_BG      = "#3D3225"   # 选中行（带一点橙）
TRACK_BG    = "#333338"

QUALITIES = [
    ("最佳画质", "best"),
    ("1080P", "1080"),
    ("720P", "720"),
    ("480P", "480"),
    ("仅音频 MP3", "audio"),
]
QUALITY_ARGS = {
    "best":  ["-f", "bv*+ba/b"],
    "1080":  ["-f", "bv*[height<=1080]+ba/b[height<=1080]"],
    "720":   ["-f", "bv*[height<=720]+ba/b[height<=720]"],
    "480":   ["-f", "bv*[height<=480]+ba/b[height<=480]"],
    "audio": ["-x", "--audio-format", "mp3"],
}

# “在线试看”用（输出到管道，不能做转码后处理）
PREVIEW_ARGS = {
    "best": ["-f", "bv*+ba/b"],
    "1080": ["-f", "bv*[height<=1080]+ba/b[height<=1080]"],
    "720":  ["-f", "bv*[height<=720]+ba/b[height<=720]"],
    "480":  ["-f", "bv*[height<=480]+ba/b[height<=480]"],
    "audio": ["-f", "ba/b"],
}

PROGRESS_TAG = "@@PROG@@"
PROGRESS_TPL = (
    "download:" + PROGRESS_TAG +
    "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|"
    "%(progress.eta)s|%(progress.fragment_index)s|%(progress.fragment_count)s"
)

FONT_UI    = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_BOLD  = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")

# 内置播放内核 mpv（首次使用时自动获取；也可手动放到 player/ 目录或设置里指定）
MPV_REPO_API = "https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest"
MPV_ASSET_RE = re.compile(r"^mpv-x86_64-\d{8}-git.*\.7z$")
MIRROR_PREFIXES = ("https://gh-proxy.com/", "https://ghfast.top/", "")


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "-"


def human_speed(n):
    if not n:
        return ""
    return human_size(n) + "/s"


def human_eta(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_duration(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "-"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_clock(seconds):
    """播放器用的时间显示：mm:ss / h:mm:ss。"""
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "--:--"
    if seconds < 0:
        return "--:--"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def pick_thumbnail(meta):
    """从 yt-dlp 的元信息里挑一张大小合适的封面图。"""
    thumbs = [t for t in (meta.get("thumbnails") or [])
              if t.get("url") and "transparent" not in t["url"].lower()]
    if thumbs:
        def score(t):
            w = t.get("width") or 0
            return (0 if 320 <= w <= 900 else 1, abs((w or 480) - 480))
        return sorted(thumbs, key=score)[0]["url"]
    url = meta.get("thumbnail") or ""
    return "" if "transparent" in url.lower() else url


def find_sibling_cover(video_path):
    """找 yt-dlp 随视频一起下载下来的封面文件（与视频同名、扩展名不同）。"""
    if not video_path:
        return ""
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    stem = os.path.splitext(video_path)[0]
    stem = re.sub(r"\.f\d+$", "", stem)        # 去掉 .f100023 这类分片后缀
    for ext in exts:
        cand = stem + ext
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) > 2000:
                return cand
        except OSError:
            continue
    # 再按封面文件里的「[视频ID]」在同一个目录里兜底找一次
    m = re.search(r"\[([0-9A-Za-z_\-]+)\]$", stem)
    if m:
        vid = m.group(1)
        folder = os.path.dirname(video_path) or "."
        try:
            for name in os.listdir(folder):
                if f"[{vid}]" in name and os.path.splitext(name)[1].lower() in exts:
                    cand = os.path.join(folder, name)
                    if os.path.getsize(cand) > 2000:
                        return cand
        except OSError:
            pass
    return ""


def cover_key(video_id, url):
    """封面缓存文件名：跟视频绑定（视频 ID 或链接哈希），不能用任务编号。"""
    key = video_id or hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return re.sub(r"[^0-9A-Za-z_\-]", "_", key)[:64]


def parse_size(text):
    """把 aria2 的 '12MiB' / '1.2GiB' / '0B' 之类转成字节数。"""
    m = re.match(r"([0-9.]+)\s*([KMGTP]?)i?B", (text or "").strip(), re.I)
    if not m:
        return 0.0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
            "T": 1024 ** 4, "P": 1024 ** 5}[m.group(2).upper()]
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return 0.0


def open_in_explorer(path):
    """在文件管理器中定位文件或打开文件夹。"""
    try:
        if not path:
            return
        if IS_WINDOWS:
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(os.path.normpath(path))  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # pragma: no cover
        print("open failed:", exc)


def reveal_file(path):
    try:
        if not path or not os.path.exists(path):
            return False
        if IS_WINDOWS:
            os.startfile(os.path.normpath(path))  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


ASSETS_DIR = os.path.join(SCRIPT_DIR, "assets")


def asset_path(*names):
    """在 assets 目录里找第一个存在的资源文件。"""
    for name in names:
        p = os.path.join(ASSETS_DIR, name)
        if os.path.isfile(p):
            return p
    return ""


def load_photo(path):
    """加载 PNG 为 Tk 图片；失败返回 None。"""
    if not path:
        return None
    try:
        return tk.PhotoImage(file=path)
    except Exception:
        return None


def make_logo_png(size=96):
    """纯标准库生成一张 PNG 图标（橙色圆角方块 + 白色下载箭头）。"""
    cx, cy = size / 2.0, size / 2.0
    r = size * 0.46
    radius = size * 0.22

    def inside_rounded(x, y):
        dx, dy = abs(x - cx), abs(y - cy)
        if dx <= r - radius or dy <= r - radius:
            return dx <= r and dy <= r
        return ((dx - (r - radius)) ** 2 + (dy - (r - radius)) ** 2) <= radius ** 2

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            if inside_rounded(x, y):
                # 竖向渐变：上浅下深
                t = y / float(size)
                col = (255, int(150 + 40 * (1 - t)), int(20 + 30 * (1 - t)), 255)
            else:
                col = (0, 0, 0, 0)
            # 白色下载箭头（竖杆 + 三角 + 底座）
            nx, ny = x / size, y / size
            if 0.455 <= nx <= 0.545 and 0.24 <= ny <= 0.53:
                col = (255, 255, 255, 255)
            if 0.33 <= nx <= 0.67 and 0.50 <= ny <= 0.68:
                half = (0.67 - 0.33) / 2
                mid = 0.5
                prog = (ny - 0.50) / 0.18
                if abs(nx - mid) <= half * (1 - prog):
                    col = (255, 255, 255, 255)
            if 0.28 <= nx <= 0.72 and 0.745 <= ny <= 0.80:
                col = (255, 255, 255, 255)
            row.extend(col)
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return png


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
class Config:
    DEFAULTS = {
        "ytdlp_path": "",          # 空 = 自动查找
        "outdir": "",              # 空 = 同目录 downloads
        "quality": "best",
        "cookies_browser": "不使用",   # 不使用 / Edge / Chrome / Firefox
        "cookies_file": "",        # cookies.txt 文件（优先级高于浏览器读取）
        "proxy": "",
        "player_path": "",         # 空 = 系统默认播放器
        "mpv_path": "",            # 内置播放内核 mpv 的路径，空 = 自动查找/下载
        "aria2c_path": "",         # 磁力下载内核 aria2c 的路径，空 = 自动查找/下载
        "ffmpeg_path": "",         # 合并/转码内核 ffmpeg 的路径，空 = 自动查找/下载
        "last_volume": 100,
        "download_playlist": False,
        "open_folder_when_done": False,
        "retries": 4,
    }

    def __init__(self, path=None):
        self.path = path or os.path.join(SCRIPT_DIR, CONFIG_NAME)
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except Exception:
            pass
        if not self.data.get("outdir"):
            self.data["outdir"] = os.path.join(SCRIPT_DIR, "downloads")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            print("config save failed:", exc)

    def __getitem__(self, k):
        return self.data.get(k, self.DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v


# ----------------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------------
class Task:
    _seq = 0

    def __init__(self, url, quality="best"):
        Task._seq += 1
        self.id = Task._seq
        self.url = url
        self.quality = quality
        self.title = "获取信息中…"
        self.uploader = ""
        self.duration = None
        self.status = "等待中"
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.path = ""
        self.error = ""
        self.proc = None
        self.cancelled = False
        self.attempt = 0
        self.thumb_url = ""       # 视频封面地址（探测信息时拿到）
        self.thumb_png = ""       # 转成 PNG 后的本地缓存路径
        self.thumb_started = False
        self.video_id = ""        # 视频自身 ID（B站 BV 号等），用于封面缓存命名
        self.cover_src = ""       # 随视频一起下载下来的封面文件
        self.notice = ""          # 提示信息（例如：这是试看片段）
        self.kind = "url"         # url = 网址下载；magnet = 磁力下载
        self.title_set = False    # 磁力任务拿到元数据后可以更新标题

    @property
    def display_title(self):
        return self.title or self.url


# ----------------------------------------------------------------------------
# 下载引擎（不依赖 tkinter，可被自检直接调用）
# ----------------------------------------------------------------------------
class Engine:
    FATAL_PATTERNS = (
        "Unsupported URL", "is not a valid URL", "Video unavailable",
        "Private video", "This video is not available", "removed by the uploader",
        "does not exist", "is not a valid URL",
    )

    def __init__(self, cfg, emit=None):
        self.cfg = cfg
        self.emit = emit or (lambda *a, **k: None)
        self._ffmpeg_lock = threading.Lock()
        self._ytdlp_lock = threading.Lock()

    # ---- 定位 yt-dlp --------------------------------------------------------
    def find_ytdlp(self):
        p = self.cfg["ytdlp_path"]
        if p and os.path.isfile(p):
            return p
        for name in ("yt-dlp.exe", "yt-dlp", "yt-dlp_linux", "yt-dlp_macos"):
            cand = os.path.join(SCRIPT_DIR, name)
            if os.path.isfile(cand):
                return cand
        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        return found or ""

    def ytdlp_version(self):
        exe = self.find_ytdlp()
        if not exe:
            return ""
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=20, creationflags=CREATE_NO_WINDOW)
            return (out.stdout or "").strip()
        except Exception:
            return ""

    # ffmpeg 合并/转码内核（首次需要时自动获取；也可手动放到 tools/ffmpeg/ 或 PATH）
    FFMPEG_DIR = os.path.join(SCRIPT_DIR, "tools", "ffmpeg")
    FFMPEG_GZ_URL = ("https://github.com/eugeneware/ffmpeg-static/"
                     "releases/latest/download/ffmpeg-win32-x64.gz")
    FFMPEG_GZ_PROBE = ("https://github.com/eugeneware/ffmpeg-static/"
                       "releases/latest/download/ffprobe-win32-x64.gz")
    FFMPEG_ZIP_BTBN = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
                       "latest/ffmpeg-master-latest-win64-gpl.zip")
    FFMPEG_ZIP_GYAN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

    def find_ffmpeg(self):
        p = self.cfg["ffmpeg_path"]
        if p and os.path.isfile(p):
            return p
        for cand in (os.path.join(self.FFMPEG_DIR, "ffmpeg.exe"),
                     os.path.join(SCRIPT_DIR, "ffmpeg.exe"),
                     os.path.join(SCRIPT_DIR, "tools", "ffmpeg.exe")):
            if os.path.isfile(cand):
                return cand
        return shutil.which("ffmpeg") or ""

    def find_ffprobe(self):
        ff = self.find_ffmpeg()
        if ff:
            cand = os.path.join(os.path.dirname(ff), "ffprobe.exe")
            if os.path.isfile(cand):
                return cand
        return shutil.which("ffprobe") or ""

    def has_ffmpeg(self):
        return bool(self.find_ffmpeg())

    def ffmpeg_version(self):
        exe = self.find_ffmpeg()
        if not exe:
            return ""
        try:
            res = subprocess.run([exe, "-version"], capture_output=True, text=True,
                                 timeout=30, creationflags=CREATE_NO_WINDOW)
            first = (res.stdout or "").splitlines()
            return first[0].strip() if first else ""
        except Exception:
            return ""

    def find_player(self):
        """自定义播放器（VLC/PotPlayer 等）；返回空串表示用系统默认关联程序。"""
        p = self.cfg["player_path"]
        if p and os.path.isfile(p):
            return p
        return ""

    def find_ffplay(self):
        return shutil.which("ffplay") or ""

    # ---- 封面（缩略图）-----------------------------------------------------
    THUMB_DIR = os.path.join(SCRIPT_DIR, "thumb_cache")
    THUMB_W, THUMB_H = 260, 130

    def fetch_thumbnail(self, url, key, width=None, height=None, page_url=""):
        """下载封面图并裁成固定尺寸的 PNG（Tk 只能直接显示 PNG/GIF，所以用 ffmpeg 转）。

        部分网站（如 B 站部分视频）只给出 1x1 透明占位图，这时回退到网页的 og:image。
        """
        import urllib.request
        width = width or self.THUMB_W
        height = height or self.THUMB_H
        os.makedirs(self.THUMB_DIR, exist_ok=True)
        png = os.path.join(self.THUMB_DIR, f"{key}.png")
        if os.path.isfile(png) and os.path.getsize(png) > 0:
            return png

        def grab(link):
            """下载图片，返回 (临时文件, 字节数)；失败返回 ("", 0)。"""
            if not link:
                return "", 0
            tmp = os.path.join(self.THUMB_DIR, f"{key}.img")
            try:
                req = urllib.request.Request(
                    link, headers={"User-Agent": "Mozilla/5.0", "Referer": page_url or link})
                with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
                    fh.write(resp.read())
                return tmp, os.path.getsize(tmp)
            except Exception:
                return "", 0

        raw, size = grab(url)
        if size < 2000 and page_url:              # 占位图或下载失败 → 网页 og:image 兜底
            alt = self._og_image(page_url)
            if alt and alt != url:
                raw2, size2 = grab(alt)
                if size2 > size:
                    raw, size = raw2, size2
        if size < 2000:
            return ""
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            return ""
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height}")
        try:
            res = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", raw, "-vf", vf, png],
                                 capture_output=True, creationflags=CREATE_NO_WINDOW,
                                 timeout=90)
        except Exception:
            res = None
        finally:
            try:
                os.remove(raw)
            except OSError:
                pass
        if res is not None and res.returncode == 0 and os.path.isfile(png):
            return png
        return ""

    def _og_image(self, page_url):
        """从网页 HTML 里找 og:image / B 站 pic 字段作为封面兜底。"""
        import urllib.request
        if not page_url:
            return ""
        try:
            req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                html = resp.read(500000).decode("utf-8", "ignore")
        except Exception:
            return ""
        patterns = (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
            r'"pic"\s*:\s*"(//[^"]+)"',
            r'"cover"\s*:\s*"(//[^"]+)"',
        )
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                link = m.group(1)
                if link.startswith("//"):
                    link = "https:" + link
                if "transparent" not in link.lower():
                    return link
        return ""

    def convert_cover(self, src, key, width=None, height=None):
        """把任意图片裁成界面用的固定尺寸 PNG；失败返回空串。"""
        if not src or not os.path.isfile(src):
            return ""
        width = width or self.THUMB_W
        height = height or self.THUMB_H
        os.makedirs(self.THUMB_DIR, exist_ok=True)
        png = os.path.join(self.THUMB_DIR, f"{key}.png")
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            return ""
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height}")
        try:
            res = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", src, "-vf", vf, png],
                                 capture_output=True, creationflags=CREATE_NO_WINDOW,
                                 timeout=90)
        except Exception:
            return ""
        if res.returncode == 0 and os.path.isfile(png) and os.path.getsize(png) > 2000:
            return png
        return ""

    def adopt_cover(self, task):
        """下载完成后：把随视频一起下载下来的封面转成界面用的 PNG。"""
        src = getattr(task, "cover_src", "") or find_sibling_cover(task.path)
        if not src:
            return ""
        key = cover_key(task.video_id, task.url) + "_dl"
        return self.convert_cover(src, key, self.THUMB_W, self.THUMB_H)

    def probe_duration(self, path):
        """用 ffprobe 读本地文件时长（秒）。"""
        ffprobe = self.find_ffprobe()
        if not ffprobe or not path or not os.path.isfile(path):
            return 0.0
        try:
            res = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60)
            return float((res.stdout or "").strip().splitlines()[0])
        except Exception:
            return 0.0

    def check_preview_segment(self, task):
        """检测“试看片段”：本地文件明显短于视频总时长时给出提示（不掩盖、不绕过）。"""
        try:
            file_dur = self.probe_duration(task.path)
            meta_dur = float(task.duration or 0)
            if file_dur and meta_dur and file_dur + 60 < meta_dur * 0.9:
                task.notice = (
                    f"这是试看片段：完整版 {fmt_clock(meta_dur)}，本地只有 {fmt_clock(file_dur)}。"
                    f"完整内容属于付费/充电专属，需要提供已购买该内容的账号 cookies 才能下载"
                    f"（设置 → cookies.txt 文件）。本程序不会绕过平台的付费认证。")
                return task.notice
        except Exception:
            pass
        return ""

    # ---- 磁力 / BT 下载（aria2c 内核）--------------------------------------
    ARIA_DIR = os.path.join(SCRIPT_DIR, "tools", "aria2")
    ARIA_URL = ("https://github.com/aria2/aria2/releases/download/release-1.37.0/"
                "aria2-1.37.0-win-64bit-build1.zip")
    # 国内网络下 DHT 的 UDP 常被墙，靠 tracker 找节点更稳；下完不做种（seed-time=0）
    DEFAULT_TRACKERS = (
        "udp://tracker.opentrackr.org:1337/announce,"
        "udp://open.tracker.cl:1337/announce,"
        "udp://tracker.openbittorrent.com:6969/announce,"
        "udp://exodus.desync.com:6969/announce,"
        "udp://tracker.torrent.eu.org:451/announce,"
        "udp://tracker.dler.org:6969/announce,"
        "http://bttracker.debian.org:6969/announce"
    )
    ARIA_LINE_RE = re.compile(
        r"\[#(\w+)\s+([0-9.]+[KMGTP]?i?B)/([0-9.]+[KMGTP]?i?B)(?:\((\d+)%\))?"
        r"\s+CN:(\d+)\s+(?:SD:(\d+)\s+)?DL:([0-9.]+[KMGTP]?i?B)")
    ARIA_META_RE = re.compile(r"\[MEMORY\]\[METADATA\](.+)")

    def find_aria2c(self):
        p = self.cfg["aria2c_path"]
        if p and os.path.isfile(p):
            return p
        for cand in (os.path.join(self.ARIA_DIR, "aria2c.exe"),
                     os.path.join(SCRIPT_DIR, "aria2c.exe"),
                     os.path.join(SCRIPT_DIR, "tools", "aria2c.exe")):
            if os.path.isfile(cand):
                return cand
        return shutil.which("aria2c") or ""

    def aria2c_version(self):
        exe = self.find_aria2c()
        if not exe:
            return ""
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=20, creationflags=CREATE_NO_WINDOW)
            lines = (out.stdout or "").splitlines()
            return lines[0].strip() if lines else ""
        except Exception:
            return ""

    def download_aria2c(self):
        """首次使用时自动获取 aria2c（GitHub 镜像 + 系统自带 bsdtar 解压）。"""
        os.makedirs(self.ARIA_DIR, exist_ok=True)
        tmp = os.path.join(self.ARIA_DIR, "aria2.zip")
        last_err = ""
        for prefix in MIRROR_PREFIXES:
            try:
                self.emit("log", f"下载 BT 内核 aria2c（约 2.4MB，来源：{prefix or '直连 GitHub'}）…")
                self._download(prefix + self.ARIA_URL, tmp)
                break
            except Exception as exc:
                last_err = str(exc)
                self.emit("log", f"aria2c 下载失败（{prefix or '直连'}）：{exc}", "warn")
        else:
            raise RuntimeError(f"aria2c 下载失败：{last_err}")
        for tool in (r"C:\Windows\System32\tar.exe", "tar", "unzip"):
            exe = tool if os.path.isabs(tool) else shutil.which(tool)
            if exe and os.path.isfile(exe):
                try:
                    subprocess.run([exe, "-xf", tmp, "-C", self.ARIA_DIR],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW,
                                   timeout=180)
                except Exception:
                    continue
        for root, _dirs, files in os.walk(self.ARIA_DIR):
            if "aria2c.exe" in files:
                src = os.path.join(root, "aria2c.exe")
                dst = os.path.join(self.ARIA_DIR, "aria2c.exe")
                if os.path.normcase(src) != os.path.normcase(dst):
                    shutil.copy2(src, dst)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self.emit("log", f"BT 内核就绪：{dst}", "ok")
                return dst
        raise RuntimeError("解压后没有找到 aria2c.exe")

    def _download(self, url, dest, label=""):
        """流式下载（带进度日志），供内核获取/更新使用。"""
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "FlashPlay"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got, step = 0, 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct >= step + 20:
                        step = pct - (pct % 20)
                        self.emit("log", f"{(label or '下载')} {pct}%")

    def _verify_ffmpeg(self, exe):
        """确认新获取的 ffmpeg 可以运行。"""
        try:
            res = subprocess.run([exe, "-version"], capture_output=True, text=True,
                                 timeout=60, creationflags=CREATE_NO_WINDOW)
            return res.returncode == 0 and "ffmpeg version" in (res.stdout or "").lower()
        except Exception:
            return False

    def download_ffmpeg(self):
        """首次需要时自动获取 ffmpeg（合并音视频、转封面用）。返回 ffmpeg.exe 路径。"""
        import gzip as _gzip
        dest_dir = self.FFMPEG_DIR
        os.makedirs(dest_dir, exist_ok=True)
        ffmpeg_dst = os.path.join(dest_dir, "ffmpeg.exe")
        probe_dst = os.path.join(dest_dir, "ffprobe.exe")
        last_err = ""

        # ① 官方静态构建的单文件 gz（最小，约 30MB）
        for prefix in MIRROR_PREFIXES:
            label = prefix or "直连 GitHub"
            tmp = ffmpeg_dst + ".gz"
            try:
                self.emit("log", f"下载 ffmpeg 合并内核（约 30MB，来源：{label}）…")
                self._download(prefix + self.FFMPEG_GZ_URL, tmp, label="ffmpeg")
                self.emit("log", "下载完成，正在解压…")
                with _gzip.open(tmp, "rb") as src, open(ffmpeg_dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                try:      # ffprobe 一并获取（可选，失败不影响）
                    self._download(prefix + self.FFMPEG_GZ_PROBE, probe_dst + ".gz", label="ffprobe")
                    with _gzip.open(probe_dst + ".gz", "rb") as src, open(probe_dst, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    os.remove(probe_dst + ".gz")
                except Exception:
                    pass
                if self._verify_ffmpeg(ffmpeg_dst):
                    self.emit("log", f"合并内核就绪：{ffmpeg_dst}", "ok")
                    return ffmpeg_dst
                last_err = f"{label}: 解压后的 ffmpeg 无法运行"
                self.emit("log", f"ffmpeg 校验失败（{label}），尝试其它来源…", "warn")
            except Exception as exc:
                last_err = f"{label}: {exc}"
                self.emit("log", f"ffmpeg 下载失败（{label}）：{exc}", "warn")

        # ②③ 整包 zip（BtbN / gyan.dev），解出 bin/ffmpeg.exe
        for url, label in ((self.FFMPEG_ZIP_BTBN, "BtbN 构建"),
                           (self.FFMPEG_ZIP_GYAN, "gyan.dev")):
            prefixes = MIRROR_PREFIXES if "github" in url else ("",)
            for prefix in prefixes:
                title = (prefix.split("/")[2] if prefix else "直连") + " · " + label
                tmp = os.path.join(dest_dir, "ffmpeg.zip")
                try:
                    self.emit("log", f"下载 ffmpeg 整包（40-90MB，来源：{title}）…")
                    self._download(prefix + url, tmp, label="ffmpeg")
                    self._unzip_into(tmp, dest_dir)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    cand = candp = ""
                    for root, _dirs, files in os.walk(dest_dir):
                        if not cand and "ffmpeg.exe" in files:
                            cand = os.path.join(root, "ffmpeg.exe")
                        if not candp and "ffprobe.exe" in files:
                            candp = os.path.join(root, "ffprobe.exe")
                        if cand and candp:
                            break
                    if cand and os.path.normcase(cand) != os.path.normcase(ffmpeg_dst):
                        shutil.copy2(cand, ffmpeg_dst)
                    if candp and os.path.normcase(candp) != os.path.normcase(probe_dst):
                        shutil.copy2(candp, probe_dst)
                    if self._verify_ffmpeg(ffmpeg_dst):
                        self.emit("log", f"合并内核就绪：{ffmpeg_dst}", "ok")
                        return ffmpeg_dst
                    last_err = f"{title}: 解压后无法运行"
                except Exception as exc:
                    last_err = f"{title}: {exc}"
                    self.emit("log", f"ffmpeg 整包失败（{title}）：{exc}", "warn")

        raise RuntimeError(f"ffmpeg 获取失败：{last_err}")

    def _unzip_into(self, archive, dest):
        """用系统自带 tar/unzip 解开 zip 包。"""
        for tool in (r"C:\Windows\System32\tar.exe", "tar", "unzip"):
            exe = tool if os.path.isabs(tool) else shutil.which(tool)
            if exe and os.path.isfile(exe):
                try:
                    res = subprocess.run([exe, "-xf", archive, "-C", dest],
                                         capture_output=True, creationflags=CREATE_NO_WINDOW,
                                         timeout=600)
                    if res.returncode == 0:
                        return True
                except Exception:
                    continue
        raise RuntimeError("解压失败（需要 tar 或 unzip）")

    def ensure_ffmpeg_quiet(self):
        """工作线程里静默确保 ffmpeg 可用（失败不阻塞下载）。"""
        if self.find_ffmpeg():
            return True
        with self._ffmpeg_lock:
            if self.find_ffmpeg():
                return True
            try:
                self.download_ffmpeg()
                return True
            except Exception as exc:
                self.emit("log", f"ffmpeg 自动获取失败：{exc}（高清可能无法合并音轨）", "warn")
                return False

    # ---- yt-dlp 内核自更新 --------------------------------------------------
    YTDLP_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
    YTDLP_EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"

    def _fetch_ytdlp(self, dest):
        """镜像循环下载 yt-dlp.exe 到 dest，做官方哈希校验 + 运行验证。返回版本号。"""
        sums = dest + ".sha256"
        last_err = ""
        ok = False
        for prefix in MIRROR_PREFIXES:
            label = prefix or "直连 GitHub"
            try:
                self.emit("log", f"下载 yt-dlp 内核（约 18MB，来源：{label}）…")
                self._download(prefix + self.YTDLP_EXE_URL, dest, label="yt-dlp")
                ok = True
                try:
                    self._download(prefix + self.YTDLP_SUMS_URL, sums, label="校验和")
                except Exception:
                    sums = ""
                break
            except Exception as exc:
                last_err = f"{label}: {exc}"
                self.emit("log", f"下载失败（{label}）：{exc}", "warn")
        if not ok:
            raise RuntimeError(f"下载失败：{last_err}")
        if sums and os.path.isfile(sums):
            want = ""
            try:
                with open(sums, "r", encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == "yt-dlp.exe":
                            want = parts[0].strip()
                            break
            except Exception:
                want = ""
            try:
                os.remove(sums)
            except OSError:
                pass
            if want:
                h = hashlib.sha256()
                with open(dest, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest().lower() != want.lower():
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    raise RuntimeError("官方哈希校验不通过，已放弃（镜像可能不稳定，稍后再试）")
                self.emit("log", "官方 SHA256 校验通过")
        ver = ""
        try:
            res = subprocess.run([dest, "--version"], capture_output=True, text=True,
                                 timeout=60, creationflags=CREATE_NO_WINDOW)
            ver = (res.stdout or "").strip()
        except Exception:
            ver = ""
        if not ver:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise RuntimeError("下载的文件无法运行，已放弃")
        return ver

    def download_ytdlp(self):
        """首次使用时自动获取 yt-dlp.exe（放到程序目录）。返回路径。"""
        dst = os.path.join(SCRIPT_DIR, "yt-dlp.exe")
        ver = self._fetch_ytdlp(dst)
        self.emit("log", f"下载内核就绪：{dst}（{ver}）", "ok")
        return dst

    def ensure_ytdlp_quiet(self):
        """工作线程里静默确保 yt-dlp 可用（首次使用时自动获取）。返回路径或空串。"""
        found = self.find_ytdlp()
        if found:
            return found
        with self._ytdlp_lock:
            found = self.find_ytdlp()
            if found:
                return found
            try:
                return self.download_ytdlp()
            except Exception as exc:
                self.emit("log", f"yt-dlp 自动获取失败：{exc}", "warn")
                return ""

    def latest_ytdlp(self):
        """查询 yt-dlp 最新版本号（网络不通时抛异常）。"""
        import urllib.request
        req = urllib.request.Request(self.YTDLP_API, headers={"User-Agent": "FlashPlay"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("tag_name") or "").strip()

    def update_ytdlp(self, force=False):
        """下载最新 yt-dlp.exe → 官方哈希校验 → 验证可运行 → 替换（旧版备份 .bak）。"""
        exe = self.find_ytdlp()
        cur = self.ytdlp_version() if exe else ""
        tag = self.latest_ytdlp()
        if not force and tag and cur and tag == cur:
            return cur
        if not exe:                     # 本机还没有 → 直接获取安装
            self.download_ytdlp()
            return self.ytdlp_version()
        dest_dir = os.path.dirname(exe) or SCRIPT_DIR
        new_exe = os.path.join(dest_dir, "yt-dlp.exe.new")
        ver = self._fetch_ytdlp(new_exe)
        self.emit("log", f"下载完成（{ver}），正在替换…")

        # 替换（旧版先备份，失败自动回滚）
        bak = exe + ".bak"
        try:
            if os.path.isfile(bak):
                os.remove(bak)
            os.replace(exe, bak)
            try:
                os.replace(new_exe, exe)
            except OSError:
                os.replace(bak, exe)
                raise
        except OSError as exc:
            try:
                os.remove(new_exe)
            except OSError:
                pass
            raise RuntimeError(f"替换 yt-dlp.exe 失败（可能正被占用，稍后再试）：{exc}")
        self.emit("log", f"yt-dlp 已更新：{cur or '旧版'} → {ver}（旧版备份为 yt-dlp.exe.bak）", "ok")
        return ver

    def download_magnet(self, task, on_progress=None):
        """用 aria2c 下载磁力链接（P2P）。下完不做种，支持断点续传。"""
        exe = self.find_aria2c()
        if not exe:
            return False, "找不到 BT 内核 aria2c"
        outdir = self.cfg["outdir"]
        os.makedirs(outdir, exist_ok=True)
        task.attempt += 1
        task.status = "连接节点"
        task.error = ""
        self.emit("log", f"开始磁力下载（第 {task.attempt} 次）：{task.display_title}")
        self.emit("task", task)
        started = time.time()

        cmd = [exe, "--dir", outdir, "--seed-time=0", "--continue=true",
               "--summary-interval=1", "--console-log-level=warn",
               "--file-allocation=none", "--bt-save-metadata=true",
               "--enable-dht=true", "--bt-enable-lpd=true",
               "--max-connection-per-server=16", "--split=16",
               "--min-split-size=1M", "--follow-torrent=mem",
               "--bt-tracker=" + self.DEFAULT_TRACKERS, task.url]
        try:
            task.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW, env=self.env())
        except Exception as exc:
            return False, f"启动 aria2c 失败：{exc}"

        last_err = ""
        for raw in task.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if task.cancelled:
                break
            m = self.ARIA_META_RE.search(line)
            if m and not getattr(task, "title_set", False):
                name = m.group(1).strip()
                if name:
                    task.title = name
                    task.title_set = True
                    self.emit("task", task)
                continue
            m = self.ARIA_LINE_RE.search(line)
            if m:
                total = parse_size(m.group(3))
                done = parse_size(m.group(2))
                peers = int(m.group(5))
                speed = parse_size(m.group(7))
                if total > 0:
                    task.percent = min(100.0, done * 100.0 / total)
                    task.status = "下载中"
                elif peers > 0:
                    task.status = "获取元数据"
                task.speed = speed or None
                self.emit("progress", task)
                if on_progress:
                    on_progress(task)
                continue
            if line.startswith("[ERROR]"):
                last_err = line
                self.emit("log", line, "err")

        rc = task.proc.wait()
        task.proc = None
        if task.cancelled:
            task.status = "已取消"
            self.emit("task", task)
            return False, "已取消"
        if rc == 0:
            if not (task.path and os.path.exists(task.path)):
                task.path = self._newest_entry(outdir, since=started)
            task.percent = 100.0
            task.status = "已完成"
            task.error = ""
            self.emit("task", task)
            self.emit("log", f"磁力下载完成：{task.path or task.title}", "ok")
            return True, task.path
        task.status = "失败"
        task.error = last_err or f"aria2c 退出码 {rc}"
        self.emit("task", task)
        return False, task.error

    @staticmethod
    def _newest_entry(folder, since=None):
        """找目录里最新出现的文件或文件夹（BT 多文件种子用文件夹兜底）。"""
        best, best_m = "", 0.0
        try:
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if since and m < since - 10:
                    continue
                if m > best_m:
                    best, best_m = p, m
        except Exception:
            return ""
        return best

    # ---- 命令拼装 -----------------------------------------------------------
    def base_cmd(self):
        exe = self.find_ytdlp()
        cmd = [exe, "--no-color", "--newline", "--no-warnings",
               "--encoding", "utf-8",
               "--retries", "5", "--fragment-retries", "5",
               "--socket-timeout", "20"]
        cookie_file = self.cfg["cookies_file"]
        if cookie_file and os.path.isfile(cookie_file):
            cmd += ["--cookies", cookie_file]          # 导出好的 cookies.txt 最稳定
        else:
            cookies = self.cfg["cookies_browser"]
            if cookies and cookies != "不使用":
                cmd += ["--cookies-from-browser", cookies.lower()]
        ff = self.find_ffmpeg()
        if ff and os.path.dirname(ff):
            path_dir = os.path.normcase(os.path.dirname(ff))
            which_dir = os.path.normcase(os.path.dirname(shutil.which("ffmpeg") or ""))
            if not which_dir or path_dir != which_dir:
                cmd += ["--ffmpeg-location", os.path.dirname(ff)]
        if self.cfg["proxy"]:
            cmd += ["--proxy", self.cfg["proxy"]]
        if not self.cfg["download_playlist"]:
            cmd += ["--no-playlist"]
        return cmd

    def env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    # ---- 元信息 -------------------------------------------------------------
    def probe(self, url):
        cmd = self.base_cmd() + ["-J", "--no-check-formats", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=90, env=self.env(),
                                 creationflags=CREATE_NO_WINDOW)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout)
        except Exception as exc:
            self.emit("log", f"获取信息失败：{exc}")
        return {}

    # ---- 下载 ---------------------------------------------------------------
    def download(self, task, on_progress=None):
        """阻塞式下载。返回 (success, message)。"""
        exe = self.find_ytdlp()
        if not exe:                     # 首次使用：自动获取下载内核（静默，失败才报错）
            task.status = "准备内核"
            self.emit("task", task)
            exe = self.ensure_ytdlp_quiet()
        if not exe:
            return False, ("无法获取下载内核 yt-dlp（网络不通？）——请稍后重试，"
                           "或手动把 yt-dlp.exe 放到本程序同目录")

        if not self.find_ffmpeg():      # 首次下载时自动获取合并内核（静默，失败不阻塞）
            self.ensure_ffmpeg_quiet()

        started = time.time()
        meta = self.probe(task.url)
        if meta:
            if meta.get("_type") == "playlist":
                entries = meta.get("entries") or []
                task.title = meta.get("title") or task.url
                task.uploader = meta.get("uploader") or ""
                task.duration = meta.get("duration")
                self.emit("log", f"播放列表：{task.title}（{len(entries)} 个视频）")
            else:
                task.title = meta.get("title") or task.url
                task.uploader = meta.get("uploader") or ""
                task.duration = meta.get("duration")
                task.video_id = str(meta.get("id") or "")
                task.thumb_url = pick_thumbnail(meta)
            task.percent = 0.0
            self.emit("task", task)
        else:
            # 拿不到元信息（网络风控等）时，标题退回链接本身
            if task.title == "获取信息中…":
                task.title = task.url
                self.emit("task", task)

        outdir = self.cfg["outdir"]
        os.makedirs(outdir, exist_ok=True)
        cmd = self.base_cmd()
        cmd += QUALITY_ARGS.get(task.quality, QUALITY_ARGS["best"])
        cmd += ["--write-thumbnail", "--convert-thumbnails", "png"]   # 视频的封面一起下下来
        cmd += ["-o", os.path.join(outdir, "%(title)s [%(id)s].%(ext)s"),
                "--progress-template", PROGRESS_TPL,
                "--continue", url_clean(task.url)]

        task.attempt += 1
        task.status = "下载中"
        task.error = ""
        self.emit("log", f"开始下载（第 {task.attempt} 次）：{task.display_title}")
        self.emit("task", task)

        try:
            task.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=self.env(), creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return False, f"启动 yt-dlp 失败：{exc}"

        last_err = ""
        err_lines = []
        dest_re = re.compile(r"\[download\] Destination: (.+)")
        merger_re = re.compile(r"\[Merger\] Merging formats into \"(.+)\"")
        exist_re = re.compile(r"\[download\] (.+) has already been downloaded")
        audio_re = re.compile(r"\[ExtractAudio\] Destination: (.+)")

        for raw_line in task.proc.stdout:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            if task.cancelled:
                break
            if PROGRESS_TAG in line:
                payload = line.split(PROGRESS_TAG, 1)[1]
                self._parse_progress(task, payload, on_progress)
                continue
            m = dest_re.search(line)
            if m:
                task.path = m.group(1).strip()
                continue
            m = merger_re.search(line)
            if m:
                task.path = m.group(1).strip()
                task.status = "合并中"
                self.emit("task", task)
                continue
            m = audio_re.search(line)
            if m:
                task.path = m.group(1).strip()
                continue
            m = exist_re.search(line)
            if m:
                task.path = m.group(1).strip()
                task.status = "已存在"
                self.emit("task", task)
                continue
            if "[ExtractAudio]" in line:
                task.status = "转换音频"
                self.emit("task", task)
                continue
            if line.startswith("ERROR:"):
                last_err = line[6:].strip()
                err_lines.append(line[6:].strip() or line.strip())
                continue
            if line.startswith("WARNING:"):
                continue
            self.emit("log", line)

        rc = task.proc.wait()
        task.proc = None

        if task.cancelled:
            task.status = "已取消"
            self.emit("task", task)
            return False, "已取消"

        if rc == 0:
            if not (task.path and os.path.exists(task.path)):
                task.path = self._newest_file(outdir, since=started) or task.path
            # 优先采用随视频一起下载的封面（和视频严格一一对应）
            try:
                sibling = find_sibling_cover(task.path)
                if sibling:
                    task.cover_src = sibling
            except Exception:
                pass
            # 如果是试看片段，如实提示（付费内容不绕过，只说明原因）
            try:
                notice = self.check_preview_segment(task)
                if notice:
                    self.emit("log", "⚠ " + notice, "warn")
            except Exception:
                pass
            task.percent = 100.0
            task.status = "已完成"
            task.error = ""      # 重试成功后清掉上一次的报错，避免“已完成 + 错误”自相矛盾
            self.emit("task", task)
            self.emit("log", f"完成：{task.path or task.display_title}", "ok")
            return True, task.path

        task.status = "失败"
        task.error = last_err or f"yt-dlp 退出码 {rc}"
        for msg in err_lines[-3:]:
            self.emit("log", f"ERROR: {msg}", "err")
        self.emit("task", task)
        return False, task.error

    def is_fatal_error(self, message):
        """这类错误重试无意义（地址不对/视频不可用），直接放弃。"""
        return any(p.lower() in (message or "").lower() for p in self.FATAL_PATTERNS)

    def _parse_progress(self, task, payload, on_progress=None):
        parts = payload.split("|")
        if len(parts) < 7:
            return
        got, total, est, speed, eta, fidx, fcnt = parts[:7]

        def num(v):
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        got_b = num(got)
        total_b = num(total) or num(est)
        task.speed = num(speed)
        task.eta = num(eta)
        if total_b:
            task.percent = min(100.0, (got_b or 0) * 100.0 / total_b)
        elif num(fidx) and num(fcnt):
            task.percent = min(100.0, (num(fidx) or 0) * 100.0 / (num(fcnt) or 1))
        self.emit("progress", task)
        if on_progress:
            on_progress(task)

    @staticmethod
    def _newest_file(folder, since=None):
        """兜底：输出编码异常时按时间找出刚下载完成的文件。"""
        try:
            best, best_m = "", 0.0
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                if not os.path.isfile(p):
                    continue
                m = os.path.getmtime(p)
                if since and m < since - 10:
                    continue
                if m > best_m:
                    best, best_m = p, m
            return best
        except Exception:
            return ""

    def cancel(self, task):
        task.cancelled = True
        if task.proc:
            try:
                task.proc.terminate()
            except Exception:
                pass


def url_clean(u):
    """去掉首尾空白与不可见字符。"""
    return u.strip().strip("\u200b").strip()


# ----------------------------------------------------------------------------
# 内置播放器（mpv 内核 + JSON IPC 控制）
# ----------------------------------------------------------------------------
class MpvPlayer:
    """把 mpv 嵌入到 Tk 窗口里播放，通过命名管道 IPC 控制播放/暂停/跳转/音量。

    为什么用 mpv：它是一个独立的播放内核（自带解码器、音视频同步、字幕），
    支持 `--wid` 直接嵌进任意窗口，不需要用户自己装播放器；
    通过 `--input-ipc-server` 还能让 GUI 完全接管控制。
    """

    PIPE_NAME = rf"\\.\pipe\sy_mpv_{os.getpid()}"   # 每个进程独立，避免多开冲突

    def __init__(self, cfg, emit=None):
        self.cfg = cfg
        self.emit = emit or (lambda *a, **k: None)
        self.proc = None
        self.producer = None          # 在线播放时给 mpv 喂流的 yt-dlp 进程
        self.pipe = None
        self.pipe_w = None            # 独立的写句柄（同步管道下读写同句柄会互相阻塞）
        self.reader = None
        self.lock = threading.Lock()
        self.state = {"time": 0.0, "duration": 0.0, "pause": False, "volume": 100}
        self.playing = False
        self.is_stream = False

    # ---- 定位播放内核 ------------------------------------------------------
    def find_mpv(self):
        p = self.cfg["mpv_path"]
        if p and os.path.isfile(p):
            return p
        for cand in (os.path.join(SCRIPT_DIR, "player", "mpv.exe"),
                     os.path.join(SCRIPT_DIR, "mpv.exe"),
                     os.path.join(SCRIPT_DIR, "player", "mpv"),
                     os.path.join(os.path.dirname(SCRIPT_DIR), "player", "mpv.exe")):
            if os.path.isfile(cand):
                return cand
        return shutil.which("mpv") or ""

    def download_mpv(self):
        """首次使用时自动获取 mpv（GitHub 镜像加速 + 系统自带 bsdtar 解压）。"""
        import urllib.request
        self.emit("log", "正在查询播放内核 mpv 的最新版本…")
        try:
            with urllib.request.urlopen(MPV_REPO_API, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"查询 mpv 版本失败：{exc}")

        asset = ""
        for a in data.get("assets", []):
            name = a.get("name", "")
            if MPV_ASSET_RE.match(name) and "-v3-" not in name:
                asset = name
                break
        if not asset:
            raise RuntimeError("没有找到适合的 mpv 安装包")

        url = f"https://github.com/zhongfly/mpv-winbuild/releases/latest/download/{asset}"
        dest = os.path.join(SCRIPT_DIR, "player")
        os.makedirs(dest, exist_ok=True)
        tmp = os.path.join(dest, "mpv_download.7z")

        last_err = ""
        for prefix in MIRROR_PREFIXES:
            label = prefix or "直连 GitHub"
            try:
                self.emit("log", f"开始下载播放内核 mpv（约 32MB，来源：{label}）…")
                self._download(prefix + url, tmp)
                break
            except Exception as exc:
                last_err = str(exc)
                self.emit("log", f"下载失败（{label}）：{exc}", "warn")
        else:
            raise RuntimeError(f"mpv 下载失败：{last_err}")

        self.emit("log", "下载完成，正在解压…")
        self._extract_7z(tmp, dest)
        try:
            os.remove(tmp)
        except OSError:
            pass
        found = self.find_mpv()
        if not found:
            raise RuntimeError("解压完成但没有找到 mpv.exe")
        self.emit("log", f"播放内核就绪：{found}", "ok")
        return found

    def _download(self, url, dest):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "FlashPlay"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    self.emit("progress_mpv", f"{got * 100 // total}%")

    def _extract_7z(self, archive, dest):
        candidates = [r"C:\Windows\System32\tar.exe", "tar", "7z", "7za"]
        for tool in candidates:
            exe = tool if os.path.isabs(tool) else shutil.which(tool)
            if not exe or not os.path.isfile(exe):
                continue
            try:
                res = subprocess.run([exe, "-xf", archive, "-C", dest],
                                     capture_output=True, text=True,
                                     creationflags=CREATE_NO_WINDOW, timeout=300)
                if res.returncode == 0:
                    return
            except Exception:
                continue
        raise RuntimeError("解压失败：请安装 7-Zip 或手动把 mpv.exe 放到 player/ 目录")

    # ---- 播放控制 ----------------------------------------------------------
    def play_file(self, path, hwnd):
        return self._start(hwnd, [path], stream=False)

    def play_stream(self, url, hwnd, ytdlp_exe, quality, env):
        fmt = PREVIEW_ARGS.get(quality, PREVIEW_ARGS["best"])
        extra = []
        cookie_file = self.cfg["cookies_file"]
        if cookie_file and os.path.isfile(cookie_file):
            extra += ["--cookies", cookie_file]
        elif self.cfg["cookies_browser"] not in ("", "不使用"):
            extra += ["--cookies-from-browser", self.cfg["cookies_browser"].lower()]
        if self.cfg["proxy"]:
            extra += ["--proxy", self.cfg["proxy"]]
        cmd = ([ytdlp_exe, "--no-color", "--newline", "--no-warnings", "--quiet",
                "--no-playlist", "--retries", "5", "--socket-timeout", "20",
                "--encoding", "utf-8"] + extra + fmt + ["-o", "-", url])
        return self._start(hwnd, ["-"], stream=True, producer_cmd=cmd, env=env)

    def _start(self, hwnd, source, stream=False, producer_cmd=None, env=None):
        self.stop()
        mpv = self.find_mpv()
        if not mpv:
            raise RuntimeError("播放内核缺失")
        args = [mpv, f"--wid={hwnd}", "--no-terminal", "--no-border", "--osc=no",
                "--force-window=yes", "--keep-open=no", "--auto-window-resize=no",
                f"--input-ipc-server={self.PIPE_NAME}",
                f"--volume={int(self.cfg['last_volume'] or 100)}",
                "--cache=yes", "--demuxer-max-bytes=150MiB",
                "--sub-auto=fuzzy",              # 自动加载同名/相似的字幕文件
                "--no-input-default-bindings",   # 键盘交给 GUI，避免 mpv 抢键（q 退出等）
                "--osd-bar=no", "--no-osc"]
        self.is_stream = stream
        try:
            if stream and producer_cmd:
                self.producer = subprocess.Popen(
                    producer_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW, env=env)
                self.proc = subprocess.Popen(args + source, stdin=self.producer.stdout,
                                             creationflags=CREATE_NO_WINDOW)
            else:
                self.proc = subprocess.Popen(args + source, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            raise RuntimeError(f"启动播放内核失败：{exc}")
        self.playing = True
        self.state.update({"time": 0.0, "duration": 0.0, "pause": False})
        self._connect_ipc()
        return True

    def _connect_ipc(self):
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return
            try:
                self.pipe = open(self.PIPE_NAME, "r+b", buffering=0)
                self.pipe_w = self.pipe      # mpv 只接受一个连接；读写互斥靠 PeekNamedPipe + 锁
                break
            except OSError:
                time.sleep(0.2)
        if not self.pipe:
            self.emit("log", "播放控制通道未建立（仍可播放，但无法用界面按钮控制）", "warn")
            return
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        for prop, pid in (("time-pos", 1), ("duration", 2), ("pause", 3), ("volume", 4)):
            self.send({"command": ["observe_property", pid, prop]})

    def _read_loop(self):
        buf = b""
        handle = self._os_handle(self.pipe)
        while True:
            if self.pipe is None:
                break
            # 只在“确实有数据”时才读：同步命名管道上挂着阻塞读会卡住写命令
            if handle is not None and not self._pipe_has_data(handle):
                time.sleep(0.02)
                continue
            try:
                with self.lock:
                    chunk = self.pipe.read(16384)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle(raw.decode("utf-8", "replace").strip())

    @staticmethod
    def _os_handle(fileobj):
        try:
            import msvcrt
            return msvcrt.get_osfhandle(fileobj.fileno())
        except Exception:
            return None

    @staticmethod
    def _pipe_has_data(handle):
        if _PeekNamedPipe is None:
            return True
        try:
            available = wintypes.DWORD(0)
            ok = _PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
            return bool(ok) and available.value > 0
        except Exception:
            return True

    def _handle(self, line):
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if msg.get("event") == "property-change":
            name, value = msg.get("name"), msg.get("data")
            if name == "time-pos" and isinstance(value, (int, float)):
                self.state["time"] = float(value)
                self.emit("player", dict(self.state))
            elif name == "duration" and isinstance(value, (int, float)):
                self.state["duration"] = float(value)
                self.emit("player", dict(self.state))
            elif name == "pause":
                self.state["pause"] = bool(value)
                self.emit("player", dict(self.state))
            elif name == "volume" and isinstance(value, (int, float)):
                self.state["volume"] = float(value)

    def send(self, obj):
        handle = self.pipe_w or self.pipe
        if not handle:
            return
        try:
            with self.lock:
                handle.write((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception:
            pass

    def toggle_pause(self):
        self.state["pause"] = not self.state["pause"]
        self.send({"command": ["set_property", "pause", self.state["pause"]]})
        self.emit("player", dict(self.state))

    def seek(self, seconds):
        if not self.is_stream:
            self.send({"command": ["seek", float(seconds), "absolute"]})

    def seek_relative(self, delta):
        if not self.is_stream:
            self.send({"command": ["seek", float(delta), "relative"]})

    def set_volume(self, value):
        self.cfg["last_volume"] = int(value)
        self.send({"command": ["set_property", "volume", float(value)]})

    def stop(self):
        self.playing = False
        if self.pipe_w:
            try:
                self.pipe_w.write(b'{"command": ["quit"]}\n')
            except Exception:
                pass
        for proc in (self.proc, self.producer):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for obj in (self.pipe_w, self.pipe):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.pipe = None
        self.pipe_w = None
        self.proc = None
        self.producer = None
        self.state.update({"time": 0.0, "duration": 0.0, "pause": False})


# ----------------------------------------------------------------------------
# 自定义控件
# ----------------------------------------------------------------------------
class Tooltip:
    """鼠标悬停提示：停 0.3 秒后弹出小气泡，说明这个控件是干什么的。"""

    def __init__(self, widget, text, delay=300, wraplength=300):
        self.widget, self.text = widget, text
        self.delay, self.wraplength = delay, wraplength
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _=None):
        if self.tip or self._after or not self.text:
            return
        try:
            self._after = self.widget.after(self.delay, self._popup)
        except Exception:
            pass

    def _popup(self):
        self._after = None
        if self.tip or not self.text or not self.widget.winfo_exists():
            return
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        border = tk.Frame(self.tip, bg="#5A5A62")
        border.pack()
        tk.Label(border, text=self.text, bg="#2E2E34", fg="#F2F2F4", font=FONT_SMALL,
                 padx=10, pady=6, justify="left",
                 wraplength=self.wraplength).pack(padx=1, pady=1)
        self.tip.update_idletasks()
        w, h = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        sw, sh = self.tip.winfo_screenwidth(), self.tip.winfo_screenheight()
        if x + w > sw - 10:
            x = max(6, sw - w - 10)
        if y + h > sh - 40:
            y = max(6, self.widget.winfo_rooty() - h - 8)
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _=None):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class IconButton(tk.Canvas):
    """工具条图标按钮：矢量绘制、悬停高亮、带提示。"""

    def __init__(self, master, icon, command=None, tooltip="", size=42,
                 bg=TOOLBAR, fg=TEXT, accent=False):
        super().__init__(master, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.icon = icon
        self.command = command
        self.size = size
        self.bg = bg
        self.fg = ACCENT if accent else fg
        self._enabled = True
        self._hover = False
        self._draw()
        if tooltip:
            Tooltip(self, tooltip)
        self.bind("<Enter>", self._on_enter, add="+")     # add="+"：不能覆盖悬停提示的绑定
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_enabled(self, flag):
        self._enabled = flag
        self.configure(cursor="hand2" if flag else "arrow")
        self._draw()

    def set_accent(self, flag):
        self.fg = ACCENT if flag else TEXT
        self._draw()

    def set_icon(self, icon):
        self.icon = icon
        self._draw()

    def _color(self):
        if not self._enabled:
            return "#5A5A62"
        if self._hover:
            return "#FFFFFF" if self.fg == ACCENT else ACCENT
        return self.fg

    def _draw(self):
        self.delete("all")
        bg = HOVER if (self._hover and self._enabled) else self.bg
        self.configure(bg=bg)
        s = self.size
        c = self._color()
        pad = 11
        x0, x1 = pad, s - pad
        y0, y1 = pad, s - pad
        cx, cy = s / 2, s / 2
        w = max(2, int(s / 16))

        if self.icon == "add":
            self.create_oval(x0, y0, x1, y1, outline=c, width=w)
            self.create_line(cx - 5, cy, cx + 5, cy, fill=c, width=w)
            self.create_line(cx, cy - 5, cx, cy + 5, fill=c, width=w)
        elif self.icon == "play":
            self.create_polygon(cx - 6, cy - 8, cx - 6, cy + 8, cx + 8, cy,
                                fill=c, outline=c)
        elif self.icon == "pause":
            self.create_rectangle(cx - 7, cy - 8, cx - 3, cy + 8, fill=c, outline=c)
            self.create_rectangle(cx + 3, cy - 8, cx + 7, cy + 8, fill=c, outline=c)
        elif self.icon == "stop":
            self.create_rectangle(cx - 6, cy - 6, cx + 6, cy + 6, fill=c, outline=c)
        elif self.icon == "folder":
            self.create_polygon(x0, y0 + 3, x0 + 9, y0 + 3, x0 + 11, y0 + 6,
                                x1, y0 + 6, x1, y1 - 2, x0, y1 - 2,
                                fill="", outline=c, width=w, joinstyle="round")
        elif self.icon == "gear":
            self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, outline=c, width=w)
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                self.create_line(cx + dx * 8, cy + dy * 8, cx + dx * 10.5, cy + dy * 10.5,
                                 fill=c, width=w)
        elif self.icon == "log":
            for i, y in enumerate((cy - 6, cy, cy + 6)):
                self.create_line(x0, y, x1 - i * 4, y, fill=c, width=w)
        elif self.icon == "trash":
            self.create_rectangle(cx - 6, cy - 5, cx + 6, cy + 8, outline=c, width=w)
            self.create_line(cx - 8, cy - 7, cx + 8, cy - 7, fill=c, width=w)
            self.create_line(cx - 3, cy - 7, cx - 3, cy - 9, fill=c, width=w)
            self.create_line(cx + 3, cy - 7, cx + 3, cy - 9, fill=c, width=w)
        elif self.icon == "clear":
            self.create_line(x0, y1, x1, y0, fill=c, width=w)
            self.create_line(x0, y0, x1, y1, fill=c, width=w)
        elif self.icon == "about":
            self.create_oval(x0, y0, x1, y1, outline=c, width=w)
            self.create_line(cx, cy - 4, cx, cy + 5, fill=c, width=w)
            self.create_line(cx, cy - 8, cx, cy - 6.5, fill=c, width=w)

    def _on_enter(self, _):
        self._hover = True
        self._draw()

    def _on_leave(self, _):
        self._hover = False
        self._draw()

    def _on_press(self, _):
        if self._enabled and self.command:
            self.command()

    def _on_release(self, _):
        pass


class FlatButton(tk.Label):
    """扁平按钮（用 Label 实现，配色可控）。"""

    def __init__(self, master, text, command=None, bg=CARD, fg=TEXT,
                 hover_bg=HOVER, padx=14, pady=6, font=FONT_UI, width=None,
                 tooltip=""):
        super().__init__(master, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, cursor="hand2")
        if width:
            self.configure(width=width)
        self._bg, self._hover_bg = bg, hover_bg
        self.command = command
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover_bg))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<Button-1>", self._click)
        if tooltip:
            Tooltip(self, tooltip)

    def _click(self, _):
        if self.command:
            self.command()


class ProgressBar(tk.Canvas):
    """VLC 风格进度条：深色轨道 + 橙色填充；未知进度时显示流动块。"""

    def __init__(self, master, height=8, bg=BG, track=TRACK_BG, fill=ACCENT):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.track_color, self.fill_color = track, fill
        self.value = 0.0
        self.indeterminate = False
        self.on_seek = None          # 传入回调后，进度条可点击/拖动跳转
        self._phase = 0
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_click)

    def _on_click(self, event):
        if not self.on_seek:
            return
        width = max(1, self.winfo_width())
        frac = min(1.0, max(0.0, event.x / width))
        self.set(frac * 100.0)
        self.on_seek(frac)

    def set(self, value, indeterminate=False):
        self.value = max(0.0, min(100.0, value or 0.0))
        self.indeterminate = indeterminate
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2:
            return
        r = h / 2
        self.create_rectangle(0, 0, w, h, fill=self.track_color, outline="")
        if self.indeterminate:
            seg = max(40, w * 0.22)
            pos = (self._phase * 18) % (w + seg) - seg
            self.create_rectangle(max(0, pos), 0, min(w, pos + seg), h,
                                  fill=self.fill_color, outline="")
            self._phase += 1
            self.after(60, self._tick)
        else:
            fw = w * self.value / 100.0
            if fw > 1:
                self.create_rectangle(0, 0, fw, h, fill=self.fill_color, outline="")

    def _tick(self):
        if self.indeterminate:
            self._draw()


class DarkDialog(tk.Toplevel):
    """深色模态对话框基类。"""

    def __init__(self, master, title, width=560, height=440):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        px = master.winfo_rootx() + (master.winfo_width() - width) // 2
        py = master.winfo_rooty() + (master.winfo_height() - height) // 3
        self.geometry(f"+{max(0, px)}+{max(0, py)}")


class SettingsDialog(DarkDialog):
    def __init__(self, master, cfg):
        super().__init__(master, "设置", 660, 620)
        self.cfg = cfg
        pad = {"padx": 24, "pady": 6}

        tk.Label(self, text="设置", bg=BG, fg=TEXT, font=FONT_TITLE).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=24, pady=(20, 14))

        row = 1

        def label(text, r):
            tk.Label(self, text=text, bg=BG, fg=TEXT_DIM, font=FONT_SMALL).grid(
                row=r, column=0, sticky="w", **pad)

        def entry_row(text, value, r, browse=None):
            label(text, r)
            var = tk.StringVar(value=value or "")
            ent = tk.Entry(self, textvariable=var, bg=INPUT_BG, fg=TEXT, font=FONT_SMALL,
                           insertbackground=ACCENT, relief="flat",
                           highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=ACCENT)
            ent.grid(row=r, column=1, sticky="we", **pad)
            if browse:
                FlatButton(self, "浏览…", browse).grid(row=r, column=2, padx=(0, 24), pady=6)
            return var

        self.var_ytdlp = entry_row("yt-dlp 路径（留空自动查找）", cfg["ytdlp_path"], row,
                                   browse=self._pick_ytdlp)
        row += 1
        self.var_outdir = entry_row("下载目录", cfg["outdir"], row, browse=self._pick_dir)
        row += 1

        label("默认画质", row)
        self.var_quality = tk.StringVar(value=dict(
            (v, k) for k, v in QUALITIES).get(cfg["quality"], "最佳画质"))
        combo = ttk.Combobox(self, textvariable=self.var_quality, state="readonly",
                             values=[q[0] for q in QUALITIES], font=FONT_SMALL)
        combo.grid(row=row, column=1, sticky="we", **pad)
        row += 1

        label("Cookies 浏览器", row)
        self.var_cookies = tk.StringVar(value=cfg["cookies_browser"])
        ttk.Combobox(self, textvariable=self.var_cookies, state="readonly",
                     values=["不使用", "Edge", "Chrome", "Firefox"], font=FONT_SMALL
                     ).grid(row=row, column=1, sticky="we", **pad)
        row += 1

        self.var_cookies_file = entry_row("cookies.txt 文件（可选，优先于上面的浏览器）",
                                          cfg["cookies_file"], row,
                                          browse=self._pick_cookies_file)
        row += 1

        self.var_proxy = entry_row("代理（如 http://127.0.0.1:7890）", cfg["proxy"], row)
        row += 1

        self.var_player = entry_row("播放器路径（留空 = 系统默认播放器）",
                                    cfg["player_path"], row, browse=self._pick_player)
        row += 1

        self.var_mpv = entry_row("内置播放内核 mpv（留空 = 自动查找 / 首次播放自动下载）",
                                 cfg["mpv_path"], row, browse=self._pick_mpv)
        row += 1

        self.var_aria = entry_row("BT 内核 aria2c（留空 = 自动查找 / 首次磁力下载自动获取）",
                                  cfg["aria2c_path"], row, browse=self._pick_aria2c)
        row += 1

        self.var_ffmpeg = entry_row("ffmpeg 合并内核（留空 = 自动查找 / 首次下载高清自动获取）",
                                    cfg["ffmpeg_path"], row, browse=self._pick_ffmpeg)
        row += 1

        label("内核维护", row)
        btns = tk.Frame(self, bg=BG)
        btns.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        FlatButton(btns, "检查 yt-dlp 更新…", self._check_update, bg=CARD, fg=TEXT,
                   tooltip="联网检查 yt-dlp 是否有新版本\n"
                           "（yt-dlp 每周更新多次，建议定期检查）").pack(side="left")
        FlatButton(btns, "获取 ffmpeg", self._get_ffmpeg, bg=CARD, fg=TEXT,
                   tooltip="下载 ffmpeg 合并内核（约 30MB，走国内镜像）\n"
                           "用于把高清视频的音轨和画轨合并成一个文件").pack(side="left", padx=10)
        row += 1

        label("失败自动重试次数", row)
        self.var_retries = tk.StringVar(value=str(cfg["retries"]))
        tk.Spinbox(self, from_=0, to=5, textvariable=self.var_retries, width=5,
                   bg=INPUT_BG, fg=TEXT, buttonbackground=CARD, relief="flat",
                   font=FONT_SMALL, insertbackground=ACCENT).grid(
            row=row, column=1, sticky="w", **pad)
        row += 1

        self.var_playlist = tk.BooleanVar(value=cfg["download_playlist"])
        self._check("下载整个播放列表/合集", self.var_playlist, row)
        row += 1
        self.var_openfolder = tk.BooleanVar(value=cfg["open_folder_when_done"])
        self._check("完成后自动打开下载目录", self.var_openfolder, row)
        row += 1

        bar = tk.Frame(self, bg=BG)
        bar.grid(row=row, column=0, columnspan=3, sticky="e", padx=24, pady=20)
        FlatButton(bar, "取消", self.destroy, bg=CARD, fg=TEXT_DIM,
                   tooltip="放弃修改，关掉这个窗口").pack(side="left", padx=6)
        FlatButton(bar, "保存", self._save, bg=ACCENT, fg="#141414",
                   hover_bg=ACCENT_DARK, font=FONT_BOLD,
                   tooltip="保存设置并关闭").pack(side="left")

        self.columnconfigure(1, weight=1)
        self._fit_to_content(master)

    def _fit_to_content(self, master):
        """按内容自适应大小并重新居中（防止新增的行被裁掉）。"""
        self.update_idletasks()
        w = max(self.winfo_reqwidth() + 16, 680)
        h = min(self.winfo_reqheight() + 12, self.winfo_screenheight() - 120)
        self.geometry(f"{w}x{h}")
        px = master.winfo_rootx() + (master.winfo_width() - w) // 2
        py = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

    def _check(self, text, var, row):
        cb = tk.Checkbutton(self, text=text, variable=var, bg=BG, fg=TEXT,
                            selectcolor=INPUT_BG, activebackground=BG,
                            activeforeground=TEXT, font=FONT_SMALL, bd=0,
                            highlightthickness=0, anchor="w")
        cb.grid(row=row, column=1, sticky="w", padx=24, pady=2)

    def _pick_ytdlp(self):
        p = filedialog.askopenfilename(title="选择 yt-dlp 可执行文件",
                                       filetypes=[("yt-dlp", "yt-dlp*"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_ytdlp.set(p)

    def _pick_dir(self):
        p = filedialog.askdirectory(title="选择下载目录")
        if p:
            self.var_outdir.set(p)

    def _pick_player(self):
        p = filedialog.askopenfilename(title="选择播放器程序（如 vlc.exe / PotPlayerMini64.exe）",
                                       filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if p:
            self.var_player.set(p)

    def _pick_mpv(self):
        p = filedialog.askopenfilename(title="选择 mpv.exe",
                                       filetypes=[("mpv", "mpv*.exe"), ("所有文件", "*.*")])
        if p:
            self.var_mpv.set(p)

    def _pick_cookies_file(self):
        p = filedialog.askopenfilename(title="选择 cookies.txt",
                                       filetypes=[("cookies 文本", "*.txt"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_cookies_file.set(p)

    def _pick_aria2c(self):
        p = filedialog.askopenfilename(title="选择 aria2c.exe",
                                       filetypes=[("aria2c", "aria2c.exe"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_aria.set(p)

    def _pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="选择 ffmpeg.exe",
                                       filetypes=[("ffmpeg", "ffmpeg*.exe"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_ffmpeg.set(p)

    def _check_update(self):
        self.master._check_ytdlp_update()

    def _get_ffmpeg(self):
        self.master._get_ffmpeg_clicked()

    def _save(self):
        self.cfg["ytdlp_path"] = self.var_ytdlp.get().strip()
        self.cfg["outdir"] = self.var_outdir.get().strip()
        self.cfg["quality"] = dict(QUALITIES).get(self.var_quality.get(), "best")
        self.cfg["cookies_browser"] = self.var_cookies.get()
        self.cfg["cookies_file"] = self.var_cookies_file.get().strip()
        self.cfg["proxy"] = self.var_proxy.get().strip()
        self.cfg["player_path"] = self.var_player.get().strip()
        self.cfg["mpv_path"] = self.var_mpv.get().strip()
        self.cfg["aria2c_path"] = self.var_aria.get().strip()
        self.cfg["ffmpeg_path"] = self.var_ffmpeg.get().strip()
        try:
            self.cfg["retries"] = max(0, int(self.var_retries.get()))
        except ValueError:
            self.cfg["retries"] = 2
        self.cfg["download_playlist"] = bool(self.var_playlist.get())
        self.cfg["open_folder_when_done"] = bool(self.var_openfolder.get())
        self.cfg.save()
        self.destroy()


class AboutDialog(DarkDialog):
    def __init__(self, master, info):
        super().__init__(master, "关于", 460, 300)
        tk.Label(self, text=APP_NAME, bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")
                 ).pack(pady=(28, 2))
        tk.Label(self, text=f"v{VERSION}", bg=BG, fg=ACCENT, font=FONT_UI).pack()
        text = ("基于 yt-dlp 的图形界面，单文件 · 零依赖。\n"
                "界面风格参考 VLC media player。\n\n"
                f"yt-dlp：{info.get('ytdlp') or '未找到'}\n"
                f"ffmpeg：{info.get('ffmpeg_ver') or ('已检测到' if info.get('ffmpeg') else '未检测到（首次下载高清时自动获取）')}\n"
                f"Python：{sys.version.split()[0]}")
        tk.Label(self, text=text, bg=BG, fg=TEXT_DIM, font=FONT_SMALL,
                 justify="left").pack(pady=14)
        FlatButton(self, "好的", self.destroy, bg=ACCENT, fg="#141414",
                   hover_bg=ACCENT_DARK, font=FONT_BOLD).pack(pady=8)


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, initial_url=None, initial_play=None):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("1180x720")
        self.minsize(940, 600)

        self.cfg = Config()
        self.events = queue.Queue()
        self.engine = Engine(self.cfg, emit=self._emit_from_worker)
        self.tasks = {}
        self.order = []
        self.current = None
        self.queue = queue.Queue()
        self.worker = None
        self.worker_busy = False
        self.stop_flag = False
        self.log_visible = True

        self._set_icon()
        self._build_styles()
        self._build_toolbar()
        self._build_body()
        self._build_bottom()
        self._build_statusbar()
        self._bind_keys()

        self.player = MpvPlayer(self.cfg, emit=self._emit_from_worker)
        self.player_view_on = False
        self.fullscreen = False
        self.play_speed = 1.0

        self.after(80, self._pump)
        self._start_worker()
        self._refresh_env_info()

        if initial_url:
            self._clear_placeholder()
            self.url_var.set(initial_url)
            self.add_from_entry()
        if initial_play:
            self.after(600, lambda: self.play_path(initial_play,
                                                   os.path.basename(initial_play)))

    # ---- 图标 --------------------------------------------------------------
    def _set_icon(self):
        """窗口/任务栏图标：优先用 assets 里随包发布的图标，缺失时回退到内置生成。"""
        self.icon_img = None
        self.logo_img = None
        img = load_photo(asset_path("icon_256.png", "icon_192.png", "icon_128.png",
                                    "icon_1024.png"))
        if img is not None:
            self.icon_img = img
            try:
                self.iconphoto(True, img)
            except Exception:
                pass
        else:
            try:
                png = base64.b64encode(make_logo_png(96)).decode("ascii")
                self.icon_img = tk.PhotoImage(data=png)
                self.iconphoto(True, self.icon_img)
            except Exception:
                self.icon_img = None
        self.logo_img = load_photo(asset_path("icon_40.png", "icon_48.png", "icon_64.png"))

    # ---- 全局样式 ----------------------------------------------------------
    def _build_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=32, borderwidth=0,
                        font=FONT_SMALL)
        style.map("Dark.Treeview",
                  background=[("selected", SEL_BG)],
                  foreground=[("selected", "#FFFFFF")])
        style.configure("Dark.Treeview.Heading", background=CARD, foreground=TEXT_DIM,
                        relief="flat", font=FONT_SMALL, padding=(8, 8))
        style.map("Dark.Treeview.Heading", background=[("active", HOVER)])
        style.layout("Dark.Treeview",
                     [("Dark.Treeview.treearea", {"sticky": "nswe"})])

        style.configure("Dark.Vertical.TScrollbar", background=CARD,
                        troughcolor=PANEL, bordercolor=PANEL,
                        arrowcolor=TEXT_DIM, relief="flat")
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", HOVER)])

        style.configure("Dark.TCombobox", fieldbackground=INPUT_BG, background=CARD,
                        foreground=TEXT, arrowcolor=TEXT_DIM, borderwidth=0,
                        relief="flat")
        style.map("Dark.TCombobox", fieldbackground=[("readonly", INPUT_BG)],
                  foreground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#141414")
        self.option_add("*TCombobox*Listbox.font", FONT_SMALL)

    # ---- 工具条 ------------------------------------------------------------
    def _build_toolbar(self):
        bar = tk.Frame(self, bg=TOOLBAR, height=58)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        logo = tk.Canvas(bar, width=58, height=58, bg=TOOLBAR, highlightthickness=0)
        logo.pack(side="left")
        if self.logo_img is not None:
            logo.create_image(29, 29, image=self.logo_img)
        elif self.icon_img:
            logo.create_image(29, 29, image=self.icon_img)
        tk.Label(bar, text=APP_NAME, bg=TOOLBAR, fg=TEXT, font=FONT_BOLD).pack(
            side="left", padx=(2, 16))

        wrap = tk.Frame(bar, bg=TOOLBAR)
        wrap.pack(side="left", fill="y")

        self.btn_start = IconButton(wrap, "play", self.start_queue,
                                    "开始下载队列\n新任务会按顺序自动下载", accent=True)
        self.btn_start.pack(side="left", pady=5)
        self.btn_pause = IconButton(wrap, "pause", self.pause_queue, "暂停当前下载任务")
        self.btn_pause.pack(side="left", pady=5)
        self.btn_stop = IconButton(wrap, "stop", self.cancel_current, "取消（终止）当前下载任务")
        self.btn_stop.pack(side="left", pady=5)
        tk.Frame(wrap, bg=BORDER, width=1).pack(side="left", fill="y", pady=12, padx=6)
        IconButton(wrap, "add", self._focus_entry,
                   "添加链接：光标移到下方输入框\n快捷键 Ctrl+N").pack(side="left", pady=5)
        IconButton(wrap, "folder", self.open_outdir,
                   "打开下载目录\n快捷键 Ctrl+O").pack(side="left", pady=5)
        self.btn_log = IconButton(wrap, "log", self.toggle_log, "显示 / 隐藏底部日志面板")
        self.btn_log.pack(side="left", pady=5)

        right = tk.Frame(bar, bg=TOOLBAR)
        right.pack(side="right", padx=10)
        IconButton(right, "gear", self.open_settings,
                   "设置：下载目录 / 默认画质 / Cookies / 代理 / 播放内核").pack(
            side="right", pady=5)
        IconButton(right, "about", self.open_about,
                   "关于本程序（版本、yt-dlp 与 ffmpeg 状态）").pack(side="right", pady=5)
        IconButton(right, "clear", self.clear_finished,
                   "清除已完成 / 失败 / 已取消的任务").pack(side="right", pady=5)

        self._toolbar = bar

    # ---- 主体 --------------------------------------------------------------
    def _build_body(self):
        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)
        self._body = body

        # 左：任务列表 + 日志
        left = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=8)
        self._left_panel = left

        head = tk.Frame(left, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="下载任务", bg=PANEL, fg=TEXT, font=FONT_BOLD).pack(
            side="left", padx=12, pady=8)
        self.count_label = tk.Label(head, text="", bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL)
        self.count_label.pack(side="right", padx=12)

        columns = ("status", "title", "progress", "speed", "eta")
        self.tree = ttk.Treeview(left, columns=columns, show="headings",
                                 style="Dark.Treeview", selectmode="browse")
        for col, text, width, anchor, stretch in (
                ("status", "状态", 84, "center", False),
                ("title", "标题", 380, "w", True),
                ("progress", "进度", 76, "center", False),
                ("speed", "速度", 104, "center", False),
                ("eta", "剩余", 76, "center", False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview,
                            style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.tag_configure("done", foreground=OK_GREEN)
        self.tree.tag_configure("error", foreground=ERR_RED)
        self.tree.tag_configure("active", foreground=TEXT)
        self.tree.tag_configure("idle", foreground=TEXT_DIM)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self._build_context_menu()
        Tooltip(self.tree, "双击任务行 = 播放视频\n"
                           "右键 = 打开文件/文件夹、复制链接、重新下载\n"
                           "Delete 键 = 移除选中任务")

        # 空状态提示（有任务时隐藏）
        self.empty_hint = tk.Label(
            left, bg=PANEL, fg=TEXT_DIM, font=FONT_UI, justify="center",
            text="还没有下载任务\n\n把视频链接粘贴到下方输入框，点击「下载」")
        self.empty_hint.place(relx=0.5, rely=0.42, anchor="center")
        self.empty_hint.lift()

        # 日志面板
        self.log_frame = tk.Frame(left, bg=PANEL)
        self.log_frame.pack(fill="x", side="bottom")
        log_head = tk.Frame(self.log_frame, bg=PANEL)
        log_head.pack(fill="x")
        tk.Label(log_head, text="日志", bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL).pack(
            side="left", padx=12, pady=(2, 0))
        self.log_text = tk.Text(self.log_frame, height=7, bg="#1D1D20", fg=TEXT_DIM,
                                font=("Consolas", 9), relief="flat", wrap="word",
                                insertbackground=TEXT, highlightthickness=1,
                                highlightbackground=BORDER, padx=10, pady=6)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.log_text.configure(state="disabled")
        self.log_text.tag_configure("err", foreground=ERR_RED)
        self.log_text.tag_configure("ok", foreground=OK_GREEN)
        self.log_text.tag_configure("warn", foreground=WARN_YELLOW)

        # 右：详情面板
        right = tk.Frame(body, bg=PANEL, width=300,
                         highlightthickness=1, highlightbackground=BORDER)
        right.pack(side="right", fill="y", padx=(6, 10), pady=8)
        right.pack_propagate(False)
        self._right_panel = right
        self._build_player_panel(body)

        tk.Label(right, text="详情", bg=PANEL, fg=TEXT, font=FONT_BOLD).pack(
            anchor="w", padx=14, pady=(10, 6))

        self.cover = tk.Canvas(right, width=260, height=130, bg="#171719",
                               highlightthickness=0)
        self.cover.pack(padx=14, pady=(2, 10))
        self._draw_cover()

        self.detail_title = tk.Label(right, text="未选择任务", bg=PANEL, fg=TEXT,
                                     font=FONT_BOLD, wraplength=250, justify="left")
        self.detail_title.pack(anchor="w", padx=14)

        self.detail_rows = {}
        for key, name in (("uploader", "UP 主"), ("duration", "时长"),
                          ("status", "状态"), ("size", "大小"),
                          ("notice", "提示"), ("error", "错误")):
            rowf = tk.Frame(right, bg=PANEL)
            rowf.pack(fill="x", padx=14, pady=2)
            tk.Label(rowf, text=name, bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL, width=6,
                     anchor="w").pack(side="left")
            val = tk.Label(rowf, text="-", bg=PANEL, fg=TEXT, font=FONT_SMALL,
                           wraplength=180, justify="left", anchor="w")
            val.pack(side="left", fill="x", expand=True)
            self.detail_rows[key] = val
        self.detail_rows["error"].master.pack_forget()   # 默认隐藏，有错误时才显示
        self.detail_rows["notice"].master.pack_forget()  # 默认隐藏，有提示时才显示

        tk.Label(right, text="保存位置", bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL).pack(
            anchor="w", padx=14, pady=(8, 0))
        self.detail_path = tk.Label(right, text="-", bg=PANEL, fg=TEXT_DIM, font=("Microsoft YaHei UI", 8),
                                    wraplength=250, justify="left")
        self.detail_path.pack(anchor="w", padx=14)

        btns = tk.Frame(right, bg=PANEL)
        btns.pack(side="bottom", fill="x", padx=14, pady=12)
        FlatButton(btns, "▶ 播放", self.play_selected, bg=ACCENT, fg="#141414",
                   hover_bg=ACCENT_DARK, font=FONT_BOLD, pady=7,
                   tooltip="用内置播放器播放选中的视频\n（还没下载完？那就是边下边播）").pack(
            side="top", fill="x", pady=(0, 6))
        row_btns = tk.Frame(btns, bg=PANEL)
        row_btns.pack(side="top", fill="x")
        FlatButton(row_btns, "打开文件", self.open_selected_file, bg=CARD,
                   tooltip="用系统默认播放器打开下载好的文件").pack(
            side="left", expand=True, fill="x", padx=(0, 3))
        FlatButton(row_btns, "打开目录", self.open_selected_dir, bg=CARD,
                   tooltip="在资源管理器里定位这个文件").pack(
            side="left", expand=True, fill="x", padx=(3, 0))

    def _build_context_menu(self):
        self.menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT, bd=0,
                            activebackground=ACCENT, activeforeground="#141414",
                            font=FONT_SMALL)
        self.menu.add_command(label="打开文件", command=self.open_selected_file)
        self.menu.add_command(label="播放视频", command=self.play_selected)
        self.menu.add_command(label="打开所在文件夹", command=self.open_selected_dir)
        self.menu.add_separator()
        self.menu.add_command(label="复制视频链接", command=self.copy_selected_url)
        self.menu.add_command(label="重新下载", command=self.retry_selected)
        self.menu.add_separator()
        self.menu.add_command(label="移除任务", command=self.remove_selected)

        def popup(event):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self.menu.tk_popup(event.x_root, event.y_root)
        self.tree.bind("<Button-3>", popup)

    def _draw_cover(self):
        self.cover.delete("all")
        # 没有选中任务时，封面区留空（不放任何图标）
        self.cover_img = None

    def _set_cover(self, task):
        """把选中任务的真实封面画到详情面板上；没有封面就留空。"""
        self.cover.delete("all")
        self.cover_img = None
        if task is None:
            return
        self._ensure_thumbnail(task)
        if task.thumb_png and os.path.isfile(task.thumb_png):
            try:
                img = tk.PhotoImage(file=task.thumb_png)
                self.cover_img = img          # 必须留引用，否则会被回收
                self.cover.create_image(130, 65, image=img)
            except Exception:
                self.cover_img = None

    def _ensure_thumbnail(self, task):
        """后台线程里下载并转换封面，不阻塞界面。"""
        # 已经有随视频一起下载的封面时，优先用它（和视频严格一一对应）
        if not task.thumb_png and task.cover_src:
            png = self.engine.convert_cover(task.cover_src,
                                            cover_key(task.video_id, task.url) + "_dl",
                                            self.engine.THUMB_W, self.engine.THUMB_H)
            if png:
                task.thumb_png = png
        if task.thumb_started or task.thumb_png or not task.thumb_url:
            return
        task.thumb_started = True
        # 缓存 key 必须跟“视频”绑定（视频 ID 或链接哈希），
        # 绝不能用任务编号 —— 每次开程序任务编号都从 1 开始，会把上一个视频的封面认成本次的
        key = cover_key(task.video_id, task.url)

        def work():
            try:
                path = self.engine.fetch_thumbnail(task.thumb_url, key,
                                                   self.engine.THUMB_W,
                                                   self.engine.THUMB_H,
                                                   page_url=task.url)
                if path:
                    task.thumb_png = path
                    self.events.put(("thumb", task))
            except Exception as exc:
                self.events.put(("log", f"封面获取失败：{exc}", "warn"))

        threading.Thread(target=work, daemon=True).start()

    def _adopt_cover_async(self, task):
        """下载完成后：把 yt-dlp 一起下载下来的封面接进详情面板。"""
        if task.status not in ("已完成", "已存在"):
            return

        def work():
            try:
                png = self.engine.adopt_cover(task)
                if png:
                    task.thumb_png = png
                    self.events.put(("thumb", task))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    # ---- 内置播放器面板 ----------------------------------------------------
    def _build_player_panel(self, parent):
        panel = tk.Frame(parent, bg="#101012",
                         highlightthickness=1, highlightbackground=BORDER)
        self.player_panel = panel

        bar = tk.Frame(panel, bg=PANEL)
        bar.pack(fill="x")
        self.player_title = tk.Label(bar, text="", bg=PANEL, fg=TEXT, font=FONT_BOLD,
                                     anchor="w")
        self.player_title.pack(side="left", padx=12, pady=8)
        FlatButton(bar, "← 返回任务列表", self.hide_player, bg=CARD, fg=TEXT_DIM,
                   tooltip="停止播放，回到下载任务列表（Esc）").pack(
            side="right", padx=10, pady=6)

        # 视频区：mpv 会被嵌进这个 Frame
        self.video_frame = tk.Frame(panel, bg="#000000")
        self.video_frame.pack(fill="both", expand=True, padx=10, pady=(6, 4))

        ctl = tk.Frame(panel, bg=PANEL)
        ctl.pack(fill="x", side="bottom")

        self.player_seek = ProgressBar(ctl, height=10, bg=PANEL, track="#4A4A52")
        self.player_seek.on_seek = self.on_player_seek
        self.player_seek.pack(fill="x", padx=12, pady=(10, 6))
        Tooltip(self.player_seek, "点击或拖动这里，跳到视频的对应位置")

        row = tk.Frame(ctl, bg=PANEL)
        row.pack(fill="x", padx=12, pady=(0, 10))

        self.btn_play = IconButton(row, "pause", self.player_toggle,
                                   "播放 / 暂停\n快捷键：空格", size=38, bg=PANEL, accent=True)
        self.btn_play.pack(side="left")
        IconButton(row, "stop", self.player_stop, "停止播放", size=38, bg=PANEL).pack(
            side="left", padx=(6, 0))

        self.player_time = tk.Label(row, text="--:-- / --:--", bg=PANEL, fg=TEXT,
                                    font=("Consolas", 10))
        self.player_time.pack(side="left", padx=12)

        self.speed_btn = FlatButton(row, "倍速 1.0x", self.cycle_speed, bg=CARD,
                                    fg=TEXT_DIM, font=FONT_SMALL,
                                    tooltip="切换播放速度：1.0x → 1.25x → 1.5x → 2.0x → 0.5x")
        self.speed_btn.pack(side="left", padx=(0, 6))
        FlatButton(row, "全屏", self.toggle_fullscreen, bg=CARD, fg=TEXT_DIM,
                   font=FONT_SMALL, tooltip="全屏播放（快捷键 F11，再按一次退出）").pack(side="left")

        tk.Label(row, text="音量", bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL).pack(
            side="right", padx=(4, 4))
        self.vol_scale = tk.Scale(row, from_=0, to=150, orient="horizontal",
                                  length=96, showvalue=0, bg=PANEL, fg=TEXT,
                                  troughcolor="#4A4A52", highlightthickness=0, bd=0,
                                  relief="flat", activebackground=ACCENT,
                                  command=self.on_volume)
        self.vol_scale.set(int(self.cfg["last_volume"] or 100))
        self.vol_scale.pack(side="right")
        Tooltip(self.vol_scale, "音量大小（0 - 150）")

    def show_player(self):
        if not self.player_view_on:
            self._left_panel.pack_forget()
            self._right_panel.pack_forget()
            self._bottom.pack_forget()          # 播放时收起下载输入区，界面只留播放器
            self.player_panel.pack(fill="both", expand=True, padx=10, pady=8)
            self.player_view_on = True
        self.update_idletasks()

    def hide_player(self):
        if not self.player_view_on:
            return
        try:
            self.player.stop()
        except Exception:
            pass
        self.player_panel.pack_forget()
        self._left_panel.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=8)
        self._right_panel.pack(side="right", fill="y", padx=(6, 10), pady=8)
        self._bottom.pack(side="bottom", fill="x", before=self._statusbar)
        self.player_view_on = False
        if self.fullscreen:
            self.toggle_fullscreen()

    def _ensure_player(self):
        """确保播放内核可用；没有就问用户是否自动下载。"""
        if self.player.find_mpv():
            return True
        if not messagebox.askyesno(
                APP_NAME,
                "还没有播放内核（mpv，约 32MB，下载后一直可用）。\n\n"
                "现在自动下载吗？（走国内镜像加速，稍等片刻）", parent=self):
            return False

        def work():
            try:
                self.player.download_mpv()
                self.events.put(("log", "播放内核已就绪，请再点一次「播放」", "ok"))
            except Exception as exc:
                self.events.put(("log", f"播放内核下载失败：{exc}", "err"))
        threading.Thread(target=work, daemon=True).start()
        return False

    def play_path(self, path, title=""):
        if not self._ensure_player():
            return
        self.show_player()
        try:
            self.player.play_file(path, self.video_frame.winfo_id())
        except Exception as exc:
            self._log(f"播放失败：{exc}", "err")
            return
        self.player_title.configure(text=title or os.path.basename(path))
        self.player_seek.set(0)
        self.btn_play.set_icon("pause")
        self.player_time.configure(text="00:00 / --:--")
        self.play_speed = 1.0
        self.speed_btn.configure(text="倍速 1.0x")
        self.after(300, self.focus_force)      # 键盘控制留在主窗口
        self._log(f"开始播放：{path}")

    def player_toggle(self):
        if self.player.playing:
            self.player.toggle_pause()

    def cycle_speed(self):
        speeds = [1.0, 1.25, 1.5, 2.0, 0.5]
        self.play_speed = speeds[(speeds.index(self.play_speed) + 1) % len(speeds)] \
            if self.play_speed in speeds else 1.0
        self.player.send({"command": ["set_property", "speed", self.play_speed]})
        self.speed_btn.configure(text=f"倍速 {self.play_speed:g}x")

    def toggle_fullscreen(self):
        if not self.player_view_on and not self.fullscreen:
            return
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self._toolbar.pack_forget()
            self._statusbar.pack_forget()
            self.attributes("-fullscreen", True)
        else:
            self.attributes("-fullscreen", False)
            self._toolbar.pack(side="top", fill="x", before=self._body)
            self._statusbar.pack(side="bottom", fill="x")
        self.update_idletasks()

    def player_stop(self):
        self.player.stop()
        self.player_seek.set(0)
        self.btn_play.set_icon("play")
        self.player_time.configure(text="--:-- / --:--")
        self.play_speed = 1.0
        self.speed_btn.configure(text="倍速 1.0x")

    def on_player_seek(self, frac):
        duration = self.player.state.get("duration") or 0
        if duration > 0:
            self.player.seek(frac * duration)
        elif self.player.is_stream:
            self._log("在线播放（流）不能拖动进度，可先下载再看完整回放", "warn")

    def on_volume(self, value):
        try:
            self.player.set_volume(float(value))
        except Exception:
            pass

    def _refresh_player_ui(self, state):
        duration = state.get("duration") or 0
        position = state.get("time") or 0
        if duration > 0:
            self.player_seek.set(position * 100.0 / duration)
            self.player_time.configure(
                text=f"{fmt_clock(position)} / {fmt_clock(duration)}")
        elif position:
            self.player_time.configure(text=f"{fmt_clock(position)} / 直播流")
        self.btn_play.set_icon("play" if state.get("pause") else "pause")

    # ---- 底部输入区 --------------------------------------------------------
    def _build_bottom(self):
        bottom = tk.Frame(self, bg=TOOLBAR)
        bottom.pack(side="bottom", fill="x")
        self._bottom = bottom

        row1 = tk.Frame(bottom, bg=TOOLBAR)
        row1.pack(fill="x", padx=12, pady=(8, 6))

        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(row1, textvariable=self.url_var, bg=INPUT_BG, fg=TEXT,
                                  insertbackground=ACCENT, relief="flat", font=FONT_UI,
                                  highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=ACCENT)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.url_entry.bind("<Return>", lambda e: self.add_from_entry())
        self.url_entry.bind("<FocusIn>", self._clear_placeholder, add="+")
        self.url_entry.bind("<FocusOut>", lambda e: self._show_placeholder(), add="+")
        self.url_entry.bind("<KeyRelease>", self._auto_detect_mode, add="+")
        self._show_placeholder()
        Tooltip(self.url_entry, "在这里粘贴视频链接（B站、YouTube 等 1800+ 网站）\n"
                                "也可以粘贴 magnet: 磁力链接（会自动切到磁力下载）\n"
                                "回车 = 加入下载队列")

        FlatButton(row1, "粘贴", self.paste_url, bg=CARD, fg=TEXT,
                   tooltip="把剪贴板里的链接填进输入框（Ctrl+V 也行）").pack(
            side="left", padx=6)

        self.quality_var = tk.StringVar(value=dict(
            (v, k) for k, v in QUALITIES).get(self.cfg["quality"], "最佳画质"))
        combo = ttk.Combobox(row1, textvariable=self.quality_var, state="readonly",
                             values=[q[0] for q in QUALITIES], width=11,
                             style="Dark.TCombobox", font=FONT_SMALL)
        combo.pack(side="left", padx=(0, 8), ipady=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self.cfg.__setitem__(
            "quality", dict(QUALITIES).get(self.quality_var.get(), "best")))
        Tooltip(combo, "选择下载画质：\n最佳画质 / 1080P / 720P / 480P / 仅音频 MP3")

        self.mode_var = tk.StringVar(value="网址下载")
        self.mode_combo = ttk.Combobox(row1, textvariable=self.mode_var, state="readonly",
                                       values=["网址下载", "磁力下载"], width=9,
                                       style="Dark.TCombobox", font=FONT_SMALL)
        self.mode_combo.pack(side="left", padx=(0, 8), ipady=4)
        Tooltip(self.mode_combo,
                "选择下载方式：\n"
                "网址下载 = B站/YouTube 等网站视频（用 yt-dlp）\n"
                "磁力下载 = magnet: 磁力链接（BitTorrent，用 aria2c）\n"
                "粘贴磁力链接时会自动切换")

        FlatButton(row1, "下载", self.add_from_entry, bg=ACCENT, fg="#141414",
                   hover_bg=ACCENT_DARK, font=FONT_BOLD, padx=26,
                   tooltip="把输入框里的链接加入下载队列（回车同效）").pack(side="left")
        FlatButton(row1, "试看", self.preview_from_entry, bg=CARD, fg=TEXT,
                   tooltip="不下载，直接在内置播放器里在线播放\n（边下边看，进度不能拖）").pack(
            side="left", padx=(6, 0))

        row2 = tk.Frame(bottom, bg=TOOLBAR)
        row2.pack(fill="x", padx=12, pady=(0, 4))
        self.progress = ProgressBar(row2, height=11, bg=TOOLBAR, track="#3F3F46")
        self.progress.pack(fill="x")
        Tooltip(self.progress, "当前任务的下载进度")

        row3 = tk.Frame(bottom, bg=TOOLBAR)
        row3.pack(fill="x", padx=12, pady=(4, 10))
        self.now_label = tk.Label(row3, text="就绪", bg=TOOLBAR, fg=TEXT_DIM,
                                  font=FONT_SMALL, anchor="w")
        self.now_label.pack(side="left")
        self.metrics_label = tk.Label(row3, text="", bg=TOOLBAR, fg=TEXT,
                                      font=("Consolas", 10), anchor="e")
        self.metrics_label.pack(side="right")

    # ---- 状态栏 ------------------------------------------------------------
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=PANEL, height=26)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self._statusbar = bar
        self.status_label = tk.Label(bar, text="就绪", bg=PANEL, fg=TEXT_DIM,
                                     font=FONT_SMALL, anchor="w")
        self.status_label.pack(side="left", padx=12)
        self.env_label = tk.Label(bar, text="", bg=PANEL, fg=TEXT_DIM, font=FONT_SMALL)
        self.env_label.pack(side="right", padx=12)

    def _refresh_env_info(self):
        def work():
            ver = self.engine.ytdlp_version()
            ff = self.engine.has_ffmpeg()
            self.events.put(("env", {"ytdlp": ver, "ffmpeg": ff}))
        threading.Thread(target=work, daemon=True).start()

    def _bind_keys(self):
        self.bind("<Control-n>", lambda e: self._focus_entry())
        self.bind("<Control-o>", lambda e: self.open_outdir())
        self.bind("<Delete>", lambda e: self.remove_selected())
        self.bind("<Control-a>", lambda e: self.tree.selection_set(self.tree.get_children()))
        self.bind("<space>", self._on_space)
        self.bind("<Escape>", self._on_escape)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<f>", lambda e: self.toggle_fullscreen())
        self.bind("<Left>", lambda e: self._player_step(-10))
        self.bind("<Right>", lambda e: self._player_step(10))

    def _on_space(self, _):
        if self.player_view_on and not isinstance(self.focus_get(), tk.Entry):
            self.player_toggle()
            return "break"

    def _on_escape(self, _):
        if self.fullscreen:
            self.toggle_fullscreen()
        elif self.player_view_on:
            self.hide_player()

    def _player_step(self, delta):
        if self.player_view_on and not isinstance(self.focus_get(), tk.Entry):
            self.player.seek_relative(delta)

    # ---- 队列与工作线程 ----------------------------------------------------
    def _start_worker(self):
        def loop():
            while not self.stop_flag:
                try:
                    task = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if task is None:
                    continue
                self.current = task
                if getattr(task, "kind", "url") == "magnet":
                    ok, msg = self.engine.download_magnet(task)
                else:
                    ok, msg = self.engine.download(task)
                if (not ok and not task.cancelled
                        and not self.engine.is_fatal_error(task.error)
                        and task.attempt <= self.cfg["retries"]):
                    self.events.put(("log", f"下载失败，准备重试（{task.attempt}/"
                                            f"{self.cfg['retries']}）：{task.title}", "warn"))
                    task.status = f"重试中 {task.attempt}/{self.cfg['retries']}"
                    self.events.put(("task", task))
                    time.sleep(2)
                    self.queue.put(task)
                    self.current = None
                    continue
                if ok and self.cfg["open_folder_when_done"] and task.path:
                    open_in_explorer(task.path)
                self.current = None
                self.events.put(("queue_done", task))
        self.worker = threading.Thread(target=loop, daemon=True)
        self.worker.start()

    def _emit_from_worker(self, kind, payload=None, level="info"):
        if kind == "log":
            self.events.put(("log", payload, level))
        elif kind in ("task", "progress"):
            self.events.put(("task", payload))
        else:
            self.events.put((kind, payload))

    # ---- UI 事件泵 ---------------------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "log":
                    msg, level = (rest + ["info"])[:2]
                    self._log(msg, level)
                elif kind == "task":
                    self._refresh_task(rest[0])
                elif kind == "progress":
                    self._refresh_progress(rest[0])
                elif kind == "queue_done":
                    self._refresh_task(rest[0])
                    self._update_counts()
                    self._adopt_cover_async(rest[0])
                elif kind == "env":
                    info = rest[0]
                    parts = []
                    parts.append(f"yt-dlp {info['ytdlp']}" if info["ytdlp"] else "yt-dlp 未找到 ⚠")
                    parts.append("ffmpeg ✓" if info["ffmpeg"] else "ffmpeg 未检测到 ⚠")
                    self.env_label.configure(text="  |  ".join(parts))
                    if not info["ffmpeg"] and not getattr(self, "_ffmpeg_hint_done", False):
                        self._ffmpeg_hint_done = True
                        self._log("未检测到 ffmpeg（合并高清音视频用）——首次下载时会自动获取，"
                                  "也可在「设置 → 获取 ffmpeg」里手动下载。", "warn")
                    if not info["ytdlp"]:
                        self._log("还没有下载内核 yt-dlp——首次下载时会自动获取（约 18MB，走国内镜像），"
                                  "也可以自行放置 yt-dlp.exe。", "warn")
                elif kind == "player":
                    self._refresh_player_ui(rest[0])
                elif kind == "thumb":
                    task = rest[0]
                    if self._selected_task() is task:
                        self._set_cover(task)
                elif kind == "update_check":
                    payload = rest[0] if rest else None
                    if payload:
                        cur, latest = payload.get("cur", ""), payload.get("latest", "")
                        if not latest:
                            messagebox.showinfo(APP_NAME, "没拿到最新版本号（网络问题），稍后再试。",
                                                parent=self)
                        elif cur and latest == cur:
                            messagebox.showinfo(APP_NAME, f"yt-dlp 已是最新版本（{cur}）。", parent=self)
                        elif messagebox.askyesno(
                                APP_NAME,
                                (f"本机还没有 yt-dlp 内核，现在自动获取吗？（约 18MB，走国内镜像）\n\n"
                                 f"将下载最新版：{latest}"
                                 if not cur else
                                 f"发现 yt-dlp 新版本：{latest}\n当前版本：{cur}\n\n"
                                 "现在下载并更新吗？（约 18MB；更新完成后\n"
                                 "旧版会备份为 yt-dlp.exe.bak，可随时还原）"), parent=self):
                            self._do_ytdlp_update()
                elif kind == "update_done":
                    payload = rest[0] if rest else {}
                    if payload.get("ok"):
                        messagebox.showinfo(APP_NAME, f"yt-dlp 更新完成：{payload.get('version', '')}",
                                            parent=self)
                        self._refresh_env_info()
                    else:
                        messagebox.showerror(APP_NAME, f"更新失败：{payload.get('error', '')}",
                                             parent=self)
                elif kind == "ffmpeg_done":
                    payload = rest[0] if rest else {}
                    if payload.get("ok"):
                        messagebox.showinfo(APP_NAME, f"ffmpeg 就绪：\n{payload.get('path', '')}",
                                            parent=self)
                        self._refresh_env_info()
                    else:
                        messagebox.showerror(APP_NAME, f"ffmpeg 获取失败：{payload.get('error', '')}",
                                             parent=self)
                elif kind == "progress_mpv":
                    pct = rest[0] if rest else ""
                    if pct.endswith("0%") or pct == "100%":
                        self._log(f"播放内核下载中… {pct}")
        except queue.Empty:
            pass
        self.after(80, self._pump)

    # ---- 日志 --------------------------------------------------------------
    def _log(self, msg, level="info"):
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def toggle_log(self):
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
        else:
            self.log_frame.pack(fill="x", side="bottom")

    # ---- 任务操作 ----------------------------------------------------------
    def _focus_entry(self):
        self.url_entry.focus_set()

    PLACEHOLDER_TEXT = "粘贴视频链接后按回车，或点击右侧「下载」按钮"
    PLACEHOLDER_FG = "#6E6E76"

    def _show_placeholder(self):
        e = self.url_entry
        if not e.get().strip():
            e.delete(0, "end")
            e.insert(0, self.PLACEHOLDER_TEXT)
            e.configure(fg=self.PLACEHOLDER_FG)
            e._ph_on = True

    def _clear_placeholder(self, _=None):
        e = self.url_entry
        if getattr(e, "_ph_on", False):
            e.delete(0, "end")
            e._ph_on = False
            e.configure(fg=TEXT)

    def _entry_url(self):
        if getattr(self.url_entry, "_ph_on", False):
            return ""
        return url_clean(self.url_var.get())

    def _auto_detect_mode(self, _=None):
        """粘贴/输入时自动切换下载方式。"""
        text = (self.url_var.get() or "").strip().lower()
        if text.startswith("magnet:"):
            if self.mode_var.get() != "磁力下载":
                self.mode_var.set("磁力下载")
        elif text.startswith("http") and self.mode_var.get() != "网址下载":
            self.mode_var.set("网址下载")

    @staticmethod
    def _magnet_name(url):
        import urllib.parse
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return ((q.get("dn") or [""])[0]).strip()[:120]
        except Exception:
            return ""

    def _ensure_aria2c(self):
        """确保 BT 内核可用；没有就问用户是否自动下载。"""
        if self.engine.find_aria2c():
            return True
        if not messagebox.askyesno(
                APP_NAME,
                "第一次使用磁力下载，需要 BT 内核 aria2c（约 2.4MB，下载后一直可用）。\n\n"
                "现在自动下载吗？（走国内镜像加速）", parent=self):
            return False

        def work():
            try:
                self.engine.download_aria2c()
                self.events.put(("log", "BT 内核已就绪，请再点一次「下载」", "ok"))
            except Exception as exc:
                self.events.put(("log", f"BT 内核下载失败：{exc}", "err"))
        threading.Thread(target=work, daemon=True).start()
        return False

    def _ensure_ytdlp(self):
        """确保下载内核可用；没有就问用户是否自动下载。"""
        if self.engine.find_ytdlp():
            return True
        if not messagebox.askyesno(
                APP_NAME,
                "还没有下载内核 yt-dlp（约 18MB，下载后一直可用）。\n\n"
                "现在自动下载吗？（走国内镜像加速，稍等片刻）", parent=self):
            return False

        def work():
            try:
                self.engine.download_ytdlp()
                self.events.put(("log", "下载内核已就绪，请再点一次「试看」", "ok"))
            except Exception as exc:
                self.events.put(("log", f"yt-dlp 获取失败：{exc}", "err"))
        threading.Thread(target=work, daemon=True).start()
        return False

    # ---- 内核维护（yt-dlp 更新 / ffmpeg 获取）------------------------------
    def _check_ytdlp_update(self):
        """先查最新版本，再问用户是否更新。"""
        def work():
            try:
                cur = self.engine.ytdlp_version()
                latest = self.engine.latest_ytdlp()
                self.events.put(("update_check", {"cur": cur, "latest": latest}))
            except Exception as exc:
                self.events.put(("log", f"检查更新失败（网络不通？）：{exc}", "warn"))
                self.events.put(("update_check", None))
        self.events.put(("log", "正在检查 yt-dlp 最新版本…"))
        threading.Thread(target=work, daemon=True).start()

    def _do_ytdlp_update(self):
        def work():
            try:
                ver = self.engine.update_ytdlp()
                self.events.put(("update_done", {"ok": True, "version": ver}))
            except Exception as exc:
                self.events.put(("update_done", {"ok": False, "error": str(exc)}))
        threading.Thread(target=work, daemon=True).start()

    def _get_ffmpeg_clicked(self):
        """「获取 ffmpeg」按钮：已就绪则报告状态，否则询问后下载。"""
        found = self.engine.find_ffmpeg()
        if found:
            messagebox.showinfo(APP_NAME,
                                f"ffmpeg 已就绪：\n{found}\n{self.engine.ffmpeg_version() or ''}",
                                parent=self)
            return
        if not messagebox.askyesno(
                APP_NAME,
                "下载 ffmpeg 合并内核（约 30MB，走国内镜像加速）。\n\n"
                "下载高清视频时，音轨和画轨需要它来合并；现在下载吗？", parent=self):
            return

        def work():
            try:
                p = self.engine.download_ffmpeg()
                self.events.put(("ffmpeg_done", {"ok": True, "path": p}))
            except Exception as exc:
                self.events.put(("ffmpeg_done", {"ok": False, "error": str(exc)}))
        threading.Thread(target=work, daemon=True).start()

    def paste_url(self):
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return
        data = url_clean(data)
        if data:
            self._clear_placeholder()
            self.url_var.set(data)
            self.url_entry.icursor("end")
            self._auto_detect_mode()

    def add_from_entry(self):
        url = self._entry_url()
        if not url:
            self.paste_url()
            url = self._entry_url()
        if not url:
            return
        is_magnet = url.lower().startswith("magnet:")
        if url.lower().startswith(("ed2k:", "thunder:", "ftp://")):
            messagebox.showinfo(APP_NAME,
                                "电驴(ed2k:) / 迅雷(thunder:) 链接本程序不支持；\n"
                                "磁力链接（magnet:）可以下载。", parent=self)
            return
        if is_magnet:
            if not self._ensure_aria2c():
                return
            if not re.match(r"^magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,64}", url, re.I):
                messagebox.showwarning(APP_NAME,
                                       "磁力链接不完整：需要包含 xt=urn:btih:… 这一段。\n"
                                       "完整格式类似 magnet:?xt=urn:btih:ABCDEF…&dn=名字",
                                       parent=self)
                return
            self.mode_var.set("磁力下载")
            task = Task(url, "bt")
            task.kind = "magnet"
            task.title = self._magnet_name(url) or "磁力任务"
        else:
            if not re.match(r"^(https?|ftp)://", url):
                messagebox.showwarning(
                    APP_NAME,
                    "链接格式看起来不对：\n网址下载需要 http/https 开头，磁力链接需要 magnet: 开头。",
                    parent=self)
                return
            self.mode_var.set("网址下载")
            quality = dict(QUALITIES).get(self.quality_var.get(), "best")
            task = Task(url, quality)
        self.tasks[task.id] = task
        self.order.append(task.id)
        iid = str(task.id)
        self.tree.insert("", "end", iid=iid, values=("等待中", task.display_title, "0%", "", ""),
                         tags=("idle",))
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.url_var.set("")
        self._show_placeholder()
        self._log(f"加入队列{'（磁力）' if task.kind == 'magnet' else ''}：{url[:140]}")
        self.queue.put(task)
        self._update_counts()
        self._refresh_progress(task)

    def start_queue(self):
        self._log("队列运行中（任务会自动依次下载）")

    def pause_queue(self):
        if self.current:
            self.engine.cancel(self.current)
            self.current.status = "已暂停"
            self._refresh_task(self.current)
            self._log("已暂停当前任务（可右键“重新下载”继续）", "warn")

    def cancel_current(self):
        if self.current:
            self.engine.cancel(self.current)
            self._log("已取消当前任务", "warn")
        else:
            self._log("当前没有正在下载的任务")

    def _selected_task(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tasks.get(int(sel[0]))

    def remove_selected(self):
        task = self._selected_task()
        if not task:
            return
        if task is self.current:
            self.engine.cancel(task)
        self.tree.delete(str(task.id))
        self.tasks.pop(task.id, None)
        if task.id in self.order:
            self.order.remove(task.id)
        self._update_counts()

    def clear_finished(self):
        for tid in list(self.order):
            t = self.tasks.get(tid)
            if t and t.status in ("已完成", "已存在", "失败", "已取消", "已暂停"):
                self.tree.delete(str(tid))
                self.tasks.pop(tid, None)
                self.order.remove(tid)
        self._update_counts()
        self._log("已清除结束的任务")

    def retry_selected(self):
        task = self._selected_task()
        if not task:
            return
        task.cancelled = False
        task.attempt = 0
        task.status = "等待中"
        task.percent = 0.0
        self._refresh_task(task)
        self.queue.put(task)
        self._log(f"重新加入队列：{task.display_title}")

    def copy_selected_url(self):
        task = self._selected_task()
        if task:
            self.clipboard_clear()
            self.clipboard_append(task.url)
            self._log("已复制链接")

    def open_selected_file(self):
        task = self._selected_task()
        if task and task.path and os.path.exists(task.path):
            reveal_file(task.path)
        elif task and task.path:
            open_in_explorer(os.path.dirname(task.path))
        else:
            self.open_outdir()

    def open_selected_dir(self):
        task = self._selected_task()
        if task and task.path:
            open_in_explorer(task.path)
        else:
            self.open_outdir()

    # ---- 播放 / 在线试看 ---------------------------------------------------
    def play_selected(self):
        """播放选中的成品（内置播放器）；还没下载就直接在线播放。"""
        task = self._selected_task()
        if not task:
            self._log("先选中一个任务再播放", "warn")
            return
        path = task.path if (task.path and os.path.exists(task.path)) else ""
        if not path:
            self.preview_stream(task.url, task.quality, task.display_title)
            return
        self.play_path(path, task.display_title)

    def preview_from_entry(self):
        url = self._entry_url()
        if not url:
            task = self._selected_task()
            url = task.url if task else ""
        if not url:
            self._log("先粘贴一个链接，或选中一个任务，再点「试看」", "warn")
            return
        quality = dict(QUALITIES).get(self.quality_var.get(), "best")
        self.preview_stream(url, quality, "在线播放")

    def preview_stream(self, url, quality="best", title=""):
        """在线播放：yt-dlp 把码流直接管道给内置播放器，不落盘、下多少看多少。"""
        if not self._ensure_player():
            return
        if not self.engine.find_ytdlp():
            self._ensure_ytdlp()
            return
        exe = self.engine.find_ytdlp()
        self.show_player()
        try:
            self.player.play_stream(url, self.video_frame.winfo_id(), exe, quality,
                                    self.engine.env())
        except Exception as exc:
            self._log(f"在线播放启动失败：{exc}", "err")
            return
        self.player_title.configure(text=f"{title or '在线播放'} · {url}")
        self.player_seek.set(0)
        self.btn_play.set_icon("pause")
        self.player_time.configure(text="00:00 / 直播流")
        self._log(f"开始在线播放：{url}")

    def _on_double(self, _):
        task = self._selected_task()
        if task:
            path = task.path if (task.path and os.path.exists(task.path)) else ""
            if path:
                self.play_path(path, task.display_title)
            else:
                self.open_selected_file()

    def _on_select(self, _):
        task = self._selected_task()
        if not task:
            return
        self.detail_title.configure(text=task.display_title)
        self.detail_rows["uploader"].configure(text=task.uploader or "-")
        self.detail_rows["duration"].configure(text=human_duration(task.duration))
        self.detail_rows["status"].configure(text=task.status, fg=(
            OK_GREEN if task.status in ("已完成", "已存在") else
            ERR_RED if task.status == "失败" else TEXT))
        self.detail_rows["size"].configure(
            text=human_size(os.path.getsize(task.path)) if task.path and os.path.exists(task.path) else "-")
        self.detail_path.configure(text=task.path or self.cfg["outdir"])
        err_row = self.detail_rows["error"].master
        if task.error:
            self.detail_rows["error"].configure(text=task.error, fg=ERR_RED)
            err_row.pack(fill="x", padx=14, pady=2)
        else:
            err_row.pack_forget()
        notice_row = self.detail_rows["notice"].master
        if getattr(task, "notice", ""):
            self.detail_rows["notice"].configure(text=task.notice, fg=WARN_YELLOW)
            notice_row.pack(fill="x", padx=14, pady=2)
        else:
            notice_row.pack_forget()
        # 封面区显示视频真实封面（后台获取，拿到后会自动刷新）
        self._set_cover(task)

    def open_outdir(self):
        outdir = self.cfg["outdir"]
        os.makedirs(outdir, exist_ok=True)
        open_in_explorer(outdir)

    def open_settings(self):
        SettingsDialog(self, self.cfg)
        self.quality_var.set(dict((v, k) for k, v in QUALITIES).get(
            self.cfg["quality"], "最佳画质"))

    def open_about(self):
        AboutDialog(self, {"ytdlp": self.engine.ytdlp_version(),
                           "ffmpeg": self.engine.has_ffmpeg(),
                           "ffmpeg_ver": self.engine.ffmpeg_version()})

    # ---- 刷新 --------------------------------------------------------------
    def _refresh_task(self, task):
        iid = str(task.id)
        if not self.tree.exists(iid):
            return
        percent = f"{task.percent:.0f}%" if task.status not in ("等待中", "获取信息中…") else "-"
        speed = human_speed(task.speed)
        eta = human_eta(task.eta)
        tag = ("done" if task.status in ("已完成", "已存在") else
               "error" if task.status == "失败" else
               "active" if task.status in ("下载中", "合并中", "转换音频", "获取信息",
                                           "准备内核") else "idle")
        self.tree.item(iid, values=(task.status, task.display_title, percent, speed, eta),
                       tags=(tag,))
        if self._selected_task() is task:
            self._on_select(None)
        self._update_bottom(task)

    def _refresh_progress(self, task):
        self._refresh_task(task)

    def _update_bottom(self, task):
        """底部进度条 + 状态文字，永远跟着任务的最新状态走。"""
        status = task.status
        terminal = status in ("已完成", "已存在", "失败", "已取消") or status.startswith("重试中")
        if task is not self.current and not terminal:
            return          # 排队中的任务不要抢底部状态栏
        if status in ("已完成", "已存在"):
            self.progress.set(100)
            head = f"✓ {status}"
            if getattr(task, "notice", ""):
                head += "（试看片段）"
            self.now_label.configure(text=f"{head}：{task.display_title}")
            self.metrics_label.configure(text="")
            return
        if status == "失败":
            self.progress.set(0)
            reason = (task.error or "").split("\n")[0][:70]
            self.now_label.configure(text=f"✗ 失败：{task.display_title}（{reason}）")
            self.metrics_label.configure(text="")
            return
        if status == "已取消":
            self.progress.set(task.percent)
            self.now_label.configure(text=f"已取消：{task.display_title}")
            self.metrics_label.configure(text="")
            return
        if status.startswith("重试中"):
            self.progress.set(task.percent)
            self.now_label.configure(text=f"{status}：{task.display_title}")
            self.metrics_label.configure(text="")
            return
        # 进行中的状态（等待中 / 获取信息 / 下载中 / 合并中 / 转换音频）
        self.progress.set(task.percent, indeterminate=(task.percent <= 0))
        self.now_label.configure(text=f"正在下载：{task.display_title}")
        bits = []
        if task.percent > 0:
            bits.append(f"{task.percent:5.1f}%")
        if task.speed:
            bits.append(human_speed(task.speed))
        if task.eta:
            bits.append(f"剩余 {human_eta(task.eta)}")
        self.metrics_label.configure(text="  •  ".join(bits))

    def _update_counts(self):
        total = len(self.order)
        done = sum(1 for t in self.tasks.values() if t.status in ("已完成", "已存在"))
        failed = sum(1 for t in self.tasks.values() if t.status == "失败")
        self.count_label.configure(text=f"共 {total} 个任务 · 完成 {done} · 失败 {failed}")
        self.status_label.configure(text="就绪" if self.current is None else
                                    f"正在处理：{self.current.display_title}")
        try:
            if self.order:
                self.empty_hint.place_forget()
            else:
                self.empty_hint.place(relx=0.5, rely=0.42, anchor="center")
                self.empty_hint.lift()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 自检模式（无界面，真实下载一个短视频验证整条链路）
# ----------------------------------------------------------------------------
def run_selftest(url=None, quality="480"):
    url = url or "https://www.bilibili.com/video/BV1xx411c7mD"
    cfg = Config()
    cfg["outdir"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "selftest_output")
    cfg["retries"] = 2
    engine = Engine(cfg, emit=lambda kind, *a, **k: None)

    print(f"[selftest] yt-dlp: {engine.find_ytdlp() or '未找到'}")
    print(f"[selftest] 版本: {engine.ytdlp_version() or '未知'}")
    print(f"[selftest] ffmpeg: {'OK' if engine.has_ffmpeg() else '未检测到'}")

    task = Task(url, quality)

    def on_progress(t):
        sys.stdout.write(f"\r[selftest] {t.status} {t.percent:5.1f}% "
                         f"{human_speed(t.speed):>12} 剩余 {human_eta(t.eta) or '-':>6}   ")
        sys.stdout.flush()

    engine.emit = lambda kind, payload=None, level="info": None
    for attempt in (1, 2, 3):
        ok, msg = engine.download(task, on_progress=on_progress)
        print()
        if ok:
            break
        if task.cancelled:
            break
        print(f"[selftest] 第 {attempt} 次失败：{msg}，重试…")
        time.sleep(2)

    if ok:
        path = task.path
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        print(f"[selftest] 文件: {path} ({human_size(size)})")
        print("[selftest] PASS" if size > 0 else "[selftest] FAIL: 文件为空")
        return 0 if size > 0 else 1
    print(f"[selftest] FAIL: {msg}")
    return 1


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def enable_dpi_awareness():
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    args = [a for a in sys.argv[1:]]
    if "--version" in args:
        print(f"{APP_NAME} {VERSION}")
        return 0
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--selftest" in args:
        idx = args.index("--selftest")
        rest = [a for a in args[idx + 1:] if not a.startswith("-")]
        return run_selftest(rest[0] if rest else None)

    enable_dpi_awareness()
    initial = next((a for a in args if a.startswith(("http", "magnet:"))), None)
    initial_play = None
    if "--play" in args:
        idx = args.index("--play")
        if idx + 1 < len(args):
            initial_play = args[idx + 1]
    app = App(initial_url=initial, initial_play=initial_play)

    def on_close():
        app.stop_flag = True
        try:
            app.player.stop()
        except Exception:
            pass
        if app.current and app.current.proc:
            try:
                app.current.proc.terminate()
            except Exception:
                pass
        try:
            app.cfg.save()
        except Exception:
            pass
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
