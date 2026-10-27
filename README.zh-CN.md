# phone-control-mcp（中文文档）

[![CI](https://github.com/wxh1783921374-xiaofeng/phone-control-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/wxh1783921374-xiaofeng/phone-control-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**让任何 AI agent 控制你的安卓手机。** 一套代码，两种接口：

- **MCP 服务器**（`server.py`，stdio）——Codex、Claude Desktop、Cursor 等 MCP 客户端直接挂载，获得 **28 个手机控制工具**，截图以真实图片返回给模型"看"。
- **零依赖 CLI**（`phone_control.py`）——任何能执行 shell 命令的 agent（如 DeepSeek Harness）都能用；人工操作、定时任务也方便。

基于 ADB。核心只用 Python 标准库；仅可选的 MCP 层需要 `pip install mcp`。

[English README →](README.md)

## 为什么快

AI 控制手机慢，慢在每个动作都是一次往返。本项目在两个层面消灭延迟：

**传输层**（实测：vivo V2408A · Android 16 · Windows 11 · USB）：

| 操作 | v1.0 | v1.1 | 提升 |
|---|---:|---:|---:|
| shell 命令 | 63 ms | **3 ms** | **21×** |
| tap 点击 | 118 ms | **19 ms** | **6×** |
| 前台应用检测 | 141 ms | **37 ms** | **4×** |
| device_info（10+ 项探测） | 1060 ms | **255 ms** | **4×** |
| ui_dump / find_text | ~2600 ms | ~2210 ms | 1.2×† |
| 截图（1080×2376 PNG） | ~950 ms | ~950 ms | — |

† 剩余 ~2s 是设备端 `uiautomator` 的 Java 启动成本（安卓固有）；本项目自身开销已降到 ~200ms。

- **持久 shell 会话**：一条长连接 `adb shell` + 标记协议，取代每条命令的 adb.exe 进程创建 + 握手 + 设备端 sh 启动。自动重连；可疑命令（多行、引号不配对、会读 stdin 的命令用 `</dev/null` 隔离）自动回退单次模式，功能永不因优化中断。
- **UI 树单次往返**：`dump→cat→rm` 合并为一条 `exec-out` 命令，无临时文件、无 `adb pull`。
- **合并探测**：`device_info` 一个脚本收齐全部信息。
- **保活**：MCP 服务器每 45s 向设备发一条无害命令，防 Windows USB 选择性暂停导致掉线。

**Agent 层**（更大的收益——省一次工具调用就省一次 LLM 往返）：

- `tap_text "发送"`——按屏幕文字定位控件**并**点击，一次调用完成。
- `wait_for_text`——轮询等待界面元素出现，替代盲目 `sleep`。
- `run_script`——整套流程（打开应用→等待→点击→输入→截图）压缩为**一次**工具调用。

## 安装

要求：Python 3.10+，已开启 USB 调试的安卓手机。

**Windows（一条命令）：**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

自动创建 `.venv`、安装 MCP SDK、下载 Google platform-tools（adb）。

**Linux / macOS / 手动：**

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp>=1.2"   # 仅 MCP 服务器需要
# adb 用系统包管理器安装，或设 ADB_PATH=/path/to/adb
```

**手机准备（一次性）：** 设置 → 关于手机 → 连点「版本号」7 次 → 开发者选项 → 打开 **USB 调试** → 插线 → USB 用途选「**传输文件**」→ 授权弹窗勾选「**一律允许**」。

验证：`python phone_control.py doctor`——每种故障都会打印对应修复建议。

## 接入你的 AI 工具

### Codex CLI

```bash
codex mcp add phone-control -- <项目路径>/.venv/bin/python <项目路径>/server.py
```

### Claude Desktop / Cursor / 任意 MCP 客户端

```json
{
  "mcpServers": {
    "phone-control": {
      "command": "<项目路径>/.venv/bin/python",
      "args": ["<项目路径>/server.py"]
    }
  }
}
```

### DeepSeek Harness（DSH）

- **Skill 方式**：把 `skills/phone-control/` 复制到 `~/.dsh/skills/`（或项目内 `.dsh/skills/`），会话技能目录**热加载**即生效；把 `SKILL.md` 里的入口路径改成你的实际路径。
- **CLI 方式**：agent 直接 shell 调用 `python phone_control.py <子命令>`，结果为 UTF-8 JSON。

### 纯 shell / 定时任务 / 手动

```bash
python phone_control.py screenshot now.png
python phone_control.py tap-text "微信"
```

## 28 个 MCP 工具

| 分组 | 工具 |
|---|---|
| 诊断 | `doctor` `list_devices` `device_info` `current_app` |
| 看屏幕 | `screenshot`（返回**图片**）`ui_dump` `find_text` |
| 触摸与输入 | `tap` `long_press` `swipe` `swipe_screen` `press_key` `screen_on` `input_text` |
| 组合技（agent 提速关键） | `tap_text` `wait_for_text` `run_script` |
| 应用 | `open_app` `stop_app` `restart_app` `list_apps` `install_app` `uninstall_app` |
| 文件 | `push_file` `pull_file` |
| 逃生舱 | `device_shell` |
| 无线调试 | `wireless_pair` `wireless_connect` |

CLI 与工具一一对应（`tap-text`、`wait-for`、`script`……），详见 `python phone_control.py --help`。

### run_script 示例

```json
[
  {"action": "open", "package": "com.tencent.mm"},
  {"action": "wait_for", "text": "微信", "timeout_s": 15},
  {"action": "tap_text", "text": "通讯录"},
  {"action": "screenshot", "save_path": "contacts.png"}
]
```

可用 action：`tap` `tap_text` `long_press` `swipe` `swipe_screen` `key` `text` `sleep` `wait_for` `open` `stop` `current_app` `assert_app` `screenshot` `find` `shell`。每步返回 `{step, action, ok, result|error}`；默认遇错即停（`stop_on_error=false` 可继续）。

## 常用配方

**中文输入**——原生 `adb shell input text` 只支持 ASCII。安装一次 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard)（`python phone_control.py install ADBKeyBoard.apk`），之后 `input_text` 检测到中文会自动：切换输入法 → base64 广播输入 → 恢复原输入法。

**无线调试（免数据线）**——开发者选项 → 无线调试 → 「使用配对码配对设备」：

```bash
python phone_control.py pair 192.168.1.20:37123 123456
python phone_control.py connect 192.168.1.20:40001
```

**插线时屏幕常亮**（避免锁屏中断自动化）：

```bash
python phone_control.py shell "svc power stayon true"
```

**多台手机**——所有命令加 `--serial <序列号>`（CLI）/ `serial` 参数（MCP），或设环境变量 `PHONE_SERIAL`。

## 故障排查

| 现象 | 解决 |
|---|---|
| 找不到 adb | 运行 `install.ps1`；或设 `ADB_PATH`；或把 adb 加入 PATH |
| 设备 `unauthorized` | 点亮手机，在授权弹窗勾选「一律允许」 |
| 设备 `offline` | 重插数据线；`adb kill-server` 后重试 |
| 完全检测不到设备 | 换支持数据传输的线；USB 用途选「传输文件」；装 OEM 驱动（小米/华为助手） |
| 空闲几分钟后掉线 | Windows USB 选择性暂停——用 MCP 服务器（自带保活）或无线调试；保持 `stayon true` |
| 安全锁屏界面 | adb 无法（也不应）绕过人脸/密码锁——解锁一次，配合 `stayon true` 即可持续可用 |
| 截图黑屏 | 银行/Netflix 等 DRM 界面系统级禁止截屏 |
| 小米安装失败 | 开发者选项关闭「MIUI 优化」或允许「USB 安装」 |

## 开发

```bash
python test_mcp.py       # MCP 端到端冒烟测试（无需设备，CI 同款）
python benchmark.py      # 延迟基准测试（需设备）
python regression.py     # 40 项真机集成回归（需设备）
```

项目结构——刻意保持只有两个模块：

```
phone_control.py   # 核心库 + CLI，纯标准库
server.py          # MCP 服务器（薄封装，兼容 mcp SDK v1/v2）
install.ps1        # Windows 一键安装（venv + mcp + platform-tools）
skills/            # DeepSeek Harness skill 包
```

## 安全

- 破坏性操作（`uninstall_app`、覆盖式 `push_file`、任意 `device_shell`）在工具描述中明确要求 agent 先向用户确认。
- 内置 skill 指示 agent：不主动操作支付/银行界面，绝不读取或外传验证码与截图。
- 一切都在本地 ADB 链路上运行；无遥测、无云服务。

## 许可证

MIT © phone-control-mcp contributors
