#!/usr/bin/env python3
"""footstep_analysis —— 从音频里量出「落脚点」与「材质切换点」，回答 bug_02 的两条判据。

bug_02 要判的是：**材质变化与落地同拍**、**跨边界最多过渡一步**。这两条都能从音频里量：

1. **落脚时刻**：脚步是短促瞬态，用「谱通量（spectral flux）」做 onset 检测，
   精度是跳帧长度（默认 5 ms），比人耳报时间码准得多；
2. **材质切换点**：不同材质的脚步音色不同（石板脆、雪地闷、金属有高频余响），
   对每一次落脚取谱质心 / 低频占比 / 衰减斜率，特征序列里的大跳变就是材质切换候选；
3. **同拍性**：切换点落在**每一次落脚上**才算同拍。若某一步的特征离前后两段的聚类中心都远，
   说明这一步是「一半旧材质一半新材质」——它就是要数的**过渡脚步**。

诚实边界（会写进产出）
- 特征只能区分**听感上确实不同**的材质；两种材质音色接近时，本工具会给不出切换点，
  这时报告写「未检出材质切换」，**不等于**「材质没切换」——需要人听或换更长的边界段；
- 它不认材质名字：段号是机器编的，对应哪种材质要人听/看画面；
- BGM 必须静音（游戏里把「背景音乐」调 0），否则瞬态会被音乐压掉——实测踩过同一坑。

用法
----
    python scripts\\footstep_analysis.py <录像或音轨> [--out <目录>] [--walk] [--clips]
"""
from __future__ import annotations

import argparse
import json
import math
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

SAMPLE_RATE = 48000
HOP_MS = 5.0            # onset 检测跳帧：决定时间精度
WIN_MS = 20.0           # STFT 窗长
FEATURE_MS = 200.0      # 每次落脚取多长做音色特征
MIN_GAP_MS = 150.0      # 两次落脚至少间隔（走路约 400–600 ms，跑步更短）
THRESHOLD_RATIO = 2.2   # 自适应阈值 = 局部中位 × 该系数 + 最小绝对量
CHANGE_RATIO = 1.8      # 特征跳变超过「相邻距离中位 × 该系数」算材质切换
LOW_BAND = (20.0, 250.0)
HIGH_BAND = (2000.0, 8000.0)


def frame_signal(samples: Any, frame: int, hop: int) -> Any:
    """把采样切成相互重叠的帧（不足一帧的尾巴丢掉）。纯函数。"""
    import numpy as np

    if len(samples) < frame:
        return np.zeros((0, frame))
    count = 1 + (len(samples) - frame) // hop
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    return samples[index]


def onset_envelope(samples: Any, sample_rate: int = SAMPLE_RATE,
                   hop_ms: float = HOP_MS, win_ms: float = WIN_MS) -> Any:
    """谱通量包络（正半波能量差分）。纯函数：返回每帧一个数。"""
    import numpy as np

    frame = int(sample_rate * win_ms / 1000)
    hop = max(1, int(sample_rate * hop_ms / 1000))
    frames = frame_signal(np.asarray(samples, dtype=np.float64), frame, hop)
    if frames.shape[0] < 2:
        return np.zeros(0)
    window = np.hanning(frame)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1))
    flux = np.zeros(spectrum.shape[0])
    difference = np.diff(spectrum, axis=0)
    flux[1:] = np.clip(difference, 0, None).sum(axis=1)
    return flux


def pick_onsets(envelope: Any, hop_ms: float = HOP_MS, min_gap_ms: float = MIN_GAP_MS,
                ratio: float = THRESHOLD_RATIO) -> list[float]:
    """自适应阈值 + 局部极大 + 最小间隔，返回落脚时刻（秒）。纯函数。

    为什么用「局部中位」而不是全局阈值：一段录音里脚步有轻有重、还有环境声，
    全局阈值要么漏掉轻的脚步，要么把环境声的起伏全报成脚步。
    """
    import numpy as np

    values = np.asarray(envelope, dtype=np.float64)
    if values.size < 3:
        return []
    window = max(3, int(300.0 / hop_ms))          # ±300 ms 的局部中位
    scene = np.array([
        float(np.median(values[max(0, i - window):i + window + 1])) for i in range(values.size)
    ])
    floor = max(float(np.median(values)) * 0.5, 1e-9)
    threshold = scene * ratio + floor
    gap = max(1, int(min_gap_ms / hop_ms))
    onsets: list[float] = []
    last_index = -gap
    for index in range(1, values.size - 1):
        if values[index] < threshold[index]:
            continue
        if values[index] < values[index - 1] or values[index] < values[index + 1]:
            continue
        if index - last_index < gap:
            continue
        onsets.append(round(index * hop_ms / 1000.0, 4))
        last_index = index
    return onsets


