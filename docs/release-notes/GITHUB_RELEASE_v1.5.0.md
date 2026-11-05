# GitHub Release v1.5.0 — 推送内容

标题：

```
Nobplaypot v1.5.0 · 后台捕获 + 本机缩放 + 延迟大降
```

Tag：`v1.5.0`　目标分支：`main`　类型：正式版（Latest）

附件（上传这个文件）：

```
F:\Download\Nobplaypot\Nobplaypot-1.5.0.zip
大小: 68.1 MB   文件数: 291
SHA256: EF478DD1AC8C1A99E9F4F12140BAE69EF7C5A95E1178032AD739605E2839D32A
```

Release 说明（直接粘贴）：

---

## Nobplaypot v1.5.0

**定位升级：从「一起看视频」到「局域网多人协同」。** 现在一个房间里可以**一起看视频**、**一起操作同一个软件**（画面 + 鼠标键盘实时回传）、**一起同步网页**，房间、密码、成员权限全部共用。

这一版的技术主题是**后台捕获**和**延迟**：共享的窗口被别的窗口盖住也能抓、缩放彻底交给房客本机、端到端延迟从 ~45ms 压到 ~25ms。

### 新增

**应用共享 · 后台捕获（PrintWindow）**

- 默认改用 `PrintWindow` 采集：窗口被别的窗口完全盖住、或者没有焦点，也照样抓得到完整画面
- 先试 `WM_PRINT`，画出来是纯色再试 `PW_RENDERFULLCONTENT`；两种都拿不到内容就自动退回屏幕 BitBlt，并在日志说明原因
- 老版屏幕截图保留，两处可切：共享对话框的「后台捕获」开关、设置 → 应用共享 → 「捕获方式」
- 统计日志带上 `捕获=printwindow|screen`，一眼看出当前用哪种
- 最小化仍然抓不到（Windows 不合成最小化窗口，OBS 也一样）

**应用共享 · 缩放改成房客本机**

- 主持端永远只传原画 100%，不再跟着某个房客的缩放请求改采集尺寸
- 房客点 ± 只改自己窗口，线性档位 25 / 50 / 75 / 100 / 125 / 150 / 175 / 200%，多人同时看互不影响
- 加入时按自己屏幕自动挑装得下的档位（屏幕小自动降到 50%/25%），记一条 `[lat] frames.fit`
- 共享窗口禁止手动拉伸（拉了会变形），放大 HALFTONE 平滑、缩小 COLORCOLOR 保速度

**延迟优化（实测 45~55ms → 25~28ms）**

- 修掉客户端最大的一处浪费：只要目标比帧大 1 像素就用 HALFTONE 插值，实测 15~24ms/帧；现在差 ≤2px 直接 BitBlt，只有放大 ≥2 倍才用 HALFTONE，贴图降到 **0.7ms**
- HDC / 内存 DC 缓存复用，不再每帧创建销毁
- 主持端 1:1 抓屏改 BitBlt（13ms → 9.7ms）；静止判定指纹从已抓下来的位图算，不再二次读屏，抓取+编码 25~30ms → **17~19ms**
- 客户端收包非阻塞、复用缓冲、一次收完，只解码最新一帧（队列最多留 2 帧）
- 新增 `[lat] frames.recv … wire=… decode=… blit=…` 分阶段日志

**浏览器同步 · 起始页随程序走**

- 模板在 `Bin\browser_start.html`，程序用相对路径读
- 运行时渲染到固定路径 `C:\Temp\nobplaypot_start.html`：浏览器的 `file://` 是绝对路径，主持与房客安装目录不同，固定同一路径后同步的网址两边都打得开
- `C:\Temp` 不可写时退到 `%TEMP%\Nobplaypot\`；起始页进 payload 清单参与签名校验

### 修复

- 共享对话框「画质」下拉框没显示，只剩一个孤零零的「画质」二字（创建后忘了 pack）
- 首次加入时窗口尺寸不是主持端传过来的 100%，而是被采集上限压过的小尺寸
- 放大到 100% 以上窗口"连动都不动"（`最大抓取宽度` 把 `源宽×倍率` 夹死）
- 放大后百分比标签一直显示 100%
- 英文界面里画质三档、后台捕获、捕获方式等文案补上翻译

### 已知限制

- 微信、多数游戏、部分 Electron/自绘程序不处理合成窗口消息，仍然只能观看
- `PrintWindow` 对 D3D 直出画面（游戏/部分播放器）可能拿到黑屏，会自动退回屏幕采集
- 跨进程 `PrintWindow` 会被 UIPI 拦截：程序被降权运行时可能抓不到别的进程窗口
- 浏览器同步受站点限制：部分站点禁止改 `playbackRate` 或拦截 `currentTime` 跳转

### 下载与校验

`Nobplaypot-1.5.0.zip`（68.1 MB）解压即用，Windows 10/11 x64，无需安装 Python 或 VLC。

SHA256：`EF478DD1AC8C1A99E9F4F12140BAE69EF7C5A95E1178032AD739605E2839D32A`

> 升级时请**整包覆盖**：`Bin\payload.manifest`、`payload.sig` 必须和同包的 `Bin\*.py` 一起替换，否则启动器会拒绝启动。

完整发布说明见 [`docs/release-notes/RELEASE_NOTES_v1.5.0.md`](https://github.com/Nobri6039/Nobplaypot/blob/main/docs/release-notes/RELEASE_NOTES_v1.5.0.md)

---

## 发布前检查清单

1. 双击 `Nobplaypot.exe`，`Bin\launch.log` 出现「清单签名校验通过, 条目数=287 / payload 文件哈希全部通过」（本地已验过）
2. 主菜单左下角版本号显示 `v1.5.0`
3. 官网 `docs/index.html` 品牌版本已是 `v1.5.0`，下载按钮指向 `releases/latest`
4. Release 里上传 `Nobplaypot-1.5.0.zip`，勾选 **Set as the latest release**
