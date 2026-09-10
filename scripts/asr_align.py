#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr_align —— 语音-字幕对齐自动检查工具

用途
    把 ASR 转写（带时间戳）与一份人工标注的语音-字幕基线逐句比对，检出四类差异：
        missing_speech    有字幕、该时段 ASR 无文本（字幕出了但没有语音 / 语音被截断）
        missing_subtitle  有语音、无对应字幕（语音在播但字幕没跟上）
        misordered        文本与字幕内容对不上，但两边都能识别（旧语音没停 / 字幕停在上一句）
        offset            文本一致，但语音起点与字幕出现时刻之差超出容差（同步漂移）
    输出机器可读 JSON 与人可读 Markdown 报告。

依赖边界
    纯 Python 标准库实现：argparse / dataclasses / json / csv / pathlib / statistics /
    difflib / unicodedata。文本比对用「规范化 + difflib.SequenceMatcher 相似度」，
    不引入任何第三方相似度库。
    ASR 引擎只在真正需要现场转写（--asr 传入的是音频而非已转写好的 JSON）时才在
    函数内部 import；引擎缺失时抛出明确错误，不影响 --asr 传 JSON 的纯标准库路径。

判据边界（必须逐条读）
    * offset 阈值「> 250 ms 记 FAIL、150–250 ms 记 WARN」是**待校准草案**，不是结论。
      依据是字幕属「读写」而非唇音同步，感知阈值比唇音同步（ITU-R BT.1359 那类
      几十毫秒量级）宽得多。必须先用人工基线的实测 offset 分布校准；若基线显示
      正常对话本身就有 ±400 ms 的常态偏移，就必须放宽阈值，不能再拿 250 ms 报问题。
    * 文本相似度用 difflib.SequenceMatcher：它是**顺序敏感**的编辑距离型算法，
      把词序调换会打低分（哪怕语义相同），而长句里个别 ASR 错字几乎不拉分。
      短字幕（2–3 字）错一个字相似度就可能跌破默认 0.6，从而把「ASR 认错字」
      误报成 misordered——所以每一类差异都必须人工回听复核，工具只给证据。
    * 本工具只做机械比对，不下主观结论：报告把「已确认差异」与「疑似差异」分开
      计数，绝不合并。只有基线条目的备注里明确写了「已确认」的，才计入已确认；
      其余全部按疑似处理（含 AI 自己转写出来的每一条）。

退出码
    0 无 FAIL 级差异   1 存在 FAIL 级差异   2 工具自身错误（解析失败、参数非法、引擎缺失）

CLI 示例
    python scripts\\asr_align.py --baseline baseline.tsv --asr asr.json --out align.md [--json align.json]
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import statistics
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

VERSION = "0.1.0"

# 基线表头（第一行必须逐列一致；多出列会被忽略）
BASELINE_COLUMNS = ("句号", "语音起", "语音止", "字幕出现", "字幕消失", "字幕原文", "备注")

# 差异类别按报告展示顺序固定，保证输出稳定、可读
CATEGORY_ORDER = ("missing_speech", "missing_subtitle", "misordered", "offset")

CATEGORY_LABEL = {
    "missing_speech": "缺语音",
    "missing_subtitle": "缺字幕",
    "misordered": "错位",
    "offset": "偏移",
}

CATEGORY_DESC = {
    "missing_speech": "有字幕、该时段 ASR 无文本",
    "missing_subtitle": "有语音、无对应字幕",
    "misordered": "文本与字幕对不上但都能识别",
    "offset": "文本一致但语音起点与字幕出现时刻之差超容差",
}

SEVERITY_ORDER = {"PASS": 0, "WARN": 2, "FAIL": 3}

# 方案草案的原话，用于报告顶部与 JSON 的文案
DRAFT_NOTE = (
    "偏移阈值为待校准草案（offset > 250 ms 记 FAIL、150–250 ms 记 WARN），"
    "必须用人工基线的实测分布校准后才可下结论；未校准前所有差异都只是疑似问题，"
    "需人工回听复核，不得据此直接判定缺陷。"
)


