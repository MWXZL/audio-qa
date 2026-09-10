"""现场记录助手（scripts/field_session.py）的行为验证。

只验两件事：客观测量有没有算进骨架、必须由人填的部分有没有留成空。
工具的输出会被直接引用进缺陷报告，所以「算错」和「替人下结论」都要挡住。
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

import field_session  # noqa: E402
from audio_qa import AudioQAError  # noqa: E402

SAMPLE_RATE = 48000


def sine(duration_s: float, amplitude: float = 0.5) -> list[int]:
    count = int(duration_s * SAMPLE_RATE)
    return [
        int(amplitude * 32767 * math.sin(2 * math.pi * 1000 * index / SAMPLE_RATE))
        for index in range(count)
    ]


def silence(duration_s: float) -> list[int]:
    return [0] * int(duration_s * SAMPLE_RATE)


def write_wav(path: Path, samples: list[int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    return path


class FieldSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def session(self, directory: Path, case: str | None = None) -> dict:
        return field_session.run_session(
            directory=directory,
            case=case,
            exts=["wav"],
            dropout_min_ms=80.0,
            do_loudness=False,
            quiet=True,
        )

    def test_measurements_and_gap_land_in_skeleton(self) -> None:
        directory = self.root / "bug_01_concurrency"
        write_wav(directory / "raw_20260910_bug_01_r01.wav",
                  sine(1.2) + silence(0.3) + sine(1.2))
        result = self.session(directory)
        text = result["skeleton"].read_text(encoding="utf-8")
        self.assertIn("raw_20260910_bug_01_r01.wav", text)
        self.assertIn("00:01.200", text)          # 间隙位置带时间码
        self.assertIn("300.00", text)             # 最长间隙毫秒数
        self.assertEqual(result["case"], "bug_01")

    def test_human_owned_fields_stay_blank(self) -> None:
        directory = self.root / "bug_02_surface"
        write_wav(directory / "raw_20260910_bug_02_r01.wav", sine(1.0))
        text = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("## 一、环境（待填）", text)
        self.assertIn("## 四、结论（待填）", text)
        self.assertIn("结论（PASS / FAIL / BLOCKED / 未复现）：\n", text)
        # 工具不得替人写结论
        for verdict in ("PASS", "FAIL", "BLOCKED", "未复现"):
            self.assertNotIn(f"结论（PASS / FAIL / BLOCKED / 未复现）：{verdict}", text)

    def test_case_inferred_from_directory_name(self) -> None:
        directory = self.root / "compat_bt_c02"
        write_wav(directory / "compat_bt_20260910_c02_r01.wav", sine(1.0))
        self.assertEqual(self.session(directory)["case"], "compat_bt_c02")

    def test_nonconforming_name_is_flagged(self) -> None:
        directory = self.root / "bug_03_music"
        write_wav(directory / "take.wav", sine(1.0))
        text = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("## 六、命名提醒", text)
        self.assertIn("`take.wav`", text)

    def test_conforming_names_produce_no_naming_section(self) -> None:
        directory = self.root / "bug_04_dialogue"
        write_wav(directory / "raw_20260910_bug_04_dialogue_r01.wav", sine(1.0))
        text = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertNotIn("命名提醒", text)

    def test_audio_only_names_are_recognised(self) -> None:
        """纯音频方案下归档出来的是 .mka/.m4a——命名规则必须认，否则会被误报不合规。"""
        for name in ("raw_20260910_bug_03_r01.mka", "raw_20260910_bug_03_r02.m4a",
                     "raw_20260910_bug_03_r03.flac", "raw_20260910_bug_03_r04.mkv"):
            self.assertIsNotNone(field_session.NAME_PATTERN.match(name), name)
        self.assertIsNone(field_session.NAME_PATTERN.match("take.mka"))

    def test_clean_batch_states_no_gap_instead_of_silence(self) -> None:
        directory = self.root / "bug_05_occlusion"
        write_wav(directory / "raw_20260910_bug_05_r01.wav", sine(1.5))
        text = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("未检出段内静音间隙", text)

    def test_missing_directory_raises_tool_error(self) -> None:
        with self.assertRaises(AudioQAError):
            self.session(self.root / "not_there")

    def test_outputs_are_written_next_to_clips(self) -> None:
        directory = self.root / "bug_01_concurrency"
        write_wav(directory / "raw_20260910_bug_01_r01.wav", sine(1.0))
        result = self.session(directory)
        self.assertTrue((directory / "measure.json").is_file())
        self.assertTrue((directory / "measure.md").is_file())
        self.assertEqual(result["skeleton"].name, "bug_01_现场记录.md")


        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.2) + silence(0.3) + sine(1.2))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8")

        # 未填结论时解析出的值为空——构建脚本据此跳过该条，不把「骨架存在」当「已执行」
        verdict_line = next(line for line in text.splitlines() if line.startswith("- 结论（"))
        parsed_empty = builder.VERDICT_LINE.match(verdict_line.strip())
        self.assertIsNotNone(parsed_empty)
        self.assertEqual(parsed_empty.group(1).strip(), "")

        # 填入结论后必须能解析出原值
        filled = text.replace(verdict_line, verdict_line + "PASS")
        skeleton.write_text(filled, encoding="utf-8")
        parsed = builder.VERDICT_LINE.match(
            next(line for line in filled.splitlines() if line.startswith("- 结论（")).strip()
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.group(1).strip(), "PASS")


if __name__ == "__main__":
    unittest.main()
