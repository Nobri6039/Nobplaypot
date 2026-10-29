**English** | [中文](README.zh-CN.md)

🌐 **Website**: https://nobri6039.github.io/Nobplaypot/

# Nobplaypot

A dark-themed video player for Windows built with **VLC (libvlc) + Tkinter**, made for LAN co-watching: host a stream, let others join, protect the room with a password, and control playback / seeking / speed remotely.

> 一个用 **VLC（libvlc）+ Tkinter** 编写的深色视频播放器，支持局域网多人同步观看。

The entire UI is hand-drawn (no stock ttk widgets): anti-aliased rounded corners and icons, a dark title bar, and a floating control island that rises from the bottom in fullscreen.

![Main window](screenshots/main.png)

## Screenshots

**Fullscreen control island**

<img src="screenshots/fullscreen-dock.png" width="820" alt="Fullscreen control island">

**Settings · Streaming**

<img src="screenshots/settings.png" width="620" alt="Settings">

**Room password**

<p>
<img src="screenshots/host-password.png" width="320" alt="Host password setup">
<img src="screenshots/room-password.png" width="260" alt="Client password prompt">
</p>

**Room members**

<img src="screenshots/room_members_host.png" width="420" alt="Room members">

**Room chat**

<img src="screenshots/chat_main_host.png" width="760" alt="Room chat">

**Danmaku**

<img src="screenshots/danmaku_window.png" width="760" alt="Danmaku">

**Chat room settings**

<img src="screenshots/settings_tab_chat.png" width="620" alt="Chat room settings">

**Theme: Violet**

<img src="screenshots/theme-violet.png" width="620" alt="Violet theme">

## Features

### Playback
- Drag & drop a video file, or open one through the custom dark file dialog
- Play / pause, seek, volume / mute, 0.25x–4.0x playback speed
- Fullscreen playback with arrow-key seeking and auto-hiding controls
- Supports MP4 / MKV / AVI / MOV / WMV / FLV / WEBM / TS / RMVB and more

### UI
- Everything drawn on Canvas: buttons, sliders, dropdowns, file list, dialogs, empty-state card
- Anti-aliased icons and rounded corners; the play button is an analytically anti-aliased circle
- 5 accent colors (Aurora / Violet / Teal / Rose / Amber) × 3 background tones (Midnight / Black / Slate)
- Hover transition animations, which can be disabled in settings

### Fullscreen control island
- Move the mouse toward the bottom of the screen and a rounded floating bar rises — it sits **above** the bottom edge instead of sticking to it
- Play / pause, skip back / forward, progress, volume, speed, exit fullscreen
- Slides away 1.6 seconds after the mouse leaves

### Streaming (LAN / internet)
- **Host**: opens a TCP command port (default 5000) and a UDP media port (default 5001). At **1x** the host **stream-copies** the source's video/audio ES into TS and sends it over UDP to a local relay; a thin distributor forwards the raw TS datagrams to every registered client. Only non-1x speeds re-encode, using x264 `tune=zerolatency`
- **Join**: enter the host's command address and the **host's UDP media address** (e.g. `192.168.1.10:5001`). The client sends a small UDP registration packet; the host replies with the raw TS datagrams, which the client re-emits to a local VLC. No client inbound port is needed
- **Low latency**: the default client buffer is **400 ms** (200–5000 ms in settings). At 1x there is no encoder in the path, so loopback/LAN delay is essentially the client cache plus the network
- **Resilient playback**: the client VLC owns jitter, reordering and buffering; a watchdog restarts the local picture if the displayed-frame count stops advancing; a rotating-dot buffering overlay appears whenever the play button says "playing" and the picture is not advancing
- State sync: file, position, duration, pause, speed; clients can remotely play / pause / seek / change speed, while volume and mute stay local
- Room size and member list sync in real time, up to 32 people (configurable)

### Room password
- The host can enable a password when starting (on by default); the password stays on the host only
- When the room is protected, the client is prompted for the password, which is **verified by the host** before the client actually joins
- Constant-time comparison, at most 5 attempts per connection, and the password is never sent to other clients

### Room members
- Every end — host or guest — can set a display name in **Settings → Chat room** (defaults to the computer name, 4–32 characters)
- After joining, a **Room members** button appears on the main window
- The list puts the host first and then guests in join order; it is scrollable (up to 32 people), and the host can remove a guest with the **Kick** button

