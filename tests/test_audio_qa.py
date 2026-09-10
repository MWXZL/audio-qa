# -*- coding: utf-8 -*-
"""audio_qa 单元测试

全部用合成 WAV 做确定性验证：每个用例现场造一个带已知缺陷的文件，
断言对应检测项必须命中、且不该命中的项不误报。
不依赖 ffmpeg、不依赖网络，`python -m unittest` 可离线复现。
"""
from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_qa  # noqa: E402
from audio_qa import Thresholds  # noqa: E402

SAMPLE_RATE = 48000


# --------------------------------------------------------------------------
# 合成素材工具
# --------------------------------------------------------------------------
def sine(
    duration_s: float,
    amplitude: float,
    freq: float = 1000.0,
    sample_rate: int = SAMPLE_RATE,
    full_scale: int = 1 << 15,
) -> list[int]:
    """生成正弦样点。amplitude 为满刻度比例，可 >1 用于制造削波。"""
    count = int(duration_s * sample_rate)
    peak = full_scale - 1
    out = []
    for index in range(count):
        value = amplitude * peak * math.sin(2 * math.pi * freq * index / sample_rate)
        out.append(int(max(-full_scale, min(peak, round(value)))))
    return out


def silence(duration_s: float, sample_rate: int = SAMPLE_RATE) -> list[int]:
    return [0] * int(duration_s * sample_rate)


def decay(
    duration_s: float,
    start_amplitude: float,
    end_amplitude: float,
    freq: float = 1000.0,
    sample_rate: int = SAMPLE_RATE,
    full_scale: int = 1 << 15,
) -> list[int]:
    """指数衰减的正弦，用来模拟残响尾巴。

    末尾电平落到 end_amplitude（可低于 -60 dBFS 门限），但样点不是零——
    这正是「低于门限」与「可无损剪掉」的区别所在。
    """
    count = int(duration_s * sample_rate)
    peak = full_scale - 1
    out = []
    for index in range(count):
        ratio = index / max(1, count - 1)
        env = start_amplitude * (end_amplitude / start_amplitude) ** ratio
        value = env * peak * math.sin(2 * math.pi * freq * index / sample_rate)
        sample = int(max(-full_scale, min(peak, round(value))))
        # 保证不出现整段真零：残响再小也有 ±1 LSB 的抖动
        out.append(sample if sample != 0 else (1 if index % 2 else -1))
    return out


def write_wav(
    path: Path,
    channels: list[list[int]],
    sample_rate: int = SAMPLE_RATE,
    sampwidth: int = 2,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = min(len(chan) for chan in channels)
    chunks = bytearray()
    for index in range(frames):
        for chan in channels:
            value = chan[index]
            if sampwidth == 1:
                chunks += bytes(((value + 128) & 0xFF,))
            elif sampwidth == 2:
                chunks += struct.pack("<h", value)
            elif sampwidth == 3:
                chunks += int(value).to_bytes(3, "little", signed=True)
            elif sampwidth == 4:
                chunks += struct.pack("<i", value)
            else:
                raise ValueError(sampwidth)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(len(channels))
        handle.setsampwidth(sampwidth)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(chunks))
    return path


class AudioQATestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Thresholds()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # 只跑单文件分析，不走 ffmpeg，也不做库级检查
    def analyze(self, path: Path, cfg: Thresholds | None = None) -> audio_qa.FileResult:
        return audio_qa.analyze_file(path, self.root, cfg or self.cfg, ffmpeg=None, do_loudness=False)

    def checks(self, result: audio_qa.FileResult) -> set[str]:
        return {issue.check for issue in result.issues}

    def severity_of(self, result: audio_qa.FileResult, check: str) -> str:
        for issue in result.issues:
            if issue.check == check:
                return issue.severity
        self.fail(f"未命中检查项 {check}，实际命中 {self.checks(result)}")

    def scan(self, cfg: Thresholds | None = None) -> dict:
        return audio_qa.scan(
            root=self.root,
            cfg=cfg or self.cfg,
            ffmpeg=None,
            jobs=2,
            do_loudness=False,
            exts=[".wav"],
            progress=False,
        )


