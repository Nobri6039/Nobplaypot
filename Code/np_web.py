"""web 模块

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""

from np_core import *  # noqa: F401,F403
from np_core import _free_tcp_port, _pid_alive  # noqa: F401

from np_net import *  # noqa: F401,F403

from np_widgets import *  # noqa: F401,F403

BROWSER_START_URL = "https://www.bing.com"
# 起始页: 模板随程序放在 Bin\ 里(相对 __file__ 读), 用的时候再渲染一份到固定目录。
# 为什么要拷到 C:\Temp: 浏览器打开的是 file:// 绝对路径, 而主持与房客的安装目录
# 往往不一样; 固定成同一个绝对路径后, 同步过去的 URL 两边都打得开。
START_PAGE_FILE = "browser_start.html"
START_PAGE_RUNTIME_DIR = r"C:\Temp"
START_PAGE_RUNTIME_NAME = "nobplaypot_start.html"
BROWSER_WINDOW_TITLE = "Nobplaypot 浏览器"
BROWSER_PROFILE_DIR = os.path.join(CONFIG_DIR, "browser-profile")
BROWSER_PROFILE_ROOT = os.path.join(CONFIG_DIR, "browser-profiles")
WEB_SYNC_INTERVAL_MS = 200
WEB_SYNC_TOLERANCE_S = 0.75

_WEB_STATE_JS = r"""
(function () {
  var v = document.querySelector('video');
  if (!v) { return JSON.stringify({ok: false, url: location.href}); }
  return JSON.stringify({
    ok: true, url: location.href,
    t: v.currentTime || 0,
    d: isFinite(v.duration) ? v.duration : 0,
    p: (!v.paused && !v.ended),
    r: v.playbackRate || 1
  });
})()
"""


def _web_apply_js(position, playing, rate):
    action = "v.play().catch(function(){});" if playing else "v.pause();"
    return (
        "(function(){var v=document.querySelector('video');"
        "if(!v){return 'no-video';}"
        "try{v.currentTime=" + repr(float(position)) + ";}catch(e){}"
        "try{v.playbackRate=" + repr(float(rate)) + ";}catch(e){}"
        + action +
        "return 'ok';})()"
    )


_SLOTS_HELD = set()


def acquire_browser_slot(slots=4):
    """给当前进程分配一个独占的浏览器 profile 槽位, 本机多开时互不干扰。"""
    os.makedirs(BROWSER_PROFILE_ROOT, exist_ok=True)
    pid = os.getpid()
    for index in range(1, slots + 1):
        # 同进程内复用一个槽位会导致两个浏览器共用 profile, 必须跳过
        if index in _SLOTS_HELD:
            continue
        lock_path = os.path.join(BROWSER_PROFILE_ROOT, "slot-%d.lock" % index)
        if os.path.exists(lock_path):
            old_pid = 0
            try:
                with open(lock_path, encoding="utf-8") as handle:
                    old_pid = int((handle.read() or "0").strip() or 0)
            except (OSError, ValueError):
                old_pid = 0
            if old_pid and old_pid != pid and _pid_alive(old_pid):
                continue
            try:
                os.remove(lock_path)
            except OSError:
                pass
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError:
            continue
        try:
            os.write(handle, str(pid).encode("ascii"))
        finally:
            os.close(handle)
        _SLOTS_HELD.add(index)
        return index, os.path.join(BROWSER_PROFILE_ROOT, "slot-%d" % index), lock_path
    # 全部占满: 用进程号单独开一个目录, 也不再和别人共用
    fallback = os.path.join(BROWSER_PROFILE_ROOT, "slot-p%d" % pid)
    os.makedirs(fallback, exist_ok=True)
    return 0, fallback, ""


def release_browser_slot(lock_path):
    try:
        name = os.path.basename(str(lock_path or ""))
        if name.startswith("slot-") and name.endswith(".lock"):
            _SLOTS_HELD.discard(int(name[5:-5]))
    except (ValueError, TypeError):
        pass
    if not lock_path:
        return
    try:
        os.remove(lock_path)
    except OSError:
        pass


class _MiniWebSocket:
    """只够 CDP 用的 WebSocket 客户端: 文本帧 + ping/pong + close。"""

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, url, timeout=8.0):
        parts = urllib.parse.urlparse(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("CDP 握手失败")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0].decode("latin1")
        if "101" not in status_line:
            raise OSError("CDP 握手被拒绝: %s" % status_line)
        client = cls(sock)
        client._buf = rest
        return client

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(1, text.encode("utf-8"))

    def _read(self, count):
        while len(self._buf) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("CDP 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def recv_text(self, timeout=10.0):
        self._sock.settimeout(timeout)
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 1:
                return payload.decode("utf-8", "replace")
            if opcode == 8:
                raise OSError("CDP 连接被关闭")
            if opcode == 9:
                self._send_frame(10, payload)


class _LegacyEdgeShell:
    """(旧实现, 保留备查) 应用内浏览器窗口: 系统 Edge 的 app 模式 + CDP 控制页面。"""

    def __init__(self):
        self.proc = None
        self.port = None
        self.ws = None
        self._id = 0
        self._lock = threading.Lock()

    @staticmethod
    def edge_path():
        candidates = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def available(self):
        return self.edge_path() is not None

    def open(self, url=""):
        if self.proc is not None:
            return True
        edge = self.edge_path()
        if edge is None:
            raise RuntimeError("未找到 Microsoft Edge, 无法打开内置浏览器")
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        self.port = _free_tcp_port(9300, 9399)
        args = [
            edge,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % BROWSER_PROFILE_DIR,
            "--no-first-run",
            "--no-default-browser-check",
            "--app=%s" % (url or BROWSER_START_URL),
        ]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not self._connect(20.0):
            return False
        if url:
            # 首次启动可能先落到新标签页, 连上后显式导航到目标地址
            self.navigate(url)
        return True

    def _targets(self):
        url = "http://127.0.0.1:%d/json" % self.port
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def _connect(self, deadline):
        end = time.monotonic() + deadline
        last_error = None
        while time.monotonic() < end:
            try:
                for target in self._targets():
                    if target.get("type") != "page":
                        continue
                    ws_url = target.get("webSocketDebuggerUrl")
                    if not ws_url:
                        continue
                    self.ws = _MiniWebSocket.connect(ws_url)
                    return True
            except Exception as error:
                last_error = error
            time.sleep(0.35)
        raise RuntimeError("浏览器调试端口连接失败: %s" % last_error)

    def evaluate(self, expression, timeout=6.0):
        with self._lock:
            if self.ws is None:
                raise RuntimeError("浏览器未就绪")
            self._id += 1
            command_id = self._id
            self.ws.send_text(json.dumps({
                "id": command_id,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True,
                           "awaitPromise": False},
            }))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = self.ws.recv_text(timeout=max(0.5, deadline - time.monotonic()))
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if payload.get("id") != command_id:
                    continue
                result = payload.get("result") or {}
                remote = result.get("result") or {}
                return remote.get("value")
            raise TimeoutError("CDP 调用超时")

    def navigate(self, url, timeout=8.0):
        with self._lock:
            if self.ws is None:
                return False
            self._id += 1
            self.ws.send_text(json.dumps({
                "id": self._id, "method": "Page.navigate", "params": {"url": url},
            }))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.15)
            try:
                ready = self.evaluate("document.readyState", timeout=2.0)
            except Exception:
                continue
            if ready == "complete":
                time.sleep(0.2)
                return True
        return False

    def read_state(self):
        raw = self.evaluate(_WEB_STATE_JS)
        if not isinstance(raw, str):
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def apply(self, position, playing, rate=1.0):
        return self.evaluate(_web_apply_js(position, playing, rate))

    def close(self):
        try:
            if self.ws is not None:
                self.ws.send_text(json.dumps({"id": 0, "method": "Browser.close"}))
        except Exception:
            pass
        try:
            if self.proc is not None:
                self.proc.terminate()
        except Exception:
            pass
        self.ws = None
        self.proc = None


_START_PAGE_STRINGS = {
    "zh": {
        "LANG": "zh-CN", "TITLE": "Nobplaypot 浏览器",
        "SUB": "一起看，同步播放 · 局域网同步模式",
        "PH": "搜索网页，或粘贴视频链接…", "SEARCH": "搜索",
        "SITE": "项目官网", "REPO": "GitHub 仓库",
        "Q1": "哔哩哔哩", "Q2": "YouTube", "Q3": "腾讯视频", "Q4": "爱奇艺",
        "TIPS": '["搜索网页，或粘贴视频链接…","试试：编程教学","试试：4K 纪录片","试试：B 站视频"]',
        "FOOT": "Nobplaypot %s · 浏览器同步模式（预览）",
    },
    "en": {
        "LANG": "en", "TITLE": "Nobplaypot Browser",
        "SUB": "Watch together, perfectly in sync · LAN sync mode",
        "PH": "Search the web, or paste a video link…", "SEARCH": "Search",
        "SITE": "Website", "REPO": "GitHub repo",
        "Q1": "Bilibili", "Q2": "YouTube", "Q3": "Tencent Video", "Q4": "iQIYI",
        "TIPS": '["Search the web, or paste a video link…","Try: coding tutorial","Try: 4K documentary","Try: a Bilibili video"]',
        "FOOT": "Nobplaypot %s · Browser Sync (preview)",
    },
}

_START_PAGE_HTML = '<!doctype html>\n<html lang="__LANG__"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>__TITLE__</title>\n<style>\n:root{--bg:#05070d;--fg:#e8eefc;--mut:#8ea0bd;--acc:#5b8cff;--acc2:#7b5bff;--card:rgba(18,25,38,.62)}\n*{box-sizing:border-box}\nhtml,body{height:100%;margin:0;background:var(--bg);color:var(--fg);overflow:hidden;\nfont-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif}\n#fx{position:fixed;inset:0;z-index:0;display:block}\n.grid{position:fixed;inset:0;z-index:0;opacity:.22;pointer-events:none;\nbackground-image:linear-gradient(rgba(140,170,255,.09) 1px,transparent 1px),linear-gradient(90deg,rgba(140,170,255,.09) 1px,transparent 1px);\nbackground-size:64px 64px;mask-image:radial-gradient(circle at 50% 42%,#000 26%,transparent 76%)}\n.vignette{position:fixed;inset:0;z-index:1;pointer-events:none;\nbackground:radial-gradient(circle at 50% 38%,transparent 30%,rgba(0,0,0,.55) 100%)}\nmain{position:relative;z-index:3;height:100%;display:flex;flex-direction:column;align-items:center;\njustify-content:center;gap:22px;padding:32px}\n.logo{display:flex;align-items:center;gap:14px;animation:rise .8s cubic-bezier(.2,.8,.2,1) both}\n.mark{width:54px;height:54px;border-radius:16px;position:relative;overflow:hidden;\nbackground:linear-gradient(140deg,var(--acc),var(--acc2));display:grid;place-items:center;\nfont-weight:800;font-size:22px;color:#080c15;box-shadow:0 14px 40px rgba(91,140,255,.45);\nanimation:pulse 3.6s ease-in-out infinite}\n.mark:after{content:"";position:absolute;inset:-40%;background:conic-gradient(from 0deg,transparent,rgba(255,255,255,.65),transparent 40%);\nanimation:spin 4.5s linear infinite}\n@keyframes spin{to{transform:rotate(360deg)}}\n@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}\nh1{font-size:31px;margin:0;letter-spacing:.6px}\n.sub{color:var(--mut);font-size:14px;margin:5px 0 0;letter-spacing:.3px}\n@keyframes rise{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}\n.card{width:min(680px,92vw);background:var(--card);border:1px solid rgba(140,170,255,.2);\nborder-radius:18px;padding:20px;backdrop-filter:blur(16px);position:relative;overflow:hidden;\nbox-shadow:0 26px 70px rgba(0,0,0,.5);animation:rise .9s .08s cubic-bezier(.2,.8,.2,1) both}\n.card:before{content:"";position:absolute;inset:0;border-radius:18px;padding:1px;pointer-events:none;\nbackground:linear-gradient(120deg,transparent 20%,rgba(120,160,255,.55),transparent 60%) border-box;\n-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);\n-webkit-mask-composite:xor;mask-composite:exclude;animation:sheen 6s linear infinite}\n@keyframes sheen{0%{background-position:-200% 0}100%{background-position:200% 0}}\nform{display:flex;gap:10px}\ninput{flex:1;padding:15px 16px;font-size:16px;border-radius:12px;border:1px solid #2a3550;\nbackground:rgba(8,12,20,.82);color:var(--fg);outline:none;transition:border-color .2s,box-shadow .2s}\ninput:focus{border-color:var(--acc);box-shadow:0 0 0 4px rgba(91,140,255,.18)}\nbutton.primary{padding:0 24px;font-size:15px;border:0;border-radius:12px;cursor:pointer;font-weight:700;\nbackground:linear-gradient(135deg,var(--acc),var(--acc2));color:#080c15;position:relative;overflow:hidden;\ntransition:transform .15s,filter .2s}\nbutton.primary:hover{transform:translateY(-1px);filter:brightness(1.1)}\n.links{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap}\na.chip{flex:1;min-width:150px;text-align:center;padding:12px 14px;border-radius:12px;text-decoration:none;\ncolor:var(--fg);border:1px solid rgba(140,170,255,.24);background:rgba(12,18,30,.72);font-size:14px;\ntransition:transform .18s,border-color .18s,background .18s,box-shadow .18s}\na.chip:hover{transform:translateY(-2px);border-color:var(--acc);background:rgba(28,42,70,.92);\nbox-shadow:0 10px 28px rgba(91,140,255,.28)}\n.quick{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;animation:rise 1s .16s both}\na.site{padding:8px 14px;border-radius:999px;text-decoration:none;color:var(--mut);font-size:13px;\nborder:1px solid rgba(140,170,255,.16);transition:color .18s,border-color .18s,transform .18s}\na.site:hover{color:var(--fg);border-color:var(--acc);transform:translateY(-2px)}\nfooter{position:fixed;bottom:14px;width:100%;text-align:center;color:#4f5f7a;font-size:12px;z-index:3}\n@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}\n</style></head>\n<body>\n<canvas id="fx"></canvas>\n<div class="grid"></div>\n<div class="vignette"></div>\n<main>\n  <div class="logo"><div class="mark">N</div>\n    <div><h1>Nobplaypot</h1><p class="sub">__SUB__</p></div>\n  </div>\n  <div class="card">\n    <form action="https://www.bing.com/search" method="get" target="_self">\n      <input id="q" type="text" name="q" placeholder="__PH__" autofocus autocomplete="off">\n      <button class="primary" type="submit">__SEARCH__</button>\n    </form>\n    <div class="links">\n      <a class="chip" href="https://nobri6039.github.io/Nobplaypot/" target="_blank">__SITE__</a>\n      <a class="chip" href="https://github.com/nobri6039/Nobplaypot" target="_blank">__REPO__</a>\n    </div>\n  </div>\n  <div class="quick">\n    <a class="site" href="https://www.bilibili.com" target="_self">__Q1__</a>\n    <a class="site" href="https://www.youtube.com" target="_self">__Q2__</a>\n    <a class="site" href="https://v.qq.com" target="_self">__Q3__</a>\n    <a class="site" href="https://www.iqiyi.com" target="_self">__Q4__</a>\n  </div>\n</main>\n<footer>__FOOT__</footer>\n<script>\n(function(){\n  var cv=document.getElementById(\'fx\'), ctx=cv.getContext(\'2d\');\n  var W=0,H=0,DPR=1,parts=[],stars=[],shoot=[],t=0;\n  var mouse={x:-9999,y:-9999};\n  function resize(){\n    DPR=Math.min(window.devicePixelRatio||1,2);\n    W=cv.width=Math.floor(innerWidth*DPR); H=cv.height=Math.floor(innerHeight*DPR);\n    cv.style.width=innerWidth+\'px\'; cv.style.height=innerHeight+\'px\';\n    var n=Math.round(Math.min(170,Math.max(60,innerWidth/11)));\n    parts=[];\n    for(var i=0;i<n;i++){\n      parts.push({x:Math.random()*W,y:Math.random()*H,\n        vx:(Math.random()-.5)*.34*DPR,vy:(Math.random()-.5)*.34*DPR,\n        r:(Math.random()*1.6+.6)*DPR, hue:Math.random()<.5?225:265});\n    }\n    stars=[]; for(var k=0;k<90;k++){stars.push({x:Math.random()*W,y:Math.random()*H*.7,r:Math.random()*1.3*DPR,a:Math.random()});}\n  }\n  addEventListener(\'resize\',resize); resize();\n  addEventListener(\'mousemove\',function(e){mouse.x=e.clientX*DPR;mouse.y=e.clientY*DPR;});\n  addEventListener(\'mouseleave\',function(){mouse.x=-9999;mouse.y=-9999;});\n\n  function aurora(){\n    var cx=W*.5, cy=H*.42;\n    for(var i=0;i<3;i++){\n      var ang=t*(0.00035+i*0.00018)+i*2.1;\n      var x=cx+Math.cos(ang)*W*(0.18+i*0.06);\n      var y=cy+Math.sin(ang*1.3)*H*(0.10+i*0.05);\n      var rad=Math.max(W,H)*(0.26+i*0.05)+Math.sin(t*0.0012+i)*30*DPR;\n      var g=ctx.createRadialGradient(x,y,0,x,y,rad);\n      var hue=[225,265,190][i];\n      g.addColorStop(0,\'hsla(\'+hue+\',95%,64%,.34)\');\n      g.addColorStop(.55,\'hsla(\'+hue+\',95%,56%,.13)\');\n      g.addColorStop(1,\'hsla(\'+hue+\',95%,50%,0)\');\n      ctx.fillStyle=g; ctx.fillRect(0,0,W,H);\n    }\n  }\n  function starfield(){\n    ctx.fillStyle=\'#fff\';\n    for(var i=0;i<stars.length;i++){\n      var s=stars[i];\n      s.a=0.25+0.75*Math.abs(Math.sin(t*0.0016+i));\n      ctx.globalAlpha=s.a*0.75;\n      ctx.beginPath(); ctx.arc(s.x,s.y,s.r,0,6.2832); ctx.fill();\n    }\n    ctx.globalAlpha=1;\n  }\n  function network(){\n    for(var i=0;i<parts.length;i++){\n      var p=parts[i];\n      p.x+=p.vx; p.y+=p.vy;\n      if(p.x<0||p.x>W)p.vx*=-1;\n      if(p.y<0||p.y>H)p.vy*=-1;\n      var dx=p.x-mouse.x, dy=p.y-mouse.y, d2=dx*dx+dy*dy;\n      if(d2<26000*DPR){var f=(1-d2/(26000*DPR))*0.9; p.x+=dx/(Math.sqrt(d2)+1)*f; p.y+=dy/(Math.sqrt(d2)+1)*f;}\n      ctx.beginPath(); ctx.arc(p.x,p.y,p.r,0,6.2832);\n      ctx.fillStyle=\'hsla(\'+p.hue+\',90%,68%,.85)\'; ctx.fill();\n    }\n    var link=175*DPR;\n    for(var a=0;a<parts.length;a++){\n      for(var b=a+1;b<parts.length;b++){\n        var dx2=parts[a].x-parts[b].x, dy2=parts[a].y-parts[b].y;\n        var dist=Math.sqrt(dx2*dx2+dy2*dy2);\n        if(dist<link){\n          ctx.strokeStyle=\'hsla(232,90%,70%,\'+(0.16*(1-dist/link))+\')\';\n          ctx.lineWidth=.7*DPR; ctx.beginPath();\n          ctx.moveTo(parts[a].x,parts[a].y); ctx.lineTo(parts[b].x,parts[b].y); ctx.stroke();\n        }\n      }\n    }\n    if(mouse.x>0){\n      for(var c=0;c<parts.length;c++){\n        var ddx=parts[c].x-mouse.x, ddy=parts[c].y-mouse.y, dd=Math.sqrt(ddx*ddx+ddy*ddy);\n        if(dd<210*DPR){\n          ctx.strokeStyle=\'hsla(190,95%,72%,\'+(0.35*(1-dd/(210*DPR)))+\')\';\n          ctx.lineWidth=.9*DPR; ctx.beginPath();\n          ctx.moveTo(parts[c].x,parts[c].y); ctx.lineTo(mouse.x,mouse.y); ctx.stroke();\n        }\n      }\n    }\n  }\n  function meteors(){\n    if(shoot.length===0&&Math.random()<0.014){\n      shoot.push({x:Math.random()*W,y:-40*DPR,vx:(1.6+Math.random()*1.4)*DPR,vy:(2.2+Math.random())*DPR,life:1});\n    }\n    for(var i=shoot.length-1;i>=0;i--){\n      var m=shoot[i];\n      m.x+=m.vx*3; m.y+=m.vy*3; m.life-=0.012;\n      var g2=ctx.createLinearGradient(m.x,m.y,m.x-m.vx*70,m.y-m.vy*70);\n      g2.addColorStop(0,\'rgba(190,215,255,\'+m.life+\')\');\n      g2.addColorStop(1,\'rgba(190,215,255,0)\');\n      ctx.strokeStyle=g2; ctx.lineWidth=1.6*DPR; ctx.beginPath();\n      ctx.moveTo(m.x,m.y); ctx.lineTo(m.x-m.vx*70,m.y-m.vy*70); ctx.stroke();\n      if(m.life<=0||m.y>H+60)shoot.splice(i,1);\n    }\n  }\n  function frame(){\n    t=performance.now();\n    ctx.clearRect(0,0,W,H);\n    var bg=ctx.createLinearGradient(0,0,W,H);\n    bg.addColorStop(0,\'#05070d\'); bg.addColorStop(.5,\'#080c16\'); bg.addColorStop(1,\'#060812\');\n    ctx.fillStyle=bg; ctx.fillRect(0,0,W,H);\n    aurora(); starfield(); network(); meteors();\n    requestAnimationFrame(frame);\n  }\n  requestAnimationFrame(frame);\n\n  var q=document.getElementById(\'q\'), tips=__TIPS__, ti=0;\n  if(tips.length){setInterval(function(){ti=(ti+1)%tips.length;q.setAttribute(\'placeholder\',tips[ti]);},2600);}\n  document.querySelectorAll(\'a.chip,button.primary\').forEach(function(el){\n    el.addEventListener(\'click\',function(ev){\n      var r=document.createElement(\'span\'), box=el.getBoundingClientRect();\n      r.style.cssText=\'position:fixed;left:\'+(ev.clientX-box.left)+\'px;top:\'+(ev.clientY-box.top)+\'px;\'+\n        \'width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,.6);pointer-events:none;\'+\n        \'transform:translate(-50%,-50%);transition:all .55s ease-out;z-index:9\';\n      el.appendChild(r);\n      requestAnimationFrame(function(){r.style.width=\'130px\';r.style.height=\'130px\';r.style.opacity=\'0\';});\n      setTimeout(function(){r.remove();},560);\n    });\n  });\n})();\n</script>\n</body></html>\n'


def start_page_template_path():
    """模板路径 = 程序目录(一般是 Bin) 下的 browser_start.html。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), START_PAGE_FILE)


