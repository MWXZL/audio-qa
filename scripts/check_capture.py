#!/usr/bin/env python3
"""采集链路自检：录完 10 秒，一条命令判断「这段录音能不能用」。

为什么单独做这个：现场最常见的挫败不是「没测出问题」，而是**录完才发现音频是空的、
单声道、或采样率不对**——那会浪费一整段操作，而且很可能当时没发现。这个脚本把
「能不能用」变成一条命令的结论，并给出下一步该做什么。

它只读文件、不改任何东西；判定结论分三档：可用 / 有警告但可用 / 不可用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audio_qa  # noqa: E402

SILENT_DBFS = -60.0          # 低于此值视为整段静音
QUIET_LUFS = -45.0           # 低于此值视为「有信号但电平异常低」，多半是音量没开
EXPECTED_RATE = 48000
MIN_DURATION_S = 5.0


def check(path: Path, ffmpeg: str | None) -> tuple[str, list[str], list[str], dict]:
    """返回 (结论, 问题列表, 提示列表, 测量值)。"""
    problems: list[str] = []
    hints: list[str] = []
    audio = audio_qa.load_audio(path, ffmpeg)
    if audio.frames == 0:
        return "不可用", ["文件不含音频采样数据"], hints, {}

    peaks = [audio_qa.channel_peak(chan) for chan in audio.chans]
    peak_dbfs = audio_qa.to_dbfs(max(peaks), audio.full_scale)
    loudness = None
    if ffmpeg is not None:
        measured = audio_qa.measure_loudness(path, ffmpeg)
        loudness = measured.get("lufs")
    measure = {
        "codec": audio.codec,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "duration_s": audio.duration_s,
        "peak_dbfs": peak_dbfs,
        "lufs": loudness,
    }

    if max(peaks) <= audio.full_scale * (10 ** (SILENT_DBFS / 20.0)):
        problems.append(f"整段静音（峰值 {peak_dbfs:.1f} dBFS）——捕获源选错，或设备被静音")
        hints.append("先看 OBS 混音器的电平条有没有跳动；不跳就换「应用程序音频捕获(beta)」并指定游戏进程")
        return "不可用", problems, hints, measure

    if audio.channels < 2:
        problems.append("单声道：游戏音频应为立体声，单声道会丢掉左右定位相关的判据")
        hints.append("在 OBS 捕获源的高级设置里把声道设为立体声；或检查系统「声音」里设备的默认格式")
    if audio.sample_rate != EXPECTED_RATE:
        problems.append(f"采样率 {audio.sample_rate} Hz，不是 48 kHz：与判据默认值不一致")
        hints.append("把输出设备与 OBS 的采样率都设为 48 kHz，避免重采样带来的额外不确定性")
    if loudness is not None and loudness < QUIET_LUFS:
        # 电平极低的「有声音」比整段静音更隐蔽：静音会被一眼看出，这个不会。
        problems.append(f"响度只有 {loudness:.1f} LUFS（正常游戏音频约 -20 ~ -30）：能录到信号，但电平异常低")
        hints.append("依次检查：系统音量合成器里该进程的滑条 → 游戏内「音频」设置的主音量 → 输出设备本身")
    if audio.duration_s < MIN_DURATION_S:
        problems.append(f"时长只有 {audio.duration_s:.1f} s：太短，录不到 10 秒环境底噪")
        hints.append("每段素材前后各留 10 秒底噪（见 RECORDING_SCRIPTS.md 的模板）")

    verdict = "有警告但可用" if problems else "可用"
    if verdict != "可用":
        hints.append("警告不致命：可以先归档，但要在报告里如实写明，别让测得的偏差看起来像游戏问题")
    return verdict, problems, hints, measure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_capture", description="采集链路自检")
    parser.add_argument("file", type=Path, help="录好的测试片段（mkv / mp4 / mka / wav）")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径（默认自动查找）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.file.is_file():
        print(f"文件不存在：{args.file}", file=sys.stderr)
        return 2
    ffmpeg = audio_qa.find_ffmpeg(args.ffmpeg)
    try:
        verdict, problems, hints, measure = check(args.file, ffmpeg)
    except audio_qa.AudioQAError as exc:
        print(f"无法解析：{exc}", file=sys.stderr)
        print("提示：非 WAV 格式需要 ffmpeg；本机若无 ffmpeg，可先用 WAV 试录。", file=sys.stderr)
        return 2

    print(f"文件：{args.file}")
    if measure:
        print(f"  格式 {measure['codec']} · {measure['sample_rate']} Hz · {measure['channels']} 声道"
              f" · {measure['duration_s']:.1f} s")
        peak = f"{measure['peak_dbfs']:.1f} dBFS"
        lufs = "未测" if measure.get("lufs") is None else f"{measure['lufs']:.1f} LUFS"
        print(f"  峰值 {peak} · 响度 {lufs}")
    print(f"结论：{verdict}")
    for item in problems:
        print(f"  ! {item}")
    for item in hints:
        print(f"    → {item}")

    print("\n下一步：")
    print(f"  1) 需要纯音频时抽音轨（无损拷贝，不要二次编码）：")
    print(f"     ffmpeg -i \"{args.file.name}\" -vn -c:a copy \"{args.file.stem}.mka\"")
    print(f"  2) 放进对应用例目录（captures/target-game/<游戏-版本>/<用例>/）")
    print(f"  3) 出客观测量与报告骨架：")
    print(f"     python scripts/field_session.py <该目录> --case <用例编号> --dropout-min-ms 80")
    return 0 if verdict == "可用" else 1


if __name__ == "__main__":
    raise SystemExit(main())
