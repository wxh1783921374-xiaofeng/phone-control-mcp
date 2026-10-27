---
name: phone-control
description: Control an Android phone via ADB — screenshot the screen, tap/swipe by coordinates or on-screen text, type text, manage apps, transfer files. 通过 ADB 控制安卓手机：截屏看屏幕、按文字或坐标点击滑动、输入文字、管理应用、传输文件。Use when the user asks to look at, operate, or automate anything on their Android phone.
whenToUse: The user asks to check/operate their phone (看手机屏幕、打开手机应用、手机自动化、传文件到手机), or an Android device needs to be driven from the agent.
---

# Phone Control (Android via ADB)

Open-source repo: https://github.com/wxh1783921374-xiaofeng/phone-control-mcp

Drive a real Android phone with the `phone-control` CLI (zero third-party dependencies).

## Entry point

```
python <repo-root>/phone_control.py <subcommand>
```

If the project is pip-installed, the same CLI is available as `phone-control <subcommand>`.
All structured results are JSON on stdout (UTF-8). Add `--serial <SN>` to any command when multiple devices are attached (or set `PHONE_SERIAL`).

## Standard workflow

1. `doctor` — self-check adb / device / authorization. On any `[X]`, follow the printed fix hints before anything else.
2. `screenshot out.png` — capture the screen. If your model has vision, read the image; otherwise rely on steps 3–4 to "see".
3. Prefer `tap-text "按钮文字"` — locates the control by on-screen text AND taps it in one call (single UI fetch). Add `--wait 10` for slow-loading screens.
4. Multi-step flows: put everything into ONE `script` call (JSON array) instead of many separate commands:

```
python phone_control.py script "[{\"action\":\"open\",\"package\":\"com.tencent.mm\"},{\"action\":\"wait_for\",\"text\":\"微信\",\"timeout_s\":15},{\"action\":\"screenshot\"}]"
```

5. After each batch, verify with `screenshot`, `current-app`, or `wait-for`.

## Command reference

| Command | Purpose |
|---|---|
| `doctor` / `devices` / `info` / `current-app` | diagnostics, device list, full device info, foreground app |
| `screenshot [out.png]` | screen capture (PNG; resolution = screen resolution) |
| `find <text> [--exact]` | locate controls by text → center coordinates (no tap) |
| `tap-text <text> [--index N] [--exact] [--wait S]` | find + tap in one call (preferred) |
| `wait-for <text> [--timeout S]` | poll until text appears (replaces blind sleeps) |
| `tap x y` / `long-press x y [--ms]` / `swipe x1 y1 x2 y2 [--ms]` | raw touch input |
| `swipe-screen up\|down\|left\|right` | full-screen scroll/page |
| `key back\|home\|recent\|power\|volume_up\|volume_down\|enter\|...` | key events |
| `screen-on` | wake screen (cannot pass a secure lock — ask the user to unlock) |
| `text "..."` | type into focused field (focus it with tap first; CJK needs ADBKeyboard apk installed on device) |
| `open/stop/restart <package>` | app lifecycle (`apps --contains kw` to find package names) |
| `apps [--contains kw] [--system]` | list installed packages |
| `install x.apk` / `uninstall <pkg>` | install / uninstall (confirm with user first) |
| `push <local> [remote]` / `pull <remote> [local]` | file transfer (default dir /sdcard/Download/) |
| `ui-dump [out.xml]` | full UI hierarchy XML |
| `script <file.json \| json \| ->` | run an action sequence in one call |
| `connect ip:port` / `pair ip:port code` | wireless debugging (Wi-Fi ADB) |
| `shell "cmd"` | arbitrary adb shell |

`script` actions: `tap{x,y}` `tap_text{text,index?,exact?,wait_timeout?}` `long_press{x,y,ms?}` `swipe{x1,y1,x2,y2,ms?}` `swipe_screen{direction,ms?}` `key{key}` `text{text}` `sleep{ms}` `wait_for{text,timeout_s?,exact?}` `open{package}` `stop{package}` `current_app{}` `assert_app{package}` `screenshot{save_path?}` `find{text,exact?}` `shell{command,timeout?}`. Each step returns `{step, action, ok, result|error}`; stops on first error unless `--continue-on-error`.

## Performance notes

- A persistent shell session and single-roundtrip UI fetch are built in; consecutive operations in ONE process (or via `script`) are dramatically faster than repeated CLI calls.
- For long sessions prefer the MCP server (`server.py`, stdio) or keep one `script` per flow: process startup is the main CLI overhead.
- `wait_for` / `wait-for` instead of fixed `sleep` — faster and more reliable.

## Troubleshooting

- `no devices`: USB debugging off / cable charge-only / not authorized → run `doctor` and follow hints; wireless: `pair` then `connect`.
- Device drops after idle: enable keep-alive (MCP server does this automatically) or use wireless debugging; on the phone keep "USB debugging" on and screen awake while plugged (`shell "svc power stayon true"`).
- Secure lock screen cannot be unlocked by adb — wake with `screen-on`, then ask the user to unlock.
- CJK input requires ADBKeyboard.apk on the device (github.com/senzhk/ADBKeyBoard); install once via `install`, then `text` handles it automatically.

## Safety rules

- Confirm with the user before: `uninstall`, destructive `shell` writes, overwriting files via `push`, sending messages in social apps.
- Never drive payment / banking / password screens unless the user explicitly asks, and never read or exfiltrate verification codes.
- Screenshots may contain private data — keep them local, do not upload.
