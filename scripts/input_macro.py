#!/usr/bin/env python3
"""外部模拟输入：让三次录制的**游戏内操作尽量一致**。

为什么需要它：复现率的前提是「三次跑同一件事」。人手操作必然有偏差——走多久、
打几下、何时脱战——这些偏差会污染结论。用脚本发同一串按键、同样的时长，
三次的刺激就一致了，测出来的差异才归因于被测对象而不是操作。

实现只用标准库 `ctypes` 调 Windows 的 SendInput（与真人按键走同一条输入路径），
**不读取游戏内存、不注入进程、不修改游戏文件**，并且用扫描码（scan code）发送，
对读原始输入的游戏兼容性更好。

风险须知（请自己决定是否使用）
------------------------------
任何自动化输入都可能被游戏反作弊判定为违规，原神的反作弊较为激进。
本脚本提供了三重克制：`--dry-run` 先看计划、`--max-seconds` 限制总时长、
运行中随时按 **Esc** 中止；不设计长时间无人值守的循环。**账号风险由使用者承担。**

用法
----
    python scripts\\input_macro.py list                      # 看有哪些预设序列
    python scripts\\input_macro.py run bug_03_combat --dry-run   # 只看计划，不发按键
    python scripts\\input_macro.py run bug_03_combat --countdown 5

配合录制：`session_runner.py auto --case bug_03 --macro bug_03_combat`
（先开始录制，再按同一条序列操作，最后由动作日志自动填出关键时间码。）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]

# 扫描码：与真人按键相同路径，游戏更容易识别
SCANCODES = {
    "W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20,
    "SPACE": 0x39, "SHIFT": 0x2A, "ESC": 0x01, "TAB": 0x0F,
    "E": 0x12, "Q": 0x10, "R": 0x13, "F": 0x21,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
}
INPUT_KEYBOARD, INPUT_MOUSE = 1, 0
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
VK_ESCAPE = 0x1B


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def send_key(name: str, hold_s: float = 0.06, user32: Any | None = None) -> None:
    """按一下键（按下 → 保持 hold_s → 松开）。未知键名直接报错，避免静默发错键。"""
    code = SCANCODES.get(name.upper())
    if code is None:
        raise ValueError(f"未知按键：{name}（可用：{', '.join(sorted(SCANCODES))}）")
    api = user32 or _user32()
    for flags in (KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP):
        item = _INPUT(type=INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None)
        api.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
        if not flags & KEYEVENTF_KEYUP:
            time.sleep(hold_s)


def send_click(button: str = "left", user32: Any | None = None) -> None:
    """点一下鼠标左键（原神普攻）。"""
    if button != "left":
        raise ValueError("目前只支持 left（普攻）")
    api = user32 or _user32()
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        item = _INPUT(type=INPUT_MOUSE)
        item.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=None)
        api.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))


def escape_pressed(user32: Any | None = None) -> bool:
    api = user32 or _user32()
    return bool(api.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


# 预设序列：每步是 (动作, 参数...)，动作取 wait / key / click / mark
# 设计原则：只做「可重复的机械动作」，把需要判断的部分留给人在报告里写。
SCENARIOS: dict[str, dict[str, Any]] = {
    "bug_03_combat": {
        "desc": "稳定探索音乐 → 靠近一个敌人 → 普攻 4 下 → 闪避 → 秒杀 → 脱战。三段用同一序列。",
        "steps": [
            ("mark", "explore_stable"),
            ("wait", 3.0),
            ("key", "W", 1.2),
            ("wait", 0.5),
            ("mark", "combat_start"),
            ("click", "left"), ("wait", 0.35), ("click", "left"), ("wait", 0.35),
            ("click", "left"), ("wait", 0.35), ("click", "left"),
            ("mark", "combat_music_pending"),
            ("wait", 1.5),
            ("key", "SPACE", 0.1),          # 闪避
            ("wait", 0.6),
            ("key", "E", 0.1),              # 元素战技补伤害
            ("wait", 1.2),
            ("key", "Q", 0.1),              # 元素爆发
            ("wait", 2.0),
            ("mark", "combat_end"),
            ("key", "S", 1.0),              # 后撤退战
            ("wait", 3.0),
            ("mark", "explore_resume"),
        ],
    },
    "bug_01_concurrency": {
        "desc": "一次吸引多个敌人后 5 秒内连招：战技 → 爆发 → 冲刺 → 受击 → 击杀。",
        "steps": [
            ("mark", "ambient_baseline"),
            ("wait", 3.0),
            ("key", "W", 2.0),              # 冲进营地一次拉多个
            ("wait", 1.0),
            ("mark", "combat_start"),
            ("key", "E", 0.1), ("wait", 0.4),
            ("key", "Q", 0.1), ("wait", 0.4),
            ("key", "SPACE", 0.1), ("wait", 0.4),
            ("click", "left"), ("wait", 0.3), ("click", "left"), ("wait", 0.3),
            ("click", "left"),
            ("mark", "burst_window_end"),
            ("wait", 3.0),
            ("mark", "combat_end"),
        ],
    },
    "walk_steps": {
        "desc": "固定步数的走/跑，用于脚步与材质类用例（步数一致才好比较）。",
        "steps": [
            ("mark", "record_start"),
            ("wait", 2.0),
            ("key", "W", 2.0),              # 走 5 步
            ("wait", 1.0),
            ("key", "SHIFT", 0.05), ("key", "W", 2.0),   # 跑 5 步
            ("wait", 1.0),
            ("mark", "surface_boundary"),
            ("key", "W", 2.0),
            ("wait", 2.0),
            ("mark", "record_end"),
        ],
    },
}


def total_seconds(scenario: dict[str, Any]) -> float:
    """估算序列时长（等待 + 按键保持 + 每次点击 0.1s），用于 max-seconds 校验。"""
    total = 0.0
    for step in scenario["steps"]:
        kind = step[0]
        if kind == "wait":
            total += float(step[1])
        elif kind == "key":
            total += float(step[2]) + 0.08
        elif kind == "click":
            total += 0.1
    return round(total, 2)


def run_scenario(name: str, countdown: float, dry_run: bool, max_seconds: float,
                 quiet: bool = False) -> dict[str, Any]:
    """执行序列，返回动作日志（含每步的绝对时间与打点时间码）。"""
    if name not in SCENARIOS:
        raise KeyError(f"未知序列：{name}（可用：{', '.join(SCENARIOS)}）")
    scenario = SCENARIOS[name]
    estimate = total_seconds(scenario)
    if estimate > max_seconds:
        raise ValueError(f"该序列约需 {estimate}s，超过上限 {max_seconds}s；"
                         f"如确认要跑请调大 --max-seconds")

    log: list[dict[str, Any]] = []
    marks: dict[str, float] = {}
    if dry_run:
        cursor = 0.0
        for step in scenario["steps"]:
            kind = step[0]
            log.append({"at": round(cursor, 3), "action": " ".join(str(part) for part in step)})
            if kind == "mark":
                marks[step[1]] = round(cursor, 3)
            elif kind == "wait":
                cursor += float(step[1])
            elif kind == "key":
                cursor += float(step[2]) + 0.08
            elif kind == "click":
                cursor += 0.1
        return {"scenario": name, "dry_run": True, "estimate_s": estimate,
                "log": log, "marks": marks, "aborted": False}

    user32 = _user32()
    for remaining in range(int(countdown), 0, -1):
        if not quiet:
            print(f"  {remaining} 秒后开始…（Esc 可中止）", flush=True)
        time.sleep(1.0)

    started = time.monotonic()
    aborted = False
    for step in scenario["steps"]:
        if escape_pressed(user32):
            aborted = True
            break
        kind = step[0]
        at = round(time.monotonic() - started, 3)
        if kind == "wait":
            time.sleep(float(step[1]))
            continue
        if kind == "mark":
            marks[step[1]] = at
            log.append({"at": at, "action": f"mark {step[1]}"})
            if not quiet:
                print(f"  ◆ {at:7.3f}s  {step[1]}", flush=True)
            continue
        if kind == "key":
            send_key(str(step[1]), float(step[2]), user32)
            log.append({"at": at, "action": f"key {step[1]} hold {step[2]}s"})
        elif kind == "click":
            send_click(str(step[1]), user32)
            log.append({"at": at, "action": f"click {step[1]}"})
        if time.monotonic() - started > max_seconds:
            aborted = True
            break
    return {"scenario": name, "dry_run": False, "estimate_s": estimate,
            "elapsed_s": round(time.monotonic() - started, 2),
            "log": log, "marks": marks, "aborted": aborted}


def write_log(result: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="input_macro", description="外部模拟输入序列（让三次操作一致）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出预设序列")
    run = sub.add_parser("run", help="执行序列")
    run.add_argument("scenario")
    run.add_argument("--countdown", type=float, default=5.0, help="开始前等待秒数（切回游戏用）")
    run.add_argument("--dry-run", action="store_true", help="只打印计划，不发按键")
    run.add_argument("--max-seconds", type=float, default=120.0, help="总时长上限，超出即中止")
    run.add_argument("--log", type=Path, help="动作日志输出路径（JSON）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        for name, scenario in SCENARIOS.items():
            print(f"{name:22s} 约 {total_seconds(scenario):5.1f}s  {scenario['desc']}")
        return 0
    try:
        result = run_scenario(args.scenario, args.countdown, args.dry_run, args.max_seconds)
    except (KeyError, ValueError) as exc:
        print(f"无法执行：{exc}", file=sys.stderr)
        return 2
    if args.log:
        write_log(result, args.log)
        print(f"动作日志：{args.log}")
    print(json.dumps({k: v for k, v in result.items() if k != "log"}, ensure_ascii=False))
    if not args.dry_run:
        print("提示：本次动作序列与时间码已记录，报告里应写明「操作为脚本模拟，三次序列一致」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
