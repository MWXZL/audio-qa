#!/usr/bin/env python3
"""现场记录助手：把一段录制目录变成「客观测量 + 待填报告骨架」。

为什么存在：现场最容易疲劳出错的两件事是「漏填环境字段」和「把主观听感
当成唯一证据」。这个脚本把可测量的部分（时长、峰值、响度、真峰值、削波、
段内静音间隙）自动算出来填进骨架，把必须由人判断的部分（结论、定性、
复现次数）留成空格。

工具只给证据，不代替判定：它不会替你写 PASS/FAIL，也不会把间隙认成缺陷。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audio_qa  # noqa: E402

DEFAULT_EXTS = "mkv,mp4,mov,mka,m4a,wav,flac"
# 现场命名规则（见 采集计划 第五节）
NAME_PATTERN = re.compile(
    r"^(raw_\d{8}_|[a-z]+_\d{8}_|\d{4}-\d{2}-\d{2}_).*\.(mkv|mp4|mov|mka|m4a|wav|flac|ogg)$",
    re.IGNORECASE,
)
CASE_PATTERN = re.compile(r"(bug_\d+|RC-\d+|C-\d+|compat_[a-z]+_c\d+)", re.IGNORECASE)
VIDEO_EXTS = (".mkv", ".mp4", ".mov", ".avi", ".webm")
# 工具自己切出来的**听辨工作副本**：按句切好的片段、按状态切好的字幕截图。
# 它们是从同目录的录像/无损音轨派生的，不是独立证据——列进测量表会把「3 段素材」
# 变成「3 段 + 98 个片段」，报告立刻失真（实测踩过：bug_04 的量表被 98 个 wav 灌满）。
DERIVED_NAME_PREFIXES = ("句_", "语音片段_", "字幕截图_")


def is_derived_workcopy(path: Path) -> bool:
    """是不是工具派生的工作副本（不进测量、不进包）。纯函数。"""
    return path.name.startswith(DERIVED_NAME_PREFIXES)


def collect_evidence_files(directory: Path, exts: Sequence[str]) -> list[Path]:
    """目录里真正算证据的媒体文件（排除派生工作副本）。"""
    return [path for path in audio_qa.collect_files(directory, exts)
            if not is_derived_workcopy(path)]



def dedupe_takes(paths: Sequence[Path]) -> list[Path]:
    """同一段落里「录像 + 从它抽出的无损音轨」是**一段素材，不是两段**。

    采集流水线会在归档目录里同时留下 `xxx.mkv` 与 `xxx.mka`（音轨是无损拷贝，
    便于提交和听辨）。若不合并，报告会把 1 段算成 2 段——「执行次数」「复现率」
    正好是证据里最不能错的两个数字（实测踩过：1 段录制成报告里写 2 段）。
    同一 stem 只留一个，优先留带视频轨的那个。纯函数。
    """
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        grouped.setdefault(path.stem, []).append(path)
    kept: list[Path] = []
    for stem in sorted(grouped):
        group = sorted(grouped[stem],
                       key=lambda item: (item.suffix.lower() not in VIDEO_EXTS, str(item)))
        kept.append(group[0])
    return sorted(kept)


def infer_case(directory: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    match = CASE_PATTERN.search(directory.name)
    return match.group(1) if match else directory.name


def gap_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    """从扫描结果里取出段内静音间隙，供逐条定性用。"""
    entries: list[dict[str, Any]] = []
    for item in report["files"]:
        for issue in item["issues"]:
            if issue["check"] != "silence_gap":
                continue
            for gap in issue["detail"].get("gaps", []):
                entries.append({"path": item["path"], **gap})
    entries.sort(key=lambda entry: (entry["path"], entry["time_s"]))
    return entries


def gap_truncation(report: dict[str, Any]) -> str:
    """哪些片段的时间间隙表被截断了（`measure.json` 每段只存前 20 处）。纯函数。

    为什么要明说：表格看起来是完整的，实际只列了前 20 处——一段里有 61 处间隙时，
    报告与测量数字会对不上，读者会以为漏检或以为表格就是全部。
    """
    parts: list[str] = []
    for item in report["files"]:
        for issue in item["issues"]:
            if issue["check"] != "silence_gap":
                continue
            listed = len(issue["detail"].get("gaps", []))
            total = int(issue["detail"].get("gap_count", listed))
            if total > listed:
                parts.append(f"`{item['path']}` 列出 {listed} / 实际 {total} 处")
    return "；".join(parts)


def naming_findings(report: dict[str, Any]) -> list[str]:
    return [item["path"] for item in report["files"] if not NAME_PATTERN.match(Path(item["path"]).name)]


def host_environment() -> dict[str, str]:
    """能自动确定的环境字段：系统 / 平台 / OBS 采集设置。

    放在骨架生成里（而不是调用方），因为骨架会因重新测量而被重写——
    预填如果依赖调用方补，重写一次就丢了（实测踩过）。
    这里只读机器侧事实与 OBS 配置文件，不依赖 OBS 是否在运行。
    """
    import configparser
    import ctypes
    import os
    import platform
    import winreg

    values: dict[str, str] = {}
    try:
        user32 = ctypes.windll.user32
        screen = f"{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}"
    except Exception:
        screen = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except Exception:
        cpu = (platform.processor() or "未知 CPU").strip()
    values["平台 / 型号"] = " · ".join(
        part for part in (cpu, platform.platform(), f"屏幕 {screen}" if screen else "") if part
    )
    values["系统版本"] = f"{platform.system()} {platform.release()}（{platform.version()}）"

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return values
    root = Path(appdata) / "obs-studio"
    profile = "未命名"
    user_ini = root / "user.ini"
    if user_ini.is_file():
        for line in user_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Profile="):
                profile = line.split("=", 1)[1].strip() or profile
    ini = root / "basic" / "profiles" / profile / "basic.ini"
    if not ini.is_file():
        return values
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(ini, encoding="utf-8-sig")   # OBS 的 ini 带 BOM，不用 utf-8-sig 会解析失败
    except Exception:
        return values

    def get(section: str, key: str) -> str:
        return (parser.get(section, key, fallback="") or "").strip()

    parts = [
        f"{get('Video', 'BaseCX') or '?'}x{get('Video', 'BaseCY') or '?'}",
        f"{get('Video', 'FPSCommon')} fps" if get("Video", "FPSCommon") else "",
        f"{get('Audio', 'SampleRate')} Hz {get('Audio', 'ChannelSetup')}".strip()
        if get("Audio", "SampleRate") else "",
        get("SimpleOutput", "RecFormat2") or get("AdvOut", "RecFormat"),
        f"轨道 {get('SimpleOutput', 'RecTracks') or get('AdvOut', 'RecTracks')}"
        if (get("SimpleOutput", "RecTracks") or get("AdvOut", "RecTracks")) else "",
    ]
    values["采集设置（OBS 分辨率 / 帧率 / 采样率 / 轨道）"] = " / ".join(p for p in parts if p and "?" not in p)
    return values

HUMAN_LINE_PREFIXES = ("- 结论", "- 执行次数：", "- 复现率：", "- 各段是否", "- 预期：",
                       "- 实际", "- 关键时间码：", "- 是否升级为缺陷", "- 其它备注：")
# 「执行次数」与「复现率」的分母是从目录里的片段数算出来的，而目录会随着补录而变长。
# 它们如果当成普通人工行保留下来，补录第 2、3 段之后报告里会一直写着 1 段（实测踩过），
# 而这两个数字正是证据里最不能错的地方——所以只保留「人改过的形态」。
AUTO_COUNT_LINE = re.compile(r"^- 执行次数：\d+（本目录内 \d+ 个片段）$")
AUTO_RATE_LINE = re.compile(r"^- 复现率：__ / \d+$")
# 复现率一行拆成「分子 / 分母 + 尾巴」：分母是机器按片段数算的，分子和尾巴是人写的。
# 尾巴必须一起保留——实测踩过：「0 / 3（三次均未出现异常）」重生成后变成了「0 / 3」，
# 把执行者写下的判断依据悄悄删掉了。
RATE_LINE = re.compile(r"^- 复现率：(?P<head>[^/]*)/\s*(?P<den>\d+)(?P<tail>.*)$")
RATE_PREFIX = "- 复现率："
# 模板自带的「冒号后文字」：不算人工内容。不区分的话，「没填」会被误判成「填了」，
# 于是空白骨架也会被当成已完成的报告（标题上的「（待填）」会被错误地摘掉）。
TEMPLATE_TAILS = ("（PASS / FAIL / BLOCKED / 未复现）：", "（写可观察事实 + 时间码）：",
                  "是 / 否", "")


def human_row_key(cells: list[str]) -> tuple[str, str] | None:
    """表格行的身份：(第一列, 第二列)。第三节的间隙表就是 (片段, 时间码)。"""
    if len(cells) >= 3 and cells[0].startswith("`") and cells[1]:
        return (cells[0], cells[1])
    return None


def collect_human_rows(old_text: str) -> dict[tuple[str, str], list[str]]:
    """收集旧文件里人工填过的表格行尾巴（第三节的「画面内容 / 定性」两列）。

    为什么按行身份而不是按行号搬：间隙表是测量结果，重跑测量会重排、增删行；
    只有按 (片段, 时间码) 认领，人的定性才不会错位到别的间隙上。
    """
    kept: dict[tuple[str, str], list[str]] = {}
    for line in old_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 4:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        key = human_row_key(cells)
        if key and any(cells[2:]):
            kept[key] = cells[2:]
    return kept


def collect_human_lines(old_text: str) -> tuple[dict[str, str], str, str]:
    """收集结论段里人工填写的行（含缩进续行）与复现率的分子/尾巴。

    续行必须一起带走：实测踩过——「- 实际：」下面列了 5 条事实，重新生成后
    只剩冒号，五条事实全没了（那正是结论的主体）。
    """
    lines = old_text.splitlines()
    kept: dict[str, str] = {}
    numerator = ""
    tail_text = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        for prefix in HUMAN_LINE_PREFIXES:
            if not stripped.startswith(prefix):
                continue
            field_tail = stripped[len(prefix):].strip()
            if prefix == "- 执行次数：" and AUTO_COUNT_LINE.match(stripped):
                break             # 机器预填的形状：片段数变了就该跟着变
            if prefix == RATE_PREFIX:
                if not field_tail:
                    break
                match = RATE_LINE.match(stripped)
                if match and not AUTO_RATE_LINE.match(stripped):
                    numerator = match.group("head").strip()
                    tail_text = match.group("tail")
                elif not match:
                    kept[f"line:{prefix}"] = stripped   # 人写成了别的形态，原样保留
                break             # 分母由新片段数决定，稍后重组这一行
            block = [stripped]
            probe = index + 1
            while probe < len(lines) and lines[probe][:1].isspace() and lines[probe].strip():
                block.append(lines[probe].rstrip())
                probe += 1
            # 只有「人真的写了东西」才算填过：尾巴不等于模板文案，或者下面带了说明行
            if field_tail not in TEMPLATE_TAILS or len(block) > 1:
                kept[f"line:{prefix}"] = "\n".join(block)
            break
    return kept, numerator, tail_text


def preserve_human_edits(new_text: str, previous_path: Path) -> str:
    """重新生成骨架时，保留已有文件里**人工填写**的内容。

    为什么必须做：骨架会因重新测量而被重写，而结论是人写的。
    实测踩过两次：① 补测一段后重跑测量，手写的 PASS/预期/实际全被清空；
    ② 收下了首行却丢掉缩进续行，结论只剩一个冒号。
    规则：环境表与非空的环境格、结论段冒号后有内容的行（含其缩进续行）、
    以及间隙表里人工填过的列——只回填这几类。
    例外是「执行次数 / 复现率」：自动预填的部分（片段数）必须跟着目录走，
    人类真的改过形态时才保留（见 AUTO_COUNT_LINE / AUTO_RATE_LINE）。
    """
    if not previous_path.is_file():
        return new_text
    old = previous_path.read_text(encoding="utf-8")
    kept: dict[str, str] = {}
    for line in old.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) >= 2 and cells[1] and cells[0] not in {"项目", "---"}:
                kept[f"env:{cells[0]}"] = cells[1]
    line_kept, rate_numerator, rate_tail = collect_human_lines(old)
    kept.update(line_kept)
    rows = collect_human_rows(old)
    if not kept and not rate_numerator and not rows:
        return new_text

    out: list[str] = []
    new_lines = new_text.splitlines()
    index = 0
    while index < len(new_lines):
        line = new_lines[index]
        stripped = line.strip()
        replaced = False
        consumed = 0
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) >= 2 and not cells[1]:
                value = kept.get(f"env:{cells[0]}")
                if value:
                    out.append(f"| {cells[0]} | {value} |")
                    replaced = True
            if not replaced and len(cells) >= 4:
                # 只把**空着的**格子按位置补上：机器算出来的列（时长、间隙位置）不能被人的旧值覆盖。
                saved = rows.get(human_row_key(cells) or ("", ""))
                if saved:
                    merged = list(cells)
                    changed = False
                    for offset, value in enumerate(saved):
                        position = 2 + offset
                        if position < len(merged) and value and not merged[position]:
                            merged[position] = value
                            changed = True
                    if changed:
                        out.append("| " + " | ".join(merged) + " |")
                        replaced = True
        if not replaced and stripped.startswith(RATE_PREFIX) and rate_numerator:
            total = stripped.rsplit("/", 1)[-1].strip() if "/" in stripped else ""
            out.append(f"{RATE_PREFIX}{rate_numerator} / {total}{rate_tail}")
            replaced = True
        if not replaced:
            for prefix in HUMAN_LINE_PREFIXES:
                if stripped.startswith(prefix):
                    value = kept.get(f"line:{prefix}")
                    if value:
                        out.append(value)
                        replaced = True
                        if "\n" in value:
                            # 人的缩进续行已经一并带回来了，新文本里生成的续行必须跳过，
                            # 否则同一段说明会出现两次。
                            probe = index + 1
                            while (probe < len(new_lines) and new_lines[probe][:1].isspace()
                                   and new_lines[probe].strip()):
                                probe += 1
                            consumed = probe - index - 1
                    break
        if not replaced:
            out.append(line)
        index += 1 + consumed
    # 填过的小节就不该再顶着「（待填）」——已经跑完的用例，报告标题还写着「待填」，
    # 自检会把它算成未完成项，报告上写的是同一句话。
    filled_env = any(key == "env:游戏 / 版本" for key in kept)
    filled_verdict = any(key.startswith("line:- 结论") for key in kept)
    if filled_env:
        out = [line.replace("## 一、环境（待填）", "## 一、环境") for line in out]
    if filled_verdict:
        out = [line.replace("## 四、结论（待填）", "## 四、结论") for line in out]
    return "\n".join(out) + "\n"

def render_skeleton(
    case: str, report: dict[str, Any], directory: Path, dropout_min_ms: float
) -> str:
    env = host_environment()
    files = report["files"]
    gaps = gap_entries(report)
    off_naming = naming_findings(report)
    lines: list[str] = [
        f"# {case} 现场记录",
        "",
        f"> 由 `scripts/field_session.py` 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}；"
        f"录制目录 `{directory}`。",
        "> 第二节是工具算出来的客观测量，可直接引用；",
        "> 第一、三、四节必须由执行者本人填写——工具只提供证据，不代替判定。",
        "",
        "## 一、环境（待填）",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        "| 游戏 / 版本 |  |",
        f"| 平台 / 型号 | {env.get('平台 / 型号', '')} |",
        f"| 系统版本 | {env.get('系统版本', '')} |",
        "| 输出设备 |  |",
        "| 游戏音频设置 |  |",
        "| 其他音频开关（蓝牙编码 / 空间音效 / 独占模式） |  |",
        f"| 采集设置（OBS 分辨率 / 帧率 / 采样率 / 轨道） | {env.get('采集设置（OBS 分辨率 / 帧率 / 采样率 / 轨道）', '')} |",
        "| 网络与并发情况 |  |",
        "",
        "## 二、客观测量（自动生成，可直接引用）",
        "",
        f"判据下限：段内静音间隙 ≥ {dropout_min_ms:.0f} ms；"
        f"静音门限 {report['thresholds']['silence_floor_dbfs']:.0f} dBFS。"
        f"响度测量：{'开启' if report['loudness_enabled'] else '未开启'}。",
        "",
        "| 片段 | 时长(s) | 峰值(dBFS) | LUFS | True Peak | 削波段数 | 段内间隙数 | 最长间隙(ms) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in files:
        clips = [issue for issue in item["issues"] if issue["check"] == "clipping"]
        clip_count = len(clips[0]["detail"].get("runs", [])) if clips else 0
        lines.append(
            f"| `{item['path']}` | {item['duration_s']:.2f} |"
            f" {_cell(item['peak_dbfs'])} | {_cell(item['lufs'])} | {_cell(item['true_peak_dbtp'])} |"
            f" {clip_count} | {item['dropout_count']} | {_cell(item['dropout_max_ms'])} |"
        )
    lines += ["", "## 三、需与视频核对的间隙（逐条定性）", ""]
    if gaps:
        lines += [
            "工具只报 WARN：加载、传送、剧情转场处的静音是**设计行为**，"
            "只凭音频无法与「断流」区分，必须回到录像画面定性。",
            "",
            "| 片段 | 时间码 | 时长(ms) | 画面内容（看帧后填） | 定性（design / 候选缺陷） |",
            "| --- | --- | --- | --- | --- |",
        ]
        for gap in gaps:
            lines.append(
                f"| `{gap['path']}` | {_timecode(gap['time_s'])} | {gap['duration_ms']:.0f} |  |  |"
            )
    else:
        lines.append("本批片段未检出段内静音间隙（达到设定下限的）。")
    truncated = gap_truncation(report)
    if truncated:
        lines += [
            "",
            f"> **本表不完整**：{truncated}。`measure.json` 为了控制体积只保留每段前 20 处间隙，"
            "完整处数见第二节的「段内间隙数」列；要逐条定性全部间隙，"
            "用 `audio_qa.py scan --dropout-min-ms <下限>` 重新导出或直接看 `measure.json` 的计数。",
        ]
    lines += [
        "",
        "## 四、结论（待填）",
        "",
        "- 结论（PASS / FAIL / BLOCKED / 未复现）：",
        f"- 执行次数：{len(files)}（本目录内 {len(files)} 个片段）",
        f"- 复现率：__ / {len(files)}",
        "- 各段是否**同一场景、同一操作**（复现率的前提）：是 / 否",
        "  （若为「否」：本目录的素材只能写成**探索性观察**，不得计算复现率——"
        "不同场景之间的差异可能来自场景本身，而不是随机性）",
        "- 预期：",
        "- 实际（写可观察事实 + 时间码）：",
        "- 关键时间码：",
        "- 是否升级为缺陷（同一版本同一设备复现 ≥3 次才可）：",
        "- 其它备注：",
        "",
        "## 五、判定提醒",
        "",
        f"- 本用例的 {len(files)} 段是**同一用例的重复录制**，结论写整条用例（含复现率，例如 2/3），"
        "不要为每一段单独下一个结论；",
        "- 加载 / 传送 / 转场处的静音标 `design`，不计缺陷；",
        "- 同一位置、同一操作复现 ≥3 次才升级为缺陷报告；",
        "- 空闲负载下就出现的间隙，优先怀疑采集链路，先做对照基线"
        "（见 `断流压测方案` 第四节）；",
        "- 客观测量与主观听感必须写在一起，缺一不算完整证据。",
        "- 录像与关键帧只证明触发与现象，**不需要出现账号信息**；"
        "版本 / 设备 / 音频设置写成文字字段即可。",
        "",
    ]
    if off_naming:
        lines += [
            "## 六、命名提醒",
            "",
            "以下文件不符合 `raw_YYYYMMDD_<case>_r##` 体例，归档前建议改名：",
            "",
        ]
        lines += [f"- `{path}`" for path in off_naming]
        lines.append("")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _timecode(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{rest:06.3f}"


def run_session(
    directory: Path,
    case: str | None,
    exts: Sequence[str],
    dropout_min_ms: float,
    do_loudness: bool,
    quiet: bool = False,
) -> dict[str, Any]:
    if not directory.is_dir():
        raise audio_qa.AudioQAError(f"目录不存在：{directory}")
    ffmpeg = audio_qa.find_ffmpeg(None)
    cfg = audio_qa.Thresholds(dropout_min_ms=dropout_min_ms)
    # 先合并「同一段的录像与无损音轨」，再送去测量：否则执行次数 / 复现率会翻倍。
    deduped = dedupe_takes(collect_evidence_files(directory.resolve(), exts))
    report = audio_qa.scan(
        root=directory.resolve(),
        cfg=cfg,
        ffmpeg=ffmpeg,
        jobs=4,
        do_loudness=do_loudness and ffmpeg is not None,
        exts=exts,
        progress=not quiet,
        paths=deduped or None,
    )
    resolved_case = infer_case(directory, case)
    (directory / "measure.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "measure.md").write_text(
        audio_qa.render_scan_markdown(report), encoding="utf-8"
    )
    skeleton = render_skeleton(resolved_case, report, directory.resolve(), dropout_min_ms)
    target = directory / f"{resolved_case}_现场记录.md"
    skeleton = preserve_human_edits(skeleton, target)
    target.write_text(skeleton, encoding="utf-8")
    return {"report": report, "skeleton": target, "case": resolved_case, "ffmpeg": ffmpeg}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="field_session",
        description="把一段录制目录变成客观测量 + 待填报告骨架",
    )
    parser.add_argument("directory", type=Path, help="本次录制的目录")
    parser.add_argument("--case", help="用例编号（默认从目录名推断，如 bug_01）")
    parser.add_argument("--ext", default=DEFAULT_EXTS, help=f"参与扫描的扩展名，默认 {DEFAULT_EXTS}")
    parser.add_argument("--dropout-min-ms", type=float, default=80.0,
                        help="段内静音间隙下限毫秒数；0 = 关闭")
    parser.add_argument("--no-loudness", action="store_true", help="跳过 LUFS / True Peak")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_session(
            directory=args.directory,
            case=args.case,
            exts=args.ext.split(","),
            dropout_min_ms=args.dropout_min_ms,
            do_loudness=not args.no_loudness,
            quiet=args.quiet,
        )
    except audio_qa.AudioQAError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    report = result["report"]
    summary = report["summary"]
    gaps = gap_entries(report)
    print(
        f"用例 {result['case']}：{summary['files']} 个片段，"
        f"PASS {summary['pass']} / INFO {summary['info']} / WARN {summary['warn']} / FAIL {summary['fail']}"
    )
    print(f"客观测量：{args.directory / 'measure.md'}")
    print(f"报告骨架：{result['skeleton']}")
    print(f"待核对间隙：{len(gaps)} 处" + ("（见骨架第三节）" if gaps else ""))
    if result["ffmpeg"] is None:
        print("提示：未找到 ffmpeg，非 WAV 片段无法解码，响度测量已跳过", file=sys.stderr)
    print("下一步：填第一、三、四节；间隙定性必须回到录像画面，design 的不算缺陷。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
