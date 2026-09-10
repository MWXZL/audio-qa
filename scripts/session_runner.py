#!/usr/bin/env python3
"""一次采集的后处理流水线：录完到证据进包，全自动。

分工要说清楚：
- **游戏里的操作只能由人做**——那是证据可信的来源，脚本不碰游戏；
- 录完之后的一切都能自动：命名归档、采集自检、无损抽音轨、客观测量、报告骨架、
  **从录像里按时间码自动截取关键帧**、把关键帧引用写回报告。

三种用法：

    # 1) 处理已经录好的文件（最常用；OBS 正常录屏即可，不必用 IPC）
    python scripts\\session_runner.py process <文件或目录> --case bug_03

    # 2) 盯着 OBS 的输出目录：一有新的录制文件就自动走完全流程
    python scripts\\session_runner.py watch --dir "%USERPROFILE%\\Videos" --case bug_03

    # 3) 通过 obs_control 的 IPC 让 OBS 截一张当前源的关键帧并归档
    python scripts\\session_runner.py screenshot --case bug_03 --source Cube

关键帧策略：有视频轨的素材，会在**每处检测到的时间间隙**前后各截一帧，外加录制开头
一帧作「环境」；纯音频素材没有视频轨可截，脚本会明确告诉你该补一张截图（或用第 3 种用法）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import audio_qa  # noqa: E402
import check_capture  # noqa: E402
import field_session  # noqa: E402

MEDIA_EXTS = (".mkv", ".mp4", ".mov", ".mka", ".m4a", ".flac", ".wav", ".ogg", ".mp3")
AUDIO_ONLY_EXTS = (".mka", ".m4a", ".flac", ".wav", ".ogg", ".mp3")
ENVIRONMENT_FRAME_AT = 5.0      # 开头这一帧当作「环境 / 地点」证据
FRAME_CONTEXT_S = 0.6           # 间隙前后各偏 0.6 秒截帧，避开静音本身
MAX_KEYFRAMES = 6

# 用例编号 -> 目标目录（相对工作区）；未列出的用例落到 captures/target-game/<game>/<case>
CASE_DIRS = {
    "bug_01": "captures/target-game/{game}/bug_01_concurrency",
    "bug_02": "captures/target-game/{game}/bug_02_surface",
    "bug_03": "captures/target-game/{game}/bug_03_music_transition",
    "bug_04": "captures/target-game/{game}/bug_04_dialogue_sync",
    "bug_05": "captures/target-game/{game}/bug_05_occlusion",
    "control": "captures/perf/baseline",
    "perf_idle": "captures/perf/idle",
    "perf_mid": "captures/perf/mid",
    "perf_stress": "captures/perf/stress",
}
DEFAULT_GAME = "genshin-填版本号"


def target_dir(case_id: str, game: str) -> Path:
    """用例编号 -> 归档目录。兼容矩阵与复测走各自的子目录。"""
    normalized = case_id.strip()
    if normalized in CASE_DIRS:
        return ROOT / CASE_DIRS[normalized].format(game=game)
    lowered = normalized.lower()
    if lowered.startswith("compat_"):
        return ROOT / "captures/target-game" / game / "compat" / normalized
    if lowered.startswith("rc-"):
        return ROOT / "captures/target-game" / game / "retest" / normalized
    return ROOT / "captures/target-game" / game / normalized


def next_index(directory: Path, case_id: str) -> int:
    """目录里已有 raw_*_r## 时自动续号，避免覆盖前一次复现。"""
    highest = 0
    for path in directory.glob(f"raw_*_{case_id}_r*"):
        tail = path.stem.rsplit("_r", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest + 1


def new_clip_name(case_id: str, index: int, suffix: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y%m%d")
    return f"raw_{stamp}_{case_id}_r{index:02d}{suffix}"


def peek_dropouts(measure_path: Path) -> list[dict]:
    """从 field_session 产出的 measure.json 里取间隙（时间码 + 时长）。"""
    if not measure_path.is_file():
        return []
    try:
        files = json.loads(measure_path.read_text(encoding="utf-8"))["files"]
    except Exception:
        return []
    found: list[dict] = []
    for item in files:
        for issue in item["issues"]:
            if issue["check"] == "silence_gap":
                found.extend(issue["detail"].get("gaps", []))
    found.sort(key=lambda gap: gap["time_s"])
    return found


def keyframe_plan(gaps: list[dict], duration_s: float) -> list[tuple[str, float]]:
    """按间隙生成 (标签, 截帧时间点)。纯函数，便于测试。

    每处间隙取两帧：间隙前（现象发生前一刻的画面）与间隙中点（现象本身）。
    没有间隙时只留环境帧——那也是有意义的证据：说明这一段没测出问题。
    """
    plan: list[tuple[str, float]] = [("环境", min(ENVIRONMENT_FRAME_AT, max(0.0, duration_s - 0.1)))]
    used = 1
    for index, gap in enumerate(gaps, 1):
        if used + 2 > MAX_KEYFRAMES:
            break
        start = float(gap["time_s"])
        length_s = float(gap.get("duration_ms", 0)) / 1000.0
        before = max(0.0, start - FRAME_CONTEXT_S)
        middle = start + max(0.05, length_s / 2)
        if middle > max(0.0, duration_s - 0.05):
            middle = max(0.0, duration_s - 0.05)
        plan.append((f"间隙{index:02d}_前", before))
        plan.append((f"间隙{index:02d}_中", middle))
        used += 2
    return plan


def extract_frames(media: Path, plan: list[tuple[str, float]], out_dir: Path,
                   ffmpeg: str) -> list[Path]:
    """按计划截帧；文件没有视频轨时静默返回空列表（脚本会提示补截图）。"""
    written: list[Path] = []
    for label, at in plan:
        target = out_dir / f"keyframe_{label}.png"
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(media),
             "-frames:v", "1", "-q:v", "2", str(target)],
            capture_output=True,
        )
        if done.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            written.append(target)
        elif target.exists():
            target.unlink()
    return written


