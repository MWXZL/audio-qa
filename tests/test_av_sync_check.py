"""文本 × 语音活动 交叉检查（scripts/av_sync_check.py）的验证。

它给出的是 bug_04 的第一遍机器判定，四类事实必须分得清：
文本有语音没跟、语音有文本没跟、两段活动之间没有静音（连读或叠音候选）、以及偏差毫秒数。
纯逻辑全部覆盖；另有一条端到端：合成一段「三段语音 + 三段文本」的素材，断言偏差量得准。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import audio_qa  # noqa: E402
import av_sync_check as av  # noqa: E402


class ActivitySegmentTestCase(unittest.TestCase):
    def test_silences_become_activity_segments(self) -> None:
        silences = [{"start": 0.0, "end": 1.5}, {"start": 2.5, "end": 3.7}, {"start": 7.5, "end": 8.0}]
        segments = av.activity_segments(8.0, silences)
        self.assertEqual([(round(s["start"], 2), round(s["end"], 2)) for s in segments],
                         [(1.5, 2.5), (3.7, 7.5)])

    def test_leading_silence_is_not_counted_as_activity(self) -> None:
        """片头静音被算成活动段时，第一条文本会匹配到 0 秒（实测偏差量成 −1200 ms）。"""
        segments = av.activity_segments(8.0, [{"start": 0.0, "end": 1.5}])
        self.assertEqual(segments[0]["start"], 1.5)

    def test_very_short_segments_are_dropped(self) -> None:
        """静音之间的碎渣（0.05 s 级）不是「一次说话」，报出来就是假活动段。"""
        silences = [{"start": 0.0, "end": 0.1}, {"start": 0.15, "end": 0.25},
                    {"start": 0.3, "end": 0.4}]
        segments = av.activity_segments(1.0, silences)
        self.assertEqual([(round(s["start"], 2), round(s["end"], 2)) for s in segments],
                         [(0.4, 1.0)])

    def test_no_silence_means_one_segment(self) -> None:
        self.assertEqual(len(av.activity_segments(5.0, [])), 1)

    def test_zero_duration(self) -> None:
        self.assertEqual(av.activity_segments(0.0, []), [])

    def test_parse_silences_reads_duration_and_intervals(self) -> None:
        stderr = ("  Duration: 00:00:08.00, start: 0.000000, bitrate: 196 kb/s\n"
                  "[silencedetect @ 0x1] silence_start: 0\n"
                  "[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1.5\n"
                  "[silencedetect @ 0x1] silence_start: 7.5\n")
        duration, silences = av.parse_silences(stderr)
        self.assertAlmostEqual(duration, 8.0)
        self.assertEqual(len(silences), 2)
        self.assertAlmostEqual(silences[1]["end"], 8.0)     # 末尾静音补到片尾

    def test_overlap_seconds(self) -> None:
        self.assertAlmostEqual(av.overlap_seconds({"start": 0.0, "end": 2.0},
                                                  {"start": 1.0, "end": 3.0}), 1.0)
        self.assertEqual(av.overlap_seconds({"start": 0.0, "end": 1.0},
                                            {"start": 2.0, "end": 3.0}), 0.0)


class CrosscheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 三段语音活动：2.0–3.0、5.0–6.0、8.0–9.0
        self.segments = [{"start": 2.0, "end": 3.0}, {"start": 5.0, "end": 6.0},
                         {"start": 8.0, "end": 9.0}]

    def test_measures_offset_between_text_and_voice(self) -> None:
        states = [{"start": 2.2, "end": 3.0}, {"start": 5.3, "end": 6.0}, {"start": 8.4, "end": 9.0}]
        result = av.crosscheck(states, self.segments, 10.0)
        self.assertEqual([row["offset_ms"] for row in result["rows"]], [-200, -300, -400])
        self.assertEqual(result["offset_median_ms"], -300)

    def test_text_without_voice_is_flagged(self) -> None:
        states = [{"start": 2.2, "end": 3.0}, {"start": 6.5, "end": 7.0}]   # 第二句落在静音里
        result = av.crosscheck(states, self.segments, 10.0)
        kinds = [item["kind"] for item in result["findings"]]
        self.assertIn("text_without_voice", kinds)

    def test_voice_without_text_is_flagged(self) -> None:
        states = [{"start": 2.2, "end": 3.0}]                                # 后两段语音没有文本
        result = av.crosscheck(states, self.segments, 10.0)
        kinds = [item["kind"] for item in result["findings"]]
        self.assertEqual(kinds.count("voice_without_text"), 2)

    def test_no_gap_between_segments_is_a_candidate_not_a_verdict(self) -> None:
        """正常连读也是无缝的——只能报候选，定性要回听。"""
        segments = [{"start": 1.0, "end": 2.0}, {"start": 2.1, "end": 3.0}]
        result = av.crosscheck([{"start": 1.0, "end": 3.0}], segments, 5.0)
        kinds = [item["kind"] for item in result["findings"]]
        self.assertIn("no_gap_between", kinds)
        text = av.render(result, Path("a.mkv"), Path("t.json"), 120.0, usable=True)
        self.assertIn("只是候选", text)

    def test_silence_share_is_reported(self) -> None:
        result = av.crosscheck([{"start": 2.2, "end": 3.0}], self.segments, 10.0)
        self.assertAlmostEqual(result["silence_share"], 0.7, places=2)

    def test_render_warns_when_activity_method_does_not_apply(self) -> None:
        result = av.crosscheck([{"start": 2.2, "end": 3.0}], [{"start": 0.0, "end": 30.0}], 30.0)
        text = av.render(result, Path("a.mkv"), Path("t.json"), 120.0, usable=False)
        self.assertIn("不适合用活动法", text)


class EndToEndTestCase(unittest.TestCase):
    """合成素材：三段语音（正弦突音）配三段文本（字幕块），断言偏差量得准。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_media(self) -> Path | None:
        """三段 1 秒的语音（在 1.5 / 4.0 / 6.5 s 处，各偏移文本 300 ms）+ 对应文本状态。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return None
        ffmpeg = audio_qa.find_ffmpeg(None)
        if ffmpeg is None:
            return None
        spans = ((1.2, 2.4), (3.7, 4.9), (6.2, 7.4))       # 文本状态
        voices = ((1.5, 2.5), (4.0, 5.0), (6.5, 7.5))      # 语音活动（各晚 300 ms）
        (self.root / "states.json").write_text(json.dumps(
            {"file": "v.mkv", "fps": 10.0, "states": [
                {"start": a, "end": b, "duration": b - a, "bright": 1.2, "has_text": True}
                for a, b in spans]}), encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp)
            for index in range(80):
                at = index / 10.0
                image = Image.new("RGB", (640, 360), (16, 18, 24))
                draw = ImageDraw.Draw(image)
                if any(a <= at < b for a, b in spans):
                    try:
                        font = ImageFont.truetype("msyh.ttc", 32)
                    except Exception:
                        font = ImageFont.load_default()
                    draw.text((40, 300), "台词", fill=(240, 240, 240), font=font)
                image.save(frames / f"f_{index:04d}.png")
            # 音频：三段突音（各自 adelay 到语音时刻），其余为静音
            inputs: list[str] = []
            chains: list[str] = []
            for index, (start, _end) in enumerate(voices, 1):
                inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000"]
                chains.append(f"[{index}:a]adelay={int(start * 1000)}|{int(start * 1000)}[a{index}]")
            labels = "".join(f"[a{index}]" for index in range(1, len(voices) + 1))
            graph = (";".join(chains) + f";{labels}amix=inputs={len(voices)}:normalize=0,apad[a]")
            done = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-framerate", "10",
                 "-i", str(frames / "f_%04d.png"), *inputs,
                 "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
                 "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 str(self.root / "v.mkv")], capture_output=True)
        return self.root / "v.mkv" if done.returncode == 0 else None

    def test_offsets_are_measured_end_to_end(self) -> None:
        media = self.make_media()
        if media is None:
            self.skipTest("需要 ffmpeg 与 Pillow 才能生成合成素材")
        ffmpeg = audio_qa.find_ffmpeg(None)
        duration, silences = av.measure(media, ffmpeg)
        segments = av.activity_segments(duration, silences)
        states = json.loads((self.root / "states.json").read_text(encoding="utf-8"))["states"]
        result = av.crosscheck(states, segments, duration)
        self.assertEqual(result["text_states"], 3)
        self.assertGreaterEqual(result["activity_segments"], 3)
        offsets = [row["offset_ms"] for row in result["rows"] if row["offset_ms"] is not None]
        self.assertEqual(len(offsets), 3)
        # 合成素材里语音比文本晚 300 ms —— 偏差量出来应当就是 +300 上下
        for offset in offsets:
            self.assertAlmostEqual(offset, 300, delta=150)


if __name__ == "__main__":
    unittest.main()
