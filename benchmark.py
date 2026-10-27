# -*- coding: utf-8 -*-
"""
benchmark.py —— 真机延迟基准测试
在已连接设备上量化各操作耗时，用于优化前后对比。
运行：python benchmark.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import phone_control as pc  # noqa: E402


def bench(name: str, fn, n: int = 3) -> float:
    ts = []
    last = None
    for _ in range(n):
        t0 = time.perf_counter()
        last = fn()
        ts.append(time.perf_counter() - t0)
    avg = sum(ts) / len(ts)
    print(f"{name:28s} x{n}  avg {avg*1000:8.0f} ms   min {min(ts)*1000:8.0f}   max {max(ts)*1000:8.0f}")
    return avg


def main() -> int:
    print("=" * 78)
    print("phone-control 基准测试（真机）")
    print("=" * 78)

    s = pc._resolve_serial("")
    print(f"设备: {s}")
    print(f"型号: {pc.run_shell('getprop ro.product.model', s).stdout.strip()}")
    print("-" * 78)

    results = {}
    # 纯 shell 往返（adb 进程 + 设备 sh 开销）
    results["shell_roundtrip"] = bench("shell 'true'", lambda: pc.run_shell("true", s), 5)
    # serial 解析开销（adb devices -l）
    results["resolve_serial"] = bench("_resolve_serial(缓存外)", lambda: pc.list_devices(), 5)
    # 按键注入延迟（无副作用的 tap 状态栏角落）
    results["tap"] = bench("tap(20,20) 状态栏角落", lambda: pc.tap(20, 20, ""), 5)
    # 前台应用检测
    results["current_app"] = bench("current_app", lambda: pc.current_app(), 3)
    # UI 树获取 + 解析
    results["ui_dump"] = bench("ui_dump", lambda: pc.ui_dump(), 3)
    results["find_text_miss"] = bench("find_text(不存在文字)", lambda: pc.find_text("zz_不存在_xyz"), 3)
    # 截图
    results["screenshot"] = bench("screenshot", lambda: pc.screenshot(), 3)
    # 综合信息（~10 次 shell 调用）
    results["device_info"] = bench("device_info", lambda: pc.device_info(), 2)

    print("-" * 78)
    total = sum(results.values())
    print(f"合计（每样一次）: {total*1000:.0f} ms")

    # 收尾：确认状态栏没被拉下来，回到桌面
    pc.press_key("back")
    time.sleep(0.3)
    pc.press_key("home")
    return 0


if __name__ == "__main__":
    sys.exit(main())
