# Nobplaypot v1.2.3

Released 2026-11-01 · [中文说明](#中文) · [Website](https://nobri6039.github.io/Nobplaypot/)

## Highlights

### Browser sync (preview)
- A lightweight alternative to media streaming: nothing is re-streamed. Only the page URL and playback position are exchanged over the existing TCP command port.
- Both ends run their own browser window. Play / pause / seek / speed sync in **both directions**, and a guest can navigate everyone to another page.
- Intended for free HTML5 video pages. DRM content is out of scope by design.
- This mode needs a single TCP port; no UDP media port is opened.

### Bilingual UI
- Full 中文 / English switch in Settings → General, applied when you press Save.
- Every page, dialog, tooltip and status line is covered, including dynamic text such as `5 s`, `12 messages` and `Room 2 · Host …`.
- The in-app start page and the browser sync hint block follow the selected language as well.

### Start page
- The built-in start page was rebuilt: search box, links to the website and the GitHub repository, quick entries to common video sites, plus an animated background (aurora, starfield, particle network, meteors).
- It is generated per language, and switching the language refreshes an open start page immediately.

### Streaming fixes
- Synchronised dragging works in the browser-sync mode even when the page's duration is unknown (ratio-based seek).
- The host no longer binds the inspector to a browser-internal page, so seek and speed now reach the actual video element.
- Media-mode sockets are released correctly when switching between streaming and browser sync.

## Upgrade notes
- Settings are compatible with v1.2.1. Existing rooms, ports and profiles keep working.
- The browser-sync mode remains a preview; expect rough edges on sites that block scripted `currentTime`.

## Files
- `Nobplaypot.py` — main application
- `docs/` — GitHub Pages site
- `screenshots/` — UI screenshots

---

## 中文

### 浏览器同步（预览）
- 比推流更轻的第二种模式：不转发画面与声音，只通过现有 TCP 指令端口同步网页地址与播放进度。
- 两端各自使用自己的浏览器窗口，播放 / 暂停 / 进度 / 倍速双向同步，房客也能为所有人切换页面。
- 面向免费 HTML5 视频页面；DRM 内容不在设计范围内。
- 该模式只需要一个 TCP 端口，不占用 UDP 媒体端口。

### 双语界面
- 设置 → 常规里可切换中文 / 英文，按保存后整体生效。
- 覆盖所有页面、对话框、提示与状态栏，包括 `5 秒`、`12 条`、`房间 2 人 · 主持 …` 这类动态文案。
- 内置起始页与浏览器同步说明同样跟随语言。

### 起始页
- 起始页重做：搜索框、官网与 GitHub 仓库入口、常用视频站快捷入口，以及动效背景（极光、星空、粒子网络、流星）。
- 按语言分别生成；切换语言时会立即刷新已打开的起始页。

### 推流相关修复
- 浏览器同步模式下即使拿不到网页总时长，也能拖动进度（按比例发送）。
- 主持端不再把调试连接挂在浏览器内部页上，跳转与倍速能真正作用到视频元素。
- 在推流与浏览器同步之间切换时，媒体端口会被正确释放。

### 升级说明
- 配置与 v1.2.1 兼容，房间、端口与浏览器配置沿用。
- 浏览器同步仍为预览模式；对禁止脚本修改 `currentTime` 的网站可能有限制。
