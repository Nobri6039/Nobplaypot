"""appshare 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403

from np_net import *  # noqa: F401,F403

from np_widgets import *  # noqa: F401,F403

from np_web import *  # noqa: F401,F403

APP_QUALITY_PRESETS = (
    ("low",  "低 (960x540 · 20fps · 1.5Mbps)", 960, 540, 20, 1500),
    ("std",  "标准 (1280x720 · 30fps · 3Mbps)", 1280, 720, 30, 3000),
    ("high", "高 (1920x1080 · 30fps · 6Mbps)", 1920, 1080, 30, 6000),
)

APP_MOUSE_MESSAGES = {
    "move": 0x0200, "left_down": 0x0201, "left_up": 0x0202,
    "right_down": 0x0204, "right_up": 0x0205,
    "middle_down": 0x0207, "middle_up": 0x0208, "wheel": 0x020A,
}

APP_MOUSE_BUTTON_FLAGS = {
    "left_down": 1, "left_up": 0,
    "right_down": 2, "right_up": 0,
    "middle_down": 16, "middle_up": 0,
    "move": 0, "wheel": 0,
}


def _win_input_structs():
    """构造 SendInput 需要的 INPUT 结构(每次调用现建, 避免模块级依赖 wintypes 时机)。"""
    import ctypes
    from ctypes import wintypes
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    return INPUT


def send_input_events(events):
    """events: [("mouse", flags, data), ("key", scan, down)]。"""
    if os.name != "nt" or not events:
        return False
    import ctypes
    INPUT = _win_input_structs()
    send = ctypes.windll.user32.SendInput
    send.restype = ctypes.c_uint
    send.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    prepared = []
    for item in events:
        entry = INPUT()
        if item[0] == "mouse":
            entry.type = 0
            entry.union.mi.dx = 0
            entry.union.mi.dy = 0
            entry.union.mi.mouseData = int(item[2])
            entry.union.mi.dwFlags = int(item[1])
            entry.union.mi.time = 0
            entry.union.mi.dwExtraInfo = 0
        else:
            entry.type = 1
            entry.union.ki.wVk = 0
            entry.union.ki.wScan = int(item[1])
            entry.union.ki.dwFlags = int(item[2])
            entry.union.ki.time = 0
            entry.union.ki.dwExtraInfo = 0
        prepared.append(entry)
    array = (INPUT * len(prepared))(*prepared)
    sent = send(len(prepared), array, ctypes.sizeof(INPUT))
    return sent == len(prepared)


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


def enum_app_windows():
    """列出可共享的顶层窗口: [(hwnd, title, cls, pid, w, h)]。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    results = []
    own_pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            cls = cls_buf.value
            if cls in ("Progman", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"):
                return True
            style = user32.GetWindowLongW(hwnd, -20)
            if style & 0x00000080:  # WS_EX_TOOLWINDOW
                return True
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 200 or height < 150:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True
            results.append((int(hwnd), title, cls, int(pid.value), width, height))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    results.sort(key=lambda item: item[1].lower())
    return results


def find_ffmpeg(extra=""):
    """查找 ffmpeg.exe。

    顺序: 设置项 > ffmpeg-runtime 目录(与 vlc-runtime/python-runtime 同级)
          > 程序目录/Bin/tools > PATH
    """
    candidates = []
    if extra:
        candidates.append(extra)
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(base)
        for folder in (base, root):
            candidates += [
                os.path.join(folder, "ffmpeg-runtime", "ffmpeg.exe"),
                os.path.join(folder, "ffmpeg.exe"),
            ]
            candidates.append(os.path.join(folder, "bin", "ffmpeg.exe"))
        candidates.append(os.path.join(root, "tools", "ffmpeg.exe"))
    except Exception:
        pass
    which = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def get_window_rect(hwnd):
    """返回窗口在屏幕上的矩形 (left, top, width, height), 失败返回 None。"""
    if os.name != "nt" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes
    try:
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        return int(rect.left), int(rect.top), width, height
    except Exception:
        return None


def get_client_rect(hwnd):
    """返回窗口客户区在屏幕上的矩形 (left, top, width, height)。

    采集和坐标映射都必须用客户区: 用整窗矩形会把标题栏也算进去,
    导致点击位置整体偏移一个标题栏的高度(这就是"点了没反应"的原因之一)。
    """
    if os.name != "nt" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        rect = wintypes.RECT()
        if not user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(int(hwnd), ctypes.byref(origin)):
            return None
        return int(origin.x), int(origin.y), width, height
    except Exception:
        return None


class AppCapture:
    """主持端: 用 ffmpeg gdigrab 抓窗口区域并编码成 MPEG-TS, 推到本地中转端口。

    不能用 gdigrab 的 title=: 它按 ANSI 窗口标题匹配, 中文标题(微信/浏览器)一律找不到。
    这里改成按屏幕坐标抓桌面区域 -- 对 DWM 合成的窗口也能出画面。
    """

    def __init__(self, ffmpeg, title, relay_port, quality="std",
                 source_width=0, source_height=0, rect=None):
        self.ffmpeg = ffmpeg
        self.title = title
        self.relay_port = int(relay_port)
        self.rect = tuple(rect) if rect else None
        preset = next((q for q in APP_QUALITY_PRESETS if q[0] == quality),
                      APP_QUALITY_PRESETS[1])
        self._label, preset_w, preset_h, self.fps, self.bitrate = preset[1:]
        if self.rect:
            source_width, source_height = self.rect[2], self.rect[3]
        self.source_width = int(source_width or 0)
        self.source_height = int(source_height or 0)
        # 不强行拉伸成 16:9: 宽度取预设, 高度按源窗口比例自动算(保持偶数)
        self.width = preset_w
        if self.source_width > 0 and self.source_height > 0:
            ratio = self.source_height / float(self.source_width)
            self.height = max(2, int(round(preset_w * ratio / 2.0)) * 2)
        else:
            self.height = preset_h
        self.proc = None
        self._stderr = []

    def stream_size(self):
        """实际编码出来的画面尺寸(竖长窗口按高度定档, 宽度是自动算的)。"""
        if self.source_height > self.source_width:
            height = min(self.height, 720)
            width = max(2, int(round(height * self.source_width
                                     / float(self.source_height) / 2.0)) * 2)
            return width, height
        return self.width, self.height

    def capture_args(self):
        """gdigrab 的输入参数: 优先按屏幕区域, 拿不到区域时退回 title。"""
        if self.rect:
            left, top, width, height = self.rect
            return ["-offset_x", str(left), "-offset_y", str(top),
                    "-video_size", f"{width}x{height}", "-i", "desktop"]
        escaped = self.title.replace('"', '\\"')
        return ["-i", f"title={escaped}"]

    def command(self):
        # 竖长窗口(如计算器)按高度定档, 否则会被放大成又细又长的巨大画面
        if self.source_height > self.source_width:
            scale = f"scale=-2:{min(self.height, 720)}"
        else:
            scale = f"scale={self.width}:-2"
        return ([
            self.ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-f", "gdigrab", "-framerate", str(self.fps),
            "-draw_mouse", "0",
        ] + self.capture_args() + [
            "-vf", scale,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", f"{self.bitrate}k", "-maxrate", f"{int(self.bitrate * 1.3)}k",
            "-bufsize", f"{int(self.bitrate * 0.5)}k", "-g", str(self.fps),
            "-pix_fmt", "yuv420p",
            "-f", "mpegts", f"udp://127.0.0.1:{self.relay_port}?pkt_size=1316",
        ])

    def start(self):
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "cwd": os.path.dirname(self.ffmpeg) or ".",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(self.command(), **kwargs)
        assign_to_ffmpeg_job(self.proc.pid)
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="app-capture-log").start()
        log.info("应用共享采集启动: %s %s (%s) -> udp 127.0.0.1:%d",
                 self.title, self.width, self.height, self.relay_port)
        return self.proc

    def _drain_stderr(self):
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "ignore").strip()
                if line:
                    self._stderr.append(line)
                    self._stderr = self._stderr[-40:]
                    log.debug("ffmpeg: %s", line)
        except Exception:
            pass

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def last_error(self):
        return self._stderr[-1] if self._stderr else ""

    def last_lines(self, count=2):
        return self._stderr[-count:] if self._stderr else []

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.5)
        except Exception:
            pass
        # 无论上面是否成功, 只要还活着就强杀: 留一个 ffmpeg 就是 30fps 的 CPU 占用
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1.5)
        except Exception:
            log.debug("强制结束 ffmpeg 失败", exc_info=True)
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:
            pass


