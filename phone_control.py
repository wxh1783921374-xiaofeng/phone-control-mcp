# -*- coding: utf-8 -*-
"""
phone_control.py —— Android 手机控制核心库 + 命令行工具（零第三方依赖，仅用 Python 标准库）

设计目标：
  1. 作为库被 server.py（MCP 服务器）调用，供 Codex / Claude Desktop / Cursor 等挂载；
  2. 作为 CLI 被任何 agent（包括 DeepSeek Harness）通过 shell 直接调用；
  3. 自动发现 adb（PATH、项目内 adb/、Android SDK 常见位置、ADB_PATH 环境变量）。

CLI 用法示例：
  python phone_control.py doctor
  python phone_control.py devices
  python phone_control.py info
  python phone_control.py screenshot shot.png
  python phone_control.py tap 500 800
  python phone_control.py tap-text 发送            # 按文字定位并点击（一步完成）
  python phone_control.py wait-for 聊天 --timeout 10
  python phone_control.py swipe 500 1500 500 500
  python phone_control.py key back
  python phone_control.py text "你好，世界"
  python phone_control.py open com.tencent.mm
  python phone_control.py apps
  python phone_control.py find 微信
  python phone_control.py script flow.json         # 批量动作（一次进程完成整套流程）
  python phone_control.py push local.txt /sdcard/Download/
  python phone_control.py pull /sdcard/DCIM/a.jpg ./
  python phone_control.py pair 192.168.1.20:37123 123456   # 无线调试配对
  python phone_control.py connect 192.168.1.20:40001       # 无线调试连接
  python phone_control.py shell "ls /sdcard"
"""

from __future__ import annotations

import atexit
import base64
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# ADB 发现与底层调用
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
_CREATE_NO_WINDOW = 0x08000000  # Windows: 不弹出控制台窗口


def find_adb() -> str | None:
    """按优先级查找 adb 可执行文件。"""
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates: list[str] = []

    env = os.environ.get("ADB_PATH")
    if env:
        candidates.append(env)

    which = shutil.which("adb")
    if which:
        candidates.append(which)

    # 项目内置（install.ps1 自动下载的 platform-tools）
    candidates.append(str(PROJECT_DIR / "adb" / "platform-tools" / exe))

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / exe))
    home = Path.home()
    candidates += [
        str(home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / exe),
        str(home / "scoop" / "shims" / exe),
        r"C:\platform-tools" + "\\" + exe,
        r"C:\ProgramData\chocolatey\bin" + "\\" + exe,
        "/usr/local/bin/adb",
        "/opt/homebrew/bin/adb",
    ]

    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        p = Path(c)
        if p.is_file():
            return str(p)
    return None


class AdbError(RuntimeError):
    """ADB 调用失败时抛出，消息面向 agent/用户，包含修复建议。"""


# ---------------------------------------------------------------------------
# 持久 shell 会话（性能优化核心）
# 每条独立 adb 命令都要付出：Windows 进程创建(~30-60ms) + adb 客户端握手(~10-30ms)
# + 设备端 sh 启动(~20-50ms)。持久会话把这三项摊销为一次，命令只付设备端执行成本。
# 会话异常时自动重连；重连失败自动回退单次模式，功能永不因优化而中断。
# 设环境变量 PHONE_CONTROL_NO_PERSIST=1 可强制关闭。
# ---------------------------------------------------------------------------

class _PersistUnavailable(Exception):
    """持久会话不可用，调用方应回退到单次 adb 模式。"""


def _safe_for_session(cmd: str) -> bool:
    """持久会话安全检查：未配对的引号/圆括号/结尾反斜杠可能吞掉标记行导致会话挂起，
    此类命令一律回退单次模式执行。"""
    if cmd.rstrip().endswith("\\"):
        return False
    in_s = in_d = False
    depth = 0
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and not in_s:
            i += 2
            continue
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    return False
        i += 1
    return not in_s and not in_d and depth == 0


