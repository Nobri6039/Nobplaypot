"""app 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

import ctypes

from np_core import *  # noqa: F401,F403
from np_core import _VLC_HOME_DIR, _circle_photo, _norm_url, _walk_translate  # noqa: F401

from np_net import *  # noqa: F401,F403
from np_net import _RelayHttpHandler, _cleanup_relay_dir, _prune_relay_files  # noqa: F401

from np_widgets import *  # noqa: F401,F403

from np_web import *  # noqa: F401,F403
from np_web import _write_start_page  # noqa: F401

from np_appshare import *  # noqa: F401,F403

import np_video  # 帧直传(不经过 ffmpeg / VLC)
import np_web    # 浏览器同步(需要改默认起始网址)

from np_dialogs import *  # noqa: F401,F403

from np_views import *  # noqa: F401,F403

class VideoPlayerApp:
    def __init__(self, root):
        setup_logging()
        install_excepthooks()
        self.root = root
        # 后台线程 → 主线程的 UI 任务队列(Tk 不允许跨线程调用)
        self._ui_queue = []
        self._ui_queue_lock = threading.Lock()
        init_fonts(root)
        self.root.after(40, self._drain_ui_queue)
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
        # 应用共享相关状态先置空, 后台线程/消息处理时不会撞 AttributeError
        self._app_share = None
        self._app_capture = None
        self._app_frames = None
        self._app_sender = None
        self._app_window = None
        self._app_player = None
        self._app_viewer = None
        self._app_controller = ""
        self._app_generation = 0
        self._app_btn_state = 0
        self._app_drag_target = 0
        self._app_input_bound = False
        self._app_text_entry = None
        self._app_text_bar = None
        self._app_input_allowed = False
        self._leaving_to_menu = False

        self._build_ui()
        self._bind_shortcuts()
        self.apply_language(self.settings.get("language", "zh"))
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
        self.home_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=lambda cv, cx, cy, s, col: icon_skip(cv, cx, cy, s, col, -1),
            icon_size=16,
            command=self._leave_to_menu,
            tooltip="结束并返回主菜单",
        )
        self.home_button.pack(side=tk.LEFT, padx=(0, 8))
        self.stream_button = RoundedButton(
            left, variant="icon", width=36, height=36, radius=9, bg=C.DECK,
            icon=icon_broadcast, icon_size=17, command=self.open_stream_menu,
            tooltip="流式传输 (停止 / 退出)",
        )
        # 默认不显示: 现在开房/加入都从主菜单进, 播放器里只在会话进行中
        # 显示这个按钮用来"停止主持 / 退出房间", 避免又出现一个开房入口。
        self._stream_button_visible = False

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
        # 启动页(主菜单): 只有选"媒体播放/打开文件"才切到播放器界面
        self.home_view = HomeMenuView(self.root, on_action=self._on_home_action,
                                      bg=C.BG)
        # 应用共享 / 浏览器同步的专属界面(和聊天面板并排)
        self.share_view = ShareSessionView(
            self.content, on_members=self.open_members,
            on_action=self._share_session_action, bg=C.VIDEO)
        self._ui_mode = "player"
        self._apply_ui_mode("menu")
        self.root.after(500, self._watch_danmaku)
        self.root.after(1000, self._client_watchdog)
        self.root.after(300, self._buffering_tick)

    # --------------------------------------------------------- 主菜单 / 界面模式
    def _apply_ui_mode(self, mode="menu"):
        """menu: 启动页; player: 播放器; share: 应用共享/浏览器同步的专属界面。"""
        target = mode if mode in ("menu", "player", "share") else "menu"
        # 兜底: 任何路径切回主菜单时如果会话还开着, 一律先结束会话,
        # 否则房间留在后台, 主菜单里的功能都会被"请先结束当前会话"挡住。
        if target == "menu" and (getattr(self, "_hosting", False)
                                 or getattr(self, "_client_active", False)) \
                and not getattr(self, "_leaving_to_menu", False):
            log.info("切回主菜单时检测到会话仍在, 自动结束会话")
            self._leave_to_menu()
            return
        self._ui_mode = target
        try:
            if self._ui_mode == "player":
                self.home_view.pack_forget()
                self.share_view.pack_forget()
                self.deck.pack(side=tk.BOTTOM, fill=tk.X)
                self.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
                if not self.video_frame.winfo_ismapped():
                    self.video_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            elif self._ui_mode == "share":
                self.home_view.pack_forget()
                self.deck.pack_forget()
                self.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
                self.video_frame.pack_forget()
                if not self.share_view.winfo_ismapped():
                    self.share_view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                self._sync_chat_panel()
            else:
                self.share_view.pack_forget()
                self.content.pack_forget()
                self.deck.pack_forget()
                self.home_view.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        except tk.TclError:
            return
        self._sync_stream_button()
        log.info("界面模式切换: %s", self._ui_mode)

    def _sync_stream_button(self):
        """流式按钮只在会话进行中显示(用来停止/退出), 平时不出现开房入口。"""
        button = getattr(self, "stream_button", None)
        if button is None:
            return
        active = bool(getattr(self, "_hosting", False)
                      or getattr(self, "_client_active", False))
        if active and not getattr(self, "_stream_button_visible", False):
            # 放在「房间成员」前面: 和左侧按钮组保持同样的间距, 不要贴在文字按钮边上
            anchor = None
            members = getattr(self, "members_button", None)
            try:
                # 用 winfo_manager 判断是否已 pack(此时可能还没真正显示出来)
                if members is not None and members.winfo_manager():
                    anchor = members
            except tk.TclError:
                anchor = None
            # home_button 右侧已有 8px、members_button 左侧也有 8px,
            # 这里不再额外加间距, 三个控件之间才是均匀的
            try:
                if anchor is not None:
                    button.pack(side=tk.LEFT, before=anchor)
                else:
                    button.pack(side=tk.LEFT, before=self.open_button)
            except tk.TclError:
                try:
                    button.pack(side=tk.LEFT)
                except tk.TclError:
                    return
            self._stream_button_visible = True
        elif not active and getattr(self, "_stream_button_visible", False):
            try:
                button.pack_forget()
            except tk.TclError:
                pass
            self._stream_button_visible = False

    def _stop_local_playback(self):
        """停止本地播放并回到空状态(返回主菜单时用)。"""
        try:
            if self.player is not None:
                self.player.stop()
        except Exception:
            log.debug("停止播放器失败", exc_info=True)
        self.current_path = None
        self.duration_ms = 0
        try:
            self.progress_var.set(0.0)
            self.time_label.configure(text="00:00")
            self.duration_label.configure(text="00:00")
            self.watch_view.pack_forget()
            self.drop_hint.pack(fill=tk.BOTH, expand=True)
            self.root.title(APP_TITLE)
            self._update_play_button()
        except tk.TclError:
            pass
        self._bump_stream_epoch("reset")

    def _leave_to_menu(self):
        """结束当前会话/播放, 再回主菜单(否则房间还开着, 菜单里的功能会被挡住)。"""
        if getattr(self, "_leaving_to_menu", False):
            return
        self._leaving_to_menu = True
        log.info("返回主菜单: 结束当前会话 hosting=%s client=%s path=%s",
                 getattr(self, "_hosting", False),
                 getattr(self, "_client_active", False),
                 self.current_path)
        try:
            if getattr(self, "_app_share", None) is not None:
                self.stop_app_share()
        except Exception:
            log.debug("返回主菜单时停止应用共享失败", exc_info=True)
        try:
            if getattr(self, "_hosting", False):
                self.stop_hosting()
        except Exception:
            log.exception("返回主菜单时停止主持失败")
        try:
            if getattr(self, "_client_active", False):
                # 客户端先把独立窗口关掉再退出, 免得留下孤儿窗口
                if getattr(self, "_app_window", None) is not None:
                    self._app_close_window()
                self.leave_stream()
        except Exception:
            log.exception("返回主菜单时退出房间失败")
        try:
            if self.player is not None and self.current_path is not None:
                self._stop_local_playback()
        except Exception:
            log.debug("返回主菜单时复位播放器失败", exc_info=True)
        try:
            self._apply_ui_mode("menu")
        finally:
            self._leaving_to_menu = False

    def _share_session_action(self, kind):
        """共享界面底部按钮: stop = 停止共享/退出房间。"""
        log.info("共享界面操作: %s", kind)
        try:
            if getattr(self, "_hosting", False):
                self.stop_hosting()
            elif getattr(self, "_client_active", False):
                self.leave_stream()
        except Exception:
            log.exception("结束共享会话失败")
        self._apply_ui_mode("menu")

    def _update_share_view(self):
        """把当前共享状态写进专属界面(房主/客户端都会调用)。"""
        view = getattr(self, "share_view", None)
        if view is None:
            return
        if getattr(self, "_hosting", False):
            share = getattr(self, "_app_share", None) or {}
            web = bool(getattr(self, "_web_mode", False))
            frames = getattr(self, "_app_frames", None)
            title = share.get("title") or (tr("浏览器同步") if web else tr("应用共享"))
            kind = "web" if web else "app"
            lines = ["房间 %d 人" % self._room_count(),
                     "指令端口 %s" % getattr(self, "_host_cmd_port", "?")]
            if frames is not None:
                size = frames.size
                lines.append("画面 %dx%d · %.0fKB/帧" % (size[0], size[1],
                                                        frames.last_size / 1024.0))
                lines.append("编码 %.1fms/帧" % frames.last_ms)
            elif web:
                lines.append("同步网址：%s" % (getattr(self, "_web_url", "") or "—"))
            view.set_state(kind=kind, title=title,
                           subtitle=tr("正在共享 · 等待房客加入") if not self._room_count() - 1
                           else tr("正在共享"),
                           lines=lines, hint=tr("房客加入后可在右侧聊天室交流"),
                           action_label=tr("停止共享"), action_kind="stop")
        elif getattr(self, "_client_active", False):
            share = getattr(self, "_app_share", None) or {}
            web = bool(getattr(self, "_web_mode", False))
            room = getattr(self, "_client_room", None) or {}
            title = share.get("title") or (tr("浏览器同步") if web else tr("共享会话"))
            kind = "web" if web else "app"
            viewer = getattr(self, "_app_viewer", None)
            lines = ["房间 %d 人" % int(room.get("count") or 0),
                     "主持 %s" % (room.get("host") or "?")]
            if viewer is not None and getattr(viewer, "size", None):
                lines.append("画面 %dx%d" % viewer.size)
            lines.append(tr("可控制") if getattr(self, "_client_can_control", False)
                         else tr("只读观看"))
            view.set_state(kind=kind, title=title,
                           subtitle=tr("已加入共享会话"),
                           lines=lines, hint=tr("画面在独立窗口中显示"),
                           action_label=tr("退出房间"), action_kind="stop")

    def _on_home_action(self, key):
        log.info("主菜单选择: %s", key)
        if key == "media":
            # 媒体播放 = 先开房(端口/密码), 开好之后再选文件
            self._host_flow("media")
        elif key == "local":
            # 只看不共享: 直接进播放器选文件
            self._apply_ui_mode("player")
            self.open_file_dialog()
        elif key == "app":
            self._host_flow("app")
        elif key == "web":
            self._host_flow("web")
        elif key == "join":
            self._join_flow()
        elif key == "settings":
            self.open_settings()
        elif key == "log":
            self.open_log_folder()

    def _host_flow(self, host_mode):
        """主持流程: media(播放器界面) / app / web(共享界面)。"""
        if getattr(self, "_hosting", False) or getattr(self, "_client_active", False):
            self._alert("无法开始共享", "当前还有会话在进行, 请先点左下角返回主菜单结束会话。")
            return
        if host_mode == "web":
            host_dialog = HostStreamDialog(self.root, *self._last_host_ports, web=True)
            self._track_dialog(host_dialog)
            host_dialog.present()
            if host_dialog.result is None:
                return
            result = host_dialog.result
            self._last_host_ports = (result[0], result[1])
            password = result[2] if len(result) > 2 else ""
            self._apply_ui_mode("menu")
            self.start_web_hosting(result[0], password)
            return
        host_dialog = HostStreamDialog(self.root, *self._last_host_ports)
        self._track_dialog(host_dialog)
        host_dialog.present()
        if host_dialog.result is None:
            return
        result = host_dialog.result
        cmd_port, udp_port = result[0], result[1]
        password = result[2] if len(result) > 2 else ""
        self._last_host_ports = (cmd_port, udp_port)
        if host_mode == "app":
            share_dialog = AppShareDialog(self.root, self.settings)
            self._track_dialog(share_dialog)
            share_dialog.present()
            if share_dialog.result is None:
                return
            self._apply_ui_mode("menu")
            self.start_app_hosting(cmd_port, udp_port, password,
                                   share_dialog.result)
        else:
            self._apply_ui_mode("player")
            if self.start_hosting(cmd_port, udp_port, password):
                # 房间开好了再去挑文件, 选完就是"边播边共享"
                self.root.after(200, self.open_file_dialog)
            else:
                self._apply_ui_mode("menu")

    def _join_flow(self):
        if getattr(self, "_hosting", False) or getattr(self, "_client_active", False):
            self._alert("无法加入", "当前还有会话在进行, 请先点左下角返回主菜单结束会话。")
            return
        join_dialog = JoinStreamDialog(
            self.root,
            cmd_addr=f"{self._last_join_addrs[0]}:{self._last_join_addrs[1]}",
            stream_addr=f"{self._last_join_addrs[2]}:{self._last_join_addrs[3]}",
        )
        self._track_dialog(join_dialog)
        join_dialog.present()
        if join_dialog.result is None:
            return
        cmd_host, cmd_port, udp_host, udp_port = join_dialog.result
        self._last_join_addrs = (cmd_host, cmd_port, udp_host, udp_port)
        self.join_stream(cmd_host, cmd_port, udp_host, udp_port)

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
        _walk_translate(dock)
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
        _walk_translate(chat)
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

    # ------------------------------------------------------ 浏览器同步模式
    def _post_to_ui(self, fn):
        """后台线程不要直接碰 Tk, 统一投递到主线程执行。"""
        lock = getattr(self, "_ui_queue_lock", None)
        if lock is None:
            return
        with lock:
            self._ui_queue.append(fn)

    def _drain_ui_queue(self):
        jobs = []
        lock = getattr(self, "_ui_queue_lock", None)
        if lock is not None:
            with lock:
                if self._ui_queue:
                    jobs = list(self._ui_queue)
                    del self._ui_queue[:]
        for job in jobs:
            try:
                job()
            except Exception:
                log.debug("UI 队列任务执行失败", exc_info=True)
        try:
            self.root.after(40, self._drain_ui_queue)
        except Exception:
            pass

    _WEB_DEFAULTS = {
        "_web_browser": None, "_web_mode": False, "_web_url": "", "_web_tab": "",
        "_web_playing": False, "_web_last_send": 0.0, "_web_tabs_sig": (),
        "_web_tabs_sent": 0.0, "_web_tabs_ready": False, "_web_open_pending": None,
        "_web_tab_map": None, "_web_tab_open_ts": None, "_web_local_active": "",
        "_web_ignore_local_until": 0.0, "_web_duration": 0.0, "_web_pos": 0.0,
        "_web_dragging": False, "_web_rate": 0.0, "_web_hold_until": 0.0,
        "_web_seen_pos": None, "_web_seen_ts": 0.0, "_web_seen_rate": 1.0,
        "_web_seen_playing": False, "_web_rate_latch": None,
        "_web_cmd_guard_until": 0.0, "_client_can_control": False,
        "_web_job": None, "_web_last_note": "", "_web_opening": False, "_web_token": 0,
        "_browser_slot": 0, "_browser_profile": None, "_browser_lock": "",
    }

    def _web_ensure_fields(self):
        for key, value in self._WEB_DEFAULTS.items():
            if not hasattr(self, key):
                setattr(self, key, value)
        for key in ("_web_open_pending", "_web_tab_map", "_web_tab_open_ts"):
            if getattr(self, key, None) is None:
                setattr(self, key, {})

    def _web_open_browser(self, url="", callback=None):
        """异步拉起浏览器: 启动 Edge + 连 CDP 要 1~3 秒, 不能放在 Tk 线程里。"""
        self._web_ensure_fields()
        # 起始网址走设置项(默认 bing), 空值时回落到内置默认
        try:
            start_url = str(self.settings.get("web_start_url") or "").strip()
            if start_url:
                np_web.BROWSER_START_URL = start_url
        except Exception:
            pass
        if self._web_browser is not None:
            if url and url != self._web_url:
                self._web_browser.navigate(url)
            if callback is not None:
                callback(True)
            return self._web_browser
        if self._web_opening:
            return None
        self._web_opening = True
        self._web_token += 1
        token = self._web_token
        if not self._browser_profile:
            slot, profile, lock = acquire_browser_slot()
            self._browser_slot = slot
            self._browser_profile = profile
            self._browser_lock = lock
            log.info("浏览器 profile 槽位: slot-%d (%s)", slot, profile)
        profile_dir = self._browser_profile

        def worker():
            shell = EdgeBrowserShell(profile_dir, lang=self.settings.get("language", "zh"))
            ok = False
            error = ""
            if not shell.available():
                error = "未找到 Microsoft Edge, 浏览器同步模式需要系统安装 Edge。"
            else:
                try:
                    shell.open(url)
                    ok = True
                except Exception as exc:
                    log.exception("内置浏览器启动失败")
                    error = str(exc)

            def finish():
                self._web_opening = False
                if token != self._web_token:
                    # 期间已经退出会话, 丢掉这个浏览器
                    try:
                        shell.close()
                    except Exception:
                        pass
                    return
                if ok:
                    self._web_browser = shell
                    self._web_url = url or ""
                else:
                    log.error("浏览器启动失败: %s", error)
                    self._set_status("浏览器启动失败", "error")
                    self._alert("浏览器启动失败", error)
                if callback is not None:
                    callback(ok)

            try:
                self._post_to_ui(finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="browser-open").start()
        return None

    def set_playback_controls_enabled(self, enabled):
        """浏览器同步模式下: 灰掉主窗口的播放类控件(一切操作都在浏览器里做)。"""
        enabled = bool(enabled)
        widgets = ("play_button", "stop_button", "back_button", "forward_button",
                   "open_button", "track_button", "mute_button", "speed_box",
                   "volume_scale", "progress", "fs_progress", "fs_volume",
                   "drop_hint")
        for name in widgets:
            widget = getattr(self, name, None)
            setter = getattr(widget, "set_enabled", None)
            if setter is None:
                continue
            try:
                setter(enabled)
            except tk.TclError:
                pass
        try:
            self.volume_label.configure(fg=C.TEXT_MUTED if enabled else C.TEXT_DIM)
        except (tk.TclError, AttributeError):
            pass
        log.info("播放控件%s (浏览器同步模式)", "已启用" if enabled else "已灰化")

    def _handle_app_view(self, client_id, message):
        """客户端发来的 app_view。

        现在缩放是客户端本机行为(主持端永远传原画), 所以这里的 zoom 请求
        已经不需要了; 只保留 release(释放控制权) 语义, 老客户端发来的 zoom
        记一条日志后忽略, 免得把全房间的采集尺寸带偏。
        """
        share = getattr(self, "_app_share", None) or {}
        frames = getattr(self, "_app_frames", None)
        if not share or frames is None:
            return
        action = str(message.get("action") or "")
        if action == "release":
            if getattr(self, "_app_controller", "") == client_id:
                self._app_controller = ""
                self._host_server.broadcast({"v": STREAM_VERSION, "type": "app_lease",
                                             "action": "released", "holder": ""})
            return
        if message.get("zoom") or message.get("width"):
            log.info("[lat] app_view 忽略缩放请求(客户端本机缩放): zoom=%s width=%s from=%s",
                     message.get("zoom"), message.get("width"), client_id)

    def _app_button_gate(self, action):
        """按钮状态机: 重复的 down / 无对应的 up 都会被挡掉(不影响真正的双击)。"""
        bits = {"left_down": 1, "left_up": 1, "right_down": 2, "right_up": 2,
                "middle_down": 4, "middle_up": 4}
        bit = bits.get(action, 0)
        if not bit:
            return True
        state = getattr(self, "_app_btn_state", 0)
        if action.endswith("_down"):
            if state & bit:
                return False
            self._app_btn_state = state | bit
            return True
        if not (state & bit):
            return False
        self._app_btn_state = state & ~bit
        return True

    def _handle_app_input(self, client_id, payload):
        """主持端: 收到客户端输入(第 3 段) -> 校验权限 -> 注入(第 4 段)。"""
        share = getattr(self, "_app_share", None)
        action = str(payload.get("action") or "")
        if action != "move":
            now = time.perf_counter()
            captured = float(payload.get("_ts") or 0.0)
            delta = (now - captured) * 1000.0 if captured else -1.0
            log.info("[appinput] host.recv from=%s kind=%s action=%s norm=(%.3f,%.3f) "
                     "t=%.1fms 捕获→收到=%.1fms",
                     client_id, payload.get("kind"), action,
                     float(payload.get("x") or 0.0), float(payload.get("y") or 0.0),
                     now * 1000.0, delta)
        if not share:
            log.info("[appinput] host.reject reason=no-share client=%s", client_id)
            return
        allowed = False
        if self._host_server is not None:
            # 只按「房间成员」里对该成员的授权判断, 不再看创建时的房间级开关
            allowed = self._host_server.can_control(client_id)
        if not allowed:
            log.info("[appinput] host.reject reason=view-only client=%s", client_id)
            seek_trace("host.reject", client=client_id, action="app_input",
                       value=str(payload.get("kind")), reason="view-only")
            return
        controller = getattr(self, "_app_controller", "")
        if controller and controller != client_id:
            log.info("[appinput] host.reject reason=other-controller client=%s holder=%s",
                     client_id, controller)
            seek_trace("host.reject", client=client_id, action="app_input",
                       value=str(payload.get("kind")), reason="other-controller")
            return
        info = {}
        # 去重(主持端兜底): 30ms 内完全相同的一条输入只注入一次
        dedup_key = (str(payload.get("kind") or ""), str(payload.get("action") or ""),
                     payload.get("vk"), payload.get("text"),
                     round(float(payload.get("x") or 0.0), 3),
                     round(float(payload.get("y") or 0.0), 3))
        now_ts = time.perf_counter()
        if payload.get("kind") != "mouse" or payload.get("action") != "move":
            cache = getattr(self, "_app_dedup_host", None)
            if cache is None:
                cache = self._app_dedup_host = {}
            previous = cache.get(dedup_key)
            if previous is not None and (now_ts - previous) < 0.03:
                log.info("[appinput] host.dedup kind=%s action=%s",
                         payload.get("kind"), payload.get("action"))
                return
            cache[dedup_key] = now_ts
            if len(cache) > 32:
                cache.clear()
        # 按键状态机: 已经按下的键又来一次 down, 或者没按下就来 up, 都当作重复丢掉。
        # 真正的双击是 down/up/down/up, 不受影响。
        if payload.get("kind") == "mouse" \
                and not self._app_button_gate(str(payload.get("action") or "")):
            log.info("[appinput] host.dedup reason=button-state action=%s",
                     payload.get("action"))
            return
        # 放大时画面只是窗口的一块子区域, 坐标要按裁剪区域换算, 否则会整体偏
        inject_rect = share.get("rect")
        crop = share.get("crop")
        if inject_rect and crop and len(crop) == 4:
            inject_rect = (inject_rect[0] + int(crop[0]),
                           inject_rect[1] + int(crop[1]),
                           int(crop[2]), int(crop[3]))
        # 按下时记住目标窗口, 拖动/抬起都发给同一个窗口:
        # 拖动过程中光标底下的子窗口可能变, 重新命中会让拖拽中断。
        drag_hint = getattr(self, "_app_drag_target", 0)
        if action.endswith("_down"):
            drag_hint = 0
        elif action.endswith("_up"):
            drag_hint = 0
        ok, detail = inject_app_input(share.get("hwnd"), share.get("width"),
                                      share.get("height"), payload,
                                      rect=inject_rect,
                                      mode=str(self.settings.get("app_input_mode", "sendinput")),
                                      info=info,
                                      target_hint=drag_hint)
        if action.endswith("_down") and ok:
            self._app_drag_target = int(info.get("target") or 0)
        elif action.endswith("_up"):
            self._app_drag_target = 0
        if action != "move":
            self._app_inject_count = getattr(self, "_app_inject_count", 0) + 1
            now = time.perf_counter()
            captured = float(payload.get("_ts") or 0.0)
            total = (now - captured) * 1000.0 if captured else -1.0
            log.info("[appinput] inject #%d %s ok=%s detail=%s target=0x%X "
                     "screen=%s mode=%s hwnd=0x%X t=%.1fms 端到端=%.1fms",
                     self._app_inject_count, action, ok, detail,
                     int(info.get("target") or 0), info.get("screen"),
                     info.get("mode"), int(share.get("hwnd") or 0),
                     now * 1000.0, total)
        if ok:
            if getattr(self, "_app_input_counter", 0) < 4 \
                    or getattr(self, "_app_input_counter", 0) % 30 == 0:
                seek_trace("app.inject", client=client_id, action=payload.get("action"),
                           kind=payload.get("kind"), detail=detail)
            self._app_input_counter = getattr(self, "_app_input_counter", 0) + 1
        else:
            log.warning("[appinput] inject.fail action=%s detail=%s", action, detail)

    def _handle_app_lease(self, client_id, action):
        """控制令牌: 目前实现为「先到先得 + 主持可收回」。"""
        share = getattr(self, "_app_share", None)
        if not share:
            return
        allowed = False
        if self._host_server is not None:
            allowed = self._host_server.can_control(client_id)
        if not allowed:
            return
        if action == "request":
            self._app_controller = client_id
            log.info("应用共享控制权授予: %s", client_id)
            self._host_server.broadcast({
                "v": STREAM_VERSION, "type": "app_lease",
                "action": "granted", "holder": client_id,
            })
            self._set_status("应用共享 · 房客正在控制", "playing")
        elif action == "release":
            if getattr(self, "_app_controller", "") == client_id:
                self._app_controller = ""
                self._host_server.broadcast({
                    "v": STREAM_VERSION, "type": "app_lease",
                    "action": "released", "holder": "",
                })
                self._set_status("应用共享 · 只读观看", "playing")

    def start_app_hosting(self, cmd_port, udp_port, password, share):
        """主持端: 共享一个第三方窗口(帧直传 + 输入注入)。"""
        set_log_role("host")
        log.info("[lat] host.start t=%.1fms window=%r",
                 time.perf_counter() * 1000.0, share.get("title"))
        transport = str(self.settings.get("app_share_transport", "frames")).lower()
        if transport == "frames":
            return self._start_frame_hosting(cmd_port, udp_port, password, share)
        ffmpeg = find_ffmpeg(self.settings.get("ffmpeg_path") or "")
        if not ffmpeg:
            self._alert(
                "缺少组件",
                "应用共享需要 ffmpeg.exe。\n"
                "请把它放到程序目录的 ffmpeg-runtime\\ 下(与 vlc-runtime 同级)后重试。",
            )
            return
        if self._hosting or self._client_active:
            self._alert("无法开始共享", "请先结束当前会话。")
            return
        if not self.start_hosting(cmd_port, udp_port, password):
            return
        relay_port = getattr(self._stream_sender, "relay_port", 0)
        if not relay_port:
            self.stop_hosting()
            self._alert("无法开始共享", "媒体中转未启动, 请检查流端口是否被占用。")
            return
        rect = get_client_rect(share.get("hwnd")) or get_window_rect(share.get("hwnd"))
        if rect:
            log.info("应用共享客户区: %dx%d @ (%d,%d)", rect[2], rect[3], rect[0], rect[1])
        capture = AppCapture(ffmpeg, share["title"], relay_port, share.get("quality", "std"),
                             share.get("width"), share.get("height"), rect=rect)
        try:
            capture.start()
        except Exception as error:
            log.exception("应用共享采集启动失败")
            self.stop_hosting()
            self._alert("无法开始共享", f"采集进程启动失败:\n{error}")
            return
        self._app_capture = capture
        self._app_monitor_stop = threading.Event()
        self._app_share = {
            "title": share["title"], "hwnd": share["hwnd"],
            "allow_control": bool(share.get("allow_control")),
            "quality": share.get("quality", "std"),
            "rect": rect,
            "width": share.get("width", 0), "height": share.get("height", 0),
            "stream_w": capture.width, "stream_h": capture.height,
        }
        self._app_generation = 1
        self._app_controller = ""
        self.set_playback_controls_enabled(False)
        self._set_status(f"应用共享 · {share['title']} · 等待房客加入", "playing")
        self._host_server.broadcast(self._app_offer_message())
        self._app_monitor_stop.clear()
        threading.Thread(target=self._app_capture_monitor, daemon=True,
                         name="app-capture-monitor").start()
        log.info("应用共享开始: %s (允许控制=%s)",
                 share["title"], self._app_share["allow_control"])

    def _start_frame_hosting(self, cmd_port, udp_port, password, share):
        """帧直传模式: GDI 抓屏 + PNG 编码 + UDP 分片, 不依赖 ffmpeg/VLC。"""
        if self._hosting or self._client_active:
            self._alert("无法开始共享", "请先结束当前会话。")
            return
        if not self.start_hosting(cmd_port, udp_port, password):
            return
        rect = get_client_rect(share.get("hwnd")) or get_window_rect(share.get("hwnd"))
        if rect:
            log.info("应用共享客户区: %dx%d @ (%d,%d)", rect[2], rect[3], rect[0], rect[1])
        fps = int(self.settings.get("app_frame_fps", 30) or 30)
        codec = str(self.settings.get("app_frame_codec", "jpeg") or "jpeg")
        quality = int(self.settings.get("app_frame_quality", 70) or 70)
        capture_mode = str(share.get("capture_mode")
                           or self.settings.get("app_capture_mode", "printwindow")
                           or "printwindow")
        if capture_mode not in ("printwindow", "screen"):
            capture_mode = "printwindow"
        base_width = int(rect[2]) if rect else int(share.get("width") or 1280)
        # 100% = 源窗口原始像素 1:1; 缩小时才受「最大抓取宽度」限制, 不能反过来把 100% 也压小
        max_width = max(320, base_width)
        try:
            sender = np_video.FrameSender(0)
            sender.start()
        except Exception as error:
            log.exception("帧发送器启动失败")
            self.stop_hosting()
            self._alert("无法开始共享", f"UDP 端口启动失败:\n{error}")
            return
        capture = np_video.FrameCapture(
            share.get("hwnd"), rect=rect, max_width=max_width, fps=fps,
            codec=codec, quality=quality, capture_mode=capture_mode,
            on_frame=lambda png, w, h, ts: sender.send_frame(png, ts))
        capture.start()
        self._app_frames = capture
        self._app_sender = sender
        self._app_capture = None
        self._app_monitor_stop = threading.Event()
        # 提前算出实际帧尺寸(缩放后偶数取整), 让 app_offer 一开始就带上正确宽高
        stream_w, stream_h = share.get("width", 0), share.get("height", 0)
        if rect and stream_w > 0 and stream_h > 0:
            scale = min(1.0, max_width / float(stream_w))
            stream_w = max(2, int(stream_w * scale) // 2 * 2)
            stream_h = max(2, int(stream_h * scale) // 2 * 2)
        self._app_share = {
            "title": share["title"], "hwnd": share["hwnd"],
            # 房间本身支持控制, 具体谁能控制看「房间成员」里的个人授权
            "allow_control": True,
            "quality": share.get("quality", "std"),
            "rect": rect, "transport": "frames",
            "frame_port": sender.port,
            "base_width": base_width, "view_width": stream_w,
            "out_width": stream_w, "out_height": stream_h,
            "zoom": 1.0, "crop": None,
            "width": share.get("width", 0), "height": share.get("height", 0),
            "stream_w": stream_w, "stream_h": stream_h,
            "capture_mode": capture_mode,
        }
        self._app_generation = 1
        self._app_controller = ""
        self.set_playback_controls_enabled(False)
        self._set_status(f"应用共享 · {share['title']} · 等待房客加入", "playing")
        self._host_server.broadcast(self._app_offer_message())
        self._app_monitor_stop.clear()
        threading.Thread(target=self._app_frame_monitor, daemon=True,
                         name="app-frame-monitor").start()
        # 进专属共享界面(显示同步状态 + 聊天 + 房间信息)
        self._apply_ui_mode("share")
        self._update_share_view()
        log.info("[lat] frames.start fps=%d codec=%s q=%d max_width=%d mode=%s port=%d",
                 fps, codec, quality, max_width, capture_mode, sender.port)

    def _app_frame_monitor(self):
        """帧模式看门狗: 窗口没了就结束房间; 否则每 5 秒打统计并跟随窗口位置。"""
        while not self._app_monitor_stop.is_set():
            if self._app_monitor_stop.wait(2.0):
                return
            capture = getattr(self, "_app_frames", None)
            sender = getattr(self, "_app_sender", None)
            if capture is None or sender is None:
                return
            share = getattr(self, "_app_share", None) or {}
            hwnd = int(share.get("hwnd") or 0)
            if hwnd and not ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)):
                # 被共享的窗口被关掉了: 通知房客并结束房间, 否则房间会一直留着
                log.info("应用共享源窗口已关闭(0x%X), 结束会话", hwnd)
                self._app_close_room("共享的窗口已关闭")
                return
            rect = get_client_rect(hwnd) or get_window_rect(hwnd)
            if rect and capture.rect != rect:
                capture.set_rect(rect)
                share["rect"] = rect
                log.info("应用共享抓取区域更新: %dx%d @ (%d,%d)",
                         rect[2], rect[3], rect[0], rect[1])
            if getattr(self, "_app_stats_tick", 0) >= 2:
                self._app_stats_tick = 0
                self._post_to_ui(self._update_share_view)
                reason = getattr(capture, "fallback_reason", "")
                if reason and not getattr(self, "_app_fallback_logged", False):
                    self._app_fallback_logged = True
                    log.warning("后台捕获不可用(%s), 已切回屏幕采集", reason)
                log.info("[lat] frames.stats 帧=%d 跳过=%d 编码=%.1fms 大小=%.0fKB "
                         "尺寸=%dx%d 客户端=%d 捕获=%s",
                         capture.frames, capture.skipped, capture.last_ms,
                         capture.last_size / 1024.0, capture.size[0], capture.size[1],
                         sender.client_count(), capture.capture_mode)
            else:
                self._app_stats_tick = getattr(self, "_app_stats_tick", 0) + 1

    def _app_close_room(self, reason):
        """被共享的窗口没了: 广播通知 -> 停采集 -> 回主线程结束房间。"""
        try:
            if self._host_server is not None:
                self._host_server.broadcast({
                    "v": STREAM_VERSION, "type": "app_closed", "reason": reason,
                })
        except Exception:
            log.debug("广播共享结束失败", exc_info=True)
        try:
            self.stop_app_share()
        except Exception:
            log.debug("停止应用共享失败", exc_info=True)

        def finish():
            # UI 操作必须回到主线程(看门狗线程里直接碰 Tk 会崩)
            try:
                self._set_status(tr(reason) + " · 会话已结束", "idle")
                if self._hosting:
                    self.stop_hosting()
            except Exception:
                log.exception("结束应用共享会话失败")

        self._post_to_ui(finish)

    def _app_offer_message(self):
        share = getattr(self, "_app_share", None) or {}
        capture = getattr(self, "_app_capture", None)
        frames = getattr(self, "_app_frames", None)
        if frames is not None:
            stream_w, stream_h = frames.size
        else:
            stream_w, stream_h = (capture.stream_size() if capture else (0, 0))
        return {
            "v": STREAM_VERSION, "type": "app_offer",
            "title": share.get("title", ""),
            "allow_control": bool(share.get("allow_control")),
            "transport": share.get("transport", "vlc"),
            "frame_port": share.get("frame_port", 0),
            "base_width": share.get("base_width", 0),
            "view_width": share.get("view_width", 0),
            "zoom": share.get("zoom", 1.0),
            "crop": share.get("crop"),
            "width": share.get("width", 0), "height": share.get("height", 0),
            "stream_w": stream_w, "stream_h": stream_h,
            "generation": int(getattr(self, "_app_generation", 1)),
            "controller": getattr(self, "_app_controller", ""),
        }

    def _app_capture_monitor(self):
        """采集进程看门狗: 退出就提示; 窗口被拖动/改大小时按新位置重启采集。"""
        while not self._app_monitor_stop.is_set():
            capture = getattr(self, "_app_capture", None)
            if capture is None:
                return
            if not capture.is_running():
                detail = " / ".join(capture.last_lines(2)) or "未知原因"
                log.warning("应用共享采集进程结束: %s", detail)
                if self._host_server is not None:
                    self._host_server.broadcast({
                        "v": STREAM_VERSION, "type": "app_error",
                        "code": "capture_failed", "detail": detail[:200],
                    })
                self._set_status("应用共享已停止 · 采集进程退出", "error")
                return
            self._app_follow_window(capture)
            if self._app_monitor_stop.wait(0.6):
                return

    def _app_follow_window(self, capture):
        """源窗口被移动/缩放后, 采集区域要跟着走, 否则画面的位置就废了。"""
        share = getattr(self, "_app_share", None) or {}
        rect = get_client_rect(share.get("hwnd")) or get_window_rect(share.get("hwnd"))
        if not rect or not capture.rect:
            return
        old = capture.rect
        moved = abs(rect[0] - old[0]) > 6 or abs(rect[1] - old[1]) > 6
        resized = abs(rect[2] - old[2]) > 6 or abs(rect[3] - old[3]) > 6
        if not (moved or resized):
            return
        if self._app_monitor_stop.is_set():
            return
        # 抖动去抖: 连续两次读到同一个新矩形才真的重启, 拖动过程中不反复重启
        pending = getattr(self, "_app_pending_rect", None)
        if pending != rect:
            self._app_pending_rect = rect
            return
        self._app_pending_rect = None
        log.info("应用共享源窗口变化: (%d,%d %dx%d) -> (%d,%d %dx%d), 重启采集",
                 old[0], old[1], old[2], old[3],
                 rect[0], rect[1], rect[2], rect[3])
        try:
            capture.stop()
        except Exception:
            pass
        new_capture = AppCapture(capture.ffmpeg, capture.title, capture.relay_port,
                                 share.get("quality", "std"),
                                 rect[2], rect[3], rect=rect)
        try:
            new_capture.start()
        except Exception:
            log.exception("应用共享按新窗口位置重启失败")
            self._app_capture = None
            return
        self._app_capture = new_capture
        share["stream_w"], share["stream_h"] = new_capture.stream_size()
        share["rect"] = rect
        self._app_share = share
        # 采集重启后 TS 是不连续的新流, 客户端必须重建播放器, 否则会停在黑屏
        self._app_generation = int(getattr(self, "_app_generation", 1)) + 1
        if self._host_server is not None:
            self._host_server.broadcast(self._app_offer_message())

    def stop_app_share(self):
        self._app_monitor_stop.set()
        capture = getattr(self, "_app_capture", None)
        if capture is not None:
            capture.stop()
        frames = getattr(self, "_app_frames", None)
        if frames is not None:
            frames.stop()
        sender = getattr(self, "_app_sender", None)
        if sender is not None:
            sender.stop()
        self._app_frames = None
        self._app_sender = None
        self._app_capture = None
        self._app_share = None
        self._app_controller = ""
        self._app_btn_state = 0
        self._app_drag_target = 0

    def start_web_hosting(self, cmd_port, password=""):
        """主持端: 先起同步房间, 浏览器在后台拉起(不阻塞界面)。"""
        self._web_ensure_fields()
        self._web_mode = True
        self._web_url = ""
        self.start_hosting(cmd_port, int(self.settings.get("udp_port", STREAM_DEFAULT_UDP_PORT)),
                           password, web=True)
        if not self._hosting:
            self._web_mode = False
            return
        # 浏览器同步进专属共享界面(不是播放器)
        self._apply_ui_mode("share")
        self._update_share_view()
        self._web_schedule_tick()
        self.set_playback_controls_enabled(False)
        self._set_status("浏览器同步已开始 · 正在打开浏览器…", "ok")
        self._web_open_browser("", callback=self._web_host_browser_ready)

    def _web_host_browser_ready(self, ok):
        if not ok:
            return
        self._set_status("浏览器同步已开始 · 在浏览器窗口里打开视频页面", "ok")

    def _web_schedule_tick(self):
        self._web_ensure_fields()
        if self._web_job is not None:
            try:
                self.root.after_cancel(self._web_job)
            except Exception:
                pass
            self._web_job = None
        self._web_tick()

    def _web_tick(self):
        self._web_ensure_fields()
        self._web_job = None
        now_ts = time.monotonic()
        gap = now_ts - float(getattr(self, "_web_last_tick_ts", now_ts))
        self._web_last_tick_ts = now_ts
        if gap > 3.0:
            log.warning("浏览器同步轮询停顿 %.1f 秒 (界面线程被阻塞?)", gap)
        if self._closing:
            return
        browser = self._web_browser
        if browser is not None and getattr(browser, "dead", False):
            # 用户直接关掉了浏览器窗口 → 自动结束/退出房间
            if self._hosting:
                self._set_status("浏览器已关闭 · 已结束房间", "idle")
                self.stop_hosting()
            else:
                self._set_status("浏览器已关闭 · 已退出房间", "idle")
                self.leave_stream()
            return
        if self._web_mode and self._client_active and browser is not None:
            self._web_report_local_url(browser)
            self._web_detect_local_seek(browser)
        if self._web_mode and self._hosting and browser is not None:
            try:
                state = browser.read_state() or {}
            except Exception:
                state = {}
            url = str(state.get("url") or self._web_url or "")
            tab = browser.attached_id
            playing = bool(state.get("p"))
            now = time.monotonic()
            rate_latch = getattr(self, "_web_rate_latch", None)
            if rate_latch and now < rate_latch[1]:
                if abs(float(state.get("r") or 1.0) - rate_latch[0]) < 0.05:
                    self._web_rate_latch = None
                else:
                    state = dict(state)
                    state["r"] = rate_latch[0]
            elif rate_latch:
                self._web_rate_latch = None
            latch = getattr(self, "_web_seek_latch", None)
            measured = float(state.get("t") or 0.0)
            if latch and now < latch[1]:
                if abs(measured - latch[0]) < 0.8:
                    self._web_seek_latch = None
                else:
                    state = dict(state)
                    state["t"] = latch[0]
                    measured = latch[0]
            elif latch:
                self._web_seek_latch = None
            # 事件优先: 网址/标签页/播放状态一变立刻广播; 进度按 0.5s 周期补发
            changed = (url != self._web_url) or (tab != self._web_tab) \
                or (playing != self._web_playing)
            if url and (changed or (now - self._web_last_send) >= 0.5):
                self._web_url = url
                self._web_tab = tab
                self._web_playing = playing
                self._web_last_send = now
                message = {
                    "v": STREAM_VERSION, "type": "web_state",
                    "url": url, "tab": tab,
                    "pos": float(state.get("t") or 0.0),
                    "playing": playing,
                    "rate": float(state.get("r") or 1.0),
                    "duration": float(state.get("d") or 0.0),
                    "ok": bool(state.get("ok")),
                    "ts": now,
                }
                if self._host_server is not None:
                    self._host_server.broadcast(message)
                # 标签页集合同步: 主持端所有打开的标签页(网址 + 顺序 + 当前页)
                if state.get("ok"):
                    note = "同步中 · %s · %s" % (
                        "播放" if playing else "暂停",
                        format_time(int(float(state.get("t") or 0.0) * 1000)),
                    )
                    if note != self._web_last_note:
                        self._web_last_note = note
                        self._set_status(note, "playing" if playing else "idle")
        interval = int(self.settings.get("web_sync_interval_ms", 200) or 200)
        self._web_job = self.root.after(max(100, min(interval, 1000)), self._web_tick)

    def _web_client_apply(self, message):
        """客户端: 按主持端状态对齐本地浏览器。"""
        self._web_ensure_fields()
        browser = self._web_browser
        if browser is None:
            return
        url = str(message.get("url") or "")
        self._web_tab = str(message.get("tab") or self._web_tab)
        seek_trace("client.recv.state", pos=round(float(message.get("pos") or 0.0), 3),
                   duration=round(float(message.get("duration") or 0.0), 3),
                   rate=message.get("rate"), ok=str(message.get("ok")),
                   holding=str(time.monotonic() < getattr(self, "_web_hold_until", 0.0)),
                   dragging=str(getattr(self, "_web_dragging", False)))
        # 先把时长/进度记下来: 即使这一轮在导航, 进度条也能用
        duration = float(message.get("duration") or 0.0)
        position = float(message.get("pos") or 0.0)
        if duration > 0:
            self._web_duration = duration
        self._web_pos = position
        if url and _norm_url(url) != _norm_url(self._web_url):
            self._web_url = url
            self._web_ignore_local_until = time.monotonic() + 1.0
            last = getattr(self, "_web_last_nav", None)
            now = time.monotonic()
            if last and last[0] == _norm_url(url) and now - last[1] < 3.0:
                return
            self._web_last_nav = (_norm_url(url), now)
            try:
                browser.navigate(url)
            except Exception:
                log.debug("同步导航失败", exc_info=True)
            return
        if not message.get("ok"):
            return
        rate = float(message.get("rate") or 1.0)
        holding = time.monotonic() < getattr(self, "_web_hold_until", 0.0)
        if not holding and abs(rate - float(getattr(self, "_web_rate", 0.0) or 0.0)) > 0.05:
            self._web_ignore_local_until = time.monotonic() + 1.0
            self._web_rate = rate
            setter = getattr(browser, "set_rate", None)
            if setter is not None:
                seek_trace("client.apply.rate", rate=rate)
                try:
                    setter(rate)
                except Exception:
                    log.debug("客户端应用倍速失败", exc_info=True)
        label = ("%.2f" % rate).rstrip("0").rstrip(".") + "x"
        if (not holding) and getattr(self, "speed_var", None) is not None \
                and self.speed_var.get() != label:
            self.speed_var.set(label)
        # 浏览器同步模式: 进度只在网页里体现, 不回写主窗口进度条
        if not getattr(self, "_web_mode", False) and duration > 0 \
                and not getattr(self, "_web_dragging", False) and not holding:
            self.progress_var.set(max(0.0, min(1000.0, position / duration * 1000.0)))
            self.time_label.configure(text=format_time(int(position * 1000)))
            self.duration_label.configure(text=format_time(int(duration * 1000)))
        try:
            state = browser.read_state()
        except Exception:
            return
        if not state or not state.get("ok"):
            return
        target = float(message.get("pos") or 0.0)
        playing = bool(message.get("playing"))
        rate = float(message.get("rate") or 1.0)
        if playing:
            target += max(0.0, time.monotonic() - float(message.get("ts") or time.monotonic()))
        actual = float(state.get("t") or 0.0)
        tolerance = float(self.settings.get("web_sync_tolerance", 0.75) or 0.75)
        need_seek = abs(actual - target) > tolerance
        need_play = playing != bool(state.get("p"))
        need_rate = abs(float(state.get("r") or 1.0) - rate) > 0.05
        if need_seek or need_play or need_rate:
            try:
                browser.apply(target, playing, rate)
            except Exception:
                log.debug("同步应用失败", exc_info=True)

    def _web_client_tabs(self, message):
        """按主持端标签页 id 建立映射: 新的就开, 当前指向的切过去。"""
        self._web_ensure_fields()
        browser = self._web_browser
        if browser is None:
            return
        tabs = message.get("tabs") or []
        if not tabs:
            return
        active_id = str(message.get("active_id") or "")
        alive = {t["id"]: t for t in browser.cached_tabs()}
        # 新开的标签页要过一会儿才会出现在列表里, 不能立刻判死, 否则会重复开
        now = time.monotonic()
        for host_id in list(self._web_tab_map):
            client_id = self._web_tab_map[host_id]
            if client_id in alive:
                continue
            opened_at = self._web_tab_open_ts.get(host_id, 0.0)
            if now - opened_at > 8.0:
                self._web_tab_map.pop(host_id, None)
                self._web_tab_open_ts.pop(host_id, None)
        for tab in tabs:
            host_id = str(tab.get("id") or "")
            url = str(tab.get("url") or "")
            if not host_id or not url:
                continue
            if self._web_tab_map.get(host_id):
                # 已经开过(可能还没出现在列表里), 不要再开第二个
                continue
            new_id = browser.open_tab(url)
            if new_id:
                self._web_tab_map[host_id] = new_id
                self._web_tab_open_ts[host_id] = time.monotonic()
        client_active = self._web_tab_map.get(active_id)
        if client_active:
            self._web_ignore_local_until = time.monotonic() + 1.0
            browser.select_tab(client_active)

    def _web_pending(self, url):
        norm = _norm_url(url)
        stamp = self._web_open_pending.get(norm)
        if stamp is None:
            return False
        if time.monotonic() - stamp > 6.0:
            self._web_open_pending.pop(norm, None)
            return False
        return True

    def _web_mark_pending(self, url):
        self._web_open_pending[_norm_url(url)] = time.monotonic()

    def _web_activate(self, active_url, active_index=0):
        browser = self._web_browser
        if browser is None:
            return
        try:
            tabs = browser.cached_tabs()
        except Exception:
            return
        if not tabs:
            return
        target = _norm_url(active_url)
        for tab in tabs:
            if _norm_url(tab.get("url")) == target:
                browser.activate_tab(tab["id"])
                return
        if 0 <= active_index < len(tabs):
            browser.activate_tab(tabs[active_index]["id"])

    def _web_report_local_active(self, browser):
        """双向同步: 客户端自己切了标签页, 也通知主持端切过去。"""
        if time.monotonic() < self._web_ignore_local_until:
            return
        local = browser.attached_id
        if not local or local == self._web_local_active:
            return
        self._web_local_active = local
        reverse = {cid: hid for hid, cid in self._web_tab_map.items()}
        host_id = reverse.get(local)
        if not host_id:
            return
        self._client_send({"v": STREAM_VERSION, "type": "web_active", "tab": host_id})

    def _web_apply_remote_active(self, tab_id):
        """主持端: 应用房客切过来的标签页。"""
        self._web_ensure_fields()
        browser = self._web_browser
        if browser is None or not tab_id:
            return
        browser.select_tab(str(tab_id))

    def _web_report_local_url(self, browser):
        """双向: 客户端自己换了页面, 也通知主持端跟着换。"""
        if not getattr(self, "_client_can_control", True):
            # 只读房客不参与任何控制, 连网址也不上报, 免得每秒被主持端拒绝
            return
        if time.monotonic() < self._web_ignore_local_until:
            return
        try:
            state = browser.read_state() or {}
        except Exception:
            return
        url = str(state.get("url") or "")
        if not url or _norm_url(url) == _norm_url(self._web_url):
            return
        # 起始页/浏览器内部页不上报, 否则会把本地文件路径推给主持端
        if url.startswith(("about:", "edge:", "chrome:", "chrome-error:", "devtools:")):
            return
        if url.lower().startswith("file:") and "start.html" in url:
            return
        if not self._web_url:
            # 还没跟主持同步过页面, 先不反向上报
            return
        self._web_url = url
        self._client_send({"v": STREAM_VERSION, "type": "web_url", "url": url})

    def _web_detect_local_seek(self, browser):
        if not getattr(self, "_client_can_control", True):
            return
        """房客在浏览器里自己拖动进度 / 改倍速时, 反向通知主持。"""
        try:
            state = browser.read_state() or {}
        except Exception:
            return
        now = time.monotonic()
        if not state.get("ok"):
            self._web_seen_pos = None
            return
        pos = float(state.get("t") or 0.0)
        rate = float(state.get("r") or 1.0)
        playing = bool(state.get("p"))
        if self._web_seen_pos is None:
            self._web_seen_pos, self._web_seen_ts = pos, now
            self._web_seen_rate, self._web_seen_playing = rate, playing
            return
        prev_rate = self._web_seen_rate
        prev_playing = self._web_seen_playing
        elapsed = max(0.0, now - self._web_seen_ts)
        expected = self._web_seen_pos
        if self._web_seen_playing:
            expected += elapsed * self._web_seen_rate
        delta = pos - expected
        self._web_seen_pos, self._web_seen_ts = pos, now
        self._web_seen_rate, self._web_seen_playing = rate, playing
        if now < getattr(self, "_web_ignore_local_until", 0.0):
            return
        # 刚发过指令的 1.2 秒内不再采样: 防止站点延迟上报造成 暂停/播放 来回抖
        if now < getattr(self, "_web_cmd_guard_until", 0.0):
            return
        # 0) 网页里暂停 / 继续
        if prev_playing != playing:
            seek_trace("client.detect.browser_play", playing=str(playing),
                       prev_playing=str(prev_playing))
            self._web_hold_until = now + 1.5
            self._web_cmd_guard_until = now + 1.2
            self._client_send_cmd("play" if playing else "pause", None)
            return
        # 1) 网页里改了倍速
        if abs(rate - prev_rate) > 0.05:
            seek_trace("client.detect.browser_rate", rate=rate, prev_rate=prev_rate,
                       playing=str(playing))
            self._web_hold_until = now + 2.0
            self._web_cmd_guard_until = now + 1.2
            self._client_send_cmd("rate", rate)
            return
        # 2) 网页里拖了进度
        if elapsed > 2.5 or abs(delta) < 1.5 or pos <= 0.2:
            return
        seek_trace("client.detect.browser", pos=round(pos, 3),
                   expected=round(expected, 3), delta=round(delta, 3),
                   rate=rate, playing=str(playing))
        self._web_hold_until = now + 2.0
        self._web_cmd_guard_until = now + 1.2
        self._client_send_cmd("seek", int(round(pos * 1000)))

    def _web_apply_remote_url(self, url):
        """主持端: 房客换了页面, 跟着导航。"""
        self._web_ensure_fields()
        browser = self._web_browser
        if browser is None or not url:
            return
        try:
            browser.navigate(str(url))
        except Exception:
            log.debug("远端页面导航失败", exc_info=True)

    def _web_remote_command(self, action, value):
        """主持端: 应用来自房客的遥控指令(作用于主持自己的浏览器)。"""
        self._web_ensure_fields()
        browser = self._web_browser
        if browser is None:
            return
        try:
            state = browser.read_state() or {}
            position = float(state.get("t") or 0.0)
            playing = bool(state.get("p"))
            rate = float(getattr(self, "_web_rate", 0.0) or state.get("r") or 1.0)
            if action == "toggle":
                playing = not playing
            elif action == "play":
                playing = True
            elif action == "pause":
                playing = False
            elif action == "seek":
                # 客户端发来的是毫秒, 浏览器 currentTime 用秒
                position = max(0.0, float(value or 0.0) / 1000.0)
            elif action == "seek_rel":
                position = max(0.0, position + float(value or 0.0) / 1000.0)
            elif action == "seek_ratio":
                ratio = max(0.0, min(1.0, float(value or 0) / 1000.0))
                total = float(state.get("d") or 0.0)
                if total > 0:
                    position = ratio * total
            elif action == "rate":
                rate = max(0.25, min(4.0, float(value or 1.0)))
                self._web_rate = rate
                self._web_rate_latch = (rate, time.monotonic() + 4.0)
                position = float(getattr(self, "_web_pos", position) or position)
            seek_trace("host.apply", action=action, value=value,
                       position=round(position, 3), playing=str(playing),
                       rate=round(rate, 3), url=str(state.get("url") or "")[-40:])
            if action == "rate":
                # 只改倍速; 同时不再重设 currentTime
                setter = getattr(browser, "set_rate", None)
                if setter is not None:
                    setter(rate)
                else:
                    browser.apply(position, playing, rate)
            else:
                browser.apply(position, playing, rate)
            self._web_seek_latch = (max(0.0, position), time.monotonic() + 3.0)
            self._web_pos = max(0.0, position)
            self._web_playing = bool(playing)
            self._web_rate = rate
            self._web_last_send = 0.0
            if self._web_mode and self._hosting and self._host_server is not None:
                payload = {
                    "v": STREAM_VERSION, "type": "web_state",
                    "url": self._web_url, "tab": self._web_tab,
                    "pos": float(position), "playing": bool(playing),
                    "rate": float(rate),
                    "duration": float(getattr(self, "_web_duration", 0.0) or 0.0),
                    "ok": True, "ts": time.monotonic(),
                }
                self._host_server.broadcast(payload)
                seek_trace("host.broadcast", pos=round(position, 3),
                           rate=round(rate, 3), playing=str(playing),
                           latch=str(getattr(self, "_web_seek_latch", None)))
                log.info("远端遥控已应用并广播: %s %r", action, value)
        except Exception:
            log.debug("远程浏览器控制失败", exc_info=True)

    def _web_client_browser_ready(self, ok):
        if ok:
            self._set_status("已加入浏览器同步房间 · 等待主持端操作", "ok")
            # 客户端也要跑周期任务: 上报自己的网址变化、维持状态
            self._web_schedule_tick()

    def _web_refresh_start_page(self):
        """语言切换后, 如果浏览器还停在本地起始页就重新生成并刷新。"""
        browser = getattr(self, "_web_browser", None)
        if browser is None:
            return
        try:
            state = browser.read_state() or {}
        except Exception:
            return
        url = str(state.get("url") or "")
        if "start.html" not in url:
            return
        path = _write_start_page(self.settings.get("language", "zh"))
        if not path:
            return
        try:
            browser.navigate("file:///" + path.replace("\\", "/"))
        except Exception:
            log.debug("刷新起始页失败", exc_info=True)

    def _web_prompt_url(self):
        """app 模式没有地址栏, 用一个输入框代替。"""
        from tkinter import simpledialog
        browser = self._web_browser
        if browser is None:
            self._alert("浏览器未就绪", "请先开始浏览器同步。")
            return
        text = simpledialog.askstring(tr("打开网址"), tr("输入网址："), parent=self.root)
        if not text or not text.strip():
            return
        text = text.strip()
        if not text.startswith(("http://", "https://", "file://")):
            text = "https://" + text
        browser.navigate(text)
        self._set_status("正在打开 · " + text, "playing")

    def _web_shutdown(self):
        self._web_ensure_fields()
        self._web_token += 1
        if self._web_job is not None:
            try:
                self.root.after_cancel(self._web_job)
            except Exception:
                pass
            self._web_job = None
        if self._web_browser is not None:
            try:
                self._web_browser.close()
            except Exception:
                log.debug("关闭内置浏览器失败", exc_info=True)
            self._web_browser = None
        profile = self._browser_profile
        if profile:
            try:
                kill_profile_edges(profile)
            except Exception:
                log.debug("清理浏览器进程失败", exc_info=True)
        release_browser_slot(self._browser_lock)
        self._browser_profile = None
        self._browser_lock = ""
        self._browser_slot = 0
        self._web_mode = False
        self._web_url = ""
        self._web_tab = ""
        self._web_playing = False
        self._web_last_send = 0.0
        self._web_tabs_sig = ()
        self._web_tabs_sent = 0.0
        self._web_tabs_ready = False
        self._web_open_pending = {}
        self._web_tab_map = {}
        self._web_last_note = ""

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
        dialog = StreamMenuDialog(self.root, mode, host_info=info, join_info=info,
                                  web=getattr(self, "_web_mode", False))
        self._track_dialog(dialog)
        dialog.present()
        choice = dialog.choice
        log.debug("流式菜单选择: %s", choice)
        if choice == "host":
            mode_dialog = HostModeDialog(self.root)
            self._track_dialog(mode_dialog)
            mode_dialog.present()
            host_mode = mode_dialog.choice
            log.debug("主持方式选择: %s", host_mode)
            if host_mode in ("media", "app", "web"):
                self._host_flow(host_mode)
        elif choice == "join":
            self._join_flow()
        elif choice == "open_url":
            self._web_prompt_url()
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
                # 单条消息处理出错也不能影响后续消息
                try:
                    self._client_handle_message(event["message"])
                except Exception:
                    log.exception("处理主持端消息失败")
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
    def start_hosting(self, cmd_port, udp_port, password="", web=False):
        set_log_role("host")
        log.info("[lat] host.start t=%.1fms cmd=%d udp=%d web=%s",
                 time.perf_counter() * 1000.0, cmd_port, udp_port, web)
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
        if web:
            # 浏览器同步不推流: 一个媒体端口都不占, 本机可以同时开主持和客户端
            log.info("浏览器同步模式: 只开指令端口 %d, 不监听媒体端口", cmd_port)
        else:
            try:
                self._stream_sender = UdpMediaSender(udp_port)
                self._stream_sender.start()
                log.info("媒体流监听 0.0.0.0:%d (客户端主动连入), VLC 本地中转 %d",
                         self._stream_sender.port, self._stream_sender.relay_port)
            except OSError as error:
                self._stream_sender = None
                log.warning("媒体中转启动失败: %s", error)
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
        self._sync_stream_button()
        return True

    def stop_hosting(self):
        self.set_playback_controls_enabled(True)
        try:
            if getattr(self, "_app_capture", None) is not None:
                self.stop_app_share()
        except Exception:
            log.debug("停止应用共享失败", exc_info=True)
        self._web_shutdown()
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
                log.exception("停止媒体中转失败")
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
        # 没有在放本地视频时回到主菜单
        if self.current_path is None:
            self._apply_ui_mode("menu")
        self._sync_stream_button()

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
        """VLC sout 的实际目标: 本机 UDP 中转(再由它向已注册客户端原包转发)。"""
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
            if getattr(self, "_web_mode", False):
                room_mode = "web"
            elif getattr(self, "_app_share", None):
                room_mode = "app"
            else:
                room_mode = "media"
            self._host_server.reply(client_id, {
                "v": STREAM_VERSION, "type": "welcome", "client_id": client_id,
                "host": self._host_name(),
                "mode": room_mode,
                "url": getattr(self, "_web_url", ""),
                "control": bool(self._host_server.can_control(client_id)),
            })
            log.info("客户端接入: %s (流目标 %s)", name, stream or "无")
            self._broadcast_room()
            self._rebuild_stream()
            self._host_server.reply(client_id, self._state_message())
            if getattr(self, "_app_share", None):
                # 应用共享中途加入的房客: 补发共享信息, 否则只有画面没有状态
                self._host_server.reply(client_id, self._app_offer_message())
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
            seek_trace("host.recv", client=client_id,
                       action=message.get("action"), value=message.get("value"),
                       web_mode=str(bool(getattr(self, "_web_mode", False))))
            self._apply_remote_command(client_id, message.get("action"),
                                       message.get("value"))
        elif message_type in ("web_cmd", "web_url"):
            if (self._host_server is not None
                    and not self._host_server.can_control(client_id)):
                seek_trace("host.reject", client=client_id,
                           action=message.get("action") or message_type,
                           value=str(message.get("url") or message.get("value"))[-40:],
                           reason="view-only")
            elif message_type == "web_cmd":
                self._web_remote_command(message.get("action"), message.get("value"))
            else:
                self._web_apply_remote_url(message.get("url"))
        elif message_type == "web_ping":
            self._host_server.reply(client_id, {
                "v": STREAM_VERSION, "type": "web_pong",
                "t1": message.get("t1"), "t2": time.monotonic(),
            })
        elif message_type == "app_input":
            self._handle_app_input(client_id, message.get("payload") or {})
        elif message_type == "app_view":
            self._handle_app_view(client_id, message)
        elif message_type == "app_lease":
            action = str(message.get("action") or "")
            self._handle_app_lease(client_id, action)
        else:
            log.debug("忽略未知消息类型: %s", message_type)

    def _apply_remote_command(self, client_id, action, value):
        if self._host_server is not None and not self._host_server.can_control(client_id):
            log.info("忽略只读房客的控制指令: client=%s action=%s value=%r",
                     client_id, action, value)
            seek_trace("host.reject", client=client_id, action=action, value=value,
                       reason="view-only")
            return
        if getattr(self, "_web_mode", False):
            seek_trace("host.route", action=action, value=value, to="browser")
            self._web_remote_command(action, value)
            return
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
        if self._client_active:
            self._client_send_cmd("seek", int(target))
            self.time_label.configure(text=format_time(int(target)))
            return
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
             "host": False, "control": bool(m.get("control"))}
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

    def _set_can_control_ui(self, allowed):
        """房客侧: 只读时灰化播放控件并提示。"""
        if allowed:
            if not getattr(self, "_web_mode", False):
                self.set_playback_controls_enabled(True)
            self._set_status("主持已允许你参与控制", "ok")
        else:
            self.set_playback_controls_enabled(False)
            self._set_status("只读模式 · 仅主持可控制播放", "idle")

    def _toggle_member_control(self, client_id, allowed):
        """主持端: 允许/禁止某个房客参与控制。"""
        if not self._hosting or self._host_server is None or client_id == "host":
            return
        if not self._host_server.set_control(client_id, allowed):
            return
        names = {m["id"]: m["name"] for m in self._room_message()["members"]}
        name = names.get(client_id, "成员")
        self._host_server.send_to(client_id, {"v": STREAM_VERSION,
                                              "type": "control",
                                              "allowed": bool(allowed)})
        log.info("控制权限变更: %s(%s) -> %s", name, client_id,
                 "可控制" if allowed else "只读")
        self._set_status(f"{name} 已设为" + ("可控制" if allowed else "只读"), "ok")
        self._broadcast_room()

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
            on_toggle_control=self._toggle_member_control,
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
        _walk_translate(dialog)
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
        set_log_role("client")
        log.info("[lat] client.join t=%.1fms cmd=%s:%d udp=%s:%d",
                 time.perf_counter() * 1000.0, cmd_host, cmd_port, udp_host, udp_port)
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
        self._sync_stream_button()
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
        self.root.title(f"{tr('同步播放')} — {APP_TITLE}")
        def connect_worker():
            ok = self._client_connect()
            self.queue_net_event({"kind": "join_result", "ok": ok})

        self._set_status("正在连接主持…", "playing")
        self._show_buffering()
        threading.Thread(target=connect_worker, daemon=True,
                         name="client-connect").start()

    def leave_stream(self):
        self.set_playback_controls_enabled(True)
        try:
            self._app_stop_router()
        except Exception:
            log.debug("停止应用输入路由失败", exc_info=True)
        self._web_shutdown()
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
        if self.current_path is None:
            self._apply_ui_mode("menu")
        self._sync_stream_button()

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
        if not getattr(self, "_client_can_control", True):
            seek_trace("client.send.dropped", action=action, value=value,
                       reason="view-only")
            return
        if not self._client_connected:
            seek_trace("client.send.dropped", action=action, value=value,
                       reason="not-connected")
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
        """新建播放器(不复用可能卡在 stop 里的旧对象)。

        这里不再提前绑定窗口句柄: VLC 在 play() 时可能重建 vout,
        提前绑定会被丢弃并导致另开独立窗口。统一在起播后绑定。
        """
        if self.instance is None:
            return None
        self.player = self.instance.media_player_new()
        self.player.set_rate(1.0)
        # 新播放器尚未绑定句柄, 复位标记, 让换片流程重新绑一次
        self._hwnd_attached = False
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

    def _app_open_frame_window(self):
        """客户端(帧直传): 建窗口 + 直接在 Tk 里显示 PNG 帧, 不经过 VLC。"""
        share = getattr(self, "_app_share", None) or {}
        cmd_addr = getattr(self, "_client_cmd_addr", None)
        host = cmd_addr[0] if cmd_addr else "127.0.0.1"
        port = int(share.get("frame_port") or 0)
        if not port:
            log.warning("[lat] frames.viewer 缺少 frame_port, 无法接收画面")
            return
        width = int(share.get("width") or 1280)
        height = int(share.get("height") or 720)
        base_width = int(share.get("base_width") or width)
        stream_w = int(share.get("view_width") or share.get("stream_w") or 0) or base_width
        stream_w = max(320, stream_w)
        stream_h = int(share.get("stream_h") or 0) or max(
            2, int(round(stream_w * height / max(1.0, float(width)))))
        # 100% = 主持端传过来的原画 1:1。缩放只改本机窗口, 主持端永远只传原画。
        self._app_base_width = base_width
        self._app_base_height = max(2, int(height))
        self._app_view_width = stream_w
        self._app_frame_px = (stream_w, stream_h)
        try:
            preferred = float(self.settings.get("app_zoom", 1.0) or 1.0)
        except (TypeError, ValueError):
            preferred = 1.0
        self._app_zoom_preferred = preferred
        self._app_zoom = self._app_fit_zoom(stream_w, stream_h, preferred)
        if abs(self._app_zoom - preferred) > 0.001:
            log.info("[lat] frames.fit 屏幕 %dx%d 放不下 %.0f%%, 加入时改用 %d%%",
                     self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
                     preferred * 100, int(round(self._app_zoom * 100)))
        try:
            dpi = float(self.root.winfo_fpixels("1i")) / 96.0
        except Exception:
            dpi = 1.0
        dpi = dpi if dpi > 0.5 else 1.0
        self._app_dpi = dpi
        layout_w = max(200, int(round(stream_w / dpi)))
        layout_h = max(120, int(round(stream_h / dpi)))
        window = AppShareWindow(self.root, share.get("title", ""), layout_w,
                                layout_h, self._app_zoom,
                                on_close=self._app_close_window,
                                label_text=tr("共享画面"))
        # ± 只改本机显示倍率, 不再向主持端请求换抓取尺寸
        window.step_zoom = lambda direction: self._app_frame_zoom(direction)
        window.zoom_label.configure(text="%d%%" % self._app_zoom_percent())
        self._app_window = window
        try:
            window.update_idletasks()
            window.update()
        except tk.TclError:
            pass
        self._app_viewer = np_video.FramePlayer(
            window.video_frame, host, port, on_stats=self._app_frame_stats,
            on_size=self._app_frame_size)
        # 帧模式不用轮询路由, 免得和 Tk 事件重复发送(一次点击变两次)
        router = getattr(self, "_app_router", None)
        if router is not None:
            router.enabled = False
            try:
                router.detach()
            except Exception:
                pass
        self._app_input_bound = False
        self._app_bind_input(window)
        self._app_build_text_bar(window)
        # 控制权来自「房间成员」里的个人授权(welcome/control 下发的 control 字段)
        self._set_app_control(bool(getattr(self, "_client_can_control", False)))
        self._app_play_wall = time.monotonic()
        if getattr(self, "_client_can_control", False):
            try:
                self._client_send({"v": STREAM_VERSION, "type": "app_lease",
                                   "action": "request"})
            except Exception:
                log.debug("申请控制权失败", exc_info=True)
        window.after(300, self._app_attach_output)
        self._set_status(f"应用共享 · {share.get('title') or ''} · 直传画面", "playing")
        log.info("[lat] frames.viewer host=%s port=%d 尺寸=%dx%d",
                 host, port, stream_w, stream_h)

    def _app_frame_stats(self, avg, worst, size):
        viewer = getattr(self, "_app_viewer", None)
        stage = getattr(viewer, "stage", None) or {}
        log.info("[lat] frames.recv 延迟 avg=%.1fms max=%.1fms 平均=%.0fKB "
                 "分阶段 wire=%.1fms decode=%.1fms blit=%.1fms",
                 avg, worst, size / 1024.0,
                 stage.get("wire", 0.0), stage.get("decode", 0.0),
                 stage.get("blit", 0.0))
        try:
            self._set_status(
                "应用共享 · 直传 · 延迟 %.0fms · %.0fKB/帧" % (avg, size / 1024.0),
                "playing")
        except tk.TclError:
            pass
        try:
            if getattr(self, "_ui_mode", "") == "share":
                view = getattr(self, "share_view", None)
                if view is not None:
                    view.set_state(lines=[
                        "房间 %d 人" % int((self._client_room or {}).get("count") or 0),
                        "主持 %s" % ((self._client_room or {}).get("host") or "?"),
                        "延迟 %.0fms · %.0fKB/帧" % (avg, size / 1024.0),
                        tr("可控制") if getattr(self, "_client_can_control", False)
                        else tr("只读观看"),
                    ])
        except Exception:
            pass

    def _app_build_text_bar(self, window):
        """输入法/粘贴用的小输入框: 回车把整段文字发给主持端填入。"""
        try:
            bar = tk.Frame(window, bg="#1b1b1f")
            try:
                bar.pack(side=tk.BOTTOM, fill=tk.X, before=window.video_frame)
            except tk.TclError:
                bar.pack(side=tk.BOTTOM, fill=tk.X)
            entry = tk.Entry(bar, bg="#232327", fg="#eee", insertbackground="#eee",
                             relief=tk.FLAT)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=4)
            tk.Label(bar, text="输入后回车发送(支持输入法)", bg="#1b1b1f",
                     fg="#888").pack(side=tk.LEFT, padx=(0, 8))
        except tk.TclError:
            return
        self._app_text_bar = bar
        self._app_text_entry = entry
        entry.bind("<Return>", lambda e: self._app_send_text())
        entry.bind("<KP_Enter>", lambda e: self._app_send_text())

    def _set_app_control(self, enabled):
        """客户端: 有没有控制权。没有就不捕获任何输入, 输入框也藏起来。"""
        enabled = bool(enabled)
        changed = enabled != bool(getattr(self, "_app_input_allowed", False))
        self._app_input_allowed = enabled
        bar = getattr(self, "_app_text_bar", None)
        window = getattr(self, "_app_window", None)
        if bar is not None:
            try:
                if enabled:
                    if window is not None and not bar.winfo_ismapped():
                        bar.pack(side=tk.BOTTOM, fill=tk.X,
                                 before=window.video_frame)
                elif bar.winfo_manager():
                    # 一开始就是只读时也要收起来, 所以不能只看 changed
                    bar.pack_forget()
            except tk.TclError:
                pass
        if changed:
            log.info("[appinput] 控制权限变为 %s",
                     "可控制(本地捕获)" if enabled else "只读(本地不捕获)")
            try:
                self._set_status(
                    "应用共享 · 你正在控制" if enabled else "应用共享 · 只读观看",
                    "playing")
            except tk.TclError:
                pass
        return enabled

    def _app_send_text(self):
        entry = getattr(self, "_app_text_entry", None)
        if entry is None:
            return "break"
        try:
            text = entry.get()
        except tk.TclError:
            return "break"
        if text:
            self._app_send_input({"kind": "text", "text": text,
                                  "_ts": time.perf_counter()})
            log.info("[appinput] send text len=%d", len(text))
            try:
                entry.delete(0, tk.END)
            except tk.TclError:
                pass
        return "break"

    def _app_frame_size(self, width, height):
        """按帧的真实像素重排窗口: 1:1 的那份尺寸存进 stream_w/h, 倍率由 zoom 决定。

        缩放是本机行为: 主持端永远只传原画(100%), 这里把帧尺寸当成"100% 基准",
        客户端放大/缩小只改自己的窗口大小, 不影响别人。
        """
        window = getattr(self, "_app_window", None)
        if window is None or width <= 0 or height <= 0:
            return
        try:
            dpi = float(self.root.winfo_fpixels("1i")) / 96.0
        except Exception:
            dpi = 1.0
        dpi = dpi if dpi > 0.5 else 1.0
        layout_w = max(200, int(round(width / dpi)))
        layout_h = max(120, int(round(height / dpi)))
        zoom = float(getattr(self, "_app_zoom", 1.0) or 1.0)
        if (abs(layout_w - window.stream_w) <= 2
                and abs(layout_h - window.stream_h) <= 2
                and abs(zoom - window.zoom) < 0.001):
            return
        window.stream_w = layout_w
        window.stream_h = layout_h
        window.zoom = zoom
        self._app_view_width = width
        try:
            window.apply_zoom(announce=False)
        except tk.TclError:
            return
        log.info("[lat] frames.size 帧=%dx%d 基准=%dx%d(dpi=%.2f) 显示=%d%%",
                 width, height, layout_w, layout_h, dpi, self._app_zoom_percent())

    def _app_zoom_percent(self):
        zoom = float(getattr(self, "_app_zoom", 1.0) or 1.0)
        return max(1, int(round(zoom * 100)))

    # 线性档位: 25% 50% 75% 100% 125% 150% 175% 200%
    _APP_ZOOM_STEPS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)

    def _app_fit_zoom(self, base_w, base_h, preferred=1.0):
        """按客户端屏幕挑一个装得下的初始倍率。

        100% = 主持端传过来的原始像素 1:1。客户端的屏幕如果放不下这个尺寸,
        加入时就自动选一个更小的档位, 而不是硬开一个超出屏幕的窗口。
        """
        steps = self._APP_ZOOM_STEPS
        base_w = max(1, int(base_w or 0))
        base_h = max(1, int(base_h or 0))
        try:
            avail_w = max(320, int(self.root.winfo_screenwidth()) - 80)
            avail_h = max(240, int(self.root.winfo_screenheight()) - 140)
        except Exception:
            return min(steps, key=lambda s: abs(s - float(preferred or 1.0)))
        preferred = min(steps, key=lambda s: abs(s - float(preferred or 1.0)))
        for zoom in sorted(steps, reverse=True):
            if zoom > preferred + 1e-6:
                continue
            if base_w * zoom <= avail_w and base_h * zoom <= avail_h:
                return zoom
        return steps[0]

    def _app_frame_zoom(self, direction):
        """本机缩放: 主持端永远传原画, 只有自己窗口跟着放大/缩小(线性档位)。"""
        current = float(getattr(self, "_app_zoom", 1.0) or 1.0)
        steps = self._APP_ZOOM_STEPS
        index = min(range(len(steps)), key=lambda i: abs(steps[i] - current))
        target = max(0, min(len(steps) - 1, index + (1 if direction > 0 else -1)))
        zoom = steps[target]
        if abs(zoom - current) < 0.001:
            return
        self._app_zoom = zoom
        window = getattr(self, "_app_window", None)
        if window is not None:
            window.zoom = zoom
            try:
                window.apply_zoom(announce=False)
            except tk.TclError:
                pass
            try:
                window.zoom_label.configure(text="%d%%" % self._app_zoom_percent())
            except tk.TclError:
                pass
        log.info("[lat] frames.zoom zoom=%.2f (%d%%) 本机显示", zoom, self._app_zoom_percent())

    def _app_bind_input(self, window):
        """帧直传窗口: 用 Tk 事件捕获鼠标/滚轮/键盘(不装钩子, 不会拖累系统)。"""
        if getattr(self, "_app_input_bound", False):
            return
        self._app_input_bound = True
        viewer = getattr(self, "_app_viewer", None)
        if viewer is None:
            self._app_input_bound = False
            return
        label = getattr(viewer, "label", None) or getattr(viewer, "widget", None)
        # 0 无 / 1 左 / 2 右 / 4 中 —— 拖动时必须带上对应 MK_ 位, 否则程序不认
        state = {"mask": 0}

        def norm(event):
            width = max(1, label.winfo_width())
            height = max(1, label.winfo_height())
            return (max(0.0, min(1.0, event.x / float(width))),
                    max(0.0, min(1.0, event.y / float(height))))

        def send_mouse(action, event, extra=None):
            # 没有控制权就完全不捕获: 事件连本地都不处理
            if not getattr(self, "_app_input_allowed", False):
                return
            widget = getattr(event, "widget", None)
            if widget is not None and widget is not label:
                # 点在底部输入框/工具栏上的操作不要同步给主持端
                try:
                    parent = widget
                    while parent is not None:
                        if parent is label:
                            break
                        parent = parent.master
                    else:
                        return
                except Exception:
                    return
            nx, ny = norm(event)
            payload = {"kind": "mouse", "action": action,
                       "x": round(nx, 4), "y": round(ny, 4), "delta": 0,
                       "buttons": state["mask"],
                       "_ts": time.perf_counter()}
            if extra:
                payload.update(extra)
            self._app_send_input(payload)

        def on_down(event, bit, action):
            state["mask"] |= bit
            try:
                label.focus_set()
                window.focus_set()
            except tk.TclError:
                pass
            send_mouse(action, event, {"buttons": state["mask"]})

        def on_up(event, bit, action):
            send_mouse(action, event, {"buttons": state["mask"]})
            state["mask"] &= ~bit

        def on_wheel(event):
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                delta = 120 if getattr(event, "num", 0) == 4 else -120
            send_mouse("wheel", event, {"delta": delta})

        def on_move(event):
            send_mouse("move", event)

        bindings = {
            "<Button-1>": lambda e: on_down(e, 1, "left_down"),
            "<ButtonRelease-1>": lambda e: on_up(e, 1, "left_up"),
            "<Button-3>": lambda e: on_down(e, 2, "right_down"),
            "<ButtonRelease-3>": lambda e: on_up(e, 2, "right_up"),
            "<Button-2>": lambda e: on_down(e, 4, "middle_down"),
            "<ButtonRelease-2>": lambda e: on_up(e, 4, "middle_up"),
            "<B1-Motion>": on_move,
            "<B2-Motion>": on_move,
            "<B3-Motion>": on_move,
            "<Motion>": on_move,
            "<MouseWheel>": on_wheel,
            "<Button-4>": on_wheel,
            "<Button-5>": on_wheel,
        }
        for sequence, handler in bindings.items():
            try:
                label.bind(sequence, handler)
            except tk.TclError:
                pass

        def on_key(event, down):
            if not getattr(self, "_app_input_allowed", False):
                return
            # 焦点在底部输入框(或它里面的东西)时不要同步按键: 那是给输入法用的
            entry = getattr(self, "_app_text_entry", None)
            widget = getattr(event, "widget", None)
            if entry is not None and widget is not None:
                try:
                    if widget is entry or str(widget).startswith(str(entry)):
                        return
                except tk.TclError:
                    pass
            try:
                focus = window.focus_get()
            except (tk.TclError, KeyError):
                focus = None
            if entry is not None and focus is not None and focus is entry:
                return
            vk = int(getattr(event, "keycode", 0) or 0)
            if not vk:
                return
            payload = {"kind": "key", "vk": vk, "down": down,
                       "_ts": time.perf_counter()}
            char = str(getattr(event, "char", "") or "")
            if down and char and char.isprintable():
                payload["char"] = char
            self._app_send_input(payload)

        for sequence, down in (("<KeyPress>", True), ("<KeyRelease>", False)):
            try:
                # 只绑在顶层窗口: 绑两层会让一次按键发送两次
                window.bind(sequence, lambda e, d=down: on_key(e, d))
            except tk.TclError:
                pass
        try:
            window.focus_set()
            label.focus_set()
        except tk.TclError:
            pass

    def _app_open_window(self):
        """客户端: 用独立窗口显示共享画面(帧直传; 老版本走 VLC)。"""
        if getattr(self, "_app_window", None) is not None:
            return
        share = getattr(self, "_app_share", None) or {}
        if share.get("transport") == "frames":
            return self._app_open_frame_window()
        if self.instance is None:
            log.warning("VLC 未就绪, 无法打开应用共享窗口")
            return
        stream_w = int(share.get("stream_w") or share.get("width") or 1280)
        stream_h = int(share.get("stream_h") or share.get("height") or 720)
        try:
            zoom = float(self.settings.get("app_zoom", 1.0) or 1.0)
        except (TypeError, ValueError):
            zoom = 1.0
        window = AppShareWindow(self.root, share.get("title", ""), stream_w,
                                stream_h, zoom, on_close=self._app_close_window)
        # 先把窗口布局跑完再交给 VLC, 否则 VLC 的输出子窗口会盖住工具栏
        try:
            window.update_idletasks()
            window.update()
        except tk.TclError:
            pass
        # 应用模式不走 _start_client_video, 没人给接收器分配本地端口的话
        # _client_stream_url() 会是空串 → 播放器根本没建出来 → 黑屏。
        receiver = getattr(self, "_stream_receiver", None)
        if receiver is not None and not getattr(receiver, "local_port", 0):
            try:
                receiver.set_local_port(ephemeral_udp_port())
            except Exception:
                log.debug("应用共享分配本地端口失败", exc_info=True)
        player = None
        url = self._client_stream_url()
        if url:
            try:
                caching = int(self.settings.get("client_caching", 1000))
                media = self.instance.media_new(url)
                media.add_option(f":network-caching={caching}")
                media.add_option(f":live-caching={caching}")
                media.add_option(":clock-jitter=0")
                media.add_option(":clock-synchro=0")
                media.add_option(":drop-late-frames")
                media.add_option(":skip-frames")
                player = self.instance.media_player_new()
                player.set_media(media)
                # 必须在 play() 之前绑定窗口: 否则 VLC 会自己建一个顶层窗口盖在上面,
                # 鼠标事件落在那个窗口上, 我们的 WndProc 永远收不到点击。
                if os.name == "nt":
                    try:
                        player.set_hwnd(window.video_frame.winfo_id())
                    except Exception:
                        log.debug("应用共享预绑定窗口失败", exc_info=True)
                player.play()
                self._app_play_ts = time.perf_counter()
                self._app_play_wall = time.monotonic()
                log.info("[lat] player.play t=%.1fms caching=%dms url=%s",
                         self._app_play_ts * 1000.0, caching, url)
            except Exception:
                log.exception("应用共享窗口播放失败")
                player = None
        self._app_window = window
        self._app_player = player
        window.after(400, self._app_attach_output)
        window.after(2500, self._app_report_latency)
        self._set_status(f"应用共享 · {share.get('title') or ''} · 已在新窗口打开", "playing")
        log.info("应用共享窗口已打开: %s (%dx%d, %.0f%%)",
                 share.get("title"), stream_w, stream_h, zoom * 100)

    def _app_restart_player(self):
        """采集端重启后(窗口被拖动/缩放)重建播放器: 新的 TS 流不连续, 旧的解不出来。"""
        if getattr(self, "_app_viewer", None) is not None:
            # 帧直传不需要重建播放器, 帧会自己继续来
            return
        window = getattr(self, "_app_window", None)
        if window is None:
            self._app_open_window()
            return
        old = getattr(self, "_app_player", None)
        self._app_player = None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
            self._dispose_player_async(old)
        # 旧播放器还占着原来的本地端口, 新播放器再绑同一个端口会失败(黑屏),
        # 所以换一个本地端口再让接收器把流转发过去。
        receiver = getattr(self, "_stream_receiver", None)
        if receiver is not None:
            try:
                receiver.set_local_port(ephemeral_udp_port())
            except Exception:
                log.debug("应用共享重建: 切换本地端口失败", exc_info=True)
        url = self._client_stream_url()
        if not url or self.instance is None:
            return
        try:
            caching = int(self.settings.get("client_caching", 1000))
            media = self.instance.media_new(url)
            media.add_option(f":network-caching={caching}")
            media.add_option(f":live-caching={caching}")
            media.add_option(":clock-jitter=0")
            media.add_option(":clock-synchro=0")
            player = self.instance.media_player_new()
            player.set_media(media)
            if os.name == "nt":
                try:
                    player.set_hwnd(window.video_frame.winfo_id())
                except Exception:
                    log.debug("应用共享重建: 预绑定窗口失败", exc_info=True)
            player.play()
            self._app_player = player
            # VLC 重开播放器会重建输出子窗口, 旧的 WndProc 就废了, 必须重新挂
            router = getattr(self, "_app_router", None)
            if router is not None:
                try:
                    router.detach()
                except Exception:
                    pass
            self.root.after(700, self._app_start_router)
            window.after(150, self._app_attach_output)
            log.info("应用共享画面已重建播放器(采集代次变化, 本地端口 %s)",
                     getattr(receiver, "local_port", None))
        except Exception:
            log.exception("应用共享画面重建失败")

    def _app_attach_output(self, attempt=0):
        window = getattr(self, "_app_window", None)
        if window is None:
            return
        if getattr(self, "_app_viewer", None) is not None:
            # 帧直传: 不装 VLC、也不装系统钩子; 输入由 Tk 事件直接捕获
            try:
                window.lift()
            except tk.TclError:
                pass
            return
        player = getattr(self, "_app_player", None)
        if player is None:
            return
        try:
            window.update_idletasks()
            if window.video_frame.winfo_width() <= 60 and attempt < 6:
                window.after(120, lambda: self._app_attach_output(attempt + 1))
                return
            hwnd = window.video_frame.winfo_id()
            if os.name == "nt":
                player.set_hwnd(hwnd)
            window.lift()
        except Exception:
            log.debug("应用共享画面绑定失败", exc_info=True)
        share = getattr(self, "_app_share", None) or {}
        if share.get("allow_control"):
            # 主持端已经开了"允许操作", 客户端不再要求成员级控制权限
            router = getattr(self, "_app_router", None)
            if router is None or not router.target:
                self._app_start_router()
            window.after(2500, self._app_watch_router)

    def _app_report_latency(self):
        """每 2 秒打一次 VLC 播放进度与墙钟的差, 用来判断画面延迟到底在哪。"""
        window = getattr(self, "_app_window", None)
        player = getattr(self, "_app_player", None)
        if window is None or player is None:
            return
        try:
            pos = player.get_time()
            state = player.get_state()
        except Exception:
            pos, state = -1, None
        started = getattr(self, "_app_play_wall", None)
        elapsed = (time.monotonic() - started) * 1000.0 if started else -1.0
        drift = (elapsed - pos) if (pos >= 0 and elapsed >= 0) else -1.0
        log.info("[lat] vlc.play pos=%.0fms wall=%.0fms 落后=%.0fms state=%s",
                 pos, elapsed, drift, state)
        try:
            window.after(2000, self._app_report_latency)
        except tk.TclError:
            pass

    def _app_watch_router(self):
        """VLC 换了输出窗口(重建/改尺寸)时把点击路由重新挂到新的窗口上。"""
        if getattr(self, "_app_window", None) is None:
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        router = getattr(self, "_app_router", None)
        if router is None or not router.target:
            self._app_start_router()
            return
        target = int(router.target)
        if not user32.IsWindow(ctypes.c_void_p(target)):
            router.detach()
            self.root.after(300, self._app_start_router)
            return
        root = int(user32.GetAncestor(ctypes.c_void_p(target), 2) or 0)
        frame = self._app_video_hwnd() if hasattr(self, "_app_video_hwnd") else 0
        top = 0
        if frame:
            top = int(user32.GetAncestor(ctypes.c_void_p(int(frame)), 2) or 0)
        if root and top and root != top:
            # 输出窗口已经不属于这个共享窗口了, 重新挂
            log.info("应用共享输出窗口已变化, 重新挂载点击路由")
            router.detach()
            self.root.after(300, self._app_start_router)

    def _app_close_window(self):
        window = getattr(self, "_app_window", None)
        player = getattr(self, "_app_player", None)
        viewer = getattr(self, "_app_viewer", None)
        self._app_window = None
        self._app_player = None
        self._app_viewer = None
        self._app_input_bound = False
        self._app_text_entry = None
        self._app_text_bar = None
        self._app_input_allowed = False
        self._app_drag_target = 0
        if viewer is not None:
            try:
                viewer.close()
            except Exception:
                log.debug("关闭帧显示失败", exc_info=True)
        if window is not None:
            try:
                self.settings["app_zoom"] = float(window.zoom)
                save_settings(self.settings)
            except Exception:
                pass
        if player is not None:
            try:
                player.stop()
            except Exception:
                pass
            self._dispose_player_async(player)
        self._app_stop_router()
        if getattr(self, "_client_active", False):
            self.leave_stream()

    def _app_start_router(self, attempt=0):
        """客户端: 在视频窗口上挂事件路由, 把操作发回主持。

        VLC 的输出子窗口可能比我们的窗口晚几百毫秒才出现, 所以挂不上要重试。
        """
        if os.name != "nt":
            return
        if getattr(self, "_app_router", None) is None:
            self._app_router = AppInputRouter(
                self.root, self._app_video_hwnd, self._app_send_input)
        if self._app_router.enabled and self._app_router.target:
            return
        if self._app_router.attach():
            self._app_router.enabled = True
            self._client_send({"v": STREAM_VERSION, "type": "app_lease",
                               "action": "request"})
            self._set_status("应用共享 · 已申请控制", "playing")
            log.info("应用输入路由已挂载: hwnd=0x%X (第 %d 次)",
                     self._app_router.target or 0, attempt + 1)
            return
        if attempt < 16:
            try:
                self.root.after(600, lambda: self._app_start_router(attempt + 1))
            except tk.TclError:
                pass
        else:
            log.warning("应用输入路由挂载失败: 找不到视频输出子窗口")

    def _app_video_hwnd(self):
        window = getattr(self, "_app_window", None)
        if window is None:
            return 0
        try:
            return window.video_frame.winfo_id()
        except tk.TclError:
            return 0

    def _app_stop_router(self):
        router = getattr(self, "_app_router", None)
        if router is not None:
            router.enabled = False
            try:
                router.detach()
            except Exception:
                pass
        self._app_share = None
        self._app_is_controller = False

    def _app_send_input(self, event):
        """客户端: 把捕获到的输入转发给主持端(第 2 段: 转发)。"""
        router = getattr(self, "_app_router", None)
        frames_mode = getattr(self, "_app_viewer", None) is not None
        if frames_mode and not getattr(self, "_app_input_allowed", False):
            return
        if not frames_mode:
            if router is None or not router.enabled:
                log.info("[appinput] send.reject reason=router-disabled")
                return
        if not getattr(self, "_app_share", None):
            log.info("[appinput] send.reject reason=no-share")
            return
        action = str(event.get("action") or "")
        kind = str(event.get("kind") or "mouse")
        if not (kind == "mouse" and action == "move"):
            # 去重: 同一种事件在 30ms 内只发一次, 防止 Tk 冒泡/残留路由导致"一次操作变两次"
            key = (kind, action, event.get("vk"), event.get("text"))
            now = time.perf_counter()
            cache = getattr(self, "_app_dedup", None)
            if cache is None:
                cache = self._app_dedup = {}
            last = cache.get(key)
            if last is not None and (now - last) < 0.03:
                log.info("[appinput] send.dedup kind=%s action=%s", kind, action)
                return
            cache[key] = now
            if len(cache) > 32:
                cache.clear()
        if event.get("kind") == "mouse" and action == "move":
            now = time.monotonic()
            if now - getattr(self, "_app_last_move", 0.0) < (1.0 / 60.0):
                return
            self._app_last_move = now
        connected = bool(getattr(self, "_client_connected", False))
        t_before = time.perf_counter()
        try:
            self._client_send({"v": STREAM_VERSION, "type": "app_input",
                               "payload": event})
        except Exception:
            log.info("[appinput] send.fail action=%s", action)
            return
        t_after = time.perf_counter()
        if action and action != "move":
            self._app_send_count = getattr(self, "_app_send_count", 0) + 1
            captured = float(event.get("_ts") or 0.0)
            delta = (t_after - captured) * 1000.0 if captured else -1.0
            log.info("[appinput] send #%d %s norm=(%.3f,%.3f) connected=%s "
                     "controller=%s t=%.1fms 捕获→发送=%.1fms 写入耗时=%.2fms",
                     self._app_send_count, action,
                     float(event.get("x") or 0.0), float(event.get("y") or 0.0),
                     connected, bool(getattr(self, "_app_is_controller", False)),
                     t_after * 1000.0, delta, (t_after - t_before) * 1000.0)

    def _start_client_video(self):
        """客户端用同一套 VLC 播放主持推来的 UDP 流, 画面直接渲染到视频区。"""
        if getattr(self, "_web_mode", False):
            # 浏览器同步模式只同步网页播放状态, 不接收媒体流
            return
        if getattr(self, "_app_share", None):
            # 应用共享: 画面走独立窗口, 不占用主窗口的视频区
            self._app_open_window()
            return
        # 媒体模式: 切换到播放器界面
        self._apply_ui_mode("player")
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
            self.root.after(150, self._attach_video_output)
            self._client_video_started = True
            self._client_video_start_ts = time.monotonic()
            self._client_last_media_ms = -1
            self._displayed_pictures = -1
            if getattr(self, "_app_share", None) and self._app_share.get("allow_control") \
                    and getattr(self, "_client_can_control", False):
                self.root.after(1200, self._app_start_router)
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
        elif message_type == "control":
            allowed = bool(message.get("allowed"))
            self._client_can_control = allowed
            log.info("控制权限变更(房客侧): %s", "可控制" if allowed else "只读")
            self._set_can_control_ui(allowed)
            if getattr(self, "_app_viewer", None) is not None:
                # 应用共享窗口里立刻生效: 没权限就停止本地捕获, 输入框也收起来
                self._set_app_control(allowed)
                if allowed:
                    try:
                        self._client_send({"v": STREAM_VERSION, "type": "app_lease",
                                           "action": "request"})
                    except Exception:
                        pass
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
            self._client_can_control = bool(message.get("control"))
            room_mode = str(message.get("mode") or "media")
            if room_mode == "web":
                self._web_ensure_fields()
                self._web_mode = True
                self.set_playback_controls_enabled(False)
                self._web_tabs_ready = False
                self._web_tabs_sig = ()
                self._web_tab_map = {}
                # 同步模式不需要媒体接收: 立刻释放本机占用的媒体端口,
                # 这样同一台机器可以同时跑主持和客户端两个实例
                if self._stream_receiver is not None:
                    try:
                        self._stream_receiver.stop()
                    except Exception:
                        log.debug("释放媒体接收端口失败", exc_info=True)
                    self._stream_receiver = None
                self._set_status("已加入浏览器同步房间 · 正在打开浏览器…", "ok")
                self._apply_ui_mode("share")
                self._update_share_view()
                self._web_open_browser(str(message.get("url") or ""),
                                       callback=self._web_client_browser_ready)
            elif room_mode == "app":
                # 应用共享房间: 进专属共享界面(画面在独立窗口)
                self._apply_ui_mode("share")
                self._update_share_view()
                if not self._client_can_control:
                    self.set_playback_controls_enabled(False)
            else:
                # 媒体同步房间: 这里是唯一会开播放器界面的房间类型
                self._apply_ui_mode("player")
                if not self._client_can_control:
                    # 媒体模式下只读房客: 控件灰化, 但流仍然要接收
                    self.set_playback_controls_enabled(False)
            log.info("主持已确认加入: client_id=%s (房间类型 %s)",
                     self._client_id, room_mode)
        elif message_type == "app_offer":
            generation = int(message.get("generation") or 1)
            previous = int(getattr(self, "_app_generation", 0) or 0)
            self._app_generation = generation
            log.info("[lat] app_offer.recv t=%.1fms gen=%d stream=%sx%s",
                     time.perf_counter() * 1000.0, generation,
                     message.get("stream_w"), message.get("stream_h"))
            self._app_share = {
                "title": str(message.get("title") or ""),
                "allow_control": bool(message.get("allow_control")),
                "transport": str(message.get("transport") or "vlc"),
                "frame_port": int(message.get("frame_port") or 0),
                "base_width": int(message.get("base_width") or 0),
                "view_width": int(message.get("view_width") or 0),
                "width": message.get("width") or 0,
                "height": message.get("height") or 0,
                "stream_w": message.get("stream_w") or 0,
                "stream_h": message.get("stream_h") or 0,
            }
            log.info("进入应用共享: %s (允许控制=%s, 第 %d 代)",
                     self._app_share["title"], self._app_share["allow_control"],
                     generation)
            self._set_status(f"应用共享 · {self._app_share['title']} · 正在接收画面", "playing")
            # 客户端进专属共享界面(和主持一致), 不切播放器
            self._apply_ui_mode("share")
            self._update_share_view()
            # 主窗口里可能已经在播媒体流: 先停掉, 画面改走独立窗口
            if getattr(self, "_client_video_started", False) and self.player is not None:
                old = self.player
                self.player = None
                self._client_video_started = False
                try:
                    self._dispose_player_async(old)
                except Exception:
                    pass
            if previous and generation != previous and getattr(self, "_app_window", None) is not None:
                # 主持端重启了采集(窗口被拖动/缩放): 等它出关键帧再重建播放器
                self.root.after(1200, self._app_restart_player)
            else:
                self.root.after(150, self._app_open_window)
        elif message_type == "app_closed":
            reason = str(message.get("reason") or "共享已结束")
            log.info("应用共享结束: %s", reason)
            try:
                if getattr(self, "_app_window", None) is not None:
                    self._app_close_window()
            except Exception:
                log.debug("关闭应用共享窗口失败", exc_info=True)
            if getattr(self, "_client_active", False):
                self.leave_stream()
            self._set_status(tr(reason), "idle")
        elif message_type == "app_lease":
            action = str(message.get("action") or "")
            if action == "granted":
                self._app_is_controller = str(message.get("holder") or "") == (self._client_id or "")
                self._set_status("应用共享 · 你正在控制" if self._app_is_controller
                                 else "应用共享 · 只读观看", "playing")
                if self._app_is_controller:
                    self._app_start_router()
            elif action in ("released", "denied"):
                self._app_is_controller = False
                self._set_status("应用共享 · 只读观看", "playing")
        elif message_type == "app_error":
            detail = str(message.get("detail") or "")
            log.warning("应用共享错误: %s %s", message.get("code"), detail)
            self._set_status(f"应用共享异常 · {detail or message.get('code')}", "error")
        elif message_type == "web_state":
            if getattr(self, "_web_mode", False):
                self._web_client_apply(message)
        elif message_type == "web_pong":
            pass
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
            self._notify_later("已离开房间", reason)
        elif message_type == "pong":
            pass
        elif message_type == "bye":
            log.info("主持结束会话: %s", message.get("reason"))
            self._close_password_dialog()
            self.leave_stream()
            self._notify_later("会话结束", str(message.get("reason") or "主持已停止会话"))
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
            self.root.title(f"{name} — {tr('同步播放')} — {APP_TITLE}")
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
        for attempt in range(2):
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
                return
            except Exception as error:
                log.exception("视频输出绑定失败(句柄 0x%X, 第 %d 次)", hwnd, attempt + 1)
                if attempt == 0:
                    time.sleep(0.08)
                    continue
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
        self.settings = settings
        save_settings(settings)
        self.apply_language(settings.get("language", "zh"))
        if hasattr(self, "buffering"):
            try:
                self.buffering.set_user_scale(settings.get("buffer_scale", 1.0))
            except Exception:
                log.debug("应用缓冲层缩放失败", exc_info=True)
        self.seek_step_ms = int(settings["seek_step"]) * 1000
        self._apply_log_level(settings.get("log_level", "INFO"))
        set_animations_enabled(settings.get("animations", True))
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

        # 换片复用同一个播放器实例: 反复新建 media_player 会让 VLC 的 vout
        # 偶发脱离已绑定的句柄, 表现为"隔一次就弹独立窗口"。
        if self.player is None:
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
        # 换片会重建播放器, 新播放器必须重新绑定窗口句柄, 否则 VLC 会另开独立输出窗口
        self._attach_video_output()

        self.current_path = path
        self._apply_ui_mode("player")
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
        # play() 之后 VLC 会重建内部视频输出, 再绑几次句柄, 避免它另开独立窗口
        for delay in (150, 450, 900):
            self.root.after(delay, self._attach_video_output)
        # VLC 起播后音轨表可能还没就绪, 多试几次直到固定到第一条
        for delay in (700, 1500, 2500):
            self.root.after(delay, self._ensure_single_audio_track)
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
        if getattr(self, "_web_mode", False) and not getattr(self, "_web_local_override", False):
            self._set_status("浏览器同步模式 · 请在浏览器里操作", "idle")
            return

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
        if getattr(self, "_web_mode", False) and not getattr(self, "_web_local_override", False):
            self._set_status("浏览器同步模式 · 请在浏览器里操作", "idle")
            return
        seek_trace("client.seek_relative", delta_ms=delta_ms,
                   mode="web" if getattr(self, "_web_mode", False) else "media",
                   client_active=str(bool(self._client_active)))
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
        if getattr(self, "_web_mode", False) and not getattr(self, "_web_local_override", False):
            self._set_status("浏览器同步模式 · 请在浏览器里操作", "idle")
            return

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
        if getattr(self, "_web_mode", False) and not getattr(self, "_web_local_override", False):
            self._set_status("浏览器同步模式 · 请在浏览器里操作", "idle")
            return
        rate = self._current_rate()
        seek_trace("client.speed_change", rate=rate,
                   mode="web" if getattr(self, "_web_mode", False) else "media",
                   client_active=str(bool(self._client_active)),
                   connected=str(bool(self._client_connected)))
        if self._client_active:
            self._client_send_cmd("rate", rate)
            self._web_hold_until = time.monotonic() + 2.0
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
        if getattr(self, "_web_mode", False):
            self._web_dragging = True
        seek_trace("client.drag.start",
                   mode="web" if getattr(self, "_web_mode", False) else "media",
                   client=str(getattr(self, "_client_active", False)),
                   duration=getattr(self, "_web_duration", 0.0))

    def _on_progress_drag(self, value):
        if getattr(self, "_web_mode", False):
            duration = getattr(self, "_web_duration", 0.0)
            if duration <= 0:
                return
            self.time_label.configure(
                text=format_time(int(float(value) / 1000.0 * duration * 1000)))
            return
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
        if getattr(self, "_web_mode", False):
            self._web_dragging = False
            self._web_rate = 0.0
            self._web_hold_until = 0.0
            self._seeking = False
            duration = getattr(self, "_web_duration", 0.0)
            seek_trace("client.detect",
                       slider=value, duration=duration,
                       target_ms=int(float(value) / 1000.0 * duration * 1000),
                       hosting=str(bool(self._hosting)),
                       client_active=str(bool(self._client_active)),
                       connected=str(bool(self._client_connected)))
            if duration <= 0:
                if self._client_active:
                    seek_trace("client.send", action="seek_ratio", value=int(float(value)),
                               reason="no-duration")
                    self._client_send_cmd("seek_ratio", int(float(value)))
                    self._web_hold_until = time.monotonic() + 3.0
                    self.progress_var.set(float(value))
                    self._set_status("已请求跳转 · 等待主持端确认")
                self._seeking = False
                return
            target = int(float(value) / 1000.0 * duration * 1000)
            self.time_label.configure(text=format_time(target))
            if self._client_active:
                seek_trace("client.send", action="seek", value=target,
                           reason="duration-known")
                self._client_send_cmd("seek", target)
                # 乐观显示: 2 秒内不让回传的旧进度把滑块打回去
                self._web_hold_until = time.monotonic() + 2.0
                self.progress_var.set(float(value))
            else:
                seek_trace("client.local_apply", action="seek", value=target)
                self._web_remote_command("seek", target)
            return
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
        # 全屏退出后清掉可能残留的 TOPMOST 与弹窗状态, 否则窗口会盖在所有窗口之上
        self.root.attributes("-topmost", True)
        self.root.attributes("-topmost", False)
        try:
            self.root.overrideredirect(False)
        except tk.TclError:
            pass
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        if self._saved_geometry:
            self.root.geometry(self._saved_geometry)
        clear_topmost(self.root)
        self.root.after(80, lambda: clear_topmost(self.root))
        self.root.after(150, lambda: clear_topmost(self.root))
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_set()
        except tk.TclError:
            pass
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
        text = tr(text)
        colors = {"idle": C.TEXT_DIM, "playing": C.ACCENT, "error": C.DANGER, "ok": C.OK}
        self.status_label.configure(text=text)
        self.status_dot.delete("all")
        dot = _circle_photo(self.status_dot, 7, colors.get(level, C.TEXT_DIM))
        self.status_dot.create_image(5 - dot.width() / 2.0, 5 - dot.height() / 2.0,
                                     anchor="nw", image=dot)
        # 主菜单里也要能看到状态
        home = getattr(self, "home_view", None)
        if home is not None and getattr(self, "_ui_mode", "player") == "menu":
            try:
                home.set_status(text, level)
            except Exception:
                pass

    def _notify_later(self, title, message, kind="info"):
        """网络消息触发的提示: 不弹模态框, 只写状态栏/聊天提示, 避免阻塞 Tk 主循环。"""
        text = f"{tr(title)} · {tr(message)}" if message else tr(title)
        log.info("提示: %s", text)
        try:
            self._set_status(text, "error" if kind == "error" else "idle")
        except Exception:
            pass
        try:
            panel = getattr(self, "chat_panel", None)
            if panel is not None and hasattr(panel, "show_hint"):
                panel.show_hint(text)
        except Exception:
            pass

    def _alert(self, title, message, kind="info"):
        title = tr(title)
        message = tr(message)
        log.debug("提示框[%s]: %s | %s", kind, title, message.replace("\n", " / "))
        self._alert_open = True
        self._sync_danmaku()
        try:
            alert = Alert(self.root, title, message, kind)
            _walk_translate(alert)
        finally:
            self._alert_open = False
            self._schedule_danmaku_sync()

    def apply_language(self, lang):
        """切换界面语言并立即刷新全部窗口。"""
        self.settings["language"] = "en" if str(lang).lower() == "en" else "zh"
        apply_language(self.root, self.settings["language"])
        self._web_refresh_start_page()
        for dialog in list(self._secondary_windows):
            try:
                if dialog.winfo_exists():
                    _walk_translate(dialog)
            except tk.TclError:
                pass
        for attr in ("_fs_chat", "_fs_dock"):
            dialog = getattr(self, attr, None)
            if dialog is not None:
                try:
                    _walk_translate(dialog)
                except tk.TclError:
                    pass

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
        try:
            self._web_shutdown()
        except Exception:
            log.debug("关闭浏览器失败", exc_info=True)
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
        # 采集进程必须跟着退出, 否则任务管理器里会攒一堆 ffmpeg
        try:
            self.stop_app_share()
        except Exception:
            log.debug("停止应用共享失败", exc_info=True)
        try:
            cleanup_orphan_ffmpeg()
        except Exception:
            log.debug("清理遗留 ffmpeg 失败", exc_info=True)
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