# --------------------------------------------------------------------------
# 基线：干净文件不得误报
# --------------------------------------------------------------------------
class TestCleanAsset(AudioQATestCase):
    def test_clean_sine_passes(self) -> None:
        path = write_wav(self.root / "clean.wav", [sine(1.0, 0.5)])
        result = self.analyze(path)
        self.assertEqual(result.status, "PASS", f"干净文件被误报：{self.checks(result)}")
        self.assertEqual(result.sample_rate, SAMPLE_RATE)
        self.assertEqual(result.channels, 1)
        self.assertEqual(result.bit_depth, 16)
        self.assertAlmostEqual(result.duration_s, 1.0, places=3)
        self.assertAlmostEqual(result.peak_dbfs, -6.02, delta=0.1)
        self.assertLess(abs(result.dc_offset), 1e-3)
        self.assertEqual(result.decoded_via, "wave")

    def test_true_stereo_not_flagged_as_fake(self) -> None:
        left = sine(1.0, 0.5, freq=440)
        right = sine(1.0, 0.5, freq=660)
        path = write_wav(self.root / "stereo.wav", [left, right])
        self.assertNotIn("fake_stereo", self.checks(self.analyze(path)))


# --------------------------------------------------------------------------
# 削波
# --------------------------------------------------------------------------
class TestClipping(AudioQATestCase):
    def test_clipped_sine_reports_fail_with_timestamp(self) -> None:
        path = write_wav(self.root / "clipped.wav", [sine(1.0, 1.6)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "clipping"), "FAIL")
        issue = next(i for i in result.issues if i.check == "clipping")
        self.assertGreater(len(issue.detail["runs"]), 0)
        self.assertGreater(issue.detail["full_scale_samples"], 100)
        first = issue.detail["runs"][0]
        self.assertIn("time_s", first)
        self.assertGreaterEqual(first["samples"], self.cfg.clip_min_run)

    def test_isolated_full_scale_sample_is_warn_not_fail(self) -> None:
        samples = sine(0.5, 0.5)
        samples[1000] = 32767  # 单点触顶，够不上连续 3 点的削波判据
        path = write_wav(self.root / "spike.wav", [samples])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "clipping"), "WARN")

    def test_clip_min_run_threshold_is_respected(self) -> None:
        samples = sine(0.5, 0.5)
        samples[2000:2004] = [32767] * 4
        path = write_wav(self.root / "run4.wav", [samples])
        self.assertEqual(self.severity_of(self.analyze(path), "clipping"), "FAIL")
        loose = Thresholds(clip_min_run=8)
        self.assertEqual(self.severity_of(self.analyze(path, loose), "clipping"), "WARN")

    def test_negative_full_scale_is_detected(self) -> None:
        samples = sine(0.5, 0.5)
        samples[3000:3005] = [-32768] * 5
        path = write_wav(self.root / "negclip.wav", [samples])
        self.assertEqual(self.severity_of(self.analyze(path), "clipping"), "FAIL")


# --------------------------------------------------------------------------
# 直流偏移
# --------------------------------------------------------------------------
class TestDCOffset(AudioQATestCase):
    def test_dc_offset_fails(self) -> None:
        bias = int(0.05 * 32767)
        samples = [min(32767, value + bias) for value in sine(1.0, 0.3)]
        path = write_wav(self.root / "dc.wav", [samples])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "dc_offset"), "FAIL")
        self.assertAlmostEqual(result.dc_offset, 0.05, delta=0.01)

    def test_dc_offset_below_threshold_passes(self) -> None:
        bias = int(0.005 * 32767)
        samples = [value + bias for value in sine(1.0, 0.3)]
        path = write_wav(self.root / "dc_small.wav", [samples])
        self.assertNotIn("dc_offset", self.checks(self.analyze(path)))


