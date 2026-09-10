"""字幕时间线（scripts/subtitle_track.py）的验证。

它量出来的时刻会被当成「字幕出现/消失」的客观测量写进现场记录，所以：
- 状态切分的纯逻辑要有测试（含抖动合并、字幕消失段剔除、字幕换行切分）；
- 端到端要有一张**已知答案**的合成录像：用 Pillow 画出三句在固定时刻出现/消失的字幕，
  再断言检出结果就是那三段——否则工具可能在真实素材上给出看着合理、实际错位的时间码。

判据不是灰度平均绝对差：实测在 320 宽的字幕条上，一整行字幕只占约 1% 的像素，
灰度平均差被暗背景稀释到噪声级（2–3），而亮像素占比是干净的 0% ↔ 1.1%。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import audio_qa  # noqa: E402
import subtitle_track as st  # noqa: E402

WIDTH, HEIGHT = 640, 360
# 三句字幕的出现/消失时刻（秒），用来生成合成录像，也是断言的目标
LINES = ((1.0, 3.0, "第一句台词"), (3.4, 5.6, "第二句台词"), (6.0, 8.0, "第三句台词"))


def make_video(path: Path) -> bool:
    """用 Pillow 画帧、用 ffmpeg 编码成 mkv（带一条静音音轨）。不依赖 drawtext 滤镜。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    ffmpeg = audio_qa.find_ffmpeg(None)
    if ffmpeg is None:
        return False
    fps = 10
    with tempfile.TemporaryDirectory() as tmp:
        frames = Path(tmp)
        for index in range(int(8 * fps)):
            at = index / fps
            image = Image.new("RGB", (WIDTH, HEIGHT), (16, 18, 24))
            draw = ImageDraw.Draw(image)
            # 对话界面：底部有一条更暗的对话条
            draw.rectangle([0, int(HEIGHT * 0.70), WIDTH, HEIGHT], fill=(10, 10, 14))
            for start, end, text in LINES:
                if start <= at < end:
                    try:
                        font = ImageFont.truetype("msyh.ttc", 32)
                    except Exception:
                        font = ImageFont.load_default()
                    draw.text((40, HEIGHT - 70), text, fill=(240, 240, 240), font=font)
            image.save(frames / f"f_{index:04d}.png")
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-framerate", str(fps), "-i", str(frames / "f_%04d.png"),
             "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
             "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
            capture_output=True)
    return done.returncode == 0 and path.is_file()


class StateSplitTestCase(unittest.TestCase):
    """纯逻辑：由「亮点占比 + 掩码翻转」切状态。"""

    def signals(self, text_spans: list[tuple[int, int]], total: int) -> tuple[list, list]:
        brights = [1.2 if any(a <= i < b for a, b in text_spans) else 0.0 for i in range(total)]
        return brights, [0.0] * total

    def test_two_spans_make_two_states(self) -> None:
        brights, changes = self.signals([(5, 25), (30, 50)], 60)
        states = st.states_from_signals(brights, changes, fps=10.0)
        self.assertEqual([(s["start"], s["end"]) for s in states], [(0.5, 2.5), (3.0, 5.0)])

    def test_line_change_without_gap_splits_the_state(self) -> None:
        """字幕换行时没有空档，只能靠掩码翻转切分。"""
        brights, changes = self.signals([(0, 40)], 40)
        changes[20] = 1.0
        states = st.states_from_signals(brights, changes, fps=10.0)
        self.assertEqual([(s["start"], s["end"]) for s in states], [(0.0, 2.0), (2.0, 4.0)])

    def test_frames_without_text_are_not_reported(self) -> None:
        brights, changes = self.signals([(10, 30)], 40)
        states = st.states_from_signals(brights, changes, fps=10.0)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["start"], 1.0)

    def test_short_flicker_is_merged_away_entirely(self) -> None:
        """一次抖动会切出「长–极短–长」三块；只并中间那块会留下假边界，把一行拆成两句。"""
        brights, changes = self.signals([(0, 40)], 40)
        changes[20] = 1.0
        changes[21] = 1.0                        # 只持续 1 帧（0.1 s）的抖动
        states = st.states_from_signals(brights, changes, fps=10.0, min_state_s=0.4)
        self.assertEqual([(s["start"], s["end"]) for s in states], [(0.0, 4.0)])

    def test_empty_or_mismatched_input(self) -> None:
        self.assertEqual(st.states_from_signals([], [], fps=5.0), [])
        self.assertEqual(st.states_from_signals([1.0, 1.0], [0.0], fps=5.0), [])

    def test_bright_floor_scales_with_the_brightest_frame(self) -> None:
        """整段偏暗时（夜间场景）不能因为固定阈值而漏掉字幕。"""
        brights = [0.9] * 5 + [0.0] * 5
        states = st.states_from_signals(brights, [0.0] * 10, fps=5.0)
        self.assertEqual(len(states), 1)


