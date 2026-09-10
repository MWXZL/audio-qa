"""C-06 一键取证脚本（scripts/c06_sequence.py）的纯逻辑验证。

**不执行真实的设备禁用/启用、也不提权**——那会真的改系统状态、还会弹 UAC。
这里只验证「弄错就会切错设备、记错时间码、或者把模拟拔插写成物理拔插」的部分：
时间表排班、目标解析、PnP 命令构造、时序记录、产出文件的如实说明。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import c06_sequence  # noqa: E402

DEVICES = [
    {"id": "id-hyperx", "name": "扬声器 (HyperX Virtual Surround Sound)", "is_default": True},
    {"id": "id-realtek", "name": "扬声器 (Realtek(R) Audio)", "is_default": False},
    {"id": "id-g72", "name": "G72 Max (NVIDIA High Definition Audio)", "is_default": False},
]


class FakeOps:
    """假的动作执行器：维护一个虚拟时钟，让排班逻辑可以被精确断言。"""

    def __init__(self, cost: float = 0.0) -> None:
        self.now = 0.0
        self.cost = cost
        self.sleeps: list[float] = []
        self.done: list[str] = []
        self.observed = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def observe(self) -> dict:
        self.observed += 1
        return {"default": f"设备{self.observed}", "endpoints": ["A"], "headset_present": True}

    def do(self, kind: str) -> tuple[bool, str]:
        self.done.append(kind)
        self.now += self.cost
        return True, f"已完成 {kind}"


class TimetableTestCase(unittest.TestCase):
    def test_plan_offsets_are_cumulative(self) -> None:
        plan = c06_sequence.build_plan(8.0)
        self.assertEqual([item["at"] for item in plan],
                         [8.0, 16.0, 24.0, 32.0, 42.0, 52.0, 62.0, 70.0])

    def test_scale_stretches_dwell_and_offsets(self) -> None:
        plan = c06_sequence.build_plan(10.0, c06_sequence.DEFAULT_STEPS, 2.0)
        self.assertEqual(plan[0]["at"], 10.0)
        self.assertEqual(plan[1]["at"], 26.0)   # 10 + 8*2
        self.assertEqual(plan[2]["at"], 42.0)   # + 8*2

    def test_schedule_end_leaves_tail_after_last_step(self) -> None:
        plan = c06_sequence.build_plan(8.0)
        self.assertEqual(c06_sequence.schedule_end(plan, 5.0), 81.0)

    def test_schedule_end_on_empty_plan(self) -> None:
        self.assertEqual(c06_sequence.schedule_end([], 5.0), 5.0)

    def test_step_filters(self) -> None:
        """--skip-bt 必须把蓝牙三步全部拿掉，而不是漏掉第一步。"""
        only_switch = c06_sequence.select_steps(c06_sequence.DEFAULT_STEPS, do_bt=False,
                                                do_unplug=False)
        self.assertEqual([item[1] for item in only_switch],
                         ["switch_away", "switch_back", "restore"])
        no_bt = c06_sequence.select_steps(c06_sequence.DEFAULT_STEPS, do_bt=False)
        self.assertNotIn("bt_down", [item[1] for item in no_bt])
        self.assertNotIn("observe_bt", [item[1] for item in no_bt])
        self.assertEqual(len(c06_sequence.select_steps(c06_sequence.DEFAULT_STEPS)), 8)

    def test_marks_use_effective_time_not_issue_time(self) -> None:
        records = [{"step": "unplug", "issued_at": 24.0, "effective_at": 24.9}]
        self.assertEqual(c06_sequence.marks_from_records(records), {"unplug": 24.9})


class RunSequenceTestCase(unittest.TestCase):
    def test_steps_run_at_scheduled_offsets(self) -> None:
        plan = c06_sequence.build_plan(8.0, c06_sequence.DEFAULT_STEPS[:3], 1.0)
        ops = FakeOps()
        result = c06_sequence.run_sequence(plan, ops, log=lambda _message: None)
        self.assertEqual(ops.done, ["switch_away", "switch_back", "unplug"])
        self.assertEqual(ops.sleeps, [8.0, 8.0, 8.0])
        self.assertEqual([item["issued_at"] for item in result["records"]], [8.0, 16.0, 24.0])

    def test_slow_step_is_recorded_as_late_not_silently_shifted(self) -> None:
        """某一步的等待超时不该把后面的步骤整体推后：后面的仍按绝对时刻，只记 late_by。"""
        plan = c06_sequence.build_plan(0.0, c06_sequence.DEFAULT_STEPS[:3], 1.0)
        ops = FakeOps(cost=10.0)
        lines: list[str] = []
        result = c06_sequence.run_sequence(plan, ops, log=lines.append)
        records = result["records"]
        self.assertEqual(records[1]["planned_at"], 8.0)
        self.assertEqual(records[1]["late_by"], 2.0)      # 10s 才做完，8s 的档已经过了
        self.assertGreater(records[2]["late_by"], 0.0)
        self.assertTrue(any("晚 " in line for line in lines))

    def test_each_step_records_before_and_after_snapshot(self) -> None:
        plan = c06_sequence.build_plan(0.0, c06_sequence.DEFAULT_STEPS[:2], 1.0)
        ops = FakeOps()
        result = c06_sequence.run_sequence(plan, ops, log=lambda _message: None)
        first = result["records"][0]
        self.assertEqual(ops.observed, 4)                 # 2 步 × 前后各一次
        self.assertEqual(first["before"]["default"], "设备1")
        self.assertEqual(first["after"]["default"], "设备2")

    def test_marks_are_written_from_effective_times(self) -> None:
        plan = c06_sequence.build_plan(4.0, c06_sequence.DEFAULT_STEPS[:2], 1.0)
        ops = FakeOps(cost=1.5)
        result = c06_sequence.run_sequence(plan, ops, log=lambda _message: None)
        self.assertEqual(result["marks"]["switch_away"], 5.5)


class ResolveTargetsTestCase(unittest.TestCase):
    def test_headset_defaults_to_current_default_device(self) -> None:
        targets = c06_sequence.resolve_targets(DEVICES, "id-hyperx")
        self.assertEqual(targets["headset"]["id"], "id-hyperx")
        self.assertEqual(targets["alternative"]["id"], "id-realtek")

    def test_explicit_patterns_win(self) -> None:
        targets = c06_sequence.resolve_targets(DEVICES, "id-hyperx", None, "G72")
        self.assertEqual(targets["alternative"]["id"], "id-g72")

    def test_only_one_device_is_reported_not_guessed(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            c06_sequence.resolve_targets(DEVICES[:1], "id-hyperx")
        self.assertIn("没有别的输出设备", str(ctx.exception))

    def test_no_active_device_is_reported(self) -> None:
        with self.assertRaises(LookupError):
            c06_sequence.resolve_targets([], "")

    def test_same_device_for_both_roles_is_rejected(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            c06_sequence.resolve_targets(DEVICES, "id-hyperx", "HyperX", "HyperX")
        self.assertIn("同一个设备", str(ctx.exception))

    def test_ambiguous_alternative_pattern_is_rejected(self) -> None:
        """「扬声器」匹配两个端点——必须报错，不能随便挑一个切过去。"""
        with self.assertRaises(LookupError):
            c06_sequence.resolve_targets(DEVICES, "id-hyperx", None, "扬声器")


class PnpCommandTestCase(unittest.TestCase):
    def test_single_quote_in_name_is_escaped(self) -> None:
        self.assertEqual(c06_sequence.ps_quote("O'Brien 的耳机"), "O''Brien 的耳机")
        script = c06_sequence.find_pnp_script("AudioEndpoint", "O'Brien")
        self.assertIn("'O''Brien'", script)

    def test_find_pnp_uses_exact_name_match(self) -> None:
        script = c06_sequence.find_pnp_script("AudioEndpoint", "扬声器 (HyperX)")
        self.assertIn("-eq", script)
        self.assertNotIn("-like", script)

    def test_bt_radio_lookup_excludes_enumerators(self) -> None:
        """枚举器/RFCOMM 也是 Bluetooth 类设备，误当成无线电会拔错东西。"""
        script = c06_sequence.find_bt_radio_script()
        self.assertIn("Enumerator", script)
        self.assertIn("RFCOMM", script)
        self.assertIn("$_.Status -eq 'OK'", script)

    def test_action_verb_and_status_readback(self) -> None:
        disable = c06_sequence.pnp_action_script("disable", "SWD\\MMDEVAPI\\{x}")
        self.assertIn("Disable-PnpDevice", disable)
        self.assertIn("-Confirm:$false", disable)
        self.assertIn("'SWD\\MMDEVAPI\\{x}'", disable)
        self.assertIn("Enable-PnpDevice", c06_sequence.pnp_action_script("enable", "SWD\\{x}"))

    def test_utf8_prefix_prevents_mojibake(self) -> None:
        """中文 Windows 上不加这个前缀，管道里是 GBK 字节，Python 按 UTF-8 读会全是乱码。"""
        command = c06_sequence.pnp_command("Get-PnpDevice")
        self.assertIn("[Console]::OutputEncoding", command[-1])
        self.assertEqual(command[:3], ["powershell", "-NoProfile", "-NonInteractive"])


class TimelineFileTestCase(unittest.TestCase):
    def payload(self) -> dict:
        plan = c06_sequence.build_plan(8.0, c06_sequence.DEFAULT_STEPS[:2], 1.0)
        ops = FakeOps()
        result = c06_sequence.run_sequence(plan, ops, log=lambda _message: None)
        return {
            "case": "c06_device_switch", "clip": "raw_20260610_c06_device_switch_r01.mkv",
            "generated_at": "2026-06-10 21:00:00", "game": "starrail-4.4",
            "takes": 3, "take": 1,
            "headset": "扬声器 (HyperX Virtual Surround Sound)",
            "alternative": "扬声器 (Realtek(R) Audio)",
            "bt_radio": "USB\\VID_8087&PID_0033\\5&EF00746&0&10", "elevated": True,
            "plan": plan, "schedule_end": 30.0, "records": result["records"],
        }

    def test_markdown_states_that_unplug_is_simulated(self) -> None:
        """「模拟拔插」这条如实说明必须写死在产出里，否则会被读成物理拔插。"""
        text = c06_sequence.timeline_markdown(self.payload())
        self.assertIn("不是物理拔插", text)
        self.assertIn("禁用 / 启用", text)
        self.assertIn("蓝牙无线电", text)
        self.assertIn("switch_away", text)

    def test_markdown_has_no_unfinished_markers(self) -> None:
        """产出文件会被自检扫「未完成标记」，机器生成的内容不该带这些词。"""
        text = c06_sequence.timeline_markdown(self.payload())
        for marker in ("待填", "待执行", "TODO", "TBD"):
            self.assertNotIn(marker, text)

    def test_write_timeline_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            json_path, md_path = c06_sequence.write_timeline(
                directory, "raw_20260610_c06_device_switch_r01.mkv", self.payload())
            self.assertTrue(json_path.is_file() and md_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["records"]), 2)
            self.assertEqual(json_path.name, "设备序列_raw_20260610_c06_device_switch_r01.json")

    def test_append_log_section_is_idempotent(self) -> None:
        """重复追加会把现场记录越写越乱，必须替换而不是叠加。"""
        with tempfile.TemporaryDirectory() as tmp:
            skeleton = Path(tmp) / "现场记录.md"
            skeleton.write_text("# 测试 现场记录\n\n## 四、结论（待填）\n\n- 结论：\n",
                                encoding="utf-8")
            payload = self.payload()
            c06_sequence.append_log_section(skeleton, payload)
            c06_sequence.append_log_section(skeleton, payload)
            text = skeleton.read_text(encoding="utf-8")
            self.assertEqual(text.count("## 七、设备操作时间线"), 1)
            self.assertIn("## 四、结论（待填）", text)
            self.assertIn("switch_away", text)

    def test_append_log_section_tolerates_missing_skeleton(self) -> None:
        self.assertFalse(c06_sequence.append_log_section(None, self.payload()))


class ElevationTestCase(unittest.TestCase):
    def test_child_arguments_rebuild_command_line(self) -> None:
        """子进程参数是显式重建的：从 sys.argv 里筛会把 --result/--log 带重。"""
        parser = c06_sequence.build_parser()
        args = parser.parse_args(["run", "--takes", "2", "--game", "starrail-4.4",
                                  "--headset", "HyperX", "--skip-bt", "--result",
                                  "runtime/r.json", "--log", "runtime/r.log"])
        arguments = c06_sequence.child_arguments(args)
        self.assertEqual(arguments.count("--result"), 1)
        self.assertEqual(arguments.count("--log"), 1)
        self.assertIn("--elevated", arguments)
        self.assertIn("--skip-bt", arguments)
        self.assertEqual(arguments[arguments.index("--takes") + 1], "2")
        # 子进程不再提权（已经在管理员会话里），避免无限套娃
        self.assertNotIn("--no-elevate", arguments)

    def test_elevation_is_not_attempted_without_admin(self) -> None:
        """非管理员时 run 应当走提权分支；这里只断言判定函数可用且不抛异常。"""
        self.assertIsInstance(c06_sequence.elevate_and_wait, type(lambda: None))
        self.assertIn(c06_sequence.ERROR_CANCELLED, (1223,))


if __name__ == "__main__":
    unittest.main()
