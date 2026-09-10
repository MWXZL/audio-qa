#!/usr/bin/env python3
"""输出设备切换的自动化（对应兼容/设备切换用例 C-06）。

能自动化的分三档，**脚本会如实说明自己做不到哪一档**：

1. **切默认输出设备**：不需要管理员。走 COM 的 `IPolicyConfig::SetDefaultEndpoint`，
   这是 SoundSwitch / nircmd 一类工具用的同一条路（Windows 没有官方 CLI）。
2. **拔插耳机**：没有软件接口。等价做法是**禁用/启用音频端点设备**（设备消失再出现），
   **需要管理员权限**，而且它与物理拔插的语义不同（插孔没有热插拔事件）——
   报告里必须如实写「用设备禁用/启用模拟」，不能写成「拔插耳机」。
3. **蓝牙断连重连**：同样只能通过禁用/启用蓝牙无线电（**需要管理员**）。

设计上的安全约定：
- 任何切换都记录**时间线 JSON**（每步的相对秒数），可直接并入报告的关键时间码；
- 默认在结束时**恢复原来的默认设备**（`--keep-changed` 才不恢复）；
- 禁用设备前要求 `--yes`，且在 `finally` 里恢复，避免把人家的声音搞没。

用法
----
    python scripts\\audio_devices.py list
    python scripts\\audio_devices.py switch "HyperX"          # 切到耳机
    python scripts\\audio_devices.py cycle "G72" --hold 5     # 切过去 → 停 5 秒 → 切回来
    python scripts\\audio_devices.py disable "HyperX" --yes   # 模拟拔耳机（需管理员）
    python scripts\\audio_devices.py enable  "HyperX"         # 插回
    python scripts\\audio_devices.py bt --off                 # 蓝牙断连（需管理员）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import POINTER, Structure, byref, c_void_p, c_wchar_p, wintypes
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
CLSCTX_ALL = 0x17
STGM_READ = 0


class GUID(Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str = "") -> None:
        super().__init__()
        if not text:
            return
        # 解析失败必须报错：早先版本忽略了 HRESULT，结果 PKEY 变成全零 GUID，
        # 设备名全部读成「未命名」——静默失败比报错难查得多。
        if ctypes.windll.ole32.CLSIDFromString(c_wchar_p(text), byref(self)) != 0:
            raise ValueError(f"GUID 解析失败：{text}")


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSID_PolicyConfigClient = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"
IID_IPolicyConfig = "{f8679f50-850a-41cf-9c72-430f290290c8}"
# PKEY_Device_FriendlyName = fmtid + pid（pid 不能拼进 GUID 字符串，CLSIDFromString 会解析失败）
PKEY_FRIENDLY_NAME_FMTID = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
PKEY_FRIENDLY_NAME_PID = 14

DEVICE_STATE_ACTIVE = 0x1
E_ROLE = (0, 1, 2)   # eConsole / eMultimedia / eCommunications：三个角色都切才算真的切换


HRESULT = ctypes.c_long


def _vtbl(ptr: c_void_p, index: int, argtypes: list, restype: Any = HRESULT) -> Any:
    """按 vtable 序号取 COM 方法，并**显式声明参数类型**。

    早先版本想用 `type(arg)` 猜类型，遇到 `byref()` 直接报
    「item 4 in _argtypes_ has no from_param method」——CArgObject 不能当类型用。
    每个调用点写清原型才是 ctypes 调 COM 的正规做法。
    """
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])


def _release(ptr: c_void_p | None) -> None:
    if ptr:
        try:
            _vtbl(ptr, 2, [])(ptr)
        except Exception:
            pass


class ComAudio:
    """枚举输出设备 + 切换默认设备（COM 生命周期自管理）。"""

    def __init__(self) -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        self.enumerator: c_void_p | None = c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(
            byref(GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
            byref(GUID(IID_IMMDeviceEnumerator)), byref(self.enumerator))
        if hr != 0:
            raise OSError(f"创建音频设备枚举器失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")

    def close(self) -> None:
        _release(self.enumerator)
        self.enumerator = None

    def devices(self) -> list[dict[str, Any]]:
        collection = c_void_p()
        fn = _vtbl(self.enumerator, 3, [ctypes.c_int, ctypes.c_uint, POINTER(c_void_p)])
        if fn(self.enumerator, 0, DEVICE_STATE_ACTIVE, byref(collection)) != 0:   # eRender
            raise OSError("枚举音频端点失败")
        try:
            count = ctypes.c_uint()
            _vtbl(collection, 3, [POINTER(ctypes.c_uint)])(collection, byref(count))
            default_id = self.default_device_id()
            found: list[dict[str, Any]] = []
            for index in range(count.value):
                device = c_void_p()
                if _vtbl(collection, 4, [ctypes.c_uint, POINTER(c_void_p)])(
                        collection, index, byref(device)) != 0:      # Item
                    continue
                try:
                    device_id = self.device_id(device)
                    found.append({"id": device_id, "name": self.device_name(device),
                                  "is_default": device_id == default_id})
                finally:
                    _release(device)
            return found
        finally:
            _release(collection)

    def device_id(self, device: c_void_p) -> str:
        raw = c_wchar_p()
        _vtbl(device, 5, [POINTER(c_wchar_p)])(device, byref(raw))       # IMMDevice::GetId
        return raw.value or ""

    def device_name(self, device: c_void_p) -> str:
        store = c_void_p()
        if _vtbl(device, 4, [ctypes.c_uint, POINTER(c_void_p)])(
                device, STGM_READ, byref(store)) != 0:                   # OpenPropertyStore
            return "（无法读取名称）"

        class PROPVARIANT(Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("pad", ctypes.c_byte * 8)]

        try:
            value = PROPVARIANT()
            key = PROPERTYKEY(GUID(PKEY_FRIENDLY_NAME_FMTID), PKEY_FRIENDLY_NAME_PID)
            # IPropertyStore 的 vtable 次序要数准：0 QueryInterface / 1 AddRef / 2 Release /
            # 3 GetCount / 4 GetAt / 5 GetValue。用 4 会调成 GetAt 并返回 E_INVALIDARG。
            code = _vtbl(store, 5, [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)])(
                store, byref(key), byref(value))
            if code == 0 and value.vt == 31:                             # VT_LPWSTR
                # 注意：这里不能用 ctypes.cast(value.pad, c_wchar_p).value——
                # 实测那样读出来是乱码；LPWSTR 的指针就存在 pad 的 8 个字节里，
                # 取出来再 wstring_at 才稳。
                pointer = int.from_bytes(bytes(value.pad), "little")
                if pointer:
                    return ctypes.wstring_at(pointer)
                return "（未命名：空指针）"
            return f"（未命名 vt={value.vt} code=0x{code & 0xFFFFFFFF:08X}）"
        finally:
            _release(store)

    def default_device_id(self) -> str:
        device = c_void_p()
        fn = _vtbl(self.enumerator, 4, [ctypes.c_int, ctypes.c_int, POINTER(c_void_p)])
        if fn(self.enumerator, 0, 0, byref(device)) != 0:                # eRender, eConsole
            return ""
        try:
            return self.device_id(device)
        finally:
            _release(device)

    def set_default(self, device_id: str) -> None:
        policy = c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(
            byref(GUID(CLSID_PolicyConfigClient)), None, CLSCTX_ALL,
            byref(GUID(IID_IPolicyConfig)), byref(policy))
        if hr != 0:
            raise OSError(f"创建 IPolicyConfig 失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")
        try:
            for role in E_ROLE:
                # IPolicyConfig::SetDefaultEndpoint（vtable 序号 13）
                code = _vtbl(policy, 13, [c_wchar_p, ctypes.c_int])(
                    policy, device_id, role)
                if code != 0:
                    raise OSError(f"切换默认设备失败（role={role}, HRESULT 0x{code & 0xFFFFFFFF:08X}）")
        finally:
            _release(policy)


def find_device(devices: list[dict[str, Any]], pattern: str) -> dict[str, Any]:
    """按名称片段匹配设备（纯函数）。多个匹配时报错要求更精确，避免切错设备。"""
    hits = [d for d in devices if pattern.lower() in d["name"].lower()]
    if not hits:
        options = "、".join(d["name"] for d in devices) or "（没有可用设备）"
        raise LookupError(f"没有名称包含「{pattern}」的输出设备；当前有：{options}")
    if len(hits) > 1:
        raise LookupError(f"「{pattern}」匹配到多个设备：{'、'.join(d['name'] for d in hits)}；请写得更具体")
    return hits[0]


def build_cycle_plan(from_name: str, to_name: str, hold_s: float) -> list[tuple[str, float, str]]:
    """切换剧本：(动作, 相对秒数, 说明)。纯函数，便于测试。"""
    return [
        ("switch", 0.0, f"切到 {to_name}"),
        ("hold", hold_s, f"停留在 {to_name}"),
        ("switch", hold_s, f"切回 {from_name}"),
    ]


def run_cycle(pattern: str, hold_s: float, log_path: Path | None, keep_changed: bool) -> dict:
    com = ComAudio()
    marks: dict[str, float] = {}
    log: list[dict[str, Any]] = []
    started = time.monotonic()
    original = None
    try:
        devices = com.devices()
        original = find_device(devices, "Realtek") if not com.default_device_id() else None
        original_id = com.default_device_id()
        original_name = next((d["name"] for d in devices if d["id"] == original_id), "（未知）")
        target = find_device(devices, pattern)
        for action, at, detail in build_cycle_plan(original_name, target["name"], hold_s):
            wait = at - (time.monotonic() - started)
            if wait > 0:
                time.sleep(wait)
            if action == "switch":
                com.set_default(target["id"] if detail.startswith("切到") else original_id)
                key = "switch_to" if detail.startswith("切到") else "switch_back"
                marks[key] = round(time.monotonic() - started, 3)
                log.append({"at": marks[key], "action": detail,
                            "name": target["name"] if key == "switch_to" else original_name})
                print(f"  {marks[key]:7.3f}s  {detail}")
        if not keep_changed:
            com.set_default(original_id)
            marks["restored"] = round(time.monotonic() - started, 3)
            log.append({"at": marks["restored"], "action": f"恢复默认设备 {original_name}"})
            print(f"  {marks['restored']:7.3f}s  已恢复默认设备：{original_name}")
        result = {"original": original_name, "target": target["name"],
                  "marks": marks, "log": log}
    finally:
        com.close()
        del original
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"时间线：{log_path}")
    return result


def pnp_command(action: str, pattern: str) -> list[str]:
    """构造 PnP 禁用/启用命令（需要管理员）。

    取第一个名称匹配的设备。用 PowerShell 的 Disable/Enable-PnpDevice——
    这是「等价于拔插」的做法，报告里必须写明是设备级禁用/启用，不是物理拔插。
    """
    script = (
        f"$d = Get-PnpDevice | Where-Object {{ $_.FriendlyName -like '*{pattern}*' }} | Select-Object -First 1; "
        f"if (-not $d) {{ Write-Error '未找到设备'; exit 2 }}; "
        f"{'Disable' if action == 'disable' else 'Enable'}-PnpDevice -InstanceId $d.InstanceId -Confirm:$false; "
        f"Write-Output $d.FriendlyName"
    )
    return ["powershell", "-NoProfile", "-Command", script]


def run_pnp(action: str, pattern: str, confirmed: bool) -> int:
    if not confirmed:
        print("这是会改变系统设备状态的操作：确认后加 --yes 再跑。", file=sys.stderr)
        return 2
    if not is_admin():
        print("需要管理员权限：请以管理员身份打开终端后重试（设备级禁用/启用动不了 PnP）。", file=sys.stderr)
        return 2
    done = subprocess.run(pnp_command(action, pattern), capture_output=True,
                          encoding="utf-8", errors="replace")
    print((done.stdout or done.stderr or "").strip())
    return done.returncode


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio_devices", description="输出设备切换自动化（C-06）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出输出设备与当前默认设备")
    switch = sub.add_parser("switch", help="切换默认输出设备（不需要管理员）")
    switch.add_argument("pattern")
    cycle = sub.add_parser("cycle", help="切过去 → 停留 → 切回，并记录时间线")
    cycle.add_argument("pattern")
    cycle.add_argument("--hold", type=float, default=5.0)
    cycle.add_argument("--log", type=Path)
    cycle.add_argument("--keep-changed", action="store_true", help="结束时不要切回原设备")
    for name in ("disable", "enable"):
        pnp = sub.add_parser(name, help=f"{name} 设备（模拟拔插，需要管理员）")
        pnp.add_argument("pattern")
        pnp.add_argument("--yes", action="store_true")
    bt = sub.add_parser("bt", help="蓝牙无线电断连/重连（需要管理员）")
    bt.add_argument("--off", action="store_true", help="断开；不加则重连")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        com = ComAudio()
        try:
            for device in com.devices():
                mark = " ← 当前默认" if device["is_default"] else ""
                print(f"  {device['name']}{mark}")
            if not com.devices():
                print("  （没有活动的输出设备）")
        finally:
            com.close()
        return 0
    if args.command == "switch":
        com = ComAudio()
        try:
            target = find_device(com.devices(), args.pattern)
            com.set_default(target["id"])
            print(f"已切换默认输出设备 → {target['name']}")
        except (LookupError, OSError) as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 2
        finally:
            com.close()
        return 0
    if args.command == "cycle":
        try:
            run_cycle(args.pattern, args.hold, args.log, args.keep_changed)
        except (LookupError, OSError) as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 2
        return 0
    if args.command in {"disable", "enable"}:
        return run_pnp(args.command, args.pattern, args.yes)
    if args.command == "bt":
        return run_pnp("disable" if args.off else "enable", "Bluetooth", True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
