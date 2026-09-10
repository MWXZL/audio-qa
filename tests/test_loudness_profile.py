"""瞬时响度剖面（scripts/loudness_profile.py）的纯逻辑验证。

它给出的窗口是「听辨入口」，会被写进现场记录——算错就会把人引到错误的时间点，
所以窗口聚合、静音剔除、汇总统计都要有测试。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import loudness_profile as profile  # noqa: E402

EBUR128_SAMPLE = """
[Parsed_ebur128_0 @ 00000147cbf31c40] t: 0.0998125  TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 00000147cbf31c40] t: 0.399812   TARGET:-23 LUFS    M: -14.1 S:-120.7     I: -14.1 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 00000147cbf31c40] t: 1.000000   TARGET:-23 LUFS    M: -16.0 S:-120.7     I: -14.1 LUFS       LRA:   0.0 LU
"""


class ParseTestCase(unittest.TestCase):
    def test_parses_time_and_momentary_loudness(self) -> None:
        samples = profile.parse_samples(EBUR128_SAMPLE)
        self.assertEqual(samples, [(0.0998125, -120.7), (0.399812, -14.1), (1.0, -16.0)])

    def test_empty_input_is_empty(self) -> None:
        self.assertEqual(profile.parse_samples(""), [])


class SummarizeTestCase(unittest.TestCase):
    def test_silence_samples_are_excluded(self) -> None:
        """数字静音（-120 LUFS）混进统计会把中位数拖偏。"""
        samples = [(0.0, -120.7), (0.1, -16.0), (0.2, -16.4), (0.3, -15.0)]
        summary = profile.summarize(samples)
        self.assertEqual(summary["samples"], 3)
        self.assertAlmostEqual(summary["median"], -16.0)

    def test_loud_share_counts_time_above_threshold(self) -> None:
        samples = [(0.0, -16.0), (0.1, -22.0), (0.2, -18.0)]
        self.assertAlmostEqual(profile.summarize(samples)["loud_share"], 2 / 3)

    def test_all_silent_input_is_reported_as_no_sample(self) -> None:
        summary = profile.summarize([(0.0, -120.0)])
        self.assertEqual(summary["samples"], 0)
        self.assertIsNone(summary["median"])


class WindowTestCase(unittest.TestCase):
    def test_picks_the_loudest_windows_in_time_order(self) -> None:
        samples = [(t * 0.1, -30.0) for t in range(120)]
        samples[45] = (4.5, -10.0)      # 落在 4–8 窗口
        samples[95] = (9.5, -12.0)      # 落在 8–12 窗口
        windows = profile.window_profile(samples, window=4.0, top=2)
        self.assertEqual([item["start"] for item in windows], [4.0, 8.0])
        self.assertGreater(windows[0]["mean"], windows[1]["mean"])   # 4–8 里那一发更响

    def test_windows_are_aligned_to_whole_seconds(self) -> None:
        """窗口按 window 的整数倍对齐：多次录制之间才可比。"""
        samples = [(t * 0.1, -16.0) for t in range(200)]
        for item in profile.window_profile(samples, window=4.0, top=4):
            self.assertEqual(item["start"] % 4.0, 0.0)

    def test_windows_do_not_overlap(self) -> None:
        """相邻窗口重叠会让四个窗口落在同一段里，等于只给了一个入口。"""
        samples = [(t * 0.1, -16.0 - (t % 5)) for t in range(400)]
        windows = profile.window_profile(samples, window=4.0, top=4)
        starts = sorted(item["start"] for item in windows)
        for earlier, later in zip(starts, starts[1:]):
            self.assertGreaterEqual(later - earlier, 4.0)

    def test_top_limit_is_respected(self) -> None:
        samples = [(t * 0.1, -16.0) for t in range(1000)]
        self.assertEqual(len(profile.window_profile(samples, 4.0, 3)), 3)

    def test_no_usable_sample_yields_no_window(self) -> None:
        self.assertEqual(profile.window_profile([(0.0, -120.0)]), [])


class RenderTestCase(unittest.TestCase):
    def test_report_states_that_windows_are_not_event_times(self) -> None:
        rows = [{"file": "a.mka", "summary": {"samples": 10, "median": -16.4, "loud_share": 0.9},
                 "windows": [{"start": 68.0, "end": 72.0, "mean": -15.3}]}]
        text = profile.render(rows, 4.0)
        self.assertIn("不是事件时刻", text)
        self.assertIn("68–72", text)
        self.assertIn("-16.4 LUFS", text)

    def test_report_handles_a_file_without_samples(self) -> None:
        rows = [{"file": "a.mka", "summary": {"samples": 0, "median": None, "loud_share": None},
                 "windows": []}]
        text = profile.render(rows, 4.0)
        self.assertIn("| `a.mka` | 0 | — | — | — |", text)


if __name__ == "__main__":
    unittest.main()