class AsrAlignError(Exception):
    """工具级错误（输入解析失败、参数非法、ASR 引擎缺失等）。"""


@dataclass
class BaselineRow:
    """人工基线里的一行（一句对话）。时间单位秒，空单元格计作 None。"""

    index: str                       # 句号
    speech_start: float | None       # 语音起
    speech_end: float | None         # 语音止
    subtitle_start: float | None     # 字幕出现
    subtitle_end: float | None       # 字幕消失
    subtitle_text: str               # 字幕原文（可能为空 => 该句无字幕）
    note: str = ""                   # 备注（可含「已确认」标记，见 _is_confirmed）


@dataclass
class ASRSegment:
    """ASR 转写出来的一段带时间戳文本。"""

    start: float
    end: float
    text: str


@dataclass
class Thresholds:
    """判定阈值。默认值取语音-字幕对齐方案里的草案，可由命令行覆盖。"""

    fail_ms: float = 250.0           # |offset| 超过记 FAIL
    warn_ms: float = 150.0           # 介于 warn 与 fail 之间记 WARN
    similarity: float = 0.6          # 文本相似度低于此值判 misordered

    def as_dict(self) -> dict[str, Any]:
        return {
            "fail_ms": self.fail_ms,
            "warn_ms": self.warn_ms,
            "similarity": self.similarity,
        }


@dataclass
class Difference:
    """一条检出差异。severity 只表达「数据离容差多远」，不代表人已确认。"""

    category: str
    severity: str
    index: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "index": self.index,
            "message": self.message,
            "detail": self.detail,
            "confirmed": self.confirmed,
        }


