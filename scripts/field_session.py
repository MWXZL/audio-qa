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

DEFAULT_EXTS = "mkv,mp4,mov,wav,flac"
# 现场命名规则（见 采集计划 第五节）
NAME_PATTERN = re.compile(r"^(raw_\d{8}_|[a-z]+_\d{8}_|\d{4}-\d{2}-\d{2}_).*\.(mkv|mp4|mov|wav|flac)$", re.IGNORECASE)
CASE_PATTERN = re.compile(r"(bug_\d+|RC-\d+|C-\d+|compat_[a-z]+_c\d+)", re.IGNORECASE)


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


def render_skeleton(
    case: str, report: dict[str, Any], directory: Path, dropout_min_ms: float
) -> str:
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
        "| 平台 / 型号 |  |",
        "| 系统版本 |  |",
        "| 输出设备 |  |",
        "| 游戏音频设置 |  |",
        "| 其他音频开关（蓝牙编码 / 空间音效 / 独占模式） |  |",
        "| 采集设置（OBS 分辨率 / 帧率 / 采样率 / 轨道） |  |",
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
        "- 执行次数：",
        "- 预期：",
        "- 实际（写可观察事实 + 时间码）：",
        "- 关键时间码：",
        "- 是否升级为缺陷（同一版本同一设备复现 ≥3 次才可）：",
        "- 其它备注：",
        "",
        "## 五、判定提醒",
        "",
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
    report = audio_qa.scan(
        root=directory.resolve(),
        cfg=cfg,
        ffmpeg=ffmpeg,
        jobs=4,
        do_loudness=do_loudness and ffmpeg is not None,
        exts=exts,
        progress=not quiet,
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