### Room chat
- After joining, a chat panel appears on the right of the main window (WeChat-style message list with an input box)
- The host stores all messages; guests send over the command port and only display a message after the host relays it back to everyone (including the sender)
- New guests receive the full history; the host keeps the latest 200 messages by default (configurable in **Settings → Chat room**)
- The host can enforce a per-member send interval (unlimited / 1 / 2 / 3 / 5 s); rejected messages show a gray "sending too fast" watermark inside the input box

### Danmaku
- New chat messages float over the video as danmaku, right to left — works in windowed and fullscreen mode
- Each danmaku picks a random lane; when it gets crowded messages queue up and the whole field speeds up automatically, so lanes never overlap
- Size scales with the video area; the layer is drawn inside the player window, so it moves with the window, follows fullscreen, and is covered normally by other windows; the master switch, size, speed, color and area live in **Settings → Chat room**

### Seeking / speed changes while streaming
- Client seeks are never handled locally; the command is sent to the host
- On a seek or speed change the host: **stops the current stream → rebuilds it from the new position / speed using `:start-time` / `:rate` → broadcasts `resync` → clients reconnect**
- At 1x the rebuilt stream is still a **stream copy**; only non-1x rates re-encode
- The stream timeline restarts from 0; clients are back within about a second (or one source keyframe)

## Requirements

- **Windows 10 / 11 (64-bit)** — the dark title bar and rounded window corners use Win32 APIs
- **Python 3.10+** (developed and tested on Python 3.15, Tk 9.0)
- **VLC 3.x desktop (64-bit)** with `libvlc.dll` (already bundled in the portable release)
- Python packages:

```bash
pip install python-vlc tkinterdnd2
```

> If the app cannot find VLC, install 64-bit VLC or set the `VLC_HOME` environment variable to the VLC installation folder that contains `libvlc.dll`.

## Installation & Running

```bash
git clone https://github.com/Nobri6039/Nobplaypot

cd Nobplaypot

pip install python-vlc tkinterdnd2

python Nobplaypot.py
```

You can also double-click **`Nobplaypot.pyw`** to start without a console window.

### Portable build (no Python install)

The portable release bundles a **minimal Python 3.15 runtime** (Tkinter, `python-vlc`, `tkinterdnd2`) in `python-runtime/` — about 34 MB, not a full Python installation — plus a **minimal VLC 3.0.24 runtime** in `vlc-runtime/` (about 53 MB). Start it by double-clicking **`启动 Nobplaypot.bat`** or running:

```bat
python-runtime\pythonw.exe Nobplaypot.pyw
```

No Python or VLC installation is required for the portable release. The bundled VLC runtime remains under GPLv2+; see `vlc-runtime/COPYING.txt` and videolan.org for its source.

## Usage

### Keyboard shortcuts
| Shortcut | Action |
| --- | --- |
| `Space` | Play / pause |
| `←` / `→` | Seek back / forward (step configurable, 5 s by default) |
| `↑` / `↓` | Volume +5 / −5 |
| `F` | Enter / exit fullscreen |
| `Esc` | Exit fullscreen |
| `Ctrl+O` | Open a file |
| `Ctrl+L` | Open the log folder |

### Host a streaming session
1. Open the video you want to watch;
2. Click the stream button at the bottom-left → **Host stream**;
3. Enter the command port (TCP, default 5000) and the media port (UDP, default 5001);
4. "Enable room password" is on by default — enter a password (or turn it off for an open room);
5. Click **Start hosting**.

### Join a session
1. Click the stream button → **Join stream**;
2. Command address: the host's IP:port (e.g. `192.168.1.10:5000`);
3. Stream address: the **host's UDP media address** (e.g. `192.168.1.10:5001`); the client sends a UDP registration packet and receives the raw TS, so no local listen port is needed;
4. If the room has a password, a password dialog appears; enter the correct password to join.

> LAN is the easiest setup. For the internet, map the host's **TCP** command port and the host's **UDP** media port on the host's router / firewall; clients only send outbound traffic. If the public mapped port differs from the local one, fill the public address on the client.

### Settings
Open settings with the gear button at the bottom-right. There are four pages:

