# -*- coding: utf-8 -*-
"""
test_mcp.py —— MCP 端到端冒烟测试
用 mcp 官方客户端通过 stdio 拉起 server.py，验证：握手、工具列表、doctor、list_devices、device_info。
运行：.venv\\Scripts\\python.exe test_mcp.py
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Windows 控制台默认可能是 cp1252/GBK，强制 UTF-8，避免中文日志导致 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    # 注意：微软商店版 Python 建的 venv 中 sys.executable 可能指向基础解释器（无 mcp），
    # 因此优先显式使用项目 .venv 里的 python 拉起 server。
    venv_py = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    if venv_py.exists():
        py = str(venv_py)
    else:
        py = os.environ.get("__PYVENV_LAUNCHER__") or sys.executable
    print(f"[i] 使用解释器: {py}")
    params = StdioServerParameters(
        command=py,
        args=[str(ROOT / "server.py")],
        cwd=str(ROOT),
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            # v1: serverInfo / v2: server_info
            sinfo = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            print(f"[OK] 握手成功，服务器: {getattr(sinfo, 'name', '?')} v{getattr(sinfo, 'version', '?')}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[OK] 工具数量: {len(names)}")
            print("     " + ", ".join(names))

            expect = {
                "doctor", "list_devices", "device_info", "current_app",
                "screenshot", "ui_dump", "find_text",
                "tap", "long_press", "swipe", "swipe_screen", "press_key",
                "screen_on", "input_text",
                "tap_text", "wait_for_text", "run_script",
                "open_app", "stop_app", "restart_app", "list_apps",
                "install_app", "uninstall_app",
                "push_file", "pull_file", "device_shell",
                "wireless_pair", "wireless_connect",
            }
            missing = expect - set(names)
            if missing:
                print(f"[X] 缺少工具: {sorted(missing)}")
                return 1
            print("[OK] 预期工具全部注册")

            res = await session.call_tool("doctor", {})
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            print(f"[OK] doctor 返回:\n{text}")

            res = await session.call_tool("list_devices", {})
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            print(f"[OK] list_devices 返回: {text.strip()}")

            # 若有在线设备，进一步测 device_info + screenshot
            import json as _json
            try:
                devs = _json.loads(text)
            except Exception:
                devs = []
            online = [d for d in devs if d.get("state") == "device"]
            if online:
                res = await session.call_tool("device_info", {})
                text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                print(f"[OK] device_info 返回:\n{text}")
                res = await session.call_tool("screenshot", {})
                kinds = [getattr(c, "type", "?") for c in res.content]
                texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
                print(f"[OK] screenshot 返回内容块: {kinds} 文本: {texts}")
            else:
                print("[i] 无在线设备，跳过 device_info / screenshot 实测（属正常，插上手机后可重测）")

            print("\n=== 冒烟测试通过 ===")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
