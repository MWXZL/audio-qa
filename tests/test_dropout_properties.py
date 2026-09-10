"""段内静音间隙（dropout）判据的随机化性质测试。

单测只覆盖几个手写场景；判据要面对的是真实录音里任意位置、任意长度的间隙，
所以这里用固定种子的随机用例验性质：

- 注入的每一处间隙都要被发现（位置与长度允许一个扫描窗口的量化误差）；
- 不该报的一处都不能多报（阈值高于最短间隙时全不报）；
- 头尾留白永远不算段内间隙（那是 head/tail 判据的地盘）。

随机种子写死，失败可复现；不是「跑一次通过就算」的测试。
"""
from __future__ import annotations

import random
import sys
import unittest
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio_qa  # noqa: E402

RATE = 48000
WINDOW = int(RATE * audio_qa.Thresholds.silence_window_ms / 1000.0)
FLOOR_DBFS = audio_qa.Thresholds.silence_floor_dbfs
FLOOR_AMP = int((1 << 15) * (10 ** (FLOOR_DBFS / 20.0)))


def tone(frames: int, amplitude: int = 8000) -> list[int]:
    return [amplitude if index % 2 == 0 else -amplitude for index in range(frames)]


def build_case(rng: random.Random, gap_count: int, min_ms: int, max_ms: int) -> tuple[array, list[tuple[int, int]]]:
    """拼一段信号，随机位置注入若干全零间隙，返回 (声道样点, 注入的间隙列表)。"""
    samples: list[int] = tone(int(rng.uniform(0.3, 0.8) * RATE))
    injected: list[tuple[int, int]] = []
    for _ in range(gap_count):
        samples += tone(int(rng.uniform(0.2, 0.6) * RATE))
        length = int(rng.uniform(min_ms, max_ms) / 1000.0 * RATE)
        start = len(samples)
        samples += [0] * length
        injected.append((start, length))
    samples += tone(int(rng.uniform(0.2, 0.6) * RATE))
    return array("h", samples), injected


class DropoutPropertyTestCase(unittest.TestCase):
    def detect(self, channel: array, threshold_ms: float) -> list[tuple[int, int]]:
        min_frames = max(1, int(RATE * threshold_ms / 1000.0))
        return audio_qa.internal_silence_gaps([channel], FLOOR_AMP, WINDOW, min_frames)

    def test_every_injected_gap_is_found(self) -> None:
        rng = random.Random(20260910)
        for case in range(120):
            gap_count = rng.randint(1, 4)
            channel, injected = build_case(rng, gap_count, 60, 400)
            found = self.detect(channel, threshold_ms=40)
            self.assertEqual(len(found), len(injected), f"第 {case} 例：间隙数不符")
            for (found_start, found_len), (want_start, want_len) in zip(found, injected):
                self.assertLessEqual(abs(found_start - want_start), WINDOW,
                                     f"第 {case} 例：起始位置偏差超过一个扫描窗口")
                self.assertLessEqual(abs(found_len - want_len), 2 * WINDOW,
                                     f"第 {case} 例：长度偏差超过两个扫描窗口")

    def test_threshold_above_shortest_gap_reports_nothing(self) -> None:
        rng = random.Random(4242)
        for case in range(40):
            channel, injected = build_case(rng, 2, 50, 90)
            found = self.detect(channel, threshold_ms=200)
            self.assertEqual(found, [], f"第 {case} 例：阈值高于所有间隙却仍报了 {len(found)} 处")

    def test_edges_are_never_reported_as_gaps(self) -> None:
        rng = random.Random(7)
        for case in range(40):
            head = [0] * int(rng.uniform(0.2, 0.6) * RATE)
            body = tone(int(rng.uniform(0.5, 1.0) * RATE))
            tail = [0] * int(rng.uniform(0.2, 0.6) * RATE)
            channel = array("h", head + body + tail)
            self.assertEqual(self.detect(channel, threshold_ms=40), [],
                             f"第 {case} 例：头尾留白被误报为段内间隙")

    def test_two_channels_silent_together_is_one_gap(self) -> None:
        """全声道同时静音才算断流：两个声道同位置注入，只应得到一处间隙。"""
        rng = random.Random(99)
        channel, injected = build_case(rng, 1, 120, 200)
        found = self.detect(channel, threshold_ms=40)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(injected), 1)

    def test_single_channel_dropout_is_not_reported(self) -> None:
        """只有一个声道哑掉时不算段内断流（那是另一类问题）。"""
        rng = random.Random(1234)
        left, injected = build_case(rng, 1, 150, 250)
        right = array("h", tone(len(left)))
        min_frames = max(1, int(RATE * 40 / 1000.0))
        found = audio_qa.internal_silence_gaps([left, right], FLOOR_AMP, WINDOW, min_frames)
        self.assertEqual(found, [])
        self.assertEqual(len(injected), 1)


if __name__ == "__main__":
    unittest.main()