def _band_energy(spectrum: Any, freqs: Any, low: float, high: float) -> float:
    import numpy as np

    mask = (freqs >= low) & (freqs < high)
    return float(spectrum[mask].sum())


def step_features(samples: Any, onsets: Sequence[float], sample_rate: int = SAMPLE_RATE,
                  feature_ms: float = FEATURE_MS) -> list[dict[str, float]]:
    """每次落脚的音色特征：谱质心、低频占比、高频占比、衰减斜率。纯函数。"""
    import numpy as np

    signal = np.asarray(samples, dtype=np.float64)
    window = int(sample_rate * feature_ms / 1000)
    freqs = np.fft.rfftfreq(window, 1.0 / sample_rate)
    rows: list[dict[str, float]] = []
    for onset in onsets:
        start = int(onset * sample_rate)
        chunk = signal[start:start + window]
        if len(chunk) < window // 2:
            chunk = np.pad(chunk, (0, window - len(chunk)))
        spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
        total = float(spectrum.sum()) or 1e-9
        centroid = float((freqs * spectrum).sum() / total)
        low = _band_energy(spectrum, freqs, *LOW_BAND) / total
        high = _band_energy(spectrum, freqs, *HIGH_BAND) / total
        energy = chunk ** 2
        half = len(energy) // 2
        head = float(energy[:half].sum()) or 1e-9
        tail = float(energy[half:].sum())
        rows.append({
            "onset": round(float(onset), 4),
            "peak": round(float(np.abs(chunk).max()), 6),
            "centroid_hz": round(centroid, 1),
            "low_ratio": round(low, 4),
            "high_ratio": round(high, 4),
            "decay": round(min(tail / head, 10.0), 4),
        })
    return rows


def feature_vector(row: dict[str, float]) -> tuple[float, float, float]:
    """把一次落脚压成用于比较的三维向量（质心归一化、低频、衰减）。纯函数。"""
    return (math.log10(max(row.get("centroid_hz", 1.0), 1.0)),
            row.get("low_ratio", 0.0) * 4.0,
            math.log10(max(row.get("decay", 1e-3), 1e-3)))


def feature_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """两次落脚的音色距离。纯函数。"""
    left, right = feature_vector(a), feature_vector(b)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(left, right)))


