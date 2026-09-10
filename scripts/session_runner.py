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
import re
import shutil
import subprocess
import sys
import tempfile
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
                   ffmpeg: str, prefix: str = "keyframe_") -> list[Path]:
    """按计划截帧；文件没有视频轨时静默返回空列表（脚本会提示补截图）。"""
    written: list[Path] = []
    for label, at in plan:
        target = out_dir / f"{prefix}{label}.png"
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


def refresh_keyframes(directory: Path, ffmpeg: str, case_id: str) -> list[Path]:
    """按 measure.json 里每段各自的间隙重新截帧，并重写骨架的关键帧小节。

    为什么要能重跑：间隙列表会随重新测量而变化（例如刚补测了一段），
    帧文件和骨架必须跟着更新，否则报告里会指向不存在的图或过时的时间点。
    命名带上片段名，避免「间隙01 到底是哪一段的」这种歧义。
    """
    measure_path = directory / "measure.json"
    if not measure_path.is_file():
        return []
    report = json.loads(measure_path.read_text(encoding="utf-8"))
    for stale in list(directory.glob("keyframe_*.png")):
        if "_现场记录" not in stale.name and stale.name.count("_") < 3:
            stale.unlink()   # 清掉旧命名（不含片段名）的帧，避免一处间隙两张图

    written: list[Path] = []
    for item in report["files"]:
        stem = Path(item["path"]).stem
        media = next((directory / f"{stem}{ext}" for ext in (".mkv", ".mp4", ".mov", ".mka")
                      if (directory / f"{stem}{ext}").is_file()), None)
        if media is None:
            continue
        gaps = [gap for issue in item["issues"] if issue["check"] == "silence_gap"
                for gap in issue["detail"].get("gaps", [])]
        plan = keyframe_plan(gaps, item["duration_s"])
        written += extract_frames(media, plan, directory, ffmpeg, prefix=f"keyframe_{stem}_")

    if written:
        skeleton = skeleton_in(directory)
        if skeleton is not None:
            rewrite_keyframe_section(skeleton, written)
    return written


def rewrite_keyframe_section(skeleton: Path, frames: list[Path]) -> None:
    """重写骨架的关键帧小节（旧的删掉，写一份新的）。"""
    if not skeleton.is_file():
        return
    text = skeleton.read_text(encoding="utf-8")
    marker = "## 关键帧"
    if marker in text:
        head = text[: text.index(marker)].rstrip()
        lines = [head, "", marker + "（自动截取，对应录制时间点）", ""]
    else:
        lines = [text.rstrip(), "", marker + "（自动截取，对应录制时间点）", ""]
    for frame in frames:
        lines.append(f"- `{frame.name}`")
    lines += ["", "> 文件名里带片段名（`r01`/`r02`/`r03`）与间隙序号；"
              "画面内容仍需你本人确认它是否足以证明「触发动作 / 现象」。", ""]
    skeleton.write_text("\n".join(lines) + "\n", encoding="utf-8")


def skeleton_in(directory: Path) -> Path | None:
    """在目录里找现场记录骨架；名字可能与用例编号不一致（如探索性目录）。"""
    found = sorted(directory.glob("*_现场记录.md"))
    return found[0] if found else None

