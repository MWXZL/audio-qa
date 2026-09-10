#!/usr/bin/env python3
"""subtitle_track —— 从录像里量出**字幕出现 / 变化 / 消失的时刻**，并给每一句留一张截图。

为什么需要它
------------
bug_04（语音-字幕同步）要判两件事：字幕是否与当前语音对应、跳过是否只结束该结束的语音。
「语音」那一侧可以用音频量；「字幕」那一侧以前只能靠人盯着屏幕按秒表——实测这类手工时间码
误差 0.3–0.5 s，而我们要判的偏移本身就只有几百毫秒，等于用两把不准的尺子量同一个东西。

本工具换成**看字幕本身**：字幕出现在画面固定的横条里，文字一换，那一带的像素就变。
于是把那条横条裁出来、按固定帧率转成灰度原始帧，逐帧算「与上一帧的平均绝对差」，
差值超过阈值的就是一次字幕变化。**不需要 OCR**：只量时刻，不认字；文字靠留档截图由人抄。

产出（每段录像一套）
--------------------
- `<录像名>_字幕时间线.json`：每个状态的起止时刻、是否含字幕、判据数值；
- `字幕截图_<序号>.jpg`：每个含字幕状态一张**全画面缩略图**（同时含字幕与对话界面，
  满足「取证点必须同时显示字幕和对话界面」的要求）；
- `<录像名>_字幕基线.tsv`：`asr_align.py` 的基线表头，**字幕出现/消失已预填**，
  「字幕原文」留空待抄（抄的就是上一行的截图），备注列写着对应截图文件名。

诚实边界（写进产出，不许省）
- 时间码精度 = 1 / 采样帧率（默认 5 fps → ±0.2 s）；要更准就提高 `--fps`；
- 字幕横条要落在 `--region` 指定的纵向范围内；范围错了会量到血条/提示条之类的东西，
  所以产出里会打印每个状态的平均亮度占比，异常时会给出提醒；
- 本工具**不判断语音与字幕是否对应**：那一步要人听（或 ASR），工具只给两侧的事实。

用法
----
    python scripts\\subtitle_track.py <录像.mkv> --out <输出目录>
    python scripts\\subtitle_track.py <目录> --out <输出目录> --fps 10 --region 0.72,0.96
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import audio_qa  # noqa: E402

VIDEO_EXTS = (".mkv", ".mp4", ".mov")
# 默认字幕横条：屏幕下沿往上一点。星铁/原神的对话字幕都在这个区间里。
DEFAULT_REGION = (0.72, 0.96)
DEFAULT_FPS = 5.0
DEFAULT_WIDTH = 320          # 判据用的小图宽度：越小越快，文字变化仍然看得出
# 两个判据都用「亮像素」而不是灰度平均绝对差：实测在 320 宽的字幕条上，一整行字幕只占
# 约 1% 的像素，灰度平均差被大片暗背景稀释到 2–3（噪声级），而亮像素占比是干净的 0% ↔ 1.1%。
BRIGHT_PIXEL = 180           # 灰度高于它算「亮点」
BRIGHT_ABS = 0.2             # 亮点占比（%）绝对下限
BRIGHT_RATIO = 0.3           # 或取「最亮状态」的这个比例，两者取大
MASK_CHANGE = 0.15           # 亮暗类别发生翻转的像素占比（%）超过它算「字幕换了一行」
MIN_STATE_S = 0.4            # 短于此的状态视为抖动


def brightness(band_frames: Any) -> list[float]:
    """逐帧亮点占比（%）。纯函数。"""
    return [float((row > BRIGHT_PIXEL).mean() * 100.0) for row in band_frames]


def band_score(brights: Sequence[float]) -> float:
    """给一条横条打「像不像字幕条」的分。纯函数。

    字幕条的特征是「平时几乎没有亮点、偶尔整行亮起来」，而血条/技能栏/小地图那种 UI
    是**一直亮着**。所以取 95 分位与 25 分位的比值：字幕条接近无穷（低分位≈0），
    常亮 UI 接近 1。分母设下限是为了避免除以 0，以及给「一直很暗」的空条一个低分。
    """
    if not brights:
        return 0.0
    values = sorted(brights)
    low = values[int(len(values) * 0.25)]
    high = values[int(len(values) * 0.95)]
    return high / max(low, 0.05)


def candidate_bands(top: float = 0.55, bottom: float = 0.97, height: float = 0.06,
                    step: float = 0.02) -> list[tuple[float, float]]:
    """候选横条（自上而下等步长滑动）。纯函数。"""
    bands: list[tuple[float, float]] = []
    start = top
    while start + height <= bottom + 1e-9:
        bands.append((round(start, 3), round(start + height, 3)))
        start = round(start + step, 3)
    return bands


def probe_thumbnails(media: Path, ffmpeg: str, fps: float = 2.0, seconds: float = 40.0,
                     width: int = 160) -> Any:
    """一次性取一小段**整屏**灰度缩略帧（N, H, W）。

    为什么不在每条候选横条上各跑一次 ffmpeg：那要解码整段录像 40 多次，慢到不可用。
    先整屏取一次低分辨率帧，横条评分全部在内存里算。
    """
    import numpy as np

    filter_chain = f"fps={fps},scale={width}:-2,format=gray"
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "thumb.raw"
        done = subprocess.run([ffmpeg, "-v", "error", "-t", f"{seconds:.1f}", "-i", str(media),
                               "-vf", filter_chain, "-f", "rawvideo", "-pix_fmt", "gray", str(raw)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0 or not raw.is_file() or raw.stat().st_size == 0:
            raise RuntimeError(f"取缩略帧失败：{(done.stderr or '').strip()[:200]}")
        buffer = raw.read_bytes()
    flat = np.frombuffer(buffer, dtype=np.uint8)
    height = _scaled_height(media, ffmpeg, width)
    frame_pixels = width * height
    count = len(flat) // frame_pixels
    if count < 2:
        raise RuntimeError("缩略帧不足 2 帧")
    return flat[: count * frame_pixels].reshape(count, height, width)


def _scaled_height(media: Path, ffmpeg: str, width: int) -> int:
    """整屏缩略帧的高度。"""
    done = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(media), "-vf", f"scale={width}:-2,format=gray",
         "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError("无法确定缩略帧尺寸")
    return len(done.stdout) // width


def band_scores(frames: Any, bands: Sequence[tuple[float, float]] | None = None) -> list[dict[str, Any]]:
    """在整屏缩略帧上给每条候选横条打分。纯函数（只依赖 numpy 数组）。"""
    import numpy as np

    count, height, _ = frames.shape
    rows: list[dict[str, Any]] = []
    for top, bottom in (bands if bands is not None else candidate_bands()):
        y0, y1 = int(top * height), max(int(bottom * height), int(top * height) + 1)
        band = frames[:, y0:y1, :]
        brights = (band > BRIGHT_PIXEL).mean(axis=(1, 2)) * 100.0
        rows.append({"band": (top, bottom), "score": round(band_score(list(brights)), 2),
                     "peak": round(float(brights.max()), 2),
                     "low": round(float(np.percentile(brights, 25)), 2)})
    return rows


def choose_band(media: Path, ffmpeg: str, fps: float = 2.0, seconds: float = 40.0,
                width: int = 160, top: float = 0.05, bottom: float = 0.99,
                height: float = 0.06, step: float = 0.02
                ) -> tuple[tuple[float, float], list[dict[str, Any]]]:
    """自动找出「字幕 / 文本」所在的横条，返回 (选中区间, 全部候选的评分表)。

    为什么必须自动找：位置随游戏与界面变——战斗界面的技能栏在最下方，角色面板的文本在
    画面中部，而血条、小地图、提示又会挤在同一带。猜错会把技能栏当成字幕，量出一堆假句。
    评分依据见 `band_score`：字幕条「平时暗、偶尔整行亮」，常亮 UI 比值接近 1。
    """
    frames = probe_thumbnails(media, ffmpeg, fps, seconds, width)
    table = band_scores(frames, candidate_bands(top, bottom, height, step))
    if not table:
        raise RuntimeError("候选横条为空")
    best = max(table, key=lambda item: item["score"])
    return tuple(best["band"]), table  # type: ignore[return-value]


def mask_changes(band_frames: Any) -> list[float]:
    """逐帧「亮暗类别翻转」的像素占比（%）。纯函数。

    比灰度平均绝对差灵敏得多：字幕换行时文字位置整体位移，翻转占比远高于压缩噪声。
    `[0]` 位置无上一帧，记 0。
    """
    masks = band_frames > BRIGHT_PIXEL
    values = [0.0]
    for index in range(1, len(masks)):
        values.append(float((masks[index] != masks[index - 1]).mean() * 100.0))
    return values


def states_from_signals(brights: Sequence[float], changes: Sequence[float], fps: float,
                        bright_floor: float | None = None,
                        change_threshold: float = MASK_CHANGE,
                        min_state_s: float = MIN_STATE_S) -> list[dict[str, Any]]:
    """由「逐帧亮点占比」与「逐帧掩码翻转占比」切出字幕状态。纯函数。

    两层判据各管一件事：
    - **亮点占比**过门限 = 这一带出现了字幕（字幕消失后占比回到基线）；
    - 占比过门限的连续段里，**掩码翻转**超过阈值 = 字幕换成了另一行（中间没有空档）。
    """
    if not brights or len(brights) != len(changes):
        return []
    if bright_floor is None:
        bright_floor = max(BRIGHT_ABS, max(brights) * BRIGHT_RATIO)

    def is_text(index: int) -> bool:
        return brights[index] >= bright_floor

    states: list[dict[str, Any]] = []
    start = None
    for index in range(len(brights)):
        if is_text(index) and start is None:
            start = index
            continue
        if start is None:
            continue
        # 段落结束：字幕消失，或字幕换行
        if not is_text(index) or changes[index] > change_threshold:
            states.append((start, index))
            start = index if is_text(index) else None

    if start is not None:
        states.append((start, len(brights)))

    rows: list[dict[str, Any]] = []
    for begin, end in states:
        if end <= begin:
            continue
        span = range(begin, end)
        rows.append({
            "start": round(begin / fps, 3),
            "end": round(end / fps, 3),
            "duration": round((end - begin) / fps, 3),
            "bright": round(sum(brights[i] for i in span) / len(span), 2),
            "has_text": True,
        })
    # 抖动的处理要**整段吃掉**：一次抖动会切出「长–极短–长」三块，
    # 只把中间那块并进前一段，仍然会留下一个假边界（一行字幕被拆成两句）。
    merged: list[dict[str, Any]] = []
    absorb_next = False
    for row in rows:
        if merged and (row["duration"] < min_state_s or absorb_next):
            merged[-1]["end"] = row["end"]
            merged[-1]["duration"] = round(merged[-1]["end"] - merged[-1]["start"], 3)
            absorb_next = row["duration"] < min_state_s
            continue
        merged.append(dict(row))
        absorb_next = False
    return [row for row in merged if row["has_text"]]


def probe_band(media: Path, ffmpeg: str, region: tuple[float, float], fps: float,
               width: int) -> tuple[list[float], list[float]]:
    """把字幕横条转成灰度原始帧，返回 (逐帧亮点占比, 逐帧掩码翻转占比)。"""
    import numpy as np

    top, bottom = region
    filter_chain = (f"crop=iw:ih*{bottom - top:.4f}:0:ih*{top:.4f},"
                    f"fps={fps},scale={width}:-2,format=gray")
    height = _frame_height(media, ffmpeg, region, width)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "band.raw"
        done = subprocess.run([ffmpeg, "-v", "error", "-i", str(media), "-vf", filter_chain,
                               "-f", "rawvideo", "-pix_fmt", "gray", str(raw)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0 or not raw.is_file() or raw.stat().st_size == 0:
            raise RuntimeError(f"取帧失败：{(done.stderr or '').strip()[:200]}")
        buffer = raw.read_bytes()
    flat = np.frombuffer(buffer, dtype=np.uint8)
    frame_pixels = width * height
    count = len(flat) // frame_pixels
    if count < 2:
        return [], []
    frames = flat[: count * frame_pixels].reshape(count, frame_pixels)
    return brightness(frames), mask_changes(frames)


def _frame_height(media: Path, ffmpeg: str, region: tuple[float, float], width: int) -> int:
    """问 ffmpeg 要裁剪后的帧高（scale=width:-2 之后必然是偶数）。"""
    top, bottom = region
    done = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(media), "-vf",
         f"crop=iw:ih*{bottom - top:.4f}:0:ih*{top:.4f},scale={width}:-2,format=gray",
         "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError("无法确定字幕条尺寸")
    return len(done.stdout) // width


def crop_shots(media: Path, ffmpeg: str, states: Sequence[dict[str, Any]], out_dir: Path,
               prefix: str, width: int = 1280) -> list[Path]:
    """给每个状态留一张全画面缩略图（同时包含字幕与对话界面）。"""
    written: list[Path] = []
    for index, state in enumerate(states, 1):
        at = state["start"] + min(state["duration"], 1.0) / 2.0
        target = out_dir / f"字幕截图_{prefix}{index:02d}.jpg"
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{at:.3f}", "-i", str(media), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(target)],
            capture_output=True)
        if done.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            state["shot"] = target.name
            written.append(target)
    return written


def cut_clips(media: Path, ffmpeg: str, states: Sequence[dict[str, Any]], out_dir: Path,
              prefix: str, margin: float = 0.5) -> list[Path]:
    """按字幕状态切出每句的音频（便于逐句听：语音与字幕是否对应）。"""
    written: list[Path] = []
    for index, state in enumerate(states, 1):
        start = max(0.0, state["start"] - margin)
        length = (state["end"] - state["start"]) + margin * 2
        target = out_dir / f"句_{prefix}{index:02d}.wav"
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(media),
             "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-y", str(target)],
            capture_output=True)
        if done.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            state["clip"] = target.name
            written.append(target)
    return written


def render(media: Path, states: Sequence[dict[str, Any]], fps: float,
           region: tuple[float, float] | None = None,
           band_table: Sequence[dict[str, Any]] = ()) -> str:
    lines = [
        f"# 字幕时间线：`{media.name}`",
        "",
        f"- 采样帧率 {fps:g} fps → 时间码精度 ±{1 / fps:.2f} s；"
        + (f"字幕条取画面高度 {region[0] * 100:.0f}%–{region[1] * 100:.0f}%"
           f"（{'自动选定' if band_table else '手动指定'}）；" if region else ""),
        "- 判据：字幕条**亮点占比**判断这一带有无字幕、**亮暗翻转占比**判断字幕是否换行"
        "（**不做 OCR**，只量时刻）；",
        "- 「字幕原文」列要人看截图抄写：截图同时含字幕与对话界面；",
        f"- 检出含字幕的状态：{len(states)} 个。",
        "",
    ]
    if band_table:
        best = max(band_table, key=lambda item: item["score"])
        lines += [
            "## 为什么选这条横条",
            "",
            "字幕条的特征是「平时几乎没有亮点、偶尔整行亮起」，常亮的血条 / 技能栏 / 小地图不是。"
            "评分 = 亮占比 95 分位 / 25 分位（越低分位接近 0 越像字幕条）。",
            "",
            "| 横条（画面高度 %） | 评分 | 峰值亮占比(%) | 低分位亮占比(%) |",
            "| --- | --- | --- | --- |",
        ]
        for item in sorted(band_table, key=lambda row: -row["score"])[:5]:
            mark = " ← 选用" if item["band"] == best["band"] else ""
            lines.append(f"| {item['band'][0] * 100:.0f}–{item['band'][1] * 100:.0f}{mark} |"
                         f" {item['score']} | {item['peak']} | {item['low']} |")
        lines.append("")
    lines += [
        "| 序号 | 字幕出现(s) | 字幕消失(s) | 时长(s) | 亮度占比(%) | 截图 | 音频片段 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, state in enumerate(states, 1):
        lines.append(
            f"| {index} | {state['start']:.2f} | {state['end']:.2f} | {state['duration']:.2f} |"
            f" {state['bright']:.2f} | `{state.get('shot', '—')}` |"
            f" {('`' + state['clip'] + '`') if state.get('clip') else '—'} |"
        )
    lines += [
        "",
        "## 怎么用这份时间线",
        "",
        "1. 看截图抄下字幕原文 → 填进同目录的 `*_字幕基线.tsv`；",
        "2. 逐句听音频片段，判断「语音是否与字幕对应」「跳过之后这一句是不是只结束自己」；",
        "3. 填完基线后跑 `python scripts\\asr_align.py --baseline <基线.tsv> --asr <转写.json>`"
        "（没有 ASR 时，基线里的时间码本身就已经是可引用的客观测量）。",
        "",
    ]
    return "\n".join(lines)


def baseline_tsv(states: Sequence[dict[str, Any]]) -> str:
    """生成 asr_align 的基线表（字幕出现/消失已预填，原文待抄）。"""
    header = "\t".join(("句号", "语音起", "语音止", "字幕出现", "字幕消失", "字幕原文", "备注"))
    rows = [header]
    for index, state in enumerate(states, 1):
        rows.append("\t".join((
            f"D-{index:02d}", "", "",
            f"{state['start']:.3f}", f"{state['end']:.3f}", "",
            f"字幕原文见截图 {state.get('shot', '—')}；语音起止待填",
        )))
    return "\n".join(rows) + "\n"


def process(media: Path, out_dir: Path, ffmpeg: str, args: argparse.Namespace,
            prefix: str = "") -> dict[str, Any]:
    region = args.region
    band_table: list[dict[str, Any]] = []
    if args.auto_region:
        region, band_table = choose_band(media, ffmpeg, args.scan_fps, args.scan_seconds,
                                         step=args.scan_step)
        print(f"  自动选定的文本横条：画面高度 {region[0] * 100:.0f}%–{region[1] * 100:.0f}%"
              f"（评分 {max(item['score'] for item in band_table):.2f}）")
    brights, changes = probe_band(media, ffmpeg, region, args.fps, args.width)
    states = states_from_signals(brights, changes, args.fps, args.bright_floor,
                                 args.change, args.min_state)
    args.region = region
    if states:
        crop_shots(media, ffmpeg, states, out_dir, prefix, args.shot_width)
        if args.clips:
            cut_clips(media, ffmpeg, states, out_dir, prefix)
    stem = media.stem
    (out_dir / f"{stem}_字幕时间线.json").write_text(
        json.dumps({"file": media.name, "fps": args.fps, "region": list(region),
                    "auto_region": bool(args.auto_region), "band_table": band_table,
                    "states": states}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{stem}_字幕基线.tsv").write_text(baseline_tsv(states), encoding="utf-8")
    (out_dir / f"{stem}_字幕时间线.md").write_text(
        render(media, states, args.fps, region, band_table), encoding="utf-8")
    return {"file": media.name, "states": len(states), "region": region,
            "median_bright": round(statistics.median([s["bright"] for s in states]), 2)
            if states else 0.0}


def collect_media(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(item for item in path.iterdir()
                  if item.is_file() and item.suffix.lower() in VIDEO_EXTS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subtitle_track",
                                     description="量出字幕出现/变化/消失的时刻并留档截图")
    parser.add_argument("path", type=Path, help="录像文件或目录")
    parser.add_argument("--out", type=Path, required=True, help="产出目录")
    parser.add_argument("--region", default=f"{DEFAULT_REGION[0]},{DEFAULT_REGION[1]}",
                        help="字幕横条的纵向范围（画面高度的比例，如 0.72,0.96）；"
                             "用 --auto-region 时此项被忽略")
    parser.add_argument("--auto-region", action="store_true",
                        help="自动找出「字幕 / 文本」所在的横条（推荐：不同界面的位置不一样）")
    parser.add_argument("--scan-fps", type=float, default=2.0, help="自动找横条时的采样帧率")
    parser.add_argument("--scan-seconds", type=float, default=40.0,
                        help="自动找横条时只看开头这么多秒（默认 40，够覆盖多句）")
    parser.add_argument("--scan-step", type=float, default=0.02, help="候选横条的滑动步长")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="采样帧率（默认 5）")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="判据用小图宽度")
    parser.add_argument("--change", type=float, default=MASK_CHANGE,
                        help="判定「字幕换行」的亮暗翻转像素占比阈值（默认 0.15）")
    parser.add_argument("--bright-floor", type=float, default=None,
                        help="判定「这一带有字幕」的亮点占比下限；不给则按最亮状态自适应")
    parser.add_argument("--min-state", type=float, default=MIN_STATE_S, help="最短状态秒数")
    parser.add_argument("--shot-width", type=int, default=1280, help="存档截图宽度")
    parser.add_argument("--clips", action="store_true", help="同时切出每句的音频片段")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.region = tuple(float(part) for part in str(args.region).split(","))  # type: ignore[assignment]
    ffmpeg = audio_qa.find_ffmpeg(None)
    if ffmpeg is None:
        print("需要 ffmpeg 才能取帧", file=sys.stderr)
        return 2
    media = collect_media(args.path)
    if not media:
        print(f"没有找到录像：{args.path}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(media, 1):
        prefix = f"{index:02d}_" if len(media) > 1 else ""
        try:
            result = process(item, args.out, ffmpeg, args, prefix)
        except RuntimeError as exc:
            print(f"{item.name}: {exc}", file=sys.stderr)
            return 2
        print(f"{item.name}: 检出 {result['states']} 句字幕"
              f"（亮度占比中位 {result['median_bright']}%）")
    print(f"产出目录：{args.out}")
    print("下一步：看截图抄字幕原文填进 *_字幕基线.tsv；逐句听音频片段判定对应关系。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
