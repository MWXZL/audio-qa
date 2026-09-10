# -*- coding: utf-8 -*-
"""asr_align 单元测试

全部用合成数据做确定性验证：基线表与 ASR segments 都是现场构造的，
不依赖任何 ASR 引擎、不依赖网络。每个用例只验一件事，测试方法名是完整英文句子，
与 test_audio_qa.py 保持同一口径。
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

import asr_align  # noqa: E402

BASELINE_HEADER = "句号\t语音起\t语音止\t字幕出现\t字幕消失\t字幕原文\t备注\n"


def row(
    index: str,
    speech_start: float | None,
    speech_end: float | None,
    subtitle_start: float | None,
    subtitle_end: float | None,
    text: str,
    note: str = "",
) -> asr_align.BaselineRow:
    return asr_align.BaselineRow(
        index, speech_start, speech_end, subtitle_start, subtitle_end, text, note
    )


def seg(start: float, end: float, text: str) -> asr_align.ASRSegment:
    return asr_align.ASRSegment(start, end, text)


def run(
    rows: list[asr_align.BaselineRow],
    segments: list[asr_align.ASRSegment],
    fail_ms: float = 250.0,
    warn_ms: float = 150.0,
    similarity: float = 0.6,
) -> dict:
    return asr_align.align(rows, segments, asr_align.Thresholds(fail_ms, warn_ms, similarity))


class AsrAlignTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_baseline(self, path: Path, lines: list[str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(BASELINE_HEADER + "".join(lines), encoding="utf-8")
        return path

    def write_asr(self, path: Path, segments: list[dict]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def categories(self, report: dict) -> dict[str, int]:
        return report["summary"]["by_category"]


class TestWellAlignedDialogue(AsrAlignTestCase):
    def test_well_aligned_dialogue_produces_no_differences(self) -> None:
        rows = [
            row("D-01", 1.0, 2.0, 1.0, 2.0, "初次见面，请多关照。"),
            row("D-02", 2.5, 3.5, 2.5, 3.5, "这里发生了什么？"),
        ]
        segments = [seg(1.0, 2.0, "初次见面请多关照"), seg(2.5, 3.5, "这里发生了什么")]
        report = run(rows, segments)
        self.assertEqual(report["differences"], [])
        self.assertEqual(report["summary"]["differences"], 0)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warn"], 0)
        self.assertEqual(report["summary"]["offset_stats"]["evaluated"], 2)


class TestOffsetDetection(AsrAlignTestCase):
    def test_late_subtitle_is_reported_as_offset_fail_with_correct_statistics(self) -> None:
        rows = [row("D-01", 1.0, 2.0, 1.4, 2.4, "你好。")]
        segments = [seg(1.0, 2.0, "你好")]
        report = run(rows, segments)
        self.assertEqual(self.categories(report)["offset"], 1)
        diff = next(d for d in report["differences"] if d["category"] == "offset")
        self.assertEqual(diff["severity"], "FAIL")
        self.assertAlmostEqual(diff["detail"]["offset_ms"], 400.0, delta=0.5)
        stats = report["summary"]["offset_stats"]
        self.assertEqual(stats["median_ms"], 400.0)
        self.assertEqual(stats["p90_ms"], 400.0)
        self.assertEqual(stats["max_abs_ms"], 400.0)
        self.assertEqual(stats["exceed_count"], 1)
        self.assertEqual(stats["fail_count"], 1)
        self.assertEqual(stats["warn_count"], 0)

    def test_offset_within_warn_band_is_severity_warn_not_fail(self) -> None:
        rows = [row("D-01", 1.0, 2.0, 1.2, 2.2, "你好。")]
        segments = [seg(1.0, 2.0, "你好")]
        report = run(rows, segments)
        diff = next(d for d in report["differences"] if d["category"] == "offset")
        self.assertEqual(diff["severity"], "WARN")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warn"], 1)


class TestMissingSpeech(AsrAlignTestCase):
    def test_subtitle_window_without_asr_text_is_missing_speech(self) -> None:
        rows = [row("D-01", 0.0, 1.0, 0.0, 1.0, "你好。")]
        report = run(rows, [])
        self.assertEqual(self.categories(report)["missing_speech"], 1)
        diff = report["differences"][0]
        self.assertEqual(diff["category"], "missing_speech")
        self.assertEqual(diff["severity"], "WARN")


class TestMissingSubtitle(AsrAlignTestCase):
    def test_asr_text_without_baseline_subtitle_is_missing_subtitle(self) -> None:
        rows = [row("D-01", 0.0, 1.0, 0.0, 1.0, "你好。")]
        # 第二段 [5,6] 与基线任何时段都不重合，却转出了文本 => 有语音、无字幕
        segments = [seg(0.0, 1.0, "你好"), seg(5.0, 6.0, "再见")]
        report = run(rows, segments)
        self.assertEqual(self.categories(report)["missing_subtitle"], 1)
        diff = next(d for d in report["differences"] if d["category"] == "missing_subtitle")
        self.assertEqual(diff["index"], "ASR#2")
        self.assertEqual(diff["severity"], "WARN")


class TestMisordered(AsrAlignTestCase):
    def test_different_text_at_overlapping_time_is_misordered(self) -> None:
        rows = [row("D-01", 0.0, 1.0, 0.0, 1.0, "你好。")]
        segments = [seg(0.0, 1.0, "再见")]
        report = run(rows, segments)
        self.assertEqual(self.categories(report)["misordered"], 1)
        diff = report["differences"][0]
        self.assertEqual(diff["severity"], "WARN")
        self.assertLess(diff["detail"]["similarity"], 0.6)


class TestThresholdConfiguration(AsrAlignTestCase):
    def test_same_offset_is_warn_not_fail_under_a_looser_fail_threshold(self) -> None:
        rows = [row("D-01", 1.0, 2.0, 1.4, 2.4, "你好。")]
        segments = [seg(1.0, 2.0, "你好")]
        report = run(rows, segments, fail_ms=500.0)
        diff = next(d for d in report["differences"] if d["category"] == "offset")
        self.assertEqual(diff["severity"], "WARN")
        self.assertEqual(report["summary"]["fail"], 0)

    def test_warn_threshold_must_not_exceed_fail_threshold(self) -> None:
        code = asr_align.main(["--baseline", "x", "--asr", "y", "--out", "z", "--warn-ms", "999"])
        self.assertEqual(code, 2)


class TestTextSimilarity(AsrAlignTestCase):
    def test_similarity_is_one_after_normalising_punctuation_and_whitespace(self) -> None:
        self.assertEqual(asr_align.text_similarity("你好，世界！", "你好世界"), 1.0)

    def test_similarity_folds_fullwidth_characters_to_halfwidth(self) -> None:
        self.assertEqual(asr_align.text_similarity("Ｈｅｌｌｏ，世界", "hello 世界"), 1.0)

    def test_completely_different_text_has_low_similarity(self) -> None:
        self.assertLess(asr_align.text_similarity("你好", "再见"), 0.6)


class TestConfirmedVsSuspected(AsrAlignTestCase):
    def test_confirmed_and_suspected_differences_are_counted_separately(self) -> None:
        rows = [
            row("D-01", 0.0, 1.0, 0.0, 1.0, "你好。", note="已确认复听"),
            row("D-02", 1.5, 2.5, 1.5, 2.5, "再见。", note=""),
        ]
        report = run(rows, [])
        self.assertEqual(report["summary"]["confirmed"], 1)
        self.assertEqual(report["summary"]["suspected"], 1)
        self.assertEqual(report["summary"]["differences"], 2)
        confirmed = [d for d in report["differences"] if d["confirmed"]]
        self.assertEqual(confirmed[0]["index"], "D-01")


class TestMarkdownRendering(AsrAlignTestCase):
    def test_markdown_contains_draft_notice_and_two_count_columns(self) -> None:
        rows = [row("D-01", 1.0, 2.0, 1.4, 2.4, "你好。")]
        segments = [seg(1.0, 2.0, "你好")]
        markdown = asr_align.render_markdown(run(rows, segments))
        self.assertIn("待校准草案", markdown)
        self.assertIn("已确认差异", markdown)
        self.assertIn("疑似差异", markdown)
        self.assertIn("offset 统计", markdown)
        self.assertIn("不合并", markdown)


class TestInputParsing(AsrAlignTestCase):
    def test_load_baseline_reads_tsv_header_and_float_fields(self) -> None:
        path = self.write_baseline(
            self.root / "baseline.tsv",
            ["D-01\t1.0\t2.0\t1.0\t2.0\t你好\t\n", "D-02\t2.5\t3.5\t2.5\t3.5\t再见\t\n"],
        )
        rows = asr_align.load_baseline(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].index, "D-02")
        self.assertAlmostEqual(rows[1].speech_start, 2.5, places=3)
        self.assertIsNone(rows[0].note or None)

    def test_load_asr_segments_sorts_by_start_time(self) -> None:
        path = self.write_asr(
            self.root / "asr.json",
            [
                {"start": 5.0, "end": 6.0, "text": "再见"},
                {"start": 1.0, "end": 2.0, "text": "你好"},
            ],
        )
        segments = asr_align.load_asr_segments(path)
        self.assertEqual([s.start for s in segments], [1.0, 5.0])
        self.assertEqual(segments[0].text, "你好")

    def test_load_baseline_reads_csv_variant(self) -> None:
        path = self.root / "baseline.csv"
        path.write_text(
            "句号,语音起,语音止,字幕出现,字幕消失,字幕原文,备注\n"
            "D-01,1.0,2.0,1.0,2.0,你好,\n",
            encoding="utf-8",
        )
        rows = asr_align.load_baseline(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subtitle_text, "你好")


class TestCommandLineInterface(AsrAlignTestCase):
    def test_cli_writes_markdown_and_json_and_returns_fail_exit_code(self) -> None:
        baseline = self.write_baseline(
            self.root / "baseline.tsv",
            ["D-01\t1.0\t2.0\t1.4\t2.4\t你好\t\n"],
        )
        asr_json = self.write_asr(self.root / "asr.json", [{"start": 1.0, "end": 2.0, "text": "你好"}])
        out_md = self.root / "out" / "align.md"
        out_json = self.root / "out" / "align.json"
        code = asr_align.main(
            [
                "--baseline", str(baseline),
                "--asr", str(asr_json),
                "--out", str(out_md),
                "--json", str(out_json),
            ]
        )
        self.assertEqual(code, 1)
        self.assertTrue(out_md.exists() and out_json.exists())
        markdown = out_md.read_text(encoding="utf-8")
        self.assertIn("待校准草案", markdown)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["fail"], 1)
        self.assertEqual(data["summary"]["offset_stats"]["max_abs_ms"], 400.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)