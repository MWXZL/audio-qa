#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio_qa —— 游戏音频资产质量检查 / 自动化回归工具

用途
    批量扫描音频资产库，检出可量化的音频缺陷（削波、直流偏移、异常静音、
    loop 点不连续、假立体声、单声道哑掉、响度不一致、重复素材、格式混乱），
    输出机器可读 JSON 与人可读 Markdown 报告，非零退出码便于接入 CI。

依赖边界
    时域检测（削波 / 直流偏移 / 静音 / loop 点 / 声道一致性 / 内容哈希）
        全部由 Python 标准库实现（wave + array），无第三方依赖。
    LUFS 整合响度与 True Peak 依赖 ffmpeg 的 ebur128 滤镜——这两项需要
        K 加权滤波与 4 倍过采样，重新实现收益低、出错风险高，故直接复用。
        无 ffmpeg 时自动跳过这两项，其余检查照常工作。
    非 WAV 容器（ogg / mp3 / flac ...）的解码同样走 ffmpeg。

子命令
    scan     扫描目录，产出报告，可作为回归基线
    compare  比对两份 scan JSON，输出资产变更与音频表现差异
    report   把 FAIL/WARN 项交给 LLM 起草缺陷报告（可选，需 DEEPSEEK_API_KEY）

退出码
    0 无 FAIL   1 存在 FAIL   2 工具自身错误
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

VERSION = "0.1.0"

DEFAULT_EXTS = (".wav", ".ogg", ".mp3", ".flac", ".aif", ".aiff", ".m4a")

SEVERITY_ORDER = {"PASS": 0, "INFO": 1, "WARN": 2, "FAIL": 3}

# 4 字节有符号整型的 typecode（平台上 'i' 通常就是 4 字节，兜底用 'l'）
_I32 = "i" if array("i").itemsize == 4 else "l"

# 无符号 8bit -> 有符号 8bit 的字节映射表，用 bytes.translate 走 C 速度
_U8_TO_S8 = bytes((b - 128) & 0xFF for b in range(256))

# 收集削波样点位置的上限，超过则只报总数不报逐个时间戳
_CLIP_POS_LIMIT = 200_000

_CH_NAME_TO_COUNT = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "quad": 4,
    "4.0": 4,
    "5.0": 5,
    "5.1": 6,
    "7.1": 8,
}


class AudioQAError(Exception):
    """工具级错误（无法解码、缺少外部程序等）。"""


# --------------------------------------------------------------------------
# 阈值配置
# --------------------------------------------------------------------------
@dataclass
class Thresholds:
    """判定阈值。默认值取游戏音频常见工程约定，可由命令行覆盖。"""

    clip_min_run: int = 3          # 连续多少个满刻度样点算一次削波
    dc_offset_max: float = 0.01    # 直流偏移占满刻度比例上限
    silence_floor_dbfs: float = -60.0
    head_silence_max_ms: float = 100.0   # 头部空白过长 = 触发延迟
    tail_silence_max_ms: float = 500.0   # 尾部零填充过长 = 白占内存
    oneshot_max_duration_s: float = 8.0  # 超过此时长不按 Event 触发的一次性音效判首部空白
    loop_step_ratio_max: float = 3.0     # 接缝跳变 / 素材自身 p99 斜率 的上限
    loop_step_floor: float = 0.01         # 跳变低于此比例一律不报，避免噪声级误报
    loop_onset_max: float = 0.05          # 循环素材起点电平上限（首次起播的 click）
    true_peak_max_dbtp: float | None = None   # 显式规范上限（如 -1.0）；给了就按规范判 FAIL
    true_peak_overshoot_dbtp: float = 0.0     # 无规范时的客观红线：超过 0 dBTP 即采样间过冲
    true_peak_headroom_dbtp: float = -1.0     # 库级余量统计的参考线，不用于单文件判定
    lufs_target: float | None = None      # 显式响度目标；给了就按规范判，不看素材库分布
    lufs_tolerance: float = 2.0           # 允许偏离基准的 LU（显式目标下为硬容差）
    lufs_min_group: int = 5               # 少于这么多同类素材不推断基准，避免拿两三个文件当标准
    lufs_spread_k: float = 3.0            # 无显式目标时，容差 = max(容差, k × MAD)
    lufs_gate_floor: float = -69.0        # 低于此值视为未达 EBU R128 门控条件，不是"太轻"
    lufs_min_duration_s: float = 0.4      # 短于一个 400 ms 门控块，整合响度无法定义
    silence_window_ms: float = 10.0       # 静音扫描窗口
    dropout_min_ms: float = 0.0           # 段内静音间隙（dropout）下限；0 = 关闭

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_min_run": self.clip_min_run,
            "dc_offset_max": self.dc_offset_max,
            "silence_floor_dbfs": self.silence_floor_dbfs,
            "head_silence_max_ms": self.head_silence_max_ms,
            "tail_silence_max_ms": self.tail_silence_max_ms,
            "oneshot_max_duration_s": self.oneshot_max_duration_s,
            "loop_step_ratio_max": self.loop_step_ratio_max,
            "loop_step_floor": self.loop_step_floor,
            "loop_onset_max": self.loop_onset_max,
            "true_peak_max_dbtp": self.true_peak_max_dbtp,
            "true_peak_overshoot_dbtp": self.true_peak_overshoot_dbtp,
            "true_peak_headroom_dbtp": self.true_peak_headroom_dbtp,
            "lufs_target": self.lufs_target,
            "lufs_tolerance": self.lufs_tolerance,
            "lufs_min_group": self.lufs_min_group,
            "lufs_spread_k": self.lufs_spread_k,
            "lufs_gate_floor": self.lufs_gate_floor,
            "lufs_min_duration_s": self.lufs_min_duration_s,
            "silence_window_ms": self.silence_window_ms,
            "dropout_min_ms": self.dropout_min_ms,
        }


@dataclass
class Issue:
    check: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class AudioData:
    """已解码的音频，逐声道存放整型样点。"""

    codec: str
    sample_fmt: str
    sample_rate: int
    channels: int
    bit_depth: int | None
    frames: int
    full_scale: int
    lsb: int            # 量化步长（24bit 被放大到 32bit 存储时为 256）
    chans: list[array]
    content_sha256: str
    decoded_via: str

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


@dataclass
class FileResult:
    path: str
    codec: str = ""
    sample_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0
    bit_depth: int | None = None
    frames: int = 0
    duration_s: float = 0.0
    peak_dbfs: float | None = None
    dc_offset: float | None = None
    head_silence_ms: float | None = None
    tail_silence_ms: float | None = None
    tail_zero_ms: float | None = None
    dropout_count: int = 0
    dropout_max_ms: float | None = None
    dropout_total_ms: float | None = None
    lufs: float | None = None
    lra: float | None = None
    true_peak_dbtp: float | None = None
    file_sha256: str = ""
    content_sha256: str = ""
    decoded_via: str = ""
    size_bytes: int = 0
    issues: list[Issue] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        worst = "PASS"
        for issue in self.issues:
            if SEVERITY_ORDER[issue.severity] > SEVERITY_ORDER[worst]:
                worst = issue.severity
        return worst

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "codec": self.codec,
            "sample_fmt": self.sample_fmt,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bit_depth": self.bit_depth,
            "frames": self.frames,
            "duration_s": round(self.duration_s, 4),
            "peak_dbfs": _round_or_none(self.peak_dbfs, 2),
            "dc_offset": _round_or_none(self.dc_offset, 5),
            "head_silence_ms": _round_or_none(self.head_silence_ms, 1),
            "tail_silence_ms": _round_or_none(self.tail_silence_ms, 1),
            "tail_zero_ms": _round_or_none(self.tail_zero_ms, 1),
            "dropout_count": self.dropout_count,
            "dropout_max_ms": _round_or_none(self.dropout_max_ms, 1),
            "dropout_total_ms": _round_or_none(self.dropout_total_ms, 1),
            "lufs": _round_or_none(self.lufs, 2),
            "lra": _round_or_none(self.lra, 2),
            "true_peak_dbtp": _round_or_none(self.true_peak_dbtp, 2),
            "file_sha256": self.file_sha256,
            "content_sha256": self.content_sha256,
            "decoded_via": self.decoded_via,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "issues": [i.as_dict() for i in self.issues],
        }


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


