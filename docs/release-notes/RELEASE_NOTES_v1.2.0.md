# Nobplaypot v1.2.0 — Release Notes / 更新说明

## English

### Highlights

- **New UDP media pipeline.** The custom TCP distributor, sequence numbers, FEC, NACK, jitter buffer and playout clock are gone. The host now forwards the VLC TS datagrams as-is over UDP; each client registers with a small UDP packet and re-emits the raw TS to a local VLC. Jitter, reordering and buffering are handled by VLC itself.
- **1x playback is now a stream copy.** The host remuxes the source's own video/audio ES into TS and sends it over UDP — no re-encode, no encoder latency, almost no CPU. Only non-1x speeds build the x264 `tune=zerolatency` transcode chain.
- **Lower default latency.** The client buffer default is now **400 ms** (was 2000 ms), adjustable from 200 to 5000 ms. On loopback/LAN the remaining delay is essentially the client buffer plus the network.
- **Buffering indicator.** A transparent rotating-dot spinner with a "缓冲中" label appears over the video whenever the play/pause button says "playing" and the actual displayed-frame count has not advanced for 450 ms (or is still 0). It covers joining, the first frame, pause → play, seeking, rate changes and stalls.
- **Scalable spinner.** The dots and label scale with the video area and with a new **Settings → Streaming → Buffering icon scale** (0.5x–2.5x). The choice is persisted.
- **New clients no longer restart the movie.** A roster change rebuilds the `sout` media; the rebuild now re-applies the current position with `:start-time` and `set_time`, so the host and every client continue from the current position instead of 00:00.
- **Pause / resume fixes.** If the client's local VLC ended during a pause, the client reconnects immediately on resume (with the buffering overlay) instead of waiting for the 5-second watchdog; if it is merely paused, it resumes directly.
- **Async join.** The initial command-channel connect runs on a background thread, so DNS/connect no longer freezes the UI for several seconds.
- **Diagnostics.** The media path logs `媒体注册` on the host and `媒体流首包到达` on the client.

### Configuration

- Command channel: TCP, default 5000.
- Media channel: **UDP**, default 5001.
- Client buffer default 400 ms; buffering-icon scale default 1.0x.
- Config version 4: older configs with a client buffer above 800 ms are migrated to 400 ms once. The buffer and the icon scale can be changed at any time in Settings.

### Networking notes

- LAN: map/allow the host's TCP command port and UDP media port; the client only sends outbound traffic.

### Verification

- Stream regression: 38/38 checks (the test clip has a long keyframe interval; under stream-copy its start/seek timing is source-dependent).
- Same-machine LAN IP with `飞驰人生2.mp4` (1920×818): client playing, picture 1920×818, playback time advancing in real time, zero restarts.

### Known limitations

- At 1x the picture is the source's own codec/resolution; a source with a very long keyframe interval starts and seeks slower.
- Seeking, speed changes and membership changes briefly reconnect clients (sout must be rebuilt); the position is preserved.
- Only non-1x speeds use CPU.

---

## 中文

### 重点

- **全新 UDP 媒体通道。** 自研的 TCP 分发、序号、FEC、NACK、抖动缓冲和播放时钟全部移除。主持端把 VLC 的 TS 数据报按原样通过 UDP 转发；客户端用一个很小的 UDP 包注册，收到后把 TS 原包交给本机 VLC。抖动、乱序、缓冲全部由 VLC 处理。
- **1x 播放改为流复制。** 主持端把源文件自己的视频/音频 ES 复用到 TS 后通过 UDP 发出——不重编码、没有编码延迟、几乎不吃 CPU。只有变速（≠1x）才构建 x264 `tune=zerolatency` 转码链。
- **默认延迟降低。** 客户端缓冲默认从 2000ms 降到 **400ms**，设置里可在 200–5000ms 调整。回环/局域网下剩余延迟基本只剩客户端缓冲和网络。
- **缓冲提示。** 只要底部播放按钮是“播放中”且实际显示帧数 450ms 没有增长（或仍为 0），视频中央就显示透明的旋转圆点 + “缓冲中”。覆盖刚加入、首帧、暂停转播放、拖进度、变速、卡顿等场景。
- **图标可缩放。** 圆点和文字随视频区大小缩放，并新增 **设置 → 流式传输 → 缓冲图标缩放**（0.5x–2.5x），选择会保存。
- **新客户端加入不再回到片头。** 成员变化需要重建 `sout` media；重建时用 `:start-time` 和 `set_time` 重新应用当前进度，主持和所有客户端都从当前位置继续，而不是 00:00。
- **暂停 / 恢复修复。** 如果暂停期间客户端本地 VLC 已经结束，恢复播放时客户端立即重连并显示缓冲层，不再等 5 秒看门狗；如果只是暂停状态，则直接继续播放。
- **异步加入。** 指令通道的首次连接放到后台线程，DNS/拨号不再卡住界面数秒。
- **诊断日志。** 主持端输出 `媒体注册`，客户端输出 `媒体流首包到达`。

### 配置

- 指令通道：TCP，默认 5000。
- 媒体通道：**UDP**，默认 5001。
- 客户端缓冲默认 400ms；缓冲图标缩放默认 1.0x。
- 配置版本 4：旧配置中客户端缓冲大于 800ms 的会一次性迁移到 400ms；缓冲和图标缩放可随时在设置里修改。

### 网络说明

- 局域网：放行主持端的 TCP 指令端口和 UDP 媒体端口；客户端只做出站连接。

### 验证

- 流式回归：38/38 项通过（测试用小样片关键帧间隔较长，流复制下起播/跳转时机取决于源文件）。
- 同机局域网 IP + `飞驰人生2.mp4`（1920×818）：客户端正常播放，画面 1920×818，播放时间实时推进，零重启。

### 已知限制

- 1x 下画面是源文件自己的编码/分辨率；源文件关键帧间隔很长时起播和跳转会慢一些。
- 拖动进度、变速、成员变化会让客户端短暂重连（sout 必须重建），进度会保留。
- 只有变速才吃 CPU。
