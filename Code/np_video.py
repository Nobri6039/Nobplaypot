"""第三方应用同步: 帧直传(不经过 ffmpeg / VLC)。

链路:  GDI BitBlt 抓客户区  ->  (可选)StretchBlt 缩放  ->  GDI+ PNG 编码
       ->  UDP 分片  ->  客户端重组  ->  Tk PhotoImage 直接显示

设计目标: 端到端 < 40ms。
   抓屏 2ms + 缩放 1ms + PNG 8~15ms + 网络 1ms + Tk 解码 8~19ms + 渲染 3ms
所以默认把宽压到 960(width 越小, 编解码越快)。

对外三个类:
   FrameCapture(w_hwnd, rect, max_width, fps)  -> on_frame(png, w, h, ts)
   FrameSender(port=0)                         -> frame_port / send_frame() / client_count
   FrameViewer(parent, host, port)             -> Tk 里显示帧, 自动 hello/重组/统计
"""
import ctypes
import ctypes.wintypes as wt
import socket
import struct
import threading
import time
import zlib

MAGIC = b"NPF1"
HELLO = b"NPF1HELLO"
HEADER = struct.Struct("<4sBBHIIHHd")     # magic,ver,flags,frag,frame_id,size,len,total,ts
MAX_PAYLOAD = 1200

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# 64 位下句柄必须显式声明为 void*, 否则默认 c_int 会把高 32 位截断(实测 GlobalLock 返回 0x20)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
ole32.CreateStreamOnHGlobal.restype = ctypes.c_long
ole32.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, wt.BOOL,
                                        ctypes.POINTER(ctypes.c_void_p)]
ole32.GetHGlobalFromStream.restype = ctypes.c_long
ole32.GetHGlobalFromStream.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", wt.DWORD), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wt.BOOL), ("SuppressExternalCodecs", wt.BOOL)]


class ImageCodecInfo(ctypes.Structure):
    _fields_ = [("Clsid", GUID), ("FormatID", GUID), ("CodecName", wt.LPWSTR),
                ("DllName", wt.LPWSTR), ("FormatDescription", wt.LPWSTR),
                ("FilenameExtension", wt.LPWSTR), ("MimeType", wt.LPWSTR),
                ("Flags", wt.DWORD), ("Version", wt.DWORD), ("SigCount", wt.DWORD),
                ("SigSize", wt.DWORD), ("SigPattern", ctypes.c_void_p),
                ("SigMask", ctypes.c_void_p)]


_lock = threading.Lock()
_png_clsid = None
_jpeg_clsid = None
_token = ctypes.c_void_p()
# EncoderParameter 的"质量"GUID: {1d5be4b5-fa4a-452d-9cdd-5db35105e7eb}
_QUALITY_GUID = GUID(0x1D5BE4B5, 0xFA4A, 0x452D,
                     (ctypes.c_ubyte * 8)(0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB))


class EncoderParameter(ctypes.Structure):
    _fields_ = [("Guid", GUID), ("NumberOfValues", wt.ULONG),
                ("Type", wt.ULONG), ("Value", ctypes.c_void_p)]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", wt.UINT), ("Parameter", EncoderParameter * 1)]