# --------------------------------------------------------------------------
# 静音相关
# --------------------------------------------------------------------------
class TestSilence(AudioQATestCase):
    def test_fully_silent_file_fails(self) -> None:
        path = write_wav(self.root / "empty_room.wav", [silence(1.0)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "silent_file"), "FAIL")

    def test_head_silence_fails_and_is_measured(self) -> None:
        path = write_wav(self.root / "late.wav", [silence(0.3) + sine(0.7, 0.5)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "head_silence"), "FAIL")
        self.assertAlmostEqual(result.head_silence_ms, 300, delta=15)

    def test_long_stem_head_offset_is_info_not_fail(self) -> None:
        """同样的头部空白，放在音乐分轨上是声部进入位置，不是触发延迟。

        对照 Cube 官方素材：Story-Main 主轨头部 0 ms，同一条 66.67 s 里
        Cello1 头部 16.1 s（90 bpm 4/4 的第 6 小节）。
        """
        path = write_wav(self.root / "stem_90bpm4-4_L8M.wav", [silence(2.0) + sine(8.0, 0.5)])
        result = self.analyze(path)
        checks = self.checks(result)
        self.assertNotIn("head_silence", checks)
        self.assertEqual(self.severity_of(result, "head_offset"), "INFO")
        self.assertAlmostEqual(result.head_silence_ms, 2000, delta=20)

    def test_oneshot_boundary_is_configurable(self) -> None:
        """把一次性音效上限抬高，同一条素材就重新按音效判 FAIL——分界线是判据参数。"""
        path = write_wav(self.root / "stem.wav", [silence(2.0) + sine(8.0, 0.5)])
        loose = Thresholds(oneshot_max_duration_s=30.0)
        result = self.analyze(path, loose)
        self.assertEqual(self.severity_of(result, "head_silence"), "FAIL")

    def test_tail_zero_padding_warns(self) -> None:
        path = write_wav(self.root / "trailing.wav", [sine(0.5, 0.5) + silence(1.2)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "tail_silence"), "WARN")
        self.assertAlmostEqual(result.tail_silence_ms, 1200, delta=20)
        self.assertAlmostEqual(result.tail_zero_ms, 1200, delta=20)

    def test_reverb_tail_is_info_not_warn(self) -> None:
        """尾部低于 -60 dBFS 但不是零：是自然衰减，剪掉会硬切爆音。

        对照 Cube 官方素材：37 条原判 tail_silence 里 28 条尾部峰值落在
        -60.2 ~ -61.7 dBFS，说明 -60 dB 门限是从衰减曲线中间切了一刀。
        """
        path = write_wav(
            self.root / "reverb.wav",
            [sine(0.5, 0.5) + decay(1.5, 0.5, 0.000005)],
        )
        result = self.analyze(path)
        checks = self.checks(result)
        self.assertNotIn("tail_silence", checks)
        self.assertEqual(self.severity_of(result, "tail_decay"), "INFO")
        self.assertGreater(result.tail_silence_ms, 500)
        self.assertLess(result.tail_zero_ms, 500)

    def test_tail_zero_measured_separately_from_floor(self) -> None:
        """衰减 + 零填充并存时，只把真零那段算成可剪掉的浪费。"""
        path = write_wav(
            self.root / "both.wav",
            [sine(0.5, 0.5) + decay(1.5, 0.5, 0.000005) + silence(0.9)],
        )
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "tail_silence"), "WARN")
        self.assertAlmostEqual(result.tail_zero_ms, 900, delta=20)
        # 低于门限的那段远长于真零段：判据必须只把真零算成可剪掉的浪费
        self.assertGreater(result.tail_silence_ms, result.tail_zero_ms + 300)

    def test_short_edges_pass(self) -> None:
        path = write_wav(self.root / "tight.wav", [silence(0.02) + sine(0.5, 0.5) + silence(0.1)])
        checks = self.checks(self.analyze(path))
        self.assertNotIn("head_silence", checks)
        self.assertNotIn("tail_silence", checks)

    def test_one_silent_channel_fails(self) -> None:
        path = write_wav(self.root / "half_dead.wav", [sine(1.0, 0.5), silence(1.0)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "channel_silent"), "FAIL")
        issue = next(i for i in result.issues if i.check == "channel_silent")
        self.assertEqual(issue.detail["silent_channels"], [1])

    # 段内静音间隙（dropout）：默认关闭，仅在显式给下限时启用
    def test_dropout_check_is_off_by_default(self) -> None:
        """素材里的静音常是设计的一部分，默认报就会把整个素材库变成假 WARN 库。"""
        path = write_wav(self.root / "take.wav", [sine(0.2, 0.5) + silence(0.3) + sine(0.2, 0.5)])
        result = self.analyze(path)
        self.assertNotIn("silence_gap", self.checks(result))
        self.assertEqual(result.dropout_count, 0)
        self.assertIsNone(result.dropout_max_ms)

    def test_dropout_gap_is_reported_with_position(self) -> None:
        cfg = Thresholds(dropout_min_ms=100.0)
        path = write_wav(self.root / "run.wav", [sine(0.5, 0.5) + silence(0.3) + sine(0.5, 0.5)])
        result = self.analyze(path, cfg)
        self.assertEqual(self.severity_of(result, "silence_gap"), "WARN")
        self.assertEqual(result.dropout_count, 1)
        self.assertAlmostEqual(result.dropout_max_ms, 300, delta=15)
        self.assertAlmostEqual(result.dropout_total_ms, 300, delta=15)
        issue = next(i for i in result.issues if i.check == "silence_gap")
        self.assertAlmostEqual(issue.detail["gaps"][0]["time_s"], 0.5, delta=0.02)

    def test_dropout_shorter_than_threshold_is_ignored(self) -> None:
        cfg = Thresholds(dropout_min_ms=200.0)
        path = write_wav(self.root / "short.wav", [sine(0.2, 0.5) + silence(0.05) + sine(0.2, 0.5)])
        self.assertNotIn("silence_gap", self.checks(self.analyze(path, cfg)))

    def test_edges_are_not_counted_as_dropouts(self) -> None:
        """头尾留白归 head / tail 判据，同一次静音不能既报 head_silence 又报 silence_gap。"""
        cfg = Thresholds(dropout_min_ms=100.0)
        path = write_wav(self.root / "edges.wav", [silence(0.5) + sine(0.5, 0.5) + silence(0.6)])
        result = self.analyze(path, cfg)
        self.assertNotIn("silence_gap", self.checks(result))
        self.assertEqual(result.dropout_count, 0)

    def test_dropout_requires_all_channels_silent(self) -> None:
        """单声道中途哑掉是另一类问题，不能记成整段断流。"""
        cfg = Thresholds(dropout_min_ms=100.0)
        loud = sine(0.5, 0.5)
        path = write_wav(
            self.root / "one_channel.wav",
            [loud + silence(0.3) + loud, sine(1.3, 0.5)],
        )
        result = self.analyze(path, cfg)
        self.assertNotIn("silence_gap", self.checks(result))


