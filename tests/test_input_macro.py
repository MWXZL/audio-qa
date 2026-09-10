"""外部模拟输入（scripts/input_macro.py）的纯逻辑验证。

**不测试真实的 SendInput 调用**——那会往系统里发按键，在测试里做是不安全的。
这里只验证「出错就会发错键」的部分：序列解析、时长估算、dry-run 计划与打点、
未知序列/未知按键必须报错、超时上限必须拦住。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import input_macro  # noqa: E402


class ScenarioTestCase(unittest.TestCase):
    def test_every_step_uses_a_known_action(self) -> None:
        for name, scenario in input_macro.SCENARIOS.items():
            for step in scenario["steps"]:
                with self.subTest(scenario=name, step=step):
                    self.assertIn(step[0], {"wait", "key", "click", "mark"})

    def test_every_key_exists_in_scan_code_table(self) -> None:
        """未知键名必须在发送前就被发现，而不是静默发错键。"""
        for name, scenario in input_macro.SCENARIOS.items():
            for step in scenario["steps"]:
                if step[0] == "key":
                    with self.subTest(scenario=name, key=step[1]):
                        self.assertIn(str(step[1]).upper(), input_macro.SCANCODES)

    def test_marks_are_reachable_in_every_scenario(self) -> None:
        for name, scenario in input_macro.SCENARIOS.items():
            marks = [step[1] for step in scenario["steps"] if step[0] == "mark"]
            with self.subTest(scenario=name):
                self.assertTrue(marks, "序列里至少要有一个打点，否则报告没有时间码")

    def test_total_seconds_matches_dry_run_estimate(self) -> None:
        scenario = input_macro.SCENARIOS["bug_03_combat"]
        result = input_macro.run_scenario("bug_03_combat", 0, True, 120)
        self.assertAlmostEqual(result["estimate_s"], input_macro.total_seconds(scenario), places=2)

    def test_dry_run_marks_are_monotonic(self) -> None:
        result = input_macro.run_scenario("bug_03_combat", 0, True, 120)
        values = list(result["marks"].values())
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], 0)

    def test_dry_run_never_touches_the_keyboard(self) -> None:
        """dry-run 必须完全不发按键——把 SendInput 换成会抛异常的桩来证明。"""
        calls: list[str] = []
        original_key, original_click = input_macro.send_key, input_macro.send_click
        input_macro.send_key = lambda *a, **k: calls.append("key")
        input_macro.send_click = lambda *a, **k: calls.append("click")
        try:
            result = input_macro.run_scenario("bug_03_combat", 0, True, 120)
        finally:
            input_macro.send_key, input_macro.send_click = original_key, original_click
        self.assertTrue(result["dry_run"])
        self.assertEqual(calls, [])

    def test_unknown_scenario_raises(self) -> None:
        with self.assertRaises(KeyError):
            input_macro.run_scenario("nope", 0, True, 120)

    def test_unknown_key_raises_before_sending(self) -> None:
        with self.assertRaises(ValueError):
            input_macro.send_key("NOTAKEY")

    def test_max_seconds_guard_blocks_long_scenarios(self) -> None:
        with self.assertRaises(ValueError):
            input_macro.run_scenario("bug_03_combat", 0, False, max_seconds=1.0)

    def test_log_contains_every_step(self) -> None:
        result = input_macro.run_scenario("walk_steps", 0, True, 120)
        self.assertEqual(len(result["log"]), len(input_macro.SCENARIOS["walk_steps"]["steps"]))


if __name__ == "__main__":
    unittest.main()
