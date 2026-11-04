"""core 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

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
  * 主持: 开放 TCP 指令端口(默认 5000)与 UDP 媒体端口(默认 5001);
    客户端先经指令端口同步身份与播放状态(文件/进度/总长/暂停/倍率)
  * 媒体流: 主持端 VLC 以 H.264/TS 送到本机 UDP 中转, 再向已注册客户端
    原包转发(不解析/不重排/不补包); 客户端发一个很小的 UDP 注册包即开始接收,
    抖动与丢包由客户端 VLC 的 network-caching 缓冲吸收, 应用层不做重传
  * 加入: 输入指令地址与主持流地址(如 192.168.1.10:5000 / 192.168.1.10:5001),
    客户端向主持的媒体端口注册接收地址, 本机不开放入站端口; 播放/暂停/倍速等控制
    只把指令发回主持端执行, 主持处理后再把新状态同步过来; 音量/静音属于本机
  * 推流方式: 主持端播放器直接挂 sout 送到本机中转; 客户端进出时按当前进度
    重建推流链, 全部离开后保持本地播放(残留推流稍后剥离)
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

import base64
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
import re
import shutil
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
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler

APP_TITLE = "Nobplaypot"
APP_VERSION = "v1.3.0"
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
    "language": "zh",           # zh / en
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
    "app_input_mode": "message",   # message(不抢鼠标) / sendinput(更兼容)
    "app_share_transport": "frames",  # frames(直传, 低延迟) / vlc(旧的 ffmpeg+VLC)
    "app_frame_fps": 30,             # 直传帧率(30 起步, 可到 60)
    "app_frame_codec": "jpeg",       # jpeg(默认, 快) / png(无损但慢)
    "app_frame_quality": 70,         # JPEG 质量 20~95
    "app_frame_max_width": 2560,     # 直传最大抓取宽度(放大到 200% 时也不裁画面)
    "app_capture_mode": "printwindow",  # printwindow(被遮挡也能抓) / screen(旧的屏幕 BitBlt)
    "app_zoom": 1.0,
    # 浏览器同步
    "web_sync_interval_ms": 200,    # 主持端轮询网页状态的间隔
    "web_sync_tolerance": 0.75,     # 允许的进度偏差(秒), 超出才强制 seek
    "web_start_url": "https://www.bing.com",
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
    "app_frame_fps": (5, 60),
    "app_frame_quality": (20, 95),
    "app_frame_max_width": (320, 2560),
    "web_sync_interval_ms": (100, 1000),
    "web_sync_tolerance": (0.2, 2.0),
    "app_zoom": (0.25, 2.0),
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

# ---------------------------------------------------------------------------
# 界面语言: 中文 / English
# 说明: 设置里切换后立即生效; 通过遍历控件把中文文本替换成英文。
# 新增 UI 文案时, 把中文原文加到 I18N 里即可被自动翻译。
# ---------------------------------------------------------------------------
LANG = "zh"

I18N = {
    # 主窗口 / 控制栏
    "打开文件": "Open File",
    "房间成员": "Room Members",
    "音轨 / 字幕": "Audio / Subtitles",
    "流式传输 (主持 / 加入)": "Stream (Host / Join)",
    "查看房间成员": "View room members",
    "打开视频文件 (Ctrl+O)": "Open video file (Ctrl+O)",
    "后退 (←)": "Back (←)",
    "播放 / 暂停 (空格)": "Play / Pause (Space)",
    "快进 (→)": "Forward (→)",
    "停止": "Stop",
    "全屏 (F)": "Fullscreen (F)",
    "设置": "Settings",
    "切换音轨 / 字幕": "Switch audio / subtitles",
    # 空状态
    "把视频文件拖到这里，或选择本地文件": "Drag a video here, or choose a local file",
    "选择视频文件": "Choose Video",
    # 聊天
    "房间聊天": "Room Chat",
    "发送": "Send",
    # 提示框
    "知道了": "OK",
    "关闭": "Close",
    "取消": "Cancel",
    "保存": "Save",
    "确定": "OK",
    # 设置: 选项卡 / 页面
    "常规": "General",
    "外观": "Appearance",
    "聊天室": "Chat",
    "流式传输": "Streaming",
    "解码方式与应用行为。": "Decode mode and application behavior.",
    "调整强调色与界面底色; 改动需要重启软件后生效。": "Accent colour and surface tone; changes take effect after restart.",
    "用户名、聊天记录与弹幕显示。": "Username, chat history and danmaku display.",
    "推流参数、端口与客户端缓冲。": "Streaming parameters, ports and client buffering.",
    "界面语言 / Language": "Interface language",
    "保存后生效（重启后仍然生效）": "Takes effect after Save (persists after restart)",
    "中文": "中文",
    "English": "English",
    # 设置: 常规
    "解码方式": "Decode mode",
    "硬件解码更省 CPU; 切换后需要重启软件生效。（重启生效）": "Hardware decoding saves CPU; takes effect after restart.",
    "默认音量": "Default volume",
    "打开软件时的初始音量": "Initial volume when the app starts",
    "快进/快退步长": "Seek step",
    "方向键与快进按钮的跳转秒数": "Seconds per arrow-key / skip-button jump",
    "打开文件后自动播放": "Autoplay on open",
    "记住上次打开的目录": "Remember last folder",
    "文件选择框从上次浏览的位置开始": "The file picker starts from the last folder",
    "日志级别": "Log level",
    "排查问题时可以调到 DEBUG": "Set to DEBUG when troubleshooting",
    # 设置: 外观
    "强调色": "Accent colour",
    "按钮、进度条与高亮使用的颜色（重启生效）": "Colour used by buttons, progress bar and highlights (restart)",
    "界面底色": "Surface tone",
    "控制栏、卡片与输入框的底色（重启生效）": "Background of deck, cards and inputs (restart)",
    "悬停动画": "Hover animation",
    "关闭后按钮与滑块的悬停变化直接切换": "When off, buttons and sliders switch instantly on hover",
    "缓冲提示缩放": "Buffering hint scale",
    "视频中央「缓冲中」动画的大小倍数": "Size multiplier of the central buffering animation",
    # 设置: 聊天
    "用户名": "Username",
    "加入房间时显示的名称（4–32 个字符）": "Name shown when joining a room (4–32 characters)",
    "成员消息频率上限": "Member message rate limit",
    "每名成员每分钟最多发送的聊天消息数": "Max chat messages per member per minute",
    "聊天记录上限": "Chat history limit",
    "最多保留的聊天消息条数": "Maximum number of chat messages kept",
    "弹幕": "Danmaku",
    "消息弹幕": "Message danmaku",
    "把新聊天消息以弹幕形式飘过视频": "Float new chat messages across the video as danmaku",
    "弹幕字号": "Danmaku font size",
    "弹幕速度": "Danmaku speed",
    "弹幕颜色": "Danmaku colour",
    "弹幕显示区域": "Danmaku area",
    # 设置: 流式传输
    "最大客户端数": "Max clients",
    "房间人数上限": "Max room members",
    "启用密码时需要填写房间密码": "A password is required when enabled",
    "主持: 开放指令/流端口, 把当前播放推给其他设备\n加入: 接收并渲染主持推来的画面(播放控制交给主持执行)":
        "Host: opens the command/media ports and pushes the current playback\nJoin: receives and renders the host's stream (playback is controlled by the host)",
    "同时允许加入的房客数量": "Number of guests that may join at once",
    "带宽上限": "Bandwidth limit",
    "视频码率上限 (kbps)": "Video bitrate cap (kbps)",
    "分辨率": "Resolution",
    "转码输出分辨率; 1x 流复制不受影响": "Transcode output resolution; 1x stream copy is unaffected",
    "帧率": "Frame rate",
    "转码输出帧率": "Transcode output frame rate",
    "音频码率": "Audio bitrate",
    "x264 预设": "x264 preset",
    "越快 CPU 占用越低, 画质略降": "Faster presets use less CPU with slightly lower quality",
    "关键帧间隔": "Keyframe interval",
    "数值越小, 客户端起播与跳转越快": "Smaller values start and seek faster on the client",
    "客户端缓冲": "Client buffer",
    "接收端缓冲, 网络抖动大时可调高": "Receive buffer; raise it when the network jitters",
    "心跳间隔": "Heartbeat interval",
    "指令端口心跳包间隔": "Heartbeat interval on the command channel",
    "离线判定": "Offline timeout",
    "超过这个时间没有心跳就判定客户端离线": "A client without a heartbeat for this long is considered offline",
    "默认指令端口": "Default command port",
    "主持/加入对话框的默认值": "Default value in the host / join dialog",
    "默认媒体端口": "Default media port",
    "UDP 媒体流的默认端口": "Default port of the UDP media stream",
    "端口必须是数字": "Ports must be numbers",
    "用户名长度需要 4–32 个字符": "Username must be 4–32 characters",
    "指令端口和流端口不能相同": "Command port and media port must differ",
    # 流式传输入口
    "主持": "Host",
    "停止主持": "Stop Hosting",
    "已停止主持": "Hosting stopped",
    "无法加入": "Cannot join",
    "当前正在主持会话, 请先停止主持。": "You are already hosting a session. Stop it first.",
    "加入": "Join",
    "开始主持": "Start Hosting",
    "加入房间": "Join Room",
    "指令端口": "Command port",
    "媒体端口（主持监听）": "Media port (host listen)",
    "启用房间密码": "Enable room password",
    "媒体端口": "Media port",
    "房间密码": "Room password",
    "第三方应用同步": "Third-party app sync",
    "应用共享": "App share",
    "共享一个程序窗口, 房客可以直接操作它":
        "Share one app window; guests can operate it directly",
    "选择一个窗口共享给房客, 画面与操作都会实时同步:":
        "Pick a window to share; both the picture and the input are mirrored in real time:",
    "没有找到可共享的窗口": "No shareable window found",
    "画质": "Quality",
    "允许房客操作这个窗口": "Let guests operate this window",
    "关闭时房客只能观看; 受保护内容可能无法采集。":
        "When off, guests can only watch; protected content may not be captured.",
    "共享期间请保持该窗口在前台可见, 最小化或被遮挡会导致画面异常。":
        "Keep that window visible while sharing; minimising or covering it breaks the picture.",
    "请先选择一个窗口": "Select a window first",
    "开始共享": "Start sharing",
    "缺少组件": "Missing component",
    "应用共享需要 ffmpeg.exe。\n请把它放到程序目录(与 Nobplaypot.exe 同级)后重试。":
        "App sharing needs ffmpeg.exe.\nPut it next to Nobplaypot.exe and try again.",
    "应用共享需要 ffmpeg.exe。\n请把它放到程序目录的 ffmpeg-runtime\\ 下(与 vlc-runtime 同级)后重试。":
        "App sharing needs ffmpeg.exe.\nPut it in the ffmpeg-runtime\\ folder next to vlc-runtime and try again.",
    "无法开始共享": "Cannot start sharing",
    "请先结束当前会话。": "End the current session first.",
    "媒体中转未启动, 请检查流端口是否被占用。":
        "Media relay did not start; check whether the media port is in use.",
    "采集进程启动失败:\n": "Failed to start the capture process:\n",
    "低 (960x540 · 20fps · 1.5Mbps)": "Low (960x540 · 20fps · 1.5Mbps)",
    "标准 (1280x720 · 30fps · 3Mbps)": "Standard (1280x720 · 30fps · 3Mbps)",
    "高 (1920x1080 · 30fps · 6Mbps)": "High (1920x1080 · 30fps · 6Mbps)",
    "主持流式传输": "Host Streaming",
    "加入流式传输": "Join Streaming",
    "选择这次要主持的内容类型:": "Choose what you want to host:",
    "视频媒体": "Video file",
    "浏览器同步": "Browser sync",
    "把本地视频文件推流给其他设备, 支持暂停/进度/倍速同步":
        "Stream a local video to other devices, with pause / seek / speed sync",
    "不推流, 只同步网页地址与播放进度":
        "No media streaming; syncs the page URL and playback position",
    "只同步网页地址与播放进度, 不推流; 仅需开放 TCP 指令端口。":
        "Syncs the page URL and playback position only; no media streaming. Needs the TCP command port.",
    "开始同步": "Start syncing",
    "端口必须是 1-65535 之间的数字": "Port must be a number between 1 and 65535",
    "指令端口和流端口不能相同": "Command port and media port must differ",
    "一台设备主持播放并推流, 其他设备加入后同步观看与遥控。":
        "One device hosts and streams; others join to watch and control together.",
    "开放 TCP 指令端口与 UDP 媒体端口; 客户端向媒体端口注册后, 主持端把转码流原包转发给它。":
        "Opens the TCP command port and the UDP media port; clients register on the media port and the host relays the transcoded stream.",
    "客户端加入时由主持端核验": "Verified by the host when a client joins",
    "密码只保存在主持端, 不会随房间信息发给客户端。":
        "The password is kept on the host and never sent to clients.",
    "填写主持的指令地址, 以及主持的媒体流地址\n(客户端主动连接主持的媒体端口, 本机无需开放任何入站端口)。":
        "Enter the host's command address and media stream address\n(clients connect out to the host, no inbound port needed).",
    "指令地址": "Command address",
    "主持流地址": "Media address",
    "加入会话": "Join Session",
    "还没有消息，打个招呼吧": "No messages yet. Say hi!",
    "主持可移除房客": "The host can remove guests",
    "仅主持可移除成员": "Only the host can remove members",
    "启用房间密码": "Enable room password",
    "密码": "Password",
    "房客加入时需要输入密码": "Guests must enter this password to join",
    "主持端会显示本机地址": "The host shows the local address",
    "正在连接...": "Connecting...",
    "已连接": "Connected",
    "已断开": "Disconnected",
    "连接断开": "Disconnected",
    "就绪": "Ready",
    "播放结束": "Playback finished",
    "播放出错, 文件可能损坏或格式不受支持": "Playback error: the file may be corrupted or unsupported",
    "缓冲中…": "Buffering…",
    "缓冲中": "Buffering",
    "自动": "Auto",
    "硬件解码": "Hardware",
    "软件解码": "Software",
    "恢复默认": "Reset to defaults",
    "空格 播放/暂停    ← → 快退/快进    F 全屏    Ctrl+L 日志":
        "Space Play/Pause    ← → Seek    F Fullscreen    Ctrl+L Log",
    "浏览器同步（预览）": "Browser Sync (preview)",
    "只同步网页地址与播放进度, 不推媒体流; 仅需开放 TCP 指令端口。":
        "Syncs the page URL and playback position only; no media streaming, and only the TCP command port is needed.",
    "主持: 选择视频媒体或浏览器, 开放指令/流端口\n加入: 接收并渲染主持推来的画面(播放控制交给主持执行)":
        "Host: pick a video file or the browser, then open the command/media ports\nJoin: receive and render the host's stream (playback control stays with the host)",
    "浏览器同步: 不推流, 只同步网页地址与播放进度":
        "Browser sync: no media push, only the page URL and playback position",
    "浏览器同步已开始 · 在浏览器窗口里打开视频页面":
        "Browser sync started · open a video page in the browser window",
    "已加入浏览器同步房间 · 等待主持端操作":
        "Joined browser sync room · waiting for the host",
    "未找到 Edge": "Edge not found",
    "浏览器同步模式需要系统安装 Microsoft Edge。":
        "Browser sync needs Microsoft Edge installed.",
    "浏览器启动失败": "Failed to start the browser",
    # 设置: 分页与页面标题
    "常规": "General",
    "外观": "Appearance",
    "聊天室": "Chat",
    "解码方式与应用行为。": "Decode mode and application behavior.",
    "调整强调色与界面底色; 改动需要重启软件后生效。":
        "Adjust the accent colour and surface; restarts required.",
    "用户名、聊天记录与弹幕显示。": "Username, chat history and danmaku display.",
    "推流参数、端口与客户端缓冲。": "Stream parameters, ports and client buffer.",
    # 常规
    "解码方式": "Decode mode",
    "硬件解码更省 CPU; 切换后需要重启软件生效。（重启生效）":
        "Hardware decoding saves CPU; takes effect after restart.",
    "默认音量": "Default volume",
    "打开软件时的初始音量": "Initial volume when the app starts",
    "快进/快退步长": "Seek step",
    "方向键与快进按钮的跳转秒数": "Seconds per arrow-key / skip-button jump",
    "打开文件后自动播放": "Autoplay on open",
    "记住上次打开的目录": "Remember last folder",
    "文件选择框从上次浏览的位置开始": "The file picker starts from the last folder",
    "日志级别": "Log level",
    "排查问题时可以调到 DEBUG": "Set to DEBUG when troubleshooting",
    # 外观
    "强调色": "Accent colour",
    "按钮、进度条与高亮使用的颜色（重启生效）":
        "Colour for buttons, progress and highlights (restart required)",
    "界面底色": "Surface",
    "控制栏、卡片与输入框的底色（重启生效）":
        "Background of the deck, cards and inputs (restart required)",
    "悬停动画": "Hover animation",
    "关闭后按钮与滑块的悬停变化直接切换":
        "Disabling makes hover changes switch instantly",
    "缓冲提示缩放": "Buffering indicator scale",
    "视频中央「缓冲中」动画的大小倍数":
        "Size multiplier of the centre “Buffering” animation",
    "极光蓝": "Aurora",
    "紫罗兰": "Violet",
    "青碧": "Teal",
    "玫瑰": "Rose",
    "琥珀": "Amber",
    "深蓝黑": "Deep Navy",
    "纯黑": "Pure Black",
    "岩灰": "Rock Grey",
    # 聊天
    "用户名": "Username",
    "加入房间时显示的名称（4–32 个字符）":
        "Name shown when joining a room (4–32 characters)",
    "成员消息频率上限": "Member message rate limit",
    "每名成员每分钟最多发送的聊天消息数":
        "Max chat messages per member per minute",
    "聊天记录上限": "Chat history limit",
    "最多保留的聊天消息条数": "Max chat messages to keep",
    "弹幕": "Danmaku",
    "消息弹幕": "Message danmaku",
    "把新聊天消息以弹幕形式飘过视频":
        "Float new chat messages over the video as danmaku",
    "弹幕字号": "Danmaku size",
    "弹幕速度": "Danmaku speed",
    "弹幕颜色": "Danmaku colour",
    "弹幕显示区域": "Danmaku area",
    "白色": "White",
    "黄色": "Yellow",
    "青色": "Cyan",
    "粉色": "Pink",
    "绿色": "Green",
    "橙色": "Orange",
    "蓝色": "Blue",
    "紫色": "Purple",
    "全屏": "Fullscreen",
    "上方 50%": "Top 50%",
    "上方 40%": "Top 40%",
    "上方 30%": "Top 30%",
    "上方 20%": "Top 20%",
    # 流式设置
    "最大客户端数": "Max clients",
    "房间人数上限": "Max room members",
    "启用密码时需要填写房间密码": "A password is required when enabled",
    "主持: 开放指令/流端口, 把当前播放推给其他设备\n加入: 接收并渲染主持推来的画面(播放控制交给主持执行)":
        "Host: opens the command/media ports and pushes the current playback\nJoin: receives and renders the host's stream (playback is controlled by the host)",
    "同时允许加入的房客数量": "Guests allowed to join at once",
    "带宽上限": "Bandwidth limit",
    "视频码率上限 (kbps)": "Video bitrate limit (kbps)",
    "分辨率": "Resolution",
    "转码输出分辨率; 1x 流复制不受影响":
        "Transcode output resolution; 1x stream copy is unaffected",
    "帧率": "Frame rate",
    "转码输出帧率": "Transcode output frame rate",
    "音频码率": "Audio bitrate",
    "x264 预设": "x264 preset",
    "越快 CPU 占用越低, 画质略降":
        "Faster presets use less CPU with slightly lower quality",
    "关键帧间隔": "Keyframe interval",
    "数值越小, 客户端起播与跳转越快":
        "Smaller values start and seek faster on clients",
    "客户端缓冲": "Client buffer",
    "接收端缓冲, 网络抖动大时可调高":
        "Receive buffer, raise it when the network jitters",
    "心跳间隔": "Heartbeat interval",
    "指令端口心跳包间隔": "Heartbeat interval on the command port",
    "离线判定": "Offline timeout",
    "超过这个时间没有心跳就判定客户端离线":
        "A client without a heartbeat for this long is offline",
    "默认指令端口": "Default command port",
    "主持/加入对话框的默认值": "Default value in the host/join dialogs",
    "默认媒体端口": "Default media port",
    "UDP 媒体流的默认端口": "Default port of the UDP media stream",
    # 错误与提示
    "保存配置失败": "Failed to save configuration",
    "端口必须是数字": "Ports must be numbers",
    "用户名长度需要 4–32 个字符": "Username must be 4–32 characters",
    "指令端口和流端口不能相同":
        "Command port and media port must differ",
    "开始主持": "Start Hosting",
    "加入房间": "Join Room",
    "加入": "Join",
    "密码": "Password",
    "房客加入时需要输入密码": "Guests need a password to join",
    "主持端会显示本机地址": "The host shows the local address",
    "指令端口": "Command port",
    "媒体端口": "Media port",
    "正在连接...": "Connecting…",
    "已连接": "Connected",
    "已断开": "Disconnected",
    "连接断开": "Connection lost",
    "就绪": "Ready",
    "播放结束": "Playback finished",
    "播放出错, 文件可能损坏或格式不受支持":
        "Playback error: the file may be damaged or unsupported",
    "房间成员": "Room Members",
    # 文件选择框
    "这里没有可显示的文件": "No files to show here",
    "换个文件夹，或把筛选切换为「所有文件」":
        "Try another folder, or switch the filter to “All files”",
    "打开视频": "Open Video",
    "上一级": "Up",
    "快速访问": "Quick access",
    "桌面": "Desktop",
    "下载": "Downloads",
    "视频": "Videos",
    "文档": "Documents",
    "用户目录": "Home",
    "磁盘": "Drives",
    "视频文件": "Video files",
    "所有文件": "All files",
    "打开": "Open",
    # 状态/错误
    "原生": "Native",
    "不可用": "Unavailable",
    "未知": "Unknown",
    "无": "None",
    "请先完成房间验证": "Please complete room verification first",
    "密码错误": "Wrong password",
    "房间需要密码": "This room requires a password",
    "房间人数已满": "The room is full",
    "主持将你移出房间": "The host removed you from the room",
    "流式消息过大": "Stream message too large",
    "源文件无法转封装为 TS(编码可能不受支持)":
        "Cannot remux the source to TS (codec may be unsupported)",
    "推流源转封装失败": "Failed to remux the stream source",
    "重建推流": "Rebuild stream",
    "未捕获异常": "Uncaught exception",
    "解析": "Parse",
    "重新开始": "Restart",
    "抢占": "Preempt",
    "正在主持流式会话": "Hosting a streaming session",
    "退出会话": "Leave session",
    "已加入流式会话": "Joined the streaming session",
    "端口必须是 1-65535 之间的数字": "Port must be between 1 and 65535",
    "房间密码最长 64 个字符": "Room password can be at most 64 characters",
    "请输入房间密码": "Enter the room password",
    "指令地址格式不对, 例如 127.0.0.1:5000": "Invalid command address, e.g. 127.0.0.1:5000",
    "同一成员两次发言的最小间隔": "Minimum gap between two messages from one member",
    "无限制": "Unlimited",
    "1 秒": "1 s",
    "2 秒": "2 s",
    "3 秒": "3 s",
    "5 秒": "5 s",
    "主持端保存的聊天消息条数": "Chat messages the host keeps",
    "新聊天消息以弹幕浮动在视频上": "New chat messages float over the video as danmaku",
    "弹幕大小": "Danmaku size",
    "基础字号, 会随视频尺寸自动缩放": "Base font size, scales with the video size",
    "数值越大, 弹幕移动越快": "Higher values move danmaku faster",
    "弹幕区域": "Danmaku area",
    "弹幕只出现在视频上方的区域": "Danmaku only appears over the video area",
    "包括主持自己, 2-32 人": "Including the host, 2-32 people",
    "单客户端带宽上限": "Per-client bandwidth cap",
    "转码目标码率, 越高越清晰也越占带宽": "Transcode bitrate: higher is sharper, uses more bandwidth",
    "推流分辨率": "Stream resolution",
    "按视频高度缩放, 不会放大原片": "Scaled by video height, never upscaled",
    "推流帧率": "Stream frame rate",
    "x264 编码预设": "x264 encoding preset",
    "越快越省 CPU, 画质略低": "Faster presets save CPU with slightly lower quality",
    "主持端监听的默认 UDP 媒体端口": "Default UDP media port the host listens on",
    "我": "Me",
    "踢出": "Kick",
    "等待主持开始播放": "Waiting for the host to start playback",
    "画面仅主持端渲染 · 本机只同步状态与控制": "Rendered on the host · this PC syncs state and controls",
    "客户端直接渲染主持推流 · 控制指令交给主持执行": "This client renders the host stream · controls go to the host",
    "重连后会自动恢复同步": "Sync resumes after reconnecting",
    "可用": "Available",
    "静音": "Mute",
    "播放 / 暂停": "Play / Pause",
    "后退": "Back",
    "快进": "Forward",
    "退出全屏 (Esc)": "Exit fullscreen (Esc)",
    "播放": "Play",
    "暂停": "Pause",
    "浏览器已关闭 · 已结束房间": "Browser closed · room ended",
    "浏览器已关闭 · 已退出房间": "Browser closed · left the room",
    "浏览器未就绪": "Browser not ready",
    "打开网址": "Open URL",
    "输入网址：": "Enter a URL:",
    "正在打开 · ": "Opening · ",
    "已重新连接 · 等待同步": "Reconnected · waiting for sync",
    "已连接主持 · 等待同步": "Connected to host · waiting for sync",
    "连接失败": "Connection failed",
    "无法主持": "Cannot host",
    "无法开始主持": "Cannot start hosting",
    "开启": "On",
    "· 密码": "· Password",
    "主持已停止会话": "The host stopped the session",
    "停止主持流式": "Stop hosting",
    "成员": "Members",
    "不在房间中, 无法发送消息": "Not in a room, cannot send",
    "点击停止主持": "Click to stop hosting",
    "点击退出会话": "Click to leave the session",
    "流端口不可用": "Stream port unavailable",
    "已退出流式会话": "Left the streaming session",
    "房间需要密码, 等待输入": "Room needs a password",
    "你已被主持移出房间": "You were removed from the room by the host",
    "未知错误": "Unknown error",
    "界面初始化完成": "UI initialised",
    "应用图标设置失败": "Failed to set the app icon",
    "内置浏览器启动失败": "Failed to launch the built-in browser",
    "时间轴发生变化(换片/跳转/重建)时通知客户端重连流.": "Tell clients to reconnect when the timeline changes.",
    "设置": "Settings",
    "聊天室": "Chat",
    "流式传输": "Streaming",
    "流式传输设置": "Streaming settings",
    # 主菜单(启动页)
    "媒体播放": "Media playback",
    "打开本地视频，开一个房间一起看": "Open a local video and watch it together",
    "把任意窗口分享给别人，可远程操作":
        "Share any window with others and control it remotely",
    "同步网页视频的播放进度": "Sync the playback position of web videos",
    "加入房间": "Join Room",
    "输入主持的地址加入已有会话": "Enter the host address to join a session",
    "共享影音 · 应用共享 · 一起看":
        "Watch together · app sharing · remote control",
    "仅本地播放": "Local playback",
    "查看日志": "View log",
    # 共享会话界面
    "正在共享 · 等待房客加入": "Sharing · waiting for guests",
    "正在共享": "Sharing",
    "房客加入后可在右侧聊天室交流": "Guests can chat in the panel on the right",
    "已加入共享会话": "Joined the shared session",
    "画面在独立窗口中显示": "The picture opens in its own window",
    "停止共享": "Stop sharing",
    "退出房间": "Leave room",
    "共享画面": "Shared screen",
    "输入后回车发送(支持输入法)": "Type, then press Enter to send (IME supported)",
    "共享的窗口已关闭": "The shared window was closed",
    "会话已结束": "session ended",
    # 设置: 新增的三个分页
    "三种共享方式共用的房间参数; 端口/心跳对所有房间生效。":
        "Room settings shared by all three modes; ports and heartbeat apply to every room.",
    "把本地视频转码推给房客的参数; 修改后在下次重建推流时生效。":
        "Transcoding parameters for local video; applied on the next stream rebuild.",
    "同步网页视频的播放进度; 数值越小跟得越紧, 但更吃 CPU。":
        "Sync the playback position of web videos; smaller values track tighter but cost more CPU.",
    "第三方窗口共享(帧直传)的画面与操作参数; 控制权在「房间成员」里按人授权。":
        "Picture and input settings for window sharing (frame stream); control is granted per member in Room Members.",
    "传输方式": "Transport",
    "后台捕获": "Background capture",
    "开启后窗口被遮住、或没有焦点也能抓到完整画面; 关掉改用屏幕截图(被遮挡会拍到别的窗口)。":
        "Capture a window even while it is covered or unfocused; turn off to use "
        "plain screen grabs (which record whatever is on top).",
    "窗口最小化时 Windows 不再合成画面, 任何捕获方式都抓不到。":
        "Windows stops compositing minimised windows, so no capture method can grab them.",
    "捕获方式": "Capture mode",
    "后台捕获 (被遮挡也能抓)": "Background capture (works while covered)",
    "屏幕截图 (旧)": "Screen grab (legacy)",
    "后台捕获(PrintWindow): 被遮挡也能抓到完整画面; 屏幕截图(BitBlt): 老方式, 窗口不能被遮挡":
        "Background capture (PrintWindow) works while covered; "
        "screen grab (BitBlt) is the legacy path and needs the window unobstructed",
    "采集帧率": "Capture frame rate",
    "画面编码": "Frame codec",
    "JPEG 质量": "JPEG quality",
    "最大抓取宽度": "Max capture width",
    "操作注入方式": "Input injection",
    "默认缩放": "Default zoom",
    "帧直传 (低延迟)": "Frame stream",
    "frames = 帧直传(低延迟, 推荐); vlc = 旧的 ffmpeg+VLC":
        "frames = frame stream (low latency, recommended); vlc = legacy ffmpeg + VLC",
    "ffmpeg + VLC (旧)": "ffmpeg + VLC",
    "JPEG (推荐)": "JPEG",
    "PNG (无损)": "PNG",
    "窗口消息 (不动鼠标)": "Window messages",
    "真实输入 (会动鼠标)": "Real input",
    "同步轮询间隔": "Sync poll interval",
    "允许的进度偏差": "Allowed drift",
    "起始网址": "Start URL",
    "是否允许操作在「房间成员」里按成员单独授权, 默认只读。":
        "Whether a guest may control is granted per member in Room Members; read-only by default.",
    "共享期间请保持该窗口在前台可见, 最小化或被遮挡会导致画面异常。":
        "Keep the shared window visible while sharing; minimising or covering it breaks the picture.",
    "主持端读取网页播放状态的频率": "How often the host reads the page state",
    "超出这个偏差就强制对齐(seek)": "Force a seek when the drift exceeds this",
    "打开内置浏览器时默认访问的地址": "URL opened by default in the built-in browser",
    "越高越跟手, 越吃 CPU 与带宽": "Higher feels smoother and costs more CPU and bandwidth",
    "jpeg 快且小; png 无损但慢很多": "jpeg is fast and small; png is lossless but slow",
    "越低越快、越省带宽, 文字可能发虚":
        "Lower is faster and lighter, but text may look soft",
    "100% 及缩小时的抓取宽度上限; 放大最多到 200%, 不受此值限制":
        "Capture width limit at 100% and below; zoom-in goes up to 200% regardless",
    "message 不移动主持端鼠标; sendinput 更兼容但会移动光标":
        "message never moves the host cursor; sendinput is more compatible but does move it",
    "房客加入时的显示倍率; 屏幕装不下会自动降到能放下的档位":
        "Zoom used when a guest joins; automatically lowered to fit the screen",
    "设置已保存": "Settings saved",
    "设置已保存 · 外观/解码设置重启后生效": "Settings saved · appearance/decode changes apply after restart",
    "主持端推流参数; 修改后在下次重建推流（客户端加入/换片）时生效。":
        "Host streaming parameters; applied on the next stream rebuild (client joins / clip change).",
    "主持开启了房间密码, 请输入密码后加入。": "The host enabled a room password. Enter it to join.",
    "房间需要密码, 请输入密码": "This room requires a password",
    "主持已停止播放": "The host stopped playback",
    "正在连接主持…": "Connecting to host…",
    "与主持失去连接 · 自动重连中…": "Lost connection to the host · reconnecting…",

    "可控制": "Can control",
    "只读": "View only",
    "允许该房客参与播放控制": "Allow this guest to control playback",
    "主持已允许你参与控制": "The host let you control playback",
    "只读模式 · 仅主持可控制播放": "View-only mode · only the host controls playback",

    "同步播放": "Synced playback",
    "打开网址…": "Open URL…",
    "输入网址：": "Enter URL:",

}

STATUS_PATTERNS = (
    (re.compile(r"^正在播放 (.+)$"), r"Now playing \1"),
    # 共享会话卡片里的动态信息行
    (re.compile(r"^房间 (\d+) 人$"), r"Room \1"),
    (re.compile(r"^指令端口 (\S+)$"), r"Command port \1"),
    (re.compile(r"^画面 (\d+)x(\d+) · (\d+)KB/帧$"), r"Picture \1x\2 · \3 KB/frame"),
    (re.compile(r"^编码 ([\d.]+)ms/帧$"), r"Encoding \1 ms/frame"),
    (re.compile(r"^延迟 ([\d.]+)ms · (\d+)KB/帧$"), r"Latency \1 ms · \2 KB/frame"),
    (re.compile(r"^主持 (\S+)$"), r"Host \1"),
    (re.compile(r"^正在连接 (.+)$"), r"Connecting to \1"),
    (re.compile(r"^已加入房间 (.+)$"), r"Joined room \1"),
    (re.compile(r"^房间已满"), r"Room is full"),
    (re.compile(r"^(\d+) 秒$"), r"\1 s"),
    (re.compile(r"^(\d+) 帧$"), r"\1 frames"),
    (re.compile(r"^(\d+) 条$"), r"\1 messages"),
    (re.compile(r"^共 (\d+) 人$"), r"\1 people"),
    (re.compile(r"^(\d+) 人$"), r"\1 people"),
    (re.compile(r"^主持流式 · 指令 (\d+) · 流 (\d+) · 房间 (\d+) 人 · 密码$"),
     r"Hosting: cmd \1 · media \2 · \3 people · password"),
    (re.compile(r"^主持流式 · 指令 (\d+) · 流 (\d+) · 房间 (\d+) 人$"),
     r"Hosting: cmd \1 · media \2 · \3 people"),
    (re.compile(r"^指令端口 (\d+) · 流端口 (\d+) · 房间 (\d+) 人$"),
     r"Command port \1 · media port \2 · \3 people"),
    (re.compile(r"^主持 (.*) · 房间 (\d+) 人$"), r"Host \1 · Room \2"),
    (re.compile(r"^与主持同步播放 · 房间 (\d+) 人 · 主持 (.*)$"),
     r"Synced with host · Room \1 · Host \2"),
    (re.compile(r"^客户端-(.+)$"), r"client-\1"),
    (re.compile(r"^(\d+) 条新消息 ↓$"), r"\1 new messages ↓"),
    (re.compile(r"^(\d+) 个项目$"), r"\1 items"),
    (re.compile(r"^(\d+(?:\.\d+)?) 秒$"), r"\1 s"),
    (re.compile(r"^指令 (\d+) · 流 (\d+)$"), r"Cmd \1 · Stream \2"),
    (re.compile(r"^主持中 · 房间 (\d+) 人 · 指令 (\d+) / 流 (\d+)(.*)$", re.S),
     r"Hosting · Room \1 · Cmd \2 · Stream \3\4"),
    (re.compile(r"^正在打开 · (.+)$"), r"Opening · \1"),
    (re.compile(r"^已请求跳转 · 等待主持端确认$"), r"Seek requested · waiting for host"),
    (re.compile(r"^已请求播放速度 (.+)$"), r"Speed requested \1"),
)


REVERSE_I18N = {value: key for key, value in I18N.items()}

REVERSE_PATTERNS = (
    (re.compile(r"^Now playing (.+)$"), r"正在播放 \1"),
    (re.compile(r"^Connecting to (.+)$"), r"正在连接 \1"),
    (re.compile(r"^Joined room (.+)$"), r"已加入房间 \1"),
    (re.compile(r"^(\d+) people$"), r"共 \1 人"),
    (re.compile(r"^Hosting: cmd (\d+) · media (\d+) · (\d+) people · password$"),
     r"主持流式 · 指令 \1 · 流 \2 · 房间 \3 人 · 密码"),
    (re.compile(r"^Hosting: cmd (\d+) · media (\d+) · (\d+) people$"),
     r"主持流式 · 指令 \1 · 流 \2 · 房间 \3 人"),
    (re.compile(r"^Host (.*) · Room (\d+) people$"), r"主持 \1 · 房间 \2 人"),
    (re.compile(r"^Synced with host · Room (\d+) people · Host (.*)$"),
     r"与主持同步播放 · 房间 \1 人 · 主持 \2"),
    (re.compile(r"^client-(.+)$"), r"客户端-\1"),
    (re.compile(r"^(\d+) new messages ↓$"), r"\1 条新消息 ↓"),
    (re.compile(r"^(\d+) items$"), r"\1 个项目"),
    (re.compile(r"^(\d+) messages$"), r"\1 条"),
    (re.compile(r"^(\d+) frames$"), r"\1 帧"),
    (re.compile(r"^(\d+) new messages ↓$"), r"\1 条新消息 ↓"),
    (re.compile(r"^Hosting · Room (\d+) · Cmd (\d+) · Stream (\d+)(.*)$", re.S),
     r"主持中 · 房间 \1 人 · 指令 \2 / 流 \3\4"),
    (re.compile(r"^(\d+(?:\.\d+)?) s$"), r"\1 秒"),
    (re.compile(r"^Cmd (\d+) · Stream (\d+)$"), r"指令 \1 · 流 \2"),
    (re.compile(r"^Opening · (.+)$"), r"正在打开 · \1"),
    (re.compile(r"^Command port (\d+) · media port (\d+) · (\d+) people$"),
     r"指令端口 \1 · 流端口 \2 · 房间 \3 人"),
)


def tr(text):
    """把界面中文翻译成当前语言; 切回中文时把英文反向翻回中文。"""
    if not isinstance(text, str):
        return text
    if LANG == "en":
        if text in I18N:
            return I18N[text]
        for pattern, repl in STATUS_PATTERNS:
            if pattern.match(text):
                return pattern.sub(repl, text)
        return text
    if text in REVERSE_I18N:
        return REVERSE_I18N[text]
    for pattern, repl in REVERSE_PATTERNS:
        if pattern.match(text):
            return pattern.sub(repl, text)
    return text


def _walk_translate(widget):
    """递归遍历控件, 把中文文本替换成当前语言。"""
    try:
        if isinstance(widget, tk.Toplevel):
            title = widget.title()
            new_title = tr(title)
            if new_title != title:
                widget.title(new_title)
    except (tk.TclError, AttributeError):
        pass
    try:
        text = widget.cget("text")
        if isinstance(text, str) and text:
            new = tr(text)
            if new != text:
                widget.configure(text=new)
    except (tk.TclError, AttributeError):
        pass
    try:
        tip = getattr(widget, "_tooltip", None)
        if isinstance(tip, str):
            widget._tooltip = tr(tip)
        elif tip is not None:
            text = getattr(tip, "text", None)
            if isinstance(text, str) and text:
                tip.text = tr(text)
    except Exception:
        pass
    # 自绘控件在 hover/重绘时用这些属性重新画文字, 只翻译 Canvas item 不够
    for attr in ("_text", "_hint_text"):
        try:
            value = getattr(widget, attr, None)
            if isinstance(value, str) and value:
                setattr(widget, attr, tr(value))
        except Exception:
            pass
    try:
        if isinstance(widget, tk.Canvas):
            for item in widget.find_all():
                if widget.type(item) == "text":
                    text = widget.itemcget(item, "text")
                    if text:
                        new = tr(text)
                        if new != text:
                            widget.itemconfigure(item, text=new)
    except tk.TclError:
        pass
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for child in children:
        _walk_translate(child)


def apply_language(root, lang="zh"):
    """设置全局语言并立即刷新整棵界面树。"""
    global LANG
    LANG = "en" if str(lang).lower() == "en" else "zh"
    if root is not None:
        _walk_translate(root)
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
    # 小数类设置不能取整, 否则 1.5 秒会被裁成 2、1.25 倍会变成 1
    decimals = {"heartbeat": 1, "stale": 1, "web_sync_tolerance": 2,
                "buffer_scale": 2, "app_zoom": 2}
    if key in decimals:
        return float(round(number, decimals[key]))
    return int(round(number))



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


def set_animations_enabled(value):
    """开关界面动画(跨模块共享同一个全局, 任何模块都走这里改)。"""
    global ANIMATIONS_ENABLED
    ANIMATIONS_ENABLED = bool(value)


def animations_enabled():
    """读取动画开关: 用函数而不是直接读全局, 拆分模块后各模块才看得到最新值。"""
    return bool(ANIMATIONS_ENABLED)


def apply_appearance(settings):
    """按设置套用界面底色与强调色(需在创建控件前调用)。"""
    surface = SURFACES.get(settings.get("surface"), SURFACES["midnight"])
    for key, value in surface.items():
        setattr(C, key, value)
    accent = ACCENTS.get(settings.get("accent"), ACCENTS["aurora"])
    C.ACCENT, C.ACCENT_HOVER, C.ACCENT_DOWN, C.ACCENT_SOFT = accent
    set_animations_enabled(settings.get("animations", True))


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
LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-7s] [%(threadName)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_ROLE = ""          # ""(未定) / "host" / "client" —— 决定写哪个日志文件
_LOG_FILE_HANDLER = None
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
        global _LOG_FILE_HANDLER
        _LOG_FILE_HANDLER = file_handler
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


def set_log_role(role):
    """把日志切到"服务端/客户端"各自的文件: nobplaypot-host.log / nobplaypot-client.log。

    同一个进程可能先当客户端再加入房间当主持, 所以角色是运行期切换的;
    每次切换都会重置一个 2MB 轮转的文件处理器。
    """
    global LOG_ROLE, LOG_PATH, _LOG_FILE_HANDLER
    role = str(role or "").lower()
    if role not in ("host", "client"):
        role = ""
    if role == LOG_ROLE and _LOG_FILE_HANDLER is not None:
        return LOG_PATH
    base_dir = os.path.dirname(LOG_PATH) or "."
    if role:
        name = "nobplaypot-%s.log" % role
    else:
        name = os.path.basename(LOG_PATH) or "nobplaypot.log"
    new_path = os.path.join(base_dir, name)
    handler = None
    for candidate in (new_path,
                      os.path.join(tempfile.gettempdir(), "Nobplaypot", "logs", name)):
        try:
            os.makedirs(os.path.dirname(candidate) or ".", exist_ok=True)
            handler = RotatingFileHandler(candidate, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            new_path = candidate
            break
        except OSError:
            handler = None
    if handler is None:
        return LOG_PATH
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    old = _LOG_FILE_HANDLER
    log.addHandler(handler)
    _LOG_FILE_HANDLER = handler
    LOG_PATH = new_path
    LOG_ROLE = role
    if old is not None:
        log.removeHandler(old)
        try:
            old.close()
        except Exception:
            pass
    log.info("=" * 60)
    log.info("[log] 角色切换为 %s, 日志文件 %s", role or "未定", new_path)
    return new_path


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


def _free_tcp_port(start, end):
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有可用的本地调试端口")


def seek_trace(stage, **fields):
    """进度链路追踪: 统一格式, 便于 grep [seek]。"""
    try:
        parts = " ".join("%s=%s" % (k, v) for k, v in fields.items())
        log.info("[seek] %s %s", stage, parts)
    except Exception:
        pass


def _norm_url(url):
    """用于标签页匹配的 URL 归一化: 去掉锚点与尾部斜杠。"""
    text = str(url or "").split("#", 1)[0].strip()
    if text.endswith("/"):
        text = text.rstrip("/")
    return text


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


_HIDDEN_RUN_COOLDOWN = {}
_HIDDEN_RUN_LOCK = threading.Lock()


def run_hidden(args, timeout=20):
    """静默运行子进程: 不弹控制台窗口(pythonw 下启动 powershell 会闪黑/蓝窗)。"""
    kwargs = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = info
        except Exception:
            pass
    return subprocess.run(args, **kwargs)


def _win_list_processes(image_name):
    """用 Toolhelp 枚举进程: 返回 [(pid, parent_pid)]。不依赖 PowerShell(cmd 会被安全软件拦)。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, ctypes.c_void_p(-1).value):
        return []
    results = []
    target = image_name.lower()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                results.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return results


