"""脚步与材质切换分析（scripts/footstep_analysis.py）的验证。

它要把「落脚时刻」和「材质切换点」量出来，用来判 bug_02 的两条判据
（材质变化与落地同拍、跨边界最多过渡一步）——所以既要有纯逻辑测试，
也要有一条**已知答案的端到端**：合成「石板（高频脆）× 5 步 → 雪地（低频闷）× 5 步」，
断言检出的落脚时刻与切换步号就是设定值。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import footstep_analysis as fa  # noqa: E402

RATE = fa.SAMPLE_RATE


def click(at_s: float, kind: str, amplitude: float = 0.6, length_s: float = 0.12) -> np.ndarray:
    """一段合成的脚步：石板=带高频的短促脉冲；雪地=低频闷响 + 快速衰减。"""
    count = int(length_s * RATE)
    t = np.arange(count) / RATE
    rng = np.random.default_rng(abs(hash(kind)) % (2 ** 31))
    noise = rng.normal(0, 1, count)
    if kind == "stone":
        envelope = np.exp(-t * 60)
        tone = np.sin(2 * np.pi * 2200 * t) * 0.6 + noise * 0.8
    else:                                   # snow
        envelope = np.exp(-t * 25)
        tone = np.sin(2 * np.pi * 180 * t) + noise * 0.15
    return amplitude * envelope * tone


def build_steps(times: list[float], kinds: list[str], total_s: float = 8.0) -> np.ndarray:
    signal = np.zeros(int(total_s * RATE))
    for at, kind in zip(times, kinds):
        start = int(at * RATE)
        chunk = click(at, kind)
        signal[start:start + len(chunk)] += chunk[:len(signal) - start]
    return signal


class OnsetTestCase(unittest.TestCase):
    def test_envelope_peaks_at_the_burst(self) -> None:
        signal = build_steps([1.0], ["stone"])
        envelope = fa.onset_envelope(signal)
        peak_frame = int(np.argmax(envelope))
        self.assertAlmostEqual(peak_frame * fa.HOP_MS / 1000.0, 1.0, delta=0.05)

    def test_onsets_are_found_at_the_right_times(self) -> None:
        times = [1.0, 1.5, 2.0, 2.5, 3.0]
        signal = build_steps(times, ["stone"] * 5)
        onsets = fa.pick_onsets(fa.onset_envelope(signal))
        self.assertEqual(len(onsets), len(times))
        for found, expected in zip(onsets, times):
            self.assertAlmostEqual(found, expected, delta=0.05)

    def test_min_gap_suppresses_double_hits(self) -> None:
        times = [1.0, 1.02, 1.5]                     # 前两次太近，算一次
        signal = build_steps(times, ["stone"] * 3)
        onsets = fa.pick_onsets(fa.onset_envelope(signal), min_gap_ms=150.0)
        self.assertEqual(len(onsets), 2)

    def test_silence_yields_no_onset(self) -> None:
        self.assertEqual(fa.pick_onsets(fa.onset_envelope(np.zeros(RATE * 2))), [])


class FeatureTestCase(unittest.TestCase):
    def test_stone_and_snow_differ_in_centroid_and_low_ratio(self) -> None:
        signal = build_steps([1.0, 2.0], ["stone", "snow"])
        rows = fa.step_features(signal, [1.0, 2.0])
        stone, snow = rows
        self.assertGreater(stone["centroid_hz"], snow["centroid_hz"])
        self.assertGreater(snow["low_ratio"], stone["low_ratio"])
        self.assertGreater(fa.feature_distance(stone, snow), 0.5)

    def test_distance_is_small_within_the_same_material(self) -> None:
        signal = build_steps([1.0, 1.5], ["stone", "stone"])
        rows = fa.step_features(signal, [1.0, 1.5])
        self.assertLess(fa.feature_distance(rows[0], rows[1]), 0.3)

    def test_short_chunk_is_zero_padded_not_crashed(self) -> None:
        rows = fa.step_features(np.zeros(RATE), [0.99])
        self.assertEqual(len(rows), 1)
        self.assertIn("centroid_hz", rows[0])


class SegmentTestCase(unittest.TestCase):
    def features(self, kinds: list[str]) -> list[dict[str, float]]:
        times = [1.0 + 0.6 * index for index in range(len(kinds))]
        signal = build_steps(times, kinds)
        return fa.step_features(signal, times)

    def test_material_change_splits_into_two_segments(self) -> None:
        rows = self.features(["stone"] * 5 + ["snow"] * 5)
        segments = fa.material_segments(rows)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["steps"], 5)
        self.assertEqual(segments[1]["boundary_step"], 6)     # 第 6 步落到新材质

    def test_no_change_keeps_one_segment(self) -> None:
        rows = self.features(["stone"] * 8)
        self.assertEqual(len(fa.material_segments(rows)), 1)

    def test_clean_boundary_has_no_transition_step(self) -> None:
        rows = self.features(["stone"] * 5 + ["snow"] * 5)
        segments = fa.material_segments(rows)
        transitions = fa.transition_steps(rows, segments)
        self.assertEqual([row["boundary_step"] for row in transitions], [6])
        self.assertFalse(transitions[0]["is_transition"])

    def test_mixed_step_is_flagged_as_transition(self) -> None:
        """混合音色的一步（一半旧一半新）必须被数成过渡脚步。"""
        rows = self.features(["stone"] * 5 + ["snow"] * 5)
        stone, snow = rows[4], rows[6]
        rows[5] = {
            "onset": rows[5]["onset"], "peak": 0.5,
            # 质心取两段的几何中点（对数域正中），低频与衰减取算术平均 → 典型的「混合」
            "centroid_hz": math.sqrt(stone["centroid_hz"] * snow["centroid_hz"]),
            "low_ratio": (stone["low_ratio"] + snow["low_ratio"]) / 2,
            "high_ratio": (stone["high_ratio"] + snow["high_ratio"]) / 2,
            "decay": (stone["decay"] + snow["decay"]) / 2,
        }
        segments = fa.material_segments(rows)
        transitions = fa.transition_steps(rows, segments)
        self.assertTrue(transitions, "切点应被检出")
        self.assertTrue(transitions[0]["is_transition"],
                        f"混合的一步应判为过渡：{transitions[0]}")

    def test_too_few_steps_yields_no_segment(self) -> None:
        self.assertEqual(fa.material_segments(self.features(["stone"] * 3)), [])


class EndToEndTestCase(unittest.TestCase):
    def test_known_answer_material_change(self) -> None:
        """已知答案：5 步石板 + 5 步雪地，落脚在 1.0 s 起每 0.6 s 一次。"""
        times = [1.0 + 0.6 * index for index in range(10)]
        kinds = ["stone"] * 5 + ["snow"] * 5
        signal = build_steps(times, kinds, total_s=7.5)
        features = fa.step_features(signal, times)
        segments = fa.material_segments(features)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1]["boundary_step"], 6)
        self.assertAlmostEqual(segments[1]["start_s"], times[5], delta=0.05)
        transitions = fa.transition_steps(features, segments)
        self.assertEqual(sum(1 for row in transitions if row["is_transition"]), 0)


class RenderTestCase(unittest.TestCase):
    def test_report_states_what_it_cannot_cover(self) -> None:
        result = {"file": "a.mka", "duration_s": 7.5, "sample_rate": RATE, "hop_ms": 5.0,
                  "steps": 0, "features": [], "step_gaps_s": [], "median_gap_s": None,
                  "segments": [], "transition_steps": []}
        args = fa.build_parser().parse_args(["a.mka"])
        text = fa.render(result, args)
        self.assertIn("不覆盖什么", text)
        self.assertIn("材质名字", text)
        self.assertIn("未检出材质切换", text)

    def test_report_mentions_transition_count(self) -> None:
        rows = [{"onset": 1.0 + 0.6 * i, "peak": 0.5, "centroid_hz": 2000.0,
                 "low_ratio": 0.1, "high_ratio": 0.3, "decay": 0.2} for i in range(10)]
        for index in range(5, 10):
            rows[index]["centroid_hz"] = 300.0
            rows[index]["low_ratio"] = 0.5
        segments = fa.material_segments(rows)
        result = {"file": "a.mka", "duration_s": 7.5, "sample_rate": RATE, "hop_ms": 5.0,
                  "steps": 10, "features": rows, "step_gaps_s": [], "median_gap_s": 0.6,
                  "segments": segments,
                  "transition_steps": fa.transition_steps(rows, segments)}
        args = fa.build_parser().parse_args(["a.mka"])
        self.assertIn("过渡脚步", fa.render(result, args))


if __name__ == "__main__":
    unittest.main()