def gdiplus_init():
    global _png_clsid, _jpeg_clsid
    with _lock:
        if _png_clsid is not None and _jpeg_clsid is not None:
            return
        startup = GdiplusStartupInput(1, None, False, False)
        gdiplus.GdiplusStartup(ctypes.byref(_token), ctypes.byref(startup), None)
        num, size = wt.UINT(0), wt.UINT(0)
        gdiplus.GdipGetImageEncodersSize(ctypes.byref(num), ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        gdiplus.GdipGetImageEncoders(num, size, buf)
        encoders = ctypes.cast(buf, ctypes.POINTER(ImageCodecInfo))
        for index in range(num.value):
            mime = (encoders[index].MimeType or "").lower()
            if _png_clsid is None and "png" in mime:
                _png_clsid = encoders[index].Clsid
            elif _jpeg_clsid is None and ("jpeg" in mime or "jpg" in mime):
                _jpeg_clsid = encoders[index].Clsid
        if _png_clsid is None:
            raise RuntimeError("GDI+ 没有 PNG 编码器")


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def _read_dib_bits(hbitmap, width, height):
    """取 HBITMAP 的 32 位像素(自上而下), 只用于给"画面有没有变"算签名。"""
    hdc = user32.GetDC(None)
    if not hdc:
        return b""
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = int(width)
    info.bmiHeader.biHeight = -int(height)
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(int(width) * int(height) * 4)
    try:
        got = gdi32.GetDIBits(hdc, ctypes.c_void_p(hbitmap), 0, int(height), buf,
                              ctypes.byref(info), 0)
        return buf.raw if got else b""
    finally:
        user32.ReleaseDC(None, hdc)


def encode_hbitmap_png(hbitmap):
    return _encode_hbitmap(hbitmap, "png", 80)


def encode_hbitmap_jpeg(hbitmap, quality=70):
    """HBITMAP -> JPEG bytes(比 PNG 快 3~5 倍, 体积小一半以上)。"""
    return _encode_hbitmap(hbitmap, "jpeg", quality)


def _encode_hbitmap(hbitmap, codec, quality):
    gdiplus_init()
    clsid = _jpeg_clsid if codec == "jpeg" else _png_clsid
    if clsid is None:
        clsid = _png_clsid
        codec = "png"
    bitmap = ctypes.c_void_p()
    if gdiplus.GdipCreateBitmapFromHBITMAP(ctypes.c_void_p(hbitmap), None,
                                           ctypes.byref(bitmap)) != 0:
        return b""
    try:
        stream = ctypes.c_void_p()
        ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
        params = None
        params_ptr = None
        if codec == "jpeg":
            value = ctypes.c_long(max(10, min(95, int(quality))))
            params = EncoderParameters()
            params.Count = 1
            params.Parameter[0].Guid = _QUALITY_GUID
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4      # EncoderParameterValueTypeLong
            params.Parameter[0].Value = ctypes.cast(ctypes.byref(value), ctypes.c_void_p)
            params_ptr = ctypes.byref(params)
        status = gdiplus.GdipSaveImageToStream(bitmap, stream, ctypes.byref(clsid),
                                               params_ptr)
        if status != 0:
            return b""
        hglob = ctypes.c_void_p()
        ole32.GetHGlobalFromStream(stream, ctypes.byref(hglob))
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(hglob)
        size = kernel32.GlobalSize(hglob)
        if not ptr:
            return b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(hglob)
    finally:
        gdiplus.GdipDisposeImage(bitmap)


def decode_image_to_hbitmap(data):
    """PNG/JPEG bytes -> HBITMAP(GDI+ 解码, 客户端用, 比 Tk 解码快很多)。"""
    if not data:
        return 0
    gdiplus_init()
    hglob = kernel32.GlobalAlloc(0x0042, len(data))   # GMEM_MOVEABLE|GMEM_ZEROINIT
    if not hglob:
        return 0
    kernel32.GlobalLock.restype = ctypes.c_void_p
    ptr = kernel32.GlobalLock(hglob)
    if not ptr:
        kernel32.GlobalFree(hglob)
        return 0
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(hglob)
    stream = ctypes.c_void_p()
    if ole32.CreateStreamOnHGlobal(hglob, True, ctypes.byref(stream)) != 0:
        return 0
    bitmap = ctypes.c_void_p()
    if gdiplus.GdipCreateBitmapFromStream(stream, ctypes.byref(bitmap)) != 0:
        return 0
    hbitmap = ctypes.c_void_p()
    try:
        if gdiplus.GdipCreateHBITMAPFromBitmap(bitmap, ctypes.byref(hbitmap),
                                               0xFF000000) != 0:
            return 0
        return int(hbitmap.value or 0)
    finally:
        gdiplus.GdipDisposeImage(bitmap)


class FrameCapture:
    """主持端: 抓共享窗口的客户区, 缩放后编码(默认 JPEG)发出去。"""

    def __init__(self, hwnd, rect=None, max_width=960, fps=20, on_frame=None,
                 codec="jpeg", quality=70, capture_mode="printwindow"):
        self.hwnd = int(hwnd or 0)
        self.rect = tuple(rect) if rect else None
        self.max_width = int(max_width or 960)
        self.fps = max(1, int(fps or 20))
        self.codec = codec if codec in ("jpeg", "png") else "jpeg"
        self.quality = int(quality or 70)
        # printwindow: 让窗口自己把内容画进来, 被遮挡/失去焦点也能抓;
        # screen: 老的屏幕 BitBlt, 简单但被盖住就拍到别人
        self.capture_mode = (capture_mode if capture_mode in ("printwindow", "screen")
                             else "printwindow")
        self.fallback_reason = ""
        self._blank_streak = 0
        self._pw_bitmap = 0
        self.skip_unchanged = True
        self.skipped = 0
        self._last_signature = None
        self._sample = None
        self._sample_dc = None
        self._last_sent = 0.0
        self.on_frame = on_frame
        # (x, y, w, h) 相对客户区; None = 整块
        self.crop = None
        self._stop = threading.Event()
        self._thread = None
        self.frames = 0
        self.last_ms = 0.0
        self.last_size = 0
        self.size = (0, 0)

    def set_rect(self, rect):
        if rect:
            self.rect = tuple(rect)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="frame-capture",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def _target_rect(self):
        rect = self.rect
        if not rect:
            r = wt.RECT()
            if not user32.GetClientRect(ctypes.c_void_p(self.hwnd), ctypes.byref(r)):
                return None
            origin = wt.POINT(0, 0)
            user32.ClientToScreen(ctypes.c_void_p(self.hwnd), ctypes.byref(origin))
            rect = (origin.x, origin.y, r.right - r.left, r.bottom - r.top)
        left, top, width, height = rect
        if width <= 0 or height <= 0:
            return None
        return int(left), int(top), int(width), int(height)

    def _grab_png(self, force=False):
        target = self._target_rect()
        if not target:
            return b""
        left, top, width, height = target
        # max_width 小于源宽度 -> 降采样(缩小); 大于源宽度 -> StretchBlt 放大(窗口跟着变大)
        scale = max(0.05, min(3.0, self.max_width / float(width)))
        out_w = max(2, int(width * scale) // 2 * 2)
        out_h = max(2, int(height * scale) // 2 * 2)
        src_left, src_top, src_w, src_h = left, top, width, height
        crop = self.crop
        if crop:
            cx, cy, cw, ch = crop
            src_left = left + max(0, min(int(cx), width - 2))
            src_top = top + max(0, min(int(cy), height - 2))
            src_w = max(2, min(int(cw), width - (src_left - left)))
            src_h = max(2, min(int(ch), height - (src_top - top)))
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        out_mem = gdi32.CreateCompatibleDC(screen)
        try:
            source_dc = screen
            if self.capture_mode == "printwindow":
                picked = self._print_window(screen, mem)
                if picked:
                    source_dc = mem
                    src_left, src_top, src_w, src_h = picked
                else:
                    # 这个窗口不吃 PrintWindow(游戏/部分 Chromium/最小化), 退回屏幕采集
                    self._fallback("PrintWindow 无内容")
            small = gdi32.CreateCompatibleBitmap(screen, out_w, out_h)
            try:
                gdi32.SelectObject(out_mem, small)
                # 1:1 时用 BitBlt(快 3~4ms); 只有需要缩放才走 StretchBlt
                if out_w == src_w and out_h == src_h:
                    ok = gdi32.BitBlt(out_mem, 0, 0, out_w, out_h,
                                      source_dc, src_left, src_top, 0x00CC0020)
                else:
                    gdi32.SetStretchBltMode(out_mem, 0x0003)  # COLORONCOLOR: 比 HALFTONE 快很多
                    ok = gdi32.StretchBlt(out_mem, 0, 0, out_w, out_h,
                                          source_dc, src_left, src_top, src_w, src_h,
                                          0x00CC0020)
                if not ok:
                    return b""
                self.size = (out_w, out_h)
                # 画面没变化就不编码不发送: 桌面大多数时间是静止的,
                # 这一步能把 CPU 和带宽省掉 80% 以上, 变化时也不增加延迟。
                # 指纹从已经抓下来的位图算(内存->内存), 不再二次读屏。
                if self.skip_unchanged and not force:
                    sample = self._sample_signature(out_mem, out_w, out_h)
                    if sample is not None and sample == self._last_signature:
                        self.skipped += 1
                        return b""
                    self._last_signature = sample
                if self.codec == "png":
                    return encode_hbitmap_png(small)
                return encode_hbitmap_jpeg(small, self.quality)
            finally:
                gdi32.DeleteObject(small)
        finally:
            gdi32.DeleteDC(mem)
            gdi32.DeleteDC(out_mem)
            user32.ReleaseDC(None, screen)

    def _fallback(self, reason):
        """PrintWindow 抓不到内容时永久切回屏幕采集(只在第一次记原因)。"""
        if self.capture_mode != "printwindow":
            return
        self.capture_mode = "screen"
        self.fallback_reason = reason

    def _print_window(self, screen, mem):
        """让窗口把内容画进 mem, 返回客户区在 mem 里的 (x, y, w, h)。

        先用 0(WM_PRINT, 内容最全), 画出来是纯色就再试 PW_RENDERFULLCONTENT(2,
        走 DWM 表面, 对遮挡最稳); 两种都拿不到内容就返回 None 让调用方退回屏幕采集。
        注意: 跨进程调用会被 UIPI 拦截(低完整性进程抓不到中完整性窗口), 这时也会返回 None。
        """
        try:
            if not self.hwnd:
                return None
            wr = wt.RECT()
            cr = wt.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(wr)):
                return None
            if not user32.GetClientRect(ctypes.c_void_p(self.hwnd), ctypes.byref(cr)):
                return None
            win_w = max(1, wr.right - wr.left)
            win_h = max(1, wr.bottom - wr.top)
            cli_w = max(4, cr.right - cr.left)
            cli_h = max(4, cr.bottom - cr.top)
            origin = wt.POINT(0, 0)
            user32.ClientToScreen(ctypes.c_void_p(self.hwnd), ctypes.byref(origin))
            off_x = max(0, int(origin.x) - int(wr.left))
            off_y = max(0, int(origin.y) - int(wr.top))
            if off_x + cli_w > win_w:
                cli_w = max(4, win_w - off_x)
            if off_y + cli_h > win_h:
                cli_h = max(4, win_h - off_y)
            if self._pw_bitmap:
                gdi32.DeleteObject(ctypes.c_void_p(self._pw_bitmap))
                self._pw_bitmap = 0
            self._pw_bitmap = gdi32.CreateCompatibleBitmap(screen, win_w, win_h)
            if not self._pw_bitmap:
                return None
            gdi32.SelectObject(mem, ctypes.c_void_p(self._pw_bitmap))
            for flag in (0x0, 0x2):
                gdi32.PatBlt(mem, 0, 0, win_w, win_h, 0x00FF0062)  # 先刷白, 免得拿到脏数据
                if not user32.PrintWindow(ctypes.c_void_p(self.hwnd), mem, flag):
                    continue
                if not self._region_is_flat(mem, off_x, off_y, cli_w, cli_h):
                    return off_x, off_y, cli_w, cli_h
            return None
        except Exception:
            return None

    def _region_is_flat(self, dc, x, y, width, height):
        """抽查客户区 9 个点, 全同色说明 PrintWindow 没画出内容(全白/全黑)。"""
        try:
            first = gdi32.GetPixel(dc, x + 2, y + 2)
            if first == 0xFFFFFFFF:
                return False
            for fx, fy in ((0.5, 0.5), (0.8, 0.8), (0.2, 0.8), (0.8, 0.2),
                           (0.5, 0.2), (0.5, 0.8), (0.2, 0.5), (0.8, 0.5)):
                px = gdi32.GetPixel(dc, x + int(width * fx), y + int(height * fy))
                if px != first:
                    return False
            return True
        except Exception:
            return False

    def _sample_signature(self, source_dc, width, height):
        """把已经抓下来的画面缩到 160x90 算 CRC(纯内存操作, 不读屏)。

        用来判断"这一帧和上一帧是否一样", 相同就跳过编码和发送。
        """
        try:
            if self._sample is None:
                screen = user32.GetDC(None)
                self._sample = gdi32.CreateCompatibleBitmap(screen, 160, 90)
                self._sample_dc = gdi32.CreateCompatibleDC(screen)
                gdi32.SelectObject(self._sample_dc, self._sample)
                user32.ReleaseDC(None, screen)
            gdi32.SetStretchBltMode(self._sample_dc, 0x0003)
            if not gdi32.StretchBlt(self._sample_dc, 0, 0, 160, 90,
                                    source_dc, 0, 0, width, height, 0x00CC0020):
                return None
            data = _read_dib_bits(self._sample, 160, 90)
            if not data:
                return None
            return zlib.crc32(data)
        except Exception:
            return None

    def _loop(self):
        interval = 1.0 / self.fps
        next_at = time.perf_counter()
        while not self._stop.is_set():
            next_at += interval
            t0 = time.perf_counter()
            try:
                # 画面静止时跳过编码; 但每 1 秒强制发一帧, 新加入的客户端才有画面
                force = (time.perf_counter() - self._last_sent) > 1.0
                png = self._grab_png(force=force)
            except Exception:
                png = b""
            if png:
                self._last_sent = time.perf_counter()
                self.frames += 1
                self.last_ms = (time.perf_counter() - t0) * 1000.0
                self.last_size = len(png)
                if self.on_frame is not None:
                    try:
                        self.on_frame(png, self.size[0], self.size[1], t0)
                    except Exception:
                        pass
            delay = next_at - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = time.perf_counter()


class FrameSender:
    """主持端: 收客户端 hello, 把帧分片通过 UDP 发过去。"""

    def __init__(self, port=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.sock.bind(("0.0.0.0", int(port or 0)))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._clients = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.frame_id = 0
        self.sent_frames = 0

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._hello_loop,
                                            name="frame-sender", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.sock.close()
        except OSError:
            pass

    def client_count(self):
        with self._lock:
            cutoff = time.time() - 5.0
            return sum(1 for addr, ts in self._clients.items() if ts >= cutoff)

    def _hello_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(HELLO):
                with self._lock:
                    self._clients[addr] = time.time()

    def send_frame(self, png, ts):
        if not png:
            return 0
        with self._lock:
            now = time.time()
            targets = [addr for addr, seen in self._clients.items() if now - seen < 5.0]
        if not targets:
            return 0
        self.frame_id += 1
        total = (len(png) + MAX_PAYLOAD - 1) // MAX_PAYLOAD
        for index in range(total):
            chunk = png[index * MAX_PAYLOAD:(index + 1) * MAX_PAYLOAD]
            header = HEADER.pack(MAGIC, 1, 0, index, self.frame_id, len(png),
                                 len(chunk), total, ts)
            packet = header + chunk
            for addr in targets:
                try:
                    self.sock.sendto(packet, addr)
                except OSError:
                    pass
        self.sent_frames += 1
        return len(targets)


class FrameViewer:
    """客户端: 收帧 -> Tk 显示(不需要 VLC)。"""

    def __init__(self, parent, host, port, on_stats=None, bg="#000000",
                 on_size=None):
        import tkinter as tk
        self.parent = parent
        self.host = str(host)
        self.port = int(port)
        self.on_stats = on_stats
        self.on_size = on_size
        self.size = (0, 0)
        self.label = tk.Label(parent, bg=bg, text="等待画面…", fg="#888")
        self.label.pack(fill=tk.BOTH, expand=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.05)
        self._hello = HELLO
        self._frames = {}
        self._photo = None
        self._stats = []
        self._closed = False
        self._last_hello = 0.0
        self._job = None
        self._tick()

    def close(self):
        self._closed = True
        if self._job is not None:
            try:
                self.parent.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.label.destroy()
        except Exception:
            pass

    def _tick(self):
        if self._closed:
            return
        import tkinter as tk
        now = time.perf_counter()
        if now - self._last_hello > 1.0:
            try:
                self.sock.sendto(self._hello, (self.host, self.port))
            except OSError:
                pass
            self._last_hello = now
        try:
            self._drain(max_frames=2)
        except Exception:
            pass
        try:
            self._job = self.parent.after(8, self._tick)
        except tk.TclError:
            self._closed = True

    def _drain(self, max_frames=2):
        import base64
        import tkinter as tk
        got = 0
        while got < max_frames:
            try:
                packet = self.sock.recv(65535)
            except (socket.timeout, OSError):
                break
            if len(packet) < HEADER.size:
                continue
            magic, ver, flags, frag, frame_id, size, plen, total, ts = \
                HEADER.unpack(packet[:HEADER.size])
            if magic != MAGIC or frag >= total:
                continue
            slot = self._frames.get(frame_id)
            if slot is None:
                slot = self._frames[frame_id] = [bytearray(size), [False] * total, 0]
            if not slot[1][frag]:
                payload = packet[HEADER.size:HEADER.size + plen]
                slot[0][frag * MAX_PAYLOAD:frag * MAX_PAYLOAD + len(payload)] = payload
                slot[1][frag] = True
                slot[2] += 1
            if slot[2] < total:
                continue
            png = bytes(slot[0])
            del self._frames[frame_id]
            t_recv = time.perf_counter()
            try:
                img = tk.PhotoImage(data=base64.b64encode(png))
                self.label.configure(image=img, text="")
                self.label.image = img
                self._photo = img
            except Exception:
                continue
            size = (int(img.width()), int(img.height()))
            if size != self.size:
                self.size = size
                if self.on_size is not None:
                    try:
                        self.on_size(size[0], size[1])
                    except Exception:
                        pass
            latency = (t_recv - ts) * 1000.0
            self._stats.append((latency, len(png), (t_recv - ts) * 1000.0))
            if len(self._stats) >= 40:
                lat = [s[0] for s in self._stats]
                sizes = [s[1] for s in self._stats]
                if self.on_stats is not None:
                    try:
                        self.on_stats(sum(lat) / len(lat), max(lat),
                                      sum(sizes) / len(sizes))
                    except Exception:
                        pass
                self._stats.clear()
            got += 1
            if len(self._frames) > 20:
                self._frames.clear()


class FramePlayer:
    """客户端: 收帧 -> GDI+ 解码 -> GDI StretchBlt 直接画到 Tk 控件上。

    比 FrameViewer(Tk PhotoImage) 快很多: 不用构造 base64 字符串, 也不用 Tk
    的图片解析器; 解码+贴图大约 2~4ms。
    """

    def __init__(self, widget, host, port, on_stats=None, on_size=None):
        import tkinter as tk
        self.widget = widget
        self.host = str(host)
        self.port = int(port)
        self.on_stats = on_stats
        self.on_size = on_size
        self.size = (0, 0)
        self.hwnd = int(widget.winfo_id())
        # Tk 的 Frame 会自己重绘背景, 直接把像素贴上去会被覆盖;
        # 所以再挂一个纯 Win32 子窗口, 由我们独占它的绘制。
        self._child = 0
        self._frames = {}
        self._ready = []          # 已收齐但还没解码的帧: 只解最新那一帧, 不排队
        self._stats = []
        self._closed = False
        self._last_hello = 0.0
        self._job = None
        self._hbitmap = 0
        self._bmp_w = 0
        self._bmp_h = 0
        self._drawn = 0
        # 分阶段耗时(ms): wire=发送->收齐, decode=解码, blit=贴图
        self.stage = {"wire": 0.0, "decode": 0.0, "blit": 0.0}
        # 复用 HDC / 内存 DC / 收包缓冲: 每帧重复创建销毁会多出好几毫秒
        self._hdc = None
        self._memdc = None
        self._hdc_target = 0
        self._buf = bytearray(65536)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(("0.0.0.0", 0))
        # 非阻塞: 收包循环"收到没有为止", 不会卡在 50ms 超时里拖慢 Tk 主循环
        self.sock.setblocking(False)
        try:
            widget.bind("<Configure>", lambda e: self._layout(), add="+")
        except tk.TclError:
            pass
        self._layout()
        self._tick()

    def _layout(self):
        """把自建的子窗口铺满 Tk 控件。"""
        try:
            if not self._child:
                # WS_EX_TRANSPARENT(0x20): 让鼠标事件穿透这个绘制子窗口,
                # 否则 Tk 收不到鼠标移动/点击(悬停、拖拽、点击全都会失效)。
                self._child = int(user32.CreateWindowExW(
                    0x00000020, "STATIC", "",
                    0x40000000 | 0x10000000 | 0x04000000,
                    0, 0, 16, 16, ctypes.c_void_p(self.hwnd), None, None, None) or 0)
            if not self._child:
                return
            rect = wt.RECT()
            user32.GetClientRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rect))
            user32.MoveWindow(ctypes.c_void_p(self._child), 0, 0,
                              max(1, rect.right - rect.left),
                              max(1, rect.bottom - rect.top), True)
        except Exception:
            pass

    def close(self):
        self._closed = True
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        try:
            self.sock.close()
        except OSError:
            pass
        if self._hbitmap:
            try:
                gdi32.DeleteObject(ctypes.c_void_p(self._hbitmap))
            except Exception:
                pass
            self._hbitmap = 0
        if self._hdc is not None:
            try:
                user32.ReleaseDC(ctypes.c_void_p(self._hdc_target), self._hdc)
                gdi32.DeleteDC(self._memdc)
            except Exception:
                pass
            self._hdc = None
            self._memdc = None

    def _blit(self):
        if not self._hbitmap or self._closed:
            return
        target = self._child or self.hwnd
        rect = wt.RECT()
        if not user32.GetClientRect(ctypes.c_void_p(target), ctypes.byref(rect)):
            return
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0 or self._bmp_w <= 0 or self._bmp_h <= 0:
            return
        # HDC/内存 DC 缓存起来: 每帧重复 GetDC+CreateCompatibleDC 也要好几毫秒
        if self._hdc is None or self._hdc_target != target:
            if self._hdc is not None:
                try:
                    user32.ReleaseDC(ctypes.c_void_p(self._hdc_target), self._hdc)
                    gdi32.DeleteDC(self._memdc)
                except Exception:
                    pass
                self._hdc = None
                self._memdc = None
            hdc = user32.GetDC(ctypes.c_void_p(target))
            if not hdc:
                return
            self._hdc = hdc
            self._memdc = gdi32.CreateCompatibleDC(hdc)
            self._hdc_target = target
        hdc = self._hdc
        mem = self._memdc
        old = gdi32.SelectObject(mem, ctypes.c_void_p(self._hbitmap))
        if abs(width - self._bmp_w) <= 2 and abs(height - self._bmp_h) <= 2:
            # 尺寸基本一致(差 1~2 像素是 DPI 取整造成的)就直接贴, 别为了这点差值
            # 走 StretchBlt: 那会把贴图从 1ms 拖到 12ms。
            gdi32.BitBlt(hdc, 0, 0, width, height, mem, 0, 0, 0x00CC0020)
        else:
            # 客户端本机缩放: 放大比较明显才用 HALFTONE(平滑, 但慢 5~10 倍)
            ratio = max(width / float(self._bmp_w), height / float(self._bmp_h))
            if ratio >= 2.0:
                gdi32.SetStretchBltMode(hdc, 0x0004)   # HALFTONE
            else:
                gdi32.SetStretchBltMode(hdc, 0x0003)   # COLORONCOLOR
            gdi32.StretchBlt(hdc, 0, 0, width, height,
                             mem, 0, 0, self._bmp_w, self._bmp_h, 0x00CC0020)
        gdi32.SelectObject(mem, old)
        self._drawn += 1
        self.stage["dst"] = "%dx%d" % (width, height)
        self.stage["src"] = "%dx%d" % (self._bmp_w, self._bmp_h)

    def _show(self, data):
        t_dec = time.perf_counter()
        hbitmap = decode_image_to_hbitmap(data)
        if not hbitmap:
            return False

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG),
                        ("bmHeight", wt.LONG), ("bmWidthBytes", wt.LONG),
                        ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD),
                        ("bmBits", ctypes.c_void_p)]

        info = BITMAP()
        if gdi32.GetObjectW(ctypes.c_void_p(hbitmap), ctypes.sizeof(info),
                            ctypes.byref(info)) == 0:
            gdi32.DeleteObject(ctypes.c_void_p(hbitmap))
            return False
        old = self._hbitmap
        self._hbitmap = hbitmap
        self._bmp_w, self._bmp_h = int(info.bmWidth), int(info.bmHeight)
        if old:
            gdi32.DeleteObject(ctypes.c_void_p(old))
        self.stage["decode"] = (time.perf_counter() - t_dec) * 1000.0
        t_blit = time.perf_counter()
        self._blit()
        self.stage["blit"] = (time.perf_counter() - t_blit) * 1000.0
        return True

    def _tick(self):
        if self._closed:
            return
        import tkinter as tk
        now = time.perf_counter()
        if now - self._last_hello > 1.0:
            try:
                self.sock.sendto(HELLO, (self.host, self.port))
            except OSError:
                pass
            self._last_hello = now
        try:
            self._drain(max_frames=2)
        except Exception:
            pass
        try:
            self._job = self.widget.after(1, self._tick)
        except tk.TclError:
            self._closed = True

    def _drain(self, max_frames=4):
        buf = self._buf
        packets = 0
        # 1) 先把网卡里的包全部收下来, 收齐的帧进 _ready(不在这里解码)
        while packets < 256:
            try:
                n = self.sock.recv_into(buf)
            except (BlockingIOError, socket.timeout, OSError):
                break
            packets += 1
            if n < HEADER.size:
                continue
            packet = memoryview(buf)[:n]
            magic, ver, flags, frag, frame_id, size, plen, total, ts = \
                HEADER.unpack_from(packet, 0)
            if magic != MAGIC or frag >= total:
                continue
            slot = self._frames.get(frame_id)
            if slot is None:
                slot = self._frames[frame_id] = [bytearray(size), [False] * total, 0]
            if not slot[1][frag]:
                payload = packet[HEADER.size:HEADER.size + plen]
                slot[0][frag * MAX_PAYLOAD:frag * MAX_PAYLOAD + len(payload)] = payload
                slot[1][frag] = True
                slot[2] += 1
            if slot[2] >= total:
                self._ready.append((ts, bytes(slot[0])))
                del self._frames[frame_id]
                # 只保留最新的两帧, 旧的直接扔掉(网络抖动时不会越积越慢)
                if len(self._ready) > 2:
                    del self._ready[:-2]
        if not packets:
            return
        # 2) 只解码并显示最新的一帧: 落后时立刻追到最新画面, 而不是排队播放旧帧
        if not self._ready:
            return
        ts, data = self._ready[-1]
        self._ready.clear()
        t_recv = time.perf_counter()
        if not self._show(data):
            return
        if (self._bmp_w, self._bmp_h) != self.size:
            self.size = (self._bmp_w, self._bmp_h)
            if self.on_size is not None:
                try:
                    self.on_size(self._bmp_w, self._bmp_h)
                except Exception:
                    pass
        latency = (t_recv - ts) * 1000.0
        self.stage["wire"] = latency
        self._stats.append((latency, len(data)))
        if len(self._stats) >= 40:
            lat = [s[0] for s in self._stats]
            sizes = [s[1] for s in self._stats]
            if self.on_stats is not None:
                try:
                    self.on_stats(sum(lat) / len(lat), max(lat),
                                  sum(sizes) / len(sizes))
                except Exception:
                    pass
            self._stats.clear()
        if len(self._frames) > 20:
            self._frames.clear()
