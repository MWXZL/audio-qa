"""现场记录助手（scripts/field_session.py）的行为验证。

只验两件事：客观测量有没有算进骨架、必须由人填的部分有没有留成空。
工具的输出会被直接引用进缺陷报告，所以「算错」和「替人下结论」都要挡住。
"""
from __future__ import annotations

import math
import struct
import subprocess
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

    def test_human_edits_survive_regeneration(self) -> None:
        """重新测量会重写骨架，但人工填的结论与环境字段必须保住（实测被清空过）。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8")
        text = text.replace("| 游戏 / 版本 |  |", "| 游戏 / 版本 | 崩坏：星穹铁道 4.4 |")
        text = text.replace("- 结论（PASS / FAIL / BLOCKED / 未复现）：", "- 结论（PASS / FAIL / BLOCKED / 未复现）：PASS")
        text = text.replace("- 实际（写可观察事实 + 时间码）：", "- 实际：三次均正常")
        skeleton.write_text(text, encoding="utf-8")

        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("| 游戏 / 版本 | 崩坏：星穹铁道 4.4 |", again)
        self.assertIn("：PASS", again)
        self.assertIn("三次均正常", again)

    def test_repeat_count_is_prefilled_from_clip_count(self) -> None:
        """三段重复录制时，「执行次数」与「复现率」应自动按片段数预填，避免手写出错。"""
        directory = self.root / "bug_03_music"
        for index in (1, 2, 3):
            write_wav(directory / f"raw_20260910_bug_03_r{index:02d}.wav", sine(1.0))
        text = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：3（本目录内 3 个片段）", text)
        self.assertIn("- 复现率：__ / 3", text)
        self.assertIn("不要为每一段单独下一个结论", text)

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

    # ---------------------------------------------------- 同一段的录像与音轨不能算两段
    def test_dedupe_prefers_the_video_file(self) -> None:
        """归档目录里同时有 .mkv 与抽出来的 .mka——那是一段素材，不是两段。"""
        paths = [Path("/x/raw_20260910_bug_03_r01.mka"),
                 Path("/x/raw_20260910_bug_03_r01.mkv")]
        kept = field_session.dedupe_takes(paths)
        self.assertEqual([path.name for path in kept], ["raw_20260910_bug_03_r01.mkv"])

    def test_dedupe_keeps_distinct_takes(self) -> None:
        paths = [Path(f"/x/raw_20260910_bug_03_r{index:02d}.mkv") for index in (1, 2, 3)]
        self.assertEqual(len(field_session.dedupe_takes(paths)), 3)

    def test_dedupe_keeps_audio_only_takes(self) -> None:
        """纯音频方案下只有 .mka，不能因为「没有视频」就把素材丢掉。"""
        paths = [Path(f"/x/raw_20260910_bug_03_r{index:02d}.mka") for index in (1, 2)]
        self.assertEqual(len(field_session.dedupe_takes(paths)), 2)

    def test_dedupe_of_empty_list(self) -> None:
        self.assertEqual(field_session.dedupe_takes([]), [])

    def test_report_counts_one_take_when_video_and_audio_copy_coexist(self) -> None:
        """端到端：录像 + 无损音轨同目录时，执行次数必须还是 1（曾经被算成 2）。"""
        ffmpeg = __import__("audio_qa").find_ffmpeg(None)
        if ffmpeg is None:
            self.skipTest("没有 ffmpeg，无法生成真实录像")
        directory = self.root / "bug_03_music"
        directory.mkdir(parents=True, exist_ok=True)
        mkv = directory / "raw_20260910_bug_03_r01.mkv"
        made = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1.2",
             "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(mkv)],
            capture_output=True,
        )
        if made.returncode != 0 or not mkv.is_file():
            self.skipTest(f"本机 ffmpeg 生成测试录像失败：{made.stderr[:80]!r}")
        mka = directory / "raw_20260910_bug_03_r01.mka"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(mkv), "-vn",
                        "-c:a", "copy", str(mka)], capture_output=True)
        self.assertTrue(mka.is_file())
        result = field_session.run_session(directory=directory, case="bug_03",
                                           exts=["mkv", "mka"], dropout_min_ms=80.0,
                                           do_loudness=False, quiet=True)
        text = result["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：1（本目录内 1 个片段）", text)
        self.assertIn("- 复现率：__ / 1", text)
        self.assertTrue(all(not item["path"].endswith(".mka")
                            for item in result["report"]["files"]))

    # ---------------------------------------------------- 片段数变了，计数必须跟着变
    def test_take_count_follows_directory_when_a_take_is_added(self) -> None:
        """补录一段后重新测量，「执行次数」必须变成新的片段数（曾经一直停在旧值）。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        first = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：1（本目录内 1 个片段）", first)

        write_wav(directory / "raw_20260910_bug_03_r02.wav", sine(1.0))
        second = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：2（本目录内 2 个片段）", second)
        self.assertIn("- 复现率：__ / 2", second)

    def test_rate_numerator_survives_but_denominator_follows_takes(self) -> None:
        """人工填的复现率分子要保住，分母必须跟着片段数变。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace("- 复现率：__ / 1", "- 复现率：0 / 1")
        skeleton.write_text(text, encoding="utf-8")

        write_wav(directory / "raw_20260910_bug_03_r02.wav", sine(1.0))
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 复现率：0 / 2", again)

    def test_rate_comment_after_denominator_is_kept(self) -> None:
        """复现率行尾的说明是执行者写下的判断依据，重生成时不能悄悄删掉。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace(
            "- 复现率：__ / 1", "- 复现率：0 / 1（三次均未出现异常）")
        skeleton.write_text(text, encoding="utf-8")

        write_wav(directory / "raw_20260910_bug_03_r02.wav", sine(1.0))
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 复现率：0 / 2（三次均未出现异常）", again)

    def test_rate_line_written_in_free_form_is_preserved(self) -> None:
        """人把复现率写成了别的形态（没有分母）时，整行原样保留。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace(
            "- 复现率：__ / 1", "- 复现率：本目录为探索性观察，不计复现率")
        skeleton.write_text(text, encoding="utf-8")
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 复现率：本目录为探索性观察，不计复现率", again)

    def test_customised_count_line_is_not_clobbered(self) -> None:
        """人真的把计数行改成别的形态时（写了备注），不能被自动预填覆盖。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace(
            "- 执行次数：1（本目录内 1 个片段）", "- 执行次数：3（另补测 1 段）")
        skeleton.write_text(text, encoding="utf-8")
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：3（另补测 1 段）", again)

    def test_empty_tail_field_with_continuation_is_kept(self) -> None:
        """「- 其它备注：」后面没有冒号后文字、只跟着说明行——那也是人写的，不能丢。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace(
            "- 其它备注：", "- 其它备注：\n  - 三段由同一脚本按绝对秒数执行\n  - 蓝牙一步未受控")
        skeleton.write_text(text, encoding="utf-8")
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 三段由同一脚本按绝对秒数执行", again)
        self.assertIn("- 蓝牙一步未受控", again)

    def test_untouched_template_is_not_treated_as_filled(self) -> None:
        """空白骨架不能被当成「已填」：否则标题里的「（待填）」会被错误摘掉。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        first = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("## 一、环境（待填）", again)
        self.assertIn("## 四、结论（待填）", again)
        self.assertEqual(first.count("## 四、结论（待填）"), again.count("## 四、结论（待填）"))

    def test_multiline_conclusion_body_survives_regeneration(self) -> None:
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8").replace(
            "- 实际（写可观察事实 + 时间码）：",
            "- 实际（写可观察事实 + 时间码）：\n  1. 第一处 42.74s\n  2. 第二处 48.91s\n  3. 第三处 69.92s")
        skeleton.write_text(text, encoding="utf-8")

        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("1. 第一处 42.74s", again)
        self.assertIn("2. 第二处 48.91s", again)
        self.assertIn("3. 第三处 69.92s", again)

    def test_filled_gap_table_cells_survive_regeneration(self) -> None:
        """第三节两个人工列（画面内容 / 定性）必须按 (片段, 时间码) 认领回来。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.2) + silence(0.3) + sine(1.2))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8")
        line = next(line for line in text.splitlines()
                    if line.startswith("| `raw_20260910_bug_03_r01.wav` | 00:01.200"))
        filled = line.replace("|  |  |", "| 实机画面，非加载图 | design（设备操作） |")
        self.assertNotEqual(filled, line)
        skeleton.write_text(text.replace(line, filled), encoding="utf-8")

        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        self.assertIn("实机画面，非加载图", again)
        self.assertIn("design（设备操作）", again)

    def test_gap_table_cells_do_not_leak_to_other_timecodes(self) -> None:
        """按行身份认领：时间码不同的行不该拿到别人的定性。"""
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.2) + silence(0.3) + sine(1.2))
        skeleton = self.session(directory)["skeleton"]
        text = skeleton.read_text(encoding="utf-8")
        line = next(line for line in text.splitlines()
                    if line.startswith("| `raw_20260910_bug_03_r01.wav` | 00:01.200"))
        skeleton.write_text(text.replace(line, line.replace("|  |  |", "| 甲 | 乙 |")),
                            encoding="utf-8")

        write_wav(directory / "raw_20260910_bug_03_r02.wav", sine(2.0) + silence(0.4) + sine(2.0))
        again = self.session(directory)["skeleton"].read_text(encoding="utf-8")
        other = [line for line in again.splitlines()
                 if line.startswith("| `raw_20260910_bug_03_r02.wav` |")][0]
        self.assertNotIn("| 甲 |", other)

    def test_derived_workcopies_are_not_treated_as_evidence(self) -> None:
        """按句切出的 wav 片段是派生工作副本：列进测量表会把 3 段素材变成 3 段 + 98 个片段。"""
        self.assertTrue(field_session.is_derived_workcopy(Path("句_r05_01.wav")))
        self.assertTrue(field_session.is_derived_workcopy(Path("语音片段_r04_49.wav")))
        self.assertTrue(field_session.is_derived_workcopy(Path("字幕截图_r05_12.jpg")))
        self.assertFalse(field_session.is_derived_workcopy(Path("raw_20260910_bug_03_r01.mka")))

    def test_scan_ignores_derived_workcopies(self) -> None:
        directory = self.root / "bug_03_music"
        write_wav(directory / "raw_20260910_bug_03_r01.wav", sine(1.0))
        write_wav(directory / "语音片段_r01_01.wav", sine(1.0))
        write_wav(directory / "句_r01_02.wav", sine(1.0))
        result = self.session(directory)
        text = result["skeleton"].read_text(encoding="utf-8")
        self.assertIn("- 执行次数：1（本目录内 1 个片段）", text)
        self.assertNotIn("语音片段_r01_01", text)


if __name__ == "__main__":
    unittest.main()