def append_keyframe_section(skeleton: Path, frames: list[Path], plan: list[tuple[str, float]]) -> None:
    """把关键帧清单写回报告骨架，说明每张对应的录制时间点。"""
    if not frames:
        return
    stamps = {label: at for label, at in plan}
    lines = ["", "## 关键帧（自动截取，对应录制时间点）", ""]
    for frame in frames:
        label = frame.stem.replace("keyframe_", "")
        lines.append(f"- `{frame.name}` —— {stamps.get(label, 0.0):.2f} s：{label}")
    lines += ["", "> 截帧由 `scripts/session_runner.py` 按检测到的时间间隙自动完成；"
              "画面内容仍需你本人确认它是否足以证明「触发动作 / 现象」。", ""]
    with skeleton.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def obs_settings_live() -> dict[str, str]:
    """从运行中的 OBS 读实时参数。

    为什么不读配置文件：OBS 只在周期/退出时把内存状态写回 basic.ini，
    刚用 API 改过的值在文件里还是旧的——读文件会填进过时信息，比不填更糟。
    """
    try:
        import obs_setup  # 与 OBS 通信的客户端（同一仓库）
        client = obs_setup.ObsClient()
    except Exception:
        return {}
    try:
        def param(category: str, name: str) -> str:
            try:
                return client.call("GetProfileParameter", parameterCategory=category,
                                   parameterName=name).get("parameterValue", "") or ""
            except Exception:
                return ""

        mode = param("Output", "Mode") or "Simple"
        advanced = mode.lower().startswith("adv")
        section = "AdvOut" if advanced else "SimpleOutput"
        rec_format = param(section, "RecFormat" if advanced else "RecFormat2")
        video = " / ".join(
            part for part in (
                f"{param('Video', 'BaseCX')}x{param('Video', 'BaseCY')}",
                f"{param('Video', 'FPSCommon')} fps",
                f"{param('Audio', 'SampleRate')} Hz {param('Audio', 'ChannelSetup')}",
                rec_format,
                f"轨道 {param(section, 'RecTracks')}" if param(section, "RecTracks") else "",
            ) if part and not part.startswith("x")
        )

        # 输出设备：从**实际在用的音频源**上取 device_id 再换成可读名称。
        # 读 profile 参数拿不到（该键不在 websocket 暴露的参数里），读源设置最直接。
        device_name = ""
        for source in ("桌面音频", "游戏音频", "麦克风/辅助音频"):
            try:
                settings = client.call("GetInputSettings", inputName=source).get("inputSettings", {})
            except Exception:
                continue
            device_id = str(settings.get("device_id") or "").strip()
            if not device_id:
                continue
            device_name = device_id
            try:
                items = client.call("GetInputPropertiesListPropertyItems", inputName=source,
                                    propertyName="device_id").get("propertyItems", [])
                for item in items:
                    if str(item.get("itemValue")) == device_id:
                        device_name = f"{item.get('itemName')}（{source}）"
                        break
            except Exception:
                pass
            break
        return {"video": video, "device": device_name}
    finally:
        client.close()