def _focus_window_of(hwnd):
    """取某个窗口所属线程的焦点窗口(键盘要投给它, 投给顶层窗口没用)。"""
    if os.name != "nt" or not hwnd:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]

    try:
        thread_id = user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), None)
        if not thread_id:
            return 0
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(int(thread_id), ctypes.byref(info)):
            return 0
        return int(info.hwndFocus or 0)
    except Exception:
        return 0


def inject_app_input(hwnd, client_w, client_h, payload, rect=None, mode="sendinput",
                     info=None, target_hint=0):
    """把客户端的归一化输入注入到目标窗口。

    归一化坐标先换成屏幕坐标, 再用 WindowFromPoint 找到真正接收事件的子窗口
    (微信/浏览器这类程序的外层窗口不处理输入, 必须投给内部子窗口)。
    info 传入 dict 时会把实际命中的目标窗口/屏幕坐标回填, 供上层打日志。
    """
    if os.name != "nt" or not hwnd:
        return False, "unsupported"
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = int(hwnd)
    kind = str(payload.get("kind") or "mouse")
    left, top, width, height = (rect if rect else (0, 0, client_w, client_h))
    width = max(1, int(width))
    height = max(1, int(height))

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def _resolve_target(nx, ny):
        sx = int(left + nx * width)
        sy = int(top + ny * height)
        point = POINT(sx, sy)
        child = int(user32.WindowFromPoint(point) or 0)
        # WindowFromPoint 取的是"屏幕最上层", 共享窗口被遮住时会拿到别人的窗口。
        # 这里先确认它的顶层祖先是不是我们要共享的窗口, 不是就退回按窗口内部命中测试。
        target = child
        if child:
            root = int(user32.GetAncestor(ctypes.c_void_p(child), 2) or 0)  # GA_ROOT
            if root and root != hwnd:
                target = 0
        if not target:
            local = POINT(sx, sy)
            user32.ScreenToClient(ctypes.c_void_p(hwnd), ctypes.byref(local))
            hit = int(user32.ChildWindowFromPointEx(
                ctypes.c_void_p(hwnd), ctypes.byref(local), 0x0001) or 0)
            target = hit or hwnd
        client_pt = POINT(sx, sy)
        user32.ScreenToClient(ctypes.c_void_p(target), ctypes.byref(client_pt))
        return target, int(client_pt.x), int(client_pt.y), sx, sy

    try:
        if kind == "mouse":
            action = str(payload.get("action") or "move")
            nx = max(0.0, min(1.0, float(payload.get("x") or 0.0)))
            ny = max(0.0, min(1.0, float(payload.get("y") or 0.0)))
            target, x, y, sx, sy = _resolve_target(nx, ny)
            if target_hint and action != "wheel" and not action.endswith("_down"):
                # 拖动中/抬起: 固定发给按下时那个窗口, 否则拖拽会中断
                target = int(target_hint)
                client_pt = POINT(sx, sy)
                user32.ScreenToClient(ctypes.c_void_p(target), ctypes.byref(client_pt))
                x, y = int(client_pt.x), int(client_pt.y)
            # 同一台机器上主持+客户端同时跑时, 注入点可能正好落在我们自己的窗口上,
            # 那会形成"注入 → 自己收到 → 再注入"的回环(表现为偶尔双击/连击)。
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(ctypes.c_void_p(target),
                                            ctypes.byref(pid))
            if pid.value == os.getpid():
                if info is not None:
                    info.update({"target": target, "screen": (sx, sy),
                                 "local": (x, y), "mode": mode, "action": action,
                                 "self": True})
                return False, "self-target"
            if info is not None:
                info.update({"target": target, "screen": (sx, sy),
                             "local": (x, y), "mode": mode, "action": action})
            if mode == "sendinput":
                # 真实注入: 先把共享窗口提到前台, 否则点击会被覆盖它的窗口吃掉
                try:
                    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                except Exception:
                    pass
                # 指针先挪到目标位置, 再合成按键事件(兼容不认 PostMessage 的程序)
                if action == "wheel":
                    delta = int(payload.get("delta") or 120)
                    user32.SetCursorPos(sx, sy)
                    send_input_events([("mouse", MOUSEEVENTF_WHEEL, delta)])
                    return True, "wheel-sendinput"
                flag = {
                    "left_down": MOUSEEVENTF_LEFTDOWN, "left_up": MOUSEEVENTF_LEFTUP,
                    "right_down": MOUSEEVENTF_RIGHTDOWN, "right_up": MOUSEEVENTF_RIGHTUP,
                    "middle_down": MOUSEEVENTF_MIDDLEDOWN, "middle_up": MOUSEEVENTF_MIDDLEUP,
                }.get(action)
                user32.SetCursorPos(sx, sy)
                if flag is None:
                    return True, "move-sendinput"
                if send_input_events([("mouse", flag, 0)]):
                    return True, action + "-sendinput"
                return False, "sendinput-failed"
            lparam = (y << 16) | (x & 0xFFFF)
            if action == "wheel":
                delta = int(payload.get("delta") or 120)
                # 滚轮用屏幕坐标, 且必须带 WHEEL_DELTA 的符号位
                screen_lparam = (sy << 16) | (sx & 0xFFFF)
                user32.PostMessageW(ctypes.c_void_p(target), 0x020A,
                                    ((delta & 0xFFFF) << 16), screen_lparam)
                return True, "wheel"
            msg = APP_MOUSE_MESSAGES.get(action)
            if msg is None:
                return False, "bad-action"
            post = user32.PostMessageW
            send_sync = user32.SendMessageW
            # 模拟事件: 先让窗口"醒"过来(激活+聚焦), 再补一帧移动, 最后投递按键。
            # 很多程序(Chromium 系/Qt)只处理"已激活窗口"上的点击。
            if action.endswith("_down"):
                try:
                    send_sync(ctypes.c_void_p(hwnd), 0x0006, 1, 0)      # WM_ACTIVATE WA_ACTIVE
                    send_sync(ctypes.c_void_p(hwnd), 0x0007, 0, 0)      # WM_SETFOCUS
                except Exception:
                    pass
                post(ctypes.c_void_p(target), 0x0200, 0, lparam)        # WM_MOUSEMOVE 预热
            wparam = APP_MOUSE_BUTTON_FLAGS.get(action, 0)
            if action == "move":
                # 拖动: 移动时要带上按住的键位(MK_LBUTTON 等), 否则程序不认
                wparam = int(payload.get("buttons") or 0)
            elif action.endswith("_up"):
                # 抬起时带上原来按住的位, 与真实事件一致
                wparam = 0
            post(ctypes.c_void_p(target), msg, wparam, lparam)
            return True, action
        if kind == "key":
            scan = int(payload.get("scan") or 0)
            down = bool(payload.get("down"))
            if not scan:
                # 客户端可能只给虚拟键码(Tk keycode), 这里换算出扫描码
                vk_in = int(payload.get("vk") or 0)
                if vk_in:
                    scan = int(user32.MapVirtualKeyW(vk_in, 0) or 0)
            if not scan:
                return False, "no-scan"
            if mode == "sendinput":
                flag = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
                if send_input_events([("key", scan, flag)]):
                    return True, "key-sendinput"
                return False, "sendinput-failed"
            vk = int(user32.MapVirtualKeyW(scan, 1) or 0)
            lparam = 1 | (scan << 16)
            if not down:
                lparam |= (1 << 30) | (1 << 31)
            # 投给共享窗口所在线程的"焦点窗口", 而不是当前前台窗口:
            # 主持端自己可能正在别处操作, 前台窗口不一定是被共享的程序。
            target = _focus_window_of(hwnd) or hwnd
            # 只发 WM_KEYDOWN/UP: 目标线程的消息循环会自己 TranslateMessage 生成
            # WM_CHAR, 再补一条 WM_CHAR 就会变成"打一个字出现两个"。
            user32.PostMessageW(ctypes.c_void_p(target), 0x0100 if down else 0x0101,
                                vk, lparam)
            return True, "key"
        if kind == "text":
            # 输入法/粘贴得到的整段文字: 直接逐字符投 WM_CHAR(绕过键盘布局和 IME)
            text = str(payload.get("text") or "")
            if not text:
                return False, "empty"
            target = _focus_window_of(hwnd) or hwnd
            sent = 0
            for ch in text:
                user32.PostMessageW(ctypes.c_void_p(target), 0x0102, ord(ch), 1)
                sent += 1
            return True, "text:%d" % sent
    except Exception as error:
        return False, str(error)
    return False, "bad-kind"


