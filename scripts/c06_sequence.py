#!/usr/bin/env python3
"""C-06 一键取证：**一次提权、一条时间线、六步设备操作**。

为什么要把三件事合成一条脚本
------------------------------
「切输出设备 / 拔插耳机 / 蓝牙断连重连」的权限要求完全不同，但它们必须在**同一条录像
时间线**上，否则三段素材的时间码互相对不上、也没法算复现率。所以本脚本自己用 UAC
提权重启（点一次「是」），然后在同一个进程里按计划依次执行，每一步的前后系统状态都
落盘。

三件事各自做了什么（报告里必须照实写，不能含糊）
-------------------------------------------------
| 脚本动作 | 等价于 | 权限 | 报告里该怎么写 |
| --- | --- | --- | --- |
| 切默认输出设备 | 在「声音」设置里改默认设备 | 不需要 | 切换默认输出设备 |
| 禁用 / 启用音频端点 | **拔 / 插**的软件等价物 | 需管理员 | 设备级禁用/启用（**模拟**拔插） |
| 禁用 / 启用蓝牙无线电 | 蓝牙断连 / 重连 | 需管理员 | 蓝牙无线电禁用/启用 |

后两行**不是物理拔插**：3.5mm/USB 插孔没有可供脚本操纵的热插拔事件，能屏蔽的只有端点
设备本身。产出文件里这段说明是固定写入的，不允许被读成「我拔了线」。

默认时间表（秒数都是**录像内相对秒数**，与 measure.json 的时间码同一坐标系）
----------------------------------------------------------------------------
    8.0   switch_away   切到替代输出设备
   16.0   switch_back   切回耳机
   24.0   unplug        模拟拔出耳机（禁用音频端点）
   32.0   replug        插回耳机（启用音频端点）
   42.0   bt_down       蓝牙无线电断连
   52.0   bt_up         蓝牙无线电重连
   62.0   bt_observe    观察蓝牙耳机是否自动重连
   70.0   restore       默认输出设备复位到耳机
   76.0   停止录制

用法
----
    python scripts\\c06_sequence.py list                 # 看端点与 PnP 映射（不需要管理员）
    python scripts\\c06_sequence.py plan                 # 只打印时间表，不动系统
    python scripts\\c06_sequence.py run --takes 3        # 提权后连录 3 段，全程自动
    python scripts\\c06_sequence.py run --skip-bt        # 蓝牙耳机没配对时跳过蓝牙三步
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import POINTER, Structure, byref, c_void_p, wintypes
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import audio_devices  # noqa: E402

# ---------------------------------------------------------------- 时间表（纯数据）

# (步骤 id, 动作类型, 人话说明, 该步之后留多少秒)
DEFAULT_STEPS: tuple[tuple[str, str, str, float], ...] = (
    ("switch_away", "switch_away", "切到替代输出设备", 8.0),
    ("switch_back", "switch_back", "切回耳机（默认设备回位）", 8.0),
    ("unplug", "unplug", "模拟拔出耳机：禁用耳机音频端点", 8.0),
    ("replug", "replug", "模拟插回耳机：重新启用音频端点", 10.0),
    ("bt_down", "bt_down", "蓝牙无线电断连", 10.0),
    ("bt_up", "bt_up", "蓝牙无线电重连", 10.0),
    ("bt_observe", "observe_bt", "观察蓝牙耳机是否自动重连", 8.0),
    ("restore", "restore", "默认输出设备复位到耳机", 6.0),
)

SWITCH_KINDS = ("switch_away", "switch_back")
UNPLUG_KINDS = ("unplug", "replug")
BT_KINDS = ("bt_down", "bt_up", "observe_bt")


def select_steps(steps: Sequence[tuple[str, str, str, float]],
                 do_switch: bool = True, do_unplug: bool = True,
                 do_bt: bool = True) -> list[tuple[str, str, str, float]]:
    """按开关裁剪时间表。纯函数：不做任何系统调用，便于测试。"""
    kept = []
    for entry in steps:
        kind = entry[1]
        if kind in SWITCH_KINDS and not do_switch:
            continue
        if kind in UNPLUG_KINDS and not do_unplug:
            continue
        if kind in BT_KINDS and not do_bt:
            continue
        kept.append(entry)
    return kept


def build_plan(lead_in: float, steps: Sequence[tuple[str, str, str, float]] = DEFAULT_STEPS,
               scale: float = 1.0) -> list[dict[str, Any]]:
    """把时间表摊成带绝对秒数的剧本。纯函数。"""
    plan: list[dict[str, Any]] = []
    cursor = float(lead_in)
    for step_id, kind, label, dwell in steps:
        plan.append({"step": step_id, "kind": kind, "label": label,
                     "at": round(cursor, 3), "dwell": round(dwell * scale, 3)})
        cursor += dwell * scale
    return plan


def schedule_end(plan: Sequence[dict[str, Any]], tail: float = 5.0) -> float:
    """最后一步之后再过多久停录。纯函数。"""
    if not plan:
        return round(tail, 3)
    return round(plan[-1]["at"] + plan[-1]["dwell"] + tail, 3)


def marks_from_records(records: Sequence[dict[str, Any]]) -> dict[str, float]:
    """打点用「生效时刻」而不是「命令发出时刻」——证据关心的是状态真的变了没有。"""
    return {str(item["step"]): float(item["effective_at"]) for item in records}


# ---------------------------------------------------------------- PowerShell / PnP


def ps_quote(value: str) -> str:
    """PowerShell 单引号字符串里的单引号要写成两个。设备名里出现过，必须转义。"""
    return value.replace("'", "''")


def pnp_command(script: str) -> list[str]:
    """统一走 Windows PowerShell：PnpDevice 模块在 5.1 与 7 都有，powershell.exe 必然存在。

    前缀是**必须的**：中文 Windows 上 powershell.exe 输出到管道默认走 OEM 代码页（936），
    而 Python 按 UTF-8 解码，设备名会整片变成问号方块（实测踩过）。改 Console.OutputEncoding
    之后管道里才是 UTF-8。
    """
    prefix = "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + script]


def run_ps(script: str, timeout: float = 60.0) -> tuple[int, str]:
    done = subprocess.run(pnp_command(script), capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    text = (done.stdout or "").strip() or (done.stderr or "").strip()
    return done.returncode, text


def find_pnp_script(class_name: str, name: str) -> str:
    """按 FriendlyName 精确定位 PnP 设备，返回 InstanceId（找不到输出空，不报错）。"""
    return (
        f"$d = Get-PnpDevice -Class {class_name} -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.FriendlyName -eq '{ps_quote(name)}' }} | Select-Object -First 1; "
        f"if ($d) {{ $d.InstanceId }} else {{ '' }}"
    )


def find_bt_radio_script() -> str:
    """蓝牙无线电＝状态 OK 的蓝牙适配器；枚举器/RFCOMM 那些不是无线电，必须排掉。"""
    return (
        "$d = Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'OK' -and $_.FriendlyName -notmatch "
        "'Enumerator|RFCOMM|TDI|枚举器' } | Select-Object -First 1; "
        "if ($d) { $d.InstanceId } else { '' }"
    )


def pnp_action_script(action: str, instance_id: str) -> str:
    verb = "Disable" if action == "disable" else "Enable"
    quoted = ps_quote(instance_id)
    return (f"$ErrorActionPreference='Stop'; "
            f"{verb}-PnpDevice -InstanceId '{quoted}' -Confirm:$false | Out-Null; "
            f"(Get-PnpDevice -InstanceId '{quoted}').Status")


def pnp_status_script(instance_id: str) -> str:
    return (
        f"$d = Get-PnpDevice -InstanceId '{ps_quote(instance_id)}' -ErrorAction SilentlyContinue; "
        f"if ($d) {{ $d.Status }} else {{ 'MISSING' }}"
    )


def pnp_fallback_script(action: str, instance_id: str) -> str:
    """第二条路：pnputil。

    实测蓝牙无线电上 `Disable-PnpDevice` 会返回「常规故障」（带子设备的适配器常见），
    而 pnputil 走的是另一套接口。两条都试、并把**哪条成功**记进证据里。
    """
    verb = "/disable-device" if action == "disable" else "/enable-device"
    return f'pnputil {verb} "{instance_id}"'


def first_line(text: str, limit: int = 80) -> str:
    return (text.splitlines()[0].strip() if text else "")[:limit]


# 蓝牙音频端点的识别标记：端点名由系统给出，中英文界面下写法不同
BT_MARKERS = ("蓝牙", "Bluetooth", "Enco", "Buds", "AirPods", "WH-", "WF-", "Bose", "JBL")


def is_bluetooth_endpoint(name: str) -> bool:
    """判断一个渲染端点是不是蓝牙音频设备。纯函数。

    为什么需要：判断「蓝牙耳机断连后是否自动重连」，必须**先确认它消失过**。
    只看「现在在不在」会得出错误结论——耳机一直连着时，端点清单看起来一模一样。
    """
    return any(marker.lower() in name.lower() for marker in BT_MARKERS)


def endpoint_delta(before: dict[str, Any], after: dict[str, Any]) -> str:
    """用「渲染端点清单的变化」描述一次操作的实际效果。纯函数。

    这是命令报错时唯一还能拿到的客观事实：命令失败了，但设备可能已经掉了。
    没有它，报告里就只剩一句「命令失败」，而真正的现象（音频通路断了多久）反而丢了。
    """
    old, new = list(before.get("endpoints") or []), list(after.get("endpoints") or [])
    gone = [name for name in old if name not in new]
    back = [name for name in new if name not in old]
    parts = []
    if gone:
        parts.append("消失：" + "、".join(gone))
    if back:
        parts.append("出现：" + "、".join(back))
    if not parts:
        parts.append("清单没有变化")
    return f"渲染端点 {len(old)} → {len(new)}（" + "；".join(parts) + "）"


def pnp_status(instance_id: str) -> str:
    code, text = run_ps(pnp_status_script(instance_id))
    if text:
        return text.splitlines()[0].strip()
    return "MISSING" if code else ""


def pnp_apply(action: str, instance_id: str) -> tuple[bool, str]:
    code, text = run_ps(pnp_action_script(action, instance_id))
    if code != 0:
        return False, first_line(text, 160)
    return True, text.splitlines()[-1].strip() if text else ""


def pnp_apply_fallback(action: str, instance_id: str) -> tuple[bool, str]:
    """pnputil 兜底。成功判据同时看退出码与输出里是否出现失败字样。"""
    code, text = run_ps(pnp_fallback_script(action, instance_id))
    lowered = text.lower()
    ok = code == 0 and "failed" not in lowered and "失败" not in lowered and "错误" not in lowered
    return ok, first_line(text, 160) if text else ""


# ---------------------------------------------------------------- 目标解析


def resolve_targets(devices: list[dict[str, Any]], default_id: str,
                    headset_pattern: str | None = None,
                    other_pattern: str | None = None) -> dict[str, Any]:
    """从渲染端点列表里挑出「耳机」与「替代输出」。纯函数。

    耳机默认取**当前默认设备**（本机默认就是耳机），可用 --headset 显式指定；
    替代输出取第一个不是耳机的端点，可用 --other 显式指定。
    """
    if not devices:
        raise LookupError("没有任何活动的输出（渲染）设备")
    if headset_pattern:
        headset = audio_devices.find_device(devices, headset_pattern)
    else:
        headset = next((d for d in devices if d["id"] == default_id), None)
        if headset is None:
            raise LookupError("读不到当前默认输出设备；请用 --headset 指定耳机名称片段")
    if other_pattern:
        alternative = audio_devices.find_device(devices, other_pattern)
    else:
        candidates = [d for d in devices if d["id"] != headset["id"]]
        if not candidates:
            raise LookupError(f"除「{headset['name']}」外没有别的输出设备，切设备这一步做不了")
        alternative = candidates[0]
    if alternative["id"] == headset["id"]:
        raise LookupError("耳机与替代输出是同一个设备，请用 --other 指定另一个设备")
    return {"headset": headset, "alternative": alternative}


def default_game() -> str:
    """目标游戏目录：优先复用已经采过证据的星铁目录，别把证据落到占位目录名下。"""
    import session_runner

    for name in ("starrail-4.4", "starrail-填版本号"):
        if (ROOT / "captures" / "target-game" / name).is_dir():
            return name
    return session_runner.DEFAULT_GAME


# ---------------------------------------------------------------- 顺序执行（可测）


def run_sequence(plan: Sequence[dict[str, Any]], ops: Any,
                 log: Callable[[str], None] = print) -> dict:
    """按剧本依次执行，每一步记「计划时刻 / 执行时刻 / 生效时刻 / 前后状态」。

    这里只负责**时序与记录**：真正的动作由 `ops.do(kind)` 提供（真机是 DeviceOps，
    测试是假对象）。步骤按绝对秒数排班——某一步的等待超时了，后面的步骤仍按各自的绝对
    时刻执行，同时把「晚了多少」如实记进 late_by，不静默漂移。
    """
    records: list[dict[str, Any]] = []
    for entry in plan:
        target_at = float(entry["at"])
        now = ops.clock()
        if now < target_at:
            ops.sleep(target_at - now)
        issued = ops.clock()
        before = ops.observe()
        ok, effect = ops.do(entry["kind"])
        after = ops.observe()
        record = {
            "step": entry["step"], "kind": entry["kind"], "label": entry["label"],
            "planned_at": target_at, "issued_at": round(issued, 3),
            "effective_at": round(ops.clock(), 3), "late_by": round(issued - target_at, 3),
            "ok": bool(ok), "effect": effect, "before": before, "after": after,
        }
        records.append(record)
        flag = "OK " if ok else "失败"
        late = f"（晚 {record['late_by']:.1f}s）" if record["late_by"] > 1.0 else ""
        log(f"  [{flag}] {issued:7.3f}s  {entry['label']} —— {effect}{late}")
    return {"records": records, "marks": marks_from_records(records)}


class DeviceOps:
    """真机动作：COM 切默认设备 + PnP 禁用/启用 + 每一步前后的状态快照。"""

    def __init__(self, com: Any, headset: dict[str, Any], alternative: dict[str, Any],
                 headset_instance: str, bt_instance: str,
                 clock: Callable[[], float], poll_timeout: float = 25.0,
                 poll_interval: float = 0.25, log: Callable[[str], None] = print) -> None:
        self.com = com
        self.headset = headset
        self.alternative = alternative
        self.headset_instance = headset_instance
        self.bt_instance = bt_instance
        self._clock = clock
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.log = log
        self.disabled: set[str] = set()      # 被我们禁用的 PnP 实例，结尾必须恢复
        self.bt_names: list[str] | None = None   # 本段开始时在册的蓝牙音频端点
        self.bt_absent_seen = False              # 本段里是否真的观察到它消失过
        try:
            self.original_default_id = com.default_device_id()
        except Exception:
            self.original_default_id = ""

    # --- 基础设施 ---
    def clock(self) -> float:
        return self._clock()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def observe(self) -> dict[str, Any]:
        devices = self.com.devices()
        names = [d["name"] for d in devices]
        default = next((d["name"] for d in devices if d["is_default"]), "")
        if self.bt_names is None:
            # 一段开始时的端点清单就是基准：之后「消失过又回来」才有意义
            self.bt_names = [name for name in names if is_bluetooth_endpoint(name)]
        elif any(name not in names for name in self.bt_names):
            self.bt_absent_seen = True
        return {
            "default": default,
            "endpoints": names,
            "headset_present": any(d["name"] == self.headset["name"] for d in devices),
        }

    def _wait(self, predicate: Callable[[], bool]) -> tuple[bool, float]:
        started = time.monotonic()
        while True:
            try:
                if predicate():
                    return True, round(time.monotonic() - started, 3)
            except Exception:
                pass
            if time.monotonic() - started >= self.poll_timeout:
                return False, round(time.monotonic() - started, 3)
            time.sleep(self.poll_interval)

    def _default_name(self) -> str:
        devices = self.com.devices()
        return next((d["name"] for d in devices if d["is_default"]), "")

    # --- 动作分发 ---
    def do(self, kind: str) -> tuple[bool, str]:
        if kind == "switch_away":
            return self._switch(self.alternative, "切到替代输出设备")
        if kind in ("switch_back", "restore"):
            return self._switch(self.headset, "切回耳机")
        if kind == "unplug":
            return self._pnp_step("disable", self.headset_instance, self.headset["name"],
                                  "耳机端点已从渲染列表消失", expect_ok=False)
        if kind == "replug":
            return self._pnp_step("enable", self.headset_instance, self.headset["name"],
                                  "耳机端点已重新可用", expect_ok=True)
        if kind == "bt_down":
            return self._pnp_step("disable", self.bt_instance, "蓝牙无线电",
                                  "蓝牙无线电已停止", expect_ok=False)
        if kind == "bt_up":
            return self._pnp_step("enable", self.bt_instance, "蓝牙无线电",
                                  "蓝牙无线电已恢复", expect_ok=True)
        if kind == "observe_bt":
            names = self.observe()["endpoints"]
            if not self.bt_names:
                return True, ("本段开始时就**没有**蓝牙音频端点（耳机未连接）"
                              "→ 本项未覆盖，不能写成「已重连」")
            present = [name for name in self.bt_names if name in names]
            if present and self.bt_absent_seen:
                return True, (f"蓝牙音频端点在本段里消失过、现已回来（{present[0]}）"
                              "→ 可记为自动重连")
            if present:
                # 没观察到消失就断言「重连」是假的：耳机一直连着也会得到同样的端点清单
                return False, ("蓝牙音频端点在整段里**从未消失**（前置断连未生效）"
                               "→ 不能记为「已重连」；本项需重录或改用受控断连")
            return False, "蓝牙音频端点仍未回来（未自动重连）"
        return False, f"未知步骤类型：{kind}"

    def _switch(self, target: dict[str, Any], label: str) -> tuple[bool, str]:
        try:
            self.com.set_default(target["id"])
        except OSError as exc:
            return False, f"{label}失败：{exc}"
        ok, waited = self._wait(lambda: self._default_name() == target["name"])
        if ok:
            return True, f"默认设备 → {target['name']}（{waited:.2f}s 生效）"
        return False, f"{label}未生效：当前默认是 {self._default_name()}（等待 {waited:.2f}s）"

    def _pnp_step(self, action: str, instance_id: str, what: str, effect: str,
                  expect_ok: bool) -> tuple[bool, str]:
        if not instance_id:
            return False, (f"没找到「{what}」对应的 PnP 设备，这一步未执行"
                           "（报告里要写明本项未覆盖）")
        was_disabled = instance_id in self.disabled
        before = self.observe()
        ok, text = pnp_apply(action, instance_id)
        mechanism = "Disable-PnpDevice" if action == "disable" else "Enable-PnpDevice"
        if not ok:
            # 第一条路失败就换 pnputil 再试：两条都失败时**不能只说「命令失败」**——
            # 渲染端点清单的变化才是现场事实（实测：命令报常规故障，蓝牙链路却真的断了）。
            ok2, text2 = pnp_apply_fallback(action, instance_id)
            if ok2:
                mechanism = "pnputil"
            else:
                after = self.observe()
                return False, (
                    f"两种机制都没成功——{mechanism}：{text or '（无输出）'}；"
                    f"pnputil：{text2 or '（无输出）'}；{endpoint_delta(before, after)}。"
                    "本步要按「未受控」记录：现象可能有，但刺激没被脚本控制住")
        if action == "disable":
            self.disabled.add(instance_id)
        else:
            self.disabled.discard(instance_id)

        def reached() -> bool:
            return (pnp_status(instance_id) == "OK") == expect_ok

        ok, waited = self._wait(reached)
        status = pnp_status(instance_id)
        suffix = ""
        if action == "enable" and not was_disabled:
            suffix = "；注意此前并未成功禁用，故此步不构成「重连」的证据"
        if ok:
            return True, f"{effect}：PnP 状态 {status}（{mechanism}，{waited:.2f}s 生效）{suffix}"
        expected = "OK" if expect_ok else "非 OK"
        return False, (f"未达到预期（期望 {expected}）：PnP 状态仍是 {status}"
                       f"（{mechanism}，等待 {waited:.2f}s）{suffix}")

    # --- 安全网 ---
    def restore(self) -> list[str]:
        """无论如何都要把系统恢复原状：被禁用的重新启用，默认设备切回去。"""
        notes: list[str] = []
        for instance_id in sorted(self.disabled):
            ok, text = pnp_apply("enable", instance_id)
            notes.append(f"重新启用 …{instance_id[-12:]}：{'成功' if ok else '失败 ' + text}")
        self.disabled.clear()
        if self.original_default_id:
            try:
                self.com.set_default(self.original_default_id)
                notes.append("默认输出设备已切回录制前的设备")
            except OSError as exc:
                notes.append(f"切回默认设备失败：{exc}")
        return notes


# ---------------------------------------------------------------- 产出文件


def timeline_markdown(payload: dict[str, Any]) -> str:
    """把时间线写成一份可复核的 Markdown：机器只记录事实，不下结论。"""
    lines = [
        "# C-06 设备操作时间线（脚本自动生成）",
        "",
        f"- 用例：`{payload['case']}`（设备切换 / 热插拔一致性）",
        f"- 录制片段：`{payload['clip']}`",
        f"- 生成时间：{payload['generated_at']}",
        "- 坐标系：表中所有秒数都是**录像内相对秒数**，与 `measure.json` 的时间码同一坐标系；",
        f"- 耳机端点：{payload['headset']}",
        f"- 替代输出：{payload['alternative']}",
        f"- 蓝牙无线电：{payload['bt_radio'] or '（未参与本次采集）'}",
        f"- 执行权限：{'管理员（提权会话）' if payload['elevated'] else '普通用户'}",
        "",
        "| 步骤 | 计划(s) | 执行(s) | 生效(s) | 延迟(s) | 结果 | 动作 | 生效判据 | 默认设备 前→后 | 端点数 前→后 | 耳机端点 前→后 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload["records"]:
        lines.append(
            f"| `{item['step']}` | {item['planned_at']:.2f} | {item['issued_at']:.3f} |"
            f" {item['effective_at']:.3f} | {item['late_by']:.2f} |"
            f" {'生效' if item.get('ok') else '**未受控**'} | {item['label']} |"
            f" {item['effect']} | {item['before']['default']} → {item['after']['default']} |"
            f" {len(item['before'].get('endpoints') or [])} →"
            f" {len(item['after'].get('endpoints') or [])} |"
            f" {'在' if item['before']['headset_present'] else '不在'} →"
            f" {'在' if item['after']['headset_present'] else '不在'} |"
        )
    failed = [item["step"] for item in payload["records"] if not item.get("ok")]
    lines += ["", "## 这一次的判定摘要（脚本自动生成）", ""]
    if failed:
        lines.append(f"- {len(payload['records'])} 步中 {len(failed)} 步**未受控**："
                     + "、".join(f"`{name}`" for name in failed) + "。"
                     "「未受控」指脚本没能确认刺激生效，**不等于现象不存在**："
                     "同一行的「端点数 前→后」列记的是实际发生了什么。")
        if any(item["step"] == "bt_down" and not item.get("ok") for item in payload["records"]):
            lines.append("- 蓝牙断连未受控时，**不得**用「耳机端点现在在不在」判定「自动重连」："
                         "耳机一直连着时端点清单看起来完全一样，"
                         "判据只能是「它消失过、又回来了」（见端点数列）。")
    else:
        lines.append(f"- {len(payload['records'])} 步全部生效。")
    lines += [
        "",
        "## 如实说明（这几条不能省，否则证据会被误读）",
        "",
        "1. 「拔插耳机」＝**音频端点设备的禁用 / 启用**（需管理员），不是物理拔插："
        "3.5mm/USB 插孔没有可被脚本操纵的热插拔事件，能屏蔽的只有端点设备本身；",
        "2. 「蓝牙断连重连」＝**蓝牙无线电的禁用 / 启用**（需管理员），"
        "断的是整块无线电，不只是耳机这一条链路；",
        "3. 每一次操作都**真实改变了系统状态**（默认设备会实际迁移），"
        "所以「前 / 后」两列是现场快照而不是理论值；",
        "4. 默认设备被切到**没有接音箱 / 显示器的输出**时，采集到的会是静音——"
        "那是「音频跟着默认设备走」的设计后果，不是断流，定性时不能算缺陷；",
        "5. 表里只写状态变化，**结论（是否算缺陷、复现率）写在同一目录现场记录的第四节**。",
        "",
    ]
    return "\n".join(lines)


def write_timeline(directory: Path, clip_name: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    stem = Path(clip_name).stem
    json_path = directory / f"设备序列_{stem}.json"
    md_path = directory / f"设备序列_{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(timeline_markdown(payload), encoding="utf-8")
    return json_path, md_path


def append_log_section(skeleton: Path | None, payload: dict[str, Any]) -> bool:
    """把设备时间线追加到现场记录末尾（独立小节，不改动既有小节）。"""
    if skeleton is None or not skeleton.is_file():
        return False
    rows = ["", "## 七、设备操作时间线（脚本自动生成，勿手改）", "",
            "| 步骤 | 结果 | 动作 | 生效时刻(s) | 生效判据 | 默认设备变化 | 端点数 前→后 |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in payload["records"]:
        rows.append(f"| `{item['step']}` | {'生效' if item.get('ok') else '**未受控**'} |"
                    f" {item['label']} | {item['effective_at']:.3f} | {item['effect']} |"
                    f" {item['before']['default']} → {item['after']['default']} |"
                    f" {len(item['before'].get('endpoints') or [])} →"
                    f" {len(item['after'].get('endpoints') or [])} |")
    rows += ["", "同目录 `设备序列_*.md` 是完整快照表（含每一步前后的端点清单）。",
             "「拔插耳机」实为端点设备的禁用/启用、「蓝牙断连」实为蓝牙无线电的禁用/启用，"
             "均需管理员权限，**不是物理拔插**。",
             "标 `未受控` 的步骤是**脚本没能确认刺激生效**，不等于现象不存在——"
             "要看同一行的端点数变化。", ""]
    text = skeleton.read_text(encoding="utf-8")
    marker = "## 七、设备操作时间线"
    if marker in text:
        text = text.split(marker)[0].rstrip() + "\n"
    skeleton.write_text(text.rstrip() + "\n" + "\n".join(rows), encoding="utf-8")
    return True


# ---------------------------------------------------------------- 提权


class SHELLEXECUTEINFOW(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
ERROR_CANCELLED = 1223
INFINITE = 0xFFFFFFFF


def elevate_and_wait(script: Path, arguments: Sequence[str], workdir: Path) -> tuple[int, str]:
    """用 UAC 提权重跑自己，并等它结束。返回 (退出码, 错误说明)。

    ShellExecuteW 拿不到进程句柄，所以用 ShellExecuteExW + SEE_MASK_NOCLOSEPROCESS：
    否则父进程不知道提权那一侧跑完没有，也就没法在同一个控制台里给结论。
    """
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline([str(script), *arguments])
    info.lpDirectory = str(workdir)
    info.nShow = 1

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteExW.argtypes = [POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # 64 位下不声明 argtypes 会把句柄截断成 32 位——这类坑在本仓库已经踩过一次。
    if not shell32.ShellExecuteExW(byref(info)):
        code = int(kernel32.GetLastError())
        if code == ERROR_CANCELLED:
            return 2, "提权被取消（UAC 里点了「否」）——设备禁用/启用必须要有管理员权限"
        return 3, f"提权启动失败（错误码 {code}）"
    if not info.hProcess:
        return 3, "提权进程句柄为空（ShellExecuteEx 没返回 hProcess）"
    kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, byref(code))
    kernel32.CloseHandle(info.hProcess)
    return int(code.value), ""


# ---------------------------------------------------------------- 录一段 + 跑一轮


class Tee:
    """同时写控制台与日志文件：提权窗口关掉之后，输出还在。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def __call__(self, message: str) -> None:
        print(message, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")


def recording_path(client: Any, stop_response: dict[str, Any]) -> str:
    path = str((stop_response or {}).get("outputPath") or "")
    if path and Path(path).is_file():
        return path
    return str(client.call("GetRecordStatus").get("outputPath") or "")


def wait_for_file(path: str, timeout: float = 25.0) -> bool:
    """OBS 停止录制后文件还在写盘：不等就会出现「找不到录像文件」。"""
    candidate = Path(path)
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        size = candidate.stat().st_size if candidate.is_file() else -1
        if size > 0 and size == last:
            return True
        last = size
        time.sleep(1.0)
    return candidate.is_file() and candidate.stat().st_size > 0


def run_takes(args: argparse.Namespace, log: Callable[[str], None]) -> tuple[int, dict[str, Any]]:
    """主流程：连 OBS → 每段「开录 → 按时间表操作 → 停录 → 自动归档测量」。"""
    import audio_qa
    import obs_setup
    import session_runner

    summary: dict[str, Any] = {"case": args.case, "game": args.game, "wanted": args.takes,
                               "done": 0, "clips": [], "elevated": audio_devices.is_admin()}
    com = audio_devices.ComAudio()
    try:
        devices = com.devices()
        targets = resolve_targets(devices, com.default_device_id(), args.headset, args.other)
        headset, alternative = targets["headset"], targets["alternative"]
        log(f"耳机端点：{headset['name']}")
        log(f"替代输出：{alternative['name']}")

        log("解析 PnP 映射（禁用/启用要用 InstanceId）…")
        _, headset_text = run_ps(find_pnp_script("AudioEndpoint", headset["name"]))
        _, bt_text = run_ps(find_bt_radio_script())
        headset_instance = (headset_text.splitlines()[0].strip() if headset_text else "")
        bt_instance = (bt_text.splitlines()[0].strip() if bt_text else "")
        log(f"  耳机 PnP：{headset_instance or '（没找到——拔插两步会如实记为未覆盖）'}")
        log(f"  蓝牙无线电：{bt_instance or '（没找到——蓝牙三步会如实记为未覆盖）'}")
        summary.update({"headset": headset["name"], "alternative": alternative["name"],
                        "bt_radio": bt_instance, "headset_instance": headset_instance})

        steps = select_steps(DEFAULT_STEPS, do_switch=not args.skip_switch,
                             do_unplug=not args.skip_unplug, do_bt=not args.skip_bt)
        if not steps:
            log("所有步骤都被跳过了，没有可执行的剧本。")
            return 2, summary
        plan = build_plan(args.lead_in, steps, args.scale)
        stop_at = schedule_end(plan, args.tail)
        summary["plan"] = plan
        summary["schedule_end"] = stop_at
        log(f"时间表就绪：{len(plan)} 步，每段约 {stop_at:.0f}s。")

        try:
            client = obs_setup.ObsClient(events=obs_setup.ObsClient.EVENT_OUTPUTS)
        except Exception as exc:
            log(f"连不上 obs-websocket：{exc}")
            log("请确认 OBS 已开、工具 → WebSocket 服务器设置里已启用服务器。")
            return 2, summary

        if client.call("GetRecordStatus").get("outputActive"):
            log("OBS 当前正在录制 —— 先停掉它，本次采集从零起点开始。")
            client.call("StopRecord")
            time.sleep(2.0)

        ffmpeg = audio_qa.find_ffmpeg(None)
        wanted = max(1, int(args.takes))
        summary["wanted"] = wanted
        for index in range(1, wanted + 1):
            log(f"\n===== 第 {index}/{wanted} 段 =====")
            client.call("StartRecord")
            record_start = time.monotonic()
            log(f"● 已开始录制。第 1 步「{plan[0]['label']}」将在录像内 "
                f"{plan[0]['at']:.0f}s 处执行 —— 现在把游戏切到前台，确认音乐正常播放。")
            ops = DeviceOps(com, headset, alternative, headset_instance, bt_instance,
                            lambda: time.monotonic() - record_start, log=log)
            try:
                outcome = run_sequence(plan, ops, log)
            finally:
                for note in ops.restore():
                    log(f"  ↺ {note}")
            remaining = stop_at - (time.monotonic() - record_start)
            if remaining > 0:
                time.sleep(remaining)
            path = recording_path(client, client.call("StopRecord"))
            log(f"■ 已停止录制：{path or '（OBS 没给出路径）'}")
            if not path or not wait_for_file(path):
                log("  录像文件没落盘或还没写完，这一段跳过处理。")
                continue
            result = session_runner.process_one(Path(path), args.case, args.game, ffmpeg,
                                                args.dropout_min_ms, args.force, quiet=True)
            session_runner.print_summary(result)
            directory = result["archived"].parent
            payload = {
                "case": args.case, "clip": result["archived"].name,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "game": args.game, "takes": wanted, "take": index,
                "headset": headset["name"], "alternative": alternative["name"],
                "bt_radio": bt_instance, "elevated": audio_devices.is_admin(),
                "plan": plan, "schedule_end": stop_at,
                "records": outcome["records"],
            }
            json_path, md_path = write_timeline(directory, result["archived"].name, payload)
            log(f"  设备时间线：{md_path.name} / {json_path.name}")
            append_log_section(result.get("skeleton"), payload)
            written = session_runner.fill_marks(result["skeleton"], outcome["marks"])
            if written:
                log(f"  打点时间码已写入报告：{'、'.join(written)}")
            summary["clips"].append(result["archived"].name)
            summary["done"] += 1

        log(f"\n本次完成 {summary['done']}/{wanted} 段。")
        log("接下来：填现场记录第四节的结论 → 重建交付材料 → git 提交")
        client.close()
        return (0 if summary["done"] == wanted else 1), summary
    finally:
        com.close()


# ---------------------------------------------------------------- CLI


def add_timetable_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--case", default="c06_device_switch")
    parser.add_argument("--game", default=None, help="目标游戏目录名（默认自动挑已有证据的目录）")
    parser.add_argument("--headset", help="耳机名称片段（默认取当前默认输出设备）")
    parser.add_argument("--other", help="替代输出设备名称片段（默认取第一个非耳机端点）")
    parser.add_argument("--lead-in", type=float, default=8.0, help="开录后到第 1 步的秒数")
    parser.add_argument("--tail", type=float, default=5.0, help="最后一步之后再过多久停录")
    parser.add_argument("--scale", type=float, default=1.0, help="时间表整体缩放（>1 更慢更稳）")
    parser.add_argument("--skip-switch", action="store_true", help="不做「切设备」两步")
    parser.add_argument("--skip-unplug", action="store_true", help="不做「模拟拔插耳机」两步")
    parser.add_argument("--skip-bt", action="store_true", help="不做蓝牙三步")


def preview(args: argparse.Namespace) -> int:
    """不需要管理员：列设备、挑出目标、打印时间表。"""
    com = audio_devices.ComAudio()
    try:
        devices = com.devices()
        default_id = com.default_device_id()
        try:
            targets = resolve_targets(devices, default_id, args.headset, args.other)
        except LookupError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 2
    finally:
        com.close()
    steps = select_steps(DEFAULT_STEPS, do_switch=not args.skip_switch,
                         do_unplug=not args.skip_unplug, do_bt=not args.skip_bt)
    plan = build_plan(args.lead_in, steps, args.scale)
    print("当前输出设备：")
    for device in devices:
        print(f"  {device['name']}{' ← 当前默认' if device['is_default'] else ''}")
    print(f"\n耳机端点：{targets['headset']['name']}")
    print(f"替代输出：{targets['alternative']['name']}")
    print(f"目标目录：captures/target-game/{args.game}/{args.case}")
    print(f"\n时间表（录像内相对秒数；每段约 {schedule_end(plan, args.tail):.0f}s）：")
    for item in plan:
        print(f"  {item['at']:7.3f}s  {item['step']:<12} {item['label']}")
    print(f"  {schedule_end(plan, args.tail):7.3f}s  {'stop':<12} 停止录制")
    return 0


def device_report() -> int:
    com = audio_devices.ComAudio()
    try:
        for device in com.devices():
            mark = " ← 当前默认" if device["is_default"] else ""
            print(f"  {device['name']}{mark}")
    finally:
        com.close()
    print("\nPnP 映射（禁用/启用要用）：")
    listing = ("Get-PnpDevice -Class {cls} -ErrorAction SilentlyContinue | "
               "ForEach-Object {{ \"$($_.Status)`t$($_.FriendlyName)\" }}")
    for label, cls in (("AudioEndpoint", "AudioEndpoint"), ("Bluetooth", "Bluetooth")):
        print(f"  [{label}]")
        _, text = run_ps(listing.format(cls=cls))
        for line in (text or "").splitlines():
            print(f"    {line}")
    return 0


def refresh_timelines(args: argparse.Namespace) -> int:
    """按已存的 `设备序列_*.json` 重渲染时间线与报告第七节。

    为什么需要：表结构、判据措辞改了之后，**已经录好的素材不该重录**——
    json 里存的是原始记录（每一步的计划/执行/生效时刻与前后快照），
    渲染层变了只要重画一遍即可。
    """
    import session_runner  # 同一仓库脚本

    directory = session_runner.target_dir(args.case, args.game)
    payloads = sorted(directory.glob("设备序列_*.json"))
    if not payloads:
        print(f"没有找到时间线：{directory}", file=sys.stderr)
        return 2
    latest: dict[str, Any] | None = None
    for path in payloads:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"读不了 {path.name}：{exc}", file=sys.stderr)
            continue
        md_path = path.with_suffix(".md")
        md_path.write_text(timeline_markdown(payload), encoding="utf-8")
        latest = payload
        print(f"已重渲染：{md_path.name}")
    skeletons = sorted(directory.glob("*_现场记录.md"))
    if latest and skeletons and append_log_section(skeletons[0], latest):
        print(f"已重写报告第七节：{skeletons[0].name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="c06_sequence",
                                     description="C-06 设备切换一条龙取证（提权后自动执行）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出输出设备与 PnP 映射（不需要管理员）")
    for name, help_text in (("preview", "预演：列设备 + 打印时间表，不动系统"),
                            ("plan", "同 preview")):
        add_timetable_args(sub.add_parser(name, help=help_text))

    refresh = sub.add_parser("refresh", help="按已存的时间线 JSON 重渲染表格与报告第七节")
    refresh.add_argument("--case", default="c06_device_switch")
    refresh.add_argument("--game", default=None)

    run = sub.add_parser("run", help="提权后按时间表执行，并自动归档测量")
    add_timetable_args(run)
    run.add_argument("--takes", type=int, default=3, help="录几段（复现率按段数算，默认 3）")
    run.add_argument("--dropout-min-ms", type=float, default=80.0)
    run.add_argument("--force", action="store_true")
    run.add_argument("--yes", action="store_true", help="不再确认，直接提权执行")
    run.add_argument("--no-elevate", action="store_true",
                     help="不提权，直接在当前会话跑（普通会话会因权限不足而失败）")
    run.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--log", type=Path, default=ROOT / "runtime" / "c06_sequence.log")
    run.add_argument("--result", type=Path, default=ROOT / "runtime" / "c06_sequence.json")
    return parser