def obs_profile_settings() -> dict[str, str]:
    """取 OBS 采集设置；优先实时查询，OBS 没开时才退回读配置文件。"""
    live = obs_settings_live()
    if live.get("video"):
        live.setdefault("profile", "运行中的 OBS")
        return live

    import configparser
    import os

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {}
    root = Path(appdata) / "obs-studio"
    profile = "未命名"
    user_ini = root / "user.ini"
    if user_ini.is_file():
        for line in user_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Profile="):
                profile = line.split("=", 1)[1].strip() or profile
    ini = root / "basic" / "profiles" / profile / "basic.ini"
    if not ini.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(ini, encoding="utf-8")
    except Exception:
        return {}

    def get(section: str, key: str) -> str:
        return (parser.get(section, key, fallback="") or "").strip()

    devices = [
        value.strip() for key, value in (parser.items("Audio") if parser.has_section("Audio") else [])
        if "device" in key.lower() and value.strip()
    ]
    return {
        "profile": profile,
        "video": f"{get('Video', 'BaseCX')}x{get('Video', 'BaseCY')}"
                 f" / {get('Video', 'FPSCommon')} fps"
                 f" / {get('Audio', 'SampleRate')} Hz {get('Audio', 'ChannelSetup')}"
                 f" / {get('SimpleOutput', 'RecFormat2') or get('AdvOut', 'RecFormat')}"
                 f" / 轨道 {get('SimpleOutput', 'RecTracks') or get('AdvOut', 'RecTracks')}",
        "device": "、".join(devices),
    }


def environment_prefill() -> dict[str, str]:
    """能自动确定的「环境」字段（其余必须由执行者本人填）。

    只填机器侧事实：系统版本、CPU/屏幕、OBS 采集设置与输出设备。
    游戏版本、游戏内音频设置、网络状态属于现场信息，脚本无权代填。
    """
    import ctypes
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

    obs = obs_profile_settings()
    if obs.get("video"):
        values["采集设置（OBS 分辨率 / 帧率 / 采样率 / 轨道）"] = (
            f"{obs['video']}（OBS 配置 {obs['profile']}）"
        )
    if obs.get("device"):
        values["输出设备"] = obs["device"]
    return values


def fill_environment(skeleton: Path, values: dict[str, str]) -> list[str]:
    """把自动确定的字段写进骨架的环境表。

    只填**空的格子**：已经写了内容的行一律不覆盖——现场手填的信息比机器推测更可信。
    返回实际填入的字段名列表。
    """
    text = skeleton.read_text(encoding="utf-8")
    lines = text.splitlines()
    filled: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith("|") or line.count("|") < 3:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[1]:
            continue
        for label, value in values.items():
            if cells[0].startswith(label) and value:
                lines[index] = f"| {cells[0]} | {value} |"
                filled.append(cells[0])
                break
    if filled:
        skeleton.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return filled


def process_one(media: Path, case_id: str, game: str, ffmpeg: str | None,
                dropout_min_ms: float, force: bool, quiet: bool = False,
                directory: Path | None = None) -> dict:
    """把单个媒体文件走完：归档 → 自检 → 抽音轨 → 测量 → 骨架 → 关键帧。

    `directory` 可显式指定归档目录（测试用临时目录走同一条代码路径，避免测试往
    真实证据目录里写东西）。
    """
    directory = directory or target_dir(case_id, game)
    directory.mkdir(parents=True, exist_ok=True)
    index = next_index(directory, case_id)
    archived = directory / new_clip_name(case_id, index, media.suffix.lower())
    shutil.copy2(media, archived)

    verdict, problems, hints, measure = check_capture.check(archived, ffmpeg)
    result: dict = {
        "case": case_id,
        "archived": archived,
        "verdict": verdict,
        "problems": problems,
        "hints": hints,
        "measure": measure,
        "audio_only": None,
        "skeleton": None,
        "keyframes": [],
    }
    if verdict == "不可用" and not force:
        result["stopped"] = "采集自检判定不可用，已停在归档这一步（加 --force 可继续）"
        return result

    # 录像抽成无损音轨，便于提交与测量；纯音频素材本就无需抽取
    if archived.suffix.lower() not in AUDIO_ONLY_EXTS and ffmpeg is not None:
        extracted = archived.with_suffix(".mka")
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(archived), "-vn", "-c:a", "copy", str(extracted)],
            capture_output=True,
        )
        result["audio_only"] = extracted if done.returncode == 0 and extracted.is_file() else None

    report = field_session.run_session(
        directory=directory,
        case=case_id,
        exts=["mkv", "mp4", "mov", "mka", "m4a", "flac", "wav"],
        dropout_min_ms=dropout_min_ms,
        do_loudness=ffmpeg is not None,
        quiet=quiet,
    )
    skeleton = report["skeleton"]
    result["skeleton"] = skeleton
    result["env_filled"] = fill_environment(skeleton, environment_prefill())

    if ffmpeg is not None:
        gaps = peek_dropouts(directory / "measure.json")
        durations = [item["duration_s"] for item in report["report"]["files"]]
        duration = max(durations) if durations else 0.0
        plan = keyframe_plan(gaps, duration)
        frames = extract_frames(archived, plan, directory, ffmpeg)
        append_keyframe_section(skeleton, frames, plan)
        result["keyframes"] = frames
        result["gap_count"] = len(gaps)
    else:
        result["gap_count"] = None
    return result