# --------------------------------------------------------------------------
# loop 接缝
# --------------------------------------------------------------------------
class TestLoop(AudioQATestCase):
    def test_loop_named_file_with_step_fails(self) -> None:
        samples = sine(1.0, 0.5)
        samples[-1] = 20000  # 尾样点远离首样点，循环处产生跳变
        path = write_wav(self.root / "ambient_loop.wav", [samples])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "loop_discontinuity"), "FAIL")

    def test_same_step_on_non_loop_file_is_info(self) -> None:
        samples = sine(1.0, 0.5)
        samples[-1] = 20000
        path = write_wav(self.root / "oneshot.wav", [samples])
        self.assertEqual(self.severity_of(self.analyze(path), "loop_discontinuity"), "INFO")

    def test_loop_name_variants_recognised(self) -> None:
        for stem in ("bgm_loop", "loop_wind", "rain-loop", "engine.loop", "fire_loop_02"):
            self.assertTrue(audio_qa.looks_like_loop(Path(f"{stem}.wav")), stem)
        for stem in ("looper_tool", "developer", "bloop"):
            self.assertFalse(audio_qa.looks_like_loop(Path(f"{stem}.wav")), stem)

    def test_seamless_loop_passes(self) -> None:
        # 整数周期正弦，首尾都在零点附近
        path = write_wav(self.root / "clean_loop.wav", [sine(1.0, 0.5, freq=1000)])
        self.assertNotIn("loop_discontinuity", self.checks(self.analyze(path)))


