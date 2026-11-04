"""views 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403
from np_core import _circle_photo, _glow_photo  # noqa: F401

from np_net import *  # noqa: F401,F403

from np_widgets import *  # noqa: F401,F403

from np_web import *  # noqa: F401,F403

from np_appshare import *  # noqa: F401,F403

from np_dialogs import *  # noqa: F401,F403

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
        self.count_label.configure(text=tr(f"{len(self._messages)} 条"))
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
        self.count_label.configure(text=tr(f"{len(self._messages)} 条")
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
                    text=tr(f"{self._unread} 条新消息 ↓"))
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
            cx, size * 0.82, text=tr("缓冲中…"), fill="#FFFFFF",
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
            title = tr("正在连接主持…")
        elif phase == "reconnecting":
            title = tr("与主持失去连接 · 自动重连中…")
        else:
            title = info.get("name") or tr("等待主持开始播放")
        self.create_text(cx, y0 + 122, text=title, fill=C.TEXT,
                         font=ui_font(15, True), width=card_w - 60)
        if phase == "joined":
            room = info.get("room") or 0
            host = info.get("host") or "?"
            self.create_text(cx, y0 + 156, text=tr(f"与主持同步播放 · 房间 {room} 人 · 主持 {host}"),
                             fill=C.TEXT_MUTED, font=ui_font(10))
        self.create_text(cx, y0 + 186, text=tr("画面仅主持端渲染 · 本机只同步状态与控制"),
                         fill=C.TEXT_DIM, font=ui_font(9))
        if phase == "joined":
            self.create_text(cx, y0 + 214,
                             text=tr("客户端直接渲染主持推流 · 控制指令交给主持执行"),
                             fill=C.TEXT_DIM, font=ui_font(9))
        elif phase in ("reconnecting",):
            self.create_text(cx, y0 + 214, text=tr("重连后会自动恢复同步"),
                             fill=C.TEXT_DIM, font=ui_font(9))


# ---------------------------------------------------------------------------
# 主菜单(启动页): 不再一上来就是播放器, 由用户先选要做什么
# ---------------------------------------------------------------------------
class HomeMenuView(tk.Canvas):
    """启动页: 四个大卡片 + 底部设置入口。只有选择"媒体播放"才切到播放器界面。"""

    TILES = (
        ("media", "icon_play", "媒体播放", "打开本地视频，开一个房间一起看"),
        ("app", "icon_broadcast", "应用共享", "把任意窗口分享给别人，可远程操作"),
        ("web", "icon_film", "浏览器同步", "同步网页视频的播放进度"),
        ("join", "icon_arrow_up", "加入房间", "输入主持的地址加入已有会话"),
    )

    def __init__(self, master, on_action=None, bg=C.BG):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self._on_action = on_action
        self._rects = {}
        self._hover = None
        self._status_text = "就绪"
        self._status_level = "idle"
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    # ---------------------------------------------------------------- 绘制
    def _render(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        self._rects = {}
        container_w = min(920.0, max(620.0, width - 120))
        left = (width - container_w) / 2.0
        top = max(36.0, (height - 520.0) / 2.0)

        self.create_text(width / 2.0, top + 8, text="Nobplaypot", anchor="n",
                         fill=C.TEXT, font=ui_font(22, True))
        self.create_text(width / 2.0, top + 46,
                         text=tr("共享影音 · 应用共享 · 一起看"),
                         anchor="n", fill=C.TEXT_MUTED, font=ui_font(10))
        try:
            version = APP_VERSION
        except Exception:
            version = ""
        if version:
            self.create_text(width / 2.0, top + 70, text=str(version), anchor="n",
                             fill=C.TEXT_DIM, font=mono_font(9))

        grid_top = top + 104
        gap = 16.0
        tile_w = (container_w - gap) / 2.0
        tile_h = 128.0
        for index, (key, icon_name, title, desc) in enumerate(self.TILES):
            row, col = divmod(index, 2)
            x0 = left + col * (tile_w + gap)
            y0 = grid_top + row * (tile_h + gap)
            self._draw_tile(key, icon_name, title, desc, x0, y0, x0 + tile_w,
                            y0 + tile_h)

        # 底部: 仅本地播放 / 设置 / 日志 + 状态
        bottom_y = grid_top + 2 * tile_h + gap + 30
        self._draw_small_button("local", tr("仅本地播放"), width / 2.0 - 232, bottom_y)
        self._draw_small_button("settings", tr("设置"), width / 2.0 - 78, bottom_y)
        self._draw_small_button("log", tr("查看日志"), width / 2.0 + 76, bottom_y)
        colors = {"idle": C.TEXT_DIM, "playing": C.ACCENT, "error": C.DANGER,
                  "ok": C.OK}
        self.create_text(width / 2.0, bottom_y + 58, text=tr(self._status_text),
                         fill=colors.get(self._status_level, C.TEXT_DIM),
                         font=ui_font(9))

    def _draw_tile(self, key, icon_name, title, desc, x0, y0, x1, y1):
        hovered = self._hover == key
        fill = mix(C.CARD, C.ACCENT, 0.10) if hovered else C.CARD
        outline = mix(C.STROKE, C.ACCENT, 0.55) if hovered else C.STROKE
        draw_round_rect(self, x0, y0, x1, y1, 18, fill=fill, outline=outline, width=1)
        cx = x0 + 56
        cy = y0 + 52
        circle = _circle_photo(self, 56, mix(fill, C.ACCENT, 0.18),
                               mix(outline, C.ACCENT, 0.40), 1)
        self.create_image(cx - circle.width() / 2.0, cy - circle.height() / 2.0,
                          anchor="nw", image=circle)
        icon = globals().get(icon_name)
        if callable(icon):
            icon(self, cx, cy, 22, C.ACCENT)
        self.create_text(x0 + 96, y0 + 34, text=tr(title), anchor="w",
                         fill=C.TEXT, font=ui_font(13, True))
        self.create_text(x0 + 96, y0 + 62, text=tr(desc), anchor="w",
                         fill=C.TEXT_MUTED, font=ui_font(9), width=x1 - x0 - 112)
        self._rects[key] = (x0, y0, x1, y1)

    def _draw_small_button(self, key, label, x, y):
        width = 140.0
        height = 34.0
        hovered = self._hover == key
        fill = mix(C.CARD, C.ACCENT, 0.16) if hovered else C.CARD
        outline = mix(C.STROKE, C.ACCENT, 0.45) if hovered else C.STROKE
        draw_round_rect(self, x, y, x + width, y + height, 17,
                        fill=fill, outline=outline, width=1)
        self.create_text(x + width / 2.0, y + height / 2.0, text=label,
                         fill=C.TEXT if hovered else C.TEXT_MUTED,
                         font=ui_font(9, True))
        self._rects[key] = (x, y, x + width, y + height)

    # ---------------------------------------------------------------- 交互
    def _hit(self, x, y):
        for key, (x0, y0, x1, y1) in self._rects.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key
        return None

    def _on_motion(self, event):
        key = self._hit(event.x, event.y)
        if key != self._hover:
            self._hover = key
            try:
                self.configure(cursor="hand2" if key else "arrow")
            except tk.TclError:
                pass
            self._render()

    def _on_leave(self, _event):
        if self._hover:
            self._hover = None
            try:
                self.configure(cursor="arrow")
            except tk.TclError:
                pass
            self._render()

    def _on_click(self, event):
        key = self._hit(event.x, event.y)
        if key and self._on_action is not None:
            self._on_action(key)

    def set_status(self, text, level="idle"):
        self._status_text = text
        self._status_level = level
        if self.winfo_exists() and self._ui_visible():
            self._render()

    def _ui_visible(self):
        try:
            return bool(self.winfo_ismapped())
        except tk.TclError:
            return False


# ---------------------------------------------------------------------------
# 应用共享 / 浏览器同步的专属界面(不显示播放器, 只显示同步状态 + 房间信息)
# ---------------------------------------------------------------------------
class ShareSessionView(tk.Canvas):
    """应用共享 / 浏览器同步时的主界面: 状态卡片 + 房间信息 + 操作按钮。"""

    def __init__(self, master, on_members=None, on_action=None, bg=C.VIDEO):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self._on_members = on_members
        self._on_action = on_action
        self._info = {}
        self._rects = {}
        self._hover = None
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def set_state(self, **info):
        self._info.update(info)
        self._render()

    # ---------------------------------------------------------------- 绘制
    def _render(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        self._rects = {}
        info = self._info
        card_w = min(620.0, max(420.0, width - 100))
        card_h = 330.0
        cx = width / 2.0
        cy = height / 2.0
        x0, y0 = cx - card_w / 2, cy - card_h / 2
        x1, y1 = x0 + card_w, y0 + card_h
        draw_round_rect(self, x0, y0, x1, y1, 20, fill=C.CARD, outline=C.STROKE, width=1)
        icon_cy = y0 + 58
        glow = _glow_photo(self, 48, C.ACCENT, max_alpha=0.16)
        self.create_image(cx - glow.width() / 2.0, icon_cy - glow.height() / 2.0,
                          anchor="nw", image=glow)
        circle = _circle_photo(self, 56, mix(C.CARD, C.ACCENT, 0.16),
                               mix(C.STROKE, C.ACCENT, 0.40), 1)
        self.create_image(cx - circle.width() / 2.0, icon_cy - circle.height() / 2.0,
                          anchor="nw", image=circle)
        kind = info.get("kind", "app")
        if kind == "web":
            icon_film(self, cx, icon_cy, 22, C.ACCENT)
        else:
            icon_broadcast(self, cx, icon_cy, 22, C.ACCENT)

        self.create_text(cx, y0 + 104, text=tr(info.get("title") or "共享会话"),
                         fill=C.TEXT, font=ui_font(15, True), width=card_w - 60)
        self.create_text(cx, y0 + 132, text=tr(info.get("subtitle") or ""),
                         fill=C.TEXT_MUTED, font=ui_font(10), width=card_w - 60)

        lines = info.get("lines") or []
        for index, line in enumerate(lines[:4]):
            self.create_text(cx, y0 + 164 + index * 22, text=tr(str(line)),
                             fill=C.TEXT_DIM if index else C.TEXT_MUTED,
                             font=ui_font(9))
        hint = info.get("hint")
        if hint:
            self.create_text(cx, y0 + 164 + 4 * 22, text=hint, fill=C.TEXT_DIM,
                             font=ui_font(9), width=card_w - 60)

        # 底部操作按钮
        actions = [("members", tr("房间成员"))]
        label = info.get("action_label")
        if label:
            actions.append(("action", tr(label)))
        total = len(actions)
        button_w, button_h, gap = 132.0, 36.0, 12.0
        start_x = cx - (total * button_w + (total - 1) * gap) / 2.0
        for index, (key, text) in enumerate(actions):
            bx = start_x + index * (button_w + gap)
            by = y1 - 56
            hovered = self._hover == key
            primary = key == "action"
            fill = mix(C.ACCENT, C.CARD, 0.15) if primary else C.CARD
            if hovered:
                fill = mix(fill, C.ACCENT, 0.25)
            outline = fill if primary else (mix(C.STROKE, C.ACCENT, 0.45)
                                            if hovered else C.STROKE)
            draw_round_rect(self, bx, by, bx + button_w, by + button_h, 18,
                            fill=fill, outline=outline, width=1)
            self.create_text(bx + button_w / 2.0, by + button_h / 2.0, text=text,
                             fill="#0A0D15" if primary else C.TEXT,
                             font=ui_font(10, True))
            self._rects[key] = (bx, by, bx + button_w, by + button_h)

    # ---------------------------------------------------------------- 交互
    def _hit(self, x, y):
        for key, (x0, y0, x1, y1) in self._rects.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key
        return None

    def _on_motion(self, event):
        key = self._hit(event.x, event.y)
        if key != self._hover:
            self._hover = key
            try:
                self.configure(cursor="hand2" if key else "arrow")
            except tk.TclError:
                pass
            self._render()

    def _on_leave(self, _event):
        if self._hover:
            self._hover = None
            try:
                self.configure(cursor="arrow")
            except tk.TclError:
                pass
            self._render()

    def _on_click(self, event):
        key = self._hit(event.x, event.y)
        if key == "members" and self._on_members is not None:
            self._on_members()
        elif key == "action" and self._on_action is not None:
            self._on_action(self._info.get("action_kind") or "stop")


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
