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


def preserve_human_edits(new_text: str, previous_path: Path) -> str:
    """重新生成骨架时，保留已有文件里**人工填写**的内容。

    为什么必须做：骨架会因重新测量而被重写，而结论是人写的。
    实测踩过：补测一段后重跑测量，手写的 PASS/预期/实际全被清空。
    规则：环境表取非空格；结论段取冒号后有内容的行——只回填这两类。
    例外是「执行次数 / 复现率」：自动预填的部分（片段数）必须跟着目录走，
    人类真的改过形态时才保留（见 AUTO_COUNT_LINE / AUTO_RATE_LINE）。
    """
    if not previous_path.is_file():
        return new_text
    old = previous_path.read_text(encoding="utf-8")
    kept: dict[str, str] = {}
    rate_numerator = ""
    rate_tail = ""
    for line in old.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 2 and cells[1] and cells[0] not in {"项目", "---"}:
                kept[f"env:{cells[0]}"] = cells[1]
        for prefix in HUMAN_LINE_PREFIXES:
            if stripped.startswith(prefix):
                tail = stripped[len(prefix):].strip()
                if tail:
                    if prefix == "- 执行次数：" and AUTO_COUNT_LINE.match(stripped):
                        continue      # 机器预填的形状：片段数变了就该跟着变
                    if prefix == RATE_PREFIX:
                        match = RATE_LINE.match(stripped)
                        if match and not AUTO_RATE_LINE.match(stripped):
                            rate_numerator = match.group("head").strip()
                            rate_tail = match.group("tail")
                        elif not match:
                            kept[f"line:{prefix}"] = stripped   # 人写成了别的形态，原样保留
                        continue      # 分母由新片段数决定，稍后重组这一行
                    kept[f"line:{prefix}"] = stripped
    if not kept and not rate_numerator:
        return new_text

    out: list[str] = []
    for line in new_text.splitlines():
        stripped = line.strip()
        replaced = False
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 2 and not cells[1]:
                value = kept.get(f"env:{cells[0]}")
                if value:
                    out.append(f"| {cells[0]} | {value} |")
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
                    break
        if not replaced:
            out.append(line)
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
            "| 片段 | 时间码 | 时长(ms) | 画面内容（待填） | 定性（design / 候选缺陷） |",
            "| --- | --- | --- | --- | --- |",
        ]
        for gap in gaps:
            lines.append(
                f"| `{gap['path']}` | {_timecode(gap['time_s'])} | {gap['duration_ms']:.0f} |  |  |"
            )
    else:
        lines.append("本批片段未检出段内静音间隙（达到设定下限的）。")
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
    deduped = dedupe_takes(audio_qa.collect_files(directory.resolve(), exts))
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