# --------------------------------------------------------------------------
# 声道冗余
# --------------------------------------------------------------------------
class TestFakeStereo(AudioQATestCase):
    def test_identical_channels_warn(self) -> None:
        samples = sine(1.0, 0.5)
        path = write_wav(self.root / "fake.wav", [samples, list(samples)])
        result = self.analyze(path)
        self.assertEqual(self.severity_of(result, "fake_stereo"), "WARN")
        issue = next(i for i in result.issues if i.check == "fake_stereo")
        self.assertGreater(issue.detail["redundant_bytes"], 0)


# --------------------------------------------------------------------------
# 解码正确性：位深与声道拆分
# --------------------------------------------------------------------------
class TestDecoding(AudioQATestCase):
    def test_24bit_peak_is_correct(self) -> None:
        full = 1 << 23
        samples = sine(0.5, 0.5, full_scale=full)
        path = write_wav(self.root / "b24.wav", [samples], sampwidth=3)
        result = self.analyze(path)
        self.assertEqual(result.bit_depth, 24)
        self.assertAlmostEqual(result.peak_dbfs, -6.02, delta=0.1)
        self.assertLess(abs(result.dc_offset), 1e-3)

    def test_24bit_clipping_detected(self) -> None:
        full = 1 << 23
        samples = sine(0.5, 0.5, full_scale=full)
        samples[1000:1006] = [full - 1] * 6
        path = write_wav(self.root / "b24_clip.wav", [samples], sampwidth=3)
        self.assertEqual(self.severity_of(self.analyze(path), "clipping"), "FAIL")

    def test_32bit_peak_is_correct(self) -> None:
        full = 1 << 31
        samples = sine(0.2, 0.5, full_scale=full)
        path = write_wav(self.root / "b32.wav", [samples], sampwidth=4)
        result = self.analyze(path)
        self.assertEqual(result.bit_depth, 32)
        self.assertAlmostEqual(result.peak_dbfs, -6.02, delta=0.1)

    def test_8bit_unsigned_roundtrip(self) -> None:
        samples = sine(0.2, 0.5, full_scale=128)
        path = write_wav(self.root / "b8.wav", [samples], sampwidth=1)
        result = self.analyze(path)
        self.assertEqual(result.bit_depth, 8)
        self.assertAlmostEqual(result.peak_dbfs, -6.02, delta=0.6)
        self.assertLess(abs(result.dc_offset), 5e-3)

    def test_channel_deinterleave_keeps_channels_apart(self) -> None:
        left = sine(0.3, 0.8, freq=440)
        right = sine(0.3, 0.2, freq=440)
        path = write_wav(self.root / "levels.wav", [left, right])
        audio = audio_qa.load_audio(path, ffmpeg=None)
        self.assertEqual(audio.channels, 2)
        left_peak = audio_qa.to_dbfs(audio_qa.channel_peak(audio.chans[0]), audio.full_scale)
        right_peak = audio_qa.to_dbfs(audio_qa.channel_peak(audio.chans[1]), audio.full_scale)
        self.assertAlmostEqual(left_peak, -1.94, delta=0.15)
        self.assertAlmostEqual(right_peak, -13.98, delta=0.15)


