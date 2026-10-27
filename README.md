# phone-control-mcp

[![CI](https://github.com/wxh1783921374-xiaofeng/phone-control-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/wxh1783921374-xiaofeng/phone-control-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**Let any AI agent drive your Android phone.** One codebase, two interfaces:

- **MCP server** (`server.py`, stdio) — plug into Codex, Claude Desktop, Cursor, or any MCP client and get **28 phone-control tools**, including screenshots returned as real images the model can see.
- **Zero-dependency CLI** (`phone_control.py`) — any agent that can run a shell command can control the phone; also great for humans and cron jobs.

Built on ADB. Pure Python standard library for the core; only the optional MCP layer needs `pip install mcp`.

[简体中文文档 →](README.zh-CN.md)

## Why it's fast

AI phone-control feels slow when every action is a round trip. This project attacks latency on both layers:

**Transport layer** (measured on vivo V2408A · Android 16 · Windows 11 · USB):

| Operation | v1.0 | v1.1 | Speedup |
|---|---:|---:|---:|
| shell command | 63 ms | **3 ms** | **21×** |
| tap | 118 ms | **19 ms** | **6×** |
| foreground-app check | 141 ms | **37 ms** | **4×** |
| device_info (10+ probes) | 1060 ms | **255 ms** | **4×** |
| ui_dump / find_text | ~2600 ms | ~2210 ms | 1.2×† |
| screenshot (1080×2376 PNG) | ~950 ms | ~950 ms | — |

† The remaining ~2 s is the on-device `uiautomator` Java startup — a fixed Android cost; our own overhead is now ~200 ms.

- **Persistent shell session** — one long-lived `adb shell` with a marker protocol instead of spawning adb.exe + handshake + device sh per command. Auto-reconnects, auto-falls back to one-shot mode on anything suspicious (multi-line commands, unbalanced quotes, stdin-reading commands are isolated with `</dev/null`).
- **Single-roundtrip UI fetch** — `dump → cat → rm` merged into one `exec-out` call, no temp files, no `adb pull`.
- **Merged probes** — `device_info` collects everything in one shell script.
- **Keep-alive** — the MCP server pings the device every 45 s so Windows USB selective-suspend doesn't drop it.

**Agent layer** (the bigger win — every saved tool call is a saved LLM round trip):

- `tap_text "Send"` — locate a control by on-screen text **and** tap it in one call.
- `wait_for_text` — poll until a screen element appears instead of blind `sleep`.
- `run_script` — run an entire multi-step flow (open app → wait → tap → type → screenshot) as **one** tool call.

## Install

Requirements: Python 3.10+, an Android phone with USB debugging enabled.

**Windows (one command):**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Creates `.venv`, installs the MCP SDK, and auto-downloads Google platform-tools (adb) if missing.

**Linux / macOS / manual:**

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp>=1.2"   # only needed for the MCP server
# adb: use your package manager, or set ADB_PATH=/path/to/adb
```

**Phone setup (once):** Settings → About phone → tap Build number 7 times → Developer options → enable **USB debugging** → plug in → choose **File transfer** → accept the RSA prompt (tick *Always allow*).

Verify: `python phone_control.py doctor` — fix hints are printed for every failure mode.

## Connect your AI tool

### Codex CLI

```bash
codex mcp add phone-control -- /path/to/.venv/bin/python /path/to/server.py
```

### Claude Desktop / Cursor / any MCP client

```json
{
  "mcpServers": {
    "phone-control": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

### DeepSeek Harness (DSH)

- **Skill**: copy `skills/phone-control/` into `~/.dsh/skills/` (or `<project>/.dsh/skills/`) — hot-reloads into the session skill catalog; edit the entry-point path inside `SKILL.md` to your checkout.
- **Or plain CLI**: agents call `python phone_control.py <subcommand>` through the shell; results are UTF-8 JSON.

### Plain shell / cron / you

```bash
python phone_control.py screenshot now.png
python phone_control.py tap-text "微信"
```

## The 28 MCP tools

| Group | Tools |
|---|---|
| Diagnostics | `doctor` `list_devices` `device_info` `current_app` |
| See the screen | `screenshot` (returns an **image**), `ui_dump`, `find_text` |
| Touch & type | `tap` `long_press` `swipe` `swipe_screen` `press_key` `screen_on` `input_text` |
| Combos (fast agent flows) | `tap_text` `wait_for_text` `run_script` |
| Apps | `open_app` `stop_app` `restart_app` `list_apps` `install_app` `uninstall_app` |
| Files | `push_file` `pull_file` |
| Escape hatches | `device_shell` |
| Wireless ADB | `wireless_pair` `wireless_connect` |

The CLI mirrors every tool (`tap-text`, `wait-for`, `script`, …) — see `python phone_control.py --help`.

### run_script example

```json
[
  {"action": "open", "package": "com.tencent.mm"},
  {"action": "wait_for", "text": "微信", "timeout_s": 15},
  {"action": "tap_text", "text": "通讯录"},
  {"action": "screenshot", "save_path": "contacts.png"}
]
```

Actions: `tap` `tap_text` `long_press` `swipe` `swipe_screen` `key` `text` `sleep` `wait_for` `open` `stop` `current_app` `assert_app` `screenshot` `find` `shell`. Each step reports `{step, action, ok, result|error}`; stops on first error unless `stop_on_error=false`.

## Recipes

**Chinese / CJK text input** — stock `adb shell input text` is ASCII-only. Install [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) once (`python phone_control.py install ADBKeyBoard.apk`); afterwards `input_text` auto-switches IME, types via base64 broadcast, and restores your IME.

**Wi-Fi (no cable)** — Developer options → Wireless debugging → *Pair device with pairing code*:

```bash
python phone_control.py pair 192.168.1.20:37123 123456
python phone_control.py connect 192.168.1.20:40001
```

**Keep screen awake while plugged** (avoids lock-screen interruptions):

```bash
python phone_control.py shell "svc power stayon true"
```

**Multiple phones** — pass `--serial <SN>` (CLI) / `serial` (MCP tools), or set `PHONE_SERIAL`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `找不到 adb` / adb not found | run `install.ps1`, or set `ADB_PATH`, or add adb to PATH |
| device `unauthorized` | unlock the phone, accept the RSA dialog (*Always allow*) |
| device `offline` | replug the cable; `adb kill-server` then retry |
| no device at all | data-capable cable; USB mode = File transfer; OEM drivers (Xiaomi/Huawei suites) |
| drops after minutes of idle | USB selective suspend — use the MCP server (keep-alive) or wireless debugging; keep `stayon true` |
| secure lock screen | adb cannot (and should not) bypass face/PIN locks — unlock once; with `stayon true` it stays usable |
| black screenshots | DRM surfaces (banking apps, Netflix) block capture by design |
| Xiaomi install failures | disable MIUI optimization / allow "Install via USB" |

## Development

```bash
python test_mcp.py       # end-to-end MCP smoke test (no device needed) — also runs in CI
python benchmark.py      # latency benchmark (device required)
python regression.py     # 40-check integration suite (device required)
```

Project layout — deliberately just two modules:

```
phone_control.py   # core library + CLI, stdlib only
server.py          # MCP server (thin wrapper, mcp SDK v1 & v2 compatible)
install.ps1        # Windows one-shot installer (venv + mcp + platform-tools)
skills/            # DeepSeek Harness skill bundle
```

## Safety

- Destructive operations (`uninstall_app`, overwriting `push_file`, arbitrary `device_shell`) are documented as confirm-first for agents.
- The bundled skill instructs agents to never drive payment/banking screens unprompted and never exfiltrate verification codes or screenshots.
- Everything runs locally over ADB; no telemetry, no cloud.

## License

MIT © phone-control-mcp contributors
