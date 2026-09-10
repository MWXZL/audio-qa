"""采集链路自检（scripts/check_capture.py）的行为验证。

这个脚本的结论会被用来决定「要不要重录」，所以它的判定方向必须是对的：
静音必须判不可用，声道/采样率/时长问题必须给警告，干净录音不能被拦。
"""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_capture  # noqa: E402


def write_wav(path: Path, channels: int, seconds: float, rate: int = 48000,
              amplitude: float = 0.5) -> Path:
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


class CheckCaptureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verdict(self, path: Path) -> tuple[str, list[str]]:
        verdict, problems, _hints, _measure = check_capture.check(path, ffmpeg=None)
        return verdict, problems

    def test_clean_stereo_recording_is_usable(self) -> None:
        path = write_wav(self.root / "ok.wav", channels=2, seconds=6.0)
        verdict, problems = self.verdict(path)
        self.assertEqual(verdict, "可用")
        self.assertEqual(problems, [])

    def test_silent_recording_is_not_usable(self) -> None:
        path = write_wav(self.root / "silent.wav", channels=2, seconds=6.0, amplitude=0.0)
        verdict, problems = self.verdict(path)
        self.assertEqual(verdict, "不可用")
        self.assertTrue(any("静音" in item for item in problems))

    def test_mono_recording_warns_but_stays_usable(self) -> None:
        path = write_wav(self.root / "mono.wav", channels=1, seconds=6.0)
        verdict, problems = self.verdict(path)
        self.assertEqual(verdict, "有警告但可用")
        self.assertTrue(any("单声道" in item for item in problems))

    def test_wrong_sample_rate_warns(self) -> None:
        path = write_wav(self.root / "44k.wav", channels=2, seconds=6.0, rate=44100)
        _verdict, problems = self.verdict(path)
        self.assertTrue(any("44100" in item for item in problems))

    def test_short_recording_warns(self) -> None:
        path = write_wav(self.root / "short.wav", channels=2, seconds=2.0)
        _verdict, problems = self.verdict(path)
        self.assertTrue(any("太短" in item for item in problems))

    def test_quiet_recording_warns_about_level(self) -> None:
        """能录到信号但电平极低，比整段静音更隐蔽——必须单独提示。

        响度需要 ffmpeg 才能测；没有 ffmpeg 的环境直接跳过，而不是假装通过。
        """
        import audio_qa  # noqa: PLC0415

        ffmpeg = audio_qa.find_ffmpeg(None)
        if ffmpeg is None:
            self.skipTest("需要 ffmpeg 才能测量响度")
        # 振幅取 0.004：峰值约 -48 dBFS（高于 -60 的静音阈值，不会被判成静音），
        # 对应响度约 -50 LUFS（低于 -45 的「电平过低」阈值）——正好落在要测的那条判据上。
        path = write_wav(self.root / "quiet.wav", channels=2, seconds=6.0, amplitude=0.004)
        verdict, problems, _hints, _measure = check_capture.check(path, ffmpeg)
        self.assertEqual(verdict, "有警告但可用")
        self.assertTrue(any("LUFS" in item for item in problems))

    def test_missing_file_exits_two(self) -> None:
        self.assertEqual(check_capture.main([str(self.root / "nope.wav")]), 2)


if __name__ == "__main__":
    unittest.main()