# --------------------------------------------------------------------------
# 资产库级检查
# --------------------------------------------------------------------------
class TestLibraryChecks(AudioQATestCase):
    def _dupe_group(self, report: dict, *paths: str) -> dict:
        wanted = sorted(paths)
        for group in report["library"]["duplicate_groups"]:
            if group["paths"] == wanted:
                return group
        self.fail(f"未找到重复组 {wanted}，实际 {report['library']['duplicate_groups']}")

    def test_same_name_across_dirs_is_redundant_copy(self) -> None:
        """同名文件散落在两个目录：真冗余，删一份改引用，计入可回收空间。"""
        samples = sine(0.3, 0.5)
        write_wav(self.root / "sfx" / "hit.wav", [samples])
        write_wav(self.root / "sfx" / "story" / "hit.wav", [samples])
        write_wav(self.root / "sfx" / "other.wav", [sine(0.3, 0.5, freq=700)])
        report = self.scan()
        group = self._dupe_group(report, "sfx/hit.wav", "sfx/story/hit.wav")
        self.assertEqual(group["kind"], "redundant_copy")
        self.assertEqual(group["severity"], "WARN")
        self.assertTrue(group["identical_bytes"])
        self.assertGreater(group["recoverable_bytes"], 0)
        self.assertEqual(
            report["library"]["duplicate_wasted_bytes"], group["recoverable_bytes"]
        )
        flagged = [
            item["path"]
            for item in report["files"]
            if any(i["check"] == "redundant_copy" for i in item["issues"])
        ]
        self.assertEqual(flagged, ["sfx/story/hit.wav"])

    def test_enharmonic_names_are_info_not_waste(self) -> None:
        """E#5 与 F5 是同一个音，采样器按音名索引，删掉就取不到音。"""
        samples = sine(0.3, 0.5)
        write_wav(self.root / "sfx" / "Suling_E#5.wav", [samples])
        write_wav(self.root / "sfx" / "Suling_F5.wav", [samples])
        report = self.scan()
        group = self._dupe_group(report, "sfx/Suling_E#5.wav", "sfx/Suling_F5.wav")
        self.assertEqual(group["kind"], "equivalent_naming")
        self.assertEqual(group["severity"], "INFO")
        self.assertEqual(group["recoverable_bytes"], 0)
        self.assertEqual(report["library"]["duplicate_wasted_bytes"], 0)

    def test_meter_annotation_difference_is_info(self) -> None:
        """只差 bpm/拍号/小节标注的音乐分轨，内容相同属预期。"""
        samples = sine(0.3, 0.5)
        write_wav(self.root / "music" / "Bridge-Gtr1_138bpm3-4_L17M-P0.wav", [samples])
        write_wav(self.root / "music" / "Bridge-Gtr1_138bpm4-4_L17M-P1B.wav", [samples])
        report = self.scan()
        group = self._dupe_group(
            report,
            "music/Bridge-Gtr1_138bpm3-4_L17M-P0.wav",
            "music/Bridge-Gtr1_138bpm4-4_L17M-P1B.wav",
        )
        self.assertEqual(group["kind"], "equivalent_naming")
        self.assertEqual(group["severity"], "INFO")

    def test_different_parts_same_content_warns_as_export_defect(self) -> None:
        """命名表示不同声部却内容相同：导出漏了声部，修法是重导出而非删文件。"""
        samples = sine(0.3, 0.5)
        write_wav(self.root / "music" / "Bridge-Gtr2_138bpm4-4_L17M-P1B.wav", [samples])
        write_wav(self.root / "music" / "Bridge-Gtr3_138bpm4-4_L17M-P1B.wav", [samples])
        report = self.scan()
        group = self._dupe_group(
            report,
            "music/Bridge-Gtr2_138bpm4-4_L17M-P1B.wav",
            "music/Bridge-Gtr3_138bpm4-4_L17M-P1B.wav",
        )
        self.assertEqual(group["kind"], "duplicate_asset")
        self.assertEqual(group["severity"], "WARN")
        # 内容相同不等于可回收：删掉丢的是「本该有另一个声部」这个信息
        self.assertEqual(group["recoverable_bytes"], 0)
        self.assertEqual(report["library"]["duplicate_wasted_bytes"], 0)

    def test_canonical_name_strips_only_identity_neutral_tokens(self) -> None:
        canon = audio_qa.canonical_asset_name
        self.assertEqual(
            canon("SFX/Suling_E#5.wav"), canon("SFX/Suling_F5.wav")
        )
        self.assertEqual(
            canon("a/Main_138bpm3-4_L17M-P0.wav"), canon("b/Main_138bpm4-4_L17M-P1B.wav")
        )
        # 声部编号不是标注，剥不掉
        self.assertNotEqual(
            canon("Gtr2_138bpm4-4_L17M-P1B.wav"), canon("Gtr3_138bpm4-4_L17M-P1B.wav")
        )

    def test_sample_rate_outlier_flagged(self) -> None:
        for name in ("a", "b", "c"):
            write_wav(self.root / f"{name}.wav", [sine(0.2, 0.5)], sample_rate=48000)
        write_wav(self.root / "hi.wav", [sine(0.2, 0.5, sample_rate=96000)], sample_rate=96000)
        report = self.scan()
        outliers = report["library"]["format_outliers"]
        self.assertEqual([item["path"] for item in outliers], ["hi.wav"])
        self.assertEqual(outliers[0]["severity"], "WARN")

    def test_uniform_library_has_no_format_issue(self) -> None:
        for name in ("a", "b"):
            write_wav(self.root / f"{name}.wav", [sine(0.2, 0.5)])
        report = self.scan()
        self.assertEqual(report["library"]["format_outliers"], [])
        self.assertEqual(len(report["library"]["formats"]), 1)

    def test_summary_counts_and_exit_signal(self) -> None:
        write_wav(self.root / "ok.wav", [sine(0.3, 0.5)])
        write_wav(self.root / "bad.wav", [sine(0.3, 1.6)])
        report = self.scan()
        self.assertEqual(report["summary"]["files"], 2)
        self.assertEqual(report["summary"]["fail"], 1)
        self.assertEqual(report["summary"]["pass"], 1)

    def test_report_is_json_serialisable(self) -> None:
        write_wav(self.root / "x.wav", [sine(0.2, 1.6)])
        write_wav(self.root / "y.wav", [silence(0.2)])
        report = self.scan()
        text = json.dumps(report, ensure_ascii=False)
        self.assertIn("audio_qa", text)
        # -inf 必须落成 null，否则不是合法 JSON
        self.assertNotIn("Infinity", text)

    def test_markdown_report_renders(self) -> None:
        write_wav(self.root / "x.wav", [sine(0.2, 1.6)])
        markdown = audio_qa.render_scan_markdown(self.scan())
        self.assertIn("# 音频资产质量检查报告", markdown)
        self.assertIn("FAIL 明细", markdown)
        self.assertIn("clipping", markdown)