def material_segments(features: Sequence[dict[str, float]],
                      ratio: float = CHANGE_RATIO) -> list[dict[str, Any]]:
    """按音色跳变把落脚序列切成材质段，并标出每次切换落在第几步。纯函数。"""
    if len(features) < 4:
        return []
    distances = [feature_distance(features[i - 1], features[i]) for i in range(1, len(features))]
    ordered = sorted(distances)
    typical = ordered[len(ordered) // 2] or 1e-9
    boundaries = [index + 1 for index, value in enumerate(distances)
                  if value > typical * ratio and value > 0.25]
    segments: list[dict[str, Any]] = []
    start = 0
    for boundary in boundaries + [len(features)]:
        chunk = features[start:boundary]
        if chunk:
            centroid = sum(row["centroid_hz"] for row in chunk) / len(chunk)
            segments.append({
                "index": len(segments) + 1,
                "first_step": start + 1,
                "last_step": boundary,
                "steps": len(chunk),
                "start_s": chunk[0]["onset"],
                "end_s": chunk[-1]["onset"],
                "mean_centroid_hz": round(centroid, 1),
                "mean_low_ratio": round(sum(r["low_ratio"] for r in chunk) / len(chunk), 4),
                "mean_decay": round(sum(r["decay"] for r in chunk) / len(chunk), 4),
                "boundary_step": None,
            })
        start = boundary
    for boundary, position in enumerate(boundaries):
        if boundary + 1 < len(segments):
            segments[boundary + 1]["boundary_step"] = position + 1
    return segments


def typical_jitter(features: Sequence[dict[str, float]],
                   segments: Sequence[dict[str, Any]]) -> float:
    """同一个材质段**内部**相邻两脚步的音色距离中位数（自然抖动）。纯函数。

    过渡脚步的判据必须是「相对自然抖动」而不是绝对阈值：绝对阈值下，
    正好落在两种材质中间的混合脚步反而因为「离两边都不算远」而被漏判（实测踩过）。
    """
    values: list[float] = []
    for segment in segments:
        chunk = features[segment["first_step"] - 1:segment["last_step"]]
        values += [feature_distance(chunk[i - 1], chunk[i]) for i in range(1, len(chunk))]
    if not values:
        return 0.0
    values.sort()
    return values[len(values) // 2]


def transition_steps(features: Sequence[dict[str, float]],
                     segments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出「过渡脚步」：音色与**前后两步都不同**的那些边界步。纯函数。

    这就是 bug_02 要数的东西——跨边界最多允许过渡一步。
    判据用的是**相邻两步**而不是段中心：段中心里可能已经混进了过渡步本身，
    用它来判会自我循环（实测踩过：混合步被吸收进新段后，离新段中心「很近」，漏判）。
    干净的一步（音色已完全属于新材质）与后一步几乎一样 → 不判过渡；
    混合的一步与前后都不同 → 判过渡。阈值 = max(3 × 段内自然抖动, 0.3)。
    """
    if len(segments) < 2:
        return []
    jitter = typical_jitter(features, segments)
    limit = max(3.0 * jitter, 0.3)
    rows: list[dict[str, Any]] = []
    for segment in segments[1:]:
        boundary = segment["boundary_step"]
        if not boundary:
            continue
        index = boundary - 1
        step = features[index]
        left = feature_distance(step, features[index - 1]) if index >= 1 else 0.0
        right = (feature_distance(step, features[index + 1])
                 if index + 1 < len(features) else 0.0)
        rows.append({"boundary_step": boundary, "onset_s": step["onset"],
                     "distance_to_previous": round(left, 3),
                     "distance_to_next": round(right, 3),
                     "typical_jitter": round(jitter, 3),
                     "is_transition": bool(min(left, right) > limit)})
    return rows


def decode_pcm(media: Path, ffmpeg: str, sample_rate: int = SAMPLE_RATE) -> Any:
    """解码成单声道浮点采样（脚步声在低频，单声道足够且更快）。"""
    import numpy as np

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "pcm.raw"
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(media), "-vn", "-ac", "1",
             "-ar", str(sample_rate), "-f", "s16le", "-y", str(raw)], capture_output=True)
        if done.returncode != 0 or not raw.is_file():
            raise RuntimeError(f"解码失败：{done.stderr.decode('utf-8', 'replace')[:200]}")
        data = raw.read_bytes()
    return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0


def analyze(media: Path, ffmpeg: str, args: argparse.Namespace) -> dict[str, Any]:
    samples = decode_pcm(media, ffmpeg)
    duration = len(samples) / SAMPLE_RATE
    envelope = onset_envelope(samples)
    onsets = pick_onsets(envelope, HOP_MS, args.min_gap_ms)
    features = step_features(samples, onsets)
    segments = material_segments(features, args.change_ratio)
    transitions = transition_steps(features, segments)
    gaps = [round(features[i]["onset"] - features[i - 1]["onset"], 3)
            for i in range(1, len(features))]
    return {
        "file": media.name,
        "duration_s": round(duration, 2),
        "sample_rate": SAMPLE_RATE,
        "hop_ms": HOP_MS,
        "steps": len(features),
        "features": features,
        "step_gaps_s": gaps,
        "median_gap_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "segments": segments,
        "transition_steps": transitions,
    }


def render(result: dict[str, Any], args: argparse.Namespace) -> str:
    lines = [
        f"# 脚步与材质切换分析：`{result['file']}`",
        "",
        f"- 时长 {result['duration_s']} s；onset 精度 {result['hop_ms']:g} ms"
        f"（跳帧长度），两次落脚最小间隔 {args.min_gap_ms:g} ms；",
        f"- 检出**落脚 {result['steps']} 次**，间隔中位 {result['median_gap_s']} s；",
        f"- 检出**材质段 {len(result['segments'])} 个**。",
        "",
    ]
    if not result["segments"]:
        lines += ["> **未检出材质切换**：脚步次数不足 4 次，或两种材质的音色太接近。",
                  "> 这不等于「材质没切换」——需要人听或把边界段录长一点再跑。", ""]
    else:
        lines += [
            "## 材质段（段号由机器编，对应哪种材质要人听/看画面确认）",
            "",
            "| 段号 | 脚步范围 | 步数 | 时间范围(s) | 平均谱质心(Hz) | 平均低频占比 | 平均衰减 | 切换落在第几步 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for segment in result["segments"]:
            lines.append(
                f"| {segment['index']} | 第 {segment['first_step']}–{segment['last_step']} 步 |"
                f" {segment['steps']} | {segment['start_s']:.2f} – {segment['end_s']:.2f} |"
                f" {segment['mean_centroid_hz']} | {segment['mean_low_ratio']} |"
                f" {segment['mean_decay']} |"
                f" {segment['boundary_step'] if segment['boundary_step'] else '—'} |")
        lines += ["", "## 判据对应", ""]
        if result["transition_steps"]:
            lines += ["| 切换点(步) | 时刻(s) | 距前一段中心 | 距后一段中心 | 判为过渡脚步 |",
                      "| --- | --- | --- | --- | --- |"]
            for row in result["transition_steps"]:
                lines.append(f"| {row['boundary_step']} | {row['onset_s']:.2f} |"
                             f" {row['distance_to_previous']} | {row['distance_to_next']} |"
                             f" {'**是**' if row['is_transition'] else '否'} |")
            counted = sum(1 for row in result["transition_steps"] if row["is_transition"])
            lines += ["", f"- **过渡脚步共 {counted} 处**（判据：跨边界最多允许 1 步）；",
                      "- 切换点都落在**某一次落脚**上（机器按落脚切段），所以「与落地同拍」"
                      "由「过渡脚步为 0」来体现：出现过渡脚步说明那一步的音色是两段混合的。"]
        else:
            lines += ["- 只检出一个材质段，没有可判的切换点。"]
    lines += [
        "",
        "## 这一遍不覆盖什么",
        "",
        "- **材质名字**：段号是机器编的，对应石板/雪地/金属要人听或看画面；",
        "- **音色接近的材质**：特征跳变不够大时不会报切换点（宁可不报，不猜）；",
        "- **BGM 未静音**：背景音乐会把脚步瞬态压掉，先确认游戏里「背景音乐」音量已调 0。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footstep_analysis",
                                     description="量落脚时刻与材质切换点（bug_02）")
    parser.add_argument("path", type=Path, help="录像或音轨（目录也行）")
    parser.add_argument("--out", type=Path, help="产出目录（默认与素材同目录）")
    parser.add_argument("--min-gap-ms", type=float, default=MIN_GAP_MS, help="两次落脚最小间隔")
    parser.add_argument("--change-ratio", type=float, default=CHANGE_RATIO,
                        help="特征跳变超过「相邻距离中位 × 该系数」算材质切换")
    return parser


def collect_media(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    preferred = sorted(item for item in path.glob("*.mka"))
    if preferred:
        return preferred
    return sorted(item for item in path.glob("*.mkv"))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffmpeg = audio_qa.find_ffmpeg(None)
    if ffmpeg is None:
        print("需要 ffmpeg 才能解码", file=sys.stderr)
        return 2
    media = collect_media(args.path)
    if not media:
        print(f"没有找到可分析的素材：{args.path}", file=sys.stderr)
        return 2
    out_dir = args.out or media[0].parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in media:
        try:
            result = analyze(item, ffmpeg, args)
        except RuntimeError as exc:
            print(f"{item.name}: {exc}", file=sys.stderr)
            return 2
        stem = item.stem
        (out_dir / f"{stem}_脚步分析.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / f"{stem}_脚步分析.md").write_text(render(result, args), encoding="utf-8")
        counted = sum(1 for row in result["transition_steps"] if row["is_transition"])
        print(f"{item.name}: 落脚 {result['steps']} 次 · 材质段 {len(result['segments'])} 个"
              f" · 过渡脚步 {counted} 处 · 间隔中位 {result['median_gap_s']} s")
    print(f"产出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
