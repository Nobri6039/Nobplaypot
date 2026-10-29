"""Nobplaypot — 基于 VLC(libvlc) + Tkinter 的深色视频播放器。

作者: DeepSeek V4.1 Flash (AI 助手) · 发布/版权: Nobri6039 · MIT License. 内置 VLC 运行库遵循 GPLv2+。

界面为全自绘的深色主题(不使用 ttk 默认浅色控件):
  * 按钮 / 滑杆 / 下拉框 / 文件列表 / 提示框全部 Canvas 自绘, 无系统浅色组件
  * 深色标题栏(Win10/11 DWM), 深色自绘文件选择框
  * 拖入视频文件, 或通过自绘文件选择框打开
  * 播放 / 暂停, 进度拖动, 音量, 播放速度
  * 流式传输: 左下角按钮 -> 主持流式 / 加入流式
  * 快捷键: 空格 播放暂停, ←/→ 快退/快进 5 秒, ↑/↓ 音量, F 全屏, Esc 退出全屏, Ctrl+L 日志目录

流式传输:
  * 主持: 开放 TCP 指令端口(默认 5000)与 TCP 媒体端口(默认 5001);
    客户端先经指令端口同步身份与播放状态(文件/进度/总长/暂停/倍率)
  * 媒体流: 主持端 VLC 转码为 H.264/TS 后先送到本机中转, 再由应用层
    通过可靠 TCP 逐客户端分发; 客户端接收后在本机重放给 VLC 渲染,
    弱网时由 TCP 重传 + 本机缓冲吸收抖动, 避免裸 UDP 丢包直接黑屏
  * 加入: 输入指令地址与主持流地址(如 192.168.1.10:5000 / 192.168.1.10:5001),
    客户端主动连接主持的媒体端口, 本机不开放入站端口; 播放/暂停/倍速等控制
    只把指令发回主持端执行, 主持处理后再把新状态的流推过来; 音量/静音属于本机
  * 推流方式: 主持端播放器直接挂 sout 推流转码到本机中转; 客户端进出由
    可靠流分发器处理, 不重建推流链; 客户端离开时保持当前播放(残留推流稍后剥离)
  * 跳转: 推流中拖动进度由主持端中断推流, 从新进度重建推流(流时间轴从 0 开始),
    然后广播 resync 让客户端重连画面; 客户端本身的进度条只把指令发给主持
  * 限制: libvlc 的 sout 链原地 seek 会永久断流, 因此推流中的跳转走"重建推流"
  * 稳定性: libvlc 停止输入/释放播放器偶尔会阻塞很久, 客户端退出与会话
    关闭都把这类回收放到后台线程, 界面线程不做可能阻塞的 VLC 调用
  * 房间密码: 主持可开启(默认开启)房间密码; 客户端加入时由主持端核验,
    密码只保存在主持端, 不会随房间信息下发给客户端
  * 指令端口使用 TCP + 换行分隔的 JSON 消息; 心跳 2s, 12s 无消息判定离线

日志:
  * 位置 %APPDATA%\\Nobplaypot\\logs\\nobplaypot.log(按 2MB 轮转, 保留 3 份)
  * 可通过环境变量 NOBPLAYPOT_LOG_LEVEL=DEBUG 提高详细程度
  * 也可用 NOBPLAYPOT_LOG_FILE 指定日志文件位置

依赖: pip install python-vlc tkinterdnd2
"""

import hashlib
import hmac
import http.server
import json
import logging
import math
import os
import platform
import queue
import random
import socket
import socketserver
import subprocess
import sys
import struct
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import urllib.parse
import uuid
from logging.handlers import RotatingFileHandler

APP_TITLE = "Nobplaypot"
APP_VERSION = "v1.2.2"
CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "Nobplaypot"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(**values):
    data = load_config()
    data.update(values)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        logging.getLogger("nobplaypot").debug("保存配置失败", exc_info=True)


VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".rmvb", ".rm", ".3gp", ".ogv", ".vob",
)
SPEED_OPTIONS = ("0.25x", "0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "1.75x", "2.0x", "3.0x", "4.0x")
SEEK_STEP_MS = 5000
UI_TICK_MS = 200
WINDOW_SIZE = (1120, 720)
MIN_SIZE = (780, 520)

# 流式传输协议常量
STREAM_VERSION = 1
STREAM_DEFAULT_CMD_PORT = 5000
STREAM_DEFAULT_UDP_PORT = 5001
STREAM_MAX_CLIENTS = 32
STREAM_HEARTBEAT_S = 2.0
STREAM_STALE_S = 12.0
CONFIG_VERSION = 4
STREAM_MAX_MSG_BYTES = 65536
STREAM_PASSWORD_ATTEMPTS = 5
CHAT_MAX_CHARS = 200
DANMAKU_MAX_CHARS = 60


def password_matches(provided, expected):
    """常量时间比较房间密码, 两边都按 UTF-8 字节比较。"""
    if not expected:
        return True
    try:
        return hmac.compare_digest(
            str(provided or "").encode("utf-8"),
            str(expected).encode("utf-8"),
        )
    except (AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 设计令牌: 深色 + 单一强调色, 保持所有界面元素同一套色板
# ---------------------------------------------------------------------------
class C:
    BG = "#0A0C11"          # 窗口底色
    VIDEO = "#04050A"       # 视频区(纯黑, 避免和 UI 抢色)
    DECK = "#0D1017"        # 控制栏
    CARD = "#101521"        # 空状态卡片
    SURFACE = "#151A24"     # 输入框 / 悬浮层
    SURFACE_HI = "#1D2431"  # 悬浮态
    STROKE = "#232B3A"      # 描边
    STROKE_SOFT = "#232C3C"  # 弱描边 / 轨道
    TEXT = "#E9ECF4"
    TEXT_MUTED = "#9AA3B8"
    TEXT_DIM = "#5F687C"
    ACCENT = "#6C8BFF"
    ACCENT_HOVER = "#88A2FF"
    ACCENT_DOWN = "#5875E8"
    ACCENT_SOFT = "#1A2440"
    DANGER = "#FF6B7A"
    OK = "#46C58C"


# ---------------------------------------------------------------------------
# 设置: 默认值 / 外观主题 / 读写
# ---------------------------------------------------------------------------
DANMAKU_COLORS = {
    "white": ("白色", "#FFFFFF"),
    "yellow": ("黄色", "#FFE45C"),
    "cyan": ("青色", "#6FF2FF"),
    "pink": ("粉色", "#FF9BCE"),
    "green": ("绿色", "#8CFFB0"),
    "orange": ("橙色", "#FFB86B"),
    "blue": ("蓝色", "#8FB8FF"),
    "violet": ("紫色", "#C9A7FF"),
}

DANMAKU_AREAS = (
    (100, "全屏"),
    (50, "上方 50%"),
    (40, "上方 40%"),
    (30, "上方 30%"),
    (20, "上方 20%"),
)

DEFAULT_SETTINGS = {
    # 常规
    "username": "",
    "decode_mode": "auto",      # auto / hardware / software
    "volume": 80,
    "seek_step": 5,             # 快进/快退秒数
    "autoplay": True,
    "remember_dir": True,
    "log_level": "INFO",
    # 外观
    "accent": "aurora",
    "surface": "midnight",
    "animations": True,
    # 流式传输
    "max_clients": 32,
    "chat_limit": 200,
    "chat_rate_limit": 1,
    "danmaku": True,
    "danmaku_size": 20,
    "danmaku_speed": 100,
    "danmaku_color": "white",
    "danmaku_area": 100,
    "bandwidth": 2200,          # kbps
    "resolution": "native",     # 480p / 720p / 1080p / native
    "fps": 30,
    "audio_bitrate": 128,
    "preset": "veryfast",
    "keyint": 24,
    "client_caching": 400,      # ms
    "buffer_scale": 1.0,        # 缓冲提示相对于视频大小的缩放倍数
    "heartbeat": 2.0,           # s
    "stale": 12.0,              # s
    "cmd_port": STREAM_DEFAULT_CMD_PORT,
    "udp_port": STREAM_DEFAULT_UDP_PORT,
}

SETTINGS_RANGES = {
    "volume": (0, 100),
    "seek_step": (1, 30),
    "max_clients": (2, 32),
    "chat_limit": (20, 500),
    "danmaku_size": (12, 36),
    "danmaku_speed": (50, 300),
    "bandwidth": (300, 12000),
    "fps": (10, 60),
    "audio_bitrate": (64, 320),
    "keyint": (10, 120),
    "client_caching": (200, 5000),
    "buffer_scale": (0.5, 2.5),
    "heartbeat": (1.0, 10.0),
    "stale": (4.0, 60.0),
    "cmd_port": (1, 65535),
    "udp_port": (1, 65535),
}

ACCENTS = {
    "aurora": ("#6C8BFF", "#88A2FF", "#5875E8", "#1A2440"),
    "violet": ("#A780FF", "#C0A4FF", "#8A64E8", "#241C40"),
    "teal": ("#3ECFB2", "#6FE3CB", "#2FAE95", "#12302B"),
    "rose": ("#FF7A9C", "#FFA0B8", "#E85C80", "#3A1723"),
    "amber": ("#F5B44B", "#FFC973", "#D99A33", "#33260F"),
}

ACCENT_LABELS = {
    "aurora": "极光蓝", "violet": "紫罗兰", "teal": "青碧",
    "rose": "玫瑰", "amber": "琥珀",
}

SURFACES = {
    "midnight": {
        "BG": "#0A0C11", "VIDEO": "#04050A", "DECK": "#0D1017", "CARD": "#101521",
        "SURFACE": "#151A24", "SURFACE_HI": "#1D2431", "STROKE": "#232B3A",
        "STROKE_SOFT": "#232C3C",
    },
    "black": {
        "BG": "#060608", "VIDEO": "#000000", "DECK": "#0A0A0C", "CARD": "#0E0E11",
        "SURFACE": "#141416", "SURFACE_HI": "#1C1C1F", "STROKE": "#26262A",
        "STROKE_SOFT": "#232327",
    },
    "slate": {
        "BG": "#0F1216", "VIDEO": "#080A0C", "DECK": "#12161B", "CARD": "#161B21",
        "SURFACE": "#1B2128", "SURFACE_HI": "#232B34", "STROKE": "#2B333D",
        "STROKE_SOFT": "#2A323B",
    },
}

SURFACE_LABELS = {"midnight": "深蓝黑", "black": "纯黑", "slate": "岩灰"}

ANIMATIONS_ENABLED = True

DECODE_LABELS = {"auto": "自动", "hardware": "硬件解码", "software": "软件解码"}
# 推流分辨率(按视频高度, 不会放大)
RESOLUTION_OPTIONS = (("480p", "480p"), ("720p", "720p"),
                      ("1080p", "1080p"), ("native", "原生"))
RESOLUTION_HEIGHTS = {"480p": 480, "720p": 720, "1080p": 1080, "native": 0}
PRESET_OPTIONS = ("ultrafast", "superfast", "veryfast", "faster", "medium")


def _clamp_setting(key, value, default):
    if key not in SETTINGS_RANGES:
        return default
    low, high = SETTINGS_RANGES[key]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    number = max(low, min(high, number))
    result = int(round(number))
    if key in ("heartbeat", "stale"):
        return float(round(number, 1))
    return result



def default_username():
    """默认用户名: 计算机名; 不足 4 个字符时补随机数字, 最长 32。"""
    try:
        name = (socket.gethostname() or "").strip()
    except OSError:
        name = ""
    if not name:
        name = "user"
    if len(name) < 4:
        name += "".join(random.choice("0123456789")
                        for _ in range(4 - len(name)))
    return name[:32]


def clean_username(value):
    name = str(value or "").strip()
    if 4 <= len(name) <= 32:
        return name
    return default_username()


def normalize_settings(data):
    """把任意字典整理成合法的设置项(缺失补默认, 越界裁剪)。"""
    data = data if isinstance(data, dict) else {}
    result = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        value = data.get(key, default)
        if key in SETTINGS_RANGES:
            value = _clamp_setting(key, value, default)
        result[key] = value
    result["username"] = clean_username(result.get("username"))
    try:
        rate_limit = int(result.get("chat_rate_limit", 1))
    except (TypeError, ValueError):
        rate_limit = 1
    if rate_limit not in (0, 1, 2, 3, 5):
        rate_limit = 1
    result["chat_rate_limit"] = rate_limit
    if result["decode_mode"] not in DECODE_LABELS:
        result["decode_mode"] = DEFAULT_SETTINGS["decode_mode"]
    if result["accent"] not in ACCENTS:
        result["accent"] = DEFAULT_SETTINGS["accent"]
    if result["surface"] not in SURFACES:
        result["surface"] = DEFAULT_SETTINGS["surface"]
    if result["resolution"] not in RESOLUTION_HEIGHTS:
        result["resolution"] = DEFAULT_SETTINGS["resolution"]
    if result["preset"] not in PRESET_OPTIONS:
        result["preset"] = DEFAULT_SETTINGS["preset"]
    if result["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        result["log_level"] = DEFAULT_SETTINGS["log_level"]
    if result.get("danmaku_color") not in DANMAKU_COLORS:
        result["danmaku_color"] = DEFAULT_SETTINGS["danmaku_color"]
    try:
        area = int(result.get("danmaku_area", 100))
    except (TypeError, ValueError):
        area = 100
    if area not in [value for value, _label in DANMAKU_AREAS]:
        area = 100
    result["danmaku_area"] = area
    for key in ("autoplay", "remember_dir", "animations", "danmaku"):
        result[key] = bool(result[key])
    return result


def load_settings():
    config = load_config()
    raw = dict(config.get("settings") or {})
    legacy = config.get("settings_version") != CONFIG_VERSION
    if legacy:
        # 1x 已改为流复制, 旧配置的大缓冲一次性降到 400ms 以降低延迟
        try:
            if int(raw.get("client_caching", 400)) > 800:
                raw["client_caching"] = 400
        except (TypeError, ValueError):
            pass
        if raw.get("keyint") == 30:
            raw["keyint"] = 24
        try:
            if abs(float(raw.get("stale", 8.0)) - 8.0) < 1e-6:
                raw["stale"] = 12.0
        except (TypeError, ValueError):
            pass
    settings = normalize_settings(raw)
    if legacy:
        save_config(settings_version=CONFIG_VERSION, settings=settings)
    return settings


def save_settings(settings):
    save_config(settings=settings)


def apply_appearance(settings):
    """按设置套用界面底色与强调色(需在创建控件前调用)。"""
    surface = SURFACES.get(settings.get("surface"), SURFACES["midnight"])
    for key, value in surface.items():
        setattr(C, key, value)
    accent = ACCENTS.get(settings.get("accent"), ACCENTS["aurora"])
    C.ACCENT, C.ACCENT_HOVER, C.ACCENT_DOWN, C.ACCENT_SOFT = accent
    global ANIMATIONS_ENABLED
    ANIMATIONS_ENABLED = bool(settings.get("animations", True))


def _rgb(color):
    text = color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def mix(color_a, color_b, t):
    """在两个 #RRGGBB 之间插值, t=0 -> a, t=1 -> b。"""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    r1, g1, b1 = _rgb(color_a)
    r2, g2, b2 = _rgb(color_b)
    return "#%02x%02x%02x" % (
        round(r1 + (r2 - r1) * t),
        round(g1 + (g2 - g1) * t),
        round(b1 + (b2 - b1) * t),
    )


def clamp(value, low, high):
    return low if value < low else (high if value > high else value)


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
_UI_FAMILY = "Microsoft YaHei UI"
_MONO_FAMILY = "Consolas"
_FONTS_READY = False


def init_fonts(root):
    global _UI_FAMILY, _MONO_FAMILY, _FONTS_READY
    if _FONTS_READY:
        return
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        families = set()
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Tahoma"):
        if family in families:
            _UI_FAMILY = family
            break
    for family in ("Cascadia Mono", "Consolas", "Lucida Console", "Courier New"):
        if family in families:
            _MONO_FAMILY = family
            break
    _FONTS_READY = True


def ui_font(size=10, bold=False):
    return (_UI_FAMILY, size, "bold" if bold else "normal")


def mono_font(size=10, bold=False):
    return (_MONO_FAMILY, size, "bold" if bold else "normal")


# ---------------------------------------------------------------------------
# 绘图辅助
# ---------------------------------------------------------------------------
def round_rect(canvas, x0, y0, x1, y1, radius, steps=14, **kwargs):
    """在 Canvas 上画圆角矩形(闭合多边形), 返回 item id。"""
    radius = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    points = []
    for cx, cy, start in (
        (x1 - radius, y0 + radius, -90),
        (x1 - radius, y1 - radius, 0),
        (x0 + radius, y1 - radius, 90),
        (x0 + radius, y0 + radius, 180),
    ):
        for index in range(steps + 1):
            angle = math.radians(start + 90.0 * index / steps)
            points.append(cx + radius * math.cos(angle))
            points.append(cy + radius * math.sin(angle))
    kwargs.setdefault("joinstyle", "round")
    return canvas.create_polygon(points, **kwargs)


def format_size(num_bytes):
    if num_bytes <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return ("%.0f %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return ""


# ---------------------------------------------------------------------------
# 图标: 用纯 Python 光栅化(4x 超采样)生成带抗锯齿的 RGBA PNG, 再由 Tk 渲染
# (Canvas 原生图元没有抗锯齿, 直接画会很明显的锯齿)
# ---------------------------------------------------------------------------
def _png_bytes(width, height, rgba):
    """编码 8 位 RGBA PNG(Tk 8.6+ 的 PhotoImage 可直接加载)。"""
    import struct
    import zlib

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _hex_rgb(color):
    if not color:
        return None
    text = str(color).strip()
    if not text.startswith("#"):
        try:
            return _rgb(text)
        except Exception:
            return None
    text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) < 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _poly_bbox(points, pad=0.0):
    xs = points[0::2]
    ys = points[1::2]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _arc_points(x0, y0, x1, y1, start, extent, steps=36):
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    rx = (x1 - x0) / 2.0
    ry = (y1 - y0) / 2.0
    points = []
    for index in range(steps + 1):
        angle = math.radians(start + extent * index / steps)
        points.append(cx + rx * math.cos(angle))
        points.append(cy - ry * math.sin(angle))
    return points


class _IconPainter:
    """记录与 Canvas 同名的绘图调用, 之后统一光栅化。"""

    def __init__(self):
        self.shapes = []

    def _append(self, kind, data, fill, outline, width, bbox):
        self.shapes.append({
            "kind": kind, "data": data,
            "fill": _hex_rgb(fill), "outline": _hex_rgb(outline),
            "width": max(0.4, float(width or 1.0)), "bbox": bbox,
        })

    def create_polygon(self, *args, fill=None, outline=None, width=1, **_kwargs):
        points = args[0] if len(args) == 1 else args
        if points and isinstance(points[0], (list, tuple)):
            points = points[0]
        flat = [float(value) for value in points]
        pad = float(width or 1.0) / 2.0
        self._append("poly", flat, fill, outline, width, _poly_bbox(flat, pad))
        return None

    def create_line(self, *args, fill=None, width=1, **_kwargs):
        points = args[0] if len(args) == 1 else args
        if points and isinstance(points[0], (list, tuple)):
            points = points[0]
        flat = [float(value) for value in points]
        pad = float(width or 1.0) / 2.0 + 0.5
        self._append("line", flat, fill, fill, width, _poly_bbox(flat, pad))
        return None

    def create_oval(self, x0, y0, x1, y1, fill=None, outline=None, width=1, **_kwargs):
        pad = float(width or 1.0) / 2.0 + 0.5
        bbox = (min(x0, x1) - pad, min(y0, y1) - pad,
                max(x0, x1) + pad, max(y0, y1) + pad)
        self._append("oval", (float(x0), float(y0), float(x1), float(y1)),
                     fill, outline, width, bbox)
        return None

    def create_rectangle(self, x0, y0, x1, y1, fill=None, outline=None, width=1, **_kwargs):
        pad = float(width or 1.0) / 2.0
        bbox = (min(x0, x1) - pad, min(y0, y1) - pad,
                max(x0, x1) + pad, max(y0, y1) + pad)
        self._append("rect", (float(x0), float(y0), float(x1), float(y1)),
                     fill, outline, width, bbox)
        return None

    def create_arc(self, x0, y0, x1, y1, start=0.0, extent=90.0, style="pieslice",
                   outline=None, fill=None, width=1, **_kwargs):
        points = _arc_points(x0, y0, x1, y1, float(start), float(extent))
        pad = float(width or 1.0) / 2.0 + 0.5
        self._append("line", points, outline or fill, outline or fill, width,
                     _poly_bbox(points, pad))
        return None


def _point_in_poly(px, py, points):
    count = len(points) // 2
    inside = False
    j = count - 1
    for i in range(count):
        xi, yi = points[2 * i], points[2 * i + 1]
        xj, yj = points[2 * j], points[2 * j + 1]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / length2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _near_poly_edge(px, py, points, half_width):
    count = len(points) // 2
    for i in range(count):
        j = (i + 1) % count
        if _dist_to_segment(px, py, points[2 * i], points[2 * i + 1],
                            points[2 * j], points[2 * j + 1]) <= half_width:
            return True
    return False


def _shape_color(shape, px, py):
    kind = shape["kind"]
    data = shape["data"]
    half = shape["width"] / 2.0
    if kind == "poly":
        if _near_poly_edge(px, py, data, half):
            return shape["outline"]
        if _point_in_poly(px, py, data):
            return shape["fill"]
        return None
    if kind == "line":
        count = len(data) // 2
        for i in range(count - 1):
            if _dist_to_segment(px, py, data[2 * i], data[2 * i + 1],
                                data[2 * i + 2], data[2 * i + 3]) <= half:
                return shape["outline"]
        return None
    if kind == "oval":
        x0, y0, x1, y1 = data
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
        if rx <= 0.001 or ry <= 0.001:
            return None
        nx = (px - cx) / rx
        ny = (py - cy) / ry
        dist = math.sqrt(nx * nx + ny * ny)
        if shape["outline"] and abs(dist - 1.0) * min(rx, ry) <= half:
            return shape["outline"]
        if dist <= 1.0:
            return shape["fill"]
        return None
    if kind == "rect":
        x0, y0, x1, y1 = data
        left, right = min(x0, x1), max(x0, x1)
        top, bottom = min(y0, y1), max(y0, y1)
        if (left - half <= px <= right + half) and (top - half <= py <= bottom + half):
            edge = (abs(px - left) <= half or abs(px - right) <= half
                    or abs(py - top) <= half or abs(py - bottom) <= half)
            if edge and shape["outline"]:
                return shape["outline"]
            if left <= px <= right and top <= py <= bottom:
                return shape["fill"]
        return None
    return None


def _rasterize_rgba(shapes, width, height, samples=4):
    """把记录的形状渲染成 RGBA 像素缓冲(测试与 PhotoImage 共用)。"""
    w = max(1, int(math.ceil(width)))
    h = max(1, int(math.ceil(height)))
    rgba = bytearray(w * h * 4)
    total = samples * samples
    inv = 1.0 / total
    for j in range(h):
        base_y = j - h / 2.0
        row = j * w
        for i in range(w):
            base_x = i - w / 2.0
            red = green = blue = 0.0
            hits = 0
            for sy in range(samples):
                py = base_y + (sy + 0.5) / samples
                for sx in range(samples):
                    px = base_x + (sx + 0.5) / samples
                    color = None
                    for shape in shapes:
                        x0, y0, x1, y1 = shape["bbox"]
                        if px < x0 or px > x1 or py < y0 or py > y1:
                            continue
                        found = _shape_color(shape, px, py)
                        if found is not None:
                            color = found
                    if color is not None:
                        hits += 1
                        red += color[0]
                        green += color[1]
                        blue += color[2]
            if hits:
                index = (row + i) * 4
                rgba[index] = int(red / hits)
                rgba[index + 1] = int(green / hits)
                rgba[index + 2] = int(blue / hits)
                rgba[index + 3] = int(255 * hits * inv)
    return bytes(rgba), w, h


def _rasterize(shapes, width, height, master, samples=4):
    """把记录的形状渲染成带 alpha 的 PNG 图片。"""
    rgba, w, h = _rasterize_rgba(shapes, width, height, samples)
    return tk.PhotoImage(master=master, data=_png_bytes(w, h, rgba))


_ICON_BOX_FACTOR = 1.32


def _icon_photo(canvas, kind, size, color, params):
    cache = getattr(canvas, "_icon_cache", None)
    if cache is None:
        cache = {}
        canvas._icon_cache = cache
    key = (kind, round(float(size), 1), color, tuple(sorted(params.items())))
    image = cache.get(key)
    if image is None:
        painter = _IconPainter()
        _ICON_BUILDERS[kind](painter, 0.0, 0.0, float(size), color, **params)
        box = float(size) * _ICON_BOX_FACTOR
        image = _rasterize(painter.shapes, box, box, canvas)
        cache[key] = image
    return image


def _paint_icon(canvas, kind, cx, cy, size, color, **params):
    image = _icon_photo(canvas, kind, size, color, params)
    box = float(size) * _ICON_BOX_FACTOR
    canvas.create_image(cx - box / 2.0, cy - box / 2.0, anchor="nw", image=image)
    return image


def _clamp01(value):
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _disc_rgba(diameter, fill, outline=None, width=1.0):
    """解析式抗锯齿圆(按像素到圆心的距离算覆盖率, 比超采样快很多)。"""
    if outline and fill and _same_color(outline, fill):
        outline = None
    radius = max(0.6, float(diameter) / 2.0)
    size = max(3, int(math.ceil(diameter)) + 2)
    center = size / 2.0
    fill_rgb = _hex_rgb(fill) or (0, 0, 0)
    stroke_rgb = _hex_rgb(outline) if outline else None
    inner = radius - (width if stroke_rgb else 0.0)
    rgba = bytearray(size * size * 4)
    for j in range(size):
        for i in range(size):
            distance = math.hypot(i + 0.5 - center, j + 0.5 - center)
            fill_cov = _clamp01(inner - distance + 0.5)
            stroke_cov = 0.0
            if stroke_rgb:
                stroke_cov = _clamp01(
                    width / 2.0 - abs(distance - (radius - width / 2.0)) + 0.5
                )
            if fill_cov <= 0.0 and stroke_cov <= 0.0:
                continue
            if stroke_rgb and stroke_cov > fill_cov:
                color, alpha = stroke_rgb, stroke_cov
            else:
                color, alpha = fill_rgb, fill_cov
            index = (j * size + i) * 4
            rgba[index] = color[0]
            rgba[index + 1] = color[1]
            rgba[index + 2] = color[2]
            rgba[index + 3] = int(255 * _clamp01(alpha))
    return rgba, size


def _circle_photo(canvas, diameter, fill, outline=None, width=1):
    """抗锯齿圆(用于滑块圆点/徽标)。"""
    cache = getattr(canvas, "_circle_cache", None)
    if cache is None:
        cache = {}
        canvas._circle_cache = cache
    key = (round(float(diameter), 2), fill, outline, round(float(width), 2))
    image = cache.get(key)
    if image is not None:
        return image
    rgba, size = _disc_rgba(diameter, fill, outline, width)
    image = tk.PhotoImage(master=canvas, data=_png_bytes(size, size, bytes(rgba)))
    cache[key] = image
    return image


def _corner_photo(canvas, radius, fill, outline, width, corner):
    """圆角矩形的一个角(0=左上 1=右上 2=左下 3=右下)。"""
    if outline and fill and _same_color(outline, fill):
        outline = ""
    cache = getattr(canvas, "_corner_cache", None)
    if cache is None:
        cache = {}
        canvas._corner_cache = cache
    key = (round(float(radius), 2), fill, outline, round(float(width), 2), corner)
    image = cache.get(key)
    if image is not None:
        return image
    size = max(2, int(math.ceil(radius)))
    fill_rgb = _hex_rgb(fill) or (0, 0, 0)
    stroke_rgb = _hex_rgb(outline) if outline else None
    inner = radius - (width if stroke_rgb else 0.0)
    center_x = radius if not (corner & 1) else size - radius
    center_y = radius if not (corner & 2) else size - radius
    rgba = bytearray(size * size * 4)
    for j in range(size):
        for i in range(size):
            distance = math.hypot(i + 0.5 - center_x, j + 0.5 - center_y)
            fill_cov = _clamp01(inner - distance + 0.5)
            stroke_cov = 0.0
            if stroke_rgb:
                stroke_cov = _clamp01(
                    width / 2.0 - abs(distance - (radius - width / 2.0)) + 0.5
                )
            if fill_cov <= 0.0 and stroke_cov <= 0.0:
                continue
            if stroke_rgb and stroke_cov > fill_cov:
                color, alpha = stroke_rgb, stroke_cov
            else:
                color, alpha = fill_rgb, fill_cov
            index = (j * size + i) * 4
            rgba[index] = color[0]
            rgba[index + 1] = color[1]
            rgba[index + 2] = color[2]
            rgba[index + 3] = int(255 * _clamp01(alpha))
    image = tk.PhotoImage(master=canvas, data=_png_bytes(size, size, bytes(rgba)))
    cache[key] = image
    return image


def _same_color(color_a, color_b):
    rgb_a = _hex_rgb(color_a)
    rgb_b = _hex_rgb(color_b)
    return rgb_a is not None and rgb_a == rgb_b


def _round_rect_rgba(width, height, radius, fill, outline, stroke_width):
    """按圆角矩形的有符号距离场渲染整块(完全连续的抗锯齿边缘)。"""
    w = max(1, int(round(width)))
    h = max(1, int(round(height)))
    radius = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    fill_rgb = _hex_rgb(fill)
    stroke_rgb = _hex_rgb(outline) if outline else None
    stroke = float(stroke_width) if stroke_rgb else 0.0
    inner = radius
    half_w = w / 2.0
    half_h = h / 2.0
    rgba = bytearray(w * h * 4)
    for j in range(h):
        py = j + 0.5 - half_h
        for i in range(w):
            px = i + 0.5 - half_w
            qx = abs(px) - (half_w - inner)
            qy = abs(py) - (half_h - inner)
            outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
            inside = min(max(qx, qy), 0.0)
            distance = outside + inside - inner
            fill_cov = _clamp01(0.5 - (distance + stroke))
            stroke_cov = 0.0
            if stroke_rgb:
                stroke_cov = _clamp01(stroke / 2.0 - abs(distance + stroke / 2.0) + 0.5)
            if fill_cov <= 0.0 and stroke_cov <= 0.0:
                continue
            if stroke_rgb and stroke_cov > fill_cov:
                color, alpha = stroke_rgb, stroke_cov
            else:
                color, alpha = (fill_rgb or (0, 0, 0)), fill_cov
            index = (j * w + i) * 4
            rgba[index] = color[0]
            rgba[index + 1] = color[1]
            rgba[index + 2] = color[2]
            rgba[index + 3] = int(255 * _clamp01(alpha))
    return rgba, w, h


def _round_rect_photo(canvas, width, height, radius, fill, outline, stroke_width):
    cache = getattr(canvas, "_round_rect_cache", None)
    if cache is None:
        cache = {}
        canvas._round_rect_cache = cache
    key = (int(round(width)), int(round(height)), round(float(radius), 2),
           fill, outline, round(float(stroke_width), 2))
    image = cache.get(key)
    if image is not None:
        return image
    rgba, w, h = _round_rect_rgba(width, height, radius, fill, outline, stroke_width)
    image = tk.PhotoImage(master=canvas, data=_png_bytes(w, h, bytes(rgba)))
    cache[key] = image
    return image


def draw_round_rect(canvas, x0, y0, x1, y1, radius, fill=None, outline=None,
                    width=1, tags=()):
    """抗锯齿圆角矩形。

    小控件直接整块渲染成一张图片(边缘连续, 没有拼接缝隙);
    大卡片仍用四角 + 直边拼接, 避免整块逐像素渲染太慢。
    """
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    fill = fill or ""
    outline = outline or ""
    if fill and outline and _same_color(fill, outline):
        outline = ""
    radius = float(radius)
    if radius < 0.5 or x1 - x0 < 2 or y1 - y0 < 2:
        return canvas.create_rectangle(x0, y0, x1, y1, fill=fill,
                                       outline=outline, width=width, tags=tags)
    radius = min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    if fill or outline:
        if (x1 - x0) * (y1 - y0) <= 70000:
            image = _round_rect_photo(canvas, x1 - x0, y1 - y0, radius,
                                      fill, outline, width)
            canvas.create_image(x0, y0, anchor="nw", image=image, tags=tags)
            return None
        if fill:
            if x1 - x0 > 2 * radius + 0.4:
                canvas.create_rectangle(x0 + radius, y0, x1 - radius, y1,
                                        fill=fill, outline="", tags=tags)
            if y1 - y0 > 2 * radius + 0.4:
                canvas.create_rectangle(x0, y0 + radius, x1, y1 - radius,
                                        fill=fill, outline="", tags=tags)
        size = max(2, int(math.ceil(radius)))
        positions = ((x0, y0), (x1 - size, y0), (x0, y1 - size), (x1 - size, y1 - size))
        for corner, (px, py) in enumerate(positions):
            image = _corner_photo(canvas, radius, fill, outline, width, corner)
            canvas.create_image(px, py, anchor="nw", image=image, tags=tags)
        if outline:
            if x1 - x0 > 2 * radius + 0.4:
                canvas.create_line(x0 + radius, y0 + 0.5, x1 - radius, y0 + 0.5,
                                   fill=outline, width=width, tags=tags)
                canvas.create_line(x0 + radius, y1 - 0.5, x1 - radius, y1 - 0.5,
                                   fill=outline, width=width, tags=tags)
            if y1 - y0 > 2 * radius + 0.4:
                canvas.create_line(x0 + 0.5, y0 + radius, x0 + 0.5, y1 - radius,
                                   fill=outline, width=width, tags=tags)
                canvas.create_line(x1 - 0.5, y0 + radius, x1 - 0.5, y1 - radius,
                                   fill=outline, width=width, tags=tags)
    return None


def _glow_photo(canvas, radius, color, max_alpha=0.38):
    """径向渐隐光晕(滑块悬停时的柔和光圈)。"""
    cache = getattr(canvas, "_glow_cache", None)
    if cache is None:
        cache = {}
        canvas._glow_cache = cache
    key = (round(float(radius), 2), color, round(float(max_alpha), 3))
    image = cache.get(key)
    if image is not None:
        return image
    size = max(6, int(math.ceil(radius)) * 2 + 2)
    center = size / 2.0
    rgb = _hex_rgb(color) or (120, 150, 255)
    rgba = bytearray(size * size * 4)
    for j in range(size):
        for i in range(size):
            distance = math.hypot(i + 0.5 - center, j + 0.5 - center)
            t = 1.0 - distance / radius
            if t <= 0.0:
                continue
            alpha = int(255 * max_alpha * t * t)
            if alpha <= 0:
                continue
            index = (j * size + i) * 4
            rgba[index] = rgb[0]
            rgba[index + 1] = rgb[1]
            rgba[index + 2] = rgb[2]
            rgba[index + 3] = alpha
    image = tk.PhotoImage(master=canvas, data=_png_bytes(size, size, bytes(rgba)))
    cache[key] = image
    return image


def _build_play(painter, cx, cy, size, color):
    half = size * 0.42
    painter.create_polygon(
        cx - size * 0.34, cy - half,
        cx - size * 0.34, cy + half,
        cx + size * 0.34, cy,
        fill=color, outline=color, width=1, joinstyle="round",
    )


def _build_pause(painter, cx, cy, size, color):
    bar_w = size * 0.16
    bar_h = size * 0.54
    gap = size * 0.14
    left = cx - gap / 2 - bar_w
    right = cx + gap / 2
    round_rect(painter, left, cy - bar_h / 2, left + bar_w, cy + bar_h / 2,
               bar_w * 0.45, fill=color, outline=color, width=1)
    round_rect(painter, right, cy - bar_h / 2, right + bar_w, cy + bar_h / 2,
               bar_w * 0.45, fill=color, outline=color, width=1)


def _build_stop(painter, cx, cy, size, color):
    half = size * 0.29
    round_rect(painter, cx - half, cy - half, cx + half, cy + half,
               size * 0.12, fill=color, outline=color, width=1)


def _build_skip(painter, cx, cy, size, color, direction=-1):
    width = size * 0.3
    height = size * 0.44
    gap = size * 0.06
    for index in range(2):
        if direction < 0:
            edge = cx + size * 0.24 - index * (width + gap)
            points = (edge, cy - height, edge, cy + height, edge - width, cy)
        else:
            edge = cx - size * 0.24 + index * (width + gap)
            points = (edge, cy - height, edge, cy + height, edge + width, cy)
        painter.create_polygon(points, fill=color, outline=color, width=1,
                               joinstyle="round")


def _build_speaker(painter, cx, cy, size, color, muted=False):
    body = (
        cx - size * 0.42, cy - size * 0.14,
        cx - size * 0.2, cy - size * 0.14,
        cx + size * 0.02, cy - size * 0.4,
        cx + size * 0.02, cy + size * 0.4,
        cx - size * 0.2, cy + size * 0.14,
        cx - size * 0.42, cy + size * 0.14,
    )
    painter.create_polygon(body, fill=color, outline=color, width=1, joinstyle="round")
    if muted:
        painter.create_line(
            cx + size * 0.02, cy - size * 0.26,
            cx + size * 0.42, cy + size * 0.26,
            fill=C.DANGER, width=1.8, capstyle="round",
        )
    else:
        for radius in (size * 0.26, size * 0.42):
            painter.create_arc(
                cx + size * 0.04 - radius, cy - radius,
                cx + size * 0.04 + radius, cy + radius,
                start=-48, extent=96, style="arc", outline=color, width=1.5,
            )


def _build_fullscreen(painter, cx, cy, size, color):
    arm = size * 0.34
    leg = size * 0.2
    for sign_x, sign_y in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        x = cx + sign_x * arm
        y = cy + sign_y * arm
        painter.create_line(x, y, x - sign_x * leg, y, fill=color, width=1.6,
                            capstyle="round")
        painter.create_line(x, y, x, y - sign_y * leg, fill=color, width=1.6,
                            capstyle="round")


def _build_restore(painter, cx, cy, size, color):
    arm = size * 0.24
    leg = size * 0.18
    for sign_x, sign_y in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        x = cx + sign_x * arm
        y = cy + sign_y * arm
        painter.create_line(x, y, x + sign_x * leg, y, fill=color, width=1.6,
                            capstyle="round")
        painter.create_line(x, y, x, y + sign_y * leg, fill=color, width=1.6,
                            capstyle="round")


def _build_folder(painter, cx, cy, size, color):
    width = size * 0.86
    height = size * 0.54
    x0, y0 = cx - width / 2, cy - height / 2 + size * 0.05
    tab_height = size * 0.2
    round_rect(painter, x0, y0 - tab_height, x0 + width * 0.46, y0 + tab_height,
               size * 0.05, fill=color, outline=color, width=1)
    round_rect(painter, x0, y0, x0 + width, y0 + height,
               size * 0.1, fill=color, outline=color, width=1)


def _build_film(painter, cx, cy, size, color):
    width = size * 0.62
    height = size * 0.72
    round_rect(painter, cx - width / 2, cy - height / 2,
               cx + width / 2, cy + height / 2,
               size * 0.12, fill="", outline=color, width=1.2)
    painter.create_polygon(
        cx - size * 0.07, cy - size * 0.15,
        cx - size * 0.07, cy + size * 0.15,
        cx + size * 0.19, cy,
        fill=color, outline=color, width=1, joinstyle="round",
    )


def _build_chevron(painter, cx, cy, size, color):
    painter.create_line(
        cx - size * 0.24, cy - size * 0.13,
        cx, cy + size * 0.17,
        cx + size * 0.24, cy - size * 0.13,
        fill=color, width=1.8, capstyle="round", joinstyle="round",
    )


def _build_arrow_up(painter, cx, cy, size, color):
    painter.create_line(
        cx, cy + size * 0.34, cx, cy - size * 0.3,
        fill=color, width=1.7, capstyle="round",
    )
    painter.create_line(
        cx - size * 0.26, cy - size * 0.04,
        cx, cy - size * 0.32,
        cx + size * 0.26, cy - size * 0.04,
        fill=color, width=1.7, capstyle="round", joinstyle="round",
    )


def _build_broadcast(painter, cx, cy, size, color):
    radius = size * 0.13
    painter.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                        fill=color, outline=color)
    for arm, extent in ((size * 0.27, 72), (size * 0.41, 100)):
        painter.create_arc(cx - arm, cy - arm, cx + arm, cy + arm,
                           start=-extent / 2, extent=extent,
                           style="arc", outline=color, width=1.5)


def _build_gear(painter, cx, cy, size, color):
    """齿轮: 8 个齿 + 一个圆环(中间留孔)。"""
    teeth = 8
    outer = size * 0.46
    root_radius = size * 0.30
    half = size * 0.085
    for index in range(teeth):
        angle = math.radians(index * 360.0 / teeth)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        nx, ny = -sin_a, cos_a
        points = (
            cx + cos_a * root_radius + nx * half, cy + sin_a * root_radius + ny * half,
            cx + cos_a * outer + nx * half * 0.62, cy + sin_a * outer + ny * half * 0.62,
            cx + cos_a * outer - nx * half * 0.62, cy + sin_a * outer - ny * half * 0.62,
            cx + cos_a * root_radius - nx * half, cy + sin_a * root_radius - ny * half,
        )
        painter.create_polygon(points, fill=color, outline=color, width=1)
    ring = size * 0.27
    painter.create_oval(cx - ring, cy - ring, cx + ring, cy + ring,
                        fill="", outline=color, width=size * 0.19)


_ICON_BUILDERS = {
    "play": _build_play,
    "pause": _build_pause,
    "stop": _build_stop,
    "skip": _build_skip,
    "speaker": _build_speaker,
    "fullscreen": _build_fullscreen,
    "restore": _build_restore,
    "folder": _build_folder,
    "film": _build_film,
    "chevron": _build_chevron,
    "arrow_up": _build_arrow_up,
    "broadcast": _build_broadcast,
    "gear": _build_gear,
}


def icon_play(canvas, cx, cy, size, color):
    _paint_icon(canvas, "play", cx, cy, size, color)


def icon_pause(canvas, cx, cy, size, color):
    _paint_icon(canvas, "pause", cx, cy, size, color)


def icon_stop(canvas, cx, cy, size, color):
    _paint_icon(canvas, "stop", cx, cy, size, color)


def icon_skip(canvas, cx, cy, size, color, direction=-1):
    _paint_icon(canvas, "skip", cx, cy, size, color, direction=direction)


def icon_speaker(canvas, cx, cy, size, color, muted=False):
    _paint_icon(canvas, "speaker", cx, cy, size, color, muted=bool(muted))


def icon_fullscreen(canvas, cx, cy, size, color):
    _paint_icon(canvas, "fullscreen", cx, cy, size, color)


def icon_restore(canvas, cx, cy, size, color):
    _paint_icon(canvas, "restore", cx, cy, size, color)


def icon_folder(canvas, cx, cy, size, color):
    _paint_icon(canvas, "folder", cx, cy, size, color)


def icon_film(canvas, cx, cy, size, color):
    _paint_icon(canvas, "film", cx, cy, size, color)


def icon_chevron(canvas, cx, cy, size, color):
    _paint_icon(canvas, "chevron", cx, cy, size, color)


def icon_arrow_up(canvas, cx, cy, size, color):
    _paint_icon(canvas, "arrow_up", cx, cy, size, color)


def icon_broadcast(canvas, cx, cy, size, color):
    _paint_icon(canvas, "broadcast", cx, cy, size, color)


def icon_gear(canvas, cx, cy, size, color):
    _paint_icon(canvas, "gear", cx, cy, size, color)


# ---------------------------------------------------------------------------
# VLC / 拖放
# ---------------------------------------------------------------------------
_DLL_DIR_HANDLES = []


def _prepare_vlc_dll_path():
    """Windows 下优先加载随程序附带的 VLC 运行库, 找不到再退回系统 VLC。

    顺序: 程序目录/vlc-runtime -> 上级目录/vlc-runtime -> 当前目录/vlc-runtime
          -> VLC_HOME -> Program Files 下的 VLC 安装目录
    找到后通过 PYTHON_VLC_LIB_PATH / PYTHON_VLC_MODULE_PATH 明确告诉
    python-vlc 使用这一份 libvlc 与插件目录, 避免串到系统安装的 VLC。
    """
    if os.name != "nt":
        return None
    app_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(app_dir, "vlc-runtime"),
        os.path.join(os.path.dirname(app_dir), "vlc-runtime"),
        os.path.join(os.getcwd(), "vlc-runtime"),
        os.environ.get("VLC_HOME", ""),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "VideoLAN", "VLC"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "VideoLAN", "VLC"),
    ]
    for folder in candidates:
        if not folder:
            continue
        dll = os.path.join(folder, "libvlc.dll")
        if not os.path.isfile(dll):
            continue
        plugins = os.path.join(folder, "plugins")
        os.environ["PYTHON_VLC_LIB_PATH"] = dll
        if os.path.isdir(plugins):
            os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
            os.environ["VLC_PLUGIN_PATH"] = plugins
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _DLL_DIR_HANDLES.append(os.add_dll_directory(folder))
        return folder
    return None