def child_arguments(args: argparse.Namespace) -> list[str]:
    """显式重建子进程参数：从 sys.argv 里筛选容易把 --result/--log 带重。"""
    arguments = ["run", "--elevated", "--takes", str(args.takes), "--case", args.case,
                 "--game", str(args.game), "--lead-in", str(args.lead_in),
                 "--tail", str(args.tail), "--scale", str(args.scale),
                 "--dropout-min-ms", str(args.dropout_min_ms),
                 "--result", str(args.result), "--log", str(args.log)]
    for flag, value in (("--headset", args.headset), ("--other", args.other)):
        if value:
            arguments += [flag, str(value)]
    for flag, enabled in (("--skip-switch", args.skip_switch),
                          ("--skip-unplug", args.skip_unplug),
                          ("--skip-bt", args.skip_bt), ("--force", args.force)):
        if enabled:
            arguments.append(flag)
    return arguments


def configure_stdio() -> None:
    """被重定向/被上层捕获时按 UTF-8 输出，否则中文设备名会变成乱码。

    只在**非交互**（管道/文件）时改：真控制台里 Python 走 Unicode 控制台 API，
    改成字节写反而会因为控制台代码页是 936 而显示乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    args.game = getattr(args, "game", None) or default_game()
    if args.command == "list":
        return device_report()
    if args.command in ("preview", "plan"):
        return preview(args)
    if args.command == "refresh":
        return refresh_timelines(args)

    admin = audio_devices.is_admin()
    if args.elevated and not admin:
        print("提权会话里仍然没有管理员权限——设备禁用/启用这一步做不了。", file=sys.stderr)
        return 3
    if not admin and not args.no_elevate:
        print("这条脚本会把三件事按顺序做完，其中两件需要管理员权限：")
        print("  1) 切默认输出设备（不需要管理员）")
        print("  2) 模拟拔出/插回耳机 = 禁用/启用音频端点   ← 需要管理员")
        print("  3) 蓝牙断连/重连 = 禁用/启用蓝牙无线电   ← 需要管理员")
        print(f"\n接下来会弹一次 UAC，请点「是」。日志：{args.log}")
        if not args.yes:
            try:
                input("准备好了按【回车】继续（Ctrl+C 取消）：")
            except (EOFError, KeyboardInterrupt):
                print("\n已取消。")
                return 2
        if args.result.is_file():
            args.result.unlink()
        code, error = elevate_and_wait(Path(__file__).resolve(), child_arguments(args), ROOT)
        if error:
            print(error, file=sys.stderr)
            return code
        print(f"\n提权会话结束（退出码 {code}）。")
        if args.result.is_file():
            try:
                payload = json.loads(args.result.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"读结果文件失败：{exc}", file=sys.stderr)
                payload = {}
            if payload:
                print(f"完成 {payload.get('done')}/{payload.get('wanted')} 段 · "
                      f"captures/target-game/{payload.get('game')}/{payload.get('case')}")
                for clip in payload.get("clips", []):
                    print(f"  {clip}")
        print(f"完整输出：{args.log}")
        return code

    log = Tee(args.log)
    log(f"C-06 自动采集开始：{time.strftime('%Y-%m-%d %H:%M:%S')} · 管理员={admin} · "
        f"用例={args.case} · 目标 {args.game} · {args.takes} 段")
    try:
        code, summary = run_takes(args, log)
    except KeyboardInterrupt:
        log("已中断（被禁用过的设备已在上面的安全网里恢复）。")
        code, summary = 2, {"done": 0, "wanted": args.takes}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"C-06 自动采集结束，退出码 {code}（结果 {args.result}）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