# --------------------------------------------------------------------------
# 文本规范化与相似度（纯标准库）
# --------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """把文本压成可比的规范形：NFKC 全角转半角、统一大小写、去空白与标点。

    只保留字母数字与中日韩表意字。NFKC 会把全角英数/标点折成半角，顺带折叠
    兼容字符（如①→1），对「是不是同一句话」的判断够用；代价是它也会折叠少数
    视觉等价但不完全相同的符号，极罕见于字幕文本，这里不单独处理。
    """
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in folded if ch.isalnum())


def text_similarity(a: str, b: str) -> float:
    """规范化后的字符级相似度，0.0–1.0，用 difflib.SequenceMatcher。

    选它的理由：标准库自带、只依赖字符序（字幕是顺序文本，不是词袋），无需
    训练语料。已知局限：顺序敏感——「打过了吗」与「过了吗打」会被压分；长句
    里个别错字几乎不影响分数。因此相似度只能做粗筛，命中/失配都得人工复核。
    """
    na, nb = normalize_text(a), normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# --------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------
def _parse_seconds(token: str, lineno: int, column: str) -> float | None:
    """把单元格解析成秒数。空 => None；非空却解析不了 => 报错，不静默跳过。

    静默把坏时间当成「无时间窗」会把缺失字幕误判成对齐，属假阴性，比报错更糟。
    """
    token = (token or "").strip()
    if not token:
        return None
    try:
        value = float(token)
    except ValueError as exc:
        raise AsrAlignError(f"基线第 {lineno} 行「{column}」无法解析为秒数：{token!r}") from exc
    return value if math.isfinite(value) else None


def load_baseline(path: Path) -> list[BaselineRow]:
    """读取人工基线（TSV 或 CSV，UTF-8，首行表头），返回按行号排序的条目。"""
    delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")  # utf-8-sig 剥 Excel BOM
    except OSError as exc:
        raise AsrAlignError(f"无法打开基线 {path}：{exc}") from exc
    with handle:
        reader = csv.reader(handle, delimiter=delimiter)
        header = next(reader, None)
    if header is None:
        raise AsrAlignError(f"基线 {path} 为空，缺少表头")
    header_cells = [cell.strip() for cell in header]
    if header_cells[: len(BASELINE_COLUMNS)] != list(BASELINE_COLUMNS):
        raise AsrAlignError(
            f"基线 {path} 表头不符，应为：{', '.join(BASELINE_COLUMNS)}"
            f"；实际：{', '.join(header_cells)}"
        )
    rows: list[BaselineRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        next(reader, None)  # 跳过表头
        for lineno, cells in enumerate(reader, 2):
            if not any((c or "").strip() for c in cells):
                continue
            if len(cells) < len(BASELINE_COLUMNS):
                raise AsrAlignError(
                    f"基线第 {lineno} 行只有 {len(cells)} 列，应至少 {len(BASELINE_COLUMNS)} 列"
                )
            rows.append(
                BaselineRow(
                    index=(cells[0].strip() or f"L{lineno}"),
                    speech_start=_parse_seconds(cells[1], lineno, "语音起"),
                    speech_end=_parse_seconds(cells[2], lineno, "语音止"),
                    subtitle_start=_parse_seconds(cells[3], lineno, "字幕出现"),
                    subtitle_end=_parse_seconds(cells[4], lineno, "字幕消失"),
                    subtitle_text=(cells[5] or "").strip(),
                    note=(cells[6] or "").strip() if len(cells) > 6 else "",
                )
            )
    if not rows:
        raise AsrAlignError(f"基线 {path} 没有数据行")
    return rows


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsrAlignError(f"无法读取 {path}：{exc}") from exc


def _segment_from_dict(item: Any, position: str) -> ASRSegment:
    """把 JSON 里的一段折成 ASRSegment，字段缺失/非法时报清楚错误。"""
    if not isinstance(item, dict):
        raise AsrAlignError(f"ASR JSON 的 {position} 不是对象：{item!r}")
    try:
        start = float(item["start"])
        end = float(item["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AsrAlignError(f"ASR JSON 的 {position} 缺少合法 start/end：{item!r}") from exc
    text = str(item.get("text") or "").strip()
    if end < start:
        raise AsrAlignError(f"ASR JSON 的 {position} end 早于 start：{item!r}")
    return ASRSegment(start, end, text)


def load_asr_segments(path: Path) -> list[ASRSegment]:
    """读取 ASR 结果：JSON 直接解析；音频文件则现场转写（懒导入引擎）。

    JSON 形如 {"segments": [{"start": 1.23, "end": 3.40, "text": "..."}]}，
    也容忍直接给一个数组。段会按 start 升序排序，便于后续对齐。
    """
    if path.suffix.lower() == ".json":
        data = _load_json(path)
        if isinstance(data, dict):
            segments_raw = data.get("segments")
        elif isinstance(data, list):
            segments_raw = data
        else:
            raise AsrAlignError("ASR JSON 需形如 {\"segments\": [...]} 或直接是 [ ... ]")
        if not isinstance(segments_raw, list):
            raise AsrAlignError("ASR JSON 缺少 segments 数组")
        segments = [
            _segment_from_dict(item, f"segments[{i}]") for i, item in enumerate(segments_raw)
        ]
        segments.sort(key=lambda s: (s.start, s.end))
        return segments
    return transcribe_audio(path)


def transcribe_audio(
    path: Path, model: str = "small", language: str | None = None
) -> list[ASRSegment]:
    """对音频现场跑 ASR，折成与 JSON 输入一致的 segments 结构。

    这是唯一会 import 第三方库的地方：引擎在函数内按需导入，未安装立即报清晰
    错误，不影响上面那条「--asr 传 JSON」的纯标准库主路径。
    """
    if not path.exists():
        raise AsrAlignError(f"音频文件不存在：{path}")
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AsrAlignError(
            "需要现场转写但缺少 ASR 引擎：请安装 faster-whisper"
            "（pip install faster-whisper），或先用它导出 segments JSON 再经 --asr 传入"
        ) from exc
    whisper = WhisperModel(model, compute_type="int8")
    iterable, _info = whisper.transcribe(str(path), language=language)
    return [ASRSegment(float(s.start), float(s.end), (s.text or "").strip()) for s in iterable]


# --------------------------------------------------------------------------
# 对齐判定
# --------------------------------------------------------------------------
def _window(start: float | None, end: float | None) -> tuple[float, float] | None:
    if start is None or end is None:
        return None
    return (start, end)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _is_confirmed(row: BaselineRow) -> bool:
    """只有当人工在备注里明确写「已确认」才算已确认，其余一律疑似。

    这是「已确认 vs 疑似」分开计数、不合并的关键：工具自己没有确认资格，
    连续、正负号、相似度再漂亮都不算数，确认只能来自人标。
    """
    return "已确认" in (row.note or "")


def _offset_ms(row: BaselineRow, matched: list[ASRSegment]) -> float | None:
    """字幕出现时刻 − 语音起点，单位毫秒。正 = 字幕晚于语音，负 = 字幕早于语音。

    语音起点优先取人工基线的「语音起」（逐帧定位，精度 0.1 s），缺失时才退回
    匹配到的那段 ASR 起点。若无字幕出现时刻则无法定义偏移，返回 None。
    """
    if row.subtitle_start is None:
        return None
    if row.speech_start is not None:
        onset = row.speech_start
    elif matched:
        onset = min(seg.start for seg in matched)
    else:
        return None
    return (row.subtitle_start - onset) * 1000.0


def _percentile(values: list[float], fraction: float) -> float:
    """最近秩百分位，与 audio_qa 的 _percentile 同口径。"""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def align(
    rows: list[BaselineRow],
    segments: list[ASRSegment],
    thresholds: Thresholds,
    baseline_source: str = "",
    asr_source: str = "",
) -> dict[str, Any]:
    """核心比对：给每条基线行分类，捡出游离语音，并汇总 offset 统计。

    返回一个可直接 JSON 序列化的报告 dict，Markdown 渲染另走 render_markdown。
    """
    differences: list[Difference] = []
    offset_abs_ms: list[float] = []

    # 覆盖标记：某 ASR 段只要落进任一基线的字幕窗或语音窗，就算「有出处」。
    # 完全不与任何基线时段重合、却转出了文本的段 => 游游离语音（missing_subtitle）。
    covered = [False] * len(segments)

    def mark_covered(win: tuple[float, float]) -> None:
        for i, seg in enumerate(segments):
            if _overlap(win[0], win[1], seg.start, seg.end):
                covered[i] = True

    def overlap_segments(win: tuple[float, float]) -> list[ASRSegment]:
        return [seg for seg in segments if _overlap(win[0], win[1], seg.start, seg.end)]

    for row in rows:
        sp_win = _window(row.speech_start, row.speech_end)
        sub_win = _window(row.subtitle_start, row.subtitle_end)
        for win in (sp_win, sub_win):
            if win is not None:
                mark_covered(win)

        if not row.subtitle_text:
            # 有语音窗但没有字幕原文：语音在播、字幕没跟上
            if sp_win is not None:
                heard = [s.text for s in overlap_segments(sp_win) if s.text]
                if heard:
                    differences.append(
                        Difference(
                            category="missing_subtitle",
                            severity="WARN",
                            index=row.index,
                            message=(
                                f"语音时段 {sp_win[0]:.2f}–{sp_win[1]:.2f}s 有语音"
                                "「" + " ".join(heard) + "」，但基线未标注字幕原文"
                            ),
                            detail={
                                "speech_start_s": round(sp_win[0], 4),
                                "speech_end_s": round(sp_win[1], 4),
                                "heard_text": " ".join(heard),
                            },
                            confirmed=_is_confirmed(row),
                        )
                    )
            continue

        # 字幕行：以字幕窗为锚（无字幕窗时退回语音窗）。锚窗为空则无法定位，跳过不臆测
        anchor = sub_win if sub_win is not None else sp_win
        if anchor is None:
            continue
        matched = overlap_segments(anchor)
        asr_text = " ".join(s.text for s in matched if s.text)
        if not asr_text:
            differences.append(
                Difference(
                    category="missing_speech",
                    severity="WARN",
                    index=row.index,
                    message=(
                        f"字幕「{row.subtitle_text}」显示于 "
                        f"{anchor[0]:.2f}–{anchor[1]:.2f}s，ASR 在该时段无文本"
                    ),
                    detail={
                        "subtitle_start_s": round(anchor[0], 4),
                        "subtitle_end_s": round(anchor[1], 4),
                        "subtitle_text": row.subtitle_text,
                    },
                    confirmed=_is_confirmed(row),
                )
            )
            continue

        similarity = text_similarity(asr_text, row.subtitle_text)
        if similarity < thresholds.similarity:
            differences.append(
                Difference(
                    category="misordered",
                    severity="WARN",
                    index=row.index,
                    message=(
                        f"字幕「{row.subtitle_text}」与 ASR 文本「{asr_text}」相似度 "
                        f"{similarity:.2f}，低于阈值 {thresholds.similarity}"
                    ),
                    detail={
                        "similarity": round(similarity, 3),
                        "subtitle_text": row.subtitle_text,
                        "asr_text": asr_text,
                    },
                    confirmed=_is_confirmed(row),
                )
            )
            continue

        # 文本一致：量偏移，落在容差内则不报
        offset_ms = _offset_ms(row, matched)
        if offset_ms is None:
            continue
        abs_ms = abs(offset_ms)
        offset_abs_ms.append(abs_ms)
        if abs_ms <= thresholds.warn_ms:
            continue
        severity = "FAIL" if abs_ms > thresholds.fail_ms else "WARN"
        direction = "晚" if offset_ms > 0 else "早"
        if severity == "FAIL":
            bound = f"超过 FAIL 容差 {thresholds.fail_ms:g} ms"
        else:
            bound = f"介于 WARN {thresholds.warn_ms:g} 与 FAIL {thresholds.fail_ms:g} ms 之间"
        differences.append(
            Difference(
                category="offset",
                severity=severity,
                index=row.index,
                message=(
                    f"文本一致但字幕出现比语音起点{direction} "
                    f"{abs_ms:.0f} ms（{bound}）"
                ),
                detail={
                    "offset_ms": round(offset_ms, 1),
                    "abs_offset_ms": round(abs_ms, 1),
                    "speech_onset_s": round(
                        row.speech_start if row.speech_start is not None else min(s.start for s in matched), 4
                    ),
                    "subtitle_start_s": round(row.subtitle_start, 4),
                    "similarity": round(similarity, 3),
                },
                confirmed=_is_confirmed(row),
            )
        )

    # 游离 ASR 段：不与任何基线时段重合，却转出了文本 => 有语音、无对应字幕
    for i, seg in enumerate(segments):
        if not covered[i] and seg.text:
            differences.append(
                Difference(
                    category="missing_subtitle",
                    severity="WARN",
                    index=f"ASR#{i + 1}",
                    message=(
                        f"ASR 在 {seg.start:.2f}–{seg.end:.2f}s 转出「{seg.text}」，"
                        "基线无该时段字幕"
                    ),
                    detail={
                        "start_s": round(seg.start, 4),
                        "end_s": round(seg.end, 4),
                        "text": seg.text,
                    },
                    confirmed=False,
                )
            )

    differences.sort(
        key=lambda d: (CATEGORY_ORDER.index(d.category), d.index)
    )

    offset_diffs = [d for d in differences if d.category == "offset"]
    offset_fail = sum(1 for d in offset_diffs if d.severity == "FAIL")
    offset_warn = sum(1 for d in offset_diffs if d.severity == "WARN")

    if offset_abs_ms:
        median_ms = round(float(statistics.median(offset_abs_ms)), 1)
        p90_ms = round(_percentile(offset_abs_ms, 0.90), 1)
        max_abs_ms = round(max(offset_abs_ms), 1)
    else:
        median_ms = p90_ms = max_abs_ms = None

    by_category = Counter(d.category for d in differences)
    by_severity = Counter(d.severity for d in differences)
    confirmed = sum(1 for d in differences if d.confirmed)

    return {
        "tool": "asr_align",
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": baseline_source,
        "asr": asr_source,
        "draft_note": DRAFT_NOTE,
        "thresholds": thresholds.as_dict(),
        "summary": {
            "baseline_rows": len(rows),
            "asr_segments": len(segments),
            "differences": len(differences),
            # 结论里「已确认 / 疑似」分开计数，绝不合并
            "confirmed": confirmed,
            "suspected": len(differences) - confirmed,
            "fail": by_severity.get("FAIL", 0),
            "warn": by_severity.get("WARN", 0),
            "by_category": {cat: by_category.get(cat, 0) for cat in CATEGORY_ORDER},
            "offset_stats": {
                # 统计口径：覆盖全部「文本一致且能算出偏移」的句（含容差内者），
                # 用于给阈值做校准；超容差条目数 = 被报成 WARN/FAIL 的偏移数量
                "evaluated": len(offset_abs_ms),
                "median_ms": median_ms,
                "p90_ms": p90_ms,
                "max_abs_ms": max_abs_ms,
                "exceed_count": offset_fail + offset_warn,
                "fail_count": offset_fail,
                "warn_count": offset_warn,
            },
        },
        "differences": [d.as_dict() for d in differences],
    }


# --------------------------------------------------------------------------
# Markdown 渲染
# --------------------------------------------------------------------------
def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:g}{suffix}"


def render_markdown(report: dict[str, Any]) -> str:
    """把 align() 的报告渲染成 Markdown。顶部先声明草案状态，结论分两类计数。"""
    summary = report["summary"]
    thr = report["thresholds"]
    lines = [
        "# 语音-字幕对齐检查报告",
        "",
        "> **阈值提示（待校准草案）：** offset > "
        f"{_fmt(thr['fail_ms'])} ms 记 FAIL、{_fmt(thr['warn_ms'])}–{_fmt(thr['fail_ms'])} ms 记 WARN，"
        f"文本相似度阈值 {_fmt(thr['similarity'])}。"
        "这是**待校准草案**，必须用人工基线的实测分布校准后才可下结论；"
        "未校准前所有差异都只是疑似问题，需人工回听复核。",
        "",
        f"- 人工基线：{report['baseline']}（{summary['baseline_rows']} 句）",
        f"- ASR 转写：{report['asr']}（{summary['asr_segments']} 段）",
        f"- 生成时间：{report['generated_at']}",
        "",
        "## 结论",
        "",
        f"- **已确认差异：{summary['confirmed']} 条**（仅统计基线条目备注中明确标注「已确认」者）",
        f"- **疑似差异：{summary['suspected']} 条**（其余全部，需人工回听复核，不并入已确认计数）",
        "",
        "两类差异分开计数、不合并：工具只做机械比对，不下结论；"
        "确认只能来自人的回听标注。",
        "",
        "| 差异类型 | 疑似 | 已确认 | 判定 |",
        "| --- | --- | --- | --- |",
    ]
    for cat in CATEGORY_ORDER:
        confirmed = sum(1 for d in report["differences"] if d["category"] == cat and d["confirmed"])
        suspected = summary["by_category"][cat] - confirmed
        lines.append(
            f"| {cat}（{CATEGORY_LABEL[cat]}） | {suspected} | {confirmed} | {CATEGORY_DESC[cat]} |"
        )
    lines.append("")

    stats = summary["offset_stats"]
    lines += [
        "## offset 统计",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 参与统计的文本一致句 | {stats['evaluated']} |",
        f"| 中位数 | {_fmt(stats['median_ms'], ' ms')} |",
        f"| P90 | {_fmt(stats['p90_ms'], ' ms')} |",
        f"| 最大绝对偏差 | {_fmt(stats['max_abs_ms'], ' ms')} |",
        f"| 超容差条目 | {stats['exceed_count']}"
        f"（其中 FAIL {stats['fail_count']} / WARN {stats['warn_count']}） |",
        "",
        "> 统计覆盖全部「文本一致且能算出偏移」的句（含容差内者），不是只算被报出的："
        "这份完整分布才是校准阈值的依据。",
        "",
    ]

    lines += ["## 差异清单", ""]
    if not report["differences"]:
        lines.append("本轮未检出差异。")
        lines.append("")
    else:
        for cat in CATEGORY_ORDER:
            rows = [d for d in report["differences"] if d["category"] == cat]
            if not rows:
                continue
            lines += [
                f"### {cat}（{CATEGORY_LABEL[cat]}）——{CATEGORY_DESC[cat]}",
                "",
                "| 句号 | 级别 | 说明 | 复核 |",
                "| --- | --- | --- | --- |",
            ]
            for d in rows:
                lines.append(
                    f"| {d['index']} | {d['severity']} | {d['message']} |"
                    f" {'已确认' if d['confirmed'] else '疑似'} |"
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
# CLI
# --------------------------------------------------------------------------
def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asr_align",
        description="语音-字幕对齐自动检查（纯标准库 + 可选懒加载 ASR）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--baseline", type=Path, required=True, help="人工基线 TSV/CSV 路径")
    parser.add_argument("--asr", type=Path, required=True, help="ASR 转写 JSON 路径（非 JSON 时现场转写）")
    parser.add_argument("--out", type=Path, required=True, help="Markdown 报告输出路径")
    parser.add_argument("--json", type=Path, default=None, help="JSON 报告输出路径（可选）")
    parser.add_argument("--fail-ms", type=float, default=Thresholds.fail_ms, help="offset 超此毫秒记 FAIL（草案）")
    parser.add_argument("--warn-ms", type=float, default=Thresholds.warn_ms, help="offset 超此毫秒记 WARN（草案）")
    parser.add_argument("--similarity", type=float, default=Thresholds.similarity, help="文本相似度阈值 0–1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not (0.0 <= args.similarity <= 1.0):
            raise AsrAlignError("--similarity 必须在 0 到 1 之间")
        if args.warn_ms > args.fail_ms:
            raise AsrAlignError("--warn-ms 不能大于 --fail-ms")
        thresholds = Thresholds(fail_ms=args.fail_ms, warn_ms=args.warn_ms, similarity=args.similarity)
        rows = load_baseline(args.baseline)
        segments = load_asr_segments(args.asr)
        report = align(
            rows,
            segments,
            thresholds,
            baseline_source=str(args.baseline),
            asr_source=str(args.asr),
        )
        _write_text(args.out, render_markdown(report))
        if args.json:
            _write_text(args.json, json.dumps(report, ensure_ascii=False, indent=2))

        summary = report["summary"]
        print(
            f"基线 {summary['baseline_rows']} 句 / ASR {summary['asr_segments']} 段："
            f"差异 {summary['differences']} 条（已确认 {summary['confirmed']} /"
            f" 疑似 {summary['suspected']} / FAIL {summary['fail']} / WARN {summary['warn']}）"
        )
        offset = summary["offset_stats"]
        if offset["evaluated"]:
            print(
                f"offset：中位数 {_fmt(offset['median_ms'])} ms / P90 {_fmt(offset['p90_ms'])} ms /"
                f" 最大 {_fmt(offset['max_abs_ms'])} ms / 超容差 {offset['exceed_count']} 条"
            )
        for diff in report["differences"]:
            if diff["severity"] == "FAIL":
                print(f"  FAIL  [{diff['category']}] {diff['index']}  {diff['message']}")
        print(f"Markdown 报告：{args.out}")
        if args.json:
            print(f"JSON 报告：{args.json}")
        return 1 if summary["fail"] else 0
    except AsrAlignError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())