class BaselineTsvTestCase(unittest.TestCase):
    def test_tsv_matches_the_asr_align_header(self) -> None:
        """基线表头必须与 asr_align 的 BASELINE_COLUMNS 逐列一致，否则下游读不了。"""
        import asr_align

        states = [{"start": 1.0, "end": 2.5, "duration": 1.5, "bright": 40.0,
                   "has_text": True, "shot": "字幕截图_01.jpg"}]
        first = st.baseline_tsv(states).splitlines()[0]
        self.assertEqual(tuple(first.split("\t")), asr_align.BASELINE_COLUMNS)

    def test_tsv_prefills_subtitle_times_and_leaves_text_blank(self) -> None:
        states = [{"start": 1.0, "end": 2.5, "duration": 1.5, "bright": 40.0,
                   "has_text": True, "shot": "s.jpg"}]
        row = st.baseline_tsv(states).splitlines()[1].split("\t")
        self.assertEqual(row[0], "D-01")
        self.assertEqual((row[3], row[4]), ("1.000", "2.500"))
        self.assertEqual(row[5], "")              # 字幕原文留空待抄


class SyntheticVideoTestCase(unittest.TestCase):
    """端到端：合成一段已知字幕时刻的录像，断言检出结果就是那三段。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_tool(self, video: Path, out: Path, extra: list[str] | None = None) -> dict:
        out.mkdir(parents=True, exist_ok=True)
        args = st.build_parser().parse_args(
            [str(video), "--out", str(out), "--fps", "10"] + list(extra or []))
        args.region = tuple(float(part) for part in str(args.region).split(","))
        return st.process(video, out, audio_qa.find_ffmpeg(None), args)

    def test_detects_the_known_subtitle_times(self) -> None:
        video = self.root / "synthetic.mkv"
        if not make_video(video):
            self.skipTest("需要 ffmpeg 与 Pillow 才能生成合成录像")
        out = self.root / "out"
        result = self.run_tool(video, out)
        self.assertEqual(result["states"], len(LINES))
        states = json.loads((out / "synthetic_字幕时间线.json").read_text(encoding="utf-8"))["states"]
        for state, (start, end, _) in zip(states, LINES):
            self.assertAlmostEqual(state["start"], start, delta=0.25)
            self.assertAlmostEqual(state["end"], end, delta=0.25)
        # 每个状态都要留下「同时含字幕与对话界面」的截图与一份基线表
        self.assertEqual(len(list(out.glob("字幕截图_*.jpg"))), len(LINES))
        baseline = (out / "synthetic_字幕基线.tsv").read_text(encoding="utf-8")
        self.assertEqual(len(baseline.strip().splitlines()), len(LINES) + 1)

    def test_clips_are_cut_when_requested(self) -> None:
        video = self.root / "synthetic.mkv"
        if not make_video(video):
            self.skipTest("需要 ffmpeg 与 Pillow 才能生成合成录像")
        out = self.root / "clips"
        self.run_tool(video, out, ["--clips"])
        clips = sorted(out.glob("句_*.wav"))
        self.assertEqual(len(clips), len(LINES))
        self.assertTrue(all(clip.stat().st_size > 1000 for clip in clips))

    def test_report_says_it_is_not_ocr(self) -> None:
        video = self.root / "synthetic.mkv"
        if not make_video(video):
            self.skipTest("需要 ffmpeg 与 Pillow 才能生成合成录像")
        out = self.root / "out2"
        self.run_tool(video, out)
        text = (out / "synthetic_字幕时间线.md").read_text(encoding="utf-8")
        self.assertIn("不做 OCR", text)
        self.assertIn("字幕原文", text)


if __name__ == "__main__":
    unittest.main()