def _win_command_line(pid):
    """读取进程命令行(NtQueryInformationProcess), 用于识别"是不是我们起的 ffmpeg"。"""
    if os.name != "nt":
        return ""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = 4096
        buffer = ctypes.create_string_buffer(size)
        returned = ctypes.c_ulong(0)
        status = ntdll.NtQueryInformationProcess(
            ctypes.c_void_p(handle), 60, buffer, size, ctypes.byref(returned))
        if status != 0:
            return ""
        length = ctypes.c_ushort.from_buffer(buffer, 0).value
        offset = ctypes.c_void_p.from_buffer(buffer, 8).value or 0
        if not (0 < offset < size - 2):
            offset = 16
        if length <= 0 or offset + length > size:
            return ""
        return buffer.raw[offset:offset + length].decode("utf-16-le", "ignore")
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _win_pid_alive(pid):
    if os.name != "nt" or not pid:
        return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


_FFMPEG_JOB = None


def _ffmpeg_job():
    """建一个"进程退出就自杀"的 Job, 把采集进程放进去 —— 杜绝孤儿 ffmpeg。"""
    global _FFMPEG_JOB
    if os.name != "nt":
        return None
    if _FFMPEG_JOB is not None:
        return _FFMPEG_JOB
    import ctypes
    from ctypes import wintypes
    LARGE_INTEGER = ctypes.c_int64
    SIZE_T = ctypes.c_size_t

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", LARGE_INTEGER),
                    ("PerJobUserTimeLimit", LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", SIZE_T),
                    ("MaximumWorkingSetSize", SIZE_T),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", SIZE_T),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                    ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]

    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None
        _FFMPEG_JOB = job
        return job
    except Exception:
        return None


def assign_to_ffmpeg_job(pid):
    """把采集进程放进 Job: 主进程无论怎么退出(崩溃/被强杀), 系统都会顺手杀掉它。"""
    if os.name != "nt" or not pid:
        return False
    import ctypes
    job = _ffmpeg_job()
    if not job:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须用完全访问权限, 只给 SET_QUOTA|TERMINATE 会 ERROR_ACCESS_DENIED(5)
    handle = kernel32.OpenProcess(0x1F0FFF, False, int(pid))
    if not handle:
        return False
    try:
        ok = kernel32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                               ctypes.c_void_p(handle))
        if not ok:
            log.debug("把 ffmpeg 加入 Job 失败: err=%s", ctypes.get_last_error())
        return bool(ok)
    except Exception:
        return False
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def cleanup_orphan_ffmpeg():
    """清理父进程已经死掉的采集型 ffmpeg(不用 PowerShell, 避免被安全软件拦)。"""
    if os.name != "nt":
        return 0
    import ctypes
    killed = 0
    for pid, parent in _win_list_processes("ffmpeg.exe"):
        if pid == os.getpid() or _win_pid_alive(parent):
            continue
        cmd = _win_command_line(pid)
        if "gdigrab" not in cmd or "udp://127.0.0.1:" not in cmd:
            continue
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, int(pid))
            if handle:
                ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
                killed += 1
        except Exception:
            log.debug("结束孤儿 ffmpeg 失败", exc_info=True)
    if killed:
        log.info("清理遗留 ffmpeg 采集进程: %d 个", killed)
    return killed


def kill_profile_edges(profile_dir, cooldown=2.0):
    """只杀使用指定 profile 目录的 Edge 进程, 不影响其它实例。"""
    if not profile_dir:
        return
    target = os.path.abspath(profile_dir)
    now = time.monotonic()
    with _HIDDEN_RUN_LOCK:
        last = _HIDDEN_RUN_COOLDOWN.get(target)
        if last is not None and now - last < cooldown:
            # 关闭窗口时会被多条路径调用, 2 秒内只真正执行一次, 避免连弹多个窗口
            return
        _HIDDEN_RUN_COOLDOWN[target] = now
    try:
        # 不用 PowerShell: 隐蔽执行 PowerShell 会被安全软件当成可疑行为拦截
        import ctypes
        killed = 0
        for pid, _parent in _win_list_processes("msedge.exe"):
            cmd = _win_command_line(pid)
            if not cmd or target.lower() not in cmd.lower():
                continue
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, int(pid))
            if not handle:
                continue
            try:
                ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
                killed += 1
            finally:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        if killed:
            log.debug("清理浏览器实例: %d 个", killed)
    except Exception:
        log.debug("清理浏览器实例失败", exc_info=True)
