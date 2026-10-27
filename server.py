# -*- coding: utf-8 -*-
"""
server.py —— phone-control MCP 服务器（stdio 传输）

把 phone_control.py 的全部能力暴露为标准 MCP 工具，可被
Codex / Claude Desktop / Cursor / DeepSeek Harness 等任何支持 MCP 的客户端挂载。

启动方式（由 MCP 客户端自动拉起，也可手动测试）：
    <venv>\\Scripts\\python.exe  server.py

依赖：pip install mcp   （install.ps1 会自动装好）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# stderr 用于日志（stdout 是 MCP 协议流，严禁写入非协议内容）
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    # mcp 2.x：FastMCP 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:
    try:
        # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _MCPServer
    except ImportError:
        _MCPServer = None

if _MCPServer is None:  # pragma: no cover
    sys.stderr.write(
        "[phone-control] 缺少 mcp 依赖。请先运行 install.ps1，"
        "或手动执行: pip install mcp\n"
    )
    sys.exit(1)

try:
    # 两个版本都提供 *Content 名称（v1: TextContent/ImageContent；v2 同）
    from mcp.types import ImageContent, TextContent
except ImportError:  # pragma: no cover
    from mcp.types import Image as ImageContent, Text as TextContent

import phone_control as pc

mcp = _MCPServer("phone-control")


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def _image(b64: str, mime: str) -> ImageContent:
    return ImageContent(type="image", data=b64, mimeType=mime)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _wrap(fn, *args, **kwargs) -> str:
    """统一异常处理：把 AdbError 变成 agent 可读的错误文本而不是崩溃。"""
    try:
        return fn(*args, **kwargs)
    except pc.AdbError as e:
        return f"[错误] {e}"
    except Exception as e:  # noqa: BLE001
        return f"[未预期错误] {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 设备与诊断
# ---------------------------------------------------------------------------

@mcp.tool()
def doctor() -> str:
    """自检：检查 adb 是否可用、设备连接与授权状态，并给出修复建议。遇到问题先调用这个。"""
    return _wrap(pc.doctor)


@mcp.tool()
def list_devices() -> str:
    """列出当前电脑可见的所有 Android 设备（序列号、状态、型号）。"""
    return _wrap(lambda: _json(pc.list_devices()))


@mcp.tool()
def device_info(serial: str = "") -> str:
    """获取手机综合信息：品牌型号、系统版本、分辨率、电量、屏幕亮灭、网络、存储、当前前台应用。"""
    return _wrap(lambda: _json(pc.device_info(serial)))


@mcp.tool()
def current_app(serial: str = "") -> str:
    """获取当前前台应用的包名与 Activity。"""
    return _wrap(lambda: _json(pc.current_app(serial)))


# ---------------------------------------------------------------------------
# 看屏幕
# ---------------------------------------------------------------------------

@mcp.tool()
def screenshot(save_path: str = "", serial: str = "") -> list:
    """截取手机当前屏幕（PNG）。返回图片本体 + 保存路径。做任何点击操作前先截图确认界面。"""
    try:
        r = pc.screenshot(save_path, serial)
    except pc.AdbError as e:
        return [_text(f"[错误] {e}")]
    except Exception as e:  # noqa: BLE001
        return [_text(f"[未预期错误] {type(e).__name__}: {e}")]
    return [
        _text(f"截图已保存: {r['path']}（{r['bytes']} 字节）"),
        _image(r["png_base64"], "image/png"),
    ]


@mcp.tool()
def ui_dump(save_path: str = "", serial: str = "") -> str:
    """导出当前界面的 UI 控件层级 XML（含每个控件的文字、坐标、可点击性）。用于精确定位控件。"""
    def go():
        r = pc.ui_dump(serial, save_path)
        return f"已保存: {r['path']}\n\n" + r["xml"]
    return _wrap(go)


@mcp.tool()
def find_text(target: str, clickable_only: bool = False, exact: bool = False, serial: str = "") -> str:
    """在当前界面按文字查找控件（匹配 text 和 content-desc），返回中心坐标等信息。
    只查不点。要『找到并点击』请优先用 tap_text（一次调用完成，更快）。"""
    return _wrap(lambda: _json(pc.find_text(target, serial, clickable_only, exact)))


# ---------------------------------------------------------------------------
# 高级组合操作（一次调用完成多步，减少往返延迟）
# ---------------------------------------------------------------------------

@mcp.tool()
def tap_text(target: str, index: int = 0, exact: bool = False, wait_timeout: float = 0, serial: str = "") -> str:
    """按文字找到控件并点击——一步完成（等效 find_text+tap，但只需一次界面抓取）。
    点击文字按钮/图标/菜单项时首选本工具。wait_timeout>0 时先等待文字出现（界面加载慢时用）。
    index 指定点击第几个匹配（0 起）；exact=True 要求文字完全相等。"""
    return _wrap(lambda: _json(pc.tap_text(target, index, exact, wait_timeout, serial)))


@mcp.tool()
def wait_for_text(target: str, timeout_s: float = 15.0, exact: bool = False, serial: str = "") -> str:
    """等待界面上出现指定文字（替代盲目 sleep），出现后立即返回控件信息，超时返回错误。
    适合等待应用启动、页面加载、弹窗出现。"""
    return _wrap(lambda: _json(pc.wait_for_text(target, timeout_s, exact=exact, serial=serial)))


@mcp.tool()
def run_script(actions: str, stop_on_error: bool = True, serial: str = "") -> str:
    """批量执行一串手机动作——一次调用完成整个流程（显著降低多步操作延迟）。
    actions 为 JSON 数组字符串，每项含 action 字段，可用动作：
    tap{x,y} | tap_text{text,index?,exact?,wait_timeout?} | long_press{x,y,ms?} |
    swipe{x1,y1,x2,y2,ms?} | swipe_screen{direction,ms?} | key{key} | text{text} |
    sleep{ms} | wait_for{text,timeout_s?,exact?} | open{package} | stop{package} |
    current_app{} | assert_app{package} | screenshot{save_path?} | find{text,exact?} | shell{command,timeout?}
    示例: [{"action":"open","package":"com.tencent.mm"},{"action":"wait_for","text":"微信"},{"action":"screenshot"}]"""
    return _wrap(lambda: _json(pc.run_script(actions, serial, stop_on_error)))


# ---------------------------------------------------------------------------
# 操作屏幕
# ---------------------------------------------------------------------------

@mcp.tool()
def tap(x: int, y: int, serial: str = "") -> str:
    """点击屏幕坐标 (x, y)。坐标可来自 screenshot 图片或 find_text 的返回值。"""
    return _wrap(lambda: pc.tap(x, y, serial))


@mcp.tool()
def long_press(x: int, y: int, duration_ms: int = 1000, serial: str = "") -> str:
    """在 (x, y) 长按指定毫秒数。"""
    return _wrap(lambda: pc.long_press(x, y, duration_ms, serial))


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, serial: str = "") -> str:
    """从 (x1,y1) 滑动到 (x2,y2)。"""
    return _wrap(lambda: pc.swipe(x1, y1, x2, y2, duration_ms, serial))


@mcp.tool()
def swipe_screen(direction: str = "up", duration_ms: int = 300, serial: str = "") -> str:
    """整屏滑动翻页。direction: up=看下方内容 / down=回到上方 / left / right。"""
    return _wrap(lambda: pc.swipe_screen(direction, duration_ms=duration_ms, serial=serial))


@mcp.tool()
def press_key(key: str, serial: str = "") -> str:
    """按物理/系统按键。key 支持: back, home, recent, power, volume_up, volume_down,
    volume_mute, enter, del, menu, camera, tab, space, esc, search, dpad_up/down/left/right/center,
    wakeup, sleep, call, end_call, screenshot 等，或数字键码，或 KEYCODE_* 全名。"""
    return _wrap(lambda: pc.press_key(key, serial))


@mcp.tool()
def screen_on(serial: str = "") -> str:
    """点亮手机屏幕并尝试解除无密码锁屏。"""
    return _wrap(lambda: pc.screen_on(serial))


@mcp.tool()
def input_text(text: str, serial: str = "") -> str:
    """向当前焦点输入框输入文字（先用 tap 聚焦输入框）。
    中文等非 ASCII 文字需要手机安装 ADBKeyboard 输入法（工具会自动切换并复原）；
    未安装时返回安装指引。"""
    return _wrap(lambda: pc.input_text(text, serial))


# ---------------------------------------------------------------------------
# 应用管理
# ---------------------------------------------------------------------------

@mcp.tool()
def open_app(package: str, serial: str = "") -> str:
    """启动应用。package 为包名，如 com.tencent.mm（微信）。不确定包名时先用 list_apps。"""
    return _wrap(lambda: pc.open_app(package, serial))


@mcp.tool()
def stop_app(package: str, serial: str = "") -> str:
    """强制停止应用。"""
    return _wrap(lambda: pc.stop_app(package, serial))


@mcp.tool()
def restart_app(package: str, serial: str = "") -> str:
    """重启应用（强制停止后重新打开）。"""
    return _wrap(lambda: pc.restart_app(package, serial))


@mcp.tool()
def list_apps(system: bool = False, contains: str = "", serial: str = "") -> str:
    """列出已安装应用的包名。system=True 时包含系统应用；contains 按子串过滤（如 contains='tencent'）。"""
    return _wrap(lambda: _json(pc.list_apps(system, contains, serial)))


@mcp.tool()
def install_app(apk_path: str, allow_downgrade: bool = False, serial: str = "") -> str:
    """把电脑上的 APK 文件安装到手机（覆盖安装）。apk_path 是电脑本地路径。"""
    return _wrap(lambda: pc.install_app(apk_path, allow_downgrade, serial))


@mcp.tool()
def uninstall_app(package: str, keep_data: bool = False, serial: str = "") -> str:
    """卸载应用。keep_data=True 时保留应用数据。危险操作，请先向用户确认。"""
    return _wrap(lambda: pc.uninstall_app(package, keep_data, serial))


# ---------------------------------------------------------------------------
# 文件传输
# ---------------------------------------------------------------------------

@mcp.tool()
def push_file(local_path: str, remote_path: str = "/sdcard/Download/", serial: str = "") -> str:
    """把电脑上的文件或目录推送到手机。remote_path 以 / 结尾视为目录。"""
    return _wrap(lambda: pc.push_file(local_path, remote_path, serial))


@mcp.tool()
def pull_file(remote_path: str, local_path: str = ".", serial: str = "") -> str:
    """把手机上的文件或目录拉取到电脑。如 /sdcard/DCIM/Camera/xxx.jpg。"""
    return _wrap(lambda: _json(pc.pull_file(remote_path, local_path, serial)))


# ---------------------------------------------------------------------------
# 通用 shell
# ---------------------------------------------------------------------------

@mcp.tool()
def device_shell(command: str, timeout: int = 60, serial: str = "") -> str:
    """在手机上执行任意 adb shell 命令并返回 stdout/stderr。
    示例: 'ls /sdcard/Download'、'dumpsys notification'、'settings get system screen_brightness'。"""
    return _wrap(lambda: _json(pc.device_shell(command, serial, timeout)))


# ---------------------------------------------------------------------------
# 无线调试（Wi-Fi ADB，Android 11+）
# ---------------------------------------------------------------------------

@mcp.tool()
def wireless_pair(address: str, pairing_code: str) -> str:
    """无线调试配对（仅首次需要）：address 为手机『无线调试→使用配对码配对设备』页显示的 ip:配对端口，
    pairing_code 为该页显示的 6 位配对码。"""
    return _wrap(lambda: pc.wireless_pair(address, pairing_code))


@mcp.tool()
def wireless_connect(address: str) -> str:
    """无线调试连接：address 为手机『无线调试』主页显示的 ip:连接端口（不是配对端口）。
    连接成功后即可拔掉数据线，所有工具经 Wi-Fi 使用。"""
    return _wrap(lambda: pc.wireless_connect(address))


def _start_keepalive(interval_s: float = 45.0) -> None:
    """保活：周期性在设备上执行无害命令，防止 Windows USB 选择性暂停 /
    链路空闲导致的设备掉线。设环境变量 PHONE_CONTROL_KEEPALIVE=0 可关闭。"""
    if os.environ.get("PHONE_CONTROL_KEEPALIVE", "1") == "0":
        return
    import threading
    import time as _time

    def _loop() -> None:
        while True:
            _time.sleep(interval_s)
            try:
                pc.run_shell("true", timeout=15)
            except Exception:  # noqa: BLE001
                pass  # 设备暂时不在时忽略，下一轮自动重试（会话会自动重建）

    threading.Thread(target=_loop, daemon=True, name="phone-control-keepalive").start()


def main() -> None:
    # stdio 传输：由 MCP 客户端拉起；日志一律走 stderr，避免污染 stdout 协议流
    _start_keepalive()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
