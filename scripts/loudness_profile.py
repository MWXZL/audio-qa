#!/usr/bin/env python3
"""瞬时响度剖面：给「没打点」的素材找听辨入口，并画像一段录音的密度是否稳定。

为什么需要它：现场记录里最有用的一栏是「关键时间码」，但它靠人手按打点键——
实测漏按过（三段 bug_01 全都没打点），于是审阅者拿到三段 2.6 分钟的音频，不知道该听哪。
本工具用 ebur128 的逐 100 ms 瞬时响度（M）给出：
  - 瞬时响度的中位数与「高于 -20 LUFS 的时间占比」→ 这段录音的密度是否平稳；
  - 平均响度最高的若干窗口 → **听辨入口**（技能/命中音效最密集的地方）。

诚实边界：窗口只说明「这里声音最密」，**不是**「技能在 68 秒触发」。定性仍要人听。

用法
----
    python scripts\\loudness_profile.py captures\\target-game\\starrail-4.4\\bug_01_concurrency
    python scripts\\loudness_profile.py <目录或文件> --window 4 --top 4 --md 响度剖面.md
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import audio_qa  # noqa: E402

AUDIO_ONLY_EXTS = (".mka", ".m4a", ".flac", ".wav", ".ogg")
VIDEO_EXTS = (".mkv", ".mp4", ".mov")
# ebur128 的输出形如：t: 0.3998125  TARGET:-23 LUFS  M: -14.1 S:-120.7 I: -14.1 LUFS
SAMPLE_PATTERN = re.compile(r"t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+)")
SILENCE_FLOOR = -70.0     # 低于这个值的瞬时响度是数字静音，不参与统计


def parse_samples(stderr: str) -> list[tuple[float, float]]:
    """从 ebur128 的输出里取 (时刻, 瞬时响度)。纯函数。"""
    return [(float(time), float(value)) for time, value in SAMPLE_PATTERN.findall(stderr or "")]


def window_profile(samples: Sequence[tuple[float, float]], window: float = 4.0,
                   top: int = 4) -> list[dict[str, Any]]:
    """按窗口聚合，取不重叠的响度前 top 个窗口。纯函数。

    窗口按 `window` 的整数倍对齐（0–4、4–8…），而不是从最响的样本起算：
    对齐后的窗口在多次录制之间可比，时间点也是整齐的整数秒。
    """
    usable = [(time, value) for time, value in samples if value > SILENCE_FLOOR]
    if not usable:
        return []
    buckets: list[tuple[float, int]] = []
    for start in range(0, int(max(time for time, _ in usable)) + 1, max(1, int(window))):
        inside = [value for time, value in usable if start <= time < start + window]
        if inside:
            buckets.append((sum(inside) / len(inside), start))
    buckets.sort(reverse=True)
    picked: list[dict[str, Any]] = []
    for mean, start in buckets:
        if all(abs(start - item["start"]) >= window for item in picked):
            picked.append({"start": float(start), "end": float(start) + window, "mean": mean})
        if len(picked) == top:
            break
    return sorted(picked, key=lambda item: item["start"])


def summarize(samples: Sequence[tuple[float, float]],
              loud_dbfs: float = -20.0) -> dict[str, Any]:
    """中位瞬时响度与「高于 loud_dbfs 的时间占比」。纯函数。"""
    usable = sorted(value for _, value in samples if value > SILENCE_FLOOR)
    if not usable:
        return {"samples": 0, "median": None, "loud_share": None}
    median = usable[len(usable) // 2]
    loud = sum(1 for value in usable if value > loud_dbfs)
    return {"samples": len(usable), "median": median, "loud_share": loud / len(usable)}


def probe(media: Path, ffmpeg: str) -> str:
    """跑一次 ebur128，返回 stderr（所有测量信息都在那里）。"""
    done = subprocess.run([ffmpeg, "-hide_banner", "-i", str(media), "-af", "ebur128",
                           "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.stderr or ""


def collect_media(path: Path) -> list[Path]:
    """目录里优先取无损音轨；没有音轨时才退回录像（会慢很多）。"""
    if path.is_file():
        return [path]
    audio = sorted(item for item in path.iterdir()
                   if item.is_file() and item.suffix.lower() in AUDIO_ONLY_EXTS)
    if audio:
        return audio
    return sorted(item for item in path.iterdir()
                  if item.is_file() and item.suffix.lower() in VIDEO_EXTS)


def render(rows: Iterable[dict[str, Any]], window: float) -> str:
    lines = [
        "# 瞬时响度剖面（听辨入口）",
        "",
        "由 `scripts/loudness_profile.py` 生成，数据源为 ebur128 的逐 100 ms 瞬时响度（M）。",
        "",
        f"- 判据：低于 {SILENCE_FLOOR:.0f} LUFS 的样本视为数字静音，不参与统计；",
        f"- 「最密集窗口」＝平均瞬时响度最高的 {int(window)} 秒窗口（互不重叠）；",
        "- **这不是事件时刻**：窗口只说明「这里声音最密」，技能是否完整、有无残留循环声仍要人听。",
        "",
        "| 片段 | 有效样本 | 瞬时响度中位 | 高于 -20 LUFS 占比 | 最密集窗口（秒 → 平均 LUFS） |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        windows = "、".join(f"{item['start']:.0f}–{item['end']:.0f}（{item['mean']:.1f}）"
                            for item in row["windows"]) or "—"
        median = "—" if row["summary"]["median"] is None else f"{row['summary']['median']:.1f} LUFS"
        share = "—" if row["summary"]["loud_share"] is None else f"{row['summary']['loud_share'] * 100:.0f}%"
        lines.append(f"| `{row['file']}` | {row['summary']['samples']} | {median} | {share} | {windows} |")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loudness_profile",
                                     description="按瞬时响度给素材画密度剖面并给听辨入口")
    parser.add_argument("path", type=Path, help="目录或音频文件")
    parser.add_argument("--window", type=float, default=4.0, help="窗口秒数（默认 4）")
    parser.add_argument("--top", type=int, default=4, help="每个文件给几个窗口（默认 4）")
    parser.add_argument("--md", type=Path, help="把剖面写成 Markdown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffmpeg = audio_qa.find_ffmpeg(None)
    if ffmpeg is None:
        print("需要 ffmpeg 才能测瞬时响度", file=sys.stderr)
        return 2
    media = collect_media(args.path)
    if not media:
        print(f"目录里没有可分析的音频：{args.path}", file=sys.stderr)
        return 2
    rows = []
    for item in media:
        samples = parse_samples(probe(item, ffmpeg))
        rows.append({"file": item.name,
                     "summary": summarize(samples),
                     "windows": window_profile(samples, args.window, args.top)})
        summary = rows[-1]["summary"]
        print(f"{item.name}: 样本 {summary['samples']}"
              + (f" · 中位 {summary['median']:.1f} LUFS · 密集占比 {summary['loud_share'] * 100:.0f}%"
                 if summary["median"] is not None else " · 无有效样本"))
    text = render(rows, args.window)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"剖面：{args.md}")
    else:
        print()
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
