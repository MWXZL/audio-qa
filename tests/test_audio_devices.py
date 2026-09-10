"""设备切换工具（scripts/audio_devices.py）的纯逻辑验证。

**不测试真实的 COM 切换**——那会真的改用户的声音输出。
这里只验证「弄错就会切错设备或改坏系统」的部分：设备匹配、切换剧本、PnP 命令构造、
以及非管理员时必须拒绝执行设备级操作。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import audio_devices  # noqa: E402

DEVICES = [
    {"id": "id-hyperx", "name": "扬声器 (HyperX Virtual Surround Sound)", "is_default": True},
    {"id": "id-realtek", "name": "扬声器 (Realtek(R) Audio)", "is_default": False},
    {"id": "id-g72", "name": "G72 Max (NVIDIA High Definition Audio)", "is_default": False},
]


class DeviceMatchTestCase(unittest.TestCase):
    def test_matches_by_name_fragment(self) -> None:
        self.assertEqual(audio_devices.find_device(DEVICES, "HyperX")["id"], "id-hyperx")
        self.assertEqual(audio_devices.find_device(DEVICES, "g72")["id"], "id-g72")

    def test_ambiguous_pattern_is_rejected(self) -> None:
        """「扬声器」匹配两个设备——必须报错而不是随便挑一个切过去。"""
        with self.assertRaises(LookupError) as ctx:
            audio_devices.find_device(DEVICES, "扬声器")
        self.assertIn("多个设备", str(ctx.exception))

    def test_unknown_pattern_lists_available(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            audio_devices.find_device(DEVICES, "蓝牙耳机")
        self.assertIn("当前有", str(ctx.exception))


class CyclePlanTestCase(unittest.TestCase):
    def test_plan_switches_and_switches_back(self) -> None:
        plan = audio_devices.build_cycle_plan("耳机", "显示器", hold_s=5.0)
        self.assertEqual([step[0] for step in plan], ["switch", "hold", "switch"])
        self.assertEqual(plan[0][1], 0.0)
        self.assertEqual(plan[-1][1], 5.0)
        self.assertIn("显示器", plan[0][2])
        self.assertIn("耳机", plan[-1][2])

    def test_plan_holds_the_requested_duration(self) -> None:
        plan = audio_devices.build_cycle_plan("A", "B", hold_s=7.5)
        self.assertEqual(plan[1][1], 7.5)


class PnpCommandTestCase(unittest.TestCase):
    def test_disable_and_enable_differ(self) -> None:
        disable = " ".join(audio_devices.pnp_command("disable", "HyperX"))
        enable = " ".join(audio_devices.pnp_command("enable", "HyperX"))
        self.assertIn("Disable-PnpDevice", disable)
        self.assertIn("Enable-PnpDevice", enable)

    def test_command_matches_first_device_by_name(self) -> None:
        command = " ".join(audio_devices.pnp_command("disable", "HyperX"))
        self.assertIn("FriendlyName -like '*HyperX*'", command)
        self.assertIn("Select-Object -First 1", command)

    def test_refuses_without_confirmation(self) -> None:
        """设备级禁用会改系统状态：没加 --yes 必须拒绝。"""
        self.assertEqual(audio_devices.run_pnp("disable", "HyperX", confirmed=False), 2)

    def test_refuses_without_admin(self) -> None:
        original = audio_devices.is_admin
        audio_devices.is_admin = lambda: False
        try:
            self.assertEqual(audio_devices.run_pnp("disable", "HyperX", confirmed=True), 2)
        finally:
            audio_devices.is_admin = original


if __name__ == "__main__":
    unittest.main()