class AppInputRouter:
    """客户端: 捕获共享窗口里的鼠标操作, 归一化后发给主持端。

    主路径用 WH_MOUSE_LL 钩子(点击落在哪个子窗口都能抓到), 钩子失败才回退到窗口子类化。
    """

    def __init__(self, root, get_hwnd, on_event):
        self.root = root
        self.get_hwnd = get_hwnd
        self.on_event = on_event
        self.enabled = False
        self.target = None
        self._old_proc = None
        self._new_proc = None
        self._queue = queue.Queue()
        self._pump_job = None
        self._pump_alive = False
        self._hook = None
        self._hook_proc = None
        self._hook_msg = None
        self._targets = {}          # hwnd -> 原窗口过程
        self._last_emit = (0.0, "")  # 去重: (时间, 动作)
        self._debug_left = 6         # 前几条鼠标消息写日志, 便于排查
        self.width = 1
        self.height = 1

    _BUTTONS = ((0x01, "left"), (0x02, "right"), (0x04, "middle"))

    def attach(self):
        """启动"轮询捕获": 每 30ms 读一次鼠标按键状态。

        为什么不用钩子/子类化: WH_MOUSE_LL 会让全系统鼠标卡死; 子类化 libvlc 的
        窗口会在 VLC 线程回调 Python, 触发 `Fatal Python error: PyEval_RestoreThread
        ... GIL` 把客户端搞崩(2026-11-04 两次实测)。轮询只在本进程主线程读
        GetAsyncKeyState + 光标位置, 没有任何跨线程回调, 不会崩。
        """
        if os.name != "nt":
            return False
        frame = int(self.get_hwnd() or 0)
        if not frame:
            return False
        self.target = frame
        self._poll_state = {"left": False, "right": False, "middle": False}
        self._poll_alive = True
        self._poll_job = None
        self._start_pump()
        self._poll()
        log.info("[appinput] poll.start frame=0x%X", frame)
        return True

    def _poll(self):
        if not getattr(self, "_poll_alive", False):
            return
        try:
            self._poll_once()
        except Exception:
            log.debug("点击轮询异常", exc_info=True)
        try:
            self._poll_job = self.root.after(16, self._poll)
        except Exception:
            self._poll_alive = False

    def _poll_once(self):
        """读按键状态 -> 检测按下/抬起 -> 换算成共享窗口内的归一化坐标。"""
        if not self.enabled:
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        frame = int(self.target or 0)
        if not frame or not user32.IsWindow(ctypes.c_void_p(frame)):
            return
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(ctypes.c_void_p(frame), ctypes.byref(origin)):
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(ctypes.c_void_p(frame), ctypes.byref(rect)):
            return
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        cx = int(point.x) - origin.x
        cy = int(point.y) - origin.y
        inside = (0 <= cx < width) and (0 <= cy < height)
        state = getattr(self, "_poll_state", None)
        if state is None:
            self._poll_state = state = {"left": False, "right": False, "middle": False}
        for vk, name in self._BUTTONS:
            pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if pressed == state.get(name, False):
                continue
            state[name] = pressed
            action = name + ("_down" if pressed else "_up")
            if not inside:
                continue
            # 点在这块区域上, 但最上层窗口不是我们的(被别的程序盖住) -> 不转发
            under = int(user32.WindowFromPoint(point) or 0)
            if under:
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(under), ctypes.byref(owner))
                if owner.value != os.getpid():
                    continue
            nx = round(cx / float(width), 4)
            ny = round(cy / float(height), 4)
            self._cap_count = getattr(self, "_cap_count", 0) + 1
            ts = time.perf_counter()
            log.info("[appinput] capture #%d %s 屏幕=(%d,%d) 窗口内=(%d,%d) "
                     "归一=(%.3f,%.3f) 尺寸=%dx%d under=0x%X t=%.1fms",
                     self._cap_count, action, point.x, point.y, cx, cy,
                     nx, ny, width, height, under, ts * 1000.0)
            self._emit({"kind": "mouse", "action": action, "x": nx, "y": ny,
                        "delta": 0, "_ts": ts})

    def _deepest_child(self, parent):
        """从 parent 的中心点一路向下找最内层子窗口(真正接收点击的那个)。"""
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetClientRect(ctypes.c_void_p(parent), ctypes.byref(rect)):
            return None
        point = wintypes.POINT((rect.right - rect.left) // 2,
                               (rect.bottom - rect.top) // 2)
        current = int(parent)
        for _ in range(8):
            child = int(user32.ChildWindowFromPointEx(
                ctypes.c_void_p(current), ctypes.byref(point), 0x0003) or 0)
            if not child or child == current:
                break
            user32.MapWindowPoints(ctypes.c_void_p(current), ctypes.c_void_p(child),
                                   ctypes.byref(point), 1)
            current = child
        return None if current == int(parent) else current

    def _child_hwnd(self):
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        parent = int(self.get_hwnd() or 0)
        if not parent:
            return None
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
                if area > 0:
                    name = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, name, 128)
                    found.append((area, int(hwnd), name.value))
            return True

        user32.EnumChildWindows(parent, _cb, 0)
        if found:
            # 真实点击落在最内层的输出窗口上(VLC 是 "VLC video output"),
            # 挂在父窗口 "VLC video main" 上收不到事件。
            preferred = [item for item in found if "video output" in item[2]]
            pool = preferred or found
            pool.sort(reverse=True)
            return pool[0][1]
        # 兜底: 有些情况 VLC 会把画面输出到自己的窗口(不是 frame 的子窗口),
        # 这时点击落在那个窗口上, 必须挂到它上面才能收到事件。
        vlc_windows = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb_vlc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, name, 128)
            if not name.value.startswith("VLC"):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            if area > 0 and name.value.startswith("VLC video main"):
                vlc_windows.append((area, int(hwnd)))
            return True

        user32.EnumWindows(_cb_vlc, 0)
        if vlc_windows:
            vlc_windows.sort(reverse=True)
            log.info("输入路由改挂 VLC 输出窗口: 0x%X", vlc_windows[0][1])
            return vlc_windows[0][1]
        return parent

    def _install_hook(self):
        """【已禁用】低级鼠标钩子会让全系统鼠标卡死, 保留代码仅作记录。

        症状: 客户端一加入房间, 整个系统的鼠标就不动了, 客户端退出后才恢复
        (2026-11-04 实测日志: `应用共享输入钩子已安装(WH_MOUSE_LL)` 之后 7 秒
        客户端断开)。WH_MOUSE_LL 的回调由安装线程的消息泵驱动, Python 主线程
        一旦忙于网络/渲染, 所有系统鼠标事件都会排队等它, 直到超时才放行。
        """
        return False

    def _install_hook_legacy(self):
        """旧的钩子实现(不再调用), 坐标换算逻辑留作参考。"""
        if os.name != "nt" or self._hook is not None:
            return False
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_void_p)

        def _hook_proc(code, wparam, lparam):
            try:
                if code == 0 and self.enabled:
                    self._handle_hook(int(wparam), lparam)
            except Exception:
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._hook_proc = HOOKPROC(_hook_proc)
        hook = user32.SetWindowsHookExW(14, self._hook_proc, None, 0)  # WH_MOUSE_LL
        if not hook:
            self._hook_proc = None
            log.warning("低级鼠标钩子安装失败, 回退到窗口子类化")
            return False
        self._hook = hook
        self._hook_msg = MSLLHOOKSTRUCT
        log.info("应用共享输入钩子已安装(WH_MOUSE_LL)")
        return True

    def _handle_hook(self, msg, lparam):
        """把屏幕坐标换成共享窗口内的归一化坐标, 只处理落在窗口里的鼠标事件。"""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        window = int(self.get_hwnd() or 0)
        if not window:
            return
        info = ctypes.cast(lparam, ctypes.POINTER(self._hook_msg)).contents
        sx, sy = int(info.pt.x), int(info.pt.y)

        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(ctypes.c_void_p(window), ctypes.byref(origin)):
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(ctypes.c_void_p(window), ctypes.byref(rect)):
            return
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        cx, cy = sx - origin.x, sy - origin.y
        if not (0 <= cx < width and 0 <= cy < height):
            return
        nx = round(max(0.0, min(1.0, cx / float(width))), 4)
        ny = round(max(0.0, min(1.0, cy / float(height))), 4)
        # 窗口被别的程序盖住时, 落在这块区域里的点击其实属于别人, 不能转发
        try:
            under = user32.WindowFromPoint(wintypes.POINT(sx, sy))
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(under, ctypes.byref(owner))
            if owner.value != os.getpid():
                return
        except Exception:
            pass
        action = {
            0x0200: "move", 0x0201: "left_down", 0x0202: "left_up",
            0x0204: "right_down", 0x0205: "right_up",
            0x0207: "middle_down", 0x0208: "middle_up",
        }.get(msg)
        if action:
            self._emit({"kind": "mouse", "action": action, "x": nx, "y": ny, "delta": 0})
            return
        if msg == 0x020A:
            delta = ctypes.c_short((int(info.mouseData) >> 16) & 0xFFFF).value
            self._emit({"kind": "mouse", "action": "wheel", "x": nx, "y": ny,
                        "delta": delta})

    def _collect_windows(self):
        """只挂**本进程 Tk 线程拥有**的窗口: 顶层窗口 + 视频框。

        绝不能去子类化 libvlc 自己创建的窗口: 那是 VLC 线程的窗口, 它的窗口过程
        由 VLC 线程调用, 跨线程回调 Python 会触发
        `PyEval_RestoreThread: must be called with the GIL held` 直接崩进程
        (2026-11-04 实测)。子窗口里的点击靠父窗口收到的 WM_PARENTNOTIFY 兜住。
        """
        if os.name != "nt":
            return []
        import ctypes
        user32 = ctypes.windll.user32
        frame = int(self.get_hwnd() or 0)
        if not frame:
            return []
        windows = []
        top = int(user32.GetAncestor(ctypes.c_void_p(frame), 2) or 0)
        for hwnd in (top, frame):
            if hwnd and hwnd not in windows:
                windows.append(hwnd)
        return windows

    def _attach_wndproc(self):
        """给共享窗口的整棵树挂 WndProc: 点击落在其中任何一个窗口上都能捕获。"""
        if os.name != "nt":
            return False
        import ctypes
        user32 = ctypes.windll.user32
        self._detach_wndproc()
        windows = self._collect_windows()
        if not windows:
            return False
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p)
        long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        long_ptr.restype = ctypes.c_void_p
        long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        call_proc = user32.CallWindowProcW
        call_proc.restype = ctypes.c_void_p
        call_proc.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p]

        def _proc(h, msg, wparam, lparam):
            old = self._targets.get(int(h))
            try:
                if msg == 0x0020:  # WM_SETCURSOR: 保持箭头
                    user32.SetCursor(user32.LoadCursorW(None, 32512))
                    return 1
                self._handle_tree(int(h), msg, wparam, lparam)
            except Exception:
                pass
            try:
                return call_proc(old, h, msg, wparam, lparam)
            except Exception:
                return 0

        self._new_proc = WNDPROC(_proc)
        hooked = 0
        for hwnd in windows:
            old = long_ptr(ctypes.c_void_p(hwnd), -4,
                           ctypes.cast(self._new_proc, ctypes.c_void_p))
            if old:
                self._targets[hwnd] = ctypes.c_void_p(old)
                hooked += 1
        if not hooked:
            self._new_proc = None
            return False
        self.target = int(self.get_hwnd() or 0)
        self._start_pump()
        log.info("应用输入路由已挂载: %d 个窗口 (frame=0x%X)",
                 hooked, self.target or 0)
        return True

    def _detach_wndproc(self):
        if os.name != "nt" or not self._targets:
            self._targets = {}
            return
        import ctypes
        user32 = ctypes.windll.user32
        long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        long_ptr.restype = ctypes.c_void_p
        long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        for hwnd, old in list(self._targets.items()):
            try:
                if user32.IsWindow(ctypes.c_void_p(hwnd)):
                    long_ptr(ctypes.c_void_p(hwnd), -4, old)
            except Exception:
                pass
        self._targets = {}
        self._new_proc = None

    _CLICK_MAP = {
        0x0201: "left_down", 0x0202: "left_up",
        0x0204: "right_down", 0x0205: "right_up",
        0x0207: "middle_down", 0x0208: "middle_up",
    }

    def _handle_tree(self, hwnd, msg, wparam, lparam):
        """捕获点击: 既处理自己的按键消息, 也处理子窗口转发上来的 WM_PARENTNOTIFY。"""
        if not self.enabled:
            return
        import ctypes
        from ctypes import wintypes
        import time as _time
        user32 = ctypes.windll.user32
        frame = int(self.get_hwnd() or 0)
        if not frame:
            return
        if msg == 0x0210:                     # WM_PARENTNOTIFY: 子窗口的鼠标按下
            action = self._CLICK_MAP.get(int(wparam or 0) & 0xFFFF)
            if action is None:
                return
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
        else:
            action = self._CLICK_MAP.get(msg)
            if action is None:
                return
            value = int(lparam or 0)
            point = wintypes.POINT(ctypes.c_short(value & 0xFFFF).value,
                                   ctypes.c_short((value >> 16) & 0xFFFF).value)
            if not user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(point)):
                return
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(ctypes.c_void_p(frame), ctypes.byref(origin)):
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(ctypes.c_void_p(frame), ctypes.byref(rect)):
            return
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        cx, cy = int(point.x) - origin.x, int(point.y) - origin.y
        inside = (0 <= cx < width) and (0 <= cy < height)
        if not inside:
            outside = getattr(self, "_outside_logged", 0)
            if outside < 3:
                self._outside_logged = outside + 1
                log.info("[appinput] capture.reject outside action=%s 屏幕=(%d,%d) "
                         "窗口内=(%d,%d) 尺寸=%dx%d",
                         action, point.x, point.y, cx, cy, width, height)
            return
        now = _time.monotonic()
        if action == self._last_emit[1] and now - self._last_emit[0] < 0.025:
            return  # 多个窗口会收到同一个点击, 去重
        self._last_emit = (now, action)
        nx = round(cx / float(width), 4)
        ny = round(cy / float(height), 4)
        self._cap_count = getattr(self, "_cap_count", 0) + 1
        log.info("[appinput] capture #%d %s msg=0x%X hwnd=0x%X 屏幕=(%d,%d) "
                 "窗口内=(%d,%d) 归一=(%.3f,%.3f) 尺寸=%dx%d",
                 self._cap_count, action, msg, int(hwnd), point.x, point.y,
                 cx, cy, nx, ny, width, height)
        self._emit({"kind": "mouse", "action": action,
                    "x": nx, "y": ny, "delta": 0})

    def _handle(self, hwnd, msg, wparam, lparam):
        if not self.enabled:
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        value = int(lparam or 0)
        x = ctypes.c_short(value & 0xFFFF).value
        y = ctypes.c_short((value >> 16) & 0xFFFF).value
        nx = max(0.0, min(1.0, x / float(width)))
        ny = max(0.0, min(1.0, y / float(height)))
        action = None
        if msg == 0x0200:
            action = "move"
        elif msg == 0x0201:
            action = "left_down"
        elif msg == 0x0202:
            action = "left_up"
        elif msg == 0x0204:
            action = "right_down"
        elif msg == 0x0205:
            action = "right_up"
        elif msg == 0x0207:
            action = "middle_down"
        elif msg == 0x0208:
            action = "middle_up"
        elif msg == 0x020A:
            action = "wheel"
        if action:
            delta = ctypes.c_short((int(wparam or 0) >> 16) & 0xFFFF).value
            self._emit({"kind": "mouse", "action": action,
                        "x": round(nx, 4), "y": round(ny, 4), "delta": delta})
            return
        if msg in (0x0100, 0x0101):
            scan = (int(lparam or 0) >> 16) & 0xFF
            self._emit({"kind": "key", "scan": scan, "down": msg == 0x0100})

    def _emit(self, event):
        # WndProc 可能跑在 libvlc 自己的线程里, 这里只能入线程安全队列
        self._queue.put(event)

    def _start_pump(self):
        if self._pump_alive:
            return
        self._pump_alive = True
        self._pump()

    def _pump(self):
        if not self._pump_alive:
            return
        sent = 0
        while sent < 80:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            sent += 1
            try:
                self.on_event(event)
            except Exception:
                pass
        try:
            self._pump_job = self.root.after(8, self._pump)
        except Exception:
            self._pump_alive = False

    def _stop_pump(self):
        self._pump_alive = False
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except Exception:
                pass
            self._pump_job = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def detach(self):
        self._stop_pump()
        self._poll_alive = False
        if getattr(self, "_poll_job", None) is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if os.name == "nt" and self._hook is not None:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
            except Exception:
                pass
            self._hook = None
            self._hook_proc = None
        self._detach_wndproc()
        self.target = None
        self._old_proc = None
        log.info("[appinput] poll.stop")