def find_media(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS)


def watch(args: argparse.Namespace) -> int:
    directory: Path = args.dir
    if not directory.is_dir():
        print(f"目录不存在：{directory}", file=sys.stderr)
        return 2
    seen = {p.resolve() for p in directory.iterdir() if p.is_file()}
    print(f"监听 {directory}（已有 {len(seen)} 个文件视为旧文件）；停止录制后会自动处理。Ctrl+C 结束。")
    while True:
        time.sleep(2)
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
                continue
            if path.resolve() in seen:
                continue
            time.sleep(3)  # 等 OBS 写完
            seen.add(path.resolve())
            print(f"\n发现新文件：{path.name}")
            summary = process_one(path, args.case, args.game, audio_qa.find_ffmpeg(None),
                                  args.dropout_min_ms, args.force)
            print_summary(summary)


def print_summary(result: dict) -> None:
    print(f"  用例 {result['case']} → {result['archived']}")
    print(f"  采集自检：{result['verdict']}")
    for item in result["problems"]:
        print(f"    ! {item}")
    if result.get("stopped"):
        print(f"  停止：{result['stopped']}")
        return
    if result.get("audio_only"):
        print(f"  纯音频：{result['audio_only'].name}（无损拷贝，未二次编码）")
    if result.get("gap_count") is not None:
        print(f"  检测到的段内间隙：{result['gap_count']} 处")
    if result.get("keyframes"):
        print(f"  自动截取关键帧：{len(result['keyframes'])} 张 → {', '.join(f.name for f in result['keyframes'][:4])}"
              + (" …" if len(result["keyframes"]) > 4 else ""))
    elif result["archived"].suffix.lower() in AUDIO_ONLY_EXTS:
        print("  提示：这是纯音频素材，没有视频轨可截帧——用 game 内截图键补一张，"
              "或 `session_runner.py screenshot --case " + result["case"] + "` 让 OBS 截一张")
    if result.get("skeleton"):
        print(f"  报告骨架：{result['skeleton'].name}（只需填「一、环境」与「四、结论」）")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="session_runner", description="采集后的自动化流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--case", required=True, help="用例编号，如 bug_03 / compat_bt_c02 / perf_idle")
    common.add_argument("--game", default=DEFAULT_GAME, help=f"目标游戏目录名，默认 {DEFAULT_GAME}")
    common.add_argument("--dropout-min-ms", type=float, default=80.0, help="段内静音间隙下限")
    common.add_argument("--force", action="store_true", help="自检判定不可用时仍继续处理")
    common.add_argument("--quiet", action="store_true")

    process = sub.add_parser("process", parents=[common], help="处理已录好的文件或目录")
    process.add_argument("path", type=Path, help="媒体文件或所在目录")

    watch_parser = sub.add_parser("watch", parents=[common], help="监听 OBS 输出目录，自动处理新文件")
    watch_parser.add_argument("--dir", type=Path, required=True, help="OBS 录制输出目录")

    shot = sub.add_parser("screenshot", help="让 OBS 截一张关键帧并归档（需 obs_control 的 IPC 已就绪）")
    shot.add_argument("--case", required=True)
    shot.add_argument("--game", default=DEFAULT_GAME)
    shot.add_argument("--source", default="Cube", choices=["Cube", "Wwise"], help="obs_evidence.lua 里已配置的源")

    args = parser.parse_args(argv)
    ffmpeg = audio_qa.find_ffmpeg(None)

    if args.command == "watch":
        return watch(args)

    if args.command == "screenshot":
        import obs_control  # 只在需要时导入，避免无 OBS 环境下的多余依赖
        directory = target_dir(args.case, args.game)
        directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(directory.glob("keyframe_手动*.png"))
        out = directory / f"keyframe_手动{len(existing) + 1:02d}.png"
        obs_control.send("screenshot", args.source, out)
        print(f"已归档关键帧：{out}")
        return 0

    if not args.path.exists():
        print(f"路径不存在：{args.path}", file=sys.stderr)
        return 2
    targets = find_media(args.path)
    if not targets:
        print(f"没有找到可处理的媒体文件：{args.path}", file=sys.stderr)
        return 2
    for media in targets:
        print(f"处理：{media}")
        print_summary(process_one(media, args.case, args.game, ffmpeg, args.dropout_min_ms,
                                  args.force, args.quiet))
        print()
    print("下一步：填骨架两节 → 重建交付材料 → git 提交")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
