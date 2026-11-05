# Nobplaypot v1.2.2 发布说明

> 2026-10-29 · Nobri with deepseek

## 新增

- **原生启动器**：新增 C++ 入口 `Nobplaypot.exe`，读取 `Bin\launch.json` 后直接启动便携 Python，不再依赖 bat，也没有控制台黑窗口。
- **启动完整性校验**：启动器会先用内置公钥校验 `payload.manifest` 的 RSA 签名，再逐个校验清单文件的 SHA-256；py、运行时或 VLC 被替换时拒绝启动并提示。
- **音轨切换**：主控制栏新增「音轨 / 字幕」按钮，仅当视频有多条音轨或字幕时出现；可随时切换音轨。
- **字幕选择**：支持内嵌字幕的版本选择，并带「关闭字幕」；不再启动时强制关闭全部字幕。
- **全屏聊天**：全屏时鼠标移到屏幕右缘，右侧聊天面板自动滑出；可查看历史、输入并发送，主窗口聊天与全屏聊天双向同步。
- **缓冲动画重做**：圆点改为字体字形渲染 + 外发光 + 闪烁，带 Q 弹缩放和呼吸；各缩放下文字不再与圆环重叠。
- **多显示器支持**：全屏控制岛和聊天面板按当前显示器矩形定位，鼠标移到第二块屏幕会正常收起。

## 修复

- 修复多音轨视频把 `-1 Disable` 当成真实音轨的问题，现在固定第一条真实音轨。
- 修复换片/重连时旧播放器在后台 stop 阻塞期间继续出声导致的“两份声音”。
- 官网截图不再用固定 16:10 裁切，按自然比例展示；全屏控制岛截图单独占满一行，避免只剩中间一小段。

## 官网

- GitHub Pages：https://nobri6039.github.io/Nobplaypot/
- 截图展示已修复错位与裁切问题。

## 下载与运行

- Windows 10 / 11 · 64 位
- 便携版无需安装 Python 或 VLC
- 源码运行需要 Python 3.10+ 与 64 位 VLC 3.x

---

# Nobplaypot v1.2.2 Release Notes

## Added

- **Native launcher**: a small C++ `Nobplaypot.exe` reads `Bin\launch.json` and starts the portable Python directly — no bat file, no console window.
- **Launch integrity check**: the launcher verifies the RSA signature of `payload.manifest` with its embedded public key, then verifies the SHA-256 of every listed file; modified py, runtime or VLC files are rejected.
- **Audio track switching**: a "音轨 / 字幕" button appears in the main control bar only when the video has multiple audio tracks or subtitles.
- **Subtitle selection**: embedded subtitle tracks can be selected, with a "关闭字幕" option; subtitles are no longer force-disabled at startup.
- **Fullscreen chat**: hovering the right edge of the screen slides out the room chat; reading and sending are supported and stay in sync with the main window.
- **Buffering animation redesign**: glyph-based dots with glow and twinkle, Q-elastic scaling and breathing; text no longer overlaps the ring at any scale.
- **Multi-monitor support**: the fullscreen dock and chat are positioned against the current monitor; moving the mouse to a second monitor now hides them correctly.

## Fixed

- Multi-audio files no longer treat `-1 Disable` as a real track; the first real audio track is selected.
- The old player is muted and paused before the asynchronous stop, so swapping files or reconnecting no longer produces two overlapping audio streams.
- Website screenshots are shown at their natural aspect ratio instead of a fixed 16:10 crop; the control-island screenshot spans the full row so it is no longer cropped to a tiny center strip.

## Website

- GitHub Pages: https://nobri6039.github.io/Nobplaypot/

## Download

- Windows 10 / 11, 64-bit
- Portable build: no Python or VLC installation required
- From source: Python 3.10+ and 64-bit VLC 3.x