def frame_size_at(media: Path, at: float, ffmpeg: str, width: int = 160) -> float | None:
    """取某一时刻的小帧体积（KB）——该时刻画面复杂度的代理指标。

    为什么要单独取「间隙中点」的帧：2 fps 的采样间隔是 500 ms，
    而最短的间隙可能只有 240 ms，最近的采样点常常落在间隙之外，
    于是会被系统性判成「画面正常」。在间隙正中取帧才真正对得上。
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "f.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(media),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(target)],
            capture_output=True,
        )
        return target.stat().st_size / 1024 if target.is_file() else None

def screen_activity(media: Path, ffmpeg: str, fps: int = 2, width: int = 160) -> list[tuple[float, float]]:
    """按帧取「画面复杂度」的时间序列（低分辨率帧的 JPEG 体积，单位 KB）。

    用途：给段内间隙做**初步定性**——画面越简单（菜单、加载图、纯色），YSTD 越小；
    正常游戏实机画面 YSTD 明显更大。于是「间隙瞬间画面很简单」更像设计行为
    （菜单/加载/切窗口），「画面仍是实机内容」才更像真的丢声。

    这只是初判，最终定性仍要人看一眼帧——工具给候选，人下结论。
    """
    # 最初想用 signalstats 的 YSTD，但本地 ffmpeg 构建的 metadata 输出只有 pts 行、
    # 取不到统计值（实测）。改用「抽低分辨率帧 + 量 JPEG 体积」：画面越复杂，
    # 同质量压缩后体积越大——一次 ffmpeg 调用抽完所有帧，再量文件大小，可靠且快。
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(media),
             "-vf", f"fps={fps},scale={width}:-2", "-q:v", "4",
             str(out / "f_%05d.jpg")],
            capture_output=True,
        )
        frames = sorted(out.glob("f_*.jpg"))
        return [(index / fps, frame.stat().st_size / 1024) for index, frame in enumerate(frames)]


def classify_gap(time_s: float, activity: list[tuple[float, float]], ratio: float = 0.5,
                 inside_size: float | None = None) -> tuple[str, float | None, float | None]:
    """把一处间隙初步定性：画面简单 → 疑似 design；画面正常 → 疑似丢声。

    阈值**不写死**：用该片段 YSTD 的中位数当基准，低于中位数一半即视为「画面简单」。
    这样不同场景（白天/夜晚、室内/室外）都能自适应，不需要为每个场景调参。
    返回 (初判, 间隙处画面体积, 片段中位体积)。
    """
    if not activity:
        return ("无法判定（未取到画面数据）", None, None)
    values = sorted(value for _, value in activity)
    median = values[len(values) // 2]
    if inside_size is None:
        inside_size = min(activity, key=lambda item: abs(item[0] - time_s))[1]
    verdict = ("疑似 design（画面很简单：菜单/加载/切窗口）" if inside_size < median * ratio
               else "疑似丢声（间隙期间画面仍是正常实机内容）")
    return (verdict, inside_size, median)


def append_screen_section(skeleton: Path, rows: list[dict]) -> None:
    """把画面初判写进骨架（独立小节，标明是自动初判、待人工确认）。"""
    if not skeleton.is_file() or not rows:
        return
    text = skeleton.read_text(encoding="utf-8")
    marker = "## 画面初判（自动，待人工确认）"
    if marker in text:
        text = text[: text.index(marker)].rstrip()
    lines = [text, "", marker, "",
             "| 片段 | 间隙时间 | 时长(ms) | 间隙处画面(KB) | 本片中位(KB) | 自动初判 | 对应帧 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        ystd = "—" if row["ystd"] is None else f"{row['ystd']:.1f}"
        median = "—" if row["median"] is None else f"{row['median']:.1f}"
        lines.append(f"| `{row['take']}` | {row['time_s']:.3f}s | {row['duration_ms']:.0f} | {ystd} |"
                     f" {median} | {row['verdict']} | `{row['frame']}` |")
    lines += ["", "> 初判由「间隙瞬间的画面复杂度 vs 本片中位数」得出，**只是候选**："
              "菜单、加载、剧情转场处的静音属设计行为，必须看一眼上表最后一列的帧再定性。", ""]
    skeleton.write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze_screen(directory: Path, ffmpeg: str, case_id: str) -> list[dict]:
    """对目录内每个片段的每处间隙做画面初判，并写进骨架。"""
    measure_path = directory / "measure.json"
    if not measure_path.is_file():
        return []
    report = json.loads(measure_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for item in report["files"]:
        stem = Path(item["path"]).stem
        media = next((directory / f"{stem}{ext}" for ext in (".mkv", ".mp4", ".mov")
                      if (directory / f"{stem}{ext}").is_file()), None)
        if media is None:
            continue
        gaps = [gap for issue in item["issues"] if issue["check"] == "silence_gap"
                for gap in issue["detail"].get("gaps", [])]
        if not gaps:
            continue
        activity = screen_activity(media, ffmpeg)
        for index, gap in enumerate(gaps, 1):
            inside = frame_size_at(media, float(gap["time_s"]) + float(gap.get("duration_ms", 0)) / 2000.0,
                                   ffmpeg)
            verdict, ystd, median = classify_gap(float(gap["time_s"]), activity, inside_size=inside)
            rows.append({
                "take": stem, "time_s": float(gap["time_s"]),
                "duration_ms": float(gap.get("duration_ms", 0)),
                "verdict": verdict, "ystd": ystd, "median": median,
                "frame": f"keyframe_{stem}_间隙{index:02d}_中.png",
            })
    append_screen_section(skeleton_in(directory), rows)
    return rows

def fill_marks(skeleton: Path | None, marks: dict[str, float]) -> list[str]:
    """把录制时按下的打点时间码写进骨架的「关键时间码」行。

    这是人工时间码的替代方案：边录边按键，比事后靠记忆回看录像准确得多，
    而且不会漏记。没按的项保持空白，绝不猜。
    """
    if skeleton is None or not skeleton.is_file() or not marks:
        return []
    text = skeleton.read_text(encoding="utf-8")
    joined = " · ".join(f"{name} {value:.3f}s" for name, value in marks.items())
    lines = []
    replaced = False
    for line in text.splitlines():
        if line.startswith("- 关键时间码："):
            lines.append(f"- 关键时间码：{joined}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        return []
    skeleton.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(marks)

def process_one(media: Path, case_id: str, game: str, ffmpeg: str | None,
                dropout_min_ms: float, force: bool, quiet: bool = False,
                directory: Path | None = None) -> dict:
    """把单个媒体文件走完：归档 → 自检 → 抽音轨 → 测量 → 骨架 → 关键帧。

    `directory` 可显式指定归档目录（测试用临时目录走同一条代码路径，避免测试往
    真实证据目录里写东西）。
    """
    directory = directory or target_dir(case_id, game)
    # 防呆：把归档目录里的素材再喂给 process 会重复归档（实测踩过，一次生成 r04–r08）。
    # 归档过的文件不需要再处理，直接拒绝，避免把目录越搞越乱。
    if media.parent.resolve() == directory.resolve():
        raise ValueError(f"「{media.name}」已经在归档目录里，不需要再处理（避免重复归档）")
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
        # 画面初判：把「间隙时画面是简单还是实机」先算出来，人只需确认
        result["screen_rows"] = analyze_screen(directory, ffmpeg, case_id)
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


def interpret_record_event(event: dict) -> tuple[str, str]:
    """把 OBS 的 RecordStateChanged 事件翻译成 (动作, 文件路径)。

    注意：OBS 实际发的状态串**带前缀**——`OBS_WEBSOCKET_OUTPUT_STARTED` /
    `..._STOPPING` / `..._STOPPED`。按裸 `STOPPED` 精确匹配会全部落空
    （实测踩过：STARTED 因为有 outputActive 兜底还能认出，STOPPED 直接丢）。

    动作取 "starting" / "started" / "stopping" / "stopped" / "other"；
    只有 "stopped" 是可据以处理文件的终态。
    """
    data = event.get("eventData", {}) if event else {}
    state = str(data.get("outputState", "")).upper().replace("OBS_WEBSOCKET_OUTPUT_", "")
    path = str(data.get("outputPath") or "")
    if state == "STARTED" or (data.get("outputActive") is True and state not in {"STOPPING", "STOPPED"}):
        return ("started", path)
    if state == "STOPPED":
        return ("stopped", path)
    if state == "STOPPING":
        return ("stopping", path)
    if state == "STARTING":
        return ("starting", path)
    return ("other", path)


def run_auto(args: argparse.Namespace) -> int:
    """把「监听」和「录制」绑在一条命令里：按回车开始/停止，停录后自动处理。

    也认你在 OBS 里手点的开始/停止——走的是 OBS 的录制状态事件，
    所以三种触发方式（回车、OBS 按钮、OBS 热键）都会被接管。
    """
    try:
        import msvcrt  # Windows 控制台按键
    except Exception:
        msvcrt = None  # 非交互环境（如被重定向）下退化为「只认 OBS 事件」

    def key_pressed() -> bool:
        if msvcrt is None:
            return False
        try:
            return bool(msvcrt.kbhit())
        except Exception:
            return False

    try:
        import obs_setup
        client = obs_setup.ObsClient(events=obs_setup.ObsClient.EVENT_OUTPUTS)
    except Exception as exc:
        print(f"连不上 obs-websocket：{exc}", file=sys.stderr)
        print("请确认 OBS 已开、工具 → WebSocket 服务器设置里已启用服务器。", file=sys.stderr)
        return 2

    ffmpeg = audio_qa.find_ffmpeg(None)
    takes_total = args.takes
    takes_done = 0
    marks = [m.strip() for m in (args.marks or "").split(",") if m.strip()]
    pressed: dict[str, float] = {}
    macro_result: dict | None = None
    record_started = 0.0
    recording = bool(client.call("GetRecordStatus").get("outputActive"))
    print(f"已连接 OBS（事件订阅已开）。用例：{args.case} · 目标 {takes_total} 段")
    print("按【回车】开始录制 → 在游戏里按拍摄脚本操作 → 再按【回车】停止；"
          "直接点 OBS 的开始/停止也一样能被接管。Ctrl+C 退出。\n")
    if recording:
        print("注意：OBS 当前正在录制，我先接管这一段。\n")

    try:
        while takes_done < takes_total:
            if key_pressed():
                key = msvcrt.getwch()
                if key in ("\r", "\n"):
                    try:
                        if recording:
                            client.call("StopRecord")
                            print("已发送停止录制…")
                        else:
                            client.call("StartRecord")
                            pressed = {}
                            record_started = time.monotonic()
                            print("已开始录制 —— 照拍摄脚本操作；关键动作可按 1..%d 打点：" % len(marks))
                            for i, name in enumerate(marks, 1):
                                print("     %d = %s" % (i, name))
                            print("完事按回车停止。")
                    except Exception as exc:
                        # OBS 可能正处于 STARTING/STOPPING 之间，按键按早了不该让整个会话挂掉
                        print(f"  这一下没生效（{exc}），稍等一秒再按。")
                    time.sleep(0.5)
                elif recording and key in "123456789":
                    # 打点：人工时间码由按键生成，比事后靠记忆回看准确得多
                    index = int(key) - 1
                    if index < len(marks):
                        name = marks[index]
                        stamp = round(time.monotonic() - record_started, 3)
                        pressed[name] = stamp
                        print(f"  ◆ 打点 {key} = {name} @ {stamp:.3f}s")
                    else:
                        print(f"  这一段的打点只定义了 {len(marks)} 个，{key} 无效")
            event = client.poll_event(0.2)
            if event and event.get("eventType") == "RecordStateChanged":
                action, path = interpret_record_event(event)
                if action == "started":
                    recording = True
                    print("● 录制中…")
                    if args.macro:
                        import input_macro  # 同仓库脚本
                        print(f"  执行模拟序列「{args.macro}」——三次用同一序列，刺激才一致…")
                        macro_result = input_macro.run_scenario(args.macro, 0.0, False,
                                                               args.macro_max_seconds)
                        print("  序列结束，自动停止录制…")
                        try:
                            client.call("StopRecord")
                        except Exception as exc:
                            print(f"  自动停止失败：{exc}")
                elif action == "stopped":
                    recording = False
                    print("■ 录制结束，正在处理…")
                    time.sleep(1.2)   # 等 OBS 把文件写完
                    if not path:
                        path = str(client.call("GetRecordStatus").get("outputPath") or "")
                    media = Path(path)
                    if not media.is_file():
                        print(f"  找不到录像文件（{path}），跳过这一段", file=sys.stderr)
                        continue
                    result = process_one(media, args.case, args.game, ffmpeg,
                                         args.dropout_min_ms, args.force, quiet=True)
                    print_summary(result)
                    if result.get("env_filled"):
                        print(f"  环境表已自动填：{'、'.join(result['env_filled'])}")
                    marks_from_macro = (macro_result or {}).get("marks", {})
                    merged = {**marks_from_macro, **pressed}
                    if marks_from_macro:
                        print(f"  序列打点 {len(marks_from_macro)} 个已并入报告")
                    written = fill_marks(result["skeleton"], merged)
                    if written:
                        print(f"  打点时间码已写入报告：{'、'.join(written)}")
                    elif recording is False and marks:
                        print("  提示：这一段没有打点（可按 1..%d 记录关键动作时间码）" % len(marks))
                    takes_done += 1
                    if takes_done < takes_total:
                        print(f"\n第 {takes_done}/{takes_total} 段完成。按回车录下一段。\n")
        print(f"\n{takes_total} 段全部处理完。")
        print("接下来：填骨架的「四、结论」（其余已自动生成）→ "
              "重建交付材料 → git 提交")
        return 0
    except KeyboardInterrupt:
        print(f"\n已退出（本次处理了 {takes_done} 段）。")
        return 0
    finally:
        client.close()


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

    auto_parser = sub.add_parser("auto", parents=[common],
                                 help="绑定录制与处理：按回车开始/停止，停录后自动处理（需 OBS 已开 WebSocket）")
    auto_parser.add_argument("--takes", type=int, default=3, help="本次要录几段（默认 3，对应 r01–r03）")
    auto_parser.add_argument("--macro", help="录制开始后自动执行这个模拟输入序列（scripts\\input_macro.py list 可看列表）")
    auto_parser.add_argument("--macro-max-seconds", type=float, default=120.0)
    auto_parser.add_argument("--marks", default="combat_start,combat_music,combat_end,explore_resume",
                              help="录制中按 1..N 打点，时间码自动写进报告；逗号分隔，默认对应 bug_03 的四个时间码")

    sub.add_parser("analyze", parents=[common],
                   help="对已有测量结果做画面初判（间隙时画面偏简单还是实机内容）并写入骨架")

    sub.add_parser("keyframes", parents=[common],
                   help="按当前 measure.json 重新截取关键帧并重写骨架的关键帧小节")

    shot = sub.add_parser("screenshot", help="让 OBS 截一张关键帧并归档（需 obs_control 的 IPC 已就绪）")
    shot.add_argument("--case", required=True)
    shot.add_argument("--game", default=DEFAULT_GAME)
    shot.add_argument("--source", default="Cube", choices=["Cube", "Wwise"], help="obs_evidence.lua 里已配置的源")

    args = parser.parse_args(argv)
    ffmpeg = audio_qa.find_ffmpeg(None)

    if args.command == "auto":
        return run_auto(args)

    if args.command == "analyze":
        directory = target_dir(args.case, args.game)
        if not directory.is_dir():
            print(f"用例目录不存在：{directory}", file=sys.stderr)
            return 2
        if ffmpeg is None:
            print("需要 ffmpeg 才能取画面数据", file=sys.stderr)
            return 2
        rows = analyze_screen(directory, ffmpeg, args.case)
        print(f"已对 {len(rows)} 处间隙做画面初判：")
        for row in rows:
            print("  %-30s %7.3fs %4.0f ms → %s" % (row["take"], row["time_s"],
                  row["duration_ms"], row["verdict"]))
        print(f"已写入骨架：{skeleton_in(directory)}")
        return 0

    if args.command == "keyframes":
        directory = target_dir(args.case, args.game)
        if not directory.is_dir():
            print(f"用例目录不存在：{directory}", file=sys.stderr)
            return 2
        if ffmpeg is None:
            print("需要 ffmpeg 才能截帧", file=sys.stderr)
            return 2
        frames = refresh_keyframes(directory, ffmpeg, args.case)
        print(f"已截取 {len(frames)} 张关键帧（按片段名与间隙序号命名）")
        for frame in frames:
            print("  ", frame.name)
        print(f"骨架关键帧小节已重写：{directory / (args.case + '_现场记录.md')}")
        return 0

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
        try:
            summary = process_one(media, args.case, args.game, ffmpeg, args.dropout_min_ms,
                                  args.force, args.quiet)
        except ValueError as exc:
            print(f"  跳过：{exc}")
            continue
        print_summary(summary)
        print()
    print("下一步：填骨架两节 → 重建交付材料 → git 提交")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