class PersistentShell:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._queue: "queue.Queue[str | None] | None" = None
        self._serial: str | None = None
        self._nonce = itertools.count(1)
        self._disabled = os.environ.get("PHONE_CONTROL_NO_PERSIST") == "1"

    def _spawn(self, serial: str | None) -> None:
        adb = find_adb()
        if not adb:
            raise _PersistUnavailable()
        cmd = [adb] + (["-s", serial] if serial else []) + ["shell"]
        kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,  # 并入 stdout，随会话文本统一返回
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError:
            raise _PersistUnavailable()

        q: "queue.Queue[str | None]" = queue.Queue()

        def _pump(stream, out_q: "queue.Queue[str | None]") -> None:
            try:
                for raw in iter(stream.readline, b""):
                    out_q.put(raw.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
            finally:
                out_q.put(None)  # EOF 哨兵

        threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()
        self._proc, self._queue, self._serial = proc, q, serial

    def _kill(self) -> None:
        proc = self._proc
        self._proc, self._queue = None, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _ensure(self, serial: str | None) -> None:
        if self._proc is None or self._proc.poll() is not None or self._serial != serial:
            self._kill()
            self._spawn(serial)

    def run(self, shell_cmd: str, serial: str | None, timeout: int) -> tuple[int, str]:
        """在持久会话中执行命令，返回 (returncode, 合并输出)。
        协议：发送 `cmd` + `echo <marker>$?<marker尾>`，读行直到标记行，从标记解析 rc。"""
        if self._disabled:
            raise _PersistUnavailable()
        if "\n" in shell_cmd or "\r" in shell_cmd:
            raise _PersistUnavailable()  # 多行命令会破坏标记协议，走单次模式
        if not _safe_for_session(shell_cmd):
            raise _PersistUnavailable()  # 引号/括号不配对，走单次模式
        with self._lock:
            self._ensure(serial)
            n = next(self._nonce)
            head = f"__PC_DONE_{n}_"
            # 子 shell 隔离 + 关闭 stdin：防止会读 stdin 的命令（uiautomator、裸 cat 等）
            # 吞掉后续标记行导致会话假死
            payload = f"({shell_cmd}) </dev/null\necho {head}$?{head}end\n"
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(payload.encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError, AssertionError):
                self._kill()
                raise _PersistUnavailable()

            out: list[str] = []
            deadline = time.monotonic() + timeout
            assert self._queue is not None
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    self._kill()
                    raise AdbError(f"持久 shell 命令超时（{timeout}s）: {shell_cmd[:120]}")
                try:
                    line = self._queue.get(timeout=min(remain, 0.25))
                except queue.Empty:
                    if self._proc is not None and self._proc.poll() is not None:
                        self._kill()
                        raise _PersistUnavailable()
                    continue
                if line is None:  # 会话 EOF（如命令里有 exit、设备掉线）
                    self._kill()
                    raise _PersistUnavailable()
                if line.startswith(head):
                    m = re.match(rf"__PC_DONE_{n}_(\-?\d+)__PC_DONE_{n}_end", line.strip())
                    return (int(m.group(1)) if m else 0), "".join(out)
                out.append(line)

    def close(self) -> None:
        try:
            with self._lock:
                self._kill()
        except Exception:  # noqa: BLE001
            pass


_PERSIST = PersistentShell()
atexit.register(_PERSIST.close)

# serial 解析缓存：避免每条命令都付出一次 `adb devices -l`（~40ms）
_SERIAL_CACHE: dict = {"serial": "", "ts": 0.0}
_DEVICE_GONE_HINTS = ("device not found", "device offline", "no devices/emulators", "device unauthorized", "closed")


def _invalidate_serial_cache() -> None:
    _SERIAL_CACHE["serial"] = ""
    _SERIAL_CACHE["ts"] = 0.0
    _PERSIST.close()


def run_adb(
    args: list[str],
    serial: str | None = None,
    timeout: int = 60,
    decode: bool = True,
) -> subprocess.CompletedProcess:
    """执行一次 adb 命令。args 不含 'adb' 本身与 -s serial。"""
    adb = find_adb()
    if not adb:
        raise AdbError(
            "找不到 adb。请运行项目内 install.ps1 自动下载 platform-tools，"
            "或安装 Android SDK platform-tools 后加入 PATH，或设置环境变量 ADB_PATH 指向 adb 可执行文件。"
        )
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += args

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 命令超时（{timeout}s）: {' '.join(args)}")
    except OSError as e:
        raise AdbError(f"无法执行 adb: {e}")

    if decode:
        proc.stdout = proc.stdout.decode("utf-8", errors="replace")  # type: ignore[assignment]
        proc.stderr = proc.stderr.decode("utf-8", errors="replace")  # type: ignore[assignment]
    # 设备掉线/未授权 → 让 serial 缓存与持久会话立即失效
    if proc.returncode != 0:
        blob = f"{proc.stderr!r}{proc.stdout!r}"
        if any(h in blob for h in _DEVICE_GONE_HINTS):
            _invalidate_serial_cache()
    return proc


def run_shell(
    shell_cmd: str, serial: str | None = None, timeout: int = 60, decode: bool = True
) -> subprocess.CompletedProcess:
    """在手机上执行 shell 命令（命令字符串交给设备端 sh 解释）。
    文本命令优先走持久会话（省进程创建/握手/sh 启动开销）；失败自动回退单次模式。"""
    if decode:
        s = _resolve_serial(serial)
        try:
            rc, out = _PERSIST.run(shell_cmd, s, timeout)
            return subprocess.CompletedProcess(["adb", "shell", shell_cmd], rc, stdout=out, stderr="")
        except _PersistUnavailable:
            serial = s  # 回退单次模式（serial 已解析，避免重复解析）
    return run_adb(["shell", shell_cmd], serial=serial, timeout=timeout, decode=decode)


def _checked(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        err = (proc.stderr or "") if isinstance(proc.stderr, str) else ""
        out = (proc.stdout or "") if isinstance(proc.stdout, str) else ""
        raise AdbError(f"{what} 失败 (rc={proc.returncode}): {(err or out).strip()}")
    return proc.stdout if isinstance(proc.stdout, str) else ""


def _resolve_serial(serial: str | None, max_age: float = 15.0) -> str | None:
    """未显式指定 serial 时：环境变量 PHONE_SERIAL > 缓存 > 唯一在线设备 > 报错。"""
    serial = (serial or "").strip() or os.environ.get("PHONE_SERIAL", "").strip()
    if serial:
        return serial
    now = time.monotonic()
    if _SERIAL_CACHE["serial"] and now - _SERIAL_CACHE["ts"] < max_age:
        return _SERIAL_CACHE["serial"]
    devices = list_devices()
    online = [d for d in devices if d["state"] == "device"]
    if len(online) == 1:
        _SERIAL_CACHE["serial"] = online[0]["serial"]
        _SERIAL_CACHE["ts"] = now
        return online[0]["serial"]
    if len(online) > 1:
        raise AdbError(
            "检测到多台设备，请通过 serial 参数或环境变量 PHONE_SERIAL 指定其一: "
            + ", ".join(d["serial"] for d in online)
        )
    if devices:
        bad = devices[0]
        if bad["state"] == "unauthorized":
            raise AdbError(
                f"设备 {bad['serial']} 未授权：请点亮手机屏幕，在『允许 USB 调试吗？』弹窗中勾选『一律允许』并确定。"
            )
        raise AdbError(f"设备 {bad['serial']} 状态异常: {bad['state']}（可尝试 adb kill-server 后重插数据线）")
    raise AdbError(
        "没有检测到任何 Android 设备。请确认：① 手机已开启 USB 调试（设置→开发者选项）；"
        "② 数据线已连接且选择『传输文件』模式；③ 手机上已授权本电脑。然后运行 doctor 复查。"
    )


def _shell_quote(s: str) -> str:
    """为设备端 sh 做单引号安全引用。"""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# 基础信息
# ---------------------------------------------------------------------------

def doctor() -> str:
    """自检：adb 是否可用、服务器/设备状态，并给出修复建议。返回人类可读文本。"""
    lines: list[str] = []
    adb = find_adb()
    if not adb:
        lines.append("[X] 找不到 adb")
        lines.append("    修复：运行 install.ps1（会自动下载 platform-tools 到项目 adb/ 目录），")
        lines.append("    或设置环境变量 ADB_PATH，或把 adb 加入 PATH。")
        return "\n".join(lines)
    lines.append(f"[OK] adb: {adb}")
    proc = run_adb(["version"])
    if proc.returncode == 0:
        lines.append("[OK] 版本: " + proc.stdout.strip().splitlines()[0])
    else:
        lines.append(f"[X] adb version 执行失败: {proc.stderr.strip()}")
        return "\n".join(lines)

    proc = run_adb(["devices", "-l"])
    if proc.returncode != 0:
        lines.append(f"[X] adb devices 失败: {proc.stderr.strip()}")
        return "\n".join(lines)

    devices = list_devices()
    if not devices:
        lines.append("[X] 未检测到设备")
        lines.append("    修复：① 手机开启 USB 调试；② 换一条支持数据传输的线；")
        lines.append("    ③ 插线后在手机通知栏把 USB 用途改为『传输文件 (MTP)』；")
        lines.append("    ④ 仍不行则 adb kill-server && adb devices 重试；")
        lines.append("    ⑤ 无线连接可用 adb pair ip:端口 配对后 adb connect ip:端口。")
    for d in devices:
        mark = "[OK]" if d["state"] == "device" else "[X]"
        extra = f" model={d.get('model')}" if d.get("model") else ""
        lines.append(f"{mark} 设备 {d['serial']} 状态={d['state']}{extra}")
        if d["state"] == "unauthorized":
            lines.append("    修复：点亮手机屏幕，在 USB 调试授权弹窗中勾选『一律允许』并确定。")
        elif d["state"] == "offline":
            lines.append("    修复：重插数据线，或 adb kill-server 后重试。")
    return "\n".join(lines)


def list_devices() -> list[dict]:
    """列出所有 adb 已知的设备及其状态。"""
    proc = run_adb(["devices", "-l"], timeout=30)
    _checked(proc, "adb devices")
    devices = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        d = {"serial": parts[0], "state": parts[1] if len(parts) > 1 else "unknown"}
        for kv in parts[2:]:
            if ":" in kv:
                k, v = kv.split(":", 1)
                d[k] = v
        devices.append(d)
    return devices


def device_info(serial: str = "") -> dict:
    """设备综合信息：型号、系统版本、分辨率、电量、屏幕状态、网络、存储。
    性能：所有探测合并为一次 shell 往返（旧版需 10+ 次）。"""
    s = _resolve_serial(serial)
    script = (
        "echo '=B='; getprop ro.product.brand; getprop ro.product.model; "
        "getprop ro.build.version.release; getprop ro.build.version.sdk; "
        "echo '=S='; wm size; wm density; "
        "echo '=BAT='; dumpsys battery; "
        "echo '=PWR='; dumpsys power | grep mWakefulness= | head -1; "
        "echo '=NET='; settings get global wifi_on; settings get global mobile_data; "
        "ip route | grep src | head -1; "
        "echo '=STO='; df -h /data | tail -1; "
        "echo '=APP='; dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head -1"
    )
    out = run_shell(script, s, timeout=60).stdout

    # 按 =X= 标记切分段落
    sections: dict[str, list[str]] = {}
    cur: str | None = None
    for line in out.splitlines():
        if len(line) <= 6 and line.startswith("=") and line.endswith("="):
            cur = line.strip("=")
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)

    def joined(name: str) -> str:
        return "\n".join(sections.get(name, []))

    b = [x.strip() for x in joined("B").splitlines()]
    info: dict = {
        "serial": s,
        "brand": b[0] if len(b) > 0 else "",
        "model": b[1] if len(b) > 1 else "",
        "android_version": b[2] if len(b) > 2 else "",
        "sdk": b[3] if len(b) > 3 else "",
    }

    size_txt = joined("S")
    m = re.search(r"(\d+)x(\d+)", size_txt)
    if m:
        info["screen"] = f"{m.group(1)}x{m.group(2)}"
        info["screen_width"], info["screen_height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"density:\s*(\d+)|(\d+)", size_txt.splitlines()[-1] if size_txt else "")
    if m:
        info["dpi"] = int(m.group(1) or m.group(2))

    bat = joined("BAT")
    bt: dict = {}
    for key, pat in [("level_percent", r"level:\s*(\d+)"), ("temperature", r"temperature:\s*(\d+)"),
                     ("status", r"status:\s*(\d+)"), ("plugged", r"plugged:\s*(\d+)")]:
        mm = re.search(pat, bat)
        if mm:
            bt[key] = int(mm.group(1))
    status_map = {1: "unknown", 2: "charging", 3: "discharging", 4: "not_charging", 5: "full"}
    if "status" in bt:
        bt["status_text"] = status_map.get(bt["status"], str(bt["status"]))
    if "temperature" in bt:
        bt["temperature_c"] = round(bt["temperature"] / 10.0, 1)
    info["battery"] = bt

    mm = re.search(r"mWakefulness=(\w+)", joined("PWR"))
    info["screen_state"] = mm.group(1) if mm else "unknown"  # Awake / Asleep / ...

    net_lines = [x.strip() for x in joined("NET").splitlines() if x.strip()]
    if len(net_lines) >= 1:
        info["wifi_on"] = net_lines[0] == "1"
    if len(net_lines) >= 2:
        info["mobile_data_on"] = net_lines[1] == "1"
    mm = re.search(r"src (\d+\.\d+\.\d+\.\d+)", joined("NET"))
    info["ip"] = mm.group(1) if mm else ""

    df = joined("STO").split()
    if len(df) >= 4:
        info["storage_data"] = {"size": df[1], "used": df[2], "avail": df[3]}

    mm = re.search(r"u0 (\S+)/(\S+)", joined("APP"))
    info["current_app"] = (
        {"package": mm.group(1), "activity": mm.group(2).rstrip("}")} if mm else {}
    )
    return info


def _current_app(serial: str | None) -> dict:
    out = run_shell(
        "dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head -1",
        serial,
    ).stdout
    mm = re.search(r"u0 (\S+)/(\S+)", out)
    if mm:
        return {"package": mm.group(1), "activity": mm.group(2)}
    out2 = run_shell("dumpsys window | grep mCurrentFocus | head -1", serial).stdout
    mm = re.search(r"(\S+)/(\S+)", out2[out2.find("Window{"):] if "Window{" in out2 else out2)
    if mm and "." in mm.group(1):
        return {"package": mm.group(1), "activity": mm.group(2)}
    return {}


def current_app(serial: str = "") -> dict:
    """当前前台应用的包名与 Activity。"""
    return _current_app(_resolve_serial(serial))


# ---------------------------------------------------------------------------
# 屏幕：截图 / UI 层级 / 找控件
# ---------------------------------------------------------------------------

def screenshot(save_path: str = "", serial: str = "") -> dict:
    """截取手机屏幕，保存为 PNG。返回 {path, size, png_base64}。"""
    s = _resolve_serial(serial)
    proc = run_adb(["exec-out", "screencap", "-p"], serial=s, timeout=60, decode=False)
    data: bytes = proc.stdout or b""
    if proc.returncode != 0 or not data.startswith(b"\x89PNG"):
        # 兜底方案：先存到手机再 pull（老设备 exec-out 可能不可靠）
        remote = "/sdcard/_phone_control_screen.png"
        _checked(run_shell(f"screencap -p {remote}", s, timeout=60), "screencap")
        tmp = Path(tempfile.gettempdir()) / "_phone_control_screen.png"
        _checked(run_adb(["pull", remote, str(tmp)], serial=s, timeout=60), "pull 截图")
        run_shell(f"rm -f {remote}", s)
        data = tmp.read_bytes()

    if save_path:
        out = Path(save_path).expanduser().resolve()
    else:
        out = PROJECT_DIR / "screenshots" / time.strftime("screen_%Y%m%d_%H%M%S.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {
        "path": str(out),
        "bytes": len(data),
        "png_base64": base64.b64encode(data).decode("ascii"),
    }


def _fetch_ui_xml(serial: str | None, timeout: int = 60) -> str:
    """单次往返获取 UI 层级 XML：dump→cat→rm 合并为一条设备端命令，经 exec-out
    原样回传（取代旧版三次 adb 往返 + 临时文件）。大流量二进制安全，不走持久会话。"""
    remote = "/sdcard/_phone_control_ui.xml"
    cmd = (
        f"uiautomator dump {remote} >/dev/null 2>&1; echo '=XML='; "
        f"cat {remote} 2>/dev/null; rm -f {remote}"
    )
    proc = run_adb(["exec-out", cmd], serial=serial, timeout=timeout)
    out = proc.stdout if isinstance(proc.stdout, str) else ""
    if "=XML=" not in out:
        err = proc.stderr if isinstance(proc.stderr, str) else ""
        raise AdbError(f"获取 UI 层级失败 (rc={proc.returncode}): {(err or out)[:200]}")
    xml_text = out[out.find("=XML=") + len("=XML="):].strip("\r\n")
    j = xml_text.find("<?xml")
    if j > 0:
        xml_text = xml_text[j:]
    k = xml_text.rfind("</hierarchy>")
    if k >= 0:
        xml_text = xml_text[: k + len("</hierarchy>")]
    if "<hierarchy" not in xml_text[:3000]:
        raise AdbError("UI 层级为空或异常（屏幕是否熄灭/锁定？当前界面是否禁止 dump？）")
    return xml_text


def _find_in_xml(xml_text: str, target: str, exact: bool = False, clickable_only: bool = False) -> list[dict]:
    """在 UI XML 中查找文字匹配的控件，返回中心坐标等信息列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise AdbError(f"解析 UI 层级失败: {e}")
    results: list[dict] = []
    target_l = target.lower()
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        if exact:
            hit = target_l in (text.lower(), desc.lower())
        else:
            hit = target_l in (text + " " + desc).lower()
        if not hit:
            continue
        clickable = node.get("clickable") == "true"
        if clickable_only and not clickable:
            continue
        bounds = node.get("bounds", "")
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        results.append({
            "text": text or desc,
            "center_x": (x1 + x2) // 2,
            "center_y": (y1 + y2) // 2,
            "bounds": bounds,
            "class": node.get("class", ""),
            "clickable": clickable,
            "package": node.get("package", ""),
        })
    return results


def ui_dump(serial: str = "", save_path: str = "") -> dict:
    """导出当前界面 UI 层级 XML（uiautomator dump，单次往返）。返回 {path, xml}。"""
    s = _resolve_serial(serial)
    _ensure_screen_ready(s)
    xml_text = _fetch_ui_xml(s)
    out = Path(save_path) if save_path else (PROJECT_DIR / "screenshots" / time.strftime("ui_%Y%m%d_%H%M%S.xml"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml_text, encoding="utf-8")
    return {"path": str(out), "xml": xml_text}


def find_text(target: str, serial: str = "", clickable_only: bool = False, exact: bool = False) -> list[dict]:
    """在当前界面查找包含指定文字的控件，返回可直接用于 tap 的中心坐标。
    匹配 text 与 content-desc 属性；exact=True 时要求完全相等。
    返回 [{text, center_x, center_y, bounds, class, clickable}]"""
    s = _resolve_serial(serial)
    _ensure_screen_ready(s)
    return _find_in_xml(_fetch_ui_xml(s), target, exact, clickable_only)


# ---------------------------------------------------------------------------
# 输入：点击 / 滑动 / 按键 / 文字
# ---------------------------------------------------------------------------

KEYCODES = {
    "back": 4, "home": 3, "recent": 187, "apps": 187, "recents": 187,
    "power": 26, "volume_up": 24, "volume_down": 25, "volume_mute": 164,
    "enter": 66, "return": 66, "del": 67, "backspace": 67, "menu": 82,
    "camera": 27, "tab": 61, "space": 62, "esc": 111, "escape": 111,
    "search": 84, "media_play_pause": 85, "brightness_up": 220,
    "brightness_down": 221, "page_up": 92, "page_down": 93,
    "dpad_up": 19, "dpad_down": 20, "dpad_left": 21, "dpad_right": 22,
    "dpad_center": 23, "wakeup": 224, "sleep": 223, "notification": 83,
    "call": 5, "end_call": 6, "screenshot": 120,
}


def tap(x: int, y: int, serial: str = "") -> str:
    """在屏幕坐标 (x, y) 处点击一次。"""
    s = _resolve_serial(serial)
    return _checked(run_shell(f"input tap {int(x)} {int(y)}", s), "tap").strip() or "ok"


def long_press(x: int, y: int, duration_ms: int = 1000, serial: str = "") -> str:
    """在 (x, y) 长按 duration_ms 毫秒。"""
    return swipe(x, y, x, y, duration_ms=duration_ms, serial=serial)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, serial: str = "") -> str:
    """从 (x1,y1) 滑动到 (x2,y2)，耗时 duration_ms 毫秒。"""
    s = _resolve_serial(serial)
    return _checked(
        run_shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}", s),
        "swipe",
    ).strip() or "ok"


def swipe_screen(direction: str = "up", fraction: float = 0.6, duration_ms: int = 300, serial: str = "") -> str:
    """按屏幕比例整屏滑动。direction: up(浏览更多内容)/down/left/right。"""
    s = _resolve_serial(serial)
    size = run_shell("wm size", s).stdout.strip()
    m = re.search(r"(\d+)x(\d+)", size.splitlines()[-1] if size else "")
    if not m:
        raise AdbError("无法获取屏幕分辨率: " + size)
    w, h = int(m.group(1)), int(m.group(2))
    f = max(0.05, min(0.95, float(fraction)))
    cx, cy = w // 2, h // 2
    if direction == "up":      # 手指向上滑 = 内容向上翻（看下面的内容）
        y1, y2 = int(h * (0.5 + f / 2)), int(h * (0.5 - f / 2))
        x1 = x2 = cx
    elif direction == "down":
        y1, y2 = int(h * (0.5 - f / 2)), int(h * (0.5 + f / 2))
        x1 = x2 = cx
    elif direction == "left":
        x1, x2 = int(w * (0.5 + f / 2)), int(w * (0.5 - f / 2))
        y1 = y2 = cy
    elif direction == "right":
        x1, x2 = int(w * (0.5 - f / 2)), int(w * (0.5 + f / 2))
        y1 = y2 = cy
    else:
        raise AdbError(f"direction 必须是 up/down/left/right，收到: {direction}")
    return swipe(x1, y1, x2, y2, duration_ms=duration_ms, serial=s)


def press_key(key: str, serial: str = "") -> str:
    """按键。key 支持名称（back/home/recent/power/volume_up/volume_down/enter/menu...）、
    数字键码或完整 KEYCODE_* 名称。"""
    s = _resolve_serial(serial)
    k = str(key).strip()
    low = k.lower()
    if low in KEYCODES:
        code = str(KEYCODES[low])
    elif k.isdigit():
        code = k
    elif low.startswith("keycode_"):
        code = low
    else:
        raise AdbError(
            f"未知按键 {key!r}。可用名称: {', '.join(sorted(set(KEYCODES)))}，或数字键码，或 KEYCODE_* 全名。"
        )
    return _checked(run_shell(f"input keyevent {code}", s), f"keyevent {key}").strip() or "ok"


def screen_on(serial: str = "") -> str:
    """点亮屏幕并尝试解除无密码锁屏。"""
    s = _resolve_serial(serial)
    state = run_shell("dumpsys power | grep mWakefulness= | head -1", s).stdout
    if "Awake" not in state:
        run_shell("input keyevent 224", s)  # WAKEUP
        time.sleep(0.5)
    run_shell("wm dismiss-keyguard", s, timeout=10)
    return "ok"


def _ensure_screen_ready(serial: str | None) -> None:
    """自动唤醒屏幕；若停在安全锁屏（人脸/指纹/密码）则报错提示用户解锁。
    常规路径只花一次 shell 往返。"""
    probe = (
        "dumpsys power | grep mWakefulness= | head -1; echo '=K='; "
        "dumpsys window policy | grep mInputRestricted | head -1"
    )
    out = run_shell(probe, serial, timeout=30).stdout
    if "Awake" not in out.split("=K=")[0]:
        run_shell("input keyevent 224", serial)
        time.sleep(0.8)
        out = run_shell(probe, serial, timeout=30).stdout
    if "mInputRestricted=true" in out:
        raise AdbError(
            "手机当前停在锁屏界面（设置了人脸/指纹/密码锁），adb 无法代为解锁。"
            "请先解锁手机屏幕，然后重试本操作。"
            "（提示：可执行 shell \"svc power stayon true\" 让充电时屏幕常亮，解锁一次即可保持可用）"
        )


def input_text(text: str, serial: str = "") -> str:
    """向当前焦点输入框输入文字。
    ASCII 直接用 input text；包含中文等非 ASCII 字符时使用 ADBKeyboard（若已安装），
    未安装则报错并给出安装指引。"""
    s = _resolve_serial(serial)
    if not text:
        return "ok"

    has_adb_keyboard = "com.android.adbkeyboard" in run_shell(
        "pm list packages com.android.adbkeyboard", s
    ).stdout

    def is_simple(t: str) -> bool:
        # input text 可安全处理的字符（%s 会被解释为空格，含 % 时也走 ADBKeyboard）
        return t.isascii() and "%" not in t

    if is_simple(text) or not has_adb_keyboard:
        if not is_simple(text):
            raise AdbError(
                "输入包含中文/特殊字符，但手机未安装 ADBKeyboard 输入法。\n"
                "解决：① 从 https://github.com/senzhk/ADBKeyBoard 下载 ADBKeyboard.apk；"
                "② 用 install_app 工具（或 adb install）安装；之后重试即可（工具会自动切换输入法并复原）。\n"
                "当前仅支持直接输入 ASCII 文本。"
            )
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if i > 0:
                run_shell("input keyevent 66", s)
            if seg:
                _checked(run_shell(f"input text {_shell_quote(seg)}", s), "input text")
        return "ok"

    # ADBKeyboard 路径：临时切换输入法 → base64 广播 → 恢复
    prev_ime = run_shell("settings get secure default_input_method", s).stdout.strip()
    try:
        _checked(run_shell("ime set com.android.adbkeyboard/.AdbIME", s), "切换 ADBKeyboard")
        time.sleep(0.4)
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        _checked(
            run_shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", s),
            "ADBKeyboard 输入",
        )
        return "ok"
    finally:
        if prev_ime and prev_ime != "null":
            run_shell(f"ime set {_shell_quote(prev_ime)}", s)


# ---------------------------------------------------------------------------
# 应用管理
# ---------------------------------------------------------------------------

def open_app(package: str, serial: str = "") -> str:
    """启动应用（通过 launcher intent）。"""
    s = _resolve_serial(serial)
    out = run_shell(
        f"monkey -p {_shell_quote(package)} -c android.intent.category.LAUNCHER 1", s, timeout=30
    ).stdout
    if "No activities found" in out or "Error" in out:
        # 兜底：解析 manifest 的 launcher activity
        alt = run_shell(
            f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {_shell_quote(package)} | tail -1",
            s,
        ).stdout.strip()
        if alt and "/" in alt:
            return _checked(run_shell(f"am start -n {_shell_quote(alt)}", s), f"启动 {package}") or "ok"
        raise AdbError(f"无法启动 {package}：未找到 launcher Activity。包名是否正确？可用 list_apps 查询。")
    return "ok"


def stop_app(package: str, serial: str = "") -> str:
    """强制停止应用。"""
    s = _resolve_serial(serial)
    return _checked(run_shell(f"am force-stop {_shell_quote(package)}", s), f"停止 {package}") or "ok"


def restart_app(package: str, serial: str = "") -> str:
    """重启应用（先停止再启动）。"""
    stop_app(package, serial)
    time.sleep(0.5)
    return open_app(package, serial)


def list_apps(system: bool = False, contains: str = "", serial: str = "") -> list[str]:
    """列出已安装应用包名。system=False 仅第三方应用；contains 做子串过滤。"""
    s = _resolve_serial(serial)
    cmd = "pm list packages" + ("" if system else " -3")
    out = _checked(run_shell(cmd, s, timeout=60), "pm list packages")
    pkgs = sorted(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("package:")
    )
    if contains:
        pkgs = [p for p in pkgs if contains.lower() in p.lower()]
    return pkgs


def install_app(apk_path: str, allow_downgrade: bool = False, serial: str = "") -> str:
    """安装本机上的 APK 到手机（覆盖安装）。"""
    s = _resolve_serial(serial)
    p = Path(apk_path).expanduser().resolve()
    if not p.is_file():
        raise AdbError(f"APK 文件不存在: {p}")
    flags = "-r" + (" -d" if allow_downgrade else "")
    proc = run_adb(["install", *flags.split(), str(p)], serial=s, timeout=600)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "Success" not in out:
        raise AdbError(f"安装失败: {out}")
    return "ok: " + out.splitlines()[-1]


def uninstall_app(package: str, keep_data: bool = False, serial: str = "") -> str:
    """卸载应用。keep_data=True 保留数据与缓存。"""
    s = _resolve_serial(serial)
    cmd = f"pm uninstall {'-k ' if keep_data else ''}{_shell_quote(package)}"
    out = _checked(run_shell(cmd, s, timeout=120), f"卸载 {package}").strip()
    if out != "Success":
        raise AdbError(f"卸载 {package} 结果: {out}")
    return "ok"


# ---------------------------------------------------------------------------
# 文件传输
# ---------------------------------------------------------------------------

def push_file(local_path: str, remote_path: str = "/sdcard/Download/", serial: str = "") -> str:
    """把电脑上的文件/目录推送到手机。remote_path 以 / 结尾时视为目录。"""
    s = _resolve_serial(serial)
    p = Path(local_path).expanduser().resolve()
    if not p.exists():
        raise AdbError(f"本地路径不存在: {p}")
    remote = remote_path
    if remote.endswith("/") and p.is_file():
        remote = remote + p.name
    proc = run_adb(["push", str(p), remote], serial=s, timeout=1800)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "error" in out.lower() and "pushed" not in out.lower():
        raise AdbError(f"push 失败: {out}")
    return f"ok: {out.splitlines()[-1] if out else remote}"


def pull_file(remote_path: str, local_path: str = ".", serial: str = "") -> dict:
    """把手机上的文件/目录拉取到电脑。返回 {local_path}。"""
    s = _resolve_serial(serial)
    local = Path(local_path).expanduser().resolve()
    local.mkdir(parents=True, exist_ok=True)
    proc = run_adb(["pull", remote_path, str(local)], serial=s, timeout=1800)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "does not exist" in out:
        raise AdbError(f"pull 失败: {out}")
    pulled = local / Path(remote_path.rstrip("/")).name
    return {"local_path": str(pulled if pulled.exists() else local), "detail": out.splitlines()[-1] if out else ""}


# ---------------------------------------------------------------------------
# 通用 shell 逃生舱
# ---------------------------------------------------------------------------

def device_shell(command: str, serial: str = "", timeout: int = 60) -> dict:
    """在手机上执行任意 shell 命令，返回 {returncode, stdout, stderr}。"""
    s = _resolve_serial(serial)
    proc = run_shell(command, s, timeout=timeout)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


# ---------------------------------------------------------------------------
# 高级组合操作 —— 减少 agent 往返：一次调用完成"找+点"、"等待+确认"、"整套动作"
# ---------------------------------------------------------------------------

def wait_for_text(target: str, timeout_s: float = 15.0, interval_s: float = 0.8,
                  exact: bool = False, serial: str = "") -> dict:
    """轮询等待界面上出现指定文字（替代盲目 sleep），返回首个匹配控件信息。"""
    s = _resolve_serial(serial)
    _ensure_screen_ready(s)
    deadline = time.monotonic() + float(timeout_s)
    while True:
        try:
            hits = _find_in_xml(_fetch_ui_xml(s), target, exact)
            if hits:
                return hits[0]
        except AdbError:
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise AdbError(f"等待超时（{timeout_s}s）：界面上未出现 '{target}'")
        time.sleep(interval_s)


def tap_text(target: str, index: int = 0, exact: bool = False,
             wait_timeout: float = 0, serial: str = "") -> dict:
    """按文字找到控件并点击（= find_text + tap 一步完成，仅一次界面抓取）。
    wait_timeout>0 时先等待文字出现（适合加载慢的界面）。返回 {tapped: 控件信息}。"""
    s = _resolve_serial(serial)
    if wait_timeout and float(wait_timeout) > 0:
        node = wait_for_text(target, wait_timeout, exact=exact, serial=s)
    else:
        _ensure_screen_ready(s)
        hits = _find_in_xml(_fetch_ui_xml(s), target, exact)
        if not hits:
            raise AdbError(
                f"当前界面未找到 '{target}'。建议：① screenshot 查看当前画面；"
                "② 界面还在加载则加 wait_timeout 重试；③ 检查文字是否精确。"
            )
        idx = max(0, min(int(index), len(hits) - 1))
        node = hits[idx]
    tap(node["center_x"], node["center_y"], s)
    return {"tapped": node}


def run_script(actions, serial: str = "", stop_on_error: bool = True) -> list[dict]:
    """按顺序执行一串动作，一次调用完成多步流程（大幅减少 agent/LLM 往返）。

    actions 为列表（或 JSON 字符串），每项为含 "action" 字段的 dict：
      tap {x, y}                       点击坐标
      tap_text {text, index?, exact?, wait_timeout?}   按文字点击
      long_press {x, y, ms?}           长按
      swipe {x1, y1, x2, y2, ms?}     滑动
      swipe_screen {direction, ms?}   整屏翻页 up/down/left/right
      key {key}                       按键 back/home/recent/...
      text {text}                     输入文字（需先聚焦输入框）
      sleep {ms}                      等待
      wait_for {text, timeout_s?, exact?}  等待文字出现
      open {package} / stop {package}  启动/停止应用
      current_app {}                  查询前台应用
      assert_app {package}            断言前台应用（不是则报错）
      screenshot {save_path?}         截图（不含 base64）
      find {text, exact?}             查找控件
      shell {command, timeout?}       设备 shell
    返回 [{step, action, ok, result|error}]；stop_on_error=True 时遇错即停。"""
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except json.JSONDecodeError as e:
            raise AdbError(f"actions 不是合法 JSON: {e}")
    if not isinstance(actions, list) or not actions:
        raise AdbError("actions 必须是非空列表")
    s = _resolve_serial(serial)
    results: list[dict] = []
    for i, a in enumerate(actions):
        act = a.get("action") if isinstance(a, dict) else None
        entry: dict = {"step": i + 1, "action": act}
        try:
            if not isinstance(a, dict) or not act:
                raise AdbError(f"第 {i + 1} 步格式错误（需要含 action 字段的 dict）")
            entry["result"] = _run_action(act, a, s)
            entry["ok"] = True
            results.append(entry)
        except Exception as e:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(e).__name__}: {e}"
            results.append(entry)
            if stop_on_error:
                break
    return results


def _run_action(act: str, a: dict, s: str | None):
    if act == "tap":
        return tap(a["x"], a["y"], s)
    if act == "tap_text":
        return tap_text(a["text"], a.get("index", 0), a.get("exact", False),
                        a.get("wait_timeout", 0), s)
    if act == "long_press":
        return long_press(a["x"], a["y"], a.get("ms", 1000), s)
    if act == "swipe":
        return swipe(a["x1"], a["y1"], a["x2"], a["y2"], a.get("ms", 300), s)
    if act == "swipe_screen":
        return swipe_screen(a.get("direction", "up"), duration_ms=a.get("ms", 300), serial=s)
    if act == "key":
        return press_key(a["key"], s)
    if act == "text":
        return input_text(a["text"], s)
    if act == "sleep":
        time.sleep(min(max(float(a.get("ms", 1000)), 0), 60000) / 1000.0)
        return "ok"
    if act == "wait_for":
        return wait_for_text(a["text"], a.get("timeout_s", 15),
                             exact=a.get("exact", False), serial=s)
    if act == "open":
        return open_app(a["package"], s)
    if act == "stop":
        return stop_app(a["package"], s)
    if act == "current_app":
        return current_app(s)
    if act == "assert_app":
        cur = current_app(s)
        if cur.get("package") != a["package"]:
            raise AdbError(f"前台应用不是 {a['package']}，实际为 {cur.get('package') or '未知'}")
        return cur
    if act == "screenshot":
        r = screenshot(a.get("save_path", ""), s)
        return {"path": r["path"], "bytes": r["bytes"]}
    if act == "find":
        return find_text(a["text"], s, exact=a.get("exact", False))
    if act == "shell":
        return device_shell(a["command"], s, a.get("timeout", 60))
    raise AdbError(f"未知 action: {act!r}")


# ---------------------------------------------------------------------------
# 无线调试（Wi-Fi ADB）
# ---------------------------------------------------------------------------

def wireless_pair(address: str, pairing_code: str, timeout: int = 120) -> str:
    """无线调试配对（Android 11+）：address 为手机『无线调试→使用配对码配对』页显示的 ip:配对端口。"""
    proc = run_adb(["pair", address, str(pairing_code)], timeout=timeout)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "Successfully paired" not in out:
        raise AdbError(f"配对失败: {out}")
    return out


def wireless_connect(address: str, timeout: int = 30) -> str:
    """无线调试连接：address 为『无线调试』主页显示的 ip:连接端口（注意不是配对端口）。"""
    proc = run_adb(["connect", address], timeout=timeout)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    low = out.lower()
    if proc.returncode != 0 or "unable" in low or "failed" in low or "cannot" in low:
        raise AdbError(f"连接失败: {out}（确认手机与电脑同一网络、已开启无线调试；首次需先 wireless_pair）")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        # 截图的 base64 太大，CLI 下不打印
        if isinstance(obj, dict) and "png_base64" in obj:
            obj = {k: v for k, v in obj.items() if k != "png_base64"}
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    import argparse

    # 统一输出 UTF-8，避免中文 Windows 控制台/管道下 JSON 乱码
    for _stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(prog="phone_control", description="通过 ADB 控制 Android 手机（CLI）")
    ap.add_argument("--serial", default="", help="设备序列号（多台设备时必填；也可用环境变量 PHONE_SERIAL）")
    ap.add_argument("--version", action="version", version=f"phone-control {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="自检 adb / 设备状态")
    sub.add_parser("devices", help="列出设备")
    sub.add_parser("info", help="设备综合信息")
    sub.add_parser("current-app", help="当前前台应用")

    p = sub.add_parser("screenshot", help="截屏")
    p.add_argument("out", nargs="?", default="", help="保存路径（默认 screenshots/screen_时间.png）")

    p = sub.add_parser("tap", help="点击")
    p.add_argument("x", type=int); p.add_argument("y", type=int)

    p = sub.add_parser("long-press", help="长按")
    p.add_argument("x", type=int); p.add_argument("y", type=int)
    p.add_argument("--ms", type=int, default=1000)

    p = sub.add_parser("swipe", help="滑动")
    p.add_argument("x1", type=int); p.add_argument("y1", type=int)
    p.add_argument("x2", type=int); p.add_argument("y2", type=int)
    p.add_argument("--ms", type=int, default=300)

    p = sub.add_parser("swipe-screen", help="整屏滑动")
    p.add_argument("direction", choices=["up", "down", "left", "right"])
    p.add_argument("--ms", type=int, default=300)

    p = sub.add_parser("key", help="按键（back/home/recent/power/volume_up/...）")
    p.add_argument("key")

    sub.add_parser("screen-on", help="点亮屏幕")

    p = sub.add_parser("text", help="输入文字（中文需手机装 ADBKeyboard）")
    p.add_argument("text")

    p = sub.add_parser("open", help="启动应用")
    p.add_argument("package")

    p = sub.add_parser("stop", help="强制停止应用")
    p.add_argument("package")

    p = sub.add_parser("restart", help="重启应用")
    p.add_argument("package")

    p = sub.add_parser("apps", help="列出应用")
    p.add_argument("--system", action="store_true", help="包含系统应用")
    p.add_argument("--contains", default="", help="包名子串过滤")

    p = sub.add_parser("install", help="安装 APK")
    p.add_argument("apk")
    p.add_argument("--downgrade", action="store_true")

    p = sub.add_parser("uninstall", help="卸载应用")
    p.add_argument("package")
    p.add_argument("--keep-data", action="store_true")

    p = sub.add_parser("push", help="推送文件到手机")
    p.add_argument("local"); p.add_argument("remote", nargs="?", default="/sdcard/Download/")

    p = sub.add_parser("pull", help="从手机拉取文件")
    p.add_argument("remote"); p.add_argument("local", nargs="?", default=".")

    p = sub.add_parser("ui-dump", help="导出 UI 层级 XML")
    p.add_argument("out", nargs="?", default="")

    p = sub.add_parser("find", help="按文字查找控件坐标")
    p.add_argument("text")
    p.add_argument("--clickable-only", action="store_true")
    p.add_argument("--exact", action="store_true", help="要求文字完全相等")

    p = sub.add_parser("tap-text", help="按文字找到控件并点击（一步完成）")
    p.add_argument("text")
    p.add_argument("--index", type=int, default=0, help="多个匹配时点第几个（0 起）")
    p.add_argument("--exact", action="store_true", help="要求文字完全相等")
    p.add_argument("--wait", type=float, default=0, help="先等待文字出现的秒数（界面加载慢时用）")

    p = sub.add_parser("wait-for", help="等待界面上出现指定文字")
    p.add_argument("text")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--exact", action="store_true")

    p = sub.add_parser("script", help="按 JSON 脚本批量执行动作（文件路径 / 内联 JSON / - 读 stdin）")
    p.add_argument("src")
    p.add_argument("--continue-on-error", action="store_true")

    p = sub.add_parser("connect", help="无线调试连接 ip:连接端口")
    p.add_argument("address")

    p = sub.add_parser("pair", help="无线调试配对 ip:配对端口 配对码")
    p.add_argument("address")
    p.add_argument("code")

    p = sub.add_parser("shell", help="在手机上执行任意命令")
    p.add_argument("command")
    p.add_argument("--timeout", type=int, default=60)

    a = ap.parse_args(argv)
    ser = a.serial

    try:
        if a.cmd == "doctor":
            _print(doctor())
        elif a.cmd == "devices":
            _print(list_devices())
        elif a.cmd == "info":
            _print(device_info(ser))
        elif a.cmd == "current-app":
            _print(current_app(ser))
        elif a.cmd == "screenshot":
            _print(screenshot(a.out, ser))
        elif a.cmd == "tap":
            _print(tap(a.x, a.y, ser))
        elif a.cmd == "long-press":
            _print(long_press(a.x, a.y, a.ms, ser))
        elif a.cmd == "swipe":
            _print(swipe(a.x1, a.y1, a.x2, a.y2, a.ms, ser))
        elif a.cmd == "swipe-screen":
            _print(swipe_screen(a.direction, duration_ms=a.ms, serial=ser))
        elif a.cmd == "key":
            _print(press_key(a.key, ser))
        elif a.cmd == "screen-on":
            _print(screen_on(ser))
        elif a.cmd == "text":
            _print(input_text(a.text, ser))
        elif a.cmd == "open":
            _print(open_app(a.package, ser))
        elif a.cmd == "stop":
            _print(stop_app(a.package, ser))
        elif a.cmd == "restart":
            _print(restart_app(a.package, ser))
        elif a.cmd == "apps":
            _print(list_apps(system=a.system, contains=a.contains, serial=ser))
        elif a.cmd == "install":
            _print(install_app(a.apk, a.downgrade, ser))
        elif a.cmd == "uninstall":
            _print(uninstall_app(a.package, a.keep_data, ser))
        elif a.cmd == "push":
            _print(push_file(a.local, a.remote, ser))
        elif a.cmd == "pull":
            _print(pull_file(a.remote, a.local, ser))
        elif a.cmd == "ui-dump":
            r = ui_dump(ser, a.out)
            _print({"path": r["path"], "xml_bytes": len(r["xml"])})
        elif a.cmd == "find":
            _print(find_text(a.text, ser, a.clickable_only, a.exact))
        elif a.cmd == "tap-text":
            _print(tap_text(a.text, a.index, a.exact, a.wait, ser))
        elif a.cmd == "wait-for":
            _print(wait_for_text(a.text, a.timeout, exact=a.exact, serial=ser))
        elif a.cmd == "script":
            src = a.src
            if src == "-":
                data = sys.stdin.read()
            elif Path(src).is_file():
                data = Path(src).read_text(encoding="utf-8")
            else:
                data = src  # 内联 JSON
            _print(run_script(data, ser, stop_on_error=not a.continue_on_error))
        elif a.cmd == "connect":
            _print(wireless_connect(a.address))
        elif a.cmd == "pair":
            _print(wireless_pair(a.address, a.code))
        elif a.cmd == "shell":
            _print(device_shell(a.command, ser, a.timeout))
        else:  # pragma: no cover
            ap.print_help()
            return 2
        return 0
    except AdbError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
