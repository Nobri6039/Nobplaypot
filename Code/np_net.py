"""net 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403
from np_core import _rgb  # noqa: F401

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
                "control": False,
            }
            return client_id

    def touch_client(self, client_id):
        with self._lock:
            entry = self._clients.get(client_id)
            if entry is not None:
                entry["last_seen"] = time.time()

    def set_control(self, client_id, allowed):
        """设置某个房客是否允许参与控制(默认只读)。"""
        with self._lock:
            entry = self._clients.get(client_id)
            if entry is None:
                return False
            entry["control"] = bool(allowed)
            return True

    def can_control(self, client_id):
        with self._lock:
            entry = self._clients.get(client_id)
            return bool(entry and entry.get("control"))

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

    def send_to(self, client_id, message):
        with self._lock:
            entry = self._clients.get(client_id)
            handler = entry["handler"] if entry else None
        if handler is None:
            return False
        return self._send_to_handler(handler, message)

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
                 "addr": entry["addr"], "control": bool(entry.get("control"))}
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


def clear_topmost(window):
    """强制去掉窗口的 WS_EX_TOPMOST(全屏退出后防止窗口卡在置顶)。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        HWND_NOTOPMOST = ctypes.c_void_p(-2)
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd), HWND_NOTOPMOST, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        style = ctypes.windll.user32.GetWindowLongW(ctypes.c_void_p(hwnd), -20)
        log.debug("退出全屏后 exstyle=0x%08X topmost=%s", style,
                  bool(style & 0x00000008))
    except Exception:
        log.debug("清除置顶失败", exc_info=True)


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
