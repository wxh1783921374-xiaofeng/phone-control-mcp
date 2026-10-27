# -*- coding: utf-8 -*-
"""
regression.py —— 真机集成回归测试（需要已连接并授权的设备）
覆盖：持久 shell 会话、serial 缓存、device_info 合并采集、UI 单次往返、
tap_text / wait_for_text / run_script、错误路径、回退机制、截图。
运行：python regression.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import phone_control as pc  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def expect_error(name: str, fn, msg_contains: str) -> None:
    try:
        fn()
        check(name, False, "预期抛出 AdbError 但没有")
    except pc.AdbError as e:
        check(name, msg_contains in str(e), f"err={str(e)[:60]}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"异常类型错误: {type(e).__name__}: {e}")


def main() -> int:
    print("=" * 70)
    print("phone-control 真机回归测试")
    print("=" * 70)

    s = pc._resolve_serial("")
    print(f"设备: {s}\n")

    # 测试期间开启勿扰，减少通知弹窗干扰（best effort，结束恢复）
    zen_before = ""
    try:
        zen_before = pc.run_shell("settings get global zen_mode", s).stdout.strip()
        pc.run_shell("settings put global zen_mode 1", s)
    except Exception:  # noqa: BLE001
        pass
    try:
        return _run_all(s)
    finally:
        try:
            pc.run_shell(f"settings put global zen_mode {zen_before or '0'}", s)
            pc.press_key("home")
        except Exception:  # noqa: BLE001
            pass


def _run_all(s: str) -> int:

    print("[1] 持久 shell 会话")
    t0 = time.perf_counter()
    r = pc.run_shell("echo PC_OK", s)
    t_first = time.perf_counter() - t0
    check("echo 输出正确", "PC_OK" in r.stdout, r.stdout.strip()[:40])
    check("returncode=0", r.returncode == 0)
    r2 = pc.run_shell("false", s)
    check("非零退出码传递 (false→rc!=0)", r2.returncode != 0, f"rc={r2.returncode}")
    ts = []
    for i in range(5):
        t0 = time.perf_counter()
        pc.run_shell(f"echo N{i}", s)
        ts.append(time.perf_counter() - t0)
    avg_ms = sum(ts) / len(ts) * 1000
    check("连续 5 条命令会话复用", avg_ms < 120, f"avg {avg_ms:.0f}ms/条")
    r3 = pc.run_shell("echo A\necho B", s)  # 多行 → 应自动回退单次模式
    check("多行命令自动回退单次模式", "A" in r3.stdout and "B" in r3.stdout)
    r4 = pc.run_shell("echo AFTER_FALLBACK", s)
    check("回退后会话仍可用", "AFTER_FALLBACK" in r4.stdout)
    # 读 stdin 的命令不得挂起会话（</dev/null 隔离）
    t0 = time.perf_counter()
    r5 = pc.run_shell("cat", s, timeout=8)
    check("裸 cat（读 stdin）立即返回不挂起", time.perf_counter() - t0 < 5, f"rc={r5.returncode}")
    r6 = pc.run_shell("echo STILL_ALIVE", s)
    check("之后会话依旧健康", "STILL_ALIVE" in r6.stdout)
    print(f"  (info) 首条命令含会话建立: {t_first*1000:.0f}ms\n")

    print("[2] serial 缓存")
    t0 = time.perf_counter()
    for _ in range(5):
        pc._resolve_serial("")
    cached_ms = (time.perf_counter() - t0) * 1000 / 5
    check("缓存命中 <5ms/次", cached_ms < 5, f"{cached_ms:.2f}ms")
    pc._invalidate_serial_cache()
    s2 = pc._resolve_serial("")
    check("失效后可重新解析", s2 == s, s2)
    print()

    print("[3] device_info 合并采集")
    t0 = time.perf_counter()
    info = pc.device_info()
    dt = (time.perf_counter() - t0) * 1000
    check("brand=vivo", info.get("brand") == "vivo", str(info.get("brand")))
    check("model 非空", bool(info.get("model")), str(info.get("model")))
    check("分辨率", "screen_width" in info, str(info.get("screen")))
    check("电量百分比", isinstance(info.get("battery", {}).get("level_percent"), int),
          str(info.get("battery", {}).get("level_percent")))
    check("屏幕状态", info.get("screen_state") in ("Awake", "Dozing"), str(info.get("screen_state")))
    check("current_app 为 dict", isinstance(info.get("current_app"), dict))
    check(f"耗时 {dt:.0f}ms < 700ms（基线 1060ms）", dt < 700, f"{dt:.0f}ms")
    print()

    print("[4] UI 单次往返 + find_text")
    t0 = time.perf_counter()
    xml = pc._fetch_ui_xml(s)
    dt = (time.perf_counter() - t0) * 1000
    check("XML 含 hierarchy", "<hierarchy" in xml[:2000])
    check("XML 完整闭合", xml.rstrip().endswith("</hierarchy>"))
    print(f"  (info) UI 抓取 {dt:.0f}ms")
    labels = []
    root_xml = xml
    import xml.etree.ElementTree as ET
    for n in ET.fromstring(root_xml).iter("node"):
        t = (n.get("text") or "").strip()
        if t and t != "null":
            labels.append(t)
    check("界面有文字标签", len(labels) > 0, f"{len(labels)} 个")
    label = labels[0] if labels else ""
    print(f"  (info) 首个标签: {label!r}")
    hits = pc.find_text(label) if label else []
    check("find_text 命中已知标签", len(hits) > 0, f"{len(hits)} hits")
    if hits:
        h = hits[0]
        check("坐标合法", 0 <= h["center_x"] <= info.get("screen_width", 1080)
              and 0 <= h["center_y"] <= info.get("screen_height", 2376), str(h["bounds"]))
    check("find_text 未命中返回空列表", pc.find_text("__绝无此文字__") == [])
    print()

    print("[5] 打开设置 → wait_for_text / tap_text")

    def reopen_settings():
        pc.open_app("com.android.settings")
        return pc.wait_for_text("设置", timeout_s=10)

    node = reopen_settings()
    check("wait_for_text('设置') 命中", node.get("center_x") is not None, str(node.get("text")))
    cur = pc.current_app()
    check("前台=设置", cur.get("package") == "com.android.settings", str(cur))
    # 环境干扰重试：日常用手机的通知弹窗可能抢走点击（悬浮通知正好盖在标题区）
    stayed, r = None, {}
    for _attempt in range(3):
        r = pc.tap_text("设置", index=0, exact=True)
        time.sleep(1.0)
        stayed = pc.current_app()
        if stayed.get("package") == "com.android.settings":
            break
        pc.press_key("home")
        time.sleep(0.5)
        reopen_settings()
    check("tap_text 返回 tapped", "tapped" in r, str(r.get("tapped", {}).get("text")))
    check("点击后仍在设置内（含通知干扰重试）",
          (stayed or {}).get("package") == "com.android.settings", str(stayed))
    expect_error("wait_for_text 超时报错",
                 lambda: pc.wait_for_text("__绝不会出现__", timeout_s=2, interval_s=0.7),
                 "等待超时")
    expect_error("tap_text 未命中报错含建议",
                 lambda: pc.tap_text("__绝无此控件__"), "screenshot")
    print()

    print("[6] run_script 组合流程（自带 open/wait，不依赖前序状态）")
    shot = str(ROOT / "screenshots" / "regression_script.png")
    flow = [
        {"action": "open", "package": "com.android.settings"},
        {"action": "wait_for", "text": "设置", "timeout_s": 10},
        {"action": "current_app"},
        {"action": "find", "text": "设置"},
        {"action": "sleep", "ms": 150},
        {"action": "screenshot", "save_path": shot},
        {"action": "assert_app", "package": "com.android.settings"},
        {"action": "shell", "command": "echo IN_SCRIPT"},
    ]
    res = pc.run_script(flow)
    check("8 步全部 ok", len(res) == 8 and all(x["ok"] for x in res),
          str([(x["step"], x["ok"]) for x in res]))
    check("shell 步骤输出正确", "IN_SCRIPT" in str(res[-1].get("result", "")) if res else False)
    png = Path(shot)
    check("script 截图为有效 PNG", png.is_file() and png.read_bytes()[:4] == b"\x89PNG")
    # 错误路径：assert 失败即停
    res2 = pc.run_script([
        {"action": "assert_app", "package": "com.fake.notexist"},
        {"action": "key", "key": "home"},
    ])
    check("遇错即停（只有 1 步）", len(res2) == 1 and res2[0]["ok"] is False, str(len(res2)))
    res3 = pc.run_script([
        {"action": "assert_app", "package": "com.fake.notexist"},
        {"action": "key", "key": "home"},
    ], stop_on_error=False)
    check("continue 模式跑完 2 步", len(res3) == 2 and res3[1].get("ok") is True, str(res3))
    res4 = pc.run_script('[{"action":"fly"}]')
    check("未知 action 报错", res4[0]["ok"] is False and "未知" in res4[0]["error"])
    expect_error("非法 JSON 报错", lambda: pc.run_script("{bad json"), "JSON")
    print()

    print("[7] 截图与按键")
    r = pc.screenshot(str(ROOT / "screenshots" / "regression.png"))
    data = Path(r["path"]).read_bytes()
    check("PNG 魔数", data[:4] == b"\x89PNG", f"{r['bytes']} bytes")
    check("base64 非空", len(r["png_base64"]) > 1000)
    check("press_key home", pc.press_key("home") == "ok")
    time.sleep(0.8)
    cur = pc.current_app()
    check("已回到桌面", "launcher" in (cur.get("package") or ""), str(cur))
    print()

    print("=" * 70)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