- **General**: decode mode (auto / hardware / software, restart required), default volume, seek step, autoplay on open, remember last folder, log level
- **Appearance**: accent color, background tone, hover animations (colors apply after restart)
- **Chat room**: username, member message rate limit, chat history limit, danmaku on/off, size, speed, color, area
- **Streaming**: max room size, per-client bandwidth / resolution / frame rate / audio bitrate / x264 preset / keyframe interval (these only take effect when re-encoding, i.e. **non-1x speed**), client buffer (default **400 ms**, 200–5000 ms), buffering-icon scale (0.5x–2.5x), heartbeat interval, offline timeout, default ports

Settings are stored in `%APPDATA%\Nobplaypot\config.json`. At **1x** the stream is a copy, so resolution/bitrate/frame rate do not change the picture; client buffer and buffering-icon scale apply immediately. Seeking, speed changes and membership changes rebuild the stream (the current position is preserved).

## Data & Logs

| Item | Location |
| --- | --- |
| Config | `%APPDATA%\Nobplaypot\config.json` |
| Logs | `%APPDATA%\Nobplaypot\logs\nobplaypot.log` (2 MB rotation, 3 files) |

Log-related environment variables:

- `NOBPLAYPOT_LOG_LEVEL=DEBUG`: enable debug logging
- `NOBPLAYPOT_LOG_FILE=<path>`: write logs to a specific file

## Implementation notes

- **Command channel**: TCP + newline-delimited JSON; heartbeat every 2 seconds, offline after 12 seconds without messages; the initial connect runs on a background thread so the UI never freezes on DNS/connect
- **Stream channel**: at 1x the host's libvlc `sout` **copies** the source's video/audio ES into TS over UDP to a local relay; a thin Python distributor forwards the raw TS datagrams to registered clients; each client re-emits them to a local VLC for rendering. No custom framing, sequence numbers, FEC or NACK — the client VLC handles jitter, reordering and buffering. Only rate ≠ 1x builds the x264 `tune=zerolatency` transcode chain
- **Seek / speed / membership**: libvlc's sout destination list is fixed at media creation, so a seek or a roster change rebuilds the media; the rebuild re-applies the current position with `:start-time` and `set_time` (and the rate with `:rate`), then broadcasts `resync`; the stream timeline restarts from 0 for clients
- **Buffering indicator**: the client polls `libvlc_media_get_stats().displayed_pictures` every 250 ms. If the play/pause button says "playing" and the displayed-frame count has not advanced for 450 ms (or is still 0), a transparent rotating-dot overlay is shown; it scales with the video area and the user's buffering-icon scale setting
- **Password**: verified by the host before a client is registered (`hmac.compare_digest`); unauthenticated connections never enter the room, count toward the room size, or receive the stream
- **Stability**: libvlc can block for a long time when stopping input or releasing a player, so those calls run on background threads and never on the UI thread; a client-side watchdog restarts the local decoder if the picture stalls

## Known limitations

- At 1x the picture is the source's own codec/resolution; a source with a very long keyframe interval starts and seeks slower because the client must wait for a keyframe
- Seeking, speed changes and membership changes briefly reconnect clients (the sout must be rebuilt); the position is preserved
- CPU is used only for non-1x speed; 4K/high-bitrate sources are limited by the client's decoder and the network
- One host = one room; designed mainly for LAN use
- No playlist, subtitles, or aspect-ratio switching yet

## Project structure

```text
Nobplaypot/
├── Nobplaypot.py      # main program (single file)
├── Nobplaypot.pyw     # console-less launcher
├── screenshots/       # README screenshots
├── README.md          # English (default)
├── README.zh-CN.md    # Chinese
├── .gitignore
└── LICENSE
```

## Roadmap

1. Expand to more system platforms
2. Smoother sync and more feature extensions

## Acknowledgements

- [VLC media player](https://www.videolan.org/) / libvlc
- [python-vlc](https://github.com/oaubert/python-vlc)
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2)

## Author & Copyright

- **Created by DeepSeek V4.1 Flash (AI assistant)** — the code, documentation and screenshots of this project were produced by the AI.
- **Published and maintained by Nobri6039.**
- Copyright © 2026 Nobri6039; released under the [MIT License](LICENSE).
- When redistributing, please credit "Created by DeepSeek V4.1 Flash" and keep this notice.

> The bundled VLC runtime remains under its own GPLv2+ license.

## License

[MIT](LICENSE)