class AppShareWindow(tk.Toplevel):
    """客户端: 独立窗口显示共享的应用画面, 像本机运行的程序一样。"""

    ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)

    def __init__(self, master, title, stream_w, stream_h, zoom=1.0, on_close=None,
                 label_text=None):
        super().__init__(master)
        self.configure(bg=C.BG)
        self.title(tr("应用共享") + " · " + (title or "?"))
        self.stream_w = max(2, int(stream_w or 1280))
        self.stream_h = max(2, int(stream_h or 720))
        self.zoom = float(zoom)
        self._on_close = on_close
        self._closed = False
        header = tk.Frame(self, bg=C.BG)
        header.pack(fill=tk.X, padx=10, pady=(8, 6))
        # 标题栏里已经有窗口名了, 这里默认不再重复(只显示通用文字)
        tk.Label(header, text=label_text or title or tr("应用共享"), bg=C.BG,
                 fg=C.TEXT, font=ui_font(10, True), anchor="w").pack(side=tk.LEFT)
        self.zoom_label = tk.Label(header, text="100%", bg=C.BG, fg=C.TEXT_MUTED,
                                   font=ui_font(9), width=6, anchor="center")
        self.zoom_label.pack(side=tk.RIGHT, padx=(6, 8))
        RoundedButton(header, text="+", variant="ghost", width=30, height=26,
                      radius=7, bg=C.BG, font=ui_font(12),
                      command=lambda: self.step_zoom(1)).pack(side=tk.RIGHT)
        RoundedButton(header, text="−", variant="ghost", width=30, height=26,
                      radius=7, bg=C.BG, font=ui_font(12),
                      command=lambda: self.step_zoom(-1)).pack(side=tk.RIGHT, padx=(0, 4))
        self.video_frame = tk.Frame(self, bg="#000000", highlightthickness=0)
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        # 画面是固定像素的帧: 允许用户自己拉窗口会把画面拉变形, 直接锁死尺寸
        try:
            self.resizable(False, False)
        except tk.TclError:
            pass
        self.apply_zoom(announce=False)

    def _fit_limits(self):
        try:
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
        except tk.TclError:
            screen_w, screen_h = 1920, 1080
        return screen_w, screen_h

    def apply_zoom(self, announce=True):
        width = int(self.stream_w * self.zoom)
        height = int(self.stream_h * self.zoom)
        screen_w, screen_h = self._fit_limits()
        # 帧直传时画面是固定像素, 窗口被压小就会裁掉画面;
        # 这里只留一个很宽松的上限, 保证放大时窗口真的能跟着变大。
        limit_w = max(screen_w - 80, int(screen_w * 1.6))
        limit_h = max(screen_h - 120, int(screen_h * 1.6))
        width = max(320, min(width, limit_w))
        height = max(200, min(height, limit_h))
        self.geometry(f"{width}x{height + 44}")
        self.zoom_label.configure(text="%d%%" % int(round(self.zoom * 100)))
        if announce:
            log.info("应用共享窗口缩放: %d%% (%dx%d)",
                     int(round(self.zoom * 100)), width, height)

    def step_zoom(self, direction):
        steps = list(self.ZOOM_STEPS)
        current = min(range(len(steps)), key=lambda i: abs(steps[i] - self.zoom))
        target = max(0, min(len(steps) - 1, current + direction))
        if steps[target] == self.zoom and target == current:
            return
        self.zoom = steps[target]
        self.apply_zoom()

    def _request_close(self):
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            self._on_close()
        try:
            self.destroy()
        except tk.TclError:
            pass
