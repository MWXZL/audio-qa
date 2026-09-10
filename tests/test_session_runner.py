"""采集流水线（scripts/session_runner.py）的行为验证。

这条流水线会自动往证据目录里写文件、并按时间码截帧，所以它的三件事必须被测试挡住：
归档命名与续号不出错（否则会覆盖上一次复现）、关键帧计划的边界正确（否则会截到错的位置）、
自检判不可用时必须停下来（否则会把一段静音当成证据收进项目文档）。
"""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import session_runner  # noqa: E402


def write_wav(path: Path, seconds: float = 2.0, amplitude: float = 0.5,
              rate: int = 48000, channels: int = 2) -> Path:
    frames = int(seconds * rate)
    payload = bytearray()
    for index in range(frames):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * 1000 * index / rate))
        for _ in range(channels):
            payload += struct.pack("<h", value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return path


class NamingTestCase(unittest.TestCase):
    def test_case_dirs_are_mapped(self) -> None:
        def posix(case: str) -> str:
            return session_runner.target_dir(case, "g").as_posix()

        self.assertTrue(posix("bug_01").endswith("bug_01_concurrency"))
        self.assertTrue(posix("perf_idle").endswith("captures/perf/idle"))
        self.assertTrue(posix("compat_bt_c02").endswith("compat/compat_bt_c02"))
        self.assertTrue(posix("RC-03").endswith("retest/RC-03"))
        self.assertTrue(posix("bug_09").endswith("g/bug_09"))

    def test_index_increments_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(session_runner.next_index(directory, "bug_03"), 1)
            (directory / "raw_20260910_bug_03_r01.mkv").write_bytes(b"x")
            self.assertEqual(session_runner.next_index(directory, "bug_03"), 2)
            (directory / "raw_20260910_bug_03_r02.mkv").write_bytes(b"x")
            self.assertEqual(session_runner.next_index(directory, "bug_03"), 3)
            # 别的用例的编号不能影响本用例
            (directory / "raw_20260910_bug_04_r09.mkv").write_bytes(b"x")
            self.assertEqual(session_runner.next_index(directory, "bug_03"), 3)

    def test_clip_name_shape(self) -> None:
        name = session_runner.new_clip_name("bug_03", 2, ".mkv", datetime(2026, 9, 10))
        self.assertEqual(name, "raw_20260910_bug_03_r02.mkv")


class KeyframePlanTestCase(unittest.TestCase):
    def test_no_gaps_keeps_only_environment_frame(self) -> None:
        plan = session_runner.keyframe_plan([], duration_s=60.0)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0], "环境")
        self.assertLessEqual(plan[0][1], 60.0)

    def test_one_gap_yields_before_and_middle(self) -> None:
        gaps = [{"time_s": 12.0, "duration_ms": 300.0}]
        labels = [label for label, _ in session_runner.keyframe_plan(gaps, duration_s=30.0)]
        self.assertEqual(labels, ["环境", "间隙01_前", "间隙01_中"])

    def test_before_frame_never_negative(self) -> None:
        gaps = [{"time_s": 0.1, "duration_ms": 200.0}]
        plan = session_runner.keyframe_plan(gaps, duration_s=10.0)
        self.assertTrue(all(at >= 0.0 for _, at in plan))

    def test_middle_frame_stays_inside_recording(self) -> None:
        gaps = [{"time_s": 29.9, "duration_ms": 500.0}]
        plan = session_runner.keyframe_plan(gaps, duration_s=30.0)
        self.assertTrue(all(at <= 30.0 for _, at in plan))

    def test_frame_count_is_capped(self) -> None:
        gaps = [{"time_s": 1.0 * index, "duration_ms": 200.0} for index in range(1, 20)]
        plan = session_runner.keyframe_plan(gaps, duration_s=120.0)
        self.assertLessEqual(len(plan), session_runner.MAX_KEYFRAMES)

    def test_environment_frame_clamped_for_short_clip(self) -> None:
        plan = session_runner.keyframe_plan([], duration_s=0.4)
        self.assertLessEqual(plan[0][1], 0.4)


class ProcessOneTestCase(unittest.TestCase):
    def test_archive_selfcheck_and_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            media = write_wav(work / "take.wav", seconds=6.0)
            result = session_runner.process_one(
                media, "bug_03", "g", ffmpeg=None, dropout_min_ms=80.0, force=False,
                quiet=True, directory=work / "case",
            )
            self.assertEqual(result["verdict"], "可用")
            self.assertTrue(result["archived"].name.startswith("raw_"))
            self.assertTrue(result["archived"].name.endswith("_r01.wav"))
            self.assertIsNotNone(result["skeleton"])
            self.assertTrue((work / "case" / "measure.json").is_file())

    def test_silent_media_stops_before_measurement_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            media = write_wav(work / "silent.wav", seconds=6.0, amplitude=0.0)
            result = session_runner.process_one(
                media, "bug_03", "g", ffmpeg=None, dropout_min_ms=80.0, force=False,
                quiet=True, directory=work / "case",
            )
            self.assertEqual(result["verdict"], "不可用")
            self.assertIn("stopped", result)
            self.assertIsNone(result["skeleton"])

    def test_force_continues_on_unusable_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            media = write_wav(work / "silent.wav", seconds=6.0, amplitude=0.0)
            result = session_runner.process_one(
                media, "bug_03", "g", ffmpeg=None, dropout_min_ms=80.0, force=True,
                quiet=True, directory=work / "case",
            )
            self.assertEqual(result["verdict"], "不可用")
            self.assertIsNotNone(result["skeleton"])

    def test_second_run_does_not_overwrite_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            media = write_wav(work / "take.wav", seconds=6.0)
            first = session_runner.process_one(media, "bug_03", "g", None, 80.0, False, True, work / "case")
            second = session_runner.process_one(media, "bug_03", "g", None, 80.0, False, True, work / "case")
            self.assertNotEqual(first["archived"].name, second["archived"].name)
            self.assertTrue(second["archived"].name.endswith("_r02.wav"))
            self.assertTrue(first["archived"].is_file())


if __name__ == "__main__":
    unittest.main()