# --------------------------------------------------------------------------
# 损坏文件不能中断整轮扫描
# --------------------------------------------------------------------------
class TestRobustness(AudioQATestCase):
    def test_broken_file_reported_not_raised(self) -> None:
        write_wav(self.root / "good.wav", [sine(0.2, 0.5)])
        (self.root / "broken.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
        report = self.scan()
        self.assertEqual(report["summary"]["files"], 2)
        broken = next(item for item in report["files"] if item["path"] == "broken.wav")
        self.assertEqual(broken["status"], "FAIL")
        self.assertIsNotNone(broken["error"])
        good = next(item for item in report["files"] if item["path"] == "good.wav")
        self.assertEqual(good["status"], "PASS")

    def test_empty_directory_raises(self) -> None:
        with self.assertRaises(audio_qa.AudioQAError):
            self.scan()


# --------------------------------------------------------------------------
# 回归比对
# --------------------------------------------------------------------------
class TestCompare(AudioQATestCase):
    def _baseline_and_current(self) -> tuple[dict, dict]:
        write_wav(self.root / "keep.wav", [sine(0.3, 0.5)])
        write_wav(self.root / "changes.wav", [sine(0.3, 0.5, freq=500)])
        write_wav(self.root / "gone.wav", [sine(0.3, 0.5, freq=800)])
        baseline = self.scan()
        (self.root / "gone.wav").unlink()
        write_wav(self.root / "changes.wav", [sine(0.3, 0.05, freq=500)])  # 电平掉 20 dB
        write_wav(self.root / "new.wav", [sine(0.3, 0.5, freq=900)])
        return baseline, self.scan()

    def test_added_removed_and_regression(self) -> None:
        baseline, current = self._baseline_and_current()
        diff = audio_qa.compare_reports(baseline, current)
        self.assertEqual(diff["added"], ["new.wav"])
        self.assertEqual(diff["removed"], ["gone.wav"])
        self.assertEqual(diff["summary"]["regressions"], 1)
        entry = next(item for item in diff["changed"] if item["path"] == "changes.wav")
        self.assertEqual(entry["severity"], "FAIL")
        peak_delta = next(d for d in entry["deltas"] if d["metric"] == "峰值(dBFS)")
        self.assertLess(peak_delta["delta"], -15)

    def test_unchanged_library_has_no_diff(self) -> None:
        write_wav(self.root / "a.wav", [sine(0.3, 0.5)])
        baseline = self.scan()
        diff = audio_qa.compare_reports(baseline, self.scan())
        self.assertEqual(diff["summary"], {"added": 0, "removed": 0, "changed": 0, "regressions": 0})

    def test_format_change_is_regression(self) -> None:
        write_wav(self.root / "a.wav", [sine(0.3, 0.5)], sample_rate=48000)
        baseline = self.scan()
        write_wav(self.root / "a.wav", [sine(0.3, 0.5, sample_rate=24000)], sample_rate=24000)
        diff = audio_qa.compare_reports(baseline, self.scan())
        entry = diff["changed"][0]
        self.assertEqual(entry["severity"], "FAIL")
        self.assertTrue(any("采样率" in note for note in entry["notes"]))

    def test_compare_markdown_renders(self) -> None:
        baseline, current = self._baseline_and_current()
        markdown = audio_qa.render_compare_markdown(audio_qa.compare_reports(baseline, current))
        self.assertIn("# 音频资产回归比对报告", markdown)
        self.assertIn("new.wav", markdown)
        self.assertIn("gone.wav", markdown)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
class TestCLI(AudioQATestCase):
    def test_scan_exit_code_and_outputs(self) -> None:
        write_wav(self.root / "bad.wav", [sine(0.3, 1.6)])
        out_json = self.root / "out" / "report.json"
        out_md = self.root / "out" / "report.md"
        code = audio_qa.main(
            [
                "scan", str(self.root),
                "-o", str(out_json),
                "--md", str(out_md),
                "--no-loudness", "--quiet",
            ]
        )
        self.assertEqual(code, 1)  # 有 FAIL -> 非零退出，便于 CI 阻断
        self.assertTrue(out_json.exists() and out_md.exists())
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["fail"], 1)

    def test_scan_exit_zero_when_clean(self) -> None:
        write_wav(self.root / "ok.wav", [sine(0.3, 0.5)])
        code = audio_qa.main(["scan", str(self.root), "--no-loudness", "--quiet"])
        self.assertEqual(code, 0)

    def test_missing_directory_returns_two(self) -> None:
        code = audio_qa.main(["scan", str(self.root / "nope"), "--no-loudness", "--quiet"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
