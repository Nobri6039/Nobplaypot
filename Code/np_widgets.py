"""widgets 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403
from np_core import _UI_FAMILY, _circle_photo, _glow_photo, _hex_rgb, _png_bytes, _point_in_poly  # noqa: F401

from np_net import *  # noqa: F401,F403

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
        if not animations_enabled():
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
        # 英文文案通常比中文长; 文字超出时自动加宽画布, 避免被裁掉
        if self._text:
            try:
                text_width = self._font.measure(self._text)
            except Exception:
                text_width = 0
            need = (text_width + self._pad_x * 2
                    + (self._icon_size + 7 if self._icon else 0))
            if need > width:
                width = int(need)
                self._cw = width
                try:
                    self.configure(width=width)
                except tk.TclError:
                    pass
        spec = self._spec
        mix_value = self._mix
        if not self._enabled:
            fill = mix(C.SURFACE, C.BG, 0.55) if spec["fill"] != self._canvas_bg else self._canvas_bg
            fg = C.TEXT_DIM
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
        self._enabled = True
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
        if not getattr(self, "_enabled", True):
            return self._render_disabled()
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

    def _render_disabled(self):
        try:
            self.delete("all")
            width = max(1, self.winfo_width())
            height = max(1, self.winfo_height())
            cy = height / 2.0
            self.create_line(self._pad, cy, width - self._pad, cy,
                             fill=mix(C.STROKE, C.BG, 0.45), width=self._track_width,
                             capstyle=tk.ROUND)
        except tk.TclError:
            pass

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

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        try:
            self.configure(cursor="hand2" if self._enabled else "arrow")
        except tk.TclError:
            pass
        self.refresh()

    def _on_press(self, event):
        if not getattr(self, "_enabled", True):
            return
        try:
            seek_trace("ui.slider.press", widget=self.winfo_name(),
                       value=round(float(self.value()), 1), x=int(event.x))
        except Exception:
            pass
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
        try:
            seek_trace("ui.slider.release", widget=self.winfo_name(),
                       value=round(float(self.value()), 1), x=int(event.x))
        except Exception:
            pass
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
        if not animations_enabled():
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
        enabled = getattr(self, "_enabled", True)
        fill = mix(self._canvas_bg, C.SURFACE, max(0.35, mix_value))
        outline = mix(C.STROKE, C.SURFACE_HI, mix_value)
        if not enabled:
            fill = self._canvas_bg
            outline = mix(C.STROKE, C.BG, 0.5)
        self.delete("all")
        draw_round_rect(self, 1, 1, width - 1, height - 1, 9, fill=fill,
                        outline=outline, width=1)
        self.create_text(13, height / 2, text=tr(self._var.get()),
                         fill=C.TEXT if enabled else C.TEXT_DIM,
                         font=self._font, anchor="w")
        icon_chevron(self, width - 14, height / 2, 16,
                     C.TEXT_DIM if not enabled else (C.TEXT_MUTED if not self._hovering else C.TEXT))

    # -------------------------------------------------------------- 弹层
    def _toggle(self):
        if not getattr(self, "_enabled", True):
            return
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
                holder, text=tr(value), anchor="w", padx=14, pady=6,
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
        self._enabled = True
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
        self.create_text(cx, y0 + 148, text=tr("把视频文件拖到这里，或选择本地文件"),
                         fill=C.TEXT_MUTED, font=ui_font(10))
        self.create_text(cx, y0 + 178, text="MP4 · MKV · AVI · MOV · WEBM · TS · RMVB",
                         fill=C.TEXT_DIM, font=ui_font(9))
        button_w, button_h = 146.0, 40.0
        bx0 = cx - button_w / 2
        by0 = y0 + 208
        enabled = getattr(self, "_enabled", True)
        if not enabled:
            fill = mix(C.ACCENT, C.CARD, 0.72)
        else:
            fill = C.ACCENT_HOVER if self._hover_button else C.ACCENT
        draw_round_rect(self, bx0, by0, bx0 + button_w, by0 + button_h, 20,
                        fill=fill, outline=fill, width=1)
        self.create_text(cx, by0 + button_h / 2, text=tr("选择视频文件"),
                         fill="#0A0D15" if enabled else C.TEXT_DIM,
                         font=ui_font(10, True))
        self._button_rect = (bx0, by0, bx0 + button_w, by0 + button_h)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not self._enabled:
            self._hover_button = False
            try:
                self.configure(cursor="arrow")
            except tk.TclError:
                pass
        self._render()

    def _inside_button(self, x, y):
        if not self._button_rect:
            return False
        return self._button_rect[0] <= x <= self._button_rect[2] and \
            self._button_rect[1] <= y <= self._button_rect[3]

    def _on_motion(self, event):
        if not getattr(self, "_enabled", True):
            return
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


def create_root():
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except Exception:
            log.warning("TkinterDnD 初始化失败, 回退到普通 Tk", exc_info=True)
    return tk.Tk()


# ---------------------------------------------------------------------------
# 浏览器同步模式
#
#   * 不推流、不转发画面/音频; 只同步 "打开哪个网址 + 播到第几秒 + 是否在播";
#   * 所有同步指令复用现有 TCP 指令端口: web_state / web_ping / web_pong / web_cmd;
#   * 浏览器窗口由应用拉起(系统 Edge 的 app 模式 + 独立配置目录),
#     通过 CDP 读取/写入页面里 <video> 的状态;
#   * 只面向普通 HTML5 内容, 不处理 DRM。
# ---------------------------------------------------------------------------