# --------------------------------------------------------------------------
# ffmpeg 定位
# --------------------------------------------------------------------------
def find_ffmpeg(explicit: str | None = None) -> str | None:
    """按 显式参数 -> 环境变量 -> PATH -> imageio-ffmpeg 的顺序定位 ffmpeg。"""
    for candidate in (explicit, os.environ.get("AUDIO_QA_FFMPEG")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # 可选兜底：pip install imageio-ffmpeg 会自带一份 ffmpeg 可执行文件
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return None


def _run_ffmpeg(ffmpeg: str, args: Sequence[str], capture_stdout: bool) -> tuple[int, bytes, str]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", *args],
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout or b"", (proc.stderr or b"").decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------
def _decode_pcm(raw: bytes, sampwidth: int) -> tuple[array, int, int]:
    """把交织的整型 PCM 字节解成 array，返回 (样点, 满刻度, 量化步长)。"""
    if sampwidth == 1:  # WAV 的 8bit 是无符号
        samples = array("b")
        samples.frombytes(raw.translate(_U8_TO_S8))
        return samples, 128, 1
    if sampwidth == 2:
        samples = array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
        return samples, 1 << 15, 1
    if sampwidth == 3:
        # 24bit 无对应 typecode：在每个样点低位补一个 0 字节升成 32bit，
        # 数值等于原值 * 256，符号位天然落在最高字节，故符号自动保持。
        count = len(raw) // 3
        src = raw[: count * 3]
        buf = bytearray(count * 4)
        buf[1::4] = src[0::3]
        buf[2::4] = src[1::3]
        buf[3::4] = src[2::3]
        samples = array(_I32)
        samples.frombytes(bytes(buf))
        if sys.byteorder == "big":
            samples.byteswap()
        return samples, 1 << 31, 256
    if sampwidth == 4:
        samples = array(_I32)
        samples.frombytes(raw[: len(raw) // 4 * 4])
        if sys.byteorder == "big":
            samples.byteswap()
        return samples, 1 << 31, 1
    raise AudioQAError(f"不支持的位宽：{sampwidth * 8} bit")


def _deinterleave(samples: array, channels: int) -> list[array]:
    if channels == 1:
        return [samples]
    return [samples[ch::channels] for ch in range(channels)]


def _load_wav(path: Path) -> AudioData:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sampwidth = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if channels <= 0 or sample_rate <= 0:
        raise AudioQAError("WAV 头信息非法")
    samples, full_scale, lsb = _decode_pcm(raw, sampwidth)
    actual_frames = len(samples) // channels
    if actual_frames < frames:  # 头里写的帧数比实际数据多，说明文件被截断
        frames = actual_frames
    chans = _deinterleave(samples, channels)
    del samples
    return AudioData(
        codec="pcm",
        sample_fmt=f"s{sampwidth * 8}" if sampwidth > 1 else "u8",
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=sampwidth * 8,
        frames=frames,
        full_scale=full_scale,
        lsb=lsb,
        chans=chans,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        decoded_via="wave",
    )


_STREAM_RE = re.compile(
    r"Stream #\d+:\d+[^:]*: Audio: (?P<codec>[\w.]+)[^,]*, (?P<rate>\d+) Hz, "
    r"(?P<layout>[^,]+), (?P<fmt>[\w]+)"
)


def _load_via_ffmpeg(path: Path, ffmpeg: str) -> AudioData:
    """用 ffmpeg 解成 16bit PCM。位深还原到 16bit 足够支撑时域判据。"""
    code, pcm, log = _run_ffmpeg(
        ffmpeg,
        ["-i", str(path), "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_stdout=True,
    )
    if code != 0 or not pcm:
        tail = " / ".join(line.strip() for line in log.strip().splitlines()[-3:])
        raise AudioQAError(f"ffmpeg 解码失败：{tail or '无输出'}")
    match = _STREAM_RE.search(log)
    if match:
        codec = match.group("codec")
        sample_rate = int(match.group("rate"))
        layout = match.group("layout").strip()
        sample_fmt = match.group("fmt")
        channels = _CH_NAME_TO_COUNT.get(layout.split("(")[0].strip())
        if channels is None:
            digits = re.match(r"(\d+) channels", layout)
            channels = int(digits.group(1)) if digits else 1
    else:  # 拿不到流信息就按最常见的 CD 规格兜底，并在报告里标出来
        codec, sample_rate, channels, sample_fmt = "unknown", 44100, 2, "s16"
    samples, full_scale, lsb = _decode_pcm(pcm, 2)
    frames = len(samples) // channels
    chans = _deinterleave(samples, channels)
    del samples
    return AudioData(
        codec=codec,
        sample_fmt=sample_fmt,
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=None,          # 有损容器谈位深没意义
        frames=frames,
        full_scale=full_scale,
        lsb=lsb,
        chans=chans,
        content_sha256=hashlib.sha256(pcm).hexdigest(),
        decoded_via="ffmpeg",
    )


def load_audio(path: Path, ffmpeg: str | None) -> AudioData:
    if path.suffix.lower() == ".wav":
        try:
            return _load_wav(path)
        except (wave.Error, EOFError) as exc:
            # WAVE_FORMAT_EXTENSIBLE / IEEE float 等标准库不认的子格式走 ffmpeg
            if ffmpeg is None:
                raise AudioQAError(f"标准库无法解析该 WAV（{exc}），且未找到 ffmpeg") from exc
            return _load_via_ffmpeg(path, ffmpeg)
    if ffmpeg is None:
        raise AudioQAError(f"{path.suffix} 需要 ffmpeg 解码，但未找到 ffmpeg")
    return _load_via_ffmpeg(path, ffmpeg)


# --------------------------------------------------------------------------
# 时域测量
# --------------------------------------------------------------------------
def to_dbfs(amplitude: float, full_scale: int) -> float:
    if amplitude <= 0:
        return float("-inf")
    return 20.0 * math.log10(amplitude / full_scale)


def channel_peak(chan: array) -> int:
    if not len(chan):
        return 0
    return max(max(chan), -min(chan))


def dc_offset(chan: array, full_scale: int) -> float:
    if not len(chan):
        return 0.0
    return sum(chan) / len(chan) / full_scale


def clip_runs(
    chan: array, full_scale: int, lsb: int, min_run: int
) -> tuple[list[tuple[int, int]], int, bool]:
    """找出连续满刻度样点段。返回 (段列表, 满刻度样点总数, 是否截断统计)。"""
    limit = full_scale - lsb
    candidates = (limit, -limit, -full_scale)
    total = sum(chan.count(value) for value in candidates)  # C 速度快速排除
    if total == 0:
        return [], 0, False
    positions: list[int] = []
    truncated = False
    for value in candidates:
        cursor = 0
        try:
            while True:
                if len(positions) >= _CLIP_POS_LIMIT:
                    truncated = True
                    break
                cursor = chan.index(value, cursor)
                positions.append(cursor)
                cursor += 1
        except ValueError:
            continue
    positions = sorted(set(positions))
    runs: list[tuple[int, int]] = []
    start = prev = positions[0]
    for pos in positions[1:]:
        if pos == prev + 1:
            prev = pos
            continue
        runs.append((start, prev - start + 1))
        start = prev = pos
    runs.append((start, prev - start + 1))
    return [run for run in runs if run[1] >= min_run], total, truncated


def edge_silence_frames(chan: array, floor_amp: float, window: int) -> tuple[int, int]:
    """返回 (头部静音帧数, 尾部静音帧数)；整段静音时头部 = 全长、尾部 = 0。"""
    total = len(chan)
    if total == 0:
        return 0, 0
    head = total
    for start in range(0, total, window):
        if channel_peak(chan[start : start + window]) > floor_amp:
            head = start
            break
    if head == total:
        return total, 0
    tail = total
    for end in range(total, 0, -window):
        if channel_peak(chan[max(0, end - window) : end]) > floor_amp:
            tail = total - end
            break
    return head, max(0, tail)


def internal_silence_gaps(
    chans: list[array], floor_amp: float, window: int, min_frames: int
) -> list[tuple[int, int]]:
    """找**所有声道同时**低于门限、且两侧都有信号的段内间隙，返回 (起始帧, 长度帧)。

    与 edge_silence_frames 的分工：那个只看头尾，用来判触发延迟与零填充；
    这里只看中间，用来查运行时录音里"明明在出声却中途断掉"（dropout）。
    两侧必须都有信号，所以头尾留白天然不计入，两条判据不会重复计数。
    单声道中途哑掉属于另一类问题，不在这里报。

    长度按整窗累计，量化误差为一个扫描窗口（默认 10 ms）；上限偏短而非偏长，
    宁可少报一些也不虚报断流时长。
    """
    if not chans or min_frames <= 0:
        return []
    total = min(len(chan) for chan in chans)
    if total == 0:
        return []
    gaps: list[tuple[int, int]] = []
    run_start: int | None = None
    saw_signal = False
    for start in range(0, total, window):
        end = min(start + window, total)
        silent = all(channel_peak(chan[start:end]) <= floor_amp for chan in chans)
        if silent:
            if run_start is None:
                run_start = start
            continue
        if run_start is not None:
            if saw_signal and start - run_start >= min_frames:
                gaps.append((run_start, start - run_start))
            run_start = None
        saw_signal = True
    # 结尾仍是静音时不报：那是尾部留白，归 edge_silence_frames / trailing_zero_frames 管
    return gaps


def trailing_zero_frames(chans: list[array]) -> int:
    """返回末尾**所有声道同时为比特级零**的帧数。

    与 edge_silence_frames 的尾部不同：那个用 -60 dBFS 门限，会把自然衰减的
    残响尾巴也算成"静音"。剪掉真零是无损的，剪掉残响是硬切爆音，两者必须分开。
    """
    if not chans or not chans[0]:
        return 0
    total = min(len(chan) for chan in chans)
    zeros = 0
    for offset in range(1, total + 1):
        if any(chan[len(chan) - offset] != 0 for chan in chans):
            break
        zeros = offset
    return zeros


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    values.sort()
    index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[index]


def loop_seam(chan: array, full_scale: int, sample_stride: int = 1) -> tuple[float, float]:
    """
    返回 (首尾跳变, 信号自身斜率参考)，两者都是占满刻度的比例。

    绝对跳变值判不了接缝好坏：48 kHz 下 1 kHz 正弦相邻样点本来就差 6.5% 满刻度，
    高频素材天然跳变大。真正的判据是「接缝跳变相对该素材自身的常规斜率是否异常」，
    所以参考值取内部相邻样点差分的 p99，宽带噪声的大跳变不会被误报。
    """
    total = len(chan)
    if total < 3:
        return 0.0, 0.0
    step = abs(chan[0] - chan[-1]) / full_scale
    stride = max(1, sample_stride)
    diffs = [
        abs(chan[index] - chan[index - 1]) / full_scale
        for index in range(1, total, stride)
    ]
    return step, _percentile(diffs, 0.99)


LOOP_NAME_RE = re.compile(r"(^|[_\-. ])loop([_\-. 0-9]|$)", re.IGNORECASE)


def looks_like_loop(path: Path) -> bool:
    return bool(LOOP_NAME_RE.search(path.stem))


# --------------------------------------------------------------------------
# ffmpeg 响度测量
# --------------------------------------------------------------------------
_LUFS_RE = re.compile(r"^\s*I:\s+(-?[\d.]+|-?inf)\s+LUFS", re.MULTILINE)
_LRA_RE = re.compile(r"^\s*LRA:\s+(-?[\d.]+|-?inf)\s+LU", re.MULTILINE)
_TP_RE = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-?inf)\s+dBFS")


def _parse_float(token: str | None) -> float | None:
    if token is None:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def measure_loudness(path: Path, ffmpeg: str) -> dict[str, float | None]:
    """用 ebur128 取整合响度、响度范围与 True Peak（4 倍过采样真峰值）。"""
    code, _, log = _run_ffmpeg(
        ffmpeg,
        ["-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_stdout=False,
    )
    if code != 0:
        return {"lufs": None, "lra": None, "true_peak_dbtp": None}
    lufs_match = _LUFS_RE.findall(log)
    lra_match = _LRA_RE.findall(log)
    tp_match = _TP_RE.findall(log)
    return {
        # 摘要块在最后，取最后一次匹配
        "lufs": _parse_float(lufs_match[-1]) if lufs_match else None,
        "lra": _parse_float(lra_match[-1]) if lra_match else None,
        "true_peak_dbtp": _parse_float(tp_match[-1]) if tp_match else None,
    }


# --------------------------------------------------------------------------
# 单文件分析
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def analyze_file(
    path: Path, root: Path, cfg: Thresholds, ffmpeg: str | None, do_loudness: bool
) -> FileResult:
    rel = path.relative_to(root).as_posix()
    result = FileResult(path=rel)
    try:
        result.size_bytes = path.stat().st_size
        result.file_sha256 = sha256_file(path)
        audio = load_audio(path, ffmpeg)
    except AudioQAError as exc:
        result.error = str(exc)
        result.issues.append(Issue("decode", "FAIL", f"无法解码：{exc}"))
        return result
    except Exception as exc:  # 未预期错误也要落进报告，不能让一个坏文件中断整轮扫描
        result.error = f"{type(exc).__name__}: {exc}"
        result.issues.append(Issue("decode", "FAIL", f"解码异常：{result.error}"))
        return result

    result.codec = audio.codec
    result.sample_fmt = audio.sample_fmt
    result.sample_rate = audio.sample_rate
    result.channels = audio.channels
    result.bit_depth = audio.bit_depth
    result.frames = audio.frames
    result.duration_s = audio.duration_s
    result.content_sha256 = audio.content_sha256
    result.decoded_via = audio.decoded_via

    if audio.frames == 0:
        result.issues.append(Issue("empty", "FAIL", "文件不含音频采样数据"))
        return result

    full_scale = audio.full_scale
    floor_amp = full_scale * (10.0 ** (cfg.silence_floor_dbfs / 20.0))
    window = max(1, int(audio.sample_rate * cfg.silence_window_ms / 1000.0))

    peaks = [channel_peak(chan) for chan in audio.chans]
    result.peak_dbfs = to_dbfs(max(peaks), full_scale)
    result.dc_offset = max(abs(dc_offset(chan, full_scale)) for chan in audio.chans)

    # 整段静音：素材没导出成功 / 导出了空轨，产线里的高频真实缺陷
    if max(peaks) <= floor_amp:
        result.issues.append(
            Issue(
                "silent_file",
                "FAIL",
                f"整段静音（峰值 {_fmt_db(result.peak_dbfs)}）",
                {"peak_dbfs": _round_or_none(result.peak_dbfs, 2)},
            )
        )
        return result

    # 部分声道哑掉：多声道文件里有声道全静音，其余有声音
    silent_chans = [idx for idx, peak in enumerate(peaks) if peak <= floor_amp]
    if silent_chans and len(silent_chans) < audio.channels:
        result.issues.append(
            Issue(
                "channel_silent",
                "FAIL",
                f"声道 {silent_chans} 全静音，其余声道有信号",
                {"silent_channels": silent_chans},
            )
        )

    # 削波
    clip_total = 0
    clip_instances: list[dict[str, Any]] = []
    clip_truncated = False
    for idx, chan in enumerate(audio.chans):
        runs, total, truncated = clip_runs(chan, full_scale, audio.lsb, cfg.clip_min_run)
        clip_total += total
        clip_truncated = clip_truncated or truncated
        for start, length in runs[:20]:
            clip_instances.append(
                {
                    "channel": idx,
                    "time_s": round(start / audio.sample_rate, 4),
                    "samples": length,
                }
            )
    # 有损格式（ogg/mp3）解码出的浮点样点本来就可能越过满刻度——实测
    # kenney_rpg/metalPot1.ogg 解码峰值 +1.83 dB。本工具转成整数域时会把它们
    # 钳到满刻度，于是「连续满刻度」是**转换过程造成的**，不是素材在制作时被削波。
    # 有损源越过满刻度这件事由 true_peak_overshoot 如实报告，这里降级为 INFO
    # 并注明来源，否则一个正常的 ogg 音效包会集体变成假缺陷。
    lossy_source = result.decoded_via == "ffmpeg"
    if clip_instances:
        clip_instances.sort(key=lambda item: item["time_s"])
        if lossy_source:
            result.issues.append(
                Issue(
                    "clip_after_decode",
                    "INFO",
                    f"解码到整数域后出现 {len(clip_instances)} 段满刻度"
                    f"（样点 {clip_total} 个）；有损源解码峰值可越过满刻度，"
                    f"这是转换钳位而非制作期削波，实际过冲量见 True Peak",
                    {
                        "runs": clip_instances[:20],
                        "full_scale_samples": clip_total,
                        "position_scan_truncated": clip_truncated,
                        "decoded_via": result.decoded_via,
                    },
                )
            )
        else:
            result.issues.append(
                Issue(
                    "clipping",
                    "FAIL",
                    f"检出 {len(clip_instances)} 段削波（满刻度样点 {clip_total} 个），"
                    f"首次出现 {clip_instances[0]['time_s']:.3f}s",
                    {
                        "runs": clip_instances[:20],
                        "full_scale_samples": clip_total,
                        "position_scan_truncated": clip_truncated,
                    },
                )
            )
    elif clip_total > 0 and not lossy_source:
        result.issues.append(
            Issue(
                "clipping",
                "WARN",
                f"存在 {clip_total} 个满刻度样点，但无连续 {cfg.clip_min_run} 点以上的削波段",
                {"full_scale_samples": clip_total},
            )
        )

    # 直流偏移
    if result.dc_offset > cfg.dc_offset_max:
        result.issues.append(
            Issue(
                "dc_offset",
                "FAIL",
                f"直流偏移 {result.dc_offset * 100:.2f}%（上限 {cfg.dc_offset_max * 100:.2f}%）",
                {"dc_offset": round(result.dc_offset, 5)},
            )
        )

    # 首尾静音
    #
    # 头部：空白等于"触发延迟"这个推论只对**靠 Event 即时触发的一次性音效**成立。
    # 几十秒的音乐分轨（stem）不是这种素材，它的头部空白是编曲——实测 Cube 官方
    # Story-Main 主轨头部 0 ms，同一条 66.67 s 里 Cello1 头部 16.1 s；90 bpm 4/4
    # 一小节 2.667 s，16.1 s ≈ 第 6 小节，就是大提琴进入的位置。按时长划界而非
    # 文件名关键词，理由同 lufs_min_duration_s：判据要承认自己的适用边界。
    #
    # 尾部：-60 dBFS 门限找到的"静音"多半是自然衰减的残响，剪掉是硬切爆音而不是
    # 省内存。真正无损可剪的只有比特级零填充，所以尾部判定改看 trailing_zero。
    edges = [edge_silence_frames(chan, floor_amp, window) for chan in audio.chans]
    head_frames = min(edge[0] for edge in edges)
    tail_frames = min(edge[1] for edge in edges)
    result.head_silence_ms = head_frames / audio.sample_rate * 1000.0
    result.tail_silence_ms = tail_frames / audio.sample_rate * 1000.0
    zero_frames = trailing_zero_frames(audio.chans)
    result.tail_zero_ms = zero_frames / audio.sample_rate * 1000.0

    is_oneshot = result.duration_s <= cfg.oneshot_max_duration_s
    if result.head_silence_ms > cfg.head_silence_max_ms:
        if is_oneshot:
            result.issues.append(
                Issue(
                    "head_silence",
                    "FAIL",
                    f"头部空白 {result.head_silence_ms:.0f} ms，"
                    f"一次性音效（{result.duration_s:.2f} s）会表现为触发延迟",
                    {
                        "head_silence_ms": round(result.head_silence_ms, 1),
                        "duration_s": round(result.duration_s, 3),
                    },
                )
            )
        else:
            result.issues.append(
                Issue(
                    "head_offset",
                    "INFO",
                    f"头部空白 {result.head_silence_ms / 1000.0:.2f} s；素材长 "
                    f"{result.duration_s:.1f} s，超过一次性音效上限 "
                    f"{cfg.oneshot_max_duration_s:.0f} s，按音乐分轨处理，"
                    f"头部留白通常是声部进入位置而非触发延迟",
                    {
                        "head_silence_ms": round(result.head_silence_ms, 1),
                        "duration_s": round(result.duration_s, 3),
                    },
                )
            )
    if result.tail_zero_ms > cfg.tail_silence_max_ms:
        result.issues.append(
            Issue(
                "tail_silence",
                "WARN",
                f"尾部比特级零填充 {result.tail_zero_ms:.0f} ms，剪掉无损，白占内存与包体",
                {
                    "tail_zero_ms": round(result.tail_zero_ms, 1),
                    "tail_below_floor_ms": round(result.tail_silence_ms, 1),
                },
            )
        )
    elif result.tail_silence_ms > cfg.tail_silence_max_ms:
        result.issues.append(
            Issue(
                "tail_decay",
                "INFO",
                f"尾部 {result.tail_silence_ms:.0f} ms 低于 {cfg.silence_floor_dbfs:.0f} dBFS，"
                f"但其中只有 {result.tail_zero_ms:.0f} ms 是比特级零；"
                f"其余是自然衰减尾巴，剪掉会硬切出爆音",
                {
                    "tail_below_floor_ms": round(result.tail_silence_ms, 1),
                    "tail_zero_ms": round(result.tail_zero_ms, 1),
                },
            )
        )

    # 段内静音间隙（dropout）：默认关闭，只有显式给了 --dropout-min-ms 才启用。
    #
    # 为什么默认关：素材里的静音往往是设计的一部分（对话句间、环境声留白、
    # 音乐休止），只有**运行时录音**里"全程有声却中途断掉"才构成现象。这类判据
    # 一旦默认开，整个素材库会变成假 WARN 库——判据要承认自己的适用边界。
    #
    # 为什么只报 WARN 不报 FAIL：加载、传送、剧情转场处的静音是设计行为，工具
    # 只凭音频无法区分"设计静音"与"断流"。定性必须与视频时间码核对后由人决定，
    # 口径同 判据设计与误报治理.md：工具给证据，人下结论。
    if cfg.dropout_min_ms > 0:
        min_gap_frames = max(1, int(audio.sample_rate * cfg.dropout_min_ms / 1000.0))
        gaps = internal_silence_gaps(audio.chans, floor_amp, window, min_gap_frames)
        result.dropout_count = len(gaps)
        if gaps:
            result.dropout_max_ms = max(length for _, length in gaps) / audio.sample_rate * 1000.0
            result.dropout_total_ms = sum(length for _, length in gaps) / audio.sample_rate * 1000.0
            gap_instances = [
                {
                    "time_s": round(start / audio.sample_rate, 4),
                    "duration_ms": round(length / audio.sample_rate * 1000.0, 1),
                }
                for start, length in gaps[:20]
            ]
            result.issues.append(
                Issue(
                    "silence_gap",
                    "WARN",
                    f"段内检出 {len(gaps)} 处全程静音间隙，最长 "
                    f"{result.dropout_max_ms:.0f} ms（首次 {gap_instances[0]['time_s']:.3f}s）；"
                    f"需与视频时间码核对，加载与转场处静音属设计行为",
                    {
                        "gaps": gap_instances,
                        "gap_count": len(gaps),
                        "max_gap_ms": round(result.dropout_max_ms, 1),
                        "total_gap_ms": round(result.dropout_total_ms, 1),
                        "threshold_ms": cfg.dropout_min_ms,
                        "window_ms": cfg.silence_window_ms,
                    },
                )
            )

    # loop 接缝：跳变要跟素材自身斜率比，绝对值判不了（高频素材天然跳变大）
    seams = [loop_seam(chan, full_scale) for chan in audio.chans]
    step = max(seam[0] for seam in seams)
    slope_ref = max(seam[1] for seam in seams)
    onset = max(abs(chan[0]) / full_scale for chan in audio.chans)
    is_loop = looks_like_loop(path)
    ratio = step / slope_ref if slope_ref > 1e-9 else 0.0
    seam_bad = step > cfg.loop_step_floor and ratio > cfg.loop_step_ratio_max
    if seam_bad:
        result.issues.append(
            Issue(
                "loop_discontinuity",
                "FAIL" if is_loop else "INFO",
                f"首尾样点跳变 {step * 100:.1f}%，是素材自身斜率（p99 {slope_ref * 100:.1f}%）的 "
                f"{ratio:.1f} 倍（上限 {cfg.loop_step_ratio_max:.1f} 倍）"
                + ("，循环播放会有 click" if is_loop else "，若非循环素材可忽略"),
                {
                    "loop_step": round(step, 4),
                    "slope_p99": round(slope_ref, 4),
                    "ratio": round(ratio, 2),
                    "named_loop": is_loop,
                },
            )
        )
    elif is_loop and onset > cfg.loop_onset_max:
        result.issues.append(
            Issue(
                "loop_onset",
                "WARN",
                f"循环素材起点电平 {onset * 100:.1f}% 不在零点附近",
                {"onset": round(onset, 4)},
            )
        )

    # 假立体声：双声道内容完全一致，等于白花一倍内存和带宽
    if audio.channels == 2 and audio.chans[0] == audio.chans[1]:
        result.issues.append(
            Issue(
                "fake_stereo",
                "WARN",
                "双声道内容完全一致，建议改为单声道以省一半内存",
                {"redundant_bytes": result.size_bytes // 2},
            )
        )

    if do_loudness and ffmpeg:
        loudness = measure_loudness(path, ffmpeg)
        result.lufs = loudness["lufs"]
        result.lra = loudness["lra"]
        result.true_peak_dbtp = loudness["true_peak_dbtp"]
        _check_true_peak(result, cfg)
    return result


def _check_true_peak(result: FileResult, cfg: Thresholds) -> None:
    """
    True Peak 判定分两种模式。

    区分「规范」和「缺陷」很重要：-1 dBTP 是常见的交付规范余量，不是物理错误。
    素材库整体归一化到 0 dBFS 是母带的普遍做法，拿 -1 dBTP 去硬判，
    会把一整个正常素材包判成 90% 以上不达标——那不是查出问题，是判据错了。

    所以：
      显式规范（--true-peak-max）= 按规范验收，超出即 FAIL，附上超出量。
      无规范                      = 只报客观红线：True Peak > 0 dBTP 说明
                                    重建波形已越过满刻度，有损转码（Vorbis/ADPCM）
                                    解码后会真削波。这个结论不依赖任何工程约定。
    """
    peak = result.true_peak_dbtp
    if peak is None:
        return

    if cfg.true_peak_max_dbtp is not None:
        if peak > cfg.true_peak_max_dbtp:
            result.issues.append(
                Issue(
                    "true_peak",
                    "FAIL",
                    f"True Peak {peak:+.2f} dBTP 超过规范上限 "
                    f"{cfg.true_peak_max_dbtp:+.2f} dBTP，"
                    f"超出 {peak - cfg.true_peak_max_dbtp:.2f} dB",
                    {
                        "true_peak_dbtp": round(peak, 2),
                        "limit_dbtp": cfg.true_peak_max_dbtp,
                        "over_by_db": round(peak - cfg.true_peak_max_dbtp, 2),
                    },
                )
            )
        return

    if peak > cfg.true_peak_overshoot_dbtp:
        result.issues.append(
            Issue(
                "true_peak_overshoot",
                "FAIL",
                f"True Peak {peak:+.2f} dBTP 越过满刻度，"
                f"有损转码后解码会削波失真",
                {
                    "true_peak_dbtp": round(peak, 2),
                    "overshoot_db": round(peak - cfg.true_peak_overshoot_dbtp, 2),
                },
            )
        )
    elif peak > cfg.true_peak_headroom_dbtp:
        result.issues.append(
            Issue(
                "true_peak_headroom",
                "INFO",
                f"True Peak {peak:+.2f} dBTP，余量不足 "
                f"{abs(cfg.true_peak_headroom_dbtp):.0f} dB，"
                f"若工程要求 {cfg.true_peak_headroom_dbtp:+.0f} dBTP 需重新归一化",
                {"true_peak_dbtp": round(peak, 2)},
            )
        )


def _fmt_db(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "-inf dBFS"
    return f"{value:+.2f} dBFS"


# --------------------------------------------------------------------------
# 资产库级分析
# --------------------------------------------------------------------------
def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values: Sequence[float], center: float) -> float:
    """中位绝对偏差。比标准差抗离群，几个异常文件不会把容差撑大。"""
    if not values:
        return 0.0
    return _median([abs(value - center) for value in values])


# 目录约定优先于文件名关键词：真实工程里素材归哪一类由它放在哪个目录决定，
# 所以先按目录名匹配，只有目录看不出类别时才退回文件名关键词。
_CATEGORY_DIR_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("vo", ("/vo/", "/voice/", "/voices/", "/dialogue/", "/dialog/", "/lines/")),
    ("bgm", ("/bgm/", "/music/", "/ost/", "/tracks/")),
    ("ambience", ("/amb/", "/ambience/", "/ambient/", "/atmo/", "/atmosphere/")),
    ("ui", ("/ui/", "/gui/", "/menu/", "/interface/", "/hud/")),
    ("footstep", ("/footstep/", "/footsteps/", "/steps/", "/foley/")),
    ("creature", ("/npc/", "/npcs/", "/monster/", "/monsters/", "/enemy/", "/enemies/", "/creature/", "/creatures/", "/mob/")),
    ("combat", ("/battle/", "/combat/", "/fight/", "/attack/")),
    ("weapon", ("/weapon/", "/weapons/", "/gun/", "/guns/")),
    ("item", ("/inventory/", "/item/", "/items/", "/pickup/", "/pickups/", "/loot/")),
    ("world", ("/world/", "/env/", "/environment/", "/props/", "/prop/")),
    ("misc", ("/misc/", "/other/", "/uncategorized/")),
]

# 文件名关键词兜底，同样按「越具体越靠前」排列
_CATEGORY_NAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("vo", ("_vo_", "voiceover", "vox_")),
    ("bgm", ("bgm", "music_", "theme", "_ost")),
    ("ambience", ("ambience", "ambient", "_amb_", "atmo")),
    ("ui", ("ui_", "_ui_", "menu_", "interface", "button", "click", "hover")),
    ("footstep", ("footstep", "step_", "_step_")),
    ("weapon", ("weapon", "gun_", "rifle", "pistol", "shotgun", "reload", "sword", "melee", "swing")),
    ("alarm", ("alarm", "siren", "warning")),
    ("notification", ("notification", "notify", "toast", "chime", "ding", "alert")),
]


def infer_category(path: str) -> str:
    """
    从相对路径推断素材类别。

    响度基准必须分类别定：BGM、SFX、VO、UI 在真实工程里各有各的目标电平，
    拿全库中位数当唯一基准会把「闹钟本来就比提示音响」判成缺陷。
    工程里靠目录约定分类，这里先还原目录约定，再用文件名关键词兜底。
    """
    key = "/" + path.replace("\\", "/").lower()
    directory = key.rsplit("/", 1)[0] + "/"
    for category, needles in _CATEGORY_DIR_RULES:
        if any(needle in directory for needle in needles):
            return category
    for category, needles in _CATEGORY_NAME_RULES:
        if any(needle in key for needle in needles):
            return category
    return "uncategorized"


# 音乐分轨的文件名里塞了 bpm / 拍号 / 小节数 / 弱起标注（如 _138bpm4-4_L17M-P1B：
# 138 bpm、4/4 拍、17 小节、弱起 1 拍），音源采样按音名命名（Suling_E#5）。
# 这些字段变了不代表素材身份变了，比较重复素材的名字关系时必须先剥掉。
_MUSIC_ANNOTATION_RE = re.compile(
    r"\d+(?:\.\d+)?bpm\d+-\d+|L\d+M(?:-P\d+[A-Za-z]?)?|P\d+[A-Za-z]?",
    re.IGNORECASE,
)
_NOTE_NAME_RE = re.compile(r"([A-Ga-g])([#b]{0,2})(-?\d)")
_NOTE_SEMITONES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def _note_semitone(token: str) -> int | None:
    """把音名解析成绝对半音号。E#5 与 F5 都是 65，这就是等音异名。"""
    match = _NOTE_NAME_RE.fullmatch(token)
    if match is None:
        return None
    letter, accidentals, octave = match.groups()
    value = _NOTE_SEMITONES[letter.lower()]
    for char in accidentals:
        value += 1 if char == "#" else -1
    return int(octave) * 12 + value


def canonical_asset_name(path: str) -> str:
    """去掉「不改变素材身份」的命名成分，得到可比较的规范名。"""
    stem = Path(path).stem
    parts: list[str] = []
    for token in stem.split("_"):
        if _MUSIC_ANNOTATION_RE.fullmatch(token):
            continue
        semitone = _note_semitone(token)
        parts.append(f"note{semitone}" if semitone is not None else token.lower())
    return "_".join(parts) if parts else stem.lower()


def classify_duplicate_group(paths: Sequence[str]) -> tuple[str, str, str]:
    """给一组内容相同的文件定性，返回 (kind, severity, 原因)。

    三类的修法完全不同，混成一个 WARN 等于报了个没法执行的缺陷：
    同名不同目录删一份即可；标注等价的删了会破坏采样器索引；
    命名表示不同声部却内容相同的，要重新导出而不是删。
    """
    names = {Path(p).name for p in paths}
    dirs = {Path(p).parent.as_posix() for p in paths}
    canon = {canonical_asset_name(p) for p in paths}
    if len(names) == 1 and len(dirs) > 1:
        return ("redundant_copy", "WARN", f"同一文件同时存在于 {len(dirs)} 个目录，可删冗余副本并改引用")
    if len(canon) == 1:
        return (
            "equivalent_naming",
            "INFO",
            "文件名只差等音异名或 bpm/拍号/小节标注，内容相同属预期；"
            "若标注互相矛盾则应统一命名而非删文件",
        )
    return (
        "duplicate_asset",
        "WARN",
        "文件名表示不同素材（" + "、".join(sorted(canon)) + "），内容却完全相同，需确认导出是否漏了声部",
    )


def analyze_library(results: list[FileResult], cfg: Thresholds) -> dict[str, Any]:
    """跨文件一致性检查：格式分布、重复素材、响度一致性。"""
    usable = [r for r in results if r.error is None and r.frames > 0]

    # 格式分布：以众数为基准，偏离的挑出来
    fmt_counter: Counter[tuple[int, int | None, int]] = Counter()
    for res in usable:
        fmt_counter[(res.sample_rate, res.bit_depth, res.channels)] += 1
    formats = [
        {
            "sample_rate": key[0],
            "bit_depth": key[1],
            "channels": key[2],
            "count": count,
        }
        for key, count in fmt_counter.most_common()
    ]
    outlier_formats: list[dict[str, Any]] = []
    if len(fmt_counter) > 1:
        dominant_rate = Counter(res.sample_rate for res in usable).most_common(1)[0][0]
        for res in usable:
            if res.sample_rate != dominant_rate:
                severity = "WARN" if res.sample_rate > dominant_rate else "INFO"
                message = (
                    f"采样率 {res.sample_rate} Hz 与资产库主流 {dominant_rate} Hz 不一致"
                    + ("，高于主流规格属于无谓开销" if res.sample_rate > dominant_rate else "")
                )
                res.issues.append(
                    Issue(
                        "format_consistency",
                        severity,
                        message,
                        {"sample_rate": res.sample_rate, "dominant_sample_rate": dominant_rate},
                    )
                )
                outlier_formats.append(
                    {"path": res.path, "sample_rate": res.sample_rate, "severity": severity}
                )

    # 重复素材
    #
    # 「内容相同」是事实，「浪费空间」是推论，中间隔着命名约定这一层。实测 Cube
    # 官方工程 9 组字节相同的文件，掰开看是三种完全不同的问题：
    #   5 组同名文件散落在两个目录（SFX/ 与 SFX/Story/）——真冗余，删一份改引用。
    #   2 组是 Suling_E#5 / Suling_F5 这类等音异名，以及 138bpm3-4 / 138bpm4-4
    #     这类拍号标注差异——采样器按音名与标注索引，删掉就取不到音了。
    #   剩下的 Gtr2 / Gtr3 命名表示不同声部而内容相同，那是导出漏了声部，
    #     修法是重新导出，删文件反而丢信息。
    # 三类塞进同一个 WARN，报出去的是个没法执行的缺陷。所以先定性再定级，
    # 「可回收空间」也只统计真冗余那一类，避免把不该删的算进收益。
    by_file_hash: dict[str, list[str]] = defaultdict(list)
    by_content_hash: dict[str, list[str]] = defaultdict(list)
    for res in usable:
        by_file_hash[res.file_sha256].append(res.path)
        if res.content_sha256:
            by_content_hash[res.content_sha256].append(res.path)
    exact_dupes = [paths for paths in by_file_hash.values() if len(paths) > 1]
    exact_set = {tuple(sorted(paths)) for paths in exact_dupes}
    content_dupes = [
        paths
        for paths in by_content_hash.values()
        if len(paths) > 1 and tuple(sorted(paths)) not in exact_set
    ]
    lookup = {res.path: res for res in usable}
    wasted = 0
    dupe_groups: list[dict[str, Any]] = []
    for group in exact_dupes + content_dupes:
        paths = sorted(group)
        keeper = paths[0]
        identical_bytes = tuple(paths) in exact_set
        kind, severity, reason = classify_duplicate_group(paths)
        recoverable = 0
        if kind == "redundant_copy":
            recoverable = sum(lookup[path].size_bytes for path in paths[1:])
            wasted += recoverable
        for path in paths[1:]:
            same = "字节完全相同" if identical_bytes else "音频内容相同（容器/元数据不同）"
            lookup[path].issues.append(
                Issue(
                    kind,
                    severity,
                    f"与 {keeper} {same}：{reason}",
                    {
                        "duplicate_of": keeper,
                        "kind": kind,
                        "identical_bytes": identical_bytes,
                        "group": paths,
                    },
                )
            )
        dupe_groups.append(
            {
                "paths": paths,
                "kind": kind,
                "severity": severity,
                "reason": reason,
                "identical_bytes": identical_bytes,
                "recoverable_bytes": recoverable,
            }
        )
    dupe_groups.sort(key=lambda g: (-SEVERITY_ORDER[g["severity"]], g["paths"][0]))

    # 响度一致性
    #
    # 判据分两种模式，区别很重要：
    #   显式目标（--lufs-target）= 按规范验收，偏离就是缺陷，硬容差。
    #   无目标                   = 只能查「同类素材之间是否齐」，不能查绝对电平。
    #
    # 无目标时有两个坑，都会造成大面积误报：
    #   1. 跨类别比。闹钟本来就比 UI 提示音响，BGM 本来就比 SFX 低，
    #      拿全库一个中位数当基准，等于要求所有素材一样响。所以按类别分组。
    #   2. 拿固定 ±2 LU 卡一个本身就离散的素材库。素材库齐不齐是它自己的属性，
    #      容差应当由组内离散度（MAD）决定，只报真正跳出本组分布的那几个。
    #   3. 把 ebur128 的门限下限当成真实响度。整合响度按 400 ms 门控块统计，
    #      短于一个块的素材达不到测量条件，ffmpeg 直接吐 -70.0 LUFS。
    #      那是「测不出」，不是「太轻」——实测本机素材库分界线正好落在 400 ms
    #      （不可测组最长 0.396 s，可测组最短 0.404 s）。短音效要排除在
    #      一致性比较之外，否则一次性音效会集体变成假缺陷。
    gated_out = [
        res
        for res in usable
        if res.lufs is not None and res.lufs <= cfg.lufs_gate_floor
    ]
    lufs_measured = [
        res
        for res in usable
        if res.lufs is not None and res.lufs > cfg.lufs_gate_floor
    ]
    loudness: dict[str, Any] = {
        "measured": len(lufs_measured),
        "gate_floor_lufs": cfg.lufs_gate_floor,
        "excluded_below_gate": len(gated_out),
    }
    if gated_out:
        loudness["excluded_files"] = sorted(res.path for res in gated_out)
    outliers: list[dict[str, Any]] = []

    if lufs_measured:
        explicit = cfg.lufs_target is not None
        loudness["basis_source"] = "explicit" if explicit else "per_category_median"

        if explicit:
            groups: dict[str, list[FileResult]] = {"__all__": lufs_measured}
        else:
            groups = defaultdict(list)
            for res in lufs_measured:
                groups[infer_category(res.path)].append(res)

        group_info: list[dict[str, Any]] = []
        for name, members in sorted(groups.items()):
            values = [res.lufs for res in members]

            if explicit:
                basis = float(cfg.lufs_target)  # type: ignore[arg-type]
                tolerance = cfg.lufs_tolerance
            else:
                # 组太小就不推断基准：三五个文件的中位数代表不了任何标准，
                # 硬判只会把正常素材报成缺陷。这类组如实记为"未评估"。
                if len(members) < cfg.lufs_min_group:
                    group_info.append(
                        {
                            "category": name,
                            "count": len(members),
                            "evaluated": False,
                            "reason": f"样本不足 {cfg.lufs_min_group} 个，不推断基准",
                        }
                    )
                    continue
                basis = _median(values)
                spread = _mad(values, basis)
                tolerance = max(cfg.lufs_tolerance, cfg.lufs_spread_k * spread)

            hits = 0
            for res in members:
                delta = res.lufs - basis
                if abs(delta) <= tolerance:
                    continue
                hits += 1
                scope = "规范目标" if explicit else f"同类（{name}）基准"
                res.issues.append(
                    Issue(
                        "loudness_consistency",
                        "WARN",
                        f"整合响度 {res.lufs:.1f} LUFS 偏离{scope} {basis:.1f} LUFS "
                        f"{delta:+.1f} LU（容差 ±{tolerance:.1f} LU）",
                        {
                            "lufs": round(res.lufs, 2),
                            "delta_lu": round(delta, 2),
                            "category": name,
                            "basis_lufs": round(basis, 2),
                            "tolerance_lu": round(tolerance, 2),
                        },
                    )
                )
                outliers.append(
                    {
                        "path": res.path,
                        "category": name,
                        "lufs": round(res.lufs, 2),
                        "delta_lu": round(delta, 2),
                    }
                )

            entry: dict[str, Any] = {
                "category": name,
                "count": len(members),
                "evaluated": True,
                "basis_lufs": round(basis, 2),
                "tolerance_lu": round(tolerance, 2),
                "outliers": hits,
            }
            if not explicit:
                entry["mad_lu"] = round(_mad(values, basis), 2)
            group_info.append(entry)

        loudness["groups"] = group_info
        loudness["outliers"] = outliers

    return {
        "formats": formats,
        "format_outliers": outlier_formats,
        "duplicate_groups": dupe_groups,
        "duplicate_wasted_bytes": wasted,
        "loudness": loudness,
    }


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def collect_files(root: Path, exts: Iterable[str]) -> list[Path]:
    wanted = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in exts}
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in wanted
    )


def scan(
    root: Path,
    cfg: Thresholds,
    ffmpeg: str | None,
    jobs: int,
    do_loudness: bool,
    exts: Iterable[str],
    progress: bool = True,
) -> dict[str, Any]:
    files = collect_files(root, exts)
    if not files:
        raise AudioQAError(f"{root} 下没有找到可检查的音频文件")
    started = time.time()
    results: list[FileResult] = []
    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = [
            pool.submit(analyze_file, path, root, cfg, ffmpeg, do_loudness) for path in files
        ]
        for done, future in enumerate(futures.as_completed(pending), 1):
            results.append(future.result())
            if progress:
                print(f"\r  检查中 {done}/{len(files)}", end="", file=sys.stderr, flush=True)
    if progress:
        print("", file=sys.stderr)
    results.sort(key=lambda res: res.path)
    library = analyze_library(results, cfg)
    counts = Counter(res.status for res in results)
    return {
        "tool": "audio_qa",
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(root),
        "elapsed_s": round(time.time() - started, 2),
        "ffmpeg": ffmpeg,
        "loudness_enabled": bool(do_loudness and ffmpeg),
        "thresholds": cfg.as_dict(),
        "summary": {
            "files": len(results),
            "pass": counts.get("PASS", 0),
            "info": counts.get("INFO", 0),
            "warn": counts.get("WARN", 0),
            "fail": counts.get("FAIL", 0),
        },
        "library": library,
        "files": [res.as_dict() for res in results],
    }


# --------------------------------------------------------------------------
# Markdown 渲染
# --------------------------------------------------------------------------
def _fmt_cell(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def render_scan_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines: list[str] = [
        "# 音频资产质量检查报告",
        "",
        f"- 扫描目录：`{report['root']}`",
        f"- 检查文件：{summary['files']} 个"
        f"（PASS {summary['pass']} / INFO {summary['info']} /"
        f" WARN {summary['warn']} / FAIL {summary['fail']}）",
        f"- 生成时间：{report['generated_at']}　耗时 {report['elapsed_s']} s",
        f"- 响度测量（LUFS / True Peak）：{'开启' if report['loudness_enabled'] else '未开启'}",
        f"- 工具：audio_qa {report['version']}",
        "",
        "## 结论",
        "",
    ]
    if summary["fail"]:
        lines.append(f"**{summary['fail']} 个文件不达标**，需修复后重新提交；"
                     f"另有 {summary['warn']} 个文件存在优化空间。")
    elif summary["warn"]:
        lines.append(f"无阻断性问题，{summary['warn']} 个文件存在优化空间。")
    else:
        lines.append("全部文件通过检查。")
    lines.append("")

    for severity, title in (("FAIL", "FAIL 明细"), ("WARN", "WARN 明细"), ("INFO", "INFO 明细")):
        rows = [item for item in report["files"] if item["status"] == severity]
        if not rows:
            continue
        lines += [f"## {title}", "", "| 文件 | 检查项 | 说明 |", "| --- | --- | --- |"]
        for item in rows:
            hits = [i for i in item["issues"] if i["severity"] == severity]
            for idx, issue in enumerate(hits):
                name = f"`{item['path']}`" if idx == 0 else ""
                lines.append(f"| {name} | {issue['check']} | {issue['message']} |")
        lines.append("")

    lines += [
        "## 全部文件测量值",
        "",
        "| 文件 | 状态 | 采样率 | 位深 | 声道 | 时长(s) | 峰值(dBFS) | LUFS | True Peak | 直流偏移 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["files"]:
        lines.append(
            "| `{path}` | {status} | {rate} | {bits} | {ch} | {dur} | {peak} | {lufs} |"
            " {tp} | {dc} |".format(
                path=item["path"],
                status=item["status"],
                rate=_fmt_cell(item["sample_rate"]),
                bits=_fmt_cell(item["bit_depth"]),
                ch=_fmt_cell(item["channels"]),
                dur=_fmt_cell(item["duration_s"]),
                peak=_fmt_cell(item["peak_dbfs"]),
                lufs=_fmt_cell(item["lufs"]),
                tp=_fmt_cell(item["true_peak_dbtp"]),
                dc=("—" if item["dc_offset"] is None else f"{item['dc_offset'] * 100:.2f}%"),
            )
        )
    lines.append("")

    dropout_on = bool(report["thresholds"].get("dropout_min_ms"))
    if dropout_on:
        hit = [item for item in report["files"] if item["dropout_count"]]
        lines += [
            "## 段内静音间隙（dropout）",
            "",
            f"判据：所有声道同时低于 {report['thresholds']['silence_floor_dbfs']:.0f} dBFS、"
            f"持续 ≥ {report['thresholds']['dropout_min_ms']:.0f} ms，且间隙两侧都有信号。"
            "长度按整窗累计，误差为一个扫描窗口；加载与转场处的静音属设计行为，"
            "需与视频时间码核对后再定性。",
            "",
        ]
        if hit:
            lines += [
                "| 文件 | 间隙数 | 最长(ms) | 累计(ms) | 首次位置(s) |",
                "| --- | --- | --- | --- | --- |",
            ]
            for item in hit:
                position = next(
                    (
                        issue["detail"]["gaps"][0]["time_s"]
                        for issue in item["issues"]
                        if issue["check"] == "silence_gap" and issue["detail"].get("gaps")
                    ),
                    None,
                )
                lines.append(
                    f"| `{item['path']}` | {item['dropout_count']} |"
                    f" {_fmt_cell(item['dropout_max_ms'])} |"
                    f" {_fmt_cell(item['dropout_total_ms'])} | {_fmt_cell(position)} |"
                )
        else:
            lines.append("本轮没有文件达到该下限。")
        lines.append("")

    library = report["library"]
    lines += ["## 资产库一致性", "", "### 格式分布", "", "| 采样率 | 位深 | 声道 | 文件数 |",
              "| --- | --- | --- | --- |"]
    for fmt in library["formats"]:
        lines.append(
            f"| {fmt['sample_rate']} Hz | {_fmt_cell(fmt['bit_depth'])} |"
            f" {fmt['channels']} | {fmt['count']} |"
        )
    lines.append("")

    if library["duplicate_groups"]:
        lines += [
            "### 内容相同的素材",
            "",
            "分三类，修法不同：`redundant_copy` 删冗余副本并改引用；"
            "`equivalent_naming` 命名等价属预期，不要删；"
            "`duplicate_asset` 命名表示不同素材却内容相同，需复查导出。",
            "",
        ]
        for group in library["duplicate_groups"]:
            same = "字节相同" if group["identical_bytes"] else "内容相同（容器/元数据不同）"
            lines.append(
                f"- **{group['severity']} / {group['kind']}**（{same}）："
                f"{'、'.join(f'`{p}`' for p in group['paths'])}"
            )
            lines.append(f"  - {group['reason']}")
        waste = library["duplicate_wasted_bytes"]
        if waste:
            lines.append(f"- 可回收空间约 {waste / 1024:.1f} KB")
        lines.append("")

    loud = library["loudness"]
    if loud.get("measured"):
        explicit = loud.get("basis_source") == "explicit"
        lines += ["### 响度一致性", ""]
        if explicit:
            lines.append(
                f"- 模式：按指定目标 {report['thresholds']['lufs_target']} LUFS 验收，"
                f"硬容差 ±{report['thresholds']['lufs_tolerance']} LU"
            )
        else:
            lines += [
                "- 模式：未指定响度目标，只检查**同类素材之间是否齐**，不判绝对电平。",
                f"  基准取各类别中位数，容差取 max(±{report['thresholds']['lufs_tolerance']} LU, "
                f"{report['thresholds']['lufs_spread_k']} × 组内 MAD)——"
                "素材库本身的离散度决定判据，避免把「闹钟本来就比 UI 音响」报成缺陷。",
            ]
        lines += [
            f"- 已测量 {loud['measured']} 个文件，超出容差 {len(loud.get('outliers', []))} 个",
        ]
        excluded = loud.get("excluded_below_gate") or 0
        if excluded:
            lines.append(
                f"- 另有 {excluded} 个文件低于 {loud['gate_floor_lufs']:+g} LUFS 门控下限，"
                "不参与比较：整合响度按 EBU R128 的 400 ms 门控块统计，"
                "短于一个块的一次性音效达不到测量条件，ffmpeg 会直接返回 -70 LUFS。"
                "那是「测不出」而非「太轻」，硬判会把短音效集体误报成缺陷。"
            )
        lines.append("")
        groups = loud.get("groups") or []
        if groups:
            lines += [
                "| 类别 | 文件数 | 基准 LUFS | 容差 | 组内 MAD | 离群 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for grp in groups:
                if not grp.get("evaluated"):
                    lines.append(
                        f"| {grp['category']} | {grp['count']} | — | — | — |"
                        f" 未评估（{grp.get('reason', '')}） |"
                    )
                    continue
                mad = grp.get("mad_lu")
                lines.append(
                    f"| {grp['category']} | {grp['count']} | {grp['basis_lufs']} |"
                    f" ±{grp['tolerance_lu']} LU | {'—' if mad is None else f'{mad} LU'} |"
                    f" {grp['outliers']} |"
                )
            lines.append("")
        if loud.get("outliers"):
            lines += ["| 文件 | 类别 | LUFS | 偏差 |", "| --- | --- | --- | --- |"]
            for item in loud["outliers"]:
                lines.append(
                    f"| `{item['path']}` | {item['category']} |"
                    f" {item['lufs']} | {item['delta_lu']:+g} LU |"
                )
            lines.append("")

    lines += [
        "## 判定阈值",
        "",
        "```json",
        json.dumps(report["thresholds"], ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 回归比对
# --------------------------------------------------------------------------
COMPARE_TOLERANCE = {"lufs": 0.5, "true_peak_dbtp": 0.5, "peak_dbfs": 0.5, "duration_s": 0.01}


def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """比对两次扫描：资产增删、格式变化、音频表现差异。"""
    base_index = {item["path"]: item for item in baseline["files"]}
    curr_index = {item["path"]: item for item in current["files"]}
    added = sorted(set(curr_index) - set(base_index))
    removed = sorted(set(base_index) - set(curr_index))
    changed: list[dict[str, Any]] = []

    for path in sorted(set(base_index) & set(curr_index)):
        before, after = base_index[path], curr_index[path]
        if before["content_sha256"] and before["content_sha256"] == after["content_sha256"]:
            continue  # 音频内容位级相同，无需比对测量值
        entry: dict[str, Any] = {"path": path, "severity": "INFO", "deltas": [], "notes": []}
        for key, label in (
            ("sample_rate", "采样率"),
            ("bit_depth", "位深"),
            ("channels", "声道数"),
        ):
            if before[key] != after[key]:
                entry["notes"].append(f"{label} {before[key]} → {after[key]}")
                entry["severity"] = "FAIL"
        for key, label in (
            ("duration_s", "时长(s)"),
            ("peak_dbfs", "峰值(dBFS)"),
            ("lufs", "LUFS"),
            ("true_peak_dbtp", "True Peak(dBTP)"),
        ):
            old, new = before.get(key), after.get(key)
            if old is None or new is None:
                continue
            delta = new - old
            if abs(delta) > COMPARE_TOLERANCE[key]:
                entry["deltas"].append(
                    {"metric": label, "before": old, "after": new, "delta": round(delta, 3)}
                )
                entry["severity"] = "FAIL"
        new_fails = {i["check"] for i in after["issues"] if i["severity"] == "FAIL"} - {
            i["check"] for i in before["issues"] if i["severity"] == "FAIL"
        }
        if new_fails:
            entry["notes"].append("新增 FAIL 项：" + "、".join(sorted(new_fails)))
            entry["severity"] = "FAIL"
        if not entry["deltas"] and not entry["notes"]:
            entry["notes"].append("内容哈希变化，但各项测量值均在容差内")
        changed.append(entry)

    regressions = [entry for entry in changed if entry["severity"] == "FAIL"]
    return {
        "tool": "audio_qa compare",
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_generated_at": baseline.get("generated_at"),
        "current_generated_at": current.get("generated_at"),
        "tolerance": COMPARE_TOLERANCE,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "regressions": len(regressions),
        },
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def render_compare_markdown(diff: dict[str, Any]) -> str:
    summary = diff["summary"]
    lines = [
        "# 音频资产回归比对报告",
        "",
        f"- 基线：{diff['baseline_generated_at']}",
        f"- 当前：{diff['current_generated_at']}",
        f"- 新增 {summary['added']} / 删除 {summary['removed']} /"
        f" 内容变更 {summary['changed']}，其中判定回归 {summary['regressions']} 个",
        "",
        "## 结论",
        "",
        f"**存在 {summary['regressions']} 个音频表现回归**，需确认是否预期改动。"
        if summary["regressions"]
        else "未发现超出容差的音频表现变化。",
        "",
    ]
    if diff["added"]:
        lines += ["## 新增资产", ""] + [f"- `{p}`" for p in diff["added"]] + [""]
    if diff["removed"]:
        lines += ["## 删除资产", ""] + [f"- `{p}`" for p in diff["removed"]] + [""]
    if diff["changed"]:
        lines += ["## 内容变更明细", ""]
        for entry in diff["changed"]:
            lines.append(f"### `{entry['path']}` — {entry['severity']}")
            lines.append("")
            for note in entry["notes"]:
                lines.append(f"- {note}")
            if entry["deltas"]:
                lines += ["", "| 指标 | 基线 | 当前 | 差值 |", "| --- | --- | --- | --- |"]
                for delta in entry["deltas"]:
                    lines.append(
                        f"| {delta['metric']} | {delta['before']} |"
                        f" {delta['after']} | {delta['delta']:+g} |"
                    )
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# LLM 缺陷报告草稿（可选）
# --------------------------------------------------------------------------
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

REPORT_SYSTEM_PROMPT = (
    "你是资深游戏音频测试工程师。基于给定的自动化检查结果，为每个问题写一条可直接提单的缺陷报告，"
    "字段包括：标题、严重度（Blocker/Critical/Major/Minor）、涉及资产、复现步骤、期望结果、"
    "实际结果、对玩家体验的影响、建议修复方向。严重度判定依据：影响可听性与可玩性的排前，"
    "仅影响内存与包体的排后。用简体中文，Markdown 输出，不要复述输入 JSON。"
)


def draft_bug_reports(report: dict[str, Any], api_key: str, model: str, timeout: int) -> str:
    findings = [
        {
            "path": item["path"],
            "status": item["status"],
            "sample_rate": item["sample_rate"],
            "channels": item["channels"],
            "duration_s": item["duration_s"],
            "peak_dbfs": item["peak_dbfs"],
            "lufs": item["lufs"],
            "true_peak_dbtp": item["true_peak_dbtp"],
            "issues": [
                {"check": i["check"], "severity": i["severity"], "message": i["message"]}
                for i in item["issues"]
                if i["severity"] in ("FAIL", "WARN")
            ],
        }
        for item in report["files"]
        if item["status"] in ("FAIL", "WARN")
    ]
    if not findings:
        return "# 缺陷报告\n\n本轮检查未发现 FAIL / WARN 级问题，无需提单。\n"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REPORT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"检查目录：{report['root']}\n"
                    f"判定阈值：{json.dumps(report['thresholds'], ensure_ascii=False)}\n"
                    f"问题清单：\n{json.dumps(findings, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
        "temperature": 0.2,
        "stream": False,
    }
    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise AudioQAError(f"LLM 接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AudioQAError(f"LLM 接口请求失败：{exc}") from exc
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AudioQAError(f"LLM 响应结构异常：{json.dumps(body, ensure_ascii=False)[:400]}") from exc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioQAError(f"无法读取 {path}：{exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_qa",
        description="游戏音频资产质量检查 / 自动化回归工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"audio_qa {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="扫描目录并产出质量报告")
    scan_parser.add_argument("directory", type=Path, help="音频资产根目录")
    scan_parser.add_argument("-o", "--json", type=Path, help="JSON 报告输出路径（可作回归基线）")
    scan_parser.add_argument("--md", type=Path, help="Markdown 报告输出路径")
    scan_parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    scan_parser.add_argument("--no-loudness", action="store_true", help="跳过 LUFS / True Peak 测量")
    scan_parser.add_argument(
        "--ext", default=",".join(DEFAULT_EXTS), help="参与检查的扩展名，逗号分隔"
    )
    scan_parser.add_argument(
        "-j", "--jobs", type=int, default=min(8, (os.cpu_count() or 4)), help="并发数"
    )
    scan_parser.add_argument("--quiet", action="store_true", help="不打印进度")
    scan_parser.add_argument("--clip-min-run", type=int, default=Thresholds.clip_min_run)
    scan_parser.add_argument("--dc-offset-max", type=float, default=Thresholds.dc_offset_max)
    scan_parser.add_argument(
        "--silence-floor", type=float, default=Thresholds.silence_floor_dbfs, help="静音门限 dBFS"
    )
    scan_parser.add_argument(
        "--head-silence-max", type=float, default=Thresholds.head_silence_max_ms, help="毫秒"
    )
    scan_parser.add_argument(
        "--tail-silence-max", type=float, default=Thresholds.tail_silence_max_ms, help="毫秒"
    )
    scan_parser.add_argument(
        "--dropout-min-ms",
        type=float,
        default=Thresholds.dropout_min_ms,
        help="段内静音间隙（dropout）下限毫秒数；0 = 关闭，查运行时录制断流时使用",
    )
    scan_parser.add_argument(
        "--loop-ratio-max",
        type=float,
        default=Thresholds.loop_step_ratio_max,
        help="接缝跳变相对素材自身 p99 斜率的倍数上限",
    )
    scan_parser.add_argument(
        "--loop-step-floor",
        type=float,
        default=Thresholds.loop_step_floor,
        help="跳变低于此满刻度比例一律不报",
    )
    scan_parser.add_argument(
        "--true-peak-max",
        type=float,
        default=Thresholds.true_peak_max_dbtp,
        help="工程规范的 True Peak 上限（dBTP）。给了才按规范判 FAIL；"
        "省略则只报越过 0 dBTP 的客观过冲",
    )
    scan_parser.add_argument(
        "--true-peak-overshoot",
        type=float,
        default=Thresholds.true_peak_overshoot_dbtp,
        help="无规范时判过冲的红线，默认 0 dBTP",
    )
    scan_parser.add_argument(
        "--true-peak-headroom",
        type=float,
        default=Thresholds.true_peak_headroom_dbtp,
        help="余量参考线，仅出 INFO 与库级统计，不判 FAIL",
    )
    scan_parser.add_argument(
        "--lufs-target", type=float, default=None, help="响度目标 LUFS，省略则用资产库中位数"
    )
    scan_parser.add_argument("--lufs-tolerance", type=float, default=Thresholds.lufs_tolerance)

    compare_parser = subparsers.add_parser("compare", help="比对两份 scan JSON，输出回归报告")
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("current", type=Path)
    compare_parser.add_argument("-o", "--json", type=Path)
    compare_parser.add_argument("--md", type=Path)

    report_parser = subparsers.add_parser("report", help="用 LLM 把检查结果写成缺陷报告草稿")
    report_parser.add_argument("scan_json", type=Path)
    report_parser.add_argument("--md", type=Path, help="输出路径，省略则打印到标准输出")
    report_parser.add_argument("--model", default="deepseek-chat")
    report_parser.add_argument("--timeout", type=int, default=180)
    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    root: Path = args.directory
    if not root.is_dir():
        raise AudioQAError(f"目录不存在：{root}")
    cfg = Thresholds(
        clip_min_run=args.clip_min_run,
        dc_offset_max=args.dc_offset_max,
        silence_floor_dbfs=args.silence_floor,
        head_silence_max_ms=args.head_silence_max,
        tail_silence_max_ms=args.tail_silence_max,
        dropout_min_ms=args.dropout_min_ms,
        loop_step_ratio_max=args.loop_ratio_max,
        loop_step_floor=args.loop_step_floor,
        true_peak_max_dbtp=args.true_peak_max,
        true_peak_overshoot_dbtp=args.true_peak_overshoot,
        true_peak_headroom_dbtp=args.true_peak_headroom,
        lufs_target=args.lufs_target,
        lufs_tolerance=args.lufs_tolerance,
    )
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        print(
            "  提示：未找到 ffmpeg，跳过 LUFS / True Peak，且非 WAV 文件无法解码",
            file=sys.stderr,
        )
    report = scan(
        root=root.resolve(),
        cfg=cfg,
        ffmpeg=ffmpeg,
        jobs=max(1, args.jobs),
        do_loudness=not args.no_loudness,
        exts=args.ext.split(","),
        progress=not args.quiet,
    )
    if args.json:
        _write_text(args.json, json.dumps(report, ensure_ascii=False, indent=2))
    if args.md:
        _write_text(args.md, render_scan_markdown(report))

    summary = report["summary"]
    print(
        f"检查 {summary['files']} 个文件："
        f"PASS {summary['pass']} / INFO {summary['info']} /"
        f" WARN {summary['warn']} / FAIL {summary['fail']}"
        f"　（{report['elapsed_s']} s）"
    )
    for item in report["files"]:
        if item["status"] == "FAIL":
            checks = "、".join(
                sorted({i["check"] for i in item["issues"] if i["severity"] == "FAIL"})
            )
            print(f"  FAIL  {item['path']}  [{checks}]")
    if args.json:
        print(f"JSON 报告：{args.json}")
    if args.md:
        print(f"Markdown 报告：{args.md}")
    return 1 if summary["fail"] else 0


def cmd_compare(args: argparse.Namespace) -> int:
    diff = compare_reports(_load_json(args.baseline), _load_json(args.current))
    if args.json:
        _write_text(args.json, json.dumps(diff, ensure_ascii=False, indent=2))
    if args.md:
        _write_text(args.md, render_compare_markdown(diff))
    summary = diff["summary"]
    print(
        f"新增 {summary['added']} / 删除 {summary['removed']} /"
        f" 内容变更 {summary['changed']}　判定回归 {summary['regressions']}"
    )
    for entry in diff["changed"]:
        if entry["severity"] == "FAIL":
            detail = "；".join(
                entry["notes"] + [f"{d['metric']} {d['delta']:+g}" for d in entry["deltas"]]
            )
            print(f"  回归  {entry['path']}  {detail}")
    return 1 if summary["regressions"] else 0


def cmd_report(args: argparse.Namespace) -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise AudioQAError("未设置环境变量 DEEPSEEK_API_KEY")
    text = draft_bug_reports(_load_json(args.scan_json), api_key, args.model, args.timeout)
    if args.md:
        _write_text(args.md, text)
        print(f"缺陷报告草稿：{args.md}")
    else:
        print(text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"scan": cmd_scan, "compare": cmd_compare, "report": cmd_report}
    try:
        return handlers[args.command](args)
    except AudioQAError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