def start_page_runtime_path():
    """渲染后的固定绝对路径(C:\\Temp\\nobplaypot_start.html, 不可写时退到 %TEMP%)。"""
    for folder in (START_PAGE_RUNTIME_DIR,
                   os.path.join(tempfile.gettempdir(), "Nobplaypot")):
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".np_write_probe")
            with open(probe, "wb") as handle:
                handle.write(b"1")
            os.remove(probe)
            return os.path.join(folder, START_PAGE_RUNTIME_NAME)
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), START_PAGE_RUNTIME_NAME)


def is_start_page_url(url):
    """判断 URL 是不是我们自己的本地起始页(不上报给主持, 语言切换时还要刷新它)。"""
    text = str(url or "").strip().lower()
    if not text.startswith("file:"):
        return False
    for name in (START_PAGE_RUNTIME_NAME.lower(), "start.html"):
        if name in text:
            return True
    return False


def ensure_start_page_template():
    """Bin 里没有模板时, 用内置字符串补一份(安装目录只读就静默跳过)。"""
    path = start_page_template_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 64:
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_START_PAGE_HTML)
        return path
    except OSError:
        return ""


def _load_start_template():
    """优先读 Bin\\browser_start.html; 读不到(开发目录/被删)才用内置字符串。"""
    try:
        with open(start_page_template_path(), "r", encoding="utf-8") as handle:
            text = handle.read()
        if "__TITLE__" in text and "__TIPS__" in text:
            return text
    except OSError:
        pass
    return _START_PAGE_HTML


