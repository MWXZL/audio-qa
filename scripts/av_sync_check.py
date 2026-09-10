#!/usr/bin/env python3
"""av_sync_check —— 把「文本状态时间线」与「音频活动段」交叉比对，给 bug_04 一个机器可判的第一遍。

为什么要有它
------------
bug_04（语音-字幕同步）以前只能靠人听。但两侧各有一半是可以机器量的：
- 文本侧：`scripts/subtitle_track.py` 量出每句文本的出现 / 消失 / 换行时刻；
- 音频侧：语音说话时音频是「活动」的，句与句之间通常有静音（角色语音面板里尤其明显）。
两侧一比，就能机械地分出四类事实：

    text_without_voice    文本出现了、但那一带音频没有活动（字幕出了、语音没跟上的候选）
    voice_without_text    音频有活动、但没有对应的文本状态（语音在播、文本没跟上的候选）
    no_gap_between        两段活动之间没有静音间隔（**连读或叠音**的候选——只有听才能定性）
    offset                文本起点与语音活动起点之差（毫秒，用于看响应延迟与漂移）

诚实边界（会写进产出）
- 活动段是从**能量**判的：素材里若一直有 BGM / 环境声（静音占比接近 0），活动法不适用，
  工具会明确说「本素材不适合用活动法」，而不是硬给一堆假结论；
- `no_gap_between` 只是候选：正常连读也是无缝的，定性必须回听（产出里会附上那一刻的音频片段）；
- 它**不判断文本内容**：说的和显示的是不是同一句，仍要靠听（或 ASR）。

用法
----
    python scripts\\av_sync_check.py --timeline <字幕时间线.json> --media <录像或音轨> --out <目录>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import audio_qa  # noqa: E402

MIN_ACTIVITY_S = 0.15      # 活动段短于此视为噪声
MIN_VOICE_S = 0.8          # 语音句短于此更像音效
MAX_VOICE_S = 12.0         # 语音句长于此更像音乐段
MIN_SILENCE_SHARE = 0.05   # 静音占比低于它，说明素材一直有声，活动法不适用
DEFAULT_NOISE_DB = -50.0   # 静音判定的噪声门限
DEFAULT_SILENCE_S = 0.12   # 静音判定的最短时长（秒）
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")


def voice_candidates(segments: Sequence[dict[str, float]], min_s: float = MIN_VOICE_S,
                     max_s: float = MAX_VOICE_S) -> list[dict[str, float]]:
    """把活动段按「像不像一句人声」筛一遍。纯函数。

    为什么必须筛：BGM 常驻的素材里没有真正的静音，活动段只能在**抬高门限**后才分得出来，
    于是音乐重音、打击声也会各成一段。人声句的长度通常落在 0.8–12 s，更短的是音效、
    更长的是音乐段。筛出来的仍只是**候选**——报告里不许写成「已确认的语音」。
    """
    return [dict(segment) for segment in segments
            if min_s <= segment["end"] - segment["start"] <= max_s]


def cut_evidence(media: Path, ffmpeg: str, rows: Sequence[dict[str, Any]], out_dir: Path,
                 prefix: str, margin: float = 0.4) -> int:
    """给每一段候选语音留一张截图与一段音频（截图用来读文本，音频用来听对应关系）。"""
    written = 0
    for index, row in enumerate(rows, 1):
        at = row["start"] + 0.2
        shot = out_dir / f"语音截图_{prefix}{index:02d}.jpg"
        clip = out_dir / f"语音片段_{prefix}{index:02d}.wav"
        start = max(0.0, row["start"] - margin)
        length = (row["end"] - row["start"]) + margin * 2
        done_shot = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{at:.3f}", "-i", str(media), "-frames:v", "1",
             "-vf", "scale=1280:-2", "-q:v", "4", "-y", str(shot)], capture_output=True)
        done_clip = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(media),
             "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-y", str(clip)],
            capture_output=True)
        if done_shot.returncode == 0 and shot.is_file() and shot.stat().st_size > 0:
            row["shot"] = shot.name
            written += 1
        if done_clip.returncode == 0 and clip.is_file() and clip.stat().st_size > 0:
            row["clip"] = clip.name
    return written


def voice_baseline_tsv(rows: Sequence[dict[str, Any]], media_name: str = "") -> str:
    """语音侧基线：`asr_align` 的表头，**语音起止预填**，字幕与文本待人工补。

    备注里写「在本段音轨的第几秒」而不是切好的片段文件名：片段是随时可再切的工作副本，
    而时间码是可复现的引用——报告里只留后者。
    """
    header = "\t".join(("句号", "语音起", "语音止", "字幕出现", "字幕消失", "字幕原文", "备注"))
    lines = [header]
    for index, row in enumerate(rows, 1):
        span = f"{row['start']:.2f}–{row['end']:.2f} s"
        lines.append("\t".join((
            f"V-{index:02d}", f"{row['start']:.3f}", f"{row['end']:.3f}", "", "", "",
            f"在 {media_name or '本段音轨'} 的 {span}；截图 {row.get('shot', '—')}",
        )))
    return "\n".join(lines) + "\n"



def parse_silences(stderr: str) -> tuple[float, list[dict[str, float]]]:
    """从 silencedetect 的输出里取 (总时长, 静音区间)。纯函数。

    为什么不用 `audio_qa` 的段内间隙：那个判据明确要求「间隙两侧都有信号」，
    所以**开头与结尾的静音不算间隙**。用它的补集当活动段，会把片头静音整段算成「有声」，
    于是第一条文本永远匹配到一个从 0 秒开始的活动段（实测踩过：偏差量成 −1200 ms）。
    """
    match = DURATION_RE.search(stderr or "")
    duration = (int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                if match else 0.0)
    silences: list[dict[str, float]] = []
    pending: float | None = None
    for kind, value in SILENCE_RE.findall(stderr or ""):
        if kind == "start":
            pending = max(0.0, float(value))
        elif pending is not None:
            silences.append({"start": pending, "end": float(value)})
            pending = None
    if pending is not None and duration:
        silences.append({"start": pending, "end": duration})
    return duration, silences


def activity_segments(duration_s: float, silences: Sequence[dict[str, float]],
                      min_activity_s: float = MIN_ACTIVITY_S) -> list[dict[str, float]]:
    """静音区间的补集 = 有声活动段。纯函数。"""
    if duration_s <= 0:
        return []
    ordered = sorted(silences, key=lambda item: item["start"])
    segments: list[dict[str, float]] = []
    cursor = 0.0
    for silence in ordered:
        begin, end = max(0.0, silence["start"]), min(duration_s, silence["end"])
        if begin > cursor:
            segments.append({"start": cursor, "end": begin})
        cursor = max(cursor, end)
    if cursor < duration_s:
        segments.append({"start": cursor, "end": duration_s})
    return [segment for segment in segments
            if segment["end"] - segment["start"] >= min_activity_s]


def overlap_seconds(a: dict[str, float], b: dict[str, float]) -> float:
    """两个区间的重叠秒数。纯函数。"""
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def crosscheck(states: Sequence[dict[str, Any]], segments: Sequence[dict[str, float]],
               duration_s: float, gap_tolerance: float = 0.35,
               has_timeline: bool = True) -> dict[str, Any]:
    """逐条文本状态找它的语音活动，并列出四类事实。纯函数。

    `has_timeline=False`（没传文本时间线）时**不做反向比对**：没有文本侧就谈不上
    「语音缺文本」，否则每一个活动段都会被报成差异——那是噪音，不是发现。
    """
    rows: list[dict[str, Any]] = []
    for index, state in enumerate(states, 1):
        start, end = float(state["start"]), float(state["end"])
        window = {"start": start, "end": end}
        covering = max(segments, key=lambda seg: overlap_seconds(window, seg), default=None)
        covered = overlap_seconds(window, covering) if covering else 0.0
        span = max(0.001, end - start)
        # 语音起点：与文本窗口重叠最多的活动段的起点；它为负值说明语音先起、文本后到
        voice_start = covering["start"] if covering and covered / span >= 0.2 else None
        rows.append({
            "index": index, "text_start": round(start, 3), "text_end": round(end, 3),
            "covered_share": round(covered / span, 2),
            "voice_start": None if voice_start is None else round(voice_start, 3),
            "offset_ms": None if voice_start is None else round((voice_start - start) * 1000),
        })

    findings: list[dict[str, Any]] = []
    for row in rows:
        if row["voice_start"] is None:
            findings.append({"kind": "text_without_voice", "index": row["index"],
                             "at": row["text_start"],
                             "detail": f"文本 {row['text_start']}–{row['text_end']}s 内没有语音活动"})
    # 反向：有活动、但没有任何文本状态覆盖它（没有文本侧时跳过，见 docstring）
    for segment in (segments if has_timeline else []):
        if segment["end"] - segment["start"] < MIN_ACTIVITY_S:
            continue
        covered = max((overlap_seconds(segment, {"start": s["text_start"], "end": s["text_end"]})
                       for s in rows), default=0.0)
        span = segment["end"] - segment["start"]
        if covered / span < 0.2:
            findings.append({"kind": "voice_without_text", "at": round(segment["start"], 3),
                             "detail": f"音频 {segment['start']:.2f}–{segment['end']:.2f}s 有活动，"
                                       "但没有对应文本状态"})
    # 活动段之间几乎没有静音间隔 → 连读 / 叠音的候选
    ordered = sorted(segments, key=lambda item: item["start"])
    for earlier, later in zip(ordered, ordered[1:]):
        between = later["start"] - earlier["end"]
        if 0 <= between <= gap_tolerance:
            findings.append({"kind": "no_gap_between", "at": round(earlier["end"], 3),
                             "detail": f"{earlier['end']:.2f}s → {later['start']:.2f}s 之间只有"
                                       f" {between * 1000:.0f} ms 静音（连读或叠音的候选）"})
    offsets = [row["offset_ms"] for row in rows if row["offset_ms"] is not None]
    ordered = sorted(segments, key=lambda item: item["start"])
    gaps = [round(later["start"] - earlier["end"], 3)
            for earlier, later in zip(ordered, ordered[1:])]
    return {"rows": rows, "findings": findings,
            "offset_median_ms": sorted(offsets)[len(offsets) // 2] if offsets else None,
            "offset_range_ms": (min(offsets), max(offsets)) if offsets else None,
            "gap_stats": {
                "count": len(gaps),
                "median_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
                "min_s": min(gaps) if gaps else None,
                "within_100ms": sum(1 for gap in gaps if gap <= 0.1),
                "within_350ms": sum(1 for gap in gaps if gap <= 0.35),
            },
            "text_states": len(rows), "activity_segments": len(segments),
            "silence_share": round(1 - sum(s["end"] - s["start"] for s in segments)
                                   / duration_s, 3) if duration_s else 0.0}


def measure(media: Path, ffmpeg: str, noise_db: float = DEFAULT_NOISE_DB,
            silence_s: float = DEFAULT_SILENCE_S) -> tuple[float, list[dict[str, float]]]:
    """跑一次 silencedetect，取该文件的（总时长, 静音区间）。"""
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(media), "-af",
         f"silencedetect=noise={noise_db:.0f}dB:d={silence_s:.3f}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    stderr = done.stderr or ""
    if "silence_start" not in stderr and "Duration" not in stderr:
        raise RuntimeError(f"silencedetect 失败：{stderr.strip()[:200]}")
    return parse_silences(stderr)


def render(result: dict[str, Any], media: Path, timeline: Path | None, dropout_ms: float,
           usable: bool) -> str:
    lines = [
        f"# 文本 × 语音活动 交叉检查：`{media.name}`",
        "",
        (f"- 文本时间线：`{timeline.name}`（由 `scripts/subtitle_track.py` 生成）；"
         if timeline is not None
         else "- 本次没有传文本时间线：只出「语音侧」结果（活动段 / 候选语音 / 截图 / 音频片段）；"),
        f"- 音频活动：`silencedetect` 门限 {dropout_ms:.0f} dB，静音占比"
        f" {result['silence_share'] * 100:.0f}%；",
        f"- 文本状态 {result['text_states']} 个、活动段 {result['activity_segments']} 个。",
        "",
    ]
    if not usable:
        lines += ["> **本素材不适合用活动法**：静音占比过低，说明全程都有 BGM / 环境声，",
                  "> 「有活动」无法区分「有人在说话」与「只是背景音乐」。",
                  "> 结论仍可基于文本时间线 + 人工听片段，但不要引用下表。", ""]
    lines += [
        "| 序号 | 文本起(s) | 文本止(s) | 语音活动覆盖 | 语音起(s) | 偏差(ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["rows"]:
        lines.append(f"| {row['index']} | {row['text_start']:.2f} | {row['text_end']:.2f} |"
                     f" {row['covered_share'] * 100:.0f}% |"
                     f" {row['voice_start'] if row['voice_start'] is not None else '—'} |"
                     f" {row['offset_ms'] if row['offset_ms'] is not None else '—'} |")
    if result["offset_median_ms"] is not None:
        low, high = result["offset_range_ms"]  # type: ignore[misc]
        lines += ["", f"文本起点与语音活动起点之差：中位 {result['offset_median_ms']} ms，"
                      f"范围 {low} ~ {high} ms。"]
    stats = result.get("gap_stats") or {}
    if stats.get("count"):
        lines += ["", f"相邻活动段之间的静音：{stats['count']} 处，中位 "
                      f"{stats['median_s']:.2f} s、最短 {stats['min_s']:.2f} s；"
                      f"≤0.10 s 的 {stats['within_100ms']} 处、≤0.35 s 的 "
                      f"{stats['within_350ms']} 处。",
                  "（≤0.10 s 才是「几乎无缝」；0.1–0.35 s 更像正常句间停顿，"
                  "所以判定叠音要看的是前一个数。）"]
    lines += ["", "## 需回听的候选", ""]
    if result["findings"]:
        lines += ["| 类别 | 位置(s) | 说明 |", "| --- | --- | --- |"]
        for item in result["findings"]:
            lines.append(f"| `{item['kind']}` | {item['at']} | {item['detail']} |")
        lines += ["", "`no_gap_between` 只是候选：正常连读也是无缝的，必须回听才能定性；",
                  "`text_without_voice` / `voice_without_text` 要先确认那一带的画面与音量，"
                  "再决定是不是真差异。", ""]
    else:
        lines += ["没有需要回听的候选。", ""]
    lines += [
        "## 这一遍不覆盖什么",
        "",
        "- **文本内容**：说的和显示的是不是同一句，只有听（或 ASR 转写）能判；",
        "- **多语言切换残留、语音被打断后从头重播**：属于时序之外的行为，要按用例步骤人工确认；",
        "- 活动法是**能量**判据：音量设置把某一路静音、或语音被压得很低时，会判成「没有语音活动」。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av_sync_check",
                                     description="文本时间线 × 音频活动段 的交叉检查")
    parser.add_argument("--timeline", type=Path,
                        help="subtitle_track 的 JSON；不给就只做「语音侧」（候选语音段 + 截图 + 音频）")
    parser.add_argument("--media", type=Path, required=True, help="对应的录像或音轨")
    parser.add_argument("--out", type=Path, required=True, help="产出目录")
    parser.add_argument("--noise-db", type=float, default=DEFAULT_NOISE_DB,
                        help="silencedetect 的噪声门限（默认 -50 dB）")
    parser.add_argument("--silence-s", type=float, default=DEFAULT_SILENCE_S,
                        help="判句间静音的最短时长（秒，默认 0.12）")
    parser.add_argument("--crops", action="store_true",
                        help="给每段候选语音留一张截图（用来读文本）")
    parser.add_argument("--clips", action="store_true",
                        help="给每段候选语音切一段音频（用来听对应关系）")
    parser.add_argument("--min-voice-s", type=float, default=MIN_VOICE_S,
                        help="候选语音的最短时长（秒）")
    parser.add_argument("--max-voice-s", type=float, default=MAX_VOICE_S,
                        help="候选语音的最长时长（秒）")
    parser.add_argument("--tag", default="", help="产出文件名前缀（多段素材时区分用）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffmpeg = audio_qa.find_ffmpeg(None)
    if ffmpeg is None:
        print("需要 ffmpeg", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {}
    if args.timeline is not None:
        if not args.timeline.is_file():
            print(f"时间线不存在：{args.timeline}", file=sys.stderr)
            return 2
        payload = json.loads(args.timeline.read_text(encoding="utf-8"))
    try:
        duration, silences = measure(args.media, ffmpeg, args.noise_db, args.silence_s)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    segments = activity_segments(duration, silences)
    result = crosscheck(payload.get("states", []), segments, duration,
                        has_timeline=bool(payload.get("states")))
    usable = result["silence_share"] >= MIN_SILENCE_SHARE
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.media.stem
    voices = voice_candidates(segments, args.min_voice_s, args.max_voice_s)
    if args.crops or args.clips:
        cut_evidence(args.media, ffmpeg, voices, args.out, f"{args.tag or ''}")
        (args.out / f"{stem}_语音基线.tsv").write_text(
            voice_baseline_tsv(voices, args.media.name), encoding="utf-8")
    (args.out / f"{stem}_交叉检查.json").write_text(
        json.dumps({"file": args.media.name, "duration": duration, "noise_db": args.noise_db,
                    "silence_s": args.silence_s, "silences": silences,
                    "activity_segments": segments, "usable": usable,
                    "voice_candidates": voices, **result},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / f"{stem}_交叉检查.md").write_text(
        render(result, args.media, args.timeline, args.noise_db, usable), encoding="utf-8")
    print(f"候选语音段（{args.min_voice_s:g}–{args.max_voice_s:g} s）：{len(voices)} 条")
    print(f"文本状态 {result['text_states']} 个 · 活动段 {result['activity_segments']} 个 · "
          f"静音占比 {result['silence_share'] * 100:.0f}% · 需回听候选 {len(result['findings'])} 条")
    if result["offset_median_ms"] is not None:
        print(f"偏差中位 {result['offset_median_ms']} ms（范围 {result['offset_range_ms']}）")
    if not usable:
        print("注意：静音占比过低，活动法不适用于本素材（产出里已写明）。")
    print(f"产出：{args.out / (stem + '_交叉检查.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
