"""dialogs 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403
from np_core import _circle_photo, _walk_translate  # noqa: F401

from np_net import *  # noqa: F401,F403

from np_widgets import *  # noqa: F401,F403

from np_web import *  # noqa: F401,F403

from np_appshare import *  # noqa: F401,F403

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
             width=24, anchor="w").pack(side=tk.LEFT)
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
        self._raw_title = title
        self.title(tr(title))
        self.configure(bg=C.BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self._dialog_width = width
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

        # 子类还会继续加控件, 等构建完再统一翻译一次
        self.after(10, lambda: _walk_translate(self))

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
        # 以设计宽度为基准; 英文比中文长时可适度加宽, 但设上限, 剩余文本折行
        width = max(self._dialog_width,
                    min(self.winfo_reqwidth() + 8, self._dialog_width + 140))
        position_center(self, self.master, width, self.winfo_reqheight())
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

    def __init__(self, master, mode, host_info="", join_info="", web=False):
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
                text=tr("主持: 选择视频媒体或浏览器, 开放指令/流端口\n"
                        "加入: 接收并渲染主持推来的画面(播放控制交给主持执行)"),
                bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w", justify=tk.LEFT,
                wraplength=380,
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
        if web:
            RoundedButton(
                self.body, text=tr("打开网址…"), variant="ghost", width=380, height=38,
                bg=C.BG, command=lambda: self.finish("open_url"), font=ui_font(10),
            ).pack(fill=tk.X, pady=(10, 0))
        self._footer("取消", self.finish, cancel=False)

    def finish(self, result=None):
        # 按钮用它传 "host"/"join"/"stop_host"/"leave"; 取消时传 None
        self.choice = result
        super().finish(result)


class HostModeDialog(_StreamDialogBase):
    """主持流式传输: 先选主持方式(视频媒体 / 浏览器)。"""

    def __init__(self, master):
        super().__init__(master, "主持流式传输", width=440)
        self.choice = None
        tk.Label(self.body, text=tr("主持流式传输"), bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(self.body, text=tr("选择这次要主持的内容类型:"), bg=C.BG,
                 fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
                 wraplength=380).pack(fill=tk.X, pady=(4, 16))
        RoundedButton(
            self.body, text=tr("视频媒体"), variant="primary", width=380, height=48,
            bg=C.BG, command=lambda: self.finish("media"), font=ui_font(11, True),
        ).pack(fill=tk.X, pady=(0, 8))
        tk.Label(self.body, text=tr("把本地视频文件推流给其他设备, 支持暂停/进度/倍速同步"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(0, 14))
        RoundedButton(
            self.body, text=tr("浏览器同步"), variant="subtle", width=380, height=48,
            bg=C.BG, command=lambda: self.finish("web"), font=ui_font(11),
        ).pack(fill=tk.X, pady=(0, 8))
        tk.Label(self.body, text=tr("不推流, 只同步网页地址与播放进度"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(0, 14))
        RoundedButton(
            self.body, text=tr("第三方应用同步"), variant="subtle", width=380, height=48,
            bg=C.BG, command=lambda: self.finish("app"), font=ui_font(11),
        ).pack(fill=tk.X, pady=(0, 8))
        tk.Label(self.body, text=tr("共享一个程序窗口, 房客可以直接操作它"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X)
        self._footer("取消", self.finish, cancel=False)

    def finish(self, result=None):
        self.choice = result
        super().finish(result)


class HostStreamDialog(_StreamDialogBase):
    """主持配置: 指令端口 + 流端口 + 房间密码。"""

    def __init__(self, master, cmd_port=STREAM_DEFAULT_CMD_PORT,
                 udp_port=STREAM_DEFAULT_UDP_PORT, web=False):
        self._web = bool(web)
        super().__init__(master, "浏览器同步" if web else "主持流式传输", width=460)
        tk.Label(self.body, text=tr("浏览器同步" if web else "主持流式传输"),
                 bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(
            self.body,
            text=tr("只同步网页地址与播放进度, 不推媒体流; 仅需开放 TCP 指令端口。")
            if web else
            tr("开放 TCP 指令端口与 UDP 媒体端口; 客户端向媒体端口注册后, 主持端把转码流原包转发给它。"),
            bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w", justify=tk.LEFT,
            wraplength=380,
        ).pack(fill=tk.X, pady=(4, 12))
        self.cmd_var = tk.StringVar(value=str(cmd_port))
        self.udp_var = tk.StringVar(value=str(udp_port))
        _stream_field_row(self.body, tr("指令端口"), self.cmd_var)
        if not web:
            _stream_field_row(self.body, tr("媒体端口（主持监听）"), self.udp_var)

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
                 font=ui_font(9), width=24, anchor="w").pack(side=tk.LEFT)
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
        self._footer("开始同步" if self._web else "开始主持", self._confirm)

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
            self.show_error(tr("端口必须是 1-65535 之间的数字"))
            return
        if not (1 <= cmd_port <= 65535):
            self.show_error(tr("端口必须是 1-65535 之间的数字"))
            return
        if not self._web and not (1 <= udp_port <= 65535):
            self.show_error(tr("端口必须是 1-65535 之间的数字"))
            return
        if not self._web and cmd_port == udp_port:
            self.show_error(tr("指令端口和流端口不能相同"))
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


class AppShareDialog(_StreamDialogBase):
    """第三方应用同步: 选窗口 + 画质 + 捕获方式。"""

    def __init__(self, master, settings=None):
        super().__init__(master, "第三方应用同步", width=460)
        self.settings = dict(settings or {})
        self.windows = enum_app_windows()
        tk.Label(self.body, text=tr("第三方应用同步"), bg=C.BG, fg=C.TEXT,
                 font=ui_font(14, True), anchor="w").pack(fill=tk.X)
        tk.Label(self.body, text=tr("选择一个窗口共享给房客, 画面与操作都会实时同步:"),
                 bg=C.BG, fg=C.TEXT_MUTED, font=ui_font(9), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(4, 10))
        self.listbox = tk.Listbox(
            self.body, height=6, bg=C.SURFACE, fg=C.TEXT, relief=tk.FLAT, bd=0,
            highlightthickness=1, highlightbackground=C.STROKE,
            selectbackground=C.ACCENT_SOFT, selectforeground=C.TEXT,
            font=ui_font(9), activestyle="none", exportselection=False,
        )
        self.listbox.pack(fill=tk.X)
        for hwnd, title, cls, pid, width, height in self.windows:
            self.listbox.insert(tk.END, f"{title}   ({width}×{height})")
        if self.windows:
            self.listbox.selection_set(0)
        else:
            tk.Label(self.body, text=tr("没有找到可共享的窗口"),
                     bg=C.BG, fg=C.TEXT_DIM, font=ui_font(9), anchor="w").pack(
                fill=tk.X, pady=(6, 0))
        self.quality_var = tk.StringVar(value=APP_QUALITY_PRESETS[1][1])
        row = tk.Frame(self.body, bg=C.BG)
        row.pack(fill=tk.X, pady=(12, 0))
        tk.Label(row, text=tr("画质"), bg=C.BG, fg=C.TEXT_MUTED,
                 font=ui_font(9), width=10, anchor="w").pack(side=tk.LEFT)
        Dropdown(row, values=[q[1] for q in APP_QUALITY_PRESETS],
                 variable=self.quality_var, width=250, height=32,
                 bg=C.BG).pack(side=tk.LEFT, padx=(8, 0))
        # 捕获方式: 默认后台捕获(PrintWindow), 关掉就用老版屏幕 BitBlt
        mode = str(self.settings.get("app_capture_mode", "printwindow") or "printwindow")
        self.background_var = tk.BooleanVar(value=(mode != "screen"))
        row2 = tk.Frame(self.body, bg=C.BG)
        row2.pack(fill=tk.X, pady=(10, 0))
        tk.Label(row2, text=tr("后台捕获"), bg=C.BG, fg=C.TEXT_MUTED,
                 font=ui_font(9), width=10, anchor="w").pack(side=tk.LEFT)
        ToggleSwitch(row2, self.background_var, bg=C.BG).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(self.body,
                 text=tr("开启后窗口被遮住、或没有焦点也能抓到完整画面; "
                         "关掉改用屏幕截图(被遮挡会拍到别的窗口)。"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(6, 0))
        # 控制权限不在这里决定: 统一由「房间成员」里对每个成员单独开关
        tk.Label(self.body,
                 text=tr("是否允许操作在「房间成员」里按成员单独授权, 默认只读。"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(10, 0))
        tk.Label(self.body,
                 text=tr("窗口最小化时 Windows 不再合成画面, 任何捕获方式都抓不到。"),
                 bg=C.BG, fg=C.TEXT_DIM, font=ui_font(8), anchor="w",
                 justify=tk.LEFT, wraplength=380).pack(fill=tk.X, pady=(6, 0))
        self._footer("开始共享", self._confirm)

    def _confirm(self):
        selection = self.listbox.curselection()
        if not selection or not self.windows:
            self.show_error(tr("请先选择一个窗口"))
            return
        hwnd, title, cls, pid, width, height = self.windows[selection[0]]
        quality = "std"
        label = self.quality_var.get()
        for key, preset_label, *_rest in APP_QUALITY_PRESETS:
            if preset_label == label:
                quality = key
                break
        capture_mode = "printwindow" if self.background_var.get() else "screen"
        self.finish({"hwnd": hwnd, "title": title, "quality": quality,
                     # 房间本身支持控制, 具体谁能控制由「房间成员」逐个授权
                     "allow_control": True, "capture_mode": capture_mode,
                     "width": width, "height": height})


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
        tk.Label(self.body, text=tr("主持开启了房间密码, 请输入密码后加入。"),
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
        width = max(self._dialog_width,
                    min(self.winfo_reqwidth() + 8, self._dialog_width + 140))
        position_center(self, self.master, width, self.winfo_reqheight())
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
             ("chat", "聊天室"), ("streaming", "流式传输"),
             ("media", "媒体播放"), ("web", "浏览器同步"), ("app", "应用共享"))

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
            "media": self._build_media,
            "web": self._build_web,
            "app": self._build_app,
        }[key]
        builder(self._page_host)
        _walk_translate(self._page_host)
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
        row = self._row(page, "界面语言 / Language",
                        "保存后生效（重启后仍然生效）")
        self._dropdown(row, "language", [
            ("zh", "中文"),
            ("en", "English"),
        ], on_change=lambda: self._on_language_changed(self._vars["language"].get()))
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

    def _on_language_changed(self, value):
        # 只记录选择; 真正生效统一由「保存」触发, 与其它设置项保持一致
        self._draft["language"] = value

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
                         "三种共享方式共用的房间参数; 端口/心跳对所有房间生效。")
        row = self._row(page, "房间人数上限", "包括主持自己, 2-32 人")
        self._slider(row, "max_clients", 2, 32, lambda v: f"{int(round(v))} 人")
        row = self._row(page, "心跳间隔", "指令端口心跳包间隔")
        self._slider(row, "heartbeat", 1.0, 10.0, lambda v: f"{v:.1f} 秒")
        row = self._row(page, "离线判定", "超过这个时间没有心跳就判定客户端离线")
        self._slider(row, "stale", 4.0, 60.0, lambda v: f"{v:.1f} 秒")
        row = self._row(page, "默认指令端口", "主持/加入对话框的默认值")
        self._entry(row, "cmd_port")
        row = self._row(page, "默认媒体端口", "主持端监听的默认 UDP 媒体端口")
        self._entry(row, "udp_port")

    def _build_media(self, page):
        self._page_title(page, "媒体播放",
                         "把本地视频转码推给房客的参数; 修改后在下次重建推流时生效。")
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
        row = self._row(page, "缓冲提示缩放", "画面卡顿时中间转圈图标的相对大小")
        self._slider(row, "buffer_scale", 0.5, 2.5, lambda v: f"{v:.2f} 倍")

    def _build_web(self, page):
        self._page_title(page, "浏览器同步",
                         "同步网页视频的播放进度; 数值越小跟得越紧, 但更吃 CPU。")
        row = self._row(page, "同步轮询间隔", "主持端读取网页播放状态的频率")
        self._slider(row, "web_sync_interval_ms", 100, 1000,
                     lambda v: f"{int(round(v))} ms", width=200)
        row = self._row(page, "允许的进度偏差", "超出这个偏差就强制对齐(seek)")
        self._slider(row, "web_sync_tolerance", 0.2, 2.0,
                     lambda v: f"{v:.2f} 秒", width=200)
        row = self._row(page, "起始网址", "打开内置浏览器时默认访问的地址")
        self._entry(row, "web_start_url")

    def _build_app(self, page):
        self._page_title(page, "应用共享",
                         "第三方窗口共享(帧直传)的画面与操作参数; 控制权在「房间成员」里按人授权。")
        row = self._row(page, "传输方式", "frames = 帧直传(低延迟, 推荐); vlc = 旧的 ffmpeg+VLC")
        self._dropdown(row, "app_share_transport", [
            ("frames", "帧直传 (低延迟)"), ("vlc", "ffmpeg + VLC (旧)"),
        ], width=250)
        row = self._row(page, "捕获方式",
                        "后台捕获(PrintWindow): 被遮挡也能抓到完整画面; "
                        "屏幕截图(BitBlt): 老方式, 窗口不能被遮挡")
        self._dropdown(row, "app_capture_mode", [
            ("printwindow", "后台捕获 (被遮挡也能抓)"),
            ("screen", "屏幕截图 (旧)"),
        ], width=250)
        row = self._row(page, "采集帧率", "越高越跟手, 越吃 CPU 与带宽")
        self._slider(row, "app_frame_fps", 5, 60, lambda v: f"{int(round(v))} fps")
        row = self._row(page, "画面编码", "jpeg 快且小; png 无损但慢很多")
        self._dropdown(row, "app_frame_codec", [
            ("jpeg", "JPEG (推荐)"), ("png", "PNG (无损)"),
        ])
        row = self._row(page, "JPEG 质量", "越低越快、越省带宽, 文字可能发虚")
        self._slider(row, "app_frame_quality", 20, 95, lambda v: f"{int(round(v))}")
        row = self._row(page, "最大抓取宽度",
                        "100% 及缩小时的抓取宽度上限; 放大最多到 200%, 不受此值限制")
        self._slider(row, "app_frame_max_width", 320, 2560,
                     lambda v: f"{int(round(v))} px", width=200)
        row = self._row(page, "操作注入方式",
                        "message 不移动主持端鼠标; sendinput 更兼容但会移动光标")
        self._dropdown(row, "app_input_mode", [
            ("message", "窗口消息 (不动鼠标)"), ("sendinput", "真实输入 (会动鼠标)"),
        ], width=250)
        row = self._row(page, "默认缩放",
                        "房客加入时的显示倍率; 屏幕装不下会自动降到能放下的档位")
        # 与客户端 ± 按钮同一套线性档位, 避免出现 125% 之外的奇怪数值
        self._dropdown(row, "app_zoom", [
            (0.25, "25%"), (0.5, "50%"), (0.75, "75%"), (1.0, "100%"),
            (1.25, "125%"), (1.5, "150%"), (1.75, "175%"), (2.0, "200%"),
        ])

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
                 is_host=False, my_id="", on_toggle_control=None):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=C.BG)
        self.title("房间成员")
        self._on_kick = on_kick
        self._on_toggle_control = on_toggle_control
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
        self.count_label.configure(text=tr(f"共 {len(self._members)} 人"))
        for child in self._inner.winfo_children():
            child.destroy()
        for index, member in enumerate(self._members):
            self._build_row(index, member)
        self._inner.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        _walk_translate(self._inner)
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
            allowed = bool(member.get("control"))
            RoundedButton(
                line,
                text=tr("可控制") if allowed else tr("只读"),
                variant="subtle" if allowed else "ghost",
                width=64, height=26, radius=7, bg=C.SURFACE, font=ui_font(8),
                command=lambda cid=member.get("id"), cur=allowed: self._toggle_control(cid, cur),
                tooltip=tr("允许该房客参与播放控制"),
            ).pack(side=tk.RIGHT, padx=(0, 6))

    def _toggle_control(self, client_id, currently_allowed):
        if client_id and self._on_toggle_control is not None:
            self._on_toggle_control(client_id, not currently_allowed)

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