def _write_start_page(lang="zh"):
    """渲染起始页到固定路径, 返回绝对路径(供 file:// 打开)。"""
    try:
        key = str(lang or "zh").lower()[:2]
        data = dict(_START_PAGE_STRINGS.get(key, _START_PAGE_STRINGS["zh"]))
        data["FOOT"] = data["FOOT"] % APP_VERSION
        html = _load_start_template()
        for name, value in data.items():
            html = html.replace("__" + name + "__", value)
        ensure_start_page_template()
        path = start_page_runtime_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
        return path
    except OSError:
        return ""


class EdgeBrowserShell:
    """应用内浏览器窗口(系统 Edge + CDP)。

    所有 CDP 调用都在后台线程执行: 前台只读缓存状态、只投递命令,
    避免页面加载时把 Tk 主线程卡死。
    """

    POLL_INTERVAL = 0.15
    TARGET_REFRESH = 1.5

    def _wait_attach(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._attach_newest():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                # 进程已经退出(被转交给旧实例), 立刻重试, 不用等满超时
                return False
            time.sleep(0.25)
        return False

    def _kill_stale_instances(self):
        """只清理使用本实例 profile 的旧 Edge, 不影响同一台机器上的另一个实例。"""
        kill_profile_edges(self.profile_dir)

    def __init__(self, profile_dir=None, lang="zh"):
        self.lang = lang
        self.profile_dir = profile_dir or BROWSER_PROFILE_DIR
        self.proc = None
        self.port = None
        self._ws = None
        self._send_lock = threading.Lock()
        self._id = 0
        self._commands = queue.Queue()
        self._state_lock = threading.Lock()
        self._state = {"ok": False, "url": "", "t": 0.0, "p": False, "r": 1.0}
        self._state_ts = 0.0
        self._stop = threading.Event()
        self._worker = None
        self._attached = None
        self._known = set()
        self._tabs_cache = []
        self._dead = False
        self._fail_count = 0

    @staticmethod
    def edge_path():
        candidates = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def available(self):
        return self.edge_path() is not None

    # ------------------------------------------------------------ 生命周期
    def open(self, url=""):
        if self.proc is not None:
            return True
        edge = self.edge_path()
        if edge is None:
            raise RuntimeError("未找到 Microsoft Edge, 无法打开内置浏览器")
        start_page = _write_start_page(getattr(self, "lang", "zh"))
        os.makedirs(self.profile_dir, exist_ok=True)
        self.port = _free_tcp_port(9300, 9399)
        # 默认窗口大小按屏幕比例给, 避免出现竖屏窄窗
        try:
            import ctypes
            screen_w = int(ctypes.windll.user32.GetSystemMetrics(0))
            screen_h = int(ctypes.windll.user32.GetSystemMetrics(1))
        except Exception:
            screen_w, screen_h = 1920, 1080
        win_w = max(960, int(screen_w * 0.72))
        win_h = max(600, int(screen_h * 0.78))
        # 普通窗口(不是 --app): 上方保留地址栏, 站内链接按新选项卡打开
        args = [
            edge,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=%d,%d" % (win_w, win_h),
            # 普通窗口: 保留地址栏; 标签栏由代码维持"只有一个标签页"
            "--new-window",
            "file:///" + start_page.replace("\\", "/") if start_page else BROWSER_START_URL,
        ]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        last_error = "浏览器调试端口连接失败"
        for attempt in range(2):
            if attempt:
                # 上一轮失败多半是旧实例占着同一个 user-data-dir:
                # Edge 会把新进程转交给旧实例, 新调试端口根本不会监听。
                self._kill_stale_instances()
                time.sleep(0.8)
                self.port = _free_tcp_port(9300, 9399)
                args[1] = "--remote-debugging-port=%d" % self.port
                self.proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if self._wait_attach(6.0 if attempt == 0 else 12.0):
                break
            try:
                self.proc.terminate()
            except Exception:
                pass
            last_error = "浏览器调试端口连接失败 (端口 %d)" % self.port
        else:
            self._kill_stale_instances()
            raise RuntimeError(last_error)
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="browser-cdp")
        self._worker.start()
        if url:
            self.navigate(url)
        # 等第一次状态采集完成, 避免调用方读到空状态
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._state.get("ok") or self._state.get("url"):
                    break
            time.sleep(0.2)
        return True

    def _targets(self):
        url = "http://127.0.0.1:%d/json" % self.port
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _pick_target(targets):
        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for target in pages:
            url = str(target.get("url") or "")
            if url.startswith(("http://", "https://", "file://")):
                return target
        return pages[0] if pages else None

    def _attach_newest(self):
        try:
            targets = self._targets()
        except Exception:
            return False
        target = self._pick_target(targets)
        if target is None:
            return False
        try:
            self._ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"])
        except Exception:
            self._ws = None
            return False
        self._attached = target.get("id")
        self._known = {t.get("id") for t in targets if t.get("type") == "page"}
        return True

    def _connect_new_tab(self, targets):
        """页面上点了 target=_blank 会开新选项卡: 跟随最新创建的那一个。"""
        pages = {t.get("id"): t for t in targets if t.get("type") == "page"}
        fresh = [tid for tid in pages if tid not in self._known]
        if not fresh:
            return False
        target = self._pick_target([pages[tid] for tid in fresh])
        if target is None:
            return False
        try:
            ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"])
        except Exception:
            return False
        self._ws = ws
        self._attached = target.get("id")
        self._known = set(pages)
        return True

    # ------------------------------------------------------------ CDP 原语
    def _send(self, method, params=None):
        with self._send_lock:
            if self._ws is None:
                return None
            self._id += 1
            command_id = self._id
            self._ws.send_text(json.dumps({
                "id": command_id, "method": method, "params": params or {},
            }))
            return command_id

    def _wait_result(self, command_id, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv_text(timeout=max(0.3, deadline - time.monotonic()))
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if payload.get("id") != command_id:
                continue
            return payload.get("result") or {}
        return None

    def _evaluate(self, expression, timeout=3.0):
        command_id = self._send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": False,
        })
        if command_id is None:
            return None
        result = self._wait_result(command_id, timeout)
        if not result:
            return None
        return (result.get("result") or {}).get("value")

    def _read_now(self):
        raw = self._evaluate(_WEB_STATE_JS)
        if not isinstance(raw, str):
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # --------------------------------------------------------- 后台轮询线程
    def _run(self):
        last_refresh = 0.0
        while not self._stop.is_set():
            try:
                self._drain_commands()
                if self._attached:
                    if self._port_alive():
                        self._fail_count = 0
                    else:
                        self._fail_count += 1
                        if self._fail_count >= 8:
                            self._dead = True
                now = time.monotonic()
                if now - last_refresh >= self.TARGET_REFRESH:
                    last_refresh = now
                    try:
                        targets = self._targets()
                    except Exception:
                        targets = None
                        self._fail_count += 1
                        if self._fail_count >= 5:
                            # 连续失败: 浏览器窗口多半已经被关掉了
                            self._dead = True
                    else:
                        self._fail_count = 0
                    if targets is not None:
                        ids = {t.get("id") for t in targets if t.get("type") == "page"}
                        if self._attached not in ids:
                            self._attach_newest()
                        else:
                            self._ensure_real_page()
                            self._detect_active_tab()
                        try:
                            # 标签页列表只在这里(后台线程)拉, 供 UI 侧读缓存
                            self._tabs_cache = self.list_tabs()
                        except Exception:
                            pass
                state = self._read_now()
                if state is not None:
                    with self._state_lock:
                        self._state = state
                        self._state_ts = time.monotonic()
            except Exception:
                log.debug("浏览器轮询异常", exc_info=True)
                self._ws = None
                time.sleep(0.5)
            time.sleep(self.POLL_INTERVAL)

    def _follow_active_video(self, targets):
        """当前标签页没有视频时, 自动找到其它标签页里正在播放的视频并切过去。"""
        try:
            with self._state_lock:
                has_video = bool(self._state.get("ok"))
        except Exception:
            return
        if has_video:
            return
        pages = [t for t in (targets or [])
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if len(pages) < 2:
            return
        for target in pages:
            if target.get("id") == self._attached:
                continue
            state = self._probe_target(target)
            if state and state.get("ok"):
                try:
                    self._ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"])
                    self._attached = target.get("id")
                    log.info("同步跟随到含视频的标签页: %s", state.get("url"))
                except Exception:
                    log.debug("切换到视频标签页失败", exc_info=True)
                return

    @staticmethod
    def _probe_target(target):
        ws = None
        try:
            ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"], timeout=1.5)
            ws.send_text(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": _WEB_STATE_JS, "returnByValue": True},
            }))
            raw = ws.recv_text(timeout=1.5)
            payload = json.loads(raw)
            value = ((payload.get("result") or {}).get("result") or {}).get("value")
            return json.loads(value) if isinstance(value, str) else None
        except Exception:
            return None
        finally:
            if ws is not None:
                try:
                    ws._sock.close()
                except Exception:
                    pass

    def _ensure_real_page(self):
        """当前挂着的可能是 edge:// 内部页(同步确认框等), 迁到真实网页。"""
        try:
            targets = self._targets()
        except Exception:
            return
        real = [t for t in targets
                if t.get("type") == "page"
                and str(t.get("url") or "").startswith(("http://", "https://", "file://"))]
        if not real:
            return
        for target in real:
            if target.get("id") == self._attached:
                return
        self._select_now(real[0].get("id"))

    def _port_alive(self):
        """快速判断调试端口还在不在(浏览器关掉后端口会立刻消失)。"""
        if self.port is None:
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        try:
            return sock.connect_ex(("127.0.0.1", self.port)) == 0
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _detect_active_tab(self):
        """找出用户真正在前台看的标签页(可见且获焦), 不能只信我们自己的连接。"""
        try:
            targets = self._targets()
        except Exception:
            return
        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                 and str(t.get("url") or "").startswith(("http://", "https://", "file://"))]
        if not pages:
            return
        if len(pages) == 1:
            target = pages[0]
            if target.get("id") != self._attached:
                self._select_now(target.get("id"))
            return
        fallback = None
        chosen = None
        for target in pages:
            info = self._probe_visibility(target)
            if not info or not info.get("visible"):
                continue
            if info.get("focus"):
                chosen = target
                break
            if fallback is None:
                fallback = target
        if chosen is None:
            chosen = fallback
        if chosen is None:
            return
        if chosen.get("id") != self._attached:
            self._select_now(chosen.get("id"))

    @staticmethod
    def _probe_visibility(target):
        ws = None
        try:
            ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"], timeout=1.5)
            ws.send_text(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression":
                           "JSON.stringify({v:document.visibilityState,"
                           "f:document.hasFocus()})",
                           "returnByValue": True},
            }))
            payload = json.loads(ws.recv_text(timeout=1.5))
            value = ((payload.get("result") or {}).get("result") or {}).get("value")
            data = json.loads(value) if isinstance(value, str) else {}
            return {"visible": data.get("v") == "visible",
                    "focus": bool(data.get("f"))}
        except Exception:
            return None
        finally:
            if ws is not None:
                try:
                    ws._sock.close()
                except Exception:
                    pass

    def _absorb_new_tab(self):
        """网页里 target=_blank 会开新标签页: 把它读出来, 关掉, 在当前页打开。"""
        try:
            targets = self._targets()
        except Exception:
            return
        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if len(pages) < 2:
            return
        for target in pages:
            if target.get("id") == self._attached:
                continue
            url = str(target.get("url") or "")
            self.close_tab(target.get("id"))
            if url and not url.startswith(("about:", "edge:", "chrome:", "devtools:")):
                self.navigate(url)
            return

    @property
    def dead(self):
        return self._dead

    def set_rate(self, rate):
        """只改播放倍速, 不动 currentTime。"""
        js = ("(function(){var v=document.querySelector('video');"
              "if(!v){return 'no-video';}"
              "try{v.playbackRate=" + repr(float(rate)) + ";}catch(e){}"
              "return 'ok';})()")
        return self.evaluate(js)

    def cached_tabs(self):
        """UI 侧只读缓存, 不做 HTTP 请求(否则会卡 Tk 主线程)。"""
        return list(self._tabs_cache)

    def select_tab(self, tab_id):
        """把本地指向切到指定标签页, 并把 CDP 会话接到它上面。"""
        self._commands.put(("select", tab_id))

    def _select_now(self, tab_id):
        try:
            targets = self._targets()
        except Exception:
            return
        target = None
        for item in targets:
            if item.get("id") == tab_id:
                target = item
                break
        if target is None or not target.get("webSocketDebuggerUrl"):
            return
        try:
            self.activate_tab(tab_id)
        except Exception:
            pass
        try:
            self._ws = _MiniWebSocket.connect(target["webSocketDebuggerUrl"])
            self._attached = tab_id
            self._known = {x.get("id") for x in targets if x.get("type") == "page"}
        except Exception:
            log.debug("切换 CDP 会话失败", exc_info=True)

    def _drain_commands(self):
        while True:
            try:
                action, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "navigate":
                    self._send("Page.navigate", {"url": payload})
                elif action == "apply":
                    position, playing, rate = payload
                    self._evaluate(_web_apply_js(position, playing, rate))
                elif action == "select":
                    self._select_now(payload)
            except Exception:
                log.debug("浏览器命令执行失败: %s", action, exc_info=True)

    # ------------------------------------------------------------ 对外 API
    @property
    def attached_id(self):
        return self._attached or ""

    def list_tabs(self):
        """按浏览器返回顺序列出所有标签页。"""
        try:
            targets = self._targets()
        except Exception:
            return []
        tabs = []
        for target in targets:
            if target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            if not url or url.startswith("devtools://"):
                continue
            # 过滤 Edge 内部页(新建标签页确认框、设置页等), 这些不该同步
            if url.startswith(("edge://", "chrome://", "about:")):
                continue
            if "confirmation-dialog" in url:
                continue
            tabs.append({"id": target.get("id"), "url": url,
                         "title": target.get("title") or ""})
        return tabs

    def close_tab(self, tab_id):
        try:
            endpoint = "http://127.0.0.1:%d/json/close/%s" % (self.port, tab_id)
            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read()
            return True
        except Exception:
            return False

    def activate_tab(self, tab_id):
        try:
            endpoint = "http://127.0.0.1:%d/json/activate/%s" % (self.port, tab_id)
            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read()
            return True
        except Exception:
            return False

    def open_tab(self, url):
        """在浏览器里新开一个选项卡(主持新开的标签页同步过来时用)。"""
        if self.port is None:
            return False
        try:
            endpoint = "http://127.0.0.1:%d/json/new?%s" % (
                self.port, urllib.parse.quote(url, safe=""))
            request = urllib.request.Request(endpoint, method="PUT")
            with urllib.request.urlopen(request, timeout=3) as response:
                raw = response.read().decode("utf-8", "ignore")
            try:
                return (json.loads(raw) or {}).get("id")
            except ValueError:
                return None
        except Exception:
            log.debug("新建标签页失败, 退化为当前页导航", exc_info=True)
            self.navigate(url)
            return False

    def read_state(self):
        """返回缓存状态(不阻塞)。"""
        with self._state_lock:
            return dict(self._state)

    def state_age(self):
        with self._state_lock:
            return time.monotonic() - self._state_ts

    def navigate(self, url):
        self._commands.put(("navigate", url))

    def apply(self, position, playing, rate=1.0):
        self._commands.put(("apply", (float(position), bool(playing), float(rate))))

    def close(self):
        self._stop.set()
        try:
            self._send("Browser.close")
        except Exception:
            pass
        time.sleep(0.2)
        try:
            if self.proc is not None:
                self.proc.terminate()
        except Exception:
            pass
        try:
            self._kill_stale_instances()
        except Exception:
            pass
        self._ws = None
        self.proc = None