_VLC_HOME_DIR = _prepare_vlc_dll_path()

try:
    import vlc
except (ImportError, OSError) as error:  # 缺少 python-vlc 或 libvlc
    vlc = None
    VLC_IMPORT_ERROR = error
else:
    VLC_IMPORT_ERROR = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None


# ---------------------------------------------------------------------------
# 日志: %APPDATA%\Nobplaypot\logs\nobplaypot.log
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)-7s] [%(threadName)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_PATH = os.environ.get("NOBPLAYPOT_LOG_FILE") or os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "Nobplaypot", "logs", "nobplaypot.log",
)
log = logging.getLogger("nobplaypot")


def vlc_version_text():
    if vlc is None:
        return "不可用"
    try:
        return vlc.libvlc_get_version().decode("utf-8", "replace")
    except Exception:
        return "未知"


def vlc_state_text(state):
    """libvlc 的 State 可能是 ctypes 枚举, 统一成 Playing/Paused 这样的短名。"""
    if state is None:
        return "无"
    return getattr(state, "name", None) or str(state).split(".")[-1]


def setup_logging():
    """初始化日志(幂等调用): 文件按 2MB 轮转保留 3 份, 控制台只显示警告及以上。"""
    global LOG_PATH
    if log.handlers:
        return log
    level = getattr(
        logging, os.environ.get("NOBPLAYPOT_LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    log.setLevel(logging.DEBUG)
    log.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)

    file_handler = None
    candidates = [
        LOG_PATH,
        os.path.join(tempfile.gettempdir(), "Nobplaypot", "logs",
                     os.path.basename(LOG_PATH) or "nobplaypot.log"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            LOG_PATH = path
            break
        except OSError:
            file_handler = None
    if file_handler is not None:
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    else:
        log.addHandler(logging.NullHandler())

    if sys.stderr is not None:  # pythonw.exe 下没有 stderr
        console = logging.StreamHandler()
        console.setLevel(max(level, logging.WARNING))
        console.setFormatter(formatter)
        log.addHandler(console)

    log.info("=" * 78)
    log.info(
        "Nobplaypot %s 启动 | Python %s | Tk %s | VLC %s",
        APP_VERSION, platform.python_version(), tk.TkVersion, vlc_version_text(),
    )
    log.info("系统 %s | 进程 %s", platform.platform(), os.getpid())
    log.info("日志文件 %s | 级别 %s", LOG_PATH, logging.getLevelName(level))
    return log


def install_excepthooks():
    """把未捕获异常(主线程 / 子线程)写进日志。"""
    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _main_hook

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("线程 %s 异常", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_hook


def format_time(ms):
    if not ms or ms < 0:
        ms = 0
    total = int(ms // 1000)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def is_video_file(path):
    return os.path.isfile(path) and path.lower().endswith(VIDEO_EXTS)


# ---------------------------------------------------------------------------
# 流式传输: 协议辅助 + 主持端 TCP 指令服务器
# ---------------------------------------------------------------------------
def parse_host_port(text):
    """解析 '127.0.0.1:5000' / '[::1]:5000' -> (host, port); 失败返回 None。"""
    text = (text or "").strip()
    if text.startswith("["):
        if "]:" not in text:
            return None
        host = text[1:text.index("]")]
        port_text = text.rsplit(":", 1)[1]
    elif text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
    else:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not host or not (1 <= port <= 65535):
        return None
    return host, port


def encode_message(message):
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > STREAM_MAX_MSG_BYTES:
        raise ValueError("流式消息过大")
    return data + b"\n"


def build_sout(targets, display=True, bandwidth=2200, scale=1.0, fps=30,
               audio_bitrate=128, preset="veryfast", keyint=30, rate=1.0):
    """libvlc :sout 选项。

    1x 播放时走 **流复制**(不重编码): 直接把源文件的 H.264/AAC 等 ES 复用到 TS
    通过 UDP 发出, 延迟和 CPU 都最低; 只有变速(≠1x)时才做 x264 zerolatency 转码。
    """
    if abs(float(rate) - 1.0) < 0.01:
        streams = ",".join(
            f'dst="std{{access=udp,mux=ts,dst={ip}:{port}}}"'
            for ip, port in targets
        )
        if display:
            return "#duplicate{dst=display," + streams + "}"
        return "#duplicate{" + streams + "}"
    udp_dsts = ",".join(
        f"dst=udp{{dst={ip}:{port},mux=ts}}" for ip, port in targets
    )
    transcode = (
        f"transcode{{vcodec=h264,vb={int(bandwidth)},scale={float(scale):g},"
        f"fps={int(fps)},acodec=mp4a,ab={int(audio_bitrate)},"
        "channels=2,samplerate=44100,"
        f"venc=x264{{preset={preset},tune=zerolatency,keyint={int(keyint)}}}}}"
    )
    if display:
        return '#duplicate{dst=display,dst="' + transcode + ":duplicate{" + udp_dsts + '}"}'
    return "#" + transcode + ":duplicate{" + udp_dsts + "}"


STREAM_CHUNK_SIZE = 1316

MEDIA_HELLO = b"NBPLAY1"


class UdpMediaSender:
    """主持端: VLC 本地 UDP 中转 -> 原包转发给已注册客户端。

    媒体层不做任何自研协议: 不解析、不排队、不改包、不做 FEC/NACK。
    客户端只发一个很小的 UDP 注册包, 主持端把 VLC 的 TS 数据报原样发回,
    抖动/乱序/缓冲全部交给客户端 VLC 自己处理。
    """

    def __init__(self, media_port):
        self._relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._relay.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self._relay.bind(("127.0.0.1", 0))
        self._relay.settimeout(0.5)
        self.relay_port = self._relay.getsockname()[1]
        self._media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._media.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._media.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self._media.bind(("0.0.0.0", int(media_port)))
        self._media.settimeout(0.5)
        self.port = self._media.getsockname()[1]
        self._clients = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._relay_thread = None
        self._media_thread = None

    def start(self):
        if self._media_thread is None:
            self._media_thread = threading.Thread(
                target=self._media_loop, name="udp-media-register", daemon=True)
            self._media_thread.start()
        if self._relay_thread is None:
            self._relay_thread = threading.Thread(
                target=self._relay_loop, name="udp-media-relay", daemon=True)
            self._relay_thread.start()

    def stop(self):
        self._stop.set()
        for sock in (self._relay, self._media):
            try:
                sock.close()
            except OSError:
                pass
        for thread in (self._media_thread, self._relay_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._media_thread = None
        self._relay_thread = None
        with self._lock:
            self._clients.clear()

    def client_count(self):
        with self._lock:
            return len(self._clients)

    def _media_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._media.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(MEDIA_HELLO):
                cid = data[len(MEDIA_HELLO):].decode("utf-8", "ignore") or (
                    "anon-%s:%d" % (addr[0], addr[1]))
                with self._lock:
                    self._clients[cid] = (addr, time.monotonic())
                log.info("媒体注册: %s -> %s:%d", cid, addr[0], addr[1])

    def _relay_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._relay.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.monotonic()
            with self._lock:
                dead = [cid for cid, (_a, t) in self._clients.items()
                        if now - t > 5.0]
                for cid in dead:
                    self._clients.pop(cid, None)
                addrs = [a for a, _t in self._clients.values()]
            for addr in addrs:
                try:
                    self._media.sendto(data, addr)
                except OSError:
                    pass


class UdpMediaReceiver:
    """客户端: UDP 注册 + 收到的 TS 原包直接交给本机 VLC。

    不解析、不排队、不重排、不补包; 缓冲与抖动由 VLC 的 network-caching 负责。
    """

    def __init__(self, host, port, on_nack=None):
        self.host = str(host)
        self.port = int(port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self._sock.bind(("0.0.0.0", 0))
        self._sock.settimeout(0.2)
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local_port = 0
        self._client_id = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._first_packet_logged = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="udp-media-receiver", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        for sock in (self._sock, self._tx):
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_local_port(self, port):
        with self._lock:
            self.local_port = int(port)

    def set_client_id(self, client_id):
        self._client_id = str(client_id or "")

    def _loop(self):
        last_hello = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_hello > 1.5:
                hello = MEDIA_HELLO + self._client_id.encode("utf-8", "ignore")
                try:
                    self._sock.sendto(hello, (self.host, self.port))
                except OSError:
                    pass
                last_hello = now
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._first_packet_logged:
                self._first_packet_logged = True
                log.info("媒体流首包到达: %s:%d (%d 字节)",
                         self.host, self.port, len(data))
            with self._lock:
                port = self.local_port
            if port > 0:
                try:
                    self._tx.sendto(data, ("127.0.0.1", port))
                except OSError:
                    pass




def ephemeral_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# 推流中转: 源文件 -> TS(只换容器) -> 本地 HTTP 按字节偏移读取
# 这样推流器可以从任意进度附近"重新开始", 而不需要对带 sout 的播放器 seek
# ---------------------------------------------------------------------------
RELAY_PACKET_SIZE = 188
RELAY_PREFIX_BYTES = 188 * 3
RELAY_DIR = os.path.join(tempfile.gettempdir(), "Nobplaypot", "relay")


class _RelayHttpHandler(http.server.BaseHTTPRequestHandler):
    """按 ?start=<字节偏移> 从 TS 文件中间开始发送。"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        token = parsed.path.strip("/").split("/")[-1]
        path = self.server.files.get(token)
        if not path or not os.path.exists(path):
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            start = int(query.get("start", ["0"])[0])
        except ValueError:
            start = 0
        size = os.path.getsize(path)
        start = max(0, min(start, max(0, size - 1)))
        prefix = b""
        if start > 0:
            # 从中间开始必须带上文件开头的 PAT/PMT(否则 demuxer 认不出节目),
            # 再跳到关键帧偏移, 解码器才拿得到 SPS/PPS
            with open(path, "rb") as head:
                prefix = head.read(min(RELAY_PREFIX_BYTES, size))
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(prefix) + max(0, size - start)))
        self.end_headers()
        try:
            with open(path, "rb") as handle:
                if prefix:
                    self.wfile.write(prefix)
                    handle.seek(start)
                while True:
                    data = handle.read(65536)
                    if not data:
                        break
                    self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class TsRelay:
    """把源文件重封装成 TS, 并建立 时间 -> 字节偏移 的索引。"""

    def __init__(self, source):
        self.source = source
        self.ts_path = None
        self.token = None
        self.size = 0
        self.index = []       # [(秒, 字节偏移), ...]
        self.keyframes = []   # 关键帧子集, 推流必须从关键帧开始
        self.ready = False
        self.error = ""

    # ------------------------------------------------------------ 构建
    def build(self, progress=None):
        os.makedirs(RELAY_DIR, exist_ok=True)
        try:
            stat = os.stat(self.source)
        except OSError as error:
            self.error = f"读取文件失败: {error}"
            return False
        digest = hashlib.md5(
            f"{self.source}|{stat.st_size}|{stat.st_mtime}".encode("utf-8")
        ).hexdigest()[:12]
        self.token = digest + ".ts"
        self.ts_path = os.path.join(RELAY_DIR, self.token)
        if self.source.lower().endswith((".ts", ".m2ts", ".mts")):
            # 源文件本身就是 TS 容器, 直接按字节偏移读取, 不用重新封装
            self.ts_path = self.source
        elif not (os.path.exists(self.ts_path) and os.path.getsize(self.ts_path) > 0):
            if not self._remux(stat.st_size, progress):
                return False
        self.size = os.path.getsize(self.ts_path)
        if progress is not None:
            progress(-1.0)  # 转封装完成, 进入索引阶段
        try:
            self.index = self._build_index()
        except Exception as error:  # noqa: BLE001
            self.error = f"索引失败: {error}"
            self.index = []
        self.ready = self.size > 0 and bool(self.index)
        if not self.ready and not self.error:
            self.error = "源文件无法转封装为 TS(编码可能不受支持)"
        log.info("推流源就绪=%s 大小=%.1fMB 索引=%d 项 关键帧=%d",
                 self.ready, self.size / 1048576.0, len(self.index),
                 len(self.keyframes))
        return self.ready

    def _remux(self, source_size, progress=None):
        instance = vlc.Instance("--no-video-title-show", "--quiet")
        player = None
        try:
            player = instance.media_player_new()
            media = instance.media_new(self.source)
            media.add_option(
                ":sout=#std{access=file,mux=ts,dst=" + self.ts_path + "}"
            )
            player.set_media(media)
            player.play()
            estimate = max(1.0, float(source_size) * 1.03)
            deadline = time.monotonic() + 1800
            last_report = 0.0
            while time.monotonic() < deadline:
                if progress is not None:
                    now = time.monotonic()
                    if now - last_report >= 1.0:
                        last_report = now
                        written = (os.path.getsize(self.ts_path)
                                   if os.path.exists(self.ts_path) else 0)
                        progress(min(99.0, written / estimate * 100.0))
                state = player.get_state()
                if state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
                    break
                time.sleep(0.2)
            player.stop()
            return os.path.exists(self.ts_path) and os.path.getsize(self.ts_path) > 0
        except Exception as error:  # noqa: BLE001
            self.error = f"转封装失败: {error}"
            log.exception("推流源转封装失败")
            return False
        finally:
            if player is not None:
                try:
                    player.release()
                except Exception:
                    pass
            try:
                instance.release()
            except Exception:
                pass

    def _build_index(self):
        """扫描 TS 的 PES 头, 记录 (PTS 秒, 包偏移); 同时标记关键帧。"""
        index = []
        keyframes = []
        base_pts = None
        offset = 0
        with open(self.ts_path, "rb") as handle:
            while True:
                chunk = handle.read(RELAY_PACKET_SIZE * 4096)
                if not chunk:
                    break
                limit = len(chunk) - RELAY_PACKET_SIZE + 1
                for pos in range(0, limit, RELAY_PACKET_SIZE):
                    if chunk[pos] != 0x47:
                        continue
                    if not (chunk[pos + 1] & 0x40):  # 只看 PES 起始包
                        continue
                    payload = pos + 4
                    if chunk[payload - 1] & 0x20:  # 有 adaptation field
                        payload += 1 + chunk[payload]
                    if payload + 14 > pos + RELAY_PACKET_SIZE:
                        continue
                    if chunk[payload:payload + 3] != b"\x00\x00\x01":
                        continue
                    flags = chunk[payload + 7]
                    if (flags >> 6) < 2:  # 只有 PTS
                        continue
                    pts = (((chunk[payload + 9] & 0x0E) << 29)
                           | (chunk[payload + 10] << 22)
                           | ((chunk[payload + 11] & 0xFE) << 14)
                           | (chunk[payload + 12] << 7)
                           | (chunk[payload + 13] >> 1))
                    if base_pts is None:
                        base_pts = pts
                    seconds = max(0, pts - base_pts) / 90000.0
                    entry = (seconds, offset + pos)
                    index.append(entry)
                    header_len = chunk[payload + 8]
                    es_start = payload + 9 + header_len
                    es_end = pos + RELAY_PACKET_SIZE
                    if es_start < es_end:
                        es = chunk[es_start:es_end]
                        # TS 封装里关键帧会带 SPS(67); 以 SPS 所在 PES 为准,
                        # 这样从该偏移开始能拿到 SPS/PPS, 解码器才能起步
                        if b"\x00\x00\x01\x67" in es:
                            keyframes.append(entry)
                offset += len(chunk)
        self.keyframes = keyframes
        return index

    # ------------------------------------------------------------ 查询
    def offset_for_ms(self, position_ms):
        points = self.keyframes or self.index
        if not points:
            return 0
        target = max(0.0, float(position_ms) / 1000.0)
        low, high = 0, len(points) - 1
        best = 0
        while low <= high:
            middle = (low + high) // 2
            if points[middle][0] <= target:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return points[best][1]


def _prune_relay_files(keep_token):
    """只保留当前推流源, 避免临时目录堆积几 GB 的 TS。"""
    try:
        keep = os.path.join(RELAY_DIR, keep_token)
        for name in os.listdir(RELAY_DIR):
            if not name.endswith(".ts"):
                continue
            path = os.path.join(RELAY_DIR, name)
            if os.path.abspath(path) == os.path.abspath(keep):
                continue
            try:
                os.remove(path)
                log.info("清理旧的推流源: %s", name)
            except OSError:
                pass
    except OSError:
        pass


def _cleanup_relay_dir(max_age_seconds=172800):
    """启动时清掉两天前的推流缓存, 防止长期占用磁盘。"""
    try:
        deadline = time.time() - max_age_seconds
        for name in os.listdir(RELAY_DIR):
            if not name.endswith(".ts"):
                continue
            path = os.path.join(RELAY_DIR, name)
            try:
                if os.path.getmtime(path) < deadline:
                    os.remove(path)
                    log.info("清理过期推流缓存: %s", name)
            except OSError:
                continue
    except OSError:
        pass


class _StreamRequestHandler(socketserver.BaseRequestHandler):
    """每个客户端一条 TCP 长连接, 换行分隔的 JSON 消息。

    注册(加入房间)放在密码核验之后: 未认证的连接只接收 hello/auth,
    主持端在这里直接核验密码, 通过后才把客户端计入房间。
    """

    def handle(self):
        server = self.server
        log.debug("接入线程启动: %s", self.client_address)
        if server.app is None:
            log.warning("接入线程: server.app 为空")
            return
        expected = str(getattr(server.app, "_host_password", "") or "")
        client_id = None
        pending_hello = None
        failures = 0
        buffer = b""
        try:
            while True:
                try:
                    chunk = self.request.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > STREAM_MAX_MSG_BYTES * 2:
                    log.warning("客户端消息过大, 断开: %s", self.client_address)
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode("utf-8", "replace"))
                    except (ValueError, UnicodeDecodeError):
                        log.warning("客户端发送了无法解析的消息: %s", self.client_address)
                        continue
                    message_type = message.get("type")
                    if client_id is None:
                        # ---- 未认证: 只处理握手/密码 ----
                        if message_type == "ping":
                            self._send_raw({"v": STREAM_VERSION, "type": "pong"})
                            continue
                        if message_type == "bye":
                            return
                        if message_type not in ("hello", "auth"):
                            self._send_raw({"v": STREAM_VERSION, "type": "error",
                                            "code": "auth_required",
                                            "message": "请先完成房间验证"})
                            continue
                        if message_type == "hello":
                            pending_hello = dict(message)
                        provided = message.get("password")
                        if not password_matches(provided, expected):
                            failures += 1
                            log.warning(
                                "客户端密码验证失败: %s (第 %d 次)",
                                self.client_address, failures,
                            )
                            if provided:
                                self._send_raw({"v": STREAM_VERSION, "type": "error",
                                                "code": "bad_password",
                                                "message": "密码错误"})
                            else:
                                self._send_raw({"v": STREAM_VERSION, "type": "need_password",
                                                "message": "房间需要密码"})
                            if failures >= STREAM_PASSWORD_ATTEMPTS:
                                log.warning("密码错误次数过多, 断开: %s", self.client_address)
                                return
                            time.sleep(0.15)
                            continue
                        client_id = server.register_client(self)
                        if client_id is None:
                            self._send_raw({"v": STREAM_VERSION, "type": "error",
                                            "code": "room_full",
                                            "message": "房间人数已满"})
                            return
                        log.debug("客户端通过房间验证: %s", self.client_address)
                        server.touch_client(client_id)
                        hello = dict(pending_hello or message)
                        hello.pop("password", None)
                        hello["type"] = "hello"
                        server.app.queue_net_event(
                            {"kind": "host_message", "client_id": client_id,
                             "message": hello}
                        )
                        continue
                    # ---- 已认证 ----
                    server.touch_client(client_id)
                    if message_type == "ping":  # 心跳直接在线程里回, 不经过 UI 线程
                        server.reply(
                            client_id, {"v": STREAM_VERSION, "type": "pong"}
                        )
                        continue
                    if message_type == "bye":
                        return
                    server.app.queue_net_event(
                        {"kind": "host_message", "client_id": client_id,
                         "message": message}
                    )
        finally:
            if client_id is not None:
                server.unregister_client(client_id)

    def _send_raw(self, message):
        try:
            self.request.sendall(encode_message(message))
        except (OSError, ValueError):
            pass


class StreamHostServer(socketserver.ThreadingTCPServer):
    """主持端指令端口(TCP)服务器。"""

    # Windows 的 SO_REUSEADDR 允许"抢占"已被占用的端口, 会让连接落到错误的
    # 进程上(表现为客户端连上却没有客户端接入); 因此 Windows 上禁用重用.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, app):
        super().__init__(address, _StreamRequestHandler)
        self.app = app
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._clients = {}

    # ------------------------------------------------------- 线程安全 API
    def register_client(self, handler):
        with self._lock:
            limit = STREAM_MAX_CLIENTS
            try:
                limit = int(self.app.settings.get("max_clients", limit))
            except (AttributeError, TypeError, ValueError):
                pass
            if len(self._clients) >= max(1, limit):
                return None
            client_id = uuid.uuid4().hex[:8]
            self._clients[client_id] = {
                "id": client_id,
                "handler": handler,
                "addr": f"{handler.client_address[0]}:{handler.client_address[1]}",
                "name": None,
                "stream": None,
                "last_seen": time.time(),
            }
            return client_id

    def touch_client(self, client_id):
        with self._lock:
            entry = self._clients.get(client_id)
            if entry is not None:
                entry["last_seen"] = time.time()

    def set_client_info(self, client_id, name, stream):
        with self._lock:
            entry = self._clients.get(client_id)
            if entry is not None:
                entry["name"] = name
                entry["stream"] = stream

    def unregister_client(self, client_id):
        with self._lock:
            entry = self._clients.pop(client_id, None)
        if entry is not None:
            try:
                entry["handler"].request.close()
            except OSError:
                pass
            self.app.queue_net_event({"kind": "host_left", "client_id": client_id})

    def kick_client(self, client_id, reason="主持将你移出房间"):
        """把成员移出房间: 先通知对方, 再断开并触发房间更新。"""
        with self._lock:
            if client_id not in self._clients:
                return False
        self.reply(client_id, {"v": STREAM_VERSION, "type": "kicked",
                               "reason": reason})
        time.sleep(0.05)  # 让通知先发出去
        self.unregister_client(client_id)
        return True

    def rename_client(self, client_id, name):
        with self._lock:
            entry = self._clients.get(client_id)
            if entry is None:
                return False
            entry["name"] = str(name)[:32]
        return True

    def reply(self, client_id, message):
        with self._lock:
            entry = self._clients.get(client_id)
        if entry is None:
            return False
        return self._send_to_handler(entry["handler"], message)

    def broadcast(self, message):
        with self._lock:
            handlers = [entry["handler"] for entry in self._clients.values()]
        failed = 0
        for handler in handlers:
            if not self._send_to_handler(handler, message):
                failed += 1
        return failed

    @staticmethod
    def _send_to_handler(handler, message):
        try:
            data = encode_message(message)
            server = handler.server
            with server._send_lock:
                handler.request.sendall(data)
            return True
        except (OSError, ValueError):
            return False

    def snapshot(self):
        with self._lock:
            return [
                {"id": entry["id"], "name": entry["name"] or f"客户端-{entry['id']}",
                 "addr": entry["addr"]}
                for entry in self._clients.values()
            ]

    def targets(self):
        """所有已注册流接收目标的去重列表 [(ip, port), ...]。"""
        with self._lock:
            targets = {
                (entry["stream"][0], int(entry["stream"][1]))
                for entry in self._clients.values()
                if entry["stream"] is not None
            }
        return sorted(targets)

    def purge_stale(self, timeout_s):
        now = time.time()
        with self._lock:
            stale = [
                entry["handler"] for entry in self._clients.values()
                if now - entry["last_seen"] > timeout_s
            ]
        for handler in stale:
            try:
                handler.request.close()
            except OSError:
                pass
        with self._lock:
            purged = [
                client_id for client_id, entry in list(self._clients.items())
                if now - entry["last_seen"] > timeout_s
            ]
            for client_id in purged:
                self._clients.pop(client_id, None)
        for client_id in purged:
            self.app.queue_net_event({"kind": "host_left", "client_id": client_id})
        return purged

    def close_all(self):
        with self._lock:
            handlers = [entry["handler"] for entry in self._clients.values()]
        for handler in handlers:
            try:
                # 先半关闭写端, 保证已发出的消息(FIN 之前)不会被 RST 丢掉
                handler.request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        for handler in handlers:
            try:
                handler.request.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Windows 外观: 深色标题栏 + 应用图标
# ---------------------------------------------------------------------------
def _colorref(color):
    r, g, b = _rgb(color)
    return r | (g << 8) | (b << 16)


def apply_dark_titlebar(window, caption=C.DECK, text_color=C.TEXT_MUTED):
    """让系统标题栏变深色(Win10 20H1+ / Win11), 失败时静默忽略。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        dwm = ctypes.windll.dwmapi
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                break
        caption_ref = ctypes.c_int(_colorref(caption))
        text_ref = ctypes.c_int(_colorref(text_color))
        border_ref = ctypes.c_int(_colorref(caption))
        for attribute, value in ((35, caption_ref), (36, text_ref), (34, border_ref)):
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def round_window_corners(window, radius):
    """把窗口本身裁成圆角(Win32 区域), 让浮动 dock 没有方形外框。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        window.update_idletasks()
        width = max(2, window.winfo_width())
        height = max(2, window.winfo_height())
        hwnd = window.winfo_id()
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, int(radius * 2), int(radius * 2)
        )
        if rgn:
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def _in_round_square(x, y, size, radius):
    inner = radius
    if x < inner and y < inner:
        return (x - inner) ** 2 + (y - inner) ** 2 <= radius * radius
    if x > size - inner and y < inner:
        return (x - size + inner) ** 2 + (y - inner) ** 2 <= radius * radius
    if x < inner and y > size - inner:
        return (x - inner) ** 2 + (y - size + inner) ** 2 <= radius * radius
    if x > size - inner and y > size - inner:
        return (x - size + inner) ** 2 + (y - size + inner) ** 2 <= radius * radius
    return True


def make_app_icon(root, size=64):
    """生成应用图标(圆角方块 + 居中播放三角), 带抗锯齿。"""
    radius = size / 4.0
    top = mix(C.ACCENT, "#8FA8FF", 0.55)
    bottom = mix(C.ACCENT, C.ACCENT_DOWN, 0.55)
    # 三角形按外接矩形居中
    triangle = [(size * 0.344, size * 0.266),
                (size * 0.344, size * 0.734),
                (size * 0.656, size * 0.5)]
    samples = 4
    total = samples * samples
    rgba = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            square_hits = 0
            triangle_hits = 0
            for sy in range(samples):
                py = y + (sy + 0.5) / samples
                for sx in range(samples):
                    px = x + (sx + 0.5) / samples
                    if _in_round_square(px, py, size, radius):
                        square_hits += 1
                        if _point_in_poly(px, py, [v for point in triangle for v in point]):
                            triangle_hits += 1
            if not square_hits:
                continue
            base = mix(top, bottom, y / (size - 1.0))
            color = mix(base, C.BG, triangle_hits / float(square_hits))
            index = (y * size + x) * 4
            red, green, blue = _hex_rgb(color)
            rgba[index] = red
            rgba[index + 1] = green
            rgba[index + 2] = blue
            rgba[index + 3] = int(255 * square_hits / total)
    return tk.PhotoImage(master=root, data=_png_bytes(size, size, bytes(rgba)))


# ---------------------------------------------------------------------------
# 基础控件
# ---------------------------------------------------------------------------
class Tooltip:
    """跟随控件的深色小提示。"""

    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._job = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress-1>", self._hide, add="+")
        widget.bind("<Destroy>", lambda _e: self._hide(), add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if not self.text or not self.widget.winfo_exists():
            return
        try:
            window = tk.Toplevel(self.widget)
        except tk.TclError:
            return
        window.wm_overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        window.configure(bg=C.STROKE)
        tk.Label(
            window, text=self.text, bg=C.SURFACE_HI, fg=C.TEXT,
            font=ui_font(9), padx=9, pady=4,
        ).pack(padx=1, pady=1)
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - width // 2
        y = self.widget.winfo_rooty() - height - 8
        window.wm_geometry(f"+{x}+{y}")
        self._window = window

    def _hide(self, _event=None):
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


class HoverCanvas(tk.Canvas):
    """带悬停/按下颜色过渡的 Canvas 基类。"""

    def __init__(self, master, width, height, bg, command=None):
        super().__init__(
            master, width=width, height=height, bg=bg,
            highlightthickness=0, bd=0, takefocus=0,
        )
        self._canvas_bg = bg
        self._cw = int(width)
        self._ch = int(height)
        self._command = command
        self._mix = 0.0
        self._target = 0.0
        self._hovering = False
        self._pressed = False
        self._enabled = True
        self._anim_job = None
        self.configure(cursor="hand2" if command else "arrow")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Destroy>", self._on_destroy)

    def _render(self):
        raise NotImplementedError

    def _on_destroy(self, _event=None):
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None

    def _on_enter(self, _event=None):
        self._hovering = True
        self._target = 1.0
        self._start_anim()

    def _on_leave(self, _event=None):
        self._hovering = False
        self._pressed = False
        self._target = 0.0
        self._start_anim()

    def _on_press(self, _event=None):
        if not self._enabled or self._command is None:
            return
        self._pressed = True
        self._render()

    def _on_release(self, event=None):
        if not self._pressed:
            return
        self._pressed = False
        self._render()
        if event is not None and not (0 <= event.x <= self._cw and 0 <= event.y <= self._ch):
            return
        if self._enabled and self._command is not None:
            self._command()

    def _start_anim(self):
        if not ANIMATIONS_ENABLED:
            if self._anim_job is not None:
                try:
                    self.after_cancel(self._anim_job)
                except tk.TclError:
                    pass
                self._anim_job = None
            self._mix = self._target
            self._render()
            return
        if self._anim_job is None:
            self._anim_step()

    def _anim_step(self):
        try:
            if not self.winfo_exists():
                self._anim_job = None
                return
        except tk.TclError:
            self._anim_job = None
            return
        difference = self._target - self._mix
        if abs(difference) <= 0.02:
            self._mix = self._target
            self._anim_job = None
            self._render()
            return
        self._mix += difference * 0.35
        self._render()
        try:
            self._anim_job = self.after(15, self._anim_step)
        except tk.TclError:
            self._anim_job = None

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if (enabled and self._command) else "arrow")
        self._render()


class RoundedButton(HoverCanvas):
    @staticmethod
    def _variant_spec(variant):
        """每次创建时现取色板, 这样换主题后新建的按钮颜色也是新的。"""
        variants = {
            "primary": {
                "fill": C.ACCENT, "hover": C.ACCENT_HOVER, "press": C.ACCENT_DOWN,
                "fg": "#0A0D15", "fg_hover": "#0A0D15", "outline": None,
            },
            "ghost": {
                "fill": None, "hover": C.SURFACE, "press": C.SURFACE,
                "fg": C.TEXT_MUTED, "fg_hover": C.TEXT, "outline": C.STROKE,
            },
            "subtle": {
                "fill": C.SURFACE, "hover": C.SURFACE_HI, "press": C.SURFACE_HI,
                "fg": C.TEXT, "fg_hover": C.TEXT, "outline": None,
            },
            "icon": {
                "fill": None, "hover": C.SURFACE, "press": C.SURFACE_HI,
                "fg": C.TEXT_MUTED, "fg_hover": C.TEXT, "outline": None,
            },
            "menu": {
                "fill": None, "hover": C.SURFACE, "press": C.SURFACE,
                "fg": C.TEXT_MUTED, "fg_hover": C.TEXT, "outline": None,
            },
            "danger": {
                "fill": None, "hover": mix(C.SURFACE, C.DANGER, 0.18),
                "press": mix(C.SURFACE, C.DANGER, 0.28),
                "fg": C.DANGER, "fg_hover": "#FF9AA6", "outline": C.STROKE,
            },
        }
        return dict(variants.get(variant, variants["ghost"]))

    def __init__(self, master, text="", command=None, variant="ghost", width=96, height=34,
                 radius=10, bg=C.DECK, font=None, icon=None, icon_size=15, align="center",
                 tooltip=None, pad_x=14):
        super().__init__(master, width, height, bg, command=command)
        spec = self._variant_spec(variant)
        if spec["fill"] is None:
            spec["fill"] = bg
        self._spec = spec
        self._variant = variant
        self._text = text
        self._icon = icon
        self._icon_size = icon_size
        self._radius = min(radius, width / 2.0, height / 2.0)
        self._font = font or ui_font(10, variant == "primary")
        self._align = align
        self._pad_x = pad_x
        self._active = False
        self._tooltip = Tooltip(self, tooltip) if tooltip else None
        self._render()

    # ---------------------------------------------------------------- API
    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._render()

    def set_icon(self, icon):
        if icon is not self._icon:
            self._icon = icon
            self._render()

    def set_active(self, active):
        active = bool(active)
        if active != self._active:
            self._active = active
            self._render()

    # -------------------------------------------------------------- 绘制
    def _render(self):
        width, height = self._cw, self._ch
        spec = self._spec
        mix_value = self._mix
        if not self._enabled:
            fill, fg = spec["fill"], C.TEXT_DIM
        elif self._active:
            fill, fg = C.ACCENT_SOFT, C.ACCENT
        elif self._pressed:
            fill, fg = spec["press"], spec["fg_hover"]
        else:
            fill = mix(spec["fill"], spec["hover"], mix_value)
            fg = mix(spec["fg"], spec["fg_hover"], mix_value)
        outline = fill if spec["outline"] is None else mix(spec["outline"], C.SURFACE_HI, mix_value)
        self.delete("all")
        draw_round_rect(self, 1, 1, width - 1, height - 1, self._radius,
                        fill=fill, outline=outline, width=1)
        self._draw_content(fg, width, height)

    def _draw_content(self, color, width, height):
        cy = height / 2 + (1 if self._pressed else 0)
        if self._icon is not None and self._text:
            item = self.create_text(width / 2, cy, text=self._text, fill=color,
                                    font=self._font, anchor="center")
            bbox = self.bbox(item)
            text_width = bbox[2] - bbox[0]
            total = text_width + self._icon_size + 7
            left = (width - total) / 2
            self._icon(self, left + self._icon_size / 2, cy, self._icon_size, color)
            self.coords(item, left + self._icon_size + 7 + text_width / 2, cy)
        elif self._icon is not None:
            self._icon(self, width / 2, cy, self._icon_size, color)
        elif self._text:
            if self._align == "left":
                self.create_text(self._pad_x, cy, text=self._text, fill=color,
                                 font=self._font, anchor="w")
            else:
                self.create_text(width / 2, cy, text=self._text, fill=color,
                                 font=self._font, anchor="center")


class Slider(tk.Canvas):
    """自绘滑杆: 用于进度条(0-1000)与音量(0-100)。"""

    def __init__(self, master, variable, from_=0.0, to=1000.0, command=None,
                 on_seek_start=None, on_seek_end=None, bg=C.DECK, width=200, height=26,
                 compact=False, integral=False, pad=8):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0, cursor="hand2")
        self._var = variable
        self._from = float(from_)
        self._to = float(to)
        self._command = command
        self._on_seek_start = on_seek_start
        self._on_seek_end = on_seek_end
        self._bg = bg
        self._integral = integral
        self._track_width = 3 if compact else 4
        self._thumb_radius = 4.5 if compact else 5.5
        # 圆点悬停时会放大, 左右留出足够空间, 避免 0%/100% 处被裁掉
        self._pad = max(float(pad), self._thumb_radius * 2.6 + 6.0)
        self._active = False
        self._hover = False
        self._anim_job = None
        self._hover_mix = 0.0
        self._last_ratio = -1.0
        self._last_size = (0, 0)
        self.bind("<Configure>", lambda _e: self.refresh())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover_motion)
        self.bind("<Leave>", self._on_leave)
        variable.trace_add("write", self._on_var_write)

    # ---------------------------------------------------------------- API
    def value(self):
        try:
            return float(self._var.get())
        except (tk.TclError, ValueError):
            return self._from

    def set(self, value):
        value = clamp(float(value), self._from, self._to)
        if abs(value - self.value()) < 0.01:
            return
        self._var.set(round(value) if self._integral else value)

    def refresh(self):
        ratio = (self.value() - self._from) / (self._to - self._from) if self._to != self._from else 0.0
        ratio = clamp(ratio, 0.0, 1.0)
        size = (self.winfo_width(), self.winfo_height())
        if (not self._active and abs(ratio - self._last_ratio) < 0.0005
                and size == self._last_size):
            return
        self._last_ratio = ratio
        self._last_size = size
        self._render(ratio)

    # -------------------------------------------------------------- 内部
    def _on_var_write(self, *_args):
        self.refresh()

    def _ratio_at(self, x):
        width = max(2, self.winfo_width())
        span = max(1.0, width - 2 * self._pad)
        return clamp((x - self._pad) / span, 0.0, 1.0)

    def _set_from_x(self, x):
        ratio = self._ratio_at(x)
        value = self._from + ratio * (self._to - self._from)
        value = round(value) if self._integral else value
        self._var.set(value)
        self._last_ratio = ratio
        self._render(ratio)
        if self._command is not None:
            self._command(value)

    def _on_press(self, event):
        self._active = True
        self._hover = True
        self._hover_mix = 1.0
        if self._on_seek_start is not None:
            self._on_seek_start()
        self._set_from_x(event.x)

    def _on_motion(self, event):
        if self._active:
            self._set_from_x(event.x)

    def _on_release(self, event):
        if not self._active:
            return
        self._set_from_x(event.x)
        self._active = False
        self._hover = False
        self.refresh()
        if self._on_seek_end is not None:
            self._on_seek_end(self.value())

    def _on_hover_motion(self, event):
        if self._active:
            return
        inside = self._over_thumb(event.x)
        if inside != self._hover:
            self._hover = inside
            self._animate_hover(1.0 if inside else 0.0)

    def _on_leave(self, _event):
        if self._active:
            return
        if self._hover:
            self._hover = False
            self._animate_hover(0.0)

    def _over_thumb(self, x):
        width = max(2, self.winfo_width())
        ratio = (self.value() - self._from) / (self._to - self._from) if self._to != self._from else 0.0
        thumb_x = self._pad + clamp(ratio, 0.0, 1.0) * (width - 2 * self._pad)
        return abs(x - thumb_x) <= self._thumb_radius + 7

    def _animate_hover(self, target):
        if not ANIMATIONS_ENABLED:
            if self._anim_job is not None:
                try:
                    self.after_cancel(self._anim_job)
                except tk.TclError:
                    pass
                self._anim_job = None
            self._hover_mix = target
            self._last_ratio = -1.0
            self.refresh()
            return
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None

        def step():
            difference = target - self._hover_mix
            if abs(difference) < 0.06:
                self._hover_mix = target
                self._anim_job = None
                self.refresh()
                return
            self._hover_mix += difference * 0.4
            self._last_ratio = -1.0
            self.refresh()
            self._anim_job = self.after(15, step)

        step()

    # -------------------------------------------------------------- 绘制
    def _render(self, ratio):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(self["width"])
        if height <= 1:
            height = int(self["height"])
        cy = height / 2.0
        pad = self._pad
        x0 = pad
        x1 = max(pad + 1, width - pad)
        thumb_x = x0 + (x1 - x0) * ratio
        grow = 1.0 + 1.6 * self._hover_mix
        radius = self._thumb_radius * grow

        self.delete("all")
        track_h = self._track_width
        draw_round_rect(self, x0, cy - track_h / 2.0, x1, cy + track_h / 2.0,
                        track_h / 2.0, fill=C.STROKE_SOFT)
        if thumb_x > x0 + 0.5:
            draw_round_rect(self, x0, cy - track_h / 2.0, thumb_x, cy + track_h / 2.0,
                            track_h / 2.0, fill=C.ACCENT)
        if self._hover_mix > 0.02:
            glow_radius = radius + 6.0 * self._hover_mix
            glow = _glow_photo(self, glow_radius, C.ACCENT,
                               max_alpha=0.42 * self._hover_mix)
            self.create_image(thumb_x - glow.width() / 2.0,
                              cy - glow.height() / 2.0, anchor="nw", image=glow)
        diameter = radius * 2.0
        thumb = _circle_photo(self, diameter, "#FFFFFF")
        self.create_image(thumb_x - thumb.width() / 2.0, cy - thumb.height() / 2.0,
                          anchor="nw", image=thumb)


class Dropdown(HoverCanvas):
    """自绘深色下拉选择器(替代浅色 Combobox)。"""

    def __init__(self, master, values, variable, command=None, width=80, height=34,
                 bg=C.DECK, font=None, direction="auto"):
        super().__init__(master, width, height, bg, command=self._toggle)
        self._values = list(values)
        self._var = variable
        # 注意: 不能写回 self._command, 那是“点击时执行”的回调(打开下拉),
        # 选择变化的回调单独保存, 否则点下拉框会变成直接执行选择回调。
        self._select_command = command
        self._font = font or ui_font(9)
        self._direction = direction
        self._popup = None
        self._var.trace_add("write", lambda *_a: self._render())
        self._render()

    def _render(self):
        width, height = self._cw, self._ch
        mix_value = self._mix
        fill = mix(self._canvas_bg, C.SURFACE, max(0.35, mix_value))
        outline = mix(C.STROKE, C.SURFACE_HI, mix_value)
        self.delete("all")
        draw_round_rect(self, 1, 1, width - 1, height - 1, 9, fill=fill,
                        outline=outline, width=1)
        self.create_text(13, height / 2, text=self._var.get(), fill=C.TEXT,
                         font=self._font, anchor="w")
        icon_chevron(self, width - 14, height / 2, 16, C.TEXT_MUTED if not self._hovering else C.TEXT)

    # -------------------------------------------------------------- 弹层
    def _toggle(self):
        if self._popup is not None:
            self._close()
        else:
            self._open()

    def _open(self):
        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.configure(bg=C.STROKE)
        holder = tk.Frame(popup, bg=C.SURFACE)
        holder.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        current = self._var.get()
        for value in self._values:
            row = tk.Label(
                holder, text=value, anchor="w", padx=14, pady=6,
                bg=C.SURFACE, fg=C.ACCENT if value == current else C.TEXT,
                font=self._font,
            )
            row.pack(fill=tk.X)
            row.bind("<Enter>", lambda _e, r=row: r.configure(bg=C.SURFACE_HI))
            row.bind("<Leave>", lambda _e, r=row: r.configure(bg=C.SURFACE))
            row.bind("<Button-1>", lambda _e, v=value: self._choose(v))
        popup.update_idletasks()
        width = max(self._cw, 120)
        height = popup.winfo_reqheight()
        x = self.winfo_rootx()
        space_below = self.winfo_screenheight() - (self.winfo_rooty() + self._ch + 6) - 12
        if self._direction == "up":
            open_up = True
        elif self._direction == "down":
            open_up = False
        else:
            open_up = space_below < height
        if open_up:
            y = self.winfo_rooty() - height - 6
        else:
            y = self.winfo_rooty() + self._ch + 6
        x = int(clamp(x, 8, self.winfo_screenwidth() - width - 8))
        y = int(clamp(y, 8, self.winfo_screenheight() - height - 8))
        popup.wm_geometry(f"{width}x{height}+{x}+{y}")
        # 先真正映射窗口再设置置顶/抓取, 否则 Tk 会把待生效的位置重置到 (0,0)
        popup.update()
        popup.attributes("-topmost", True)
        popup.grab_set()
        popup.bind("<Escape>", lambda _e: self._close())
        popup.bind("<Button-1>", self._on_popup_click)
        popup.focus_force()
        self._popup = popup
        self._render()

    def _on_popup_click(self, event):
        if event.widget is self._popup or str(event.widget).endswith("!frame"):
            self._close()

    def _choose(self, value):
        self._close()
        if value != self._var.get():
            self._var.set(value)
            if self._select_command is not None:
                self._select_command()

    def _close(self):
        if self._popup is None:
            return
        try:
            self._popup.grab_release()
        except tk.TclError:
            pass
        try:
            self._popup.destroy()
        except tk.TclError:
            pass
        self._popup = None


class EmptyStateView(tk.Canvas):
    """未加载视频时的引导卡片。"""

    def __init__(self, master, on_open=None, bg=C.VIDEO):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self._on_open = on_open
        self._hover_button = False
        self._button_rect = None
        self.configure(cursor="arrow")
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _render(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            return
        self.delete("all")
        card_w = min(540.0, max(360.0, width - 80))
        card_h = 286.0
        cx = width / 2.0
        cy = height / 2.0
        x0, y0 = cx - card_w / 2, cy - card_h / 2
        x1, y1 = cx + card_w / 2, cy + card_h / 2
        icon_cy = y0 + 62
        glow = _glow_photo(self, 48, C.ACCENT, max_alpha=0.16)
        self.create_image(cx - glow.width() / 2.0, icon_cy - glow.height() / 2.0,
                          anchor="nw", image=glow)
        draw_round_rect(self, x0, y0, x1, y1, 20,
                        fill=C.CARD, outline=C.STROKE, width=1)
        circle = _circle_photo(self, 52, mix(C.CARD, C.ACCENT, 0.14),
                               mix(C.STROKE, C.ACCENT, 0.35), 1)
        self.create_image(cx - circle.width() / 2.0, icon_cy - circle.height() / 2.0,
                          anchor="nw", image=circle)
        icon_play(self, cx, icon_cy, 24, C.ACCENT)
        self.create_text(cx, y0 + 116, text="Nobplaypot", fill=C.TEXT, font=ui_font(18, True))
        self.create_text(cx, y0 + 148, text="把视频文件拖到这里，或选择本地文件",
                         fill=C.TEXT_MUTED, font=ui_font(10))
        self.create_text(cx, y0 + 178, text="MP4 · MKV · AVI · MOV · WEBM · TS · RMVB",
                         fill=C.TEXT_DIM, font=ui_font(9))
        button_w, button_h = 146.0, 40.0
        bx0 = cx - button_w / 2
        by0 = y0 + 208
        fill = C.ACCENT_HOVER if self._hover_button else C.ACCENT
        draw_round_rect(self, bx0, by0, bx0 + button_w, by0 + button_h, 20,
                        fill=fill, outline=fill, width=1)
        self.create_text(cx, by0 + button_h / 2, text="选择视频文件",
                         fill="#0A0D15", font=ui_font(10, True))
        self._button_rect = (bx0, by0, bx0 + button_w, by0 + button_h)

    def _inside_button(self, x, y):
        if not self._button_rect:
            return False
        return self._button_rect[0] <= x <= self._button_rect[2] and \
            self._button_rect[1] <= y <= self._button_rect[3]

    def _on_motion(self, event):
        inside = self._inside_button(event.x, event.y)
        if inside != self._hover_button:
            self._hover_button = inside
            self.configure(cursor="hand2" if inside else "arrow")
            self._render()

    def _on_leave(self, _event):
        if self._hover_button:
            self._hover_button = False
            self.configure(cursor="arrow")
            self._render()

    def _on_click(self, event):
        if self._inside_button(event.x, event.y) and self._on_open is not None:
            self._on_open()


class FileList(tk.Canvas):
    """文件列表: 自绘行、悬停、选中、滚动条, 完全不依赖 ttk。"""

    ROW_H = 40

    def __init__(self, master, on_open=None, on_select=None, bg=C.DECK):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, takefocus=1)
        self._on_open = on_open
        self._on_select = on_select
        self._bg = bg
        self._items = []
        self._hover_index = -1
        self._selected_index = -1
        self._offset = 0
        self._font = tkfont.Font(family=_UI_FAMILY, size=10)
        self._font_small = tkfont.Font(family=_UI_FAMILY, size=9)
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Up>", lambda _e: self._move(-1))
        self.bind("<Down>", lambda _e: self._move(1))
        self.bind("<Return>", lambda _e: self._open_selected())

    # ---------------------------------------------------------------- API
    def set_items(self, items):
        self._items = list(items)
        self._selected_index = -1
        self._hover_index = -1
        self._offset = 0
        self._render()

    def selected_item(self):
        if 0 <= self._selected_index < len(self._items):
            return self._items[self._selected_index]
        return None

    def select_first(self):
        if self._items:
            self._selected_index = 0
            self._render()
            if self._on_select is not None:
                self._on_select(self._items[0])

    # -------------------------------------------------------------- 交互
    def _max_offset(self):
        return max(0, len(self._items) * self.ROW_H - self.winfo_height() + 6)

    def _index_at(self, y):
        index = int((y + self._offset) // self.ROW_H)
        if 0 <= index < len(self._items):
            return index
        return -1

    def _on_motion(self, event):
        index = self._index_at(event.y)
        if index != self._hover_index:
            self._hover_index = index
            self._render()

    def _on_leave(self, _event):
        if self._hover_index != -1:
            self._hover_index = -1
            self._render()

    def _on_click(self, event):
        self.focus_set()
        index = self._index_at(event.y)
        if index < 0:
            return
        self._selected_index = index
        self._render()
        if self._on_select is not None:
            self._on_select(self._items[index])

    def _on_double_click(self, event):
        index = self._index_at(event.y)
        if index >= 0:
            self._selected_index = index
            self._render()
            self._open_selected()

    def _open_selected(self):
        item = self.selected_item()
        if item is not None and self._on_open is not None:
            self._on_open(item)

    def _move(self, step):
        if not self._items:
            return
        index = clamp(self._selected_index + step, 0, len(self._items) - 1)
        if index == self._selected_index:
            return
        self._selected_index = index
        self._ensure_visible(index)
        self._render()
        if self._on_select is not None:
            self._on_select(self._items[index])

    def _ensure_visible(self, index):
        top = index * self.ROW_H
        bottom = top + self.ROW_H
        if top < self._offset:
            self._offset = top
        elif bottom > self._offset + self.winfo_height():
            self._offset = bottom - self.winfo_height()
        self._offset = clamp(self._offset, 0, self._max_offset())

    def _on_wheel(self, event):
        step = -1 if event.delta > 0 else 1
        self._offset = clamp(self._offset + step * self.ROW_H * 2, 0, self._max_offset())
        self._hover_index = -1
        self._render()

    # -------------------------------------------------------------- 绘制
    def _fit(self, text, font, max_width):
        if font.measure(text) <= max_width:
            return text
        ellipsis = "…"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if font.measure(text[:middle] + ellipsis) <= max_width:
                low = middle
            else:
                high = middle - 1
        return text[:low] + ellipsis

    def _render(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            return
        self.delete("all")
        self._offset = clamp(self._offset, 0, self._max_offset())
        if not self._items:
            self.create_text(width / 2, height / 2 - 14, text="这里没有可显示的文件",
                             fill=C.TEXT_MUTED, font=ui_font(10))
            self.create_text(width / 2, height / 2 + 14, text="换个文件夹，或把筛选切换为「所有文件」",
                             fill=C.TEXT_DIM, font=ui_font(9))
            return

        first = max(0, int(self._offset // self.ROW_H))
        visible = height // self.ROW_H + 2
        last = min(len(self._items), first + visible)
        scroll_room = self._max_offset() > 0
        right_limit = width - (26 if scroll_room else 14)

        for index in range(first, last):
            item = self._items[index]
            top = index * self.ROW_H - self._offset
            row_y0 = top + 4
            row_y1 = top + self.ROW_H - 4
            if index == self._selected_index:
                draw_round_rect(self, 6, row_y0, right_limit, row_y1, 9,
                                fill=C.ACCENT_SOFT, outline="")
                self.create_rectangle(0, row_y0 + 8, 3, row_y1 - 8,
                                      fill=C.ACCENT, outline="")
            elif index == self._hover_index:
                draw_round_rect(self, 6, row_y0, right_limit, row_y1, 9,
                                fill=C.SURFACE, outline="")
            center_y = (row_y0 + row_y1) / 2
            if item["is_dir"]:
                icon_folder(self, 28, center_y, 18, "#5D6C93")
                name_color = C.TEXT
            else:
                icon_film(self, 28, center_y, 18, "#3F4761")
                name_color = "#CBD3E3"
            if item["is_dir"]:
                name_max = right_limit - 52
                size_text = ""
            else:
                size_text = format_size(item.get("size", 0))
                name_max = right_limit - 52 - (self._font_small.measure(size_text) + 20 if size_text else 0)
            name = self._fit(item["name"], self._font, max(40, name_max))
            self.create_text(50, center_y, text=name, fill=name_color,
                             font=self._font, anchor="w")
            if size_text:
                self.create_text(right_limit - 12, center_y, text=size_text,
                                 fill=C.TEXT_DIM, font=self._font_small, anchor="e")

        if scroll_room:
            track_x = width - 10
            self.create_line(track_x, 10, track_x, height - 10, width=3,
                             capstyle="round", fill=C.STROKE_SOFT)
            total = len(self._items) * self.ROW_H
            ratio = height / float(total)
            thumb_h = max(28.0, (height - 20) * ratio)
            travel = (height - 20) - thumb_h
            start = 10 + travel * (self._offset / max(1, self._max_offset()))
            self.create_line(track_x, start, track_x, start + thumb_h, width=4,
                             capstyle="round", fill=C.SURFACE_HI)


class Alert(tk.Toplevel):
    """深色模态提示框, 替代系统 messagebox。"""

    def __init__(self, master, title, message, kind="info"):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.configure(bg=C.BG)
        self.resizable(False, False)
        self.transient(master)
        body = tk.Frame(self, bg=C.BG, padx=26, pady=22)
        body.pack(fill=tk.BOTH, expand=True)
        accent = C.DANGER if kind == "error" else (C.ACCENT if kind == "info" else C.OK)
        head = tk.Frame(body, bg=C.BG)
        head.pack(fill=tk.X)
        badge = tk.Canvas(head, width=30, height=30, bg=C.BG, highlightthickness=0)
        badge.pack(side=tk.LEFT)
        badge_circle = _circle_photo(badge, 28, mix(C.BG, accent, 0.16),
                                     mix(C.BG, accent, 0.5), 1)
        badge.create_image(15 - badge_circle.width() / 2.0,
                           15 - badge_circle.height() / 2.0,
                           anchor="nw", image=badge_circle)
        badge.create_text(15, 15, text="!" if kind == "error" else "i", fill=accent,
                          font=ui_font(12, True))
        tk.Label(head, text=title, bg=C.BG, fg=C.TEXT, font=ui_font(12, True),
                 anchor="w").pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(body, text=message, bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(10),
                 justify=tk.LEFT, anchor="w", wraplength=380).pack(fill=tk.X, pady=(16, 20))
        footer = tk.Frame(body, bg=C.BG)
        footer.pack(fill=tk.X)
        button = RoundedButton(footer, text="知道了", command=self.destroy, variant="primary",
                               width=104, height=36, bg=C.BG)
        button.pack(side=tk.RIGHT)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        width = max(400, self.winfo_reqwidth())
        height = self.winfo_reqheight()
        position_center(self, master, width, height)
        self.deiconify()
        apply_dark_titlebar(self, caption=C.BG)
        self.grab_set()
        self.focus_force()
        self.wait_window(self)


def position_center(window, master, width, height):
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    try:
        mx, my = master.winfo_rootx(), master.winfo_rooty()
        mw, mh = master.winfo_width(), master.winfo_height()
    except tk.TclError:
        mx, my, mw, mh = 0, 0, screen_w, screen_h
    if mw <= 1 or mh <= 1:
        mx, my, mw, mh = 0, 0, screen_w, screen_h
    x = mx + (mw - width) // 2
    y = my + (mh - height) // 2
    x = int(clamp(x, 0, max(0, screen_w - width)))
    y = int(clamp(y, 0, max(0, screen_h - height)))
    window.geometry(f"{int(width)}x{int(height)}+{x}+{y}")


def center_on_screen(window, width, height):
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    width = int(min(width, screen_w - 80))
    height = int(min(height, screen_h - 120))
    x = int(max(0, (screen_w - width) / 2))
    y = int(max(0, (screen_h - height) / 2 - 24))
    window.geometry(f"{width}x{height}+{x}+{y}")
    return width, height


class OpenVideoDialog(tk.Toplevel):
    """深色自绘文件选择框(替代浅色系统 filedialog)。"""

    def __init__(self, master, start_dir=None, title="打开视频"):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.configure(bg=C.BG)
        self.transient(master)
        self.result = None
        self._filter_video = True
        self._dir = start_dir if start_dir and os.path.isdir(start_dir) else os.path.expanduser("~")
        self._place_buttons = []
        self._build()
        self.minsize(860, 620)
        dialog_w = min(980, self.winfo_screenwidth() - 80)
        dialog_h = min(700, self.winfo_screenheight() - 100)
        self.minsize(min(860, dialog_w), min(620, dialog_h))
        position_center(self, master, dialog_w, dialog_h)
        self.deiconify()
        apply_dark_titlebar(self, caption=C.BG)
        self.grab_set()
        self.focus_force()
        self._navigate(self._dir)
        self.wait_window(self)

    # -------------------------------------------------------------- 构建
    def _build(self):
        header = tk.Frame(self, bg=C.BG, padx=22, pady=18)
        header.pack(fill=tk.X)
        up_button = RoundedButton(
            header, variant="icon", width=36, height=36, radius=9, bg=C.BG,
            icon=icon_arrow_up, icon_size=16, command=self._go_up, tooltip="上一级",
        )
        up_button.pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value=self._dir)
        entry = tk.Entry(
            header, textvariable=self.path_var, bg=C.SURFACE, fg=C.TEXT,
            relief=tk.FLAT, bd=0, insertbackground=C.TEXT,
            highlightthickness=1, highlightbackground=C.STROKE,
            highlightcolor=C.ACCENT, font=ui_font(10),
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0), ipady=7, ipadx=6)
        entry.bind("<Return>", self._on_path_enter)
        entry.bind("<Escape>", self._on_escape)
        self.path_entry = entry

        body = tk.Frame(self, bg=C.BG, padx=22)
        body.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(body, bg=C.BG, width=168)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="快速访问", bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9),
                 anchor="w").pack(fill=tk.X, pady=(2, 8))
        home = os.path.expanduser("~")
        places = [
            ("桌面", os.path.join(home, "Desktop")),
            ("下载", os.path.join(home, "Downloads")),
            ("视频", os.path.join(home, "Videos")),
            ("文档", os.path.join(home, "Documents")),
            ("用户目录", home),
        ]
        for label, path in places:
            if os.path.isdir(path):
                button = RoundedButton(
                    sidebar, text=label, variant="menu", width=160, height=32, radius=8,
                    bg=C.BG, align="left", pad_x=12, font=ui_font(10),
                    command=lambda p=path: self._navigate(p),
                )
                button.pack(fill=tk.X, pady=1)
                button._target_path = path
                self._place_buttons.append(button)
        tk.Label(sidebar, text="磁盘", bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9),
                 anchor="w").pack(fill=tk.X, pady=(14, 8))
        for letter in "CDEFGHIJ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                button = RoundedButton(
                    sidebar, text=drive, variant="menu", width=160, height=32, radius=8,
                    bg=C.BG, align="left", pad_x=12, font=ui_font(10),
                    command=lambda p=drive: self._navigate(p),
                )
                button.pack(fill=tk.X, pady=1)
                button._target_path = drive
                self._place_buttons.append(button)

        list_wrap = tk.Frame(body, bg=C.DECK, highlightthickness=1,
                             highlightbackground=C.STROKE, highlightcolor=C.STROKE)
        list_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(16, 0))
        self.file_list = FileList(
            list_wrap, on_open=self._on_item_open, on_select=self._on_item_select, bg=C.DECK,
        )
        self.file_list.pack(fill=tk.BOTH, expand=True)

        footer = tk.Frame(self, bg=C.BG, padx=22, pady=18)
        footer.pack(fill=tk.X)
        self.filter_var = tk.StringVar(value="视频文件")
        Dropdown(footer, values=("视频文件", "所有文件"), variable=self.filter_var,
                 command=self._on_filter_change, width=116, height=36, bg=C.BG,
                 direction="up").pack(side=tk.LEFT)
        self.count_label = tk.Label(footer, text="", bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9))
        self.count_label.pack(side=tk.LEFT, padx=(12, 0))
        open_button = RoundedButton(footer, text="打开", variant="primary", width=100, height=38,
                                    bg=C.BG, command=self._accept)
        open_button.pack(side=tk.RIGHT)
        cancel_button = RoundedButton(footer, text="取消", variant="ghost", width=92, height=38,
                                      bg=C.BG, command=self._cancel)
        cancel_button.pack(side=tk.RIGHT, padx=(0, 10))
        self.bind("<Escape>", lambda _e: self._on_escape())

    # -------------------------------------------------------------- 导航
    def _navigate(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            log.debug("文件选择框: 目录不存在 %s", path)
            return
        self._dir = path
        self.path_var.set(path)
        log.debug("文件选择框: 浏览 %s", path)
        self._refresh()

    def _refresh(self):
        items = []
        error_text = ""
        try:
            with os.scandir(self._dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            items.append({"name": entry.name, "path": entry.path,
                                          "is_dir": True, "size": 0})
                        elif not self._filter_video or entry.name.lower().endswith(VIDEO_EXTS):
                            stat = entry.stat()
                            items.append({"name": entry.name, "path": entry.path,
                                          "is_dir": False, "size": stat.st_size})
                    except OSError:
                        continue
        except OSError as error:
            error_text = f"无法读取目录: {error.strerror or error}"
        items.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        items = items[:1500]
        self.file_list.set_items(items)
        self.count_label.configure(text=error_text or f"{len(items)} 个项目")
        for button in self._place_buttons:
            button.set_active(os.path.normcase(button._target_path) == os.path.normcase(self._dir))

    def _go_up(self):
        parent = os.path.dirname(self._dir.rstrip("\\/"))
        if parent and os.path.normcase(parent) != os.path.normcase(self._dir):
            self._navigate(parent)

    def _on_path_enter(self, _event):
        path = self.path_var.get().strip().strip('"')
        if os.path.isdir(path):
            self._navigate(path)
        elif os.path.isfile(path):
            self.result = path
            self.destroy()
        else:
            self.path_var.set(self._dir)
        return "break"

    def _on_item_open(self, item):
        if item["is_dir"]:
            self._navigate(item["path"])
        else:
            self.result = item["path"]
            self.destroy()

    def _on_item_select(self, _item):
        return

    def _on_filter_change(self):
        self._filter_video = self.filter_var.get() == "视频文件"
        log.debug("文件选择框: 筛选 %s", self.filter_var.get())
        self._refresh()

    def _accept(self):
        item = self.file_list.selected_item()
        if item is not None:
            if item["is_dir"]:
                self._navigate(item["path"])
            else:
                self.result = item["path"]
                self.destroy()
            return
        current = self.path_var.get().strip().strip('"')
        if os.path.isfile(current):
            self.result = current
            self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def _on_escape(self, _event=None):
        self._cancel()
        return "break"


# ---------------------------------------------------------------------------
# 流式传输界面: 入口菜单 / 主持配置 / 加入配置 / 同步面板
# ---------------------------------------------------------------------------
def _stream_field_row(parent, label_text, variable, width=24):
    row = tk.Frame(parent, bg=C.BG)
    row.pack(fill=tk.X, pady=5)
    tk.Label(row, text=label_text, bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(10),
             width=11, anchor="w").pack(side=tk.LEFT)
    entry = tk.Entry(
        row, textvariable=variable, bg=C.SURFACE, fg=C.TEXT, relief=tk.FLAT, bd=0,
        insertbackground=C.TEXT, highlightthickness=1, highlightbackground=C.STROKE,
        highlightcolor=C.ACCENT, font=mono_font(10), width=width, justify=tk.CENTER,
    )
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7, padx=(10, 0))
    return entry


class _StreamDialogBase(tk.Toplevel):
    """深色流式配置弹窗基类。"""

    def __init__(self, master, title, width=460):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.configure(bg=C.BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self._drag_offset = None
        self._drag_active = False
        # 整个顶部区域(含标题下方留白)都能拖动, 不只是文字标签
        self.bind("<Button-1>", self._start_drag_top, add="+")
        self.bind("<B1-Motion>", self._drag_top, add="+")
        self.bind("<ButtonRelease-1>", self._end_drag_top, add="+")
        self.body = tk.Frame(self, bg=C.BG, padx=28, pady=24)
        self.body.pack(fill=tk.BOTH, expand=True)
        self.error_label = tk.Label(self.body, text="", bg=C.BG, fg=C.DANGER,
                                    font=ui_font(9), anchor="w")
        self.error_label.pack(fill=tk.X, pady=(8, 0))
        self.bind("<Escape>", lambda _e: self._on_escape())
        # 点窗口右上角关闭等同于取消, 保证 result/choice 一定有值
        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))

    def _start_drag_top(self, event):
        self._drag_active = False
        self._drag_offset = None
        if event.y_root - self.winfo_rooty() > 54:
            return
        self._drag_active = True
        self._drag_offset = (event.x_root - self.winfo_x(),
                             event.y_root - self.winfo_y())

    def _drag_top(self, event):
        if not self._drag_active or self._drag_offset is None:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    def _end_drag_top(self, _event=None):
        self._drag_active = False
        self._drag_offset = None

    def show_error(self, text):
        self.error_label.configure(text=text)

    def finish(self, result=None):
        self.result = result
        self.destroy()

    def _on_escape(self):
        self.finish()
        return "break"

    def present(self):
        self.update_idletasks()
        position_center(self, self.master, self.winfo_reqwidth() + 8,
                        self.winfo_reqheight())
        self.deiconify()
        apply_dark_titlebar(self, caption=C.BG)
        self.grab_set()
        self.focus_force()
        self.wait_window(self)

    def _footer(self, primary_text, primary_command, cancel=True):
        footer = tk.Frame(self.body, bg=C.BG)
        footer.pack(fill=tk.X, pady=(18, 0))
        if cancel:
            RoundedButton(footer, text="取消", variant="ghost", width=96, height=38,
                          bg=C.BG, command=self.finish).pack(side=tk.RIGHT)
        RoundedButton(footer, text=primary_text, variant="primary", width=120, height=38,
                      bg=C.BG, command=primary_command).pack(side=tk.RIGHT, padx=(0, 10))


class StreamMenuDialog(_StreamDialogBase):
    """左下角流式按钮的入口菜单。"""

    def __init__(self, master, mode, host_info="", join_info=""):
        super().__init__(master, "流式传输", width=440)
        self.choice = None
        tk.Label(self.body, text="流式传输", bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(self.body, text="一台设备主持播放并推流, 其他设备加入后同步观看与遥控。",
                 bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(4, 16))
        if mode == "off":
            RoundedButton(
                self.body, text="主持流式传输", variant="primary", width=380, height=46,
                bg=C.BG, command=lambda: self.finish("host"), font=ui_font(11, True),
            ).pack(fill=tk.X, pady=(0, 8))
            RoundedButton(
                self.body, text="加入流式传输", variant="subtle", width=380, height=46,
                bg=C.BG, command=lambda: self.finish("join"), font=ui_font(11),
            ).pack(fill=tk.X)
            tk.Label(
                self.body,
                text="主持: 开放指令/流端口, 把当前播放推给其他设备\n"
                     "加入: 接收并渲染主持推来的画面(播放控制交给主持执行)",
                bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w", justify=tk.LEFT,
            ).pack(fill=tk.X, pady=(14, 0))
        elif mode == "host":
            RoundedButton(
                self.body, text="停止主持", variant="primary", width=380, height=46,
                bg=C.BG, command=lambda: self.finish("stop_host"), font=ui_font(11, True),
            ).pack(fill=tk.X)
            tk.Label(self.body, text=host_info or "正在主持流式会话", bg=C.BG,
                     fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
                     wraplength=380).pack(fill=tk.X, pady=(14, 0))
        else:
            RoundedButton(
                self.body, text="退出会话", variant="primary", width=380, height=46,
                bg=C.BG, command=lambda: self.finish("leave"), font=ui_font(11, True),
            ).pack(fill=tk.X)
            tk.Label(self.body, text=join_info or "已加入流式会话", bg=C.BG,
                     fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
                     wraplength=380).pack(fill=tk.X, pady=(14, 0))
        self._footer("取消", self.finish, cancel=False)

    def finish(self, result=None):
        # 按钮用它传 "host"/"join"/"stop_host"/"leave"; 取消时传 None
        self.choice = result
        super().finish(result)


class HostStreamDialog(_StreamDialogBase):
    """主持配置: 指令端口 + 流端口 + 房间密码。"""

    def __init__(self, master, cmd_port=STREAM_DEFAULT_CMD_PORT,
                 udp_port=STREAM_DEFAULT_UDP_PORT):
        super().__init__(master, "主持流式传输", width=460)
        tk.Label(self.body, text="主持流式传输", bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(
            self.body,
            text="开放 TCP 指令端口与媒体端口; 客户端主动连入媒体端口, 主持端转码后经可靠 TCP 推流。",
            bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
            wraplength=380,
        ).pack(fill=tk.X, pady=(4, 12))
        self.cmd_var = tk.StringVar(value=str(cmd_port))
        self.udp_var = tk.StringVar(value=str(udp_port))
        _stream_field_row(self.body, "指令端口", self.cmd_var)
        _stream_field_row(self.body, "媒体端口（主持监听）", self.udp_var)

        tk.Frame(self.body, bg=C.STROKE, height=1).pack(fill=tk.X, pady=(14, 10))
        password_line = tk.Frame(self.body, bg=C.BG)
        password_line.pack(fill=tk.X)
        self.use_password_var = tk.BooleanVar(value=True)
        ToggleSwitch(password_line, self.use_password_var, bg=C.BG).pack(side=tk.LEFT)
        tk.Label(password_line, text="启用房间密码", bg=C.BG, fg=C.TEXT,
                 font=ui_font(10), anchor="w").pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(password_line, text="客户端加入时由主持端核验", bg=C.BG,
                 fg=C.TEXT_DIM, font=ui_font(8), anchor="e").pack(side=tk.RIGHT)

        password_row = tk.Frame(self.body, bg=C.BG)
        password_row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(password_row, text="房间密码", bg=C.BG, fg=C.TEXT_MUTED,
                 font=ui_font(9), width=8, anchor="w").pack(side=tk.LEFT)
        self.password_var = tk.StringVar(value="")
        self.password_entry = tk.Entry(
            password_row, textvariable=self.password_var, show="*",
            bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT, relief=tk.FLAT,
            bd=0, highlightthickness=1, highlightbackground=C.STROKE,
            highlightcolor=C.ACCENT, font=ui_font(10),
            disabledbackground=C.DECK, disabledforeground=C.TEXT_DIM,
        )
        self.password_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        tk.Label(self.body, text="密码只保存在主持端, 不会随房间信息发给客户端。",
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8), anchor="w").pack(
            fill=tk.X, pady=(4, 0))
        self.use_password_var.trace_add("write", lambda *_a: self._sync_password_state())
        self._sync_password_state()
        self._footer("开始主持", self._confirm)

    def _sync_password_state(self):
        enabled = bool(self.use_password_var.get())
        try:
            self.password_entry.configure(
                state="normal" if enabled else "disabled",
                bg=C.SURFACE if enabled else C.DECK,
            )
        except tk.TclError:
            pass

    def _confirm(self):
        try:
            cmd_port = int(self.cmd_var.get().strip())
            udp_port = int(self.udp_var.get().strip())
        except ValueError:
            self.show_error("端口必须是 1-65535 之间的数字")
            return
        if not (1 <= cmd_port <= 65535 and 1 <= udp_port <= 65535):
            self.show_error("端口必须是 1-65535 之间的数字")
            return
        if cmd_port == udp_port:
            self.show_error("指令端口和流端口不能相同")
            return
        password = ""
        if self.use_password_var.get():
            password = self.password_var.get().strip()
            if not password:
                self.show_error("启用密码时需要填写房间密码")
                return
            if len(password) > 64:
                self.show_error("房间密码最长 64 个字符")
                return
        self.finish((cmd_port, udp_port, password))


class RoomPasswordDialog(_StreamDialogBase):
    """客户端加入带密码房间时弹出的密码框(密码提交给主持核验)。"""

    def __init__(self, master, submit, cancel, error=""):
        super().__init__(master, "房间密码", width=380)
        self.overrideredirect(True)
        self._submit = submit
        self._cancel = cancel
        self._waiting = False
        self._drag_offset = None
        self.resizable(False, False)
        header = tk.Frame(self.body, bg=C.BG)
        header.pack(fill=tk.X)
        title = tk.Label(header, text="房间密码", bg=C.BG, fg=C.TEXT,
                         font=ui_font(13, True), anchor="w")
        title.pack(side=tk.LEFT)
        close = RoundedButton(header, text="×", variant="ghost", width=30, height=28,
                              radius=8, bg=C.BG, font=ui_font(12),
                              command=self._do_cancel, tooltip="关闭")
        close.pack(side=tk.RIGHT)
        tk.Label(self.body, text="主持开启了房间密码, 请输入密码后加入。",
                 bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w").pack(
            fill=tk.X, pady=(4, 12))
        self.password_var = tk.StringVar(value="")
        self.entry = tk.Entry(
            self.body, textvariable=self.password_var, show="*",
            bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT, relief=tk.FLAT,
            bd=0, highlightthickness=1, highlightbackground=C.STROKE,
            highlightcolor=C.ACCENT, font=ui_font(10),
        )
        self.entry.pack(fill=tk.X, ipady=6)
        self.entry.bind("<Return>", lambda _e: self._do_submit())
        self.join_button = RoundedButton(
            self.body, text="加入房间", variant="primary", width=140, height=38,
            bg=C.BG, command=self._do_submit,
        )
        self.join_button.pack(fill=tk.X, pady=(16, 0))
        RoundedButton(self.body, text="取消", variant="ghost", width=140, height=38,
                      bg=C.BG, command=self._do_cancel).pack(fill=tk.X, pady=(8, 0))
        self.error_label.configure(text=error)
        self.protocol("WM_DELETE_WINDOW", self._do_cancel)
        self.bind("<Escape>", lambda _e: self._do_cancel())

    def present_async(self):
        """非阻塞弹出: 客户端网络线程继续收主持的消息。"""
        self.update_idletasks()
        position_center(self, self.master, self.winfo_reqwidth() + 8,
                        self.winfo_reqheight())
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_force()
        self.entry.focus_set()

    def set_waiting(self, waiting):
        self._waiting = bool(waiting)
        try:
            self.entry.configure(state="disabled" if waiting else "normal")
            self.join_button.set_enabled(not waiting)
            if not waiting:
                self.entry.focus_set()
        except tk.TclError:
            pass

    def _do_submit(self):
        if self._waiting:
            return
        password = self.password_var.get().strip()
        if not password:
            self.show_error("请输入房间密码")
            return
        self.show_error("")
        self.set_waiting(True)
        self._submit(password)

    def _do_cancel(self):
        self.destroy()
        self._cancel()


class JoinStreamDialog(_StreamDialogBase):
    """加入配置: 指令地址 + 流接收地址。"""

    def __init__(self, master, cmd_addr="127.0.0.1:5000", stream_addr="127.0.0.1:5001"):
        super().__init__(master, "加入流式传输", width=480)
        tk.Label(self.body, text="加入流式传输", bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(
            self.body,
            text="填写主持的指令地址, 以及主持的媒体流地址\n"
                 "(客户端主动连接主持的媒体端口, 本机无需开放任何入站端口)。",
            bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(4, 12))
        self.cmd_var = tk.StringVar(value=cmd_addr)
        self.udp_var = tk.StringVar(value=stream_addr)
        _stream_field_row(self.body, "指令地址", self.cmd_var)
        _stream_field_row(self.body, "主持流地址", self.udp_var)
        self._footer("加入会话", self._confirm)

    def _confirm(self):
        cmd = parse_host_port(self.cmd_var.get())
        stream = parse_host_port(self.udp_var.get())
        if cmd is None:
            self.show_error("指令地址格式不对, 例如 127.0.0.1:5000")
            return
        if stream is None:
            self.show_error("主持流地址格式不对, 例如 192.168.1.10:5001")
            return
        self.finish((cmd[0], cmd[1], stream[0], stream[1]))


class ToggleSwitch(tk.Canvas):
    """抗锯齿的开关控件。"""

    def __init__(self, master, variable, bg=C.BG, width=48, height=26):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0, cursor="hand2")
        self._var = variable
        self._bg = bg
        self._cw = width
        self._ch = height
        self.bind("<Button-1>", self._toggle)
        self._var.trace_add("write", lambda *_a: self._render())
        self._render()

    def _toggle(self, _event=None):
        self._var.set(not bool(self._var.get()))

    def _render(self):
        self.delete("all")
        on = bool(self._var.get())
        fill = C.ACCENT if on else C.SURFACE_HI
        draw_round_rect(self, 1, 4, self._cw - 1, self._ch - 4,
                        (self._ch - 8) / 2, fill=fill)
        knob = 16
        x = self._cw - 6 - knob / 2 if on else 6 + knob / 2
        image = _circle_photo(self, knob, "#FFFFFF")
        self.create_image(x - image.width() / 2.0,
                          self._ch / 2.0 - image.height() / 2.0,
                          anchor="nw", image=image)


class SettingsDialog(_StreamDialogBase):
    """设置窗口: 左侧选项卡 + 右侧滚动内容。"""

    PAGES = (("general", "常规"), ("appearance", "外观"),
             ("chat", "聊天室"), ("streaming", "流式传输"))

    def __init__(self, master, settings, on_save):
        super().__init__(master, "设置", width=860)
        self.overrideredirect(True)
        self.resizable(False, False)
        self._draft = dict(settings)
        self._on_save = on_save
        self._vars = {}
        self._label_maps = {}
        self._pages = {}
        self._tab_buttons = {}
        self._drag_offset = None
        self.error_label.pack_forget()

        header = tk.Frame(self.body, bg=C.BG)
        header.pack(fill=tk.X, pady=(0, 8))
        title = tk.Label(header, text="设置", bg=C.BG, fg=C.TEXT,
                         font=ui_font(13, True), anchor="w")
        title.pack(side=tk.LEFT)
        close = RoundedButton(header, text="×", variant="ghost", width=34, height=30,
                              radius=8, bg=C.BG, font=ui_font(13),
                              command=self.finish, tooltip="关闭")
        close.pack(side=tk.RIGHT)
        for widget in (header, title):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        container = tk.Frame(self.body, bg=C.BG)
        container.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(container, bg=C.BG, width=160)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        for key, label in self.PAGES:
            button = RoundedButton(
                sidebar, text=label, variant="ghost", width=146, height=40,
                radius=9, bg=C.BG,
                command=lambda k=key: self._select_tab(k),
            )
            button.pack(fill=tk.X, pady=2)
            self._tab_buttons[key] = button

        content = tk.Frame(container, bg=C.BG, padx=24)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scrollbar = tk.Canvas(content, width=6, bg=C.BG,
                                    highlightthickness=0, bd=0)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        self._scrollbar.bind("<Button-1>", self._on_scrollbar)
        self._scrollbar.bind("<B1-Motion>", self._on_scrollbar)
        self._scroll = tk.Canvas(content, bg=C.BG, highlightthickness=0, bd=0,
                                 width=600, height=520,
                                 yscrollcommand=lambda *_a: self._update_scrollbar())
        self._scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._page_host = tk.Frame(self._scroll, bg=C.BG)
        self._page_window = self._scroll.create_window(
            (0, 0), window=self._page_host, anchor="nw", width=600
        )
        self._page_host.bind(
            "<Configure>",
            lambda _e: self._refresh_scrollregion(),
        )
        self._scroll.bind(
            "<Configure>",
            lambda event: (self._scroll.itemconfigure(self._page_window,
                                                      width=event.width),
                           self._refresh_scrollregion()),
        )
        self.bind("<MouseWheel>", self._on_wheel)

        footer = tk.Frame(self.body, bg=C.BG)
        footer.pack(fill=tk.X, pady=(16, 0))
        RoundedButton(footer, text="恢复默认", variant="ghost", width=104, height=38,
                      bg=C.BG, command=self._reset).pack(side=tk.LEFT)
        RoundedButton(footer, text="取消", variant="ghost", width=96, height=38,
                      bg=C.BG, command=self.finish).pack(side=tk.RIGHT)
        RoundedButton(footer, text="保存", variant="primary", width=120, height=38,
                      bg=C.BG, command=self._save).pack(side=tk.RIGHT, padx=(0, 10))
        self.error_label.pack(fill=tk.X, pady=(8, 0))

        self._select_tab("general")
        self.geometry("860x620")

    def present(self):
        self.update_idletasks()
        position_center(self, self.master, self.winfo_reqwidth() + 8,
                        self.winfo_reqheight())
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()
        self.wait_window(self)

    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(),
                             event.y_root - self.winfo_y())

    def _on_drag(self, event):
        if self._drag_offset is None:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    # ---------------------------------------------------------- 构建辅助
    def _on_wheel(self, event):
        try:
            if self._page_host.winfo_height() <= self._scroll.winfo_height() + 2:
                self._scroll.yview_moveto(0)
                return
            self._scroll.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    def _on_scrollbar(self, event):
        if self._page_host.winfo_height() <= self._scroll.winfo_height() + 2:
            return
        height = self._scrollbar.winfo_height() or 1
        self._scroll.yview_moveto(max(0.0, min(1.0, event.y / height)))

    def _refresh_scrollregion(self):
        try:
            width = max(1, self._scroll.winfo_width())
            content = max(1, self._page_host.winfo_height())
            view = max(1, self._scroll.winfo_height())
            self._scroll.configure(
                scrollregion=(0, 0, width, max(content, view)))
            self._update_scrollbar()
        except tk.TclError:
            pass

    def _update_scrollbar(self):
        bar = self._scrollbar
        try:
            bar.delete("all")
            total = float(self._page_host.winfo_height() or 0)
            view = float(self._scroll.winfo_height() or 0)
            if total <= view + 2:
                return
            first, last = self._scroll.yview()
            height = bar.winfo_height() or 1
            y0 = int(first * height)
            y1 = max(y0 + 24, int(last * height))
            draw_round_rect(bar, 1, y0, 5, min(y1, height), 2, fill=C.STROKE_SOFT)
        except tk.TclError:
            pass

    def _select_tab(self, key):
        for child in self._page_host.winfo_children():
            child.destroy()
        for tab_key, button in self._tab_buttons.items():
            button.set_active(tab_key == key)
        builder = {
            "general": self._build_general,
            "appearance": self._build_appearance,
            "chat": self._build_chat,
            "streaming": self._build_streaming,
        }[key]
        builder(self._page_host)
        self._page_host.update_idletasks()
        self._refresh_scrollregion()
        self._scroll.yview_moveto(0)

    def _page_title(self, page, title, subtitle):
        tk.Label(page, text=title, bg=C.BG, fg=C.TEXT, font=ui_font(12, True),
                 anchor="w").pack(fill=tk.X)
        tk.Label(page, text=subtitle, bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9),
                 anchor="w", justify=tk.LEFT, wraplength=520).pack(fill=tk.X, pady=(2, 14))

    def _row(self, page, title, hint=""):
        row = tk.Frame(page, bg=C.BG)
        row.pack(fill=tk.X, pady=(0, 10))
        left = tk.Frame(row, bg=C.BG)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(left, text=title, bg=C.BG, fg=C.TEXT, font=ui_font(10),
                 anchor="w").pack(fill=tk.X)
        if hint:
            tk.Label(left, text=hint, bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8),
                     anchor="w").pack(fill=tk.X)
        # 固定宽度的右侧控件列: 所有行控件的右边缘都对齐
        right = tk.Frame(row, bg=C.BG, width=300, height=36)
        right.pack(side=tk.RIGHT)
        right.pack_propagate(False)
        return right

    def _dropdown(self, parent, key, options, width=190, on_change=None):
        labels = [label for _value, label in options]
        mapping = {label: value for value, label in options}
        current = self._draft.get(key)
        current_label = next((label for value, label in options if value == current), labels[0])
        var = tk.StringVar(value=current_label)
        self._vars[key] = var
        self._label_maps[key] = mapping
        drop = Dropdown(parent, values=labels, variable=var, width=width,
                        height=34, bg=C.BG, direction="down", command=on_change)
        drop.pack(side=tk.RIGHT)
        return drop

    def _slider(self, parent, key, low, high, fmt, width=190, compact=False):
        var = tk.DoubleVar(value=float(self._draft.get(key, low)))
        self._vars[key] = var
        value_label = tk.Label(parent, text=fmt(var.get()), bg=C.BG, fg=C.TEXT_MUTED,
                               font=mono_font(9), width=10, anchor="e")
        value_label.pack(side=tk.RIGHT)

        def on_change(value):
            value_label.configure(text=fmt(value))

        slider = Slider(parent, variable=var, from_=low, to=high,
                        command=on_change, bg=C.BG, width=width, height=34,
                        compact=compact)
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        return slider

    def _toggle(self, parent, key):
        var = tk.BooleanVar(value=bool(self._draft.get(key, True)))
        self._vars[key] = var
        switch = ToggleSwitch(parent, var, bg=C.BG)
        switch.pack(side=tk.RIGHT)
        switch._render()

    def _entry(self, parent, key, width=8):
        var = tk.StringVar(value=str(self._draft.get(key, "")))
        self._vars[key] = var
        entry = tk.Entry(parent, textvariable=var, width=width, bg=C.SURFACE,
                         fg=C.TEXT, relief=tk.FLAT, bd=0, insertbackground=C.TEXT,
                         highlightthickness=1, highlightbackground=C.STROKE,
                         highlightcolor=C.ACCENT, justify=tk.CENTER,
                         font=mono_font(10))
        entry.pack(side=tk.RIGHT, ipady=6)
        return entry

    def _text_entry(self, parent, key, width=18):
        var = tk.StringVar(value=str(self._draft.get(key, "")))
        self._vars[key] = var
        entry = tk.Entry(parent, textvariable=var, width=width, bg=C.SURFACE,
                         fg=C.TEXT, relief=tk.FLAT, bd=0, insertbackground=C.TEXT,
                         highlightthickness=1, highlightbackground=C.STROKE,
                         highlightcolor=C.ACCENT, justify=tk.LEFT,
                         font=ui_font(10))
        entry.pack(side=tk.RIGHT, ipady=6)
        return entry

    # -------------------------------------------------------------- 页面
    def _build_general(self, page):
        self._page_title(page, "常规", "解码方式与应用行为。")
        row = self._row(page, "解码方式",
                        "硬件解码更省 CPU; 切换后需要重启软件生效。（重启生效）")
        self._dropdown(row, "decode_mode", [
            ("auto", DECODE_LABELS["auto"]),
            ("hardware", DECODE_LABELS["hardware"]),
            ("software", DECODE_LABELS["software"]),
        ])
        row = self._row(page, "默认音量", "打开软件时的初始音量")
        self._slider(row, "volume", 0, 100, lambda v: f"{int(round(v))}%")
        row = self._row(page, "快进/快退步长", "方向键与快进按钮的跳转秒数")
        self._slider(row, "seek_step", 1, 30, lambda v: f"{int(round(v))} 秒")
        row = self._row(page, "打开文件后自动播放", "")
        self._toggle(row, "autoplay")
        row = self._row(page, "记住上次打开的目录", "文件选择框从上次浏览的位置开始")
        self._toggle(row, "remember_dir")
        row = self._row(page, "日志级别", "排查问题时可以调到 DEBUG")
        self._dropdown(row, "log_level", [
            ("DEBUG", "DEBUG"), ("INFO", "INFO"),
            ("WARNING", "WARNING"), ("ERROR", "ERROR"),
        ])

    def _build_appearance(self, page):
        self._page_title(page, "外观",
                         "调整强调色与界面底色; 改动需要重启软件后生效。")

        row = self._row(page, "强调色", "按钮、进度条与高亮使用的颜色（重启生效）")
        chip = tk.Canvas(row, width=28, height=20, bg=C.BG, highlightthickness=0)

        def accent_palette():
            key = self._label_maps["accent"].get(self._vars["accent"].get(), "aurora")
            accent = ACCENTS.get(key, ACCENTS["aurora"])
            chip.delete("all")
            draw_round_rect(chip, 1, 1, 27, 19, 6, fill=accent[0],
                            outline=accent[1], width=1)

        self._dropdown(row, "accent", [
            (key, ACCENT_LABELS[key]) for key in
            ("aurora", "violet", "teal", "rose", "amber")
        ], on_change=lambda: accent_palette())
        chip.pack(side=tk.RIGHT, padx=(0, 10))
        accent_palette()

        row = self._row(page, "界面底色", "控制栏、卡片与输入框的底色（重启生效）")
        surface_chip = tk.Canvas(row, width=28, height=20, bg=C.BG, highlightthickness=0)

        def surface_palette():
            key = self._label_maps["surface"].get(self._vars["surface"].get(), "midnight")
            palette = SURFACES.get(key, SURFACES["midnight"])
            surface_chip.delete("all")
            draw_round_rect(surface_chip, 1, 1, 27, 19, 6,
                            fill=palette["DECK"], outline=palette["STROKE"], width=1)

        self._dropdown(row, "surface", [
            (key, SURFACE_LABELS[key]) for key in ("midnight", "black", "slate")
        ], on_change=lambda: surface_palette())
        surface_chip.pack(side=tk.RIGHT, padx=(0, 10))
        surface_palette()

        row = self._row(page, "悬停动画", "关闭后按钮与滑块的悬停变化直接切换")
        self._toggle(row, "animations")
        row = self._row(page, "缓冲提示缩放", "视频中央「缓冲中」动画的大小倍数")
        self._slider(row, "buffer_scale", 0.5, 2.5,
                     lambda v: f"{v:.1f}x", width=200)

    def _build_chat(self, page):
        self._page_title(page, "聊天室", "用户名、聊天记录与弹幕显示。")
        row = self._row(page, "用户名",
                        "加入房间时显示的名称（4–32 个字符）")
        self._text_entry(row, "username", width=18)
        row = self._row(page, "成员消息频率上限",
                        "同一成员两次发言的最小间隔")
        self._dropdown(row, "chat_rate_limit", [
            (0, "无限制"), (1, "1 秒"), (2, "2 秒"), (3, "3 秒"), (5, "5 秒"),
        ])
        row = self._row(page, "聊天记录上限", "主持端保存的聊天消息条数")
        self._slider(row, "chat_limit", 20, 500, lambda v: f"{int(round(v))} 条")
        row = self._row(page, "消息弹幕", "新聊天消息以弹幕浮动在视频上")
        self._toggle(row, "danmaku")
        row = self._row(page, "弹幕大小", "基础字号, 会随视频尺寸自动缩放")
        self._slider(row, "danmaku_size", 12, 36,
                     lambda v: f"{int(round(v))} px")
        row = self._row(page, "弹幕速度", "数值越大, 弹幕移动越快")
        self._slider(row, "danmaku_speed", 50, 300,
                     lambda v: f"{v / 100:.1f}x")
        row = self._row(page, "弹幕颜色", "")
        self._dropdown(row, "danmaku_color", [
            (key, label) for key, (label, _color) in DANMAKU_COLORS.items()
        ])
        row = self._row(page, "弹幕区域", "弹幕只出现在视频上方的区域")
        self._dropdown(row, "danmaku_area", [
            (value, label) for value, label in DANMAKU_AREAS
        ])

    def _build_streaming(self, page):
        self._page_title(page, "流式传输",
                         "主持端推流参数; 修改后在下次重建推流（客户端加入/换片）时生效。")
        row = self._row(page, "房间人数上限", "包括主持自己, 2-32 人")
        self._slider(row, "max_clients", 2, 32, lambda v: f"{int(round(v))} 人")
        row = self._row(page, "单客户端带宽上限", "转码目标码率, 越高越清晰也越占带宽")
        self._slider(row, "bandwidth", 300, 12000,
                     lambda v: f"{int(round(v))} kbps", width=200)
        row = self._row(page, "推流分辨率", "按视频高度缩放, 不会放大原片")
        self._dropdown(row, "resolution", [
            (value, label) for value, label in RESOLUTION_OPTIONS
        ])
        row = self._row(page, "推流帧率", "转码输出帧率")
        self._dropdown(row, "fps", [
            (15, "15 fps"), (24, "24 fps"), (25, "25 fps"),
            (30, "30 fps"), (60, "60 fps"),
        ])
        row = self._row(page, "音频码率", "")
        self._dropdown(row, "audio_bitrate", [
            (64, "64 kbps"), (96, "96 kbps"), (128, "128 kbps"),
            (192, "192 kbps"), (256, "256 kbps"),
        ])
        row = self._row(page, "x264 编码预设", "越快越省 CPU, 画质略低")
        self._dropdown(row, "preset", [
            ("ultrafast", "ultrafast"), ("superfast", "superfast"),
            ("veryfast", "veryfast"), ("faster", "faster"), ("medium", "medium"),
        ])
        row = self._row(page, "关键帧间隔", "数值越小, 客户端起播与跳转越快")
        self._slider(row, "keyint", 10, 120, lambda v: f"{int(round(v))} 帧")
        row = self._row(page, "客户端缓冲", "接收端缓冲, 网络抖动大时可调高")
        self._slider(row, "client_caching", 200, 5000,
                     lambda v: f"{int(round(v))} ms", width=200)
        row = self._row(page, "心跳间隔", "指令端口心跳包间隔")
        self._slider(row, "heartbeat", 1.0, 10.0, lambda v: f"{v:.1f} 秒")
        row = self._row(page, "离线判定", "超过这个时间没有心跳就判定客户端离线")
        self._slider(row, "stale", 4.0, 60.0, lambda v: f"{v:.1f} 秒")
        row = self._row(page, "默认指令端口", "主持/加入对话框的默认值")
        self._entry(row, "cmd_port")
        row = self._row(page, "默认媒体端口", "主持端监听的默认 TCP 媒体端口")
        self._entry(row, "udp_port")

    # -------------------------------------------------------------- 保存
    def _reset(self):
        self._draft = normalize_settings(dict(DEFAULT_SETTINGS))
        self._vars.clear()
        self._label_maps.clear()
        self._select_tab(self._current_tab() or "general")

    def _current_tab(self):
        for key, button in self._tab_buttons.items():
            if button._active:
                return key
        return "general"

    def _save(self):
        data = dict(self._draft)
        for key, var in self._vars.items():
            if key in self._label_maps:
                data[key] = self._label_maps[key].get(var.get(), data.get(key))
                continue
            try:
                value = var.get()
            except tk.TclError:
                continue
            if isinstance(value, bool):
                data[key] = value
            elif isinstance(value, str):
                if key in ("cmd_port", "udp_port"):
                    try:
                        data[key] = int(value.strip())
                    except ValueError:
                        self.show_error("端口必须是数字")
                        return
                else:
                    data[key] = value.strip()
            else:
                data[key] = value
        username = str(data.get("username") or "").strip()
        if not (4 <= len(username) <= 32):
            self.show_error("用户名长度需要 4–32 个字符")
            return
        data["username"] = username
        validated = normalize_settings(data)
        if validated["cmd_port"] == validated["udp_port"]:
            self.show_error("指令端口和流端口不能相同")
            return
        self._on_save(validated)
        self.destroy()


class RoomMembersDialog(tk.Toplevel):
    """房间成员列表: 主持在最上面, 房客按加入顺序排列, 可滚动。"""

    def __init__(self, master, on_kick=None, on_close=None,
                 is_host=False, my_id=""):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=C.BG)
        self.title("房间成员")
        self._on_kick = on_kick
        self._on_close = on_close
        self._is_host = is_host
        self._my_id = my_id
        self._drag_offset = None
        self._drag_active = False
        self._members = []
        self._width = 440
        self._height = 500

        header = tk.Frame(self, bg=C.BG)
        header.pack(fill=tk.X, padx=18, pady=(14, 6))
        self.title_label = tk.Label(header, text="房间成员", bg=C.BG, fg=C.TEXT,
                                    font=ui_font(13, True), anchor="w")
        self.title_label.pack(side=tk.LEFT)
        self.count_label = tk.Label(header, text="", bg=C.BG, fg=C.TEXT_DIM,
                                    font=ui_font(9), anchor="w")
        self.count_label.pack(side=tk.LEFT, padx=(10, 0))
        close = RoundedButton(header, text="×", variant="ghost", width=30,
                              height=28, radius=8, bg=C.BG, font=ui_font(12),
                              command=self._close, tooltip="关闭")
        close.pack(side=tk.RIGHT)
        for widget in (header, self.title_label):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
        self.bind("<Button-1>", self._start_drag_top, add="+")
        self.bind("<B1-Motion>", self._on_drag, add="+")
        self.bind("<ButtonRelease-1>", self._end_drag, add="+")

        wrap = tk.Frame(self, bg=C.BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=18)
        self._scrollbar = tk.Canvas(wrap, width=6, bg=C.BG,
                                    highlightthickness=0, bd=0)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        self._scrollbar.bind("<Button-1>", self._on_scrollbar)
        self._scrollbar.bind("<B1-Motion>", self._on_scrollbar)
        self._canvas = tk.Canvas(wrap, bg=C.BG, highlightthickness=0, bd=0,
                                 width=380, height=360,
                                 yscrollcommand=lambda *_a: self._update_scrollbar())
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._inner = tk.Frame(self._canvas, bg=C.BG)
        self._window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw", width=380)
        self._inner.bind(
            "<Configure>",
            lambda _e: (self._canvas.configure(scrollregion=self._canvas.bbox("all")),
                        self._update_scrollbar()))
        self._canvas.bind(
            "<Configure>",
            lambda event: self._canvas.itemconfigure(self._window, width=event.width))
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Escape>", lambda _e: self._close())

        footer = tk.Frame(self, bg=C.BG)
        footer.pack(fill=tk.X, padx=18, pady=(8, 14))
        hint = "主持可移除房客" if is_host else "仅主持可移除成员"
        tk.Label(footer, text=hint, bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8),
                 anchor="w").pack(side=tk.LEFT)

    # ------------------------------------------------------------ 显示
    def present_async(self):
        self.update_idletasks()
        position_center(self, self.master, self._width, self._height)
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 数据
    def update_room(self, members):
        self._members = list(members or [])
        self.count_label.configure(text=f"共 {len(self._members)} 人")
        for child in self._inner.winfo_children():
            child.destroy()
        for index, member in enumerate(self._members):
            self._build_row(index, member)
        self._inner.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_scrollbar()

    def _build_row(self, index, member):
        row = tk.Frame(self._inner, bg=C.BG)
        row.pack(fill=tk.X, pady=(0, 4))
        line = tk.Frame(row, bg=C.SURFACE, padx=12, pady=8)
        line.pack(fill=tk.X)
        tk.Label(line, text=f"{index + 1:02d}", bg=C.SURFACE, fg=C.TEXT_DIM,
                 font=mono_font(9), width=3, anchor="w").pack(side=tk.LEFT)
        tk.Label(line, text=member.get("name") or "?", bg=C.SURFACE, fg=C.TEXT,
                 font=ui_font(10), anchor="w").pack(side=tk.LEFT, padx=(6, 8))
        if member.get("host"):
            tk.Label(line, text="主持", bg=C.ACCENT_SOFT, fg=C.ACCENT,
                     font=ui_font(8), padx=6, pady=1).pack(side=tk.LEFT)
        elif member.get("id") == self._my_id:
            tk.Label(line, text="我", bg=C.SURFACE_HI, fg=C.TEXT_MUTED,
                     font=ui_font(8), padx=6, pady=1).pack(side=tk.LEFT)
        if (self._is_host and not member.get("host")
                and member.get("id") != "host"):
            RoundedButton(
                line, text="踢出", variant="danger", width=52, height=26,
                radius=7, bg=C.SURFACE, font=ui_font(8),
                command=lambda cid=member.get("id"): self._kick(cid),
            ).pack(side=tk.RIGHT)

    def _kick(self, client_id):
        if client_id and self._on_kick is not None:
            self._on_kick(client_id)

    # ------------------------------------------------------------ 滚动
    def _on_wheel(self, event):
        try:
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
            self._check_bottom_after_scroll()
        except tk.TclError:
            pass

    def _on_scrollbar(self, event):
        height = self._scrollbar.winfo_height() or 1
        self._canvas.yview_moveto(max(0.0, min(1.0, event.y / height)))
        self._check_bottom_after_scroll()

    def _update_scrollbar(self):
        bar = self._scrollbar
        try:
            bar.delete("all")
            total = float(self._inner.winfo_height() or 0)
            view = float(self._canvas.winfo_height() or 0)
            if total <= view + 2:
                return
            first, last = self._canvas.yview()
            height = bar.winfo_height() or 1
            y0 = int(first * height)
            y1 = max(y0 + 24, int(last * height))
            draw_round_rect(bar, 1, y0, 5, min(y1, height), 2,
                            fill=C.STROKE_SOFT)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 拖动/关闭
    def _start_drag(self, event):
        self._drag_active = True
        self._drag_offset = (event.x_root - self.winfo_x(),
                             event.y_root - self.winfo_y())

    def _start_drag_top(self, event):
        self._drag_active = False
        self._drag_offset = None
        if event.y_root - self.winfo_rooty() <= 54:
            self._start_drag(event)

    def _on_drag(self, event):
        if not self._drag_active or self._drag_offset is None:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    def _end_drag(self, _event=None):
        self._drag_active = False
        self._drag_offset = None

    def _close(self):
        if self._on_close is not None:
            self._on_close()
        try:
            self.destroy()
        except tk.TclError:
            pass


class ChatPanel(tk.Frame):
    """主窗口右侧聊天面板: 上方消息列表, 下方输入框。"""

    WIDTH = 340

    def __init__(self, master, on_send):
        super().__init__(master, bg=C.BG, width=self.WIDTH,
                         highlightthickness=0, bd=0)
        self.pack_propagate(False)
        self._on_send = on_send
        self._messages = []
        self._max_rows = 200
        self._scroll_job = None

        header = tk.Frame(self, bg=C.BG)
        header.pack(side=tk.TOP, fill=tk.X, padx=14, pady=(12, 8))
        tk.Label(header, text="房间聊天", bg=C.BG, fg=C.TEXT,
                 font=ui_font(12, True), anchor="w").pack(side=tk.LEFT)
        self.count_label = tk.Label(header, text="", bg=C.BG, fg=C.TEXT_DIM,
                                    font=ui_font(8), anchor="w")
        self.count_label.pack(side=tk.LEFT, padx=(8, 0))
        tk.Frame(self, bg=C.STROKE, height=1).pack(side=tk.TOP, fill=tk.X)

        input_bar = tk.Frame(self, bg=C.BG, padx=12, pady=10)
        input_bar.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Frame(self, bg=C.STROKE, height=1).pack(side=tk.BOTTOM, fill=tk.X)
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(
            input_bar, textvariable=self.entry_var, bg=C.SURFACE, fg=C.TEXT,
            insertbackground=C.TEXT, relief=tk.FLAT, bd=0,
            highlightthickness=1, highlightbackground=C.STROKE,
            highlightcolor=C.ACCENT, font=ui_font(10),
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self._hint_text = ""
        self.hint_label = tk.Label(
            self.entry, text="", bg=C.SURFACE, fg=C.TEXT_DIM,
            font=ui_font(10), anchor="w", cursor="xterm")
        self.hint_label.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry_var.trace_add("write", self._on_entry_changed)
        RoundedButton(input_bar, text="发送", variant="primary", width=72,
                      height=32, radius=8, bg=C.BG,
                      command=self._submit).pack(side=tk.RIGHT, padx=(8, 0))

        wrap = tk.Frame(self, bg=C.BG)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                  padx=(10, 4), pady=(8, 4))
        self._scrollbar = tk.Canvas(wrap, width=6, bg=C.BG,
                                    highlightthickness=0, bd=0)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))
        self._scrollbar.bind("<Button-1>", self._on_scrollbar)
        self._scrollbar.bind("<B1-Motion>", self._on_scrollbar)
        self._canvas = tk.Canvas(
            wrap, bg=C.BG, highlightthickness=0, bd=0,
            yscrollcommand=lambda *_a: self._update_scrollbar())
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._inner = tk.Frame(self._canvas, bg=C.BG)
        self._window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw", width=300)
        self._inner.bind(
            "<Configure>",
            lambda _e: (self._canvas.configure(scrollregion=self._canvas.bbox("all")),
                        self._update_scrollbar()))
        self._canvas.bind(
            "<Configure>",
            lambda event: self._canvas.itemconfigure(self._window, width=event.width))
        self.bind("<MouseWheel>", self._on_wheel)
        self._list = tk.Frame(self._inner, bg=C.BG)
        self._list.pack(fill=tk.X)
        self.empty_label = tk.Label(self._inner, text="还没有消息，打个招呼吧",
                                    bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9))
        self.empty_label.pack(pady=30)
        self._unread = 0
        self.unread_label = tk.Label(
            self._canvas, text="", bg=C.ACCENT_SOFT, fg=C.ACCENT,
            font=ui_font(8), padx=10, pady=4, cursor="hand2")
        self.unread_label.bind("<Button-1>",
                               lambda _e: self.scroll_to_bottom())

    # ------------------------------------------------------------ 数据
    def add_message(self, message, mine=False):
        at_bottom = self._at_bottom()
        self._messages.append(message)
        self._render_message(message, mine=mine)
        while len(self._messages) > self._max_rows:
            self._messages.pop(0)
            children = self._list.winfo_children()
            if not children:
                break
            children[0].destroy()
        self.count_label.configure(text=f"{len(self._messages)} 条")
        self.empty_label.pack_forget()
        if mine or at_bottom:
            self._schedule_scroll()
        else:
            self._unread += 1
            self._update_unread()

    def set_messages(self, messages, my_id=""):
        self._clear_rows()
        self._messages = list(messages or [])[-self._max_rows:]
        for message in self._messages:
            self._render_message(message,
                                 mine=(message.get("sender_id") == my_id))
        self.count_label.configure(text=f"{len(self._messages)} 条"
                                   if self._messages else "")
        if self._messages:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(pady=30)
        self._unread = 0
        self._update_unread()
        self._schedule_scroll()

    def clear(self):
        self._clear_rows()
        self._messages = []
        self._unread = 0
        self.count_label.configure(text="")
        self.empty_label.pack(pady=30)
        self._update_unread()
        self._schedule_scroll()

    # ---------------------------------------------------------- 滚动/未读
    def _at_bottom(self):
        try:
            return self._canvas.yview()[1] >= 0.99
        except tk.TclError:
            return True

    def _update_unread(self):
        try:
            if self._unread > 0:
                self.unread_label.configure(
                    text=f"{self._unread} 条新消息 ↓")
                self.unread_label.place(in_=self._canvas, relx=0.5, rely=1.0,
                                        anchor="s", y=-6)
            else:
                self.unread_label.place_forget()
        except tk.TclError:
            pass

    def _schedule_scroll(self):
        if self._scroll_job is None:
            self._scroll_job = self.after_idle(self._do_scheduled_scroll)

    def _do_scheduled_scroll(self):
        self._scroll_job = None
        self.scroll_to_bottom()

    def scroll_to_bottom(self):
        try:
            self._inner.update_idletasks()
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._canvas.yview_moveto(1.0)
        except tk.TclError:
            return
        self._unread = 0
        self._update_unread()

    def _check_bottom_after_scroll(self):
        if self._unread and self._at_bottom():
            self._unread = 0
            self._update_unread()

    def _clear_rows(self):
        for child in self._list.winfo_children():
            child.destroy()

    def _render_message(self, message, mine=False):
        row = tk.Frame(self._list, bg=C.BG)
        row.pack(fill=tk.X, pady=(0, 8), padx=2)
        wrap = tk.Frame(row, bg=C.BG)
        wrap.pack(side=tk.RIGHT if mine else tk.LEFT)
        tk.Label(wrap, text=message.get("name") or "?",
                 bg=C.BG, fg=C.ACCENT if mine else C.TEXT_MUTED,
                 font=ui_font(8), anchor="e" if mine else "w").pack(
            fill=tk.X, padx=2)
        tk.Label(wrap, text=message.get("text") or "",
                 bg=C.ACCENT_SOFT if mine else C.SURFACE,
                 fg=C.TEXT, font=ui_font(10), wraplength=220,
                 justify=tk.LEFT, padx=10, pady=7, anchor="w").pack(fill=tk.X)
        try:
            clock = time.strftime("%H:%M", time.localtime(int(message.get("ts"))))
        except (TypeError, ValueError):
            clock = ""
        if clock:
            tk.Label(wrap, text=clock, bg=C.BG, fg=C.TEXT_DIM,
                     font=mono_font(7), anchor="e" if mine else "w").pack(
                fill=tk.X, padx=2)

    # ------------------------------------------------------------ 输入
    def show_hint(self, text):
        """在输入框内以灰色水印显示提示, 不填入输入内容。"""
        self._hint_text = str(text or "")
        if self._hint_text and not self.entry_var.get().strip():
            self.hint_label.configure(text=self._hint_text)
            self.hint_label.place(x=10, rely=0.5, anchor="w")

    def _hide_hint(self):
        try:
            self.hint_label.place_forget()
        except tk.TclError:
            pass

    def _on_entry_changed(self, *_args):
        if self.entry_var.get().strip():
            self._hide_hint()
        elif self._hint_text:
            self.hint_label.configure(text=self._hint_text)
            self.hint_label.place(x=10, rely=0.5, anchor="w")

    def _submit(self):
        text = self.entry_var.get().strip()
        if not text:
            return
        self._hint_text = ""
        self._hide_hint()
        self.entry_var.set("")
        self._on_send(text[:CHAT_MAX_CHARS])
        self.scroll_to_bottom()

    # ------------------------------------------------------------ 滚动
    def _on_wheel(self, event):
        try:
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    def _on_scrollbar(self, event):
        height = self._scrollbar.winfo_height() or 1
        self._canvas.yview_moveto(max(0.0, min(1.0, event.y / height)))

    def _update_scrollbar(self):
        bar = self._scrollbar
        try:
            bar.delete("all")
            total = float(self._inner.winfo_height() or 0)
            view = float(self._canvas.winfo_height() or 0)
            if total <= view + 2:
                return
            first, last = self._canvas.yview()
            height = bar.winfo_height() or 1
            y0 = int(first * height)
            y1 = max(y0 + 24, int(last * height))
            draw_round_rect(bar, 1, y0, 5, min(y1, height), 2,
                            fill=C.STROKE_SOFT)
        except tk.TclError:
            pass

    def _scroll_bottom(self):
        try:
            self._canvas.yview_moveto(1.0)
        except tk.TclError:
            pass


class DanmakuOverlay(tk.Canvas):
    """窗口内透明弹幕层: 作为视频区的子窗口, 随窗口移动/缩放, 可被其他窗口盖住。"""

    KEY = "#010203"

    def __init__(self, master):
        super().__init__(master, bg=self.KEY, highlightthickness=0, bd=0)
        self.canvas = self
        self._active = []
        self._queue = []
        self._seq = 0
        self._job = None
        self._last = None
        self._base_size = 20
        self._size = 20
        self._scale = 1.0
        self._speed = 1.0
        self._color = "#FFFFFF"
        self._area = 100
        self._width = 1
        self._height = 1
        self._lane_height = 32
        self._font = tkfont.Font(font=ui_font(self._size, True))
        self._visible = False
        self._layered_child()
        self.bind("<Configure>", self._on_configure)

    # ------------------------------------------------------------ 透明子窗口
    def _layered_child(self):
        """把 Tk Canvas 子窗口设为透明色键 + 鼠标穿透。"""
        try:
            import ctypes
            hwnd = self.winfo_id()
            user32 = ctypes.windll.user32
            gwl_exstyle = -20
            style = user32.GetWindowLongW(hwnd, gwl_exstyle)
            user32.SetWindowLongW(
                hwnd, gwl_exstyle,
                style | 0x00080000 | 0x00000020 | 0x08000000)
            key = (int(self.KEY[1:3], 16) | (int(self.KEY[3:5], 16) << 8)
                   | (int(self.KEY[5:7], 16) << 16))
            user32.SetLayeredWindowAttributes(hwnd, key, 0, 0x1)
        except Exception:
            pass

    def _raise_native(self):
        """把弹幕层抬到 VLC 视频子窗口之上。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetWindowPos(self.winfo_id(), 0, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0010 | 0x0040)
        except Exception:
            pass

    def _on_configure(self, event):
        width = max(1, int(event.width))
        height = max(1, int(event.height))
        if (width, height) == (self._width, self._height):
            return
        self._width, self._height = width, height
        self._recalc_font()
        self._lane_height = max(18, self._size + 12)
        self._clear()

    def _recalc_font(self):
        # 弹幕字号随视频高度缩放: 窗口和全屏视觉大小一致
        scale = max(0.8, min(2.8, self._height / 600.0)) if self._height > 1 else 1.0
        self._scale = scale
        actual = max(9, min(96, int(round(self._base_size * scale))))
        if actual != self._size:
            self._size = actual
            self._lane_height = max(18, actual + 12)
            self._font = tkfont.Font(font=ui_font(actual, True))

    def configure_style(self, size, speed, color="#FFFFFF", area=100):
        base_size = max(10, min(48, int(size)))
        speed = max(0.2, min(5.0, float(speed)))
        try:
            area = int(area)
        except (TypeError, ValueError):
            area = 100
        if area not in (100, 50, 40, 30, 20):
            area = 100
        color = str(color or "#FFFFFF")
        base_changed = base_size != self._base_size
        area_changed = area != self._area
        color_changed = color != self._color
        self._base_size = base_size
        self._speed = speed
        self._color = color
        self._area = area
        if base_changed or area_changed:
            self._recalc_font()
            self._clear()
        elif color_changed:
            for item in self._active:
                self.canvas.itemconfigure(item["item"], fill=self._color)

    def show(self):
        if self._visible:
            self._raise_native()
            return
        self._visible = True
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self._raise_native()
        self._last = None
        self._start()

    def hide(self):
        self._visible = False
        self._clear()
        self._stop()
        try:
            self.place_forget()
        except tk.TclError:
            pass

    def _recalc_font(self):
        # 弹幕字号随视频高度缩放: 窗口和全屏视觉大小一致
        scale = max(0.8, min(2.8, self._height / 600.0)) if self._height > 1 else 1.0
        self._scale = scale
        actual = max(9, min(96, int(round(self._base_size * scale))))
        if actual != self._size:
            self._size = actual
            self._lane_height = max(18, actual + 12)
            self._font = tkfont.Font(font=ui_font(actual, True))

    def configure_style(self, size, speed, color="#FFFFFF", area=100):
        base_size = max(10, min(48, int(size)))
        speed = max(0.2, min(5.0, float(speed)))
        try:
            area = int(area)
        except (TypeError, ValueError):
            area = 100
        if area not in (100, 50, 40, 30, 20):
            area = 100
        color = str(color or "#FFFFFF")
        base_changed = base_size != self._base_size
        area_changed = area != self._area
        color_changed = color != self._color
        self._base_size = base_size
        self._speed = speed
        self._color = color
        self._area = area
        if base_changed or area_changed:
            self._recalc_font()
            self._clear()
        elif color_changed:
            for item in self._active:
                self.canvas.itemconfigure(item["item"], fill=self._color)

    def show(self):
        if self._visible:
            self._raise_native()
            return
        self._visible = True
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self._raise_native()
        self._last = None
        self._start()

    def hide(self):
        self._visible = False
        self._clear()
        self._stop()
        try:
            self.place_forget()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 弹幕
    def spawn(self, text):
        if not self._visible:
            return
        text = str(text or "").strip()[:DANMAKU_MAX_CHARS]
        if not text:
            return
        self._queue.append(text)
        if len(self._queue) > 120:
            self._queue.pop(0)

    def _lane_count(self):
        visible = self._height * self._area / 100.0
        return max(1, int((visible - 8) // self._lane_height))

    def _lane_y(self, lane):
        return int(lane * self._lane_height + 6)

    def _lane_free(self, lane):
        for item in reversed(self._active):
            if item["lane"] == lane:
                return item["x"] + item["width"] < self._width - 36
        return True

    def _next_item(self):
        lanes = [lane for lane in range(self._lane_count()) if self._lane_free(lane)]
        if not lanes:
            return None
        lane = random.choice(lanes)
        text = self._queue.pop(0)
        return text, lane

    def _active_limit(self):
        return max(24, min(96, self._lane_count() * 2))

    def _spawn_one(self):
        if not self._queue:
            return False
        if len(self._active) >= self._active_limit():
            return False
        picked = self._next_item()
        if picked is None:
            return False
        text, lane = picked
        width = max(1, int(self._font.measure(text)))
        speed = max(40.0, self._width * self._speed / 8.0)
        y = self._lane_y(lane)
        tag = f"dm{self._seq}"
        self._seq += 1
        # 单层描边 + 整条弹幕用 tag 一起移动, 海量弹幕时显著降低 Canvas 调用
        self.canvas.create_text(
            self._width + 1, y + 1, text=text, anchor="nw",
            fill="#000000", font=self._font, tags=(tag,))
        item_id = self.canvas.create_text(
            self._width, y, text=text, anchor="nw",
            fill=self._color, font=self._font, tags=(tag,))
        self._active.append({
            "text": text, "lane": lane, "x": float(self._width),
            "y": y, "width": width, "speed": speed,
            "tag": tag, "item": item_id,
        })
        return True

    def _start(self):
        if self._job is None:
            self._last = None
            self._job = self.after(16, self._tick)

    def _stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _tick(self):
        self._job = None
        if not self._visible:
            return
        now = time.monotonic()
        dt = 0.016 if self._last is None else min(0.12, now - self._last)
        self._last = now
        load = len(self._active) + len(self._queue)
        base_lanes = self._lane_count()
        # 弹幕多时整体加速, 所有弹幕同倍速, 不会追尾重叠
        threshold = max(base_lanes, int(base_lanes * 1.2))
        boost = 1.0
        if load > threshold:
            boost = 1.0 + min(1.2, (load - threshold) / max(1, base_lanes * 2))
        alive = []
        for item in self._active:
            dx = item["speed"] * boost * dt
            item["x"] -= dx
            if item["x"] + item["width"] < -6:
                self.canvas.delete(item["tag"])
                continue
            self.canvas.move(item["tag"], -dx, 0)
            alive.append(item)
        self._active = alive
        if self._queue:
            self._spawn_one()
        self._job = self.after(16, self._tick)

    def _clear(self):
        self._queue = []
        for item in self._active:
            self.canvas.delete(item["tag"])
        self._active = []
        self.canvas.delete("all")


class BufferingOverlay(tk.Canvas):
    """视频区中央的透明「缓冲中」层: 圆点旋转 + Q弹 + 闪烁, 随视频大小与设置缩放。"""

    KEY = "#010203"
    DOTS = 8
    BASE_HEIGHT = 600.0

    def __init__(self, master):
        super().__init__(master, width=220, height=220, bg=self.KEY,
                         highlightthickness=0, bd=0)
        self._job = None
        self._visible = False
        self._scale = 1.0
        self._user_scale = 1.0
        self._phase = 0.0
        self._size = 220
        self._dots = []
        self._sparkles = []
        self._text = None
        self._fonts = {}
        self._layered_child()
        self._draw()
        master.bind("<Configure>", self._on_master_configure, add="+")

    # ------------------------------------------------------------ 缩放
    def set_user_scale(self, scale):
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            scale = 1.0
        self._user_scale = max(0.4, min(3.0, scale))
        self._apply_scale()

    def _on_master_configure(self, event=None):
        self._apply_scale()

    def _apply_scale(self):
        try:
            height = self.master.winfo_height()
        except Exception:
            height = 0
        video_scale = (max(0.6, min(2.5, height / self.BASE_HEIGHT))
                       if height > 1 else 1.0)
        scale = max(0.4, min(4.0, video_scale * self._user_scale))
        if abs(scale - self._scale) < 0.02:
            return
        self._scale = scale
        self._draw()

    def _font(self, px):
        px = max(6, int(round(px)))
        font = self._fonts.get(px)
        if font is None:
            font = tkfont.Font(font=ui_font(px, True))
            self._fonts[px] = font
        return font

    # ------------------------------------------------------------ 绘制
    def _draw(self):
        self.delete("all")
        size = int(220 * self._scale)
        self._size = size
        self.configure(width=size, height=size)
        if self._visible:
            try:
                self.place_configure(width=size, height=size)
            except tk.TclError:
                pass
        cx = size / 2.0
        ring_cy = size * 0.39
        radius = size * 0.215
        dot_px = max(6, size * 0.080)
        text_px = max(10, size * 0.065)
        self._text = self.create_text(
            cx, size * 0.82, text="缓冲中…", fill="#FFFFFF",
            font=self._font(text_px), anchor="center")
        self._dots = []
        for i in range(self.DOTS):
            angle = -math.pi / 2 + i * 2 * math.pi / self.DOTS
            x = cx + radius * math.cos(angle)
            y = ring_cy + radius * math.sin(angle)
            glow = self.create_text(x, y, text="●", fill="#1c2740",
                                    font=self._font(dot_px * 1.55),
                                    anchor="center")
            dot = self.create_text(x, y, text="●", fill="#FFFFFF",
                                   font=self._font(dot_px), anchor="center")
            self._dots.append((glow, dot, i * (2 * math.pi / self.DOTS)))
        self._sparkles = []
        for i in range(4):
            angle = -math.pi / 2 + (i + 0.5) * (2 * math.pi / 4)
            x = cx + radius * 1.38 * math.cos(angle)
            y = ring_cy + radius * 1.38 * math.sin(angle)
            item = self.create_text(x, y, text="✦", fill="#2a3860",
                                    font=self._font(dot_px * 0.55),
                                    anchor="center")
            self._sparkles.append((item, i * 1.9))
        self._phase = 0.0
        self._paint()

    def _paint(self):
        size = self._size
        base = max(6, size * 0.080)
        t = self._phase
        for glow, dot, offset in self._dots:
            wave = 0.5 + 0.5 * math.cos(t - offset)
            pulse = wave ** 2
            breathe = 1.0 + 0.04 * math.sin(t * 0.7)
            px = base * (0.68 + 0.62 * pulse) * breathe
            value = int(65 + 190 * pulse)
            color = "#%02X%02X%02X" % (value, value, value)
            gv = max(18, value // 6)
            try:
                self.itemconfigure(dot, fill=color, font=self._font(px))
                self.itemconfigure(glow, fill="#%02X%02X%02X" % (gv, gv + 4, gv + 16),
                                   font=self._font(px * 1.55))
            except tk.TclError:
                pass
        for item, offset in self._sparkles:
            tw = 0.5 + 0.5 * math.sin(t * 1.8 + offset)
            value = int(40 + 150 * tw)
            try:
                self.itemconfigure(item, fill="#%02X%02X%02X" % (value, value, min(255, value + 55)))
            except tk.TclError:
                pass

    def _layered_child(self):
        try:
            import ctypes
            hwnd = self.winfo_id()
            user32 = ctypes.windll.user32
            gwl_exstyle = -20
            style = user32.GetWindowLongW(hwnd, gwl_exstyle)
            user32.SetWindowLongW(
                hwnd, gwl_exstyle,
                style | 0x00080000 | 0x00000020 | 0x08000000)
            key = (int(self.KEY[1:3], 16) | (int(self.KEY[3:5], 16) << 8)
                   | (int(self.KEY[5:7], 16) << 16))
            user32.SetLayeredWindowAttributes(hwnd, key, 0, 0x1)
        except Exception:
            pass

    def _raise_native(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetWindowPos(self.winfo_id(), 0, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0010 | 0x0040)
        except Exception:
            pass

    def show(self):
        self._visible = True
        self._raise_native()
        if self._job is None:
            self._tick()

    def hide(self):
        self._visible = False
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self):
        if not self._visible:
            self._job = None
            return
        self._phase += 0.16
        self._paint()
        self._raise_tick = (getattr(self, "_raise_tick", 0) + 1) % 10
        if self._raise_tick == 0:
            # VLC 的视频子窗口会周期重绘, 定期把透明层抬回最上面
            self._raise_native()
        self._job = self.after(33, self._tick)


class StreamWatchView(tk.Canvas):
    """加入会话后的同步面板: 不渲染视频, 只展示同步状态。"""

    def __init__(self, master, bg=C.VIDEO):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self._info = None
        self.bind("<Configure>", lambda _e: self._render())

    def update(self, info):
        if info == self._info:
            return
        self._info = dict(info)
        self._render()

    def _render(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            return
        self.delete("all")
        info = self._info or {}
        phase = info.get("phase", "connecting")
        card_w = min(560.0, max(380.0, width - 80))
        card_h = 300.0
        cx = width / 2.0
        cy = height / 2.0
        x0, y0 = cx - card_w / 2, cy - card_h / 2
        x1, y1 = cx + card_w / 2, cy + card_h / 2
        icon_cy = y0 + 66
        glow = _glow_photo(self, 48, C.ACCENT, max_alpha=0.16)
        self.create_image(cx - glow.width() / 2.0, icon_cy - glow.height() / 2.0,
                          anchor="nw", image=glow)
        draw_round_rect(self, x0, y0, x1, y1, 20,
                        fill=C.CARD, outline=C.STROKE, width=1)
        circle = _circle_photo(self, 52, mix(C.CARD, C.ACCENT, 0.14),
                               mix(C.STROKE, C.ACCENT, 0.35), 1)
        self.create_image(cx - circle.width() / 2.0, icon_cy - circle.height() / 2.0,
                          anchor="nw", image=circle)
        icon_broadcast(self, cx, icon_cy, 22, C.ACCENT)

        if phase == "connecting":
            title = "正在连接主持…"
        elif phase == "reconnecting":
            title = "与主持失去连接 · 自动重连中…"
        else:
            title = info.get("name") or "等待主持开始播放"
        self.create_text(cx, y0 + 122, text=title, fill=C.TEXT,
                         font=ui_font(15, True), width=card_w - 60)
        if phase == "joined":
            room = info.get("room") or 0
            host = info.get("host") or "?"
            self.create_text(cx, y0 + 156, text=f"与主持同步播放 · 房间 {room} 人 · 主持 {host}",
                             fill=C.TEXT_MUTED, font=ui_font(10))
        self.create_text(cx, y0 + 186, text="画面仅主持端渲染 · 本机只同步状态与控制",
                         fill=C.TEXT_DIM, font=ui_font(9))
        if phase == "joined":
            self.create_text(cx, y0 + 214,
                             text="客户端直接渲染主持推流 · 控制指令交给主持执行",
                             fill=C.TEXT_DIM, font=ui_font(9))
        elif phase in ("reconnecting",):
            self.create_text(cx, y0 + 214, text="重连后会自动恢复同步",
                             fill=C.TEXT_DIM, font=ui_font(9))


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
class VideoPlayerApp:
    def __init__(self, root):
        setup_logging()
        install_excepthooks()
        self.root = root
        init_fonts(root)
        self.settings = load_settings()
        apply_appearance(self.settings)
        self._active_accent = self.settings["accent"]
        self._active_surface = self.settings["surface"]
        self._active_decode_mode = self.settings["decode_mode"]
        self._active_volume = int(self.settings["volume"])
        self._active_username = self.settings["username"]
        self.seek_step_ms = int(self.settings["seek_step"]) * 1000
        self._apply_log_level(self.settings.get("log_level", "INFO"))
        self.root.title(APP_TITLE)
        self.root.configure(bg=C.BG)
        width, height = center_on_screen(self.root, WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.root.minsize(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height))
        self.root.option_add("*Font", ui_font(10))
        self.root.report_callback_exception = self._on_tk_exception
        try:
            # 多尺寸: 标题栏用 16/32, 任务栏/Alt-Tab 用 64
            icons = [make_app_icon(self.root, 64),
                     make_app_icon(self.root, 32),
                     make_app_icon(self.root, 16)]
            self._app_icons = icons
            self.root.iconphoto(True, *icons)
        except Exception:
            log.debug("应用图标设置失败", exc_info=True)
        try:
            scaling = float(self.root.tk.call("tk", "scaling"))
        except (tk.TclError, ValueError):
            scaling = 0.0
        log.info(
            "窗口 %dx%d @ %dx%d | Tk 缩放 %.2f | 拖放 %s",
            width, height, self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            scaling, "可用" if TkinterDnD is not None else "不可用",
        )
        apply_dark_titlebar(self.root)
        self.root.bind("<Map>", lambda _e: apply_dark_titlebar(self.root), add="+")
        self.root.bind("<FocusIn>", lambda _e: apply_dark_titlebar(self.root), add="+")

        self.instance = None
        self.player = None
        self.media = None
        self.current_path = None
        self.duration_ms = 0
        self._seeking = False
        self._volume_before_mute = 80
        self._fullscreen = False
        self._saved_geometry = None
        # 全屏自动隐藏控制 dock
        self._fs_dock = None
        self._fs_dock_visible = False
        self._fs_animating = False
        self._fs_poll_job = None
        self._fs_anim_job = None
        self._fs_hide_job = None
        self._fs_dock_height = 84
        self._fs_dock_gap = 26
        self._fs_dock_radius = 26
        self._fs_dock_rect = None
        self._fs_chat = None
        self._fs_chat_panel = None
        self._fs_chat_visible = False
        self._fs_chat_typing = False
        self._fs_chat_rect = None
        self._fs_chat_hide_job = None
        self._fs_chat_anim_job = None
        self._fs_chat_animating = False
        self._fs_chat_width = 360
        self.fs_play_button = None
        self._tick_job = None
        self._hwnd_attached = False
        self._volume_applied = False
        self._last_dir = self._load_last_dir()
        self._started_at = time.monotonic()
        self._session_files = 0
        self._last_state = None
        self._last_video_size = (0, 0)
        self._duration_logged = False
        self._logged_volume = -1
        self._closing = False
        self._closed = False

        # 流式传输状态
        self._net_queue = queue.Queue()
        self._client_send_lock = threading.Lock()
        self._host_server = None
        self._host_cmd_port = None
        self._host_udp_port = None
        self._stream_sender = None
        self._stream_receiver = None
        self._host_monitor_stop = threading.Event()
        self._hosting = False
        self._host_password = ""
        self._client_password = None
        self._password_dialog = None
        self._member_dialog = None
        self._secondary_windows = []
        self._secondary_job = None
        self._chat_messages = []
        self._chat_last_sent = {}
        self._danmaku_sync_job = None
        self._danmaku_watch_job = None
        self._settings_dialog = None
        self._alert_open = False
        self._sout_targets = None
        self._stream_epoch = 0
        self._last_room_count = -1
        self._last_state_push = 0.0
        self._seek_hint = None
        self._seek_rebuild_job = None
        self._seek_pending = None
        self._resyncing = False
        # 推流中转(TS + 本地 HTTP 偏移)与独立推流器
        self._relay = None
        self._relay_key = None
        self._relay_building = False
        self._relay_percent = 0.0
        self._relay_http = None
        self._streamer_instance = None
        self._streamer_player = None
        self._streamer_targets = ()
        self._last_streamer_check = 0.0
        self._last_host_ports = (int(self.settings["cmd_port"]), int(self.settings["udp_port"]))
        self._last_join_addrs = (
            "127.0.0.1", int(self.settings["cmd_port"]),
            "127.0.0.1", int(self.settings["udp_port"]),
        )
        self._client_active = False
        self._client_sock = None
        self._client_connected = False
        self._client_id = None
        self._client_state = None
        self._client_state_ts = 0.0
        self._client_last_recv = 0.0
        self._client_watchdog_job = None
        self._client_video_start_ts = 0.0
        self._client_last_media_ms = -1
        self._client_stall_since = None
        self._client_last_restart = 0.0
        self._client_restart_count = 0
        self._client_cmd_addr = None
        self._client_udp_addr = None
        self._client_stop = threading.Event()
        self._client_next_retry = 0.0
        self._client_lost_at = 0.0
        self._client_reconnecting = False
        self._client_video_started = False
        self._client_epoch_seen = None
        self._client_media = None
        self._client_speed_backup = "1.0x"
        self._client_preparing = False
        self._client_room = {"count": 0, "host": "", "members": []}

        self._build_ui()
        self._bind_shortcuts()
        self._init_player()
        self._enable_drag_and_drop()
        threading.Thread(target=_cleanup_relay_dir, daemon=True,
                         name="relay-cleanup").start()
        self._ui_beat = time.monotonic()
        self._start_ui_watchdog()
        self._schedule_tick()
        log.info("界面初始化完成")

    def _start_ui_watchdog(self):
        """界面线程卡住时把各线程调用栈写进日志, 便于定位 (仅诊断, 不干预)."""
        def loop():
            while not self._closed:
                time.sleep(1.0)
                if self._closed:
                    return
                gap = time.monotonic() - self._ui_beat
                if gap <= 6.0:
                    continue
                log.warning("界面线程疑似卡死 %.1f 秒, 输出各线程调用栈", gap)
                frames = sys._current_frames()
                for thread in threading.enumerate():
                    frame = frames.get(thread.ident)
                    if frame is None:
                        continue
                    stack = "".join(traceback.format_stack(frame))
                    log.warning("线程 %s 栈:\n%s", thread.name, stack)
                self._ui_beat = time.monotonic()

        threading.Thread(target=loop, daemon=True, name="ui-watchdog").start()

    # ------------------------------------------------------------- 日志
    def _on_tk_exception(self, exc_type, exc_value, exc_tb):
        """Tk 回调里未捕获的异常: 记日志 + 状态栏提示, 不让窗口直接崩掉。"""
        log.error("界面回调异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            self._set_status(f"界面异常: {exc_value}", "error")
        except Exception:
            pass

    def open_log_folder(self):
        folder = os.path.dirname(LOG_PATH)
        try:
            if os.name == "nt":
                os.startfile(folder)  # noqa: S606 - 打开资源管理器
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as error:
            log.error("打开日志目录失败: %s", error)
            self._set_status(f"打开日志目录失败: {error}", "error")
            return
        log.info("打开日志目录: %s", folder)
        self._set_status(f"日志目录: {folder}", "ok")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # 控制栏先占底部, 视频区再撑满剩余空间
        self.deck = tk.Frame(self.root, bg=C.DECK)
        self.deck.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Frame(self.deck, bg=C.STROKE, height=1).pack(side=tk.TOP, fill=tk.X)
        body = tk.Frame(self.deck, bg=C.DECK, padx=22, pady=14)
        body.pack(fill=tk.X)

        # 进度行
        seek_row = tk.Frame(body, bg=C.DECK)
        seek_row.pack(fill=tk.X)
        self.time_label = tk.Label(seek_row, text="00:00", bg=C.DECK, fg=C.TEXT,
                                   font=mono_font(10), width=9, anchor="w")
        self.time_label.pack(side=tk.LEFT)
        self.duration_label = tk.Label(seek_row, text="00:00", bg=C.DECK, fg=C.TEXT_DIM,
                                       font=mono_font(10), width=9, anchor="e")
        self.duration_label.pack(side=tk.RIGHT)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = Slider(
            seek_row, variable=self.progress_var, from_=0.0, to=1000.0,
            command=self._on_progress_drag, on_seek_start=self._on_seek_start,
            on_seek_end=self._on_seek_end,
            bg=C.DECK, height=36,
        )
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 12))

        # 按钮行
        row = tk.Frame(body, bg=C.DECK)
        row.pack(fill=tk.X, pady=(10, 0))
        left = tk.Frame(row, bg=C.DECK)
        left.pack(side=tk.LEFT)
        right = tk.Frame(row, bg=C.DECK)
        right.pack(side=tk.RIGHT)

        # 左下角: 流式传输入口(主持 / 加入)
        self.stream_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=icon_broadcast, icon_size=17, command=self.open_stream_menu,
            tooltip="流式传输 (主持 / 加入)",
        )
        self.stream_button.pack(side=tk.LEFT)

        self.members_button = RoundedButton(
            left, text="房间成员", variant="ghost", width=96, height=36, radius=9,
            bg=C.DECK, command=self.open_members, tooltip="查看房间成员",
        )

        self.open_button = RoundedButton(
            left, text="打开文件", variant="ghost", width=112, height=36, radius=9,
            bg=C.DECK, icon=icon_folder, icon_size=15, command=self.open_file_dialog,
            tooltip="打开视频文件 (Ctrl+O)",
        )
        self.open_button.pack(side=tk.LEFT, padx=(10, 0))

        # 多音轨 / 字幕切换: 仅当媒体存在多音轨或字幕时显示
        self.track_button = RoundedButton(
            left, text="音轨 / 字幕", variant="ghost", width=112, height=36, radius=9,
            bg=C.DECK, command=self.open_track_menu,
            tooltip="切换音轨 / 字幕",
        )

        self.back_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_skip(cv, cx, cy, s, col, -1),
            icon_size=16, command=lambda: self.seek_relative(-self.seek_step_ms),
            tooltip="后退 (←)",
        )
        self.back_button.pack(side=tk.LEFT, padx=(20, 0))

        self.play_button = RoundedButton(
            left, variant="primary", width=48, height=48, radius=24, bg=C.DECK,
            icon=icon_play, icon_size=20, command=self.toggle_play,
            tooltip="播放 / 暂停 (空格)",
        )
        self.play_button.pack(side=tk.LEFT, padx=(10, 8))

        self.forward_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_skip(cv, cx, cy, s, col, 1),
            icon_size=16, command=lambda: self.seek_relative(self.seek_step_ms),
            tooltip="快进 (→)",
        )
        self.forward_button.pack(side=tk.LEFT)

        self.stop_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=icon_stop, icon_size=14, command=self.stop, tooltip="停止",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))

        # 右侧: 设置 / 全屏 / 速度 / 音量
        self.settings_button = RoundedButton(
            right, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=icon_gear, icon_size=17, command=self.open_settings,
            tooltip="设置",
        )
        self.settings_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.fullscreen_button = RoundedButton(
            right, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=icon_fullscreen, icon_size=16, command=self.toggle_fullscreen,
            tooltip="全屏 (F)",
        )
        self.fullscreen_button.pack(side=tk.RIGHT)

        self.speed_var = tk.StringVar(value="1.0x")
        self.speed_box = Dropdown(
            right, values=SPEED_OPTIONS, variable=self.speed_var,
            command=self._on_speed_change, width=82, height=36, bg=C.DECK, direction="up",
        )
        self.speed_box.pack(side=tk.RIGHT, padx=(0, 12))

        self.volume_label = tk.Label(right, text="80%", bg=C.DECK, fg=C.TEXT_MUTED,
                                     font=mono_font(9), width=4, anchor="e")
        self.volume_label.pack(side=tk.RIGHT, padx=(8, 2))
        self.volume_var = tk.IntVar(value=int(self.settings["volume"]))
        self.volume_scale = Slider(
            right, variable=self.volume_var, from_=0, to=100, compact=True,
            command=self._on_volume_change, bg=C.DECK, width=96, height=36,
            integral=True,
        )
        self.volume_scale.pack(side=tk.RIGHT)
        self.mute_var = tk.BooleanVar(value=False)
        self.mute_button = RoundedButton(
            right, variant="icon", width=34, height=34, radius=9, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_speaker(cv, cx, cy, s, col, False),
            icon_size=17, command=self._on_mute_click, tooltip="静音",
        )
        self.mute_button.pack(side=tk.RIGHT, padx=(0, 6))

        # 状态行
        status_row = tk.Frame(body, bg=C.DECK)
        status_row.pack(fill=tk.X, pady=(12, 0))
        self.status_dot = tk.Canvas(status_row, width=10, height=10, bg=C.DECK,
                                    highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, pady=(2, 0))
        self.status_label = tk.Label(status_row, text="就绪", bg=C.DECK, fg=C.TEXT_MUTED,
                                     font=ui_font(9), anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=(7, 0))
        Tooltip(self.status_label, f"日志: {LOG_PATH}")
        tk.Label(status_row, text="空格 播放/暂停    ← → 快退/快进    F 全屏    Ctrl+L 日志",
                 bg=C.DECK, fg=C.TEXT_DIM, font=ui_font(8)).pack(side=tk.RIGHT)

        # 视频区 + 右侧聊天 + 空状态 + 同步面板
        self.content = tk.Frame(self.root, bg=C.VIDEO, highlightthickness=0)
        self.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.video_frame = tk.Frame(self.content, bg=C.VIDEO, highlightthickness=0)
        self.video_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.chat_panel = ChatPanel(self.content, on_send=self._send_chat)
        self.danmaku = DanmakuOverlay(self.video_frame)
        self.video_frame.bind("<Configure>",
                              lambda _e: self._schedule_danmaku_sync())
        self.root.bind("<Configure>",
                       lambda _e: self._schedule_danmaku_sync(), add="+")
        self.drop_hint = EmptyStateView(self.video_frame, on_open=self.open_file_dialog,
                                        bg=C.VIDEO)
        self.drop_hint.pack(fill=tk.BOTH, expand=True)
        self.watch_view = StreamWatchView(self.video_frame, bg=C.VIDEO)
        self.buffering = BufferingOverlay(self.video_frame)
        self._buffering_visible = False
        self._client_prev_playing = False
        self._displayed_pictures = -1
        self._last_frame_ts = 0.0
        self._buffer_job = None
        self._build_fs_dock()
        self._build_fs_chat()
        self.root.after(500, self._watch_danmaku)
        self.root.after(1000, self._client_watchdog)
        self.root.after(300, self._buffering_tick)

    # ------------------------------------------------------- 全屏控制 dock
    def _build_fs_dock(self):
        """全屏时浮在屏幕下方的控制岛(独立置顶窗口 + 窗口圆角, 才能盖在 VLC 之上)。"""
        dock = tk.Toplevel(self.root)
        dock.withdraw()
        dock.overrideredirect(True)
        dock.configure(bg=C.DECK)
        try:
            dock.attributes("-topmost", True)
        except tk.TclError:
            pass
        body = tk.Frame(dock, bg=C.DECK, padx=20, pady=13)
        body.pack(fill=tk.BOTH, expand=True)

        self.fs_play_button = RoundedButton(
            body, variant="primary", width=54, height=54, radius=27, bg=C.DECK,
            icon=icon_play, icon_size=24, command=self.toggle_play,
            tooltip="播放 / 暂停",
        )
        self.fs_play_button.pack(side=tk.LEFT)
        self.fs_back_button = RoundedButton(
            body, variant="icon", width=36, height=36, radius=10, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_skip(cv, cx, cy, s, col, -1),
            icon_size=16, command=lambda: self.seek_relative(-self.seek_step_ms),
            tooltip="后退",
        )
        self.fs_back_button.pack(side=tk.LEFT, padx=(10, 0))
        self.fs_forward_button = RoundedButton(
            body, variant="icon", width=36, height=36, radius=10, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_skip(cv, cx, cy, s, col, 1),
            icon_size=16, command=lambda: self.seek_relative(self.seek_step_ms),
            tooltip="快进",
        )
        self.fs_forward_button.pack(side=tk.LEFT)

        right = tk.Frame(body, bg=C.DECK)
        right.pack(side=tk.RIGHT)
        self.fs_exit_button = RoundedButton(
            right, variant="icon", width=36, height=36, radius=10, bg=C.DECK,
            icon=icon_restore, icon_size=16, command=self.exit_fullscreen,
            tooltip="退出全屏 (Esc)",
        )
        self.fs_exit_button.pack(side=tk.RIGHT)
        self.fs_speed_box = Dropdown(
            right, values=SPEED_OPTIONS, variable=self.speed_var,
            command=self._on_speed_change, width=78, height=34, bg=C.DECK,
            direction="up",
        )
        self.fs_speed_box.pack(side=tk.RIGHT, padx=(10, 10))
        self.fs_volume = Slider(
            right, variable=self.volume_var, from_=0, to=100, compact=True,
            command=self._on_volume_change, bg=C.DECK, width=92, height=34,
            integral=True,
        )
        self.fs_volume.pack(side=tk.RIGHT)
        self.fs_mute_button = RoundedButton(
            right, variant="icon", width=34, height=34, radius=9, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_speaker(cv, cx, cy, s, col, False),
            icon_size=17, command=self._on_mute_click, tooltip="静音",
        )
        self.fs_mute_button.pack(side=tk.RIGHT, padx=(0, 6))

        middle = tk.Frame(body, bg=C.DECK)
        middle.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(18, 18))
        labels = tk.Frame(middle, bg=C.DECK)
        labels.pack(fill=tk.X, pady=(3, 0))
        self.fs_time_label = tk.Label(labels, text="00:00", bg=C.DECK, fg=C.TEXT,
                                      font=mono_font(9), width=9, anchor="w")
        self.fs_time_label.pack(side=tk.LEFT)
        self.fs_duration_label = tk.Label(labels, text="00:00", bg=C.DECK,
                                          fg=C.TEXT_DIM, font=mono_font(9),
                                          width=9, anchor="e")
        self.fs_duration_label.pack(side=tk.RIGHT)
        self.fs_progress = Slider(
            middle, variable=self.progress_var, from_=0.0, to=1000.0,
            command=self._on_progress_drag, on_seek_start=self._on_seek_start,
            on_seek_end=self._on_seek_end, bg=C.DECK, height=30,
        )
        self.fs_progress.pack(fill=tk.X, pady=(2, 0))

        self._fs_dock = dock
        log.debug("全屏控制 dock 已创建")

    # ---------------------------------------------------- 全屏右侧聊天面板
    def _build_fs_chat(self):
        """全屏时浮在屏幕右侧的聊天面板: 鼠标靠近右缘滑出, 可看消息/发消息。"""
        chat = tk.Toplevel(self.root)
        chat.withdraw()
        chat.overrideredirect(True)
        chat.configure(bg=C.BG)
        try:
            chat.attributes("-topmost", True)
        except tk.TclError:
            pass
        panel = ChatPanel(chat, on_send=self._send_chat)
        panel.pack(fill=tk.BOTH, expand=True)
        panel._max_rows = self._chat_limit()
        panel.entry.bind("<FocusIn>", self._on_fs_chat_focus_in, add="+")
        panel.entry.bind("<FocusOut>", self._on_fs_chat_focus_out, add="+")
        chat.bind("<Escape>", lambda _e: self._hide_fs_chat())
        chat.bind("<Enter>", lambda _e: self._cancel_fs_chat_hide())
        chat.bind("<Leave>", lambda _e: self._schedule_fs_chat_hide())
        self._fs_chat = chat
        self._fs_chat_panel = panel
        log.debug("全屏聊天面板已创建")

    def _fs_chat_size(self):
        left, top, sw, sh = self._fs_screen_rect()
        width = 340
        height = max(420, sh - 48)
        x = left + sw - width - 18
        y = top + (sh - height) // 2
        return width, height, x, y

    def _fs_chat_show_zone(self, px):
        try:
            left, _top, sw, _sh = self._fs_screen_rect()
        except tk.TclError:
            return False
        right = left + sw
        return left <= px <= right and px >= right - 6

    def _fs_chat_zone(self, px, py):
        rect = self._fs_chat_rect
        if rect is None:
            return False
        x, y, w, h = rect
        return (x - 14 <= px <= x + w + 14) and (y - 14 <= py <= y + h + 14)

    def _on_fs_chat_focus_in(self, _event=None):
        self._fs_chat_typing = True
        self._cancel_fs_chat_hide()

    def _on_fs_chat_focus_out(self, _event=None):
        self._fs_chat_typing = False

    def _show_fs_chat(self):
        chat = self._fs_chat
        if chat is None or self._fs_chat_visible:
            return
        if not (self._hosting or self._client_active):
            return
        self._fs_chat_visible = True
        self._cancel_fs_chat_hide()
        width, height, x, y = self._fs_chat_size()
        self._fs_chat_rect = (x, y, width, height)
        try:
            left, _top, sw, _sh = self._fs_screen_rect()
            right = left + sw
            chat.geometry(f"{width}x{height}+{right + 10}+{y}")
            chat.deiconify()
            chat.lift()
            chat.update_idletasks()
            round_window_corners(chat, 20)
        except tk.TclError:
            return
        self._animate_fs_chat(right + 10, x)

    def _hide_fs_chat(self):
        self._fs_chat_hide_job = None
        if not self._fs_chat_visible or self._fs_chat is None:
            return
        self._fs_chat_visible = False
        self._fs_chat_rect = None
        try:
            left, _top, sw, _sh = self._fs_screen_rect()
            right = left + sw
            current = self._fs_chat.winfo_x()
        except tk.TclError:
            return
        self._animate_fs_chat(current, right + 10, hide_after=True)

    def _animate_fs_chat(self, start_x, end_x, hide_after=False, step=0, steps=8):
        chat = self._fs_chat
        if chat is None:
            return
        self._fs_chat_animating = True
        step += 1
        ease = 1.0 - (1.0 - step / steps) ** 3
        x = int(start_x + (end_x - start_x) * ease)
        try:
            width, height, _x, y = self._fs_chat_size()
            chat.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self._fs_chat_animating = False
            return
        if step < steps:
            self._fs_chat_anim_job = self.root.after(
                13, lambda: self._animate_fs_chat(start_x, end_x, hide_after, step, steps))
            return
        self._fs_chat_anim_job = None
        self._fs_chat_animating = False
        if hide_after:
            try:
                chat.withdraw()
            except tk.TclError:
                pass

    def _schedule_fs_chat_hide(self):
        if self._fs_chat_hide_job is not None:
            return
        self._fs_chat_hide_job = self.root.after(1600, self._hide_fs_chat)

    def _cancel_fs_chat_hide(self):
        if self._fs_chat_hide_job is not None:
            try:
                self.root.after_cancel(self._fs_chat_hide_job)
            except tk.TclError:
                pass
            self._fs_chat_hide_job = None

    def _fs_dock_size(self):
        left, top, sw, sh = self._fs_screen_rect()
        width = min(900, max(560, int(sw * 0.62)))
        height = self._fs_dock_height
        return (width, height, left + (sw - width) // 2,
                top + sh - self._fs_dock_gap - height)

    def _fs_dock_zone(self, px, py):
        rect = self._fs_dock_rect
        if rect is None:
            return False
        x, y, w, h = rect
        return (x - 10 <= px <= x + w + 10) and (y - 10 <= py <= y + h + self._fs_dock_gap + 10)

    def _fs_screen_rect(self):
        """当前全屏窗口所在显示器的 (left, top, width, height), 多显示器下不越界。"""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(int(self.root.winfo_id()), 2)
            if hwnd:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD),
                                ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT),
                                ("dwFlags", wintypes.DWORD)]
                monitor = user32.MonitorFromWindow(hwnd, 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    rect = info.rcMonitor
                    return (int(rect.left), int(rect.top),
                            max(1, int(rect.right - rect.left)),
                            max(1, int(rect.bottom - rect.top)))
        except Exception:
            pass
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _fs_screen(self):
        _left, _top, width, height = self._fs_screen_rect()
        return width, height

    def _sync_fs_dock_labels(self):
        if self._fs_dock is None:
            return
        try:
            self.fs_time_label.configure(text=self.time_label.cget("text"))
            self.fs_duration_label.configure(text=self.duration_label.cget("text"))
        except tk.TclError:
            pass

    def _start_fs_watch(self):
        self._stop_fs_watch()
        self._fs_poll_job = self.root.after(80, self._fs_watch)

    def _stop_fs_watch(self):
        if self._fs_poll_job is not None:
            try:
                self.root.after_cancel(self._fs_poll_job)
            except tk.TclError:
                pass
            self._fs_poll_job = None
        self._cancel_fs_hide()
        self._cancel_fs_anim()
        self._cancel_fs_chat_hide()
        if self._fs_chat_anim_job is not None:
            try:
                self.root.after_cancel(self._fs_chat_anim_job)
            except tk.TclError:
                pass
            self._fs_chat_anim_job = None
        self._fs_chat_animating = False

    def _fs_show_zone(self, px, py):
        """底部显示区: 只在全屏窗口所在显示器内生效。"""
        try:
            left, top, sw, sh = self._fs_screen_rect()
            _w, height, _x, y = self._fs_dock_size()
        except tk.TclError:
            return False
        right = left + sw
        if not (left <= px <= right):
            return False
        return py >= max(top, min(top + sh, y - 40))

    def _on_fs_motion(self, event):
        if not self._fullscreen:
            return
        try:
            px, py = event.x_root, event.y_root
        except Exception:
            return
        if self._fs_show_zone(px, py) or self._fs_dock_zone(px, py):
            if not self._fs_dock_visible:
                self._show_fs_dock()
            else:
                self._cancel_fs_hide()
        if self._fs_chat is not None and (self._hosting or self._client_active):
            if self._fs_chat_show_zone(px) or self._fs_chat_zone(px, py):
                if not self._fs_chat_visible:
                    self._show_fs_chat()
                else:
                    self._cancel_fs_chat_hide()

    def _fs_watch(self):
        self._fs_poll_job = None
        if not self._fullscreen:
            return
        try:
            px, py = self.root.winfo_pointerxy()
        except tk.TclError:
            return
        show_zone = self._fs_show_zone(px, py)
        dock_zone = self._fs_dock_zone(px, py)
        if not self._fs_dock_visible:
            if show_zone or dock_zone:
                self._show_fs_dock()
        else:
            if show_zone or dock_zone:
                self._cancel_fs_hide()
            else:
                self._schedule_fs_hide()
        if self._fs_chat is not None and (self._hosting or self._client_active):
            chat_show = self._fs_chat_show_zone(px)
            chat_zone = self._fs_chat_zone(px, py)
            if not self._fs_chat_visible:
                if chat_show or chat_zone:
                    self._show_fs_chat()
            else:
                if chat_zone or chat_show or self._fs_chat_typing:
                    self._cancel_fs_chat_hide()
                else:
                    self._schedule_fs_chat_hide()
        self._fs_poll_job = self.root.after(80, self._fs_watch)

    def _schedule_fs_hide(self):
        if self._fs_hide_job is not None:
            return
        self._fs_hide_job = self.root.after(1600, self._hide_fs_dock)

    def _cancel_fs_hide(self):
        if self._fs_hide_job is not None:
            try:
                self.root.after_cancel(self._fs_hide_job)
            except tk.TclError:
                pass
            self._fs_hide_job = None

    def _cancel_fs_anim(self):
        if self._fs_anim_job is not None:
            try:
                self.root.after_cancel(self._fs_anim_job)
            except tk.TclError:
                pass
            self._fs_anim_job = None
        self._fs_animating = False

    def _show_fs_dock(self):
        dock = self._fs_dock
        if dock is None or self._fs_dock_visible:
            return
        self._fs_dock_visible = True
        self._cancel_fs_hide()
        width, height, x, y = self._fs_dock_size()
        self._fs_dock_rect = (x, y, width, height)
        try:
            _left, top, _sw, sh = self._fs_screen_rect()
            hidden_y = top + sh + 10
            dock.geometry(f"{width}x{height}+{x}+{hidden_y}")
            dock.deiconify()
            dock.lift()
            dock.update_idletasks()
            round_window_corners(dock, self._fs_dock_radius)
        except tk.TclError:
            return
        self._animate_fs_dock(hidden_y, y)

    def _hide_fs_dock(self):
        self._fs_hide_job = None
        if not self._fs_dock_visible or self._fs_dock is None:
            return
        self._fs_dock_visible = False
        self._fs_dock_rect = None
        try:
            _left, top, _sw, sh = self._fs_screen_rect()
            hidden_y = top + sh + 10
            current = self._fs_dock.winfo_y()
        except tk.TclError:
            return
        self._animate_fs_dock(current, hidden_y, hide_after=True)

    def _animate_fs_dock(self, start_y, end_y, hide_after=False, step=0, steps=8):
        dock = self._fs_dock
        if dock is None:
            return
        self._fs_animating = True
        step += 1
        ease = 1.0 - (1.0 - step / steps) ** 3
        y = int(start_y + (end_y - start_y) * ease)
        try:
            width, height, x, _y = self._fs_dock_size()
            dock.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self._fs_animating = False
            return
        if step < steps:
            self._fs_anim_job = self.root.after(
                13, lambda: self._animate_fs_dock(start_y, end_y, hide_after, step, steps)
            )
            return
        self._fs_anim_job = None
        self._fs_animating = False
        if hide_after:
            try:
                dock.withdraw()
            except tk.TclError:
                pass


    def _bind_shortcuts(self):
        self.root.bind("<space>", lambda e: self._shortcut(self.toggle_play))
        self.root.bind("<Left>", lambda e: self._shortcut(
            lambda: self.seek_relative(-self.seek_step_ms)))
        self.root.bind("<Right>", lambda e: self._shortcut(
            lambda: self.seek_relative(self.seek_step_ms)))
        self.root.bind("<Up>", lambda e: self._shortcut(lambda: self.change_volume(5)))
        self.root.bind("<Down>", lambda e: self._shortcut(lambda: self.change_volume(-5)))
        self.root.bind("<f>", lambda e: self._shortcut(self.toggle_fullscreen))
        self.root.bind("<F>", lambda e: self._shortcut(self.toggle_fullscreen))
        self.root.bind("<Escape>", lambda e: self._shortcut(self.exit_fullscreen))
        self.root.bind("<Control-o>", lambda e: self._shortcut(self.open_file_dialog))
        self.root.bind("<Control-l>", lambda e: self._shortcut(self.open_log_folder))
        self.root.bind("<Control-L>", lambda e: self._shortcut(self.open_log_folder))
        self.root.bind("<FocusOut>", self._on_root_focus_out, add="+")
        self.root.bind("<FocusIn>", lambda _e: self._lift_secondary(), add="+")
        self.root.bind("<Button-1>",
                       lambda _e: self.root.after_idle(self._lift_secondary),
                       add="+")
        self.root.bind("<Motion>", self._on_fs_motion, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _on_root_focus_out(self, _event=None):
        """切到别的程序时收起全屏 dock/聊天(指针还在面板上时不算)。"""
        if not self._fullscreen or (not self._fs_dock_visible and not self._fs_chat_visible):
            return
        try:
            px, py = self.root.winfo_pointerxy()
        except tk.TclError:
            return
        if not self._fs_dock_zone(px, py):
            self._hide_fs_dock()
        if not self._fs_chat_zone(px, py) and not self._fs_chat_show_zone(px):
            self._hide_fs_chat()

    def _shortcut(self, action):
        # 避免在输入框里按空格等键时误触发
        widget = self.root.focus_get()
        if isinstance(widget, tk.Entry):
            return None
        action()
        return "break"

    # ------------------------------------------------------------ 流式传输
    def open_stream_menu(self):
        """左下角流式按钮: 未启用时选主持/加入, 启用时停止/退出。"""
        if self._hosting:
            mode = "host"
            info = f"指令端口 {self._host_cmd_port} · 流端口 {self._host_udp_port} · "
            info += f"房间 {self._room_count()} 人"
        elif self._client_active:
            mode = "client"
            info = f"主持 {self._client_room.get('host') or '?'} · "
            info += f"房间 {self._client_room.get('count', 0)} 人"
        else:
            mode = "off"
            info = ""
        dialog = StreamMenuDialog(self.root, mode, host_info=info, join_info=info)
        self._track_dialog(dialog)
        dialog.present()
        choice = dialog.choice
        log.debug("流式菜单选择: %s", choice)
        if choice == "host":
            host_dialog = HostStreamDialog(self.root, *self._last_host_ports)
            self._track_dialog(host_dialog)
            host_dialog.present()
            if host_dialog.result is not None:
                result = host_dialog.result
                cmd_port, udp_port = result[0], result[1]
                password = result[2] if len(result) > 2 else ""
                self._last_host_ports = (cmd_port, udp_port)
                self.start_hosting(cmd_port, udp_port, password)
        elif choice == "join":
            join_dialog = JoinStreamDialog(
                self.root,
                cmd_addr=f"{self._last_join_addrs[0]}:{self._last_join_addrs[1]}",
                stream_addr=f"{self._last_join_addrs[2]}:{self._last_join_addrs[3]}",
            )
            self._track_dialog(join_dialog)
            join_dialog.present()
            if join_dialog.result is not None:
                cmd_host, cmd_port, udp_host, udp_port = join_dialog.result
                self._last_join_addrs = (cmd_host, cmd_port, udp_host, udp_port)
                self.join_stream(cmd_host, cmd_port, udp_host, udp_port)
        elif choice == "stop_host":
            self.stop_hosting()
        elif choice == "leave":
            self.leave_stream()

    def _room_count(self):
        if self._host_server is None:
            return 0
        return len(self._host_server.snapshot()) + 1

    # ------------------------------------------------------------- 网络事件
    def queue_net_event(self, event):
        """网络线程 -> UI 线程的唯一通道。"""
        try:
            self._net_queue.put_nowait(event)
        except queue.Full:
            log.warning("网络事件队列已满, 丢弃事件 %s", event.get("kind"))

    def _drain_network(self):
        try:
            while True:
                self._handle_net_event(self._net_queue.get_nowait())
        except queue.Empty:
            pass

    def _handle_net_event(self, event):
        kind = event.get("kind")
        try:
            if kind == "host_message":
                self._host_handle_message(event["client_id"], event["message"])
            elif kind == "host_left":
                log.info("客户端断开: %s", event["client_id"])
                self._chat_last_sent.pop(event["client_id"], None)
                if self._hosting:
                    self._broadcast_room()
                    self._rebuild_stream()
                    if not self._stream_targets() and self._sout_targets:
                        self._schedule_orphan_strip()
            elif kind == "relay_progress":
                percent = float(event.get("percent") or 0)
                if percent < 0:
                    self._set_status("正在建立推流索引…", "playing")
                else:
                    self._relay_percent = percent
                    self._set_status(
                        f"正在准备推流源 {percent:.0f}%…", "playing")
            elif kind == "relay_ready":
                relay = event.get("relay")
                if relay is not None and relay is not self._relay:
                    return  # 换片后旧的准备任务完成, 忽略
                self._relay_building = False
                relay = self._relay
                if event.get("ok") and relay is not None and relay.ready:
                    log.info("推流源就绪: %s (%.1fMB, %d 个索引点)",
                             relay.ts_path, relay.size / 1048576.0,
                             len(relay.index))
                else:
                    self._set_status(
                        f"无法准备推流源: {event.get('error') or '未知错误'}",
                        "error",
                    )
            elif kind == "client_message":
                self._client_handle_message(event["message"])
            elif kind == "client_lost":
                self._client_on_lost()
            elif kind == "client_reconnected":
                self._client_reconnecting = False
                if event.get("ok"):
                    self._client_lost_at = 0.0
                    self._set_status("已重新连接 · 等待同步", "playing")
            elif kind == "join_result":
                if not self._client_active:
                    return
                if event.get("ok"):
                    log.info("已连接主持指令通道: %s", self._client_cmd_addr)
                    self._set_status("已连接主持 · 等待同步", "playing")
                else:
                    log.error("无法连接主持指令端口: %s", self._client_cmd_addr)
                    self._alert(
                        "连接失败",
                        "无法连接主持的指令端口。\n"
                        "请检查主持是否已开始、地址端口是否填写正确。",
                    )
                    self.leave_stream()
        except Exception:
            log.exception("处理网络事件失败: %s", kind)

    # ------------------------------------------------------------- 主持端
    def start_hosting(self, cmd_port, udp_port, password=""):
        if self._hosting:
            return True
        if self._client_active:
            self._alert("无法主持", "当前已加入其他会话, 请先退出。")
            return False
        try:
            server = StreamHostServer(("0.0.0.0", cmd_port), self)
        except OSError as error:
            log.error("指令端口 %d 打开失败: %s", cmd_port, error)
            self._alert("无法开始主持", f"指令端口 {cmd_port} 打开失败:\n{error}")
            return False
        self._host_password = str(password or "")
        self._host_server = server
        self._host_cmd_port = cmd_port
        self._host_udp_port = udp_port
        self._hosting = True
        self._last_room_count = -1
        self._host_monitor_stop.clear()
        threading.Thread(target=server.serve_forever, daemon=True,
                         name=f"host-serve-{cmd_port}").start()
        threading.Thread(target=self._host_monitor_loop, daemon=True,
                         name=f"host-monitor-{cmd_port}").start()
        try:
            self._stream_sender = UdpMediaSender(udp_port)
            self._stream_sender.start()
            log.info("媒体流监听 0.0.0.0:%d (客户端主动连入), VLC 本地中转 %d",
                     self._stream_sender.port, self._stream_sender.relay_port)
        except OSError as error:
            self._stream_sender = None
            log.warning("可靠流中转启动失败: %s", error)
        self.stream_button.set_active(True)
        self._refresh_stream_button_tooltip()
        self._sync_members_button()
        self._chat_messages = []
        self._chat_last_sent = {}
        self.chat_panel._max_rows = self._chat_limit()
        self.chat_panel.clear()
        try:
            if self._fs_chat_panel is not None:
                self._fs_chat_panel.clear()
        except tk.TclError:
            pass
        self._sync_chat_panel()
        self._sync_danmaku()
        log.info("开始主持流式: TCP 指令端口 %d, 媒体端口 %d, 房间密码 %s",
                 cmd_port, udp_port, "开启" if self._host_password else "关闭")
        password_hint = " · 密码" if self._host_password else ""
        self._set_status(
            f"主持流式 · 指令 {cmd_port} · 流 {udp_port} · 房间 1 人{password_hint}",
            "playing",
        )
        return True

    def stop_hosting(self):
        if not self._hosting:
            return
        self._hosting = False
        self._host_monitor_stop.set()
        server = self._host_server
        self._host_server = None
        if server is not None:
            try:
                server.broadcast({"v": STREAM_VERSION, "type": "bye",
                                  "reason": "主持已停止会话"})
            except Exception:
                log.exception("广播停止消息失败")
            try:
                server.close_all()
            except Exception:
                pass
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        sender = self._stream_sender
        self._stream_sender = None
        if sender is not None:
            try:
                sender.stop()
            except Exception:
                log.exception("停止可靠流中转失败")
        self.stream_button.set_active(False)
        self._refresh_stream_button_tooltip()
        self._close_member_dialog()
        self._sync_members_button()
        self._chat_messages = []
        self._chat_last_sent = {}
        self.chat_panel.clear()
        try:
            if self._fs_chat_panel is not None:
                self._fs_chat_panel.clear()
        except tk.TclError:
            pass
        self._sync_chat_panel()
        self._sync_danmaku()
        self._host_password = ""
        log.info("停止主持流式")
        self._set_status("已停止主持")
        self._maybe_strip_orphan()

    # ---------------------------------------------------- 推流中转 / 推流器
    def _relay_http_port(self):
        if self._relay_http is None:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RelayHttpHandler)
            server.files = {}
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="relay-http").start()
            self._relay_http = server
            log.info("推流中转 HTTP 监听 127.0.0.1:%d", server.server_address[1])
        return self._relay_http.server_address[1]

    def _prepare_relay(self):
        """在后台把当前文件重封装成 TS 并建索引(支持任意偏移推流)。"""
        if self.current_path is None:
            return
        try:
            mtime = os.path.getmtime(self.current_path)
        except OSError:
            mtime = 0
        key = (self.current_path, mtime)
        if (self._relay is not None and self._relay_key == key
                and self._relay.ready):
            return
        if self._relay_building:
            return
        self._relay_building = True
        self._relay_key = key
        self._relay_percent = 0.0
        relay = TsRelay(self.current_path)
        self._relay = relay
        self._set_status("正在准备推流源…", "playing")

        def on_progress(percent):
            if self._relay is not relay:
                return
            self.queue_net_event({"kind": "relay_progress", "percent": percent})

        def worker():
            try:
                relay.build(progress=on_progress)
            except Exception:
                log.exception("准备推流源失败")
                relay.error = relay.error or "准备推流源失败"
            if relay.ready:
                _prune_relay_files(relay.token)
            self.queue_net_event({"kind": "relay_ready", "relay": relay,
                                  "ok": relay.ready, "error": relay.error})

        threading.Thread(target=worker, daemon=True, name="relay-build").start()

    def _sync_streamer(self, restart=False):
        """重建主持端推流链(客户端进出时调用)。"""
        self._rebuild_stream()

    def _sout_targets_now(self):
        """VLC sout 的实际目标: 本机可靠流中转(客户端由 TCP 分发)。"""
        if self._hosting and self._stream_sender is not None:
            return (("127.0.0.1", self._stream_sender.relay_port),)
        return ()

    def _rebuild_stream(self):
        """客户端进出/断开时重建媒体: 本地显示 + 推流到本机中转。"""
        targets = tuple(self._stream_targets())
        if not targets:
            if self._sout_targets:
                log.info("客户端已全部离开: 保持当前播放, 不重建媒体")
            return
        sout_targets = self._sout_targets_now()
        if not sout_targets:
            return
        if sout_targets == self._sout_targets:
            return
        self._sout_targets = sout_targets
        log.info("重建流: %d 个客户端 -> 可靠中转 127.0.0.1:%d",
                 len(targets), self._stream_sender.port)
        if self.player is None or self.current_path is None:
            return
        started = time.monotonic()
        position = max(0, self.player.get_time())
        was_playing = self.player.is_playing()
        rate = self._current_rate()
        self.media = self.instance.media_new(self.current_path)
        if position > 0:
            # 成员变化重建时必须从当前进度继续, 否则整条流会回到视频第一帧
            self.media.add_option(f":start-time={position / 1000.0:.3f}")
        self.media.add_option(
            f":sout={build_sout(sout_targets, rate=rate, **self._sout_kwargs())}")
        self.media.add_option(":sout-keep")
        # 有 sout 时 player.set_rate 会被转码链按 1x 处理, 必须用媒体选项
        self.media.add_option(f":rate={rate:g}")
        self.player.set_media(self.media)
        self.player.play()
        if position > 0:
            try:
                self.player.set_time(int(position))
            except Exception:
                log.debug("重建后回跳进度失败", exc_info=True)
        self.player.set_rate(rate)
        if not was_playing:
            self.player.pause()
        self._duration_logged = False
        self._last_state = None
        self._volume_applied = False
        self._bump_stream_epoch("rebuild")
        self._set_status(f"推流已从当前进度重建 · 房间 {self._room_count()} 人", "playing")
        log.info("流重建完成: 从当前进度推流(成员变化), 进度 %s, 耗时 %.2fs",
                 format_time(position), time.monotonic() - started)
        self._update_play_button()
        self._broadcast_state()

    def _strip_orphan_stream(self):
        """没有客户端时用一次原地 seek 剥离残留 sout(本地继续播放)。"""
        if self._closing or self._stream_targets():
            return
        had_stream = bool(self._sout_targets)
        self._sout_targets = None
        if not had_stream or self.player is None or self.current_path is None:
            return
        try:
            position = max(0, self.player.get_time())
            self.player.set_time(int(position))
            log.info("已剥离残留推流链(本地继续播放于 %s)", format_time(position))
        except Exception:
            log.exception("剥离残留推流链失败")

    def _maybe_strip_orphan(self):
        if self._hosting and self._stream_targets():
            return
        self._strip_orphan_stream()

    def _schedule_orphan_strip(self):
        self.root.after(5000, self._maybe_strip_orphan)

    def _mirror_streamer_state(self):
        """旧方案里主持播放器本身就是推流源, 无需镜像。"""
        return

    def _stop_streamer(self):
        return

    def _host_monitor_loop(self):
        while not self._host_monitor_stop.wait(1.0):
            server = self._host_server
            if server is None:
                break
            try:
                purged = server.purge_stale(float(self.settings.get("stale", STREAM_STALE_S)))
            except Exception:
                continue
            if purged:
                log.info("心跳超时清理客户端: %s", purged)

    def _host_name(self):
        return str(self.settings.get("username") or default_username())[:32]

    def _host_handle_message(self, client_id, message):
        if self._host_server is None:
            return
        message_type = message.get("type")
        if message_type == "hello":
            name = str(message.get("name") or "").strip()[:32] or f"客户端-{client_id}"
            stream_host = str(message.get("stream_host") or "").strip()
            try:
                stream_port = int(message.get("stream_port") or 0)
            except (TypeError, ValueError):
                stream_port = 0
            stream = None
            if stream_host and 1 <= stream_port <= 65535:
                stream = (stream_host, stream_port)
            else:
                log.warning("客户端 %s 未提供有效的流接收地址", name)
            self._host_server.set_client_info(client_id, name, stream)
            self._host_server.reply(client_id, {
                "v": STREAM_VERSION, "type": "welcome", "client_id": client_id,
                "host": self._host_name(),
            })
            log.info("客户端接入: %s (流目标 %s)", name, stream or "无")
            self._broadcast_room()
            self._rebuild_stream()
            self._host_server.reply(client_id, self._state_message())
            self._host_server.reply(client_id, {
                "v": STREAM_VERSION, "type": "chat_history",
                "messages": list(self._chat_messages),
            })
            self._set_status(f"客户端接入 · 房间 {self._room_count()} 人", "playing")
        elif message_type == "rename":
            name = str(message.get("name") or "").strip()[:32]
            if 4 <= len(name) <= 32 and self._host_server.rename_client(client_id, name):
                log.info("成员改名: %s (%s)", name, client_id)
                self._broadcast_room()
        elif message_type == "chat":
            text = str(message.get("text") or "").strip()
            if text:
                if self._chat_rate_allowed(client_id):
                    self._host_add_chat(client_id, text[:CHAT_MAX_CHARS])
                else:
                    self._host_server.reply(client_id, {
                        "v": STREAM_VERSION, "type": "chat_rejected",
                        "reason": "rate", "message": "消息发送频率过快",
                    })
        elif message_type == "cmd":
            self._apply_remote_command(client_id, message.get("action"),
                                       message.get("value"))
        else:
            log.debug("忽略未知消息类型: %s", message_type)

    def _apply_remote_command(self, client_id, action, value):
        if self.player is None or self.current_path is None:
            self._host_server.reply(client_id, {
                "v": STREAM_VERSION, "type": "error",
                "message": "主持端还没有可播放的视频",
            })
            return
        try:
            if action == "toggle":
                self.toggle_play()
            elif action == "play":
                if not self.player.is_playing():
                    self.toggle_play()
            elif action == "pause":
                if self.player.is_playing():
                    self.toggle_play()
            elif action == "seek":
                self._seek_to_ms(int(value))
            elif action == "seek_rel":
                self.seek_relative(int(value))
            elif action == "rate":
                rate = clamp(float(value), 0.25, 4.0)
                label = min(SPEED_OPTIONS,
                            key=lambda option: abs(float(option.rstrip("x")) - rate))
                self.speed_var.set(label)
                self._on_speed_change()
            elif action == "volume":
                volume = clamp(int(value), 0, 100)
                self.volume_var.set(volume)
                self._on_volume_change(volume)
            elif action == "mute":
                self.mute_var.set(bool(value))
                self._on_mute_toggle()
            elif action == "stop":
                self.stop()
            else:
                log.debug("客户端 %s 发送未知指令: %s", client_id, action)
                return
        except (TypeError, ValueError):
            log.warning("客户端 %s 指令参数无效: %s=%r", client_id, action, value)
            return
        self._broadcast_state()

    def _bump_stream_epoch(self, reason):
        """时间轴发生变化(换片/跳转/重建)时通知客户端重连流."""
        self._stream_epoch += 1
        log.info("流时间轴版本 +1 -> %d (%s)", self._stream_epoch, reason)

    def _stream_seek_allowed(self):
        """推流中也允许跳转: 主持端会中断推流、按新进度重建, 再通知客户端重连。"""
        return True

    def _schedule_stream_seek(self, target):
        """推流中的跳转: 先记下目标, 稍后中断推流并按新进度重建。"""
        if self.player is None or self.current_path is None:
            return
        if self.duration_ms:
            target = max(0, min(int(target), self.duration_ms - 200))
        target = max(0, int(target))
        self._restart_reason = "seek"
        self._seek_pending = target
        self._seek_hint = (target, time.monotonic())
        self.time_label.configure(text=format_time(target))
        if self._seek_rebuild_job is not None:
            try:
                self.root.after_cancel(self._seek_rebuild_job)
            except tk.TclError:
                pass
        self._seek_rebuild_job = self.root.after(80, self._perform_stream_seek)

    def _schedule_stream_rate(self):
        """推流中改速度: 在当前位置按新速度重建推流。"""
        if self.player is None or self.current_path is None:
            return
        position = max(0, int(self.player.get_time()))
        self._restart_reason = "rate"
        self._seek_pending = position
        if self._seek_rebuild_job is not None:
            try:
                self.root.after_cancel(self._seek_rebuild_job)
            except tk.TclError:
                pass
        self._seek_rebuild_job = self.root.after(120, self._perform_stream_seek)

    def _perform_stream_seek(self):
        """中断当前推流, 从目标进度重新起一条推流(流时间轴从 0 开始)。"""
        self._seek_rebuild_job = None
        target = self._seek_pending
        self._seek_pending = None
        reason = getattr(self, "_restart_reason", "seek")
        self._restart_reason = "seek"
        if target is None or self.player is None or self.current_path is None:
            return
        targets = tuple(self._stream_targets())
        sout_targets = self._sout_targets_now()
        if not (self._hosting and targets and sout_targets):
            return
        started = time.monotonic()
        rate = self._current_rate()
        was_playing = self.player.is_playing()
        self._sout_targets = sout_targets
        self._resyncing = True
        if reason == "rate":
            log.info("推流速度: 按 %gx 中断推流并重建", rate)
            self._set_status(f"切换速度 {rate:g}x · 正在重建推流…", "playing")
        else:
            log.info("推流跳转: 中断推流 -> %s -> 重建推流", format_time(target))
            self._set_status(f"跳转至 {format_time(target)} · 正在重建推流…", "playing")
        try:
            self.media = self.instance.media_new(self.current_path)
            self.media.add_option(
                f":sout={build_sout(sout_targets, **self._sout_kwargs())}")
            self.media.add_option(":sout-keep")
            self.media.add_option(f":start-time={target / 1000.0:.3f}")
            self.media.add_option(f":rate={rate:g}")
            self.player.set_media(self.media)
            self.player.play()
            self.player.set_rate(rate)
            if not was_playing:
                self.player.pause()
        except Exception:
            log.exception("推流跳转重建失败")
            self._resyncing = False
            return
        self._duration_logged = False
        self._last_state = None
        self._volume_applied = False
        self._seek_hint = (target, time.monotonic())
        self._bump_stream_epoch(reason)
        # 先通知客户端重连, 再补一份新状态
        self._broadcast_resync(target, reason)
        self._broadcast_state()
        self._update_play_button()
        self._resyncing = False
        if reason == "rate":
            log.info("推流速度重建完成: %gx (耗时 %.2fs)",
                     rate, time.monotonic() - started)
            self._set_status(f"已切换速度 {rate:g}x · 推流已重建", "playing")
        else:
            log.info("推流跳转完成: %s (耗时 %.2fs)",
                     format_time(target), time.monotonic() - started)
            self._set_status(f"已跳转到 {format_time(target)} · 推流已重建", "playing")

    def _broadcast_resync(self, position, reason="seek"):
        if not self._hosting or self._host_server is None:
            return
        self._host_server.broadcast({
            "v": STREAM_VERSION, "type": "resync",
            "reason": reason,
            "epoch": self._stream_epoch,
            "pos": int(position),
            "duration": int(self.duration_ms or 0),
        })
        if not self._hosting or self._host_server is None:
            return
        self._host_server.broadcast({
            "v": STREAM_VERSION, "type": "resync",
            "epoch": self._stream_epoch,
            "pos": int(position),
            "duration": int(self.duration_ms or 0),
        })

    def _kill_orphan_stream(self):
        """没有在线客户端时, 本地跳转会打断残留的 sout 链, 这里同步清理记录."""
        if self._sout_targets and not self._stream_targets():
            self._sout_targets = None
            log.info("本地跳转终止了残留推流链")

    def _seek_to_ms(self, target):
        if self.player is None or self.current_path is None:
            return
        if self._hosting and self._stream_targets():
            self._schedule_stream_seek(target)
            return
        self._kill_orphan_stream()
        if self.duration_ms:
            target = max(0, min(int(target), self.duration_ms - 200))
        target = max(0, int(target))
        self.player.set_time(target)
        self.time_label.configure(text=format_time(target))
        self._seek_hint = (target, time.monotonic())
        self._bump_stream_epoch("seek")
        log.info("进度跳转 -> %s / %s (%d ms)", format_time(target),
                 format_time(self.duration_ms), target)

    def _state_message(self):
        name = ""
        if self.current_path:
            name = os.path.basename(self.current_path)
        pos = 0
        if self.player is not None and self.current_path is not None:
            pos = self.player.get_time()
            if pos < 0:
                pos = 0
            # 播放结束后位置归零, 否则新加入的客户端会从片尾字幕开始
            if (self.player.get_state() in (vlc.State.Ended, vlc.State.Stopped)
                    or (self.duration_ms and pos >= self.duration_ms - 500)):
                pos = 0
        if self._seek_hint is not None:
            hint_pos, hint_ts = self._seek_hint
            if time.monotonic() - hint_ts > 2.0 or pos > 800:
                self._seek_hint = None
            else:
                pos = hint_pos
        playing = bool(self.player is not None and self.current_path is not None
                       and self.player.is_playing())
        return {
            "v": STREAM_VERSION, "type": "state",
            "file": self.current_path or "",
            "name": name,
            "pos": int(pos),
            "duration": int(self.duration_ms or 0),
            "playing": playing,
            "rate": self._current_rate(),
            "volume": self.volume_var.get(),
            "muted": bool(self.mute_var.get()),
            "stopped": self.current_path is None,
            "epoch": self._stream_epoch,
        }

    def _broadcast_state(self):
        if not self._hosting or self._host_server is None:
            return
        self._host_server.broadcast(self._state_message())

    def _room_message(self):
        members = self._host_server.snapshot() if self._host_server is not None else []
        host_name = self._host_name()
        entries = [{"id": "host", "name": host_name, "host": True}]
        entries += [
            {"id": m["id"], "name": m.get("name") or f"客户端-{m['id']}",
             "host": False}
            for m in members
        ]
        return {
            "v": STREAM_VERSION, "type": "room",
            "host": host_name,
            "count": len(entries),
            "members": entries,
        }

    def _broadcast_room(self):
        if not self._hosting or self._host_server is None:
            return
        count = self._room_count()
        if count != self._last_room_count:
            self._last_room_count = count
            self._refresh_stream_button_tooltip()
        self._host_server.broadcast(self._room_message())
        self._refresh_member_dialog()

    def _kick_member(self, client_id):
        """主持端: 把某个房客移出房间。"""
        if not self._hosting or self._host_server is None or client_id == "host":
            return
        names = {m["id"]: m["name"] for m in self._room_message()["members"]}
        name = names.get(client_id, "成员")
        if self._host_server.kick_client(client_id):
            log.info("已移出成员: %s (%s)", name, client_id)
            self._set_status(f"已移出 {name}", "ok")
            self._broadcast_room()
            self._rebuild_stream()

    # ---------------------------------------------------------- 房间成员窗
    def open_members(self):
        if not (self._hosting or self._client_active):
            return
        if self._member_dialog is not None:
            try:
                if self._member_dialog.winfo_exists():
                    self._refresh_member_dialog()
                    self._member_dialog.lift()
                    self._member_dialog.focus_force()
                    return
            except tk.TclError:
                pass
            self._member_dialog = None
        self._member_dialog = RoomMembersDialog(
            self.root,
            on_kick=self._kick_member,
            on_close=self._member_dialog_closed,
            is_host=self._hosting,
            my_id="host" if self._hosting else (self._client_id or ""),
        )
        self._track_dialog(self._member_dialog)
        self._refresh_member_dialog()
        self._member_dialog.present_async()

    def _member_dialog_closed(self):
        self._member_dialog = None

    def _close_member_dialog(self):
        dialog = self._member_dialog
        self._member_dialog = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _refresh_member_dialog(self):
        dialog = self._member_dialog
        if dialog is None:
            return
        try:
            if not dialog.winfo_exists():
                self._member_dialog = None
                return
            data = self._room_message() if self._hosting else (self._client_room or {})
            dialog.update_room(data.get("members") or [])
        except tk.TclError:
            self._member_dialog = None

    def _sync_members_button(self):
        try:
            if self._hosting or self._client_active:
                if not self.members_button.winfo_ismapped():
                    self.members_button.pack(side=tk.LEFT, padx=(8, 0),
                                             before=self.open_button)
            else:
                self.members_button.pack_forget()
        except (tk.TclError, AttributeError):
            pass

    # ---------------------------------------------------------- 房间聊天
    def _chat_limit(self):
        try:
            return max(1, int(self.settings.get("chat_limit", 200)))
        except (TypeError, ValueError):
            return 200

    def _trim_chat_history(self):
        limit = self._chat_limit()
        if len(self._chat_messages) > limit:
            del self._chat_messages[:len(self._chat_messages) - limit]

    def _send_chat(self, text):
        text = str(text or "").strip()
        if not text:
            return
        text = text[:CHAT_MAX_CHARS]
        if self._hosting:
            self._host_add_chat("host", text)
        elif self._client_active:
            # 客户端不在本地展示, 等主持转发回来再显示
            self._client_send({"v": STREAM_VERSION, "type": "chat",
                               "text": text})
        else:
            self._set_status("不在房间中, 无法发送消息", "error")

    def _chat_rate_allowed(self, client_id):
        try:
            limit = int(self.settings.get("chat_rate_limit", 1))
        except (TypeError, ValueError):
            limit = 1
        if limit <= 0:
            return True
        now = time.monotonic()
        last = self._chat_last_sent.get(client_id)
        if last is not None and now - last < limit:
            return False
        self._chat_last_sent[client_id] = now
        return True

    def _host_add_chat(self, sender_id, text):
        if self._host_server is None:
            return
        name = self._host_name()
        if sender_id != "host":
            for member in self._host_server.snapshot():
                if member["id"] == sender_id:
                    name = member.get("name") or f"客户端-{sender_id}"
                    break
        message = {
            "sender_id": sender_id,
            "name": name,
            "text": str(text)[:CHAT_MAX_CHARS],
            "ts": int(time.time()),
        }
        self._chat_messages.append(message)
        self._trim_chat_history()
        self._host_server.broadcast({"v": STREAM_VERSION, "type": "chat",
                                     "message": message})
        self._append_chat_ui(message, mine=(sender_id == "host"))
        self._spawn_danmaku(message)

    def _append_chat_ui(self, message, mine=False):
        try:
            self.chat_panel.add_message(message, mine=mine)
        except tk.TclError:
            pass
        try:
            if self._fs_chat_panel is not None:
                self._fs_chat_panel.add_message(message, mine=mine)
        except tk.TclError:
            pass

    def _sync_chat_panel(self):
        try:
            active = (self._hosting or self._client_active) and not self._fullscreen
            if active:
                if not self.chat_panel.winfo_ismapped():
                    self.chat_panel.pack(side=tk.RIGHT, fill=tk.Y)
            else:
                self.chat_panel.pack_forget()
            if self._fs_chat_panel is not None:
                self._fs_chat_panel._max_rows = self._chat_limit()
            if not (self._hosting or self._client_active):
                self._hide_fs_chat()
            self._schedule_danmaku_sync()
        except (tk.TclError, AttributeError):
            pass

    # ---------------------------------------------------------- 消息弹幕
    def _app_is_foreground(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if not fg:
                return True
            root_hwnd = user32.GetAncestor(int(self.root.winfo_id()), 2)
            return int(fg) == int(root_hwnd)
        except Exception:
            return True

    def _app_visible_at_video(self):
        """窗口没在前台时, 只要视频区域在屏幕上可见也显示弹幕。"""
        if self._fullscreen:
            return True
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            root_hwnd = user32.GetAncestor(int(self.root.winfo_id()), 2)
            if not root_hwnd:
                return True
            self.video_frame.update_idletasks()
            vx = self.video_frame.winfo_rootx()
            vy = self.video_frame.winfo_rooty()
            vw = max(1, self.video_frame.winfo_width())
            vh = max(1, self.video_frame.winfo_height())
            for fx, fy in ((0.5, 0.5), (0.25, 0.25), (0.75, 0.3), (0.5, 0.8)):
                point = wintypes.POINT(int(vx + vw * fx), int(vy + vh * fy))
                hwnd = user32.WindowFromPoint(point)
                if hwnd and user32.GetAncestor(hwnd, 2) == root_hwnd:
                    return True
            return False
        except Exception:
            return True

    def _track_dialog(self, dialog):
        if dialog is None:
            return
        if dialog not in self._secondary_windows:
            self._secondary_windows.append(dialog)
        try:
            dialog.bind("<Destroy>", lambda _e, d=dialog: self._forget_dialog(d),
                        add="+")
        except tk.TclError:
            pass
        self._lift_secondary()
        if self._secondary_job is None:
            self._secondary_job = self.root.after(300, self._watch_secondary)

    def _forget_dialog(self, dialog):
        try:
            self._secondary_windows.remove(dialog)
        except ValueError:
            pass

    def _app_group_active(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if not fg:
                return True
            root_hwnd = user32.GetAncestor(int(self.root.winfo_id()), 2)
            if int(fg) == int(root_hwnd):
                return True
            for dialog in self._secondary_windows:
                if not dialog.winfo_exists():
                    continue
                if int(fg) == int(user32.GetAncestor(int(dialog.winfo_id()), 2)):
                    return True
            return False
        except Exception:
            return True

    def _lift_secondary(self):
        alive = []
        for dialog in self._secondary_windows:
            try:
                if not dialog.winfo_exists():
                    continue
                alive.append(dialog)
                if dialog.winfo_viewable():
                    dialog.lift()
            except tk.TclError:
                pass
        self._secondary_windows = alive

    def _watch_secondary(self):
        self._secondary_job = None
        if self._secondary_windows and self._app_group_active():
            self._lift_secondary()
        if self._secondary_windows:
            self._secondary_job = self.root.after(300, self._watch_secondary)

    def _dialog_open(self):
        for dialog in self._secondary_windows:
            try:
                if dialog.winfo_exists():
                    return True
            except tk.TclError:
                pass
        for attr in ("_settings_dialog", "_member_dialog", "_password_dialog"):
            dialog = getattr(self, attr, None)
            if dialog is not None:
                try:
                    if dialog.winfo_exists():
                        return True
                except tk.TclError:
                    pass
        return bool(getattr(self, "_alert_open", False))

    def _danmaku_should_show(self):
        return (bool(self.settings.get("danmaku", True))
                and (self._hosting or self._client_active)
                and (self.current_path is not None or self._client_active)
                and bool(self.root.winfo_viewable()))

    def _sync_danmaku(self, clear=False):
        overlay = getattr(self, "danmaku", None)
        if overlay is None:
            return
        try:
            color = DANMAKU_COLORS.get(
                self.settings.get("danmaku_color", "white"),
                ("", "#FFFFFF"))[1]
            overlay.configure_style(
                self.settings.get("danmaku_size", 20),
                int(self.settings.get("danmaku_speed", 100)) / 100.0,
                color, self.settings.get("danmaku_area", 100))
            if not self._danmaku_should_show():
                overlay.hide()
                return
            if clear:
                overlay._clear()
            overlay.show()
        except tk.TclError:
            pass

    def _schedule_danmaku_sync(self):
        if self._danmaku_sync_job is None:
            self._danmaku_sync_job = self.root.after(80, self._danmaku_sync_done)

    def _danmaku_sync_done(self):
        self._danmaku_sync_job = None
        self._sync_danmaku()

    def _watch_danmaku(self):
        self._danmaku_watch_job = None
        if self._closing:
            return
        try:
            self._sync_danmaku()
        finally:
            self._danmaku_watch_job = self.root.after(500, self._watch_danmaku)

    def _spawn_danmaku(self, message):
        if not self.settings.get("danmaku", True):
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        name = str(message.get("name") or "").strip()
        text = (f"{name}：{text}" if name else text)[:DANMAKU_MAX_CHARS]
        if not self.danmaku._visible:
            self._sync_danmaku()
        try:
            self.danmaku.spawn(text)
        except tk.TclError:
            pass

    def _refresh_stream_button_tooltip(self):
        tooltip = getattr(self.stream_button, "_tooltip", None)
        if tooltip is None:
            return
        if self._hosting:
            tooltip.text = (f"主持中 · 房间 {self._room_count()} 人 · "
                            f"指令 {self._host_cmd_port} / 流 {self._host_udp_port}\n"
                            f"点击停止主持")
        elif self._client_active:
            tooltip.text = (f"已加入 · 房间 {self._client_room.get('count', 0)} 人\n"
                            f"点击退出会话")
        else:
            tooltip.text = "流式传输 (主持 / 加入)"

    def _stream_targets(self):
        if self._host_server is None or not self._hosting:
            return []
        return self._host_server.targets()

    # ------------------------------------------------------------- 客户端
    def join_stream(self, cmd_host, cmd_port, udp_host, udp_port):
        if self._client_active:
            self.leave_stream()
        if self._hosting:
            self._alert("无法加入", "当前正在主持会话, 请先停止主持。")
            return
        if self.player is not None and self.current_path is not None:
            old = self.player
            self.player = None
            self._dispose_player_async(old)
            self._create_player()
            self.current_path = None
            self.progress_var.set(0.0)
            self.time_label.configure(text="00:00")
            self.duration_label.configure(text="00:00")
        if self._stream_receiver is not None:
            try:
                self._stream_receiver.stop()
            except Exception:
                pass
            self._stream_receiver = None
        try:
            receiver = UdpMediaReceiver(udp_host, udp_port)
            receiver.start()
        except OSError as error:
            log.error("流接收端口 %d 打开失败: %s", udp_port, error)
            self._alert(
                "流端口不可用",
                f"本机流接收端口 {udp_port} 打开失败:\n{error}\n请换一个端口后重试。",
            )
            return
        self._stream_receiver = receiver
        self._client_active = True
        self._client_password = None
        self._close_password_dialog()
        self._client_cmd_addr = (cmd_host, cmd_port)
        self._client_udp_addr = (udp_host, udp_port)
        self._client_state = None
        self._client_room = {"count": 0, "host": "", "members": []}
        self._client_stop.clear()
        self._client_next_retry = 0.0
        self._client_lost_at = 0.0
        self._client_reconnecting = False
        self._client_video_started = False
        self._client_epoch_seen = None
        self._client_speed_backup = self.speed_var.get()
        self.drop_hint.pack_forget()
        self.watch_view.update({"phase": "connecting", "name": "", "room": 0,
                                "host": ""})
        self.watch_view.pack(fill=tk.BOTH, expand=True)
        self.stream_button.set_active(True)
        self.open_button.set_enabled(False)
        self._refresh_stream_button_tooltip()
        self._sync_members_button()
        self._chat_messages = []
        self.chat_panel._max_rows = self._chat_limit()
        self.chat_panel.clear()
        try:
            if self._fs_chat_panel is not None:
                self._fs_chat_panel.clear()
        except tk.TclError:
            pass
        self._sync_chat_panel()
        self._sync_danmaku()
        self.root.title(f"同步播放 — {APP_TITLE}")
        def connect_worker():
            ok = self._client_connect()
            self.queue_net_event({"kind": "join_result", "ok": ok})

        self._set_status("正在连接主持…", "playing")
        self._show_buffering()
        threading.Thread(target=connect_worker, daemon=True,
                         name="client-connect").start()

    def leave_stream(self):
        if not self._client_active:
            return
        self._client_active = False
        self._client_reconnecting = False
        self._client_lost_at = 0.0
        self._client_preparing = False
        self._client_stop.set()
        receiver = self._stream_receiver
        self._stream_receiver = None
        if receiver is not None:
            try:
                receiver.stop()
            except Exception:
                log.exception("停止可靠流接收失败")
        sock = self._client_sock
        self._client_sock = None
        self._client_connected = False
        self._close_password_dialog()
        self._client_password = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._stop_client_video()
        self._client_state = None
        self._client_id = None
        self.watch_view.pack_forget()
        if self.current_path is None:
            self.drop_hint.pack(fill=tk.BOTH, expand=True)
        self.stream_button.set_active(False)
        self.open_button.set_enabled(True)
        self._refresh_stream_button_tooltip()
        self._close_member_dialog()
        self._sync_members_button()
        self._chat_messages = []
        self.chat_panel.clear()
        try:
            if self._fs_chat_panel is not None:
                self._fs_chat_panel.clear()
        except tk.TclError:
            pass
        self._sync_chat_panel()
        self._sync_danmaku()
        self.root.title(APP_TITLE)
        speed_backup = getattr(self, "_client_speed_backup", None) or "1.0x"
        if self.speed_var.get() != speed_backup:
            self.speed_var.set(speed_backup)
        self.time_label.configure(text="00:00")
        self.duration_label.configure(text="00:00")
        self.progress_var.set(0.0)
        self._update_play_button()
        log.info("已退出流式会话")
        self._set_status("已退出流式会话")

    def _client_connect(self):
        sock = None
        try:
            sock = socket.create_connection(self._client_cmd_addr, timeout=4)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            return False
        with self._client_send_lock:
            self._client_sock = sock
            self._client_connected = True
        self._client_last_recv = time.time()
        threading.Thread(target=self._client_reader_loop, args=(sock,), daemon=True).start()
        threading.Thread(target=self._client_heartbeat_loop, daemon=True).start()
        self._client_send({
            "v": STREAM_VERSION, "type": "hello",
            "name": self._host_name()[:32],
            "stream_host": self._client_udp_addr[0],
            "stream_port": self._client_udp_addr[1],
            "password": self._client_password or "",
        })
        return True

    def _client_send(self, message):
        sock = self._client_sock
        if sock is None or not self._client_connected:
            return
        try:
            with self._client_send_lock:
                sock.sendall(encode_message(message))
        except (OSError, ValueError):
            self._client_mark_lost(sock)

    def _client_send_cmd(self, action, value=None):
        if not self._client_connected:
            return
        self._client_send({"v": STREAM_VERSION, "type": "cmd",
                           "action": action, "value": value})
        log.debug("向主持发送指令: %s = %r", action, value)

    def _client_mark_lost(self, sock):
        with self._client_send_lock:
            if self._client_sock is not sock:
                return
            self._client_sock = None
            self._client_connected = False
        try:
            sock.close()
        except OSError:
            pass
        self.queue_net_event({"kind": "client_lost"})

    def _client_reader_loop(self, sock):
        buffer = b""
        while not self._client_stop.is_set():
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            self._client_last_recv = time.time()
            buffer += chunk
            if len(buffer) > STREAM_MAX_MSG_BYTES * 2:
                log.warning("主持消息过大, 断开")
                break
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self.queue_net_event({"kind": "client_message", "message": message})
        self._client_mark_lost(sock)

    def _client_heartbeat_loop(self):
        heartbeat = float(self.settings.get("heartbeat", STREAM_HEARTBEAT_S))
        stale = float(self.settings.get("stale", STREAM_STALE_S))
        while not self._client_stop.is_set():
            if self._client_stop.wait(heartbeat):
                break
            if not self._client_connected or self._client_sock is None:
                break
            if time.time() - self._client_last_recv > stale:
                log.warning("主持心跳超时")
                self._client_mark_lost(self._client_sock)
                break
            self._client_send({"v": STREAM_VERSION, "type": "ping"})

    def _client_stream_url(self):
        receiver = self._stream_receiver
        if receiver is not None and receiver.local_port > 0:
            return f"udp://@127.0.0.1:{receiver.local_port}"
        return ""

    def _dispose_player_async(self, player):
        """在后台线程回收旧播放器: libvlc 停止 UDP 输入偶尔会阻塞很久,
        放在 UI 线程里会直接把窗口卡死。"""
        # 先立即静音 + 暂停, 避免后台 stop() 阻塞期间旧播放器继续出声,
        # 换片/重连时出现"两份声音"。
        try:
            player.audio_set_mute(True)
            player.set_pause(1)
        except Exception:
            log.debug("静音旧播放器失败", exc_info=True)

        def worker():
            try:
                player.stop()
            except Exception:
                log.debug("后台停止旧播放器失败", exc_info=True)
            try:
                player.release()
            except Exception:
                log.debug("后台释放旧播放器失败", exc_info=True)
            log.debug("旧播放器已在后台回收")

        threading.Thread(target=worker, daemon=True, name="vlc-dispose").start()

    def _create_player(self):
        """新建一个播放器并绑定视频窗口(不复用可能卡在 stop 里的旧对象)。"""
        if self.instance is None:
            return None
        self.player = self.instance.media_player_new()
        self.player.set_rate(1.0)
        self._attach_video_output()
        return self.player

    def _show_buffering(self):
        if self._closing or self._buffering_visible:
            return
        try:
            self.buffering.set_user_scale(self.settings.get("buffer_scale", 1.0))
        except Exception:
            log.debug("设置缓冲层缩放失败", exc_info=True)
        self._buffering_visible = True
        try:
            self.buffering.place(relx=0.5, rely=0.5, anchor="center")
            self.buffering.lift()
            self.buffering.show()
        except Exception:
            log.debug("显示缓冲层失败", exc_info=True)

    def _hide_buffering(self):
        if not self._buffering_visible:
            return
        self._buffering_visible = False
        try:
            self.buffering.hide()
            self.buffering.place_forget()
        except Exception:
            log.debug("隐藏缓冲层失败", exc_info=True)

    def _buffering_tick(self):
        self._buffer_job = None
        try:
            self._update_buffering_state()
        except Exception:
            log.exception("缓冲状态检查异常")
        if not self._closing:
            self._buffer_job = self.root.after(250, self._buffering_tick)

    def _update_buffering_state(self):
        if not self._client_active:
            self._hide_buffering()
            return
        state = self._client_state or {}
        # 与底部播放/暂停按钮一致: 按钮显示“播放中”才需要出画面
        if not state.get("playing") or state.get("stopped"):
            self._hide_buffering()
            return
        if not self._client_video_started or self.player is None:
            self._show_buffering()
            return
        pictures = -1
        try:
            media = self.player.get_media()
            if media is not None:
                stats = vlc.MediaStats()
                if media.get_stats(stats):
                    pictures = int(stats.displayed_pictures or 0)
        except Exception:
            pictures = -1
        if pictures < 0:
            try:
                pictures = int(self.player.get_time())
            except Exception:
                pictures = -1
        now = time.monotonic()
        if pictures <= 0:
            # 还没显示过任何一帧
            self._show_buffering()
            return
        if pictures != self._displayed_pictures:
            self._displayed_pictures = pictures
            self._last_frame_ts = now
            self._hide_buffering()
            return
        if self._last_frame_ts == 0.0:
            self._last_frame_ts = now
            return
        if now - self._last_frame_ts > 0.45:
            self._show_buffering()

    def _start_client_video(self):
        """客户端用同一套 VLC 播放主持推来的 UDP 流, 画面直接渲染到视频区。"""
        if self.instance is None or self._client_udp_addr is None:
            return
        if self.player is None:
            self._create_player()
        elif self._client_video_started:
            # 时间轴变了要重连流: 旧播放器异步回收, 换一个新的再播
            old = self.player
            self.player = None
            self._dispose_player_async(old)
            self._create_player()
        if self.player is None:
            return
        if self._stream_receiver is not None:
            self._stream_receiver.set_local_port(ephemeral_udp_port())
        url = self._client_stream_url()
        if not url:
            log.warning("可靠流接收器未就绪, 无法开始客户端画面")
            return
        caching = int(self.settings.get("client_caching", 1000))
        try:
            self._client_media = self.instance.media_new(url)
            self._client_media.add_option(f":network-caching={caching}")
            self._client_media.add_option(f":live-caching={caching}")
            # 直播流不对时钟做严格等待, 丢包时尽量继续出画面而不是直接黑屏
            self._client_media.add_option(":clock-jitter=0")
            self._client_media.add_option(":clock-synchro=0")
            self._client_media.add_option(":drop-late-frames")
            self._client_media.add_option(":skip-frames")
            self.player.set_media(self._client_media)
            # 画面速率由主持端的转码流决定, 本地固定 1.0x
            self.player.set_rate(1.0)
            self.player.audio_set_volume(self.volume_var.get())
            self.player.audio_set_mute(self.mute_var.get())
            self.player.play()
            self._client_video_started = True
            self._client_video_start_ts = time.monotonic()
            self._client_last_media_ms = -1
            self._displayed_pictures = -1
            self._last_frame_ts = 0.0
            self._client_stall_since = None
            self._schedule_danmaku_sync()
            self.watch_view.pack_forget()
            self._show_buffering()
            self.root.after(900, self._update_track_button)
            log.info("客户端开始接收并渲染流: %s", url)
        except Exception:
            log.exception("客户端流渲染启动失败")

    def _client_watchdog(self):
        self._client_watchdog_job = None
        if self._closing:
            return
        try:
            self._client_watchdog_check()
        except Exception:
            log.exception("客户端画面看门狗异常")
        finally:
            if not self._closing:
                self._client_watchdog_job = self.root.after(
                    1000, self._client_watchdog)

    def _client_watchdog_check(self):
        if not (self._client_active and self._client_video_started
                and self.player is not None):
            self._client_stall_since = None
            return
        if getattr(self, "_client_preparing", False):
            return
        state = self._client_state or {}
        if not state.get("playing") or state.get("stopped"):
            self._client_stall_since = None
            return
        now = time.monotonic()
        try:
            media_ms = int(self.player.get_time())
            playing = bool(self.player.is_playing())
        except Exception:
            return
        if media_ms >= 0 and media_ms != self._client_last_media_ms:
            self._client_last_media_ms = media_ms
            self._client_stall_since = now
            return
        if now - self._client_video_start_ts < 6.0:
            return
        if self._client_stall_since is None:
            self._client_stall_since = now
            return
        stall = now - self._client_stall_since
        if (not playing and stall >= 2.0) or stall >= 5.0:
            if now - self._client_last_restart < 5.0:
                return
            self._client_last_restart = now
            self._client_restart_count += 1
            log.warning("客户端画面停滞 %.1fs, 自动重启本机画面 (第 %d 次)",
                        stall, self._client_restart_count)
            self._set_status("画面中断 · 正在重建本机画面…", "playing")
            self._client_stall_since = None
            self._client_last_media_ms = -1
            self._start_client_video()

    def _stop_client_video(self):
        """退出会话: 旧播放器交给后台回收, UI 立刻可用。"""
        self._hide_buffering()
        self._client_video_started = False
        self._client_epoch_seen = None
        player = self.player
        self.player = None
        if player is not None:
            self._dispose_player_async(player)
        # 给本地播放准备一个干净的播放器(关闭过程中就不必了)
        if not self._closing:
            self._create_player()

    def _client_handle_message(self, message):
        if not self._client_active:
            return
        message_type = message.get("type")
        if message_type == "need_password":
            log.info("房间需要密码, 等待输入")
            self._prompt_room_password(str(message.get("message") or ""))
        elif message_type == "resync":
            # 主持中断了推流并从新进度/新速度重建: 客户端重新连接 UDP 流
            position = int(message.get("pos") or 0)
            reason = message.get("reason")
            try:
                epoch = int(message.get("epoch"))
            except (TypeError, ValueError):
                epoch = None
            self._client_epoch_seen = epoch
            if reason == "rate":
                self._set_status("主持调整了播放速度 · 正在重新连接画面…", "playing")
                log.info("主持按新速度重建推流, 客户端重连画面")
            else:
                self._set_status(
                    f"主持跳转到 {format_time(position)} · 正在重新连接画面…", "playing")
                log.info("主持跳转重建推流, 客户端重连画面: %s", format_time(position))
            if self.player is not None:
                self._start_client_video()
        elif message_type == "welcome":
            self._client_id = message.get("client_id")
            if self._stream_receiver is not None:
                self._stream_receiver.set_client_id(self._client_id)
            self._close_password_dialog()
            log.info("主持已确认加入: client_id=%s", self._client_id)
        elif message_type == "state":
            self._close_password_dialog()
            self._client_apply_state(message)
        elif message_type == "room":
            self._client_room = message
            self._refresh_stream_button_tooltip()
            self._refresh_member_dialog()
            self._watch_refresh()
        elif message_type == "chat_rejected":
            text = str(message.get("message") or "消息发送频率过快")
            self.chat_panel.show_hint(text)
            self._set_status(text, "error")
        elif message_type == "chat":
            chat = message.get("message") or {}
            self._chat_messages.append(chat)
            self._trim_chat_history()
            self._append_chat_ui(
                chat, mine=(chat.get("sender_id") == self._client_id))
            self._spawn_danmaku(chat)
        elif message_type == "chat_history":
            self._chat_messages = list(message.get("messages") or [])
            self._trim_chat_history()
            self.chat_panel.set_messages(self._chat_messages,
                                         self._client_id or "")
            try:
                if self._fs_chat_panel is not None:
                    self._fs_chat_panel.set_messages(self._chat_messages,
                                                     self._client_id or "")
            except tk.TclError:
                pass
        elif message_type == "kicked":
            reason = str(message.get("reason") or "你已被主持移出房间")
            log.info("被主持移出房间: %s", reason)
            self._close_member_dialog()
            self.leave_stream()
            self._alert("已离开房间", reason)
        elif message_type == "pong":
            pass
        elif message_type == "bye":
            log.info("主持结束会话: %s", message.get("reason"))
            self._close_password_dialog()
            self._alert("会话结束", str(message.get("reason") or "主持已停止会话"))
            self.leave_stream()
        elif message_type == "error":
            code = message.get("code")
            if code == "bad_password":
                log.warning("房间密码验证失败")
                self._set_status("房间密码错误", "error")
                if self._password_dialog is not None:
                    try:
                        self._password_dialog.set_waiting(False)
                        self._password_dialog.show_error("密码错误, 请重试")
                        self._password_dialog.lift()
                    except tk.TclError:
                        self._password_dialog = None
                else:
                    self._prompt_room_password("密码错误, 请重试")
            elif code == "room_full":
                self._alert("房间已满", str(message.get("message") or "房间人数已满"))
                self.leave_stream()
            else:
                log.warning("主持返回错误: %s", message.get("message"))
                self._set_status(f"主持: {message.get('message', '错误')}", "error")
        else:
            log.debug("忽略未知消息类型: %s", message_type)

    # --------------------------------------------------------- 房间密码
    def _prompt_room_password(self, error=""):
        if self._password_dialog is not None:
            try:
                self._password_dialog.set_waiting(False)
                self._password_dialog.show_error(error or "请输入房间密码")
                self._password_dialog.lift()
                return
            except tk.TclError:
                self._password_dialog = None
        dialog = RoomPasswordDialog(
            self.root,
            submit=self._submit_room_password,
            cancel=self._cancel_room_password,
            error=error,
        )
        self._password_dialog = dialog
        self._track_dialog(dialog)
        dialog.present_async()
        self._set_status("房间需要密码 · 请输入密码", "playing")

    def _submit_room_password(self, password):
        self._client_password = password
        self._client_send({
            "v": STREAM_VERSION, "type": "auth", "password": password,
        })
        self._set_status("正在验证房间密码…", "playing")

    def _cancel_room_password(self):
        if self._client_active:
            self.leave_stream()

    def _close_password_dialog(self):
        dialog = self._password_dialog
        self._password_dialog = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _client_apply_state(self, message):
        self._client_state = message
        self._client_state_ts = time.time()
        name = message.get("name") or ""
        if message.get("file"):
            self.root.title(f"{name} — 同步播放 — {APP_TITLE}")
        try:
            rate = float(message.get("rate") or 1.0)
        except (TypeError, ValueError):
            rate = 1.0
        label = f"{rate:g}x"
        if label not in SPEED_OPTIONS:
            label = min(SPEED_OPTIONS,
                        key=lambda option: abs(float(option.rstrip("x")) - rate))
        if self.speed_var.get() != label:
            self.speed_var.set(label)
        # 音量/静音属于各端本地体验, 不跟着主持的状态走
        self._update_play_button()

        # 主持正在准备推流源: 先回到等待面板, 不启动/重启视频
        if message.get("preparing"):
            self._client_preparing = True
            if self._client_video_started:
                self._stop_client_video()
                self.watch_view.pack(fill=tk.BOTH, expand=True)
            self._watch_refresh()
            self._set_status(
                f"主持正在准备推流源 {int(message.get('prepare_pct') or 0)}%…",
                "playing",
            )
            return
        if getattr(self, "_client_preparing", False):
            # 推流源就绪, 强制重连新推流
            self._client_preparing = False
            self._client_epoch_seen = None

        # 时间轴版本变化(换片/跳转/变速/重建)时重连流, 否则只在首帧前启动
        try:
            epoch = int(message.get("epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        if not self._client_video_started or epoch != self._client_epoch_seen:
            self._client_epoch_seen = epoch
            self._start_client_video()

        # 主持暂停时客户端会把缓冲播到暂停点后停住; 恢复播放时如果本地流已结束,
        # 立即重连并让缓冲提示盖住首帧, 不要停在视频第一帧上。
        playing = bool(message.get("playing")) and not message.get("stopped")
        if (playing and self._client_video_started and self.player is not None
                and not getattr(self, "_client_preparing", False)):
            try:
                pstate = self.player.get_state()
            except Exception:
                pstate = None
            ended_states = (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error,
                            getattr(vlc.State, "NothingSpecial", None))
            if pstate in ended_states:
                log.info("主持恢复播放, 本地流已结束, 重新连接画面")
                self._client_epoch_seen = None
                self._start_client_video()
            elif not self.player.is_playing():
                self.player.play()
                self._show_buffering()

        if message.get("stopped"):
            self._set_status("主持已停止播放")
        self._watch_refresh()
        if self._client_video_started:
            self._set_status(
                f"同步播放 {name}" if name else "同步播放", "playing")

    def _watch_refresh(self):
        self.watch_view.update({
            "phase": "reconnecting" if not self._client_connected else "joined",
            "name": (self._client_state or {}).get("name") or "",
            "room": self._client_room.get("count", 0),
            "host": self._client_room.get("host") or "",
        })

    def _client_on_lost(self):
        if not self._client_active:
            return
        self._hide_buffering()
        self._client_connected = False
        self._client_reconnecting = False
        self._client_lost_at = time.time()
        self._client_next_retry = time.time() + 3.0
        self._close_password_dialog()
        self._set_status("与主持失去连接 · 3 秒后自动重连", "error")
        self._update_play_button()

    def _client_retry_reconnect(self):
        if not self._client_active or self._client_connected:
            return
        if self._client_reconnecting:
            return
        if time.time() < self._client_next_retry:
            return
        if self._client_lost_at and time.time() - self._client_lost_at > 20.0:
            log.warning("与主持断开超过 12 秒, 退出会话")
            self._set_status("与主持断开连接, 已退出会话", "error")
            self.leave_stream()
            return
        self._client_reconnecting = True
        self._client_next_retry = time.time() + 3.0
        log.info("尝试重连主持 %s:%d", *self._client_cmd_addr)

        def worker():
            ok = False
            try:
                ok = self._client_connect()
            except Exception:
                log.exception("重连主持失败")
            self.queue_net_event({"kind": "client_reconnected", "ok": ok})

        threading.Thread(target=worker, daemon=True, name="client-reconnect").start()

    def _refresh_client_state(self):
        state = self._client_state
        if state is None:
            return
        position = float(state.get("pos") or 0)
        duration = int(state.get("duration") or 0)
        if state.get("playing") and duration:
            position += (time.time() - self._client_state_ts) * 1000.0 \
                * float(state.get("rate") or 1.0)
            position = min(position, float(duration))
        if not self._seeking:
            self.time_label.configure(text=format_time(int(position)))
            if duration > 0:
                self.progress.set(min(1000.0, position / duration * 1000.0))
        self.duration_label.configure(text=format_time(duration))
        self._sync_fs_dock_labels()
        self._update_play_button()

    # -------------------------------------------------------------- player
    def _init_player(self):
        if vlc is None:
            log.error("未找到 python-vlc / libvlc: %s", VLC_IMPORT_ERROR)
            self._set_status("未找到 python-vlc / libvlc, 无法播放", "error")
            self._alert(
                "缺少 VLC",
                "无法加载 VLC 播放内核。\n\n"
                "请先安装 VLC 播放器(https://www.videolan.org/)，\n"
                "然后执行: pip install python-vlc",
            )
            return
        try:
            args = ["--no-video-title-show", "--quiet",
                    "--no-sub-autodetect-file"]
            mode = self.settings.get("decode_mode", "auto")
            if mode == "hardware":
                args.append("--avcodec-hw=any")
            elif mode == "software":
                args.append("--avcodec-hw=none")
            log.info("解码方式: %s", DECODE_LABELS.get(mode, mode))
            self.instance = vlc.Instance(*args)
            self.player = self.instance.media_player_new()
        except Exception as error:  # libvlc 初始化失败
            self.player = None
            log.exception("VLC 初始化失败")
            self._set_status(f"VLC 初始化失败: {error}", "error")
            self._alert("VLC 初始化失败", str(error))
            return

        dll_path = ""
        try:
            dll_path = str(getattr(getattr(vlc, "dll", None), "_name", "") or "")
        except Exception:
            dll_path = ""
        log.info("VLC 播放内核就绪: %s (%s)", vlc_version_text(),
                 dll_path or _VLC_HOME_DIR or "未知路径")
        self.player.set_rate(1.0)
        self._attach_video_output()

    def _attach_video_output(self):
        if self.player is None:
            return
        self.root.update_idletasks()
        hwnd = self.video_frame.winfo_id()
        try:
            if os.name == "nt":
                self.player.set_hwnd(hwnd)
            elif sys.platform == "darwin":
                self.player.set_nsobject(hwnd)
            else:
                self.player.set_xwindow(hwnd)
            self._hwnd_attached = True
            self._schedule_danmaku_sync()
            log.info("视频输出已绑定窗口句柄 0x%X", hwnd)
        except Exception as error:
            log.exception("视频输出绑定失败(句柄 0x%X)", hwnd)
            self._set_status(f"视频输出绑定失败: {error}", "error")

    def _enable_drag_and_drop(self):
        if TkinterDnD is None or DND_FILES is None:
            log.warning("tkinterdnd2 未安装, 拖放不可用")
            self._set_status("提示: 未安装 tkinterdnd2, 拖放不可用 (pip install tkinterdnd2)")
            return
        for widget in (self.root, self.video_frame, self.drop_hint):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except tk.TclError as error:
                log.warning("拖放注册失败: %s", error)
                self._set_status("提示: 拖放初始化失败, 请使用「打开文件」")
                return
        log.debug("拖放已启用")

    def _on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        log.info("拖入 %d 个文件", len(paths))
        for path in paths:
            path = path.strip("{}")
            if is_video_file(path):
                self.load_file(path)
                return
        log.warning("拖入的文件不受支持: %s", paths)
        self._set_status("拖入的文件不是支持的视频格式", "error")

    # --------------------------------------------------------------- files
    def _load_last_dir(self):
        if not self.settings.get("remember_dir", True):
            return os.path.expanduser("~")
        try:
            saved = load_config().get("last_dir")
        except Exception:
            saved = None
        if saved and os.path.isdir(saved):
            return saved
        return os.path.expanduser("~")

    def open_file_dialog(self):
        if self._client_active:
            self._alert("同步播放中", "正在同步主持的画面, 本地不能打开文件。\n请先退出会话。")
            return
        log.debug("打开文件选择框, 起始目录 %s", self._last_dir)
        start_dir = self._last_dir
        if not self.settings.get("remember_dir", True):
            start_dir = os.path.expanduser("~")
        dialog = OpenVideoDialog(self.root, start_dir)
        # 记住这次浏览到的目录(即使取消, 下次也停在这里)
        browsed = getattr(dialog, "_dir", None)
        if (browsed and os.path.isdir(browsed) and browsed != self._last_dir
                and self.settings.get("remember_dir", True)):
            self._last_dir = browsed
            save_config(last_dir=browsed)
        path = dialog.result
        if path:
            self.load_file(path)
        else:
            log.debug("文件选择已取消")

    # ---------------------------------------------------------------- 设置
    @staticmethod
    def _apply_log_level(level):
        try:
            log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        except (TypeError, ValueError):
            log.setLevel(logging.INFO)

    def _source_video_size(self):
        """当前视频的像素尺寸; 解码开始前可能拿不到, 返回 (0, 0)。"""
        if self.player is None:
            return 0, 0
        try:
            width, height = self.player.video_get_size(0)
        except Exception:
            return 0, 0
        if width and height:
            self._last_video_size = (int(width), int(height))
        return int(width or 0), int(height or 0)

    def _stream_scale(self):
        """把 480p/720p/1080p 换算成 transcode 的 scale(只缩小不放大)。"""
        target = RESOLUTION_HEIGHTS.get(self.settings.get("resolution"), 0)
        if not target:
            return 1.0
        _width, height = self._source_video_size()
        if not height:
            height = int(getattr(self, "_last_video_size", (0, 0))[1] or 0)
        if not height or height <= target:
            return 1.0
        return round(target / float(height), 4)

    def _sout_kwargs(self):
        settings = self.settings
        return {
            "bandwidth": settings["bandwidth"],
            "scale": self._stream_scale(),
            "fps": settings["fps"],
            "audio_bitrate": settings["audio_bitrate"],
            "preset": settings["preset"],
            "keyint": settings["keyint"],
        }

    def open_settings(self):
        self._settings_dialog = SettingsDialog(self.root, self.settings,
                                               self.apply_settings)
        self._track_dialog(self._settings_dialog)
        self._sync_danmaku()
        try:
            self._settings_dialog.present()
        finally:
            self._settings_dialog = None
            self._schedule_danmaku_sync()

    def apply_settings(self, settings):
        global ANIMATIONS_ENABLED
        self.settings = settings
        save_settings(settings)
        if hasattr(self, "buffering"):
            try:
                self.buffering.set_user_scale(settings.get("buffer_scale", 1.0))
            except Exception:
                log.debug("应用缓冲层缩放失败", exc_info=True)
        self.seek_step_ms = int(settings["seek_step"]) * 1000
        self._apply_log_level(settings.get("log_level", "INFO"))
        ANIMATIONS_ENABLED = bool(settings.get("animations", True))
        self._trim_chat_history()
        self.chat_panel._max_rows = self._chat_limit()
        if self._fs_chat_panel is not None:
            self._fs_chat_panel._max_rows = self._chat_limit()
        if self._hosting:
            try:
                self.chat_panel.set_messages(self._chat_messages, "host")
            except tk.TclError:
                pass
            try:
                if self._fs_chat_panel is not None:
                    self._fs_chat_panel.set_messages(self._chat_messages, "host")
            except tk.TclError:
                pass
        if self.danmaku is not None:
            color = DANMAKU_COLORS.get(
                settings.get("danmaku_color", "white"), ("", "#FFFFFF"))[1]
            self.danmaku.configure_style(
                settings.get("danmaku_size", 20),
                int(settings.get("danmaku_speed", 100)) / 100.0,
                color, settings.get("danmaku_area", 100))
            self._sync_danmaku(clear=True)
        restart_needed = (
            settings["decode_mode"] != self._active_decode_mode
            or settings["accent"] != self._active_accent
            or settings["surface"] != self._active_surface
        )
        if int(settings["volume"]) != self._active_volume:
            self._active_volume = int(settings["volume"])
            self.volume_var.set(self._active_volume)
            self.volume_label.configure(text=f"{self._active_volume}%")
            self._apply_volume()
        if settings.get("username") != self._active_username:
            self._active_username = settings.get("username", "")
            if self._hosting:
                self._broadcast_room()
                self._refresh_stream_button_tooltip()
            elif self._client_active:
                self._client_send({"v": STREAM_VERSION, "type": "rename",
                                   "name": self._active_username})
        if restart_needed:
            self._set_status("设置已保存 · 外观/解码设置重启后生效", "ok")
        else:
            self._set_status("设置已保存", "ok")
        log.info("设置已保存: %s", settings)

    def load_file(self, path):
        if self._client_active:
            log.warning("同步模式中忽略本地载入: %s", path)
            return
        if self.player is None:
            log.error("载入失败: VLC 内核未就绪 (%s)", path)
            self._alert("无法播放", "VLC 播放内核未就绪。")
            return
        if not is_video_file(path):
            log.warning("拒绝载入非视频文件: %s", path)
            self._alert("不支持的文件", f"不是支持的视频文件:\n{path}")
            return

        try:
            file_size = os.path.getsize(path)
        except OSError:
            file_size = 0
        log.info("载入媒体: %s (%s)", path, format_size(file_size) or "大小未知")

        # 换片时重建播放器: 避免上一条媒体的字幕轨/ES 选择残留,
        # 否则 VLC 可能把上一部 MKV 的内嵌字幕带到新视频上。
        if self.current_path is not None:
            previous = self.player
            self.player = None
            self._dispose_player_async(previous)
            self._create_player()
            if self.player is None:
                return

        # 主持中载入时把推流链一起带上(与旧版一致, 同步设置媒体)
        media = self.instance.media_new(path)
        self.media = media
        targets = tuple(self._stream_targets()) if self._hosting else ()
        sout_targets = self._sout_targets_now() if targets else ()
        self._sout_targets = sout_targets or None
        if sout_targets:
            media.add_option(f":sout={build_sout(sout_targets, **self._sout_kwargs())}")
            media.add_option(":sout-keep")
            media.add_option(f":rate={self._current_rate():g}")
            log.info("载入即推流: %d 个客户端 -> 可靠中转", len(targets))
        if not self._hwnd_attached:
            self._attach_video_output()
        self.player.set_media(media)

        self.current_path = path
        self._schedule_danmaku_sync()
        self._last_dir = os.path.dirname(os.path.abspath(path))
        if self.settings.get("remember_dir", True):
            save_config(last_dir=self._last_dir)
        self.duration_ms = 0
        self._duration_logged = False
        self._last_state = None
        self._volume_applied = False
        self._bump_stream_epoch("load")
        self.progress_var.set(0.0)
        self.time_label.configure(text="00:00")
        self.duration_label.configure(text="00:00")
        self.drop_hint.pack_forget()
        self.root.title(f"{os.path.basename(path)} — {APP_TITLE}")

        self._session_files += 1
        self.player.play()
        self.player.set_rate(self._current_rate())
        self.root.after(700, self._ensure_single_audio_track)
        self.root.after(900, self._update_track_button)
        if not self.settings.get("autoplay", True):
            self.player.pause()
            log.info("设置: 打开文件后不自动播放")
        self._set_status(f"正在播放 {os.path.basename(path)}", "playing")
        self._update_play_button()
        self._broadcast_state()

    def _current_rate(self):
        try:
            return float(self.speed_var.get().rstrip("x"))
        except ValueError:
            return 1.0

    # ------------------------------------------------------------ playback
    def toggle_play(self):
        if self._client_active:
            self._client_send_cmd("toggle")
            return
        if self.player is None:
            return
        if self.current_path is None:
            self.open_file_dialog()
            return
        restart_from_zero = False
        if self.player.is_playing():
            position = max(0, self.player.get_time())
            self.player.pause()
            log.info("暂停 @ %s", format_time(position))
            self._set_status("已暂停")
        else:
            state_before = self.player.get_state()
            restart_from_zero = state_before in (vlc.State.Stopped, vlc.State.Ended,
                                                 getattr(vlc.State, "NothingSpecial", None))
            # libvlc 在 Ended 状态下 play() 无效, 必须先 stop 再 play 才能从头重播
            if state_before in (vlc.State.Ended, vlc.State.Stopped):
                try:
                    self.player.stop()
                except Exception:
                    log.exception("结束后重新播放失败")
            self.player.play()
            if restart_from_zero:
                self._bump_stream_epoch("resume-from-stopped")
            log.info("继续播放")
            if self.current_path:
                self._set_status(f"正在播放 {os.path.basename(self.current_path)}", "playing")
        self._update_play_button()
        self._mirror_streamer_state()
        self._broadcast_state()

    def stop(self):
        if self._client_active:
            self._client_send_cmd("stop")
            return
        if self.player is None:
            return
        self.player.stop()
        log.info("停止播放: %s", self.current_path or "-")
        self._bump_stream_epoch("stop")
        self.progress_var.set(0.0)
        self.time_label.configure(text="00:00")
        self.duration_label.configure(text="00:00")
        self._update_play_button()
        self._set_status("已停止")
        self._mirror_streamer_state()
        self._broadcast_state()

    def seek_relative(self, delta_ms):
        if self._client_active:
            self._client_send_cmd("seek_rel", int(delta_ms))
            return
        if self.player is None or self.current_path is None:
            return
        current = self.player.get_time()
        if current < 0:
            current = 0
        target = current + delta_ms
        if self.duration_ms:
            target = min(target, self.duration_ms - 500)
        target = max(0, int(target))
        if self._hosting and self._stream_targets():
            self._schedule_stream_seek(target)
            log.info("相对跳转(推流重建) %+.1f 秒: %s -> %s",
                     delta_ms / 1000.0, format_time(current), format_time(target))
            return
        self._kill_orphan_stream()
        self.player.set_time(target)
        self._seek_hint = (target, time.monotonic())
        self._bump_stream_epoch("seek-rel")
        log.info("相对跳转 %+.1f 秒: %s -> %s",
                 delta_ms / 1000.0, format_time(current), format_time(target))
        self._broadcast_state()

    def change_volume(self, delta):
        value = max(0, min(100, self.volume_var.get() + delta))
        self.volume_var.set(value)
        self._on_volume_change(value)
        log.info("音量 -> %d%%", value)

    def _apply_volume(self):
        """libvlc 在未开始播放时设置音量会失败, 因此播放开始后再应用。"""
        if self.player is None:
            return
        self.player.audio_set_volume(self.volume_var.get())
        self.player.audio_set_mute(self.mute_var.get())
        self._volume_applied = True

    def _on_volume_change(self, value):
        volume = int(float(value))
        self.volume_label.configure(text=f"{volume}%")
        if self._client_active:
            # 音量只作用在本端播放器上(主持端的音量不经过转码链)
            if volume > 0 and self.mute_var.get():
                self.mute_var.set(False)
                self._refresh_mute_icon()
            if self.player is not None:
                self.player.audio_set_volume(volume)
                self.player.audio_set_mute(self.mute_var.get())
            return
        if volume > 0 and self.mute_var.get():
            self.mute_var.set(False)
            self._refresh_mute_icon()
        if volume != self._logged_volume:
            self._logged_volume = volume
            log.debug("音量 -> %d%%", volume)
        self._apply_volume()
        self._broadcast_state()

    def _on_mute_click(self):
        self.mute_var.set(not self.mute_var.get())
        self._on_mute_toggle()

    def _on_mute_toggle(self):
        if self._client_active:
            self._refresh_mute_icon()
            if self.player is not None:
                self.player.audio_set_volume(self.volume_var.get())
                self.player.audio_set_mute(self.mute_var.get())
            return
        if self.mute_var.get():
            self._volume_before_mute = self.volume_var.get() or 80
        else:
            self.volume_var.set(self._volume_before_mute)
            self.volume_label.configure(text=f"{self._volume_before_mute}%")
        log.info("静音: %s (音量 %d%%)", "开" if self.mute_var.get() else "关",
                 self.volume_var.get())
        self._refresh_mute_icon()
        self._apply_volume()
        self._broadcast_state()

    def _refresh_mute_icon(self):
        muted = self.mute_var.get()
        self.mute_button.set_icon(
            lambda cv, cx, cy, s, col: icon_speaker(cv, cx, cy, s, col, muted)
        )
        tooltip = getattr(self.mute_button, "_tooltip", None)
        if tooltip is not None:
            tooltip.text = "取消静音" if muted else "静音"

    def _on_speed_change(self, _event=None):
        rate = self._current_rate()
        if self._client_active:
            self._client_send_cmd("rate", rate)
            self._set_status(f"已请求播放速度 {rate:g}x")
            return
        if self.player is None:
            return
        if self._hosting and self._stream_targets():
            # 推流中 set_rate 不生效(转码链按 1x 跑), 需要在当前位置按新速度重建推流
            self._schedule_stream_rate()
            log.info("推流速度切换 -> %gx, 重建推流", rate)
            self._set_status(f"已切换速度 {rate:g}x · 正在重建推流…", "playing")
            return
        self.player.set_rate(rate)
        log.info("播放速度 -> %gx", rate)
        self._set_status(f"播放速度 {rate:g}x")
        self._mirror_streamer_state()
        self._broadcast_state()

    def _on_seek_start(self):
        self._seeking = True

    def _on_progress_drag(self, value):
        if self._client_active:
            state = self._client_state
            if not state or not state.get("duration"):
                return
            target = int(float(value) / 1000.0 * state["duration"])
            self.time_label.configure(text=format_time(target))
            return
        if not self.duration_ms:
            return
        target = int(float(value) / 1000.0 * self.duration_ms)
        self.time_label.configure(text=format_time(target))

    def _on_seek_end(self, value):
        if self._client_active:
            state = self._client_state
            if state and state.get("duration"):
                target = int(float(value) / 1000.0 * state["duration"])
                self._client_send_cmd("seek", target)
                state["pos"] = target
                self._client_state_ts = time.time()
            self._seeking = False
            return
        if self.player is not None and self.duration_ms:
            target = int(float(value) / 1000.0 * self.duration_ms)
            target = max(0, min(target, self.duration_ms - 200))
            if self._hosting and self._stream_targets():
                self._schedule_stream_seek(target)
                self._seeking = False
                return
            self._kill_orphan_stream()
            self.player.set_time(target)
            self.time_label.configure(text=format_time(target))
            self._seek_hint = (target, time.monotonic())
            self._bump_stream_epoch("seek")
            log.info("进度跳转 -> %s / %s (%d ms)", format_time(target),
                     format_time(self.duration_ms), target)
            self._broadcast_state()
        self._seeking = False

    def _update_play_button(self):
        if self._client_active:
            playing = bool(self._client_state and self._client_state.get("playing"))
            self.play_button.set_icon(icon_pause if playing else icon_play)
            if self.fs_play_button is not None:
                self.fs_play_button.set_icon(icon_pause if playing else icon_play)
            return
        playing = self.player is not None and self.player.is_playing()
        self.play_button.set_icon(icon_pause if playing else icon_play)
        if self.fs_play_button is not None:
            self.fs_play_button.set_icon(icon_pause if playing else icon_play)

    # ------------------------------------------------------------ fullscreen
    def toggle_fullscreen(self):
        if self._fullscreen:
            self.exit_fullscreen()
            return
        self._saved_geometry = self.root.geometry()
        self._fullscreen = True
        self.deck.pack_forget()
        self.root.attributes("-fullscreen", True)
        self._sync_chat_panel()
        self.root.after(150, self._sync_danmaku)
        self.fullscreen_button.set_icon(icon_restore)
        if self._fs_dock is not None:
            try:
                self._fs_dock.withdraw()
            except tk.TclError:
                pass
        self._fs_dock_visible = False
        if self._fs_chat is not None:
            try:
                self._fs_chat.withdraw()
            except tk.TclError:
                pass
        self._fs_chat_visible = False
        self._fs_chat_rect = None
        self._start_fs_watch()
        self.root.after(60, lambda: apply_dark_titlebar(self.root))
        log.info("进入全屏 (恢复尺寸 %s)", self._saved_geometry)

    def exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        self._stop_fs_watch()
        if self._fs_dock is not None:
            try:
                self._fs_dock.withdraw()
            except tk.TclError:
                pass
        self._fs_dock_visible = False
        if self._fs_chat is not None:
            try:
                self._fs_chat.withdraw()
            except tk.TclError:
                pass
        self._fs_chat_visible = False
        self._fs_chat_rect = None
        self.root.attributes("-fullscreen", False)
        if self._saved_geometry:
            self.root.geometry(self._saved_geometry)
        # 只把控制栏插回视频区之前, 不重建视频区, 避免打扰 VLC 的输出窗口
        self.deck.pack(side=tk.BOTTOM, fill=tk.X, before=self.content)
        self._sync_chat_panel()
        self.root.after(150, self._sync_danmaku)
        self.fullscreen_button.set_icon(icon_fullscreen)
        self.root.after(60, lambda: apply_dark_titlebar(self.root))
        log.info("退出全屏 (尺寸 %s)", self.root.geometry())

    # ---------------------------------------------------------------- tick
    def _schedule_tick(self):
        self._tick_job = self.root.after(UI_TICK_MS, self._tick)

    def _tick(self):
        try:
            self._ui_beat = time.monotonic()
            self._drain_network()
            if self._client_active:
                self._client_retry_reconnect()
            self._refresh_state()
            now = time.monotonic()
            if now - getattr(self, "_last_track_check", 0.0) >= 1.0:
                self._last_track_check = now
                self._update_track_button()
        finally:
            self._schedule_tick()

    def _refresh_state(self):
        if self._client_active:
            self._refresh_client_state()
            return
        if self.player is None or self.current_path is None:
            return
        if not self._volume_applied and self.player.is_playing():
            self._apply_volume()
        length = self.player.get_length()
        if length and length > 0:
            self.duration_ms = length
            if not self._duration_logged:
                self._duration_logged = True
                self._log_media_details()
        current = self.player.get_time()
        if current < 0:
            current = 0

        if not self._seeking and self.duration_ms > 0:
            self.progress.set(min(1000.0, current / self.duration_ms * 1000.0))
        if not self._seeking:
            self.time_label.configure(text=format_time(current))
        self.duration_label.configure(text=format_time(self.duration_ms))
        self._sync_fs_dock_labels()
        self._update_play_button()

        state = self.player.get_state()
        previous_state = self._last_state
        if state != previous_state:
            log.debug("播放状态 %s -> %s @ %s",
                      vlc_state_text(previous_state), vlc_state_text(state),
                      format_time(current))
            self._last_state = state
        if state == vlc.State.Ended and previous_state != vlc.State.Ended:
            self.progress_var.set(0.0)
            self.time_label.configure(text="00:00")
            log.info("播放结束: %s", self.current_path)
            self._set_status("播放结束")
            self._broadcast_state()
        elif state == vlc.State.Error and previous_state != vlc.State.Error:
            log.error("播放出错: %s (VLC 状态 Error)", self.current_path)
            self._set_status("播放出错, 文件可能损坏或格式不受支持", "error")
            self._broadcast_state()

        # 主持端周期推送进度(客户端本地按倍率插值)
        if self._hosting and self.player.is_playing():
            now = time.monotonic()
            if now - self._last_state_push >= 1.0:
                self._last_state_push = now
                self._broadcast_state()

    def _ensure_single_audio_track(self):
        """多音轨媒体只保留第一条音轨, 避免出现两条音轨叠着响。"""
        if self.player is None or self._client_active:
            return
        try:
            count = self.player.audio_get_track_count()
        except Exception:
            return
        if count <= 1:
            return
        try:
            desc = self.player.audio_get_track_description() or []
            tracks = [entry for entry in desc if entry and entry[0] > 0]
            if not tracks:
                return
            current = self.player.audio_get_track()
            if current != tracks[0][0]:
                self.player.audio_set_track(tracks[0][0])
                log.info("多音轨媒体: 已固定第一条音轨 %s", tracks[0][1])
        except Exception:
            log.debug("选择音轨失败", exc_info=True)

    def _track_display_name(self, name):
        if isinstance(name, bytes):
            name = name.decode("utf-8", "ignore")
        name = str(name or "").strip()
        return name or "默认"

    def _audio_track_entries(self):
        if self.player is None:
            return []
        try:
            desc = self.player.audio_get_track_description() or []
        except Exception:
            return []
        entries = []
        for tid, name in desc:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                continue
            if tid > 0:
                entries.append((tid, self._track_display_name(name)))
        return entries

    def _subtitle_entries(self):
        if self.player is None:
            return []
        try:
            desc = self.player.video_get_spu_description() or []
        except Exception:
            return []
        entries = []
        for tid, name in desc:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                continue
            if tid > 0:
                entries.append((tid, self._track_display_name(name)))
        return entries

    def _update_track_button(self):
        if self.player is None or (self.current_path is None and not self._client_active):
            self._hide_track_button()
            return
        show = len(self._audio_track_entries()) > 1 or len(self._subtitle_entries()) > 0
        try:
            mapped = bool(self.track_button.winfo_ismapped())
        except tk.TclError:
            mapped = False
        if show and not mapped:
            try:
                self.track_button.pack(side=tk.LEFT, padx=(10, 0),
                                       before=self.back_button)
            except tk.TclError:
                pass
        elif not show and mapped:
            self.track_button.pack_forget()

    def _hide_track_button(self):
        try:
            if self.track_button.winfo_ismapped():
                self.track_button.pack_forget()
        except tk.TclError:
            pass

    def open_track_menu(self):
        if self.player is None or (self.current_path is None and not self._client_active):
            return
        audio = self._audio_track_entries()
        subs = self._subtitle_entries()
        if not audio and not subs:
            return
        menu = tk.Menu(self.root, tearoff=0, bg=C.SURFACE, fg=C.TEXT,
                       activebackground=C.ACCENT, activeforeground="#0B0F1A",
                       relief=tk.FLAT, bd=0, font=ui_font(10))
        try:
            if audio:
                menu.add_command(label="音频", state=tk.DISABLED)
                try:
                    current = int(self.player.audio_get_track())
                except Exception:
                    current = -1
                for tid, name in audio:
                    mark = "✓ " if tid == current else "   "
                    menu.add_command(label=mark + name,
                                     command=lambda t=tid: self._set_audio_track(t))
            if subs:
                if audio:
                    menu.add_separator()
                menu.add_command(label="字幕", state=tk.DISABLED)
                try:
                    current = int(self.player.video_get_spu())
                except Exception:
                    current = -1
                mark = "✓ " if current < 0 else "   "
                menu.add_command(label=mark + "关闭字幕",
                                 command=lambda: self._set_subtitle(-1))
                for tid, name in subs:
                    mark = "✓ " if tid == current else "   "
                    menu.add_command(label=mark + name,
                                     command=lambda t=tid: self._set_subtitle(t))
            x = self.track_button.winfo_rootx()
            y = self.track_button.winfo_rooty() + self.track_button.winfo_height()
            menu.tk_popup(x, y)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
            self.root.after(50, menu.destroy)

    def _set_audio_track(self, tid):
        try:
            self.player.audio_set_track(int(tid))
            log.info("切换音轨: %s", tid)
            self._set_status("已切换音轨", "ok")
        except Exception:
            log.exception("切换音轨失败")
        self._update_track_button()

    def _set_subtitle(self, tid):
        try:
            self.player.video_set_spu(int(tid))
            log.info("切换字幕: %s", tid)
            self._set_status("已关闭字幕" if int(tid) < 0 else "已切换字幕", "ok")
        except Exception:
            log.exception("切换字幕失败")
        self._update_track_button()

    def _log_media_details(self):
        width = height = 0
        try:
            width, height = self.player.video_get_size(0)
        except Exception:
            pass
        tracks = 0
        try:
            tracks = self.player.audio_get_track_count()
        except Exception:
            pass
        log.info(
            "媒体信息: 时长 %s | 画面 %dx%d | 音轨 %d | 速度 %sx",
            format_time(self.duration_ms), width, height, tracks, self._current_rate(),
        )

    def _set_status(self, text, level="idle"):
        colors = {"idle": C.TEXT_DIM, "playing": C.ACCENT, "error": C.DANGER, "ok": C.OK}
        self.status_label.configure(text=text)
        self.status_dot.delete("all")
        dot = _circle_photo(self.status_dot, 7, colors.get(level, C.TEXT_DIM))
        self.status_dot.create_image(5 - dot.width() / 2.0, 5 - dot.height() / 2.0,
                                     anchor="nw", image=dot)

    def _alert(self, title, message, kind="info"):
        log.debug("提示框[%s]: %s | %s", kind, title, message.replace("\n", " / "))
        self._alert_open = True
        self._sync_danmaku()
        try:
            Alert(self.root, title, message, kind)
        finally:
            self._alert_open = False
            self._schedule_danmaku_sync()

    # --------------------------------------------------------------- close
    def _exit_watchdog(self):
        """libvlc 偶发在 stop/release 时卡死, 兜底保证进程能退出。"""
        for _ in range(30):  # 最多等 3 秒
            if self._closed:
                return
            time.sleep(0.1)
        log.warning("VLC 释放超时, 强制结束进程")
        logging.shutdown()
        os._exit(0)

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        log.info("关闭播放器")
        threading.Thread(target=self._exit_watchdog, daemon=True).start()

        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None
        # 先结束流式会话
        try:
            if self.danmaku is not None:
                self.danmaku.hide()
        except tk.TclError:
            pass
        self._close_member_dialog()
        self._close_password_dialog()
        try:
            if self._hosting:
                self.stop_hosting()
        except Exception:
            log.exception("停止主持失败")
        try:
            if self._client_active:
                self.leave_stream()
        except Exception:
            log.exception("退出会话失败")

        # 先关窗口, 用户立刻看到程序退出; VLC 的回收放到后台线程,
        # 避免 libvlc 偶发的 stop/release 阻塞把关闭过程卡住。
        try:
            self.root.destroy()
        except tk.TclError:
            pass

        streamer = self._streamer_player
        streamer_instance = self._streamer_instance
        self._streamer_player = None
        self._streamer_instance = None
        http_server = self._relay_http
        self._relay_http = None
        player = self.player
        instance = self.instance
        self.player = None
        self.instance = None

        def cleanup():
            for item in (streamer, player):
                if item is None:
                    continue
                try:
                    item.stop()
                except Exception:
                    log.debug("后台停止播放器失败", exc_info=True)
                try:
                    item.release()
                except Exception:
                    log.debug("后台释放播放器失败", exc_info=True)
            for inst in (streamer_instance, instance):
                if inst is None:
                    continue
                try:
                    inst.release()
                except Exception:
                    log.debug("后台释放实例失败", exc_info=True)
            if http_server is not None:
                try:
                    http_server.shutdown()
                except Exception:
                    pass
            log.info("VLC 资源已在后台回收")
            self._closed = True

        threading.Thread(target=cleanup, daemon=True, name="vlc-cleanup").start()
        uptime = time.monotonic() - getattr(self, "_started_at", time.monotonic())
        log.info("会话结束: 运行 %.1f 秒, 播放 %d 个文件",
                 uptime, getattr(self, "_session_files", 0))


def create_root():
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except Exception:
            log.warning("TkinterDnD 初始化失败, 回退到普通 Tk", exc_info=True)
    return tk.Tk()


def main():
    setup_logging()
    install_excepthooks()
    log.info("进入主循环")
    try:
        root = create_root()
        VideoPlayerApp(root)
        root.mainloop()
    except Exception:
        log.exception("主程序异常退出")
        raise
    finally:
        log.info("主循环结束")


if __name__ == "__main__":
    main()
