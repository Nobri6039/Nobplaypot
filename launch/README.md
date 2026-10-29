# Nobplaypot 启动器（C++ / VS2019）

这是 Nobplaypot 的原生入口。它只做四件事：

1. 读取 `Bin\launch.json`，决定用哪个 runtime、跑哪个脚本、工作目录、参数、环境变量；
2. 校验 `Bin\payload.manifest` 的 RSA 签名，再逐个校验清单里文件的 SHA-256；
3. 用 `CreateProcessW` **直接启动 pythonw**（不经过 cmd/bat，没有黑窗口）；
4. 出错时写 `Bin\launch.log` 并弹出错误对话框。

更新 py / python-runtime / vlc-runtime 后，只需要重新运行 `tools\sign.ps1`，**exe 不用换**，快捷方式、文件关联、注册表也不用动。

## 目录结构

```
Nobplaypot.exe              ← VS2019 编译出来的入口（Release x64）
Nobplaypot.cpp/.rc/.vcxproj
app.manifest
app.ico
pubkey.h                    ← sign.ps1 生成，编译进 exe 的公钥
tools/
  sign.ps1                  ← 生成公钥、清单、签名
  private-key.xml           ← 私钥，绝对不能外发/提交
Bin/
  launch.json               ← 启动配置（唯一需要改的启动相关文件）
  payload.manifest          ← sign.ps1 生成的 SHA-256 清单
  payload.sig               ← sign.ps1 生成的签名
  launch.log                ← 启动日志
  Nobplaypot.pyw
  Nobplaypot.py
python-runtime/             ← 便携 Python（pythonw.exe 等）
vlc-runtime/                ← 便携 VLC（libvlc.dll 等）
```

## 构建

1. 用 VS2019 打开 `Nobplaypot.sln`，配置选 **Release / x64**，生成；
2. 首次构建前先运行一次 `tools\sign.ps1`（会生成 `pubkey.h`，否则编译会找不到头文件）；
3. 生成的 `x64\Release\Nobplaypot.exe` 就是入口。

如果换了私钥，必须重新编译 exe（公钥写在 exe 里）。

## 首次部署 / 更新

首次：

```powershell
# 1. 把 python-runtime、vlc-runtime、Bin\Nobplaypot.pyw 放到位
# 2. 生成清单和签名
powershell -ExecutionPolicy Bypass -File tools\sign.ps1
# 3. 编译（或使用已编译好的 exe），双击 Nobplaypot.exe
```

以后更新（只换了 py / runtime / vlc / launch.json）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\sign.ps1
```

然后替换对应文件即可，exe、快捷方式、文件关联全部保持不变。

## launch.json 字段

```json
{
  "schema": 1,
  "runtime": "python-runtime\\pythonw.exe",
  "script": "Bin\\Nobplaypot.pyw",
  "args": [],
  "cwd": ".",
  "inherit_args": true,
  "env": { "VLC_HOME": "vlc-runtime" },
  "log": "Bin\\launch.log"
}
```

| 字段 | 说明 |
| --- | --- |
| `runtime` | 相对程序根目录（exe 所在目录）的 runtime 路径，也可以在根目录内写绝对路径 |
| `script` | 要交给 pythonw 的脚本，必须在程序根目录内 |
| `args` | 额外传给脚本的参数数组 |
| `cwd` | 工作目录，默认程序根目录；App 的相对路径、`VLC_HOME` 都基于它 |
| `inherit_args` | 是否把 exe 收到的参数（例如“打开方式”传入的文件路径）继续传给脚本 |
| `env` | 追加/覆盖的环境变量 |
| `log` | 预留字段，当前启动日志固定写 `Bin\launch.log` |

所有路径都必须落在程序根目录内，越界会被拒绝。

## 安全模型

- `payload.manifest` 由 `tools\private-key.xml` 对应的私钥签名，exe 内置公钥校验；
- 清单里每个文件都校验 SHA-256，任何文件被替换/修改都会拒绝启动；
- `launch.json` 只是**数据**，exe 不解析、不执行任何 shell 命令，不存在 bat 那种命令注入面；
- 私钥只留在你的开发机上；泄露私钥等于失去这套校验的意义。

## 常见问题

- **弹窗“文件校验失败”**：payload 被改过但没重新签名，运行 `tools\sign.ps1`；
- **弹窗“缺少 payload.manifest / payload.sig”**：同上；
- **双击没反应**：看 `Bin\launch.log`；错误都会写进去并弹窗；
- **黑窗口**：exe 是 Windows 子系统，pythonw 也是无控制台进程，正常不会有黑窗口；如果出现，说明改成调用 python.exe 或 cmd 了。
