#!/usr/bin/env python3
"""通过 obs-websocket 远程配置 OBS：配好采集源并**实录一段验证**。

为什么要远程配：OBS 的场景与源在运行时只存在内存里，直接改配置文件会被退出时回写覆盖；
而 obs-websocket 是 OBS 自带的官方接口，改完立即生效、可查询、可回读验证。

要配到「点一下开始录制就是对的」需要四件事：
1. 画面源：Game Capture（捕获任何全屏应用，无需手选窗口）；
2. 声音源：应用程序音频捕获，进程选游戏——**只抓游戏，不抓系统通知与麦克风**；
3. 音轨：只写轨道 1（单轨、干净，便于客观测量）；
4. 输出：MKV + 48 kHz 立体声 + 录到工作区里的目录，录像一落地就能被 session_runner 接管。

用法：

    python scripts\\obs_setup.py check                     # 连接与现状
    python scripts\\obs_setup.py configure                 # 配置场景/源/输出
    python scripts\\obs_setup.py verify --seconds 12       # 实录一段并判定声音是否正常
    python scripts\\obs_setup.py all                       # 配置 + 验证

前置：OBS 里 工具 → WebSocket 服务器设置 → 勾选「启用 WebSocket 服务器」。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import check_capture  # noqa: E402

WS_URL = "ws://127.0.0.1:4455"
SCENE_NAME = "原神 QA"
VIDEO_SOURCE = "游戏画面"
AUDIO_SOURCE = "游戏音频"
RECORD_DIR = ROOT / "runtime" / "game-recordings"
PROCESS_PATTERN = "yuanshen"

VIDEO_SETTINGS = {
    "capture_mode": "any_fullscreen",   # 不需手选窗口；游戏一进全屏就被抓到
    "capture_cursor": False,
    "capture_audio": False,             # 声音单独走应用程序音频捕获，避免重复
    "allow_hdr": False,
    "rgb_range": 0,
}
VIDEO_SETTINGS_WINDOWED = {
    "capture_mode": "window",
    "capture_cursor": False,
    "capture_audio": False,
    "allow_hdr": False,
    "rgb_range": 0,
}
PROFILE_PARAMS = (
    ("SimpleOutput", "RecFormat2", "mkv"),
    ("SimpleOutput", "FilePath", str(RECORD_DIR).replace("\\", "/")),
    ("SimpleOutput", "RecTracks", "1"),
    ("Video", "BaseCX", "1920"),
    ("Video", "BaseCY", "1080"),
    ("Video", "OutputCX", "1920"),
    ("Video", "OutputCY", "1080"),
    ("Video", "FPSCommon", "30"),
    ("Audio", "SampleRate", "48000"),
    ("Audio", "ChannelSetup", "Stereo"),
)
TRACK_ONE_ONLY = {"1": True, "2": False, "3": False, "4": False, "5": False, "6": False}


def auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 的鉴权串：base64(sha256(base64(sha256(pwd+salt)) + challenge))。"""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


def pick_window_item(items: list[dict[str, Any]], pattern: str) -> str | None:
    """从 OBS 的窗口属性列表里挑出目标进程对应的值（纯函数，便于测试）。

    列表项的 itemValue 形如 `标题:类名:进程.exe`，因此按进程名匹配最稳。
    """
    for item in items:
        value = str(item.get("itemValue", ""))
        if pattern.lower() in value.lower():
            return value
    return None


def looks_fullscreen(rect: tuple[int, int, int, int], screen: tuple[int, int],
                     tolerance: int = 4) -> bool:
    """窗口矩形是否铺满整个屏幕（纯函数，便于测试）。

    这决定画面源用哪种模式：铺满屏幕可用 game_capture 的 any_fullscreen，
    否则必须指定窗口——**用错模式的表现是录出来全黑**，属于最难自查的一类错误。
    """
    left, top, right, bottom = rect
    width, height = screen
    return (right - left) >= width - tolerance and (bottom - top) >= height - tolerance


def game_window_rect(title_pattern: str) -> tuple[int, int, int, int] | None:
    """按标题找可见顶层窗口的矩形（仅 Windows）。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):  # noqa: ANN001
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title_pattern.lower() in buffer.value.lower() and user32.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                found.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def screen_size() -> tuple[int, int]:
    if sys.platform != "win32":
        return (0, 0)
    import ctypes

    user32 = ctypes.windll.user32
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def profile_params_for(mode: str) -> tuple[tuple[str, str, str], ...]:
    """按输出模式（Simple / Advanced）给出正确的参数位置。

    两个模式的录像参数在不同 section：简单模式是 SimpleOutput/RecFormat2，
    高级模式是 AdvOut/RecFormat。设错 section 不会报错，但**不会生效**——
    所以先读模式再决定，而不是假定。
    """
    common = (
        ("Video", "BaseCX", "1920"),
        ("Video", "BaseCY", "1080"),
        ("Video", "OutputCX", "1920"),
        ("Video", "OutputCY", "1080"),
        ("Video", "FPSCommon", "30"),
        ("Audio", "SampleRate", "48000"),
        ("Audio", "ChannelSetup", "Stereo"),
    )
    if mode.lower().startswith("adv"):
        return (
            ("AdvOut", "RecFormat", "mkv"),
            ("AdvOut", "RecFilePath", str(RECORD_DIR).replace("\\", "/")),
            ("AdvOut", "RecTracks", "1"),
        ) + common
    return (
        ("SimpleOutput", "RecFormat2", "mkv"),
        ("SimpleOutput", "FilePath", str(RECORD_DIR).replace("\\", "/")),
        ("SimpleOutput", "RecTracks", "1"),
    ) + common


def source_screenshot_size(client: "ObsClient", source_name: str, width: int = 480) -> int:
    """让 OBS 截一张源画面，返回 PNG 字节数。黑屏/纯色会非常小（几 KB）。"""
    data = client.call("GetSourceScreenshot", sourceName=source_name,
                       imageFormat="png", imageWidth=width)
    return len(base64.b64decode(data.get("imageData", "").split(",", 1)[-1]))


def monitor_value(client: "ObsClient", input_name: str) -> str | None:
    """显示器的属性名在不同版本里是 monitor_id（新版）或 monitor（旧版），两个都试。"""
    for property_name in ("monitor_id", "monitor"):
        try:
            items = client.call("GetInputPropertiesListPropertyItems",
                                inputName=input_name, propertyName=property_name).get("propertyItems", [])
        except Exception:
            continue
        for item in items:
            if item.get("itemEnabled", True) and item.get("itemValue"):
                return str(item["itemValue"])
    return None


def video_candidates(fullscreen: bool) -> list[str]:
    """画面采集方式的尝试顺序（纯函数，便于测试）。

    实测结论（原神 PC）：
    - game_capture / window_capture 的图形钩子会被反作弊挡住，源尺寸为 0x0、截图为黑；
    - monitor_capture 走 DXGI 桌面复制、不注入游戏进程，实测 2560x1440、截图 30 KB 正常。

    所以若非全屏，**先试显示器采集**；全屏时 game_capture 有机会成功，放第一位。
    """
    if fullscreen:
        return ["game_capture", "monitor_capture", "window_capture"]
    return ["monitor_capture", "window_capture", "game_capture"]


def audio_plan(mode: str) -> tuple[str, str]:
    """返回 (要启用的音频源, 要静音的音频源)。

    实测结论（原神 PC，本机 A/B 对照）：
    - 桌面音频 / 系统混音（wasapi_output_capture）：-20.6 LUFS，正常；
    - 应用程序音频捕获（wasapi_process_output_capture）：-57 ~ -68 LUFS，电平异常低。

    系统混音里游戏声音是响的，说明问题出在进程捕获这条通路（多半与反作弊的会话重定向有关）。
    因此默认用系统混音；代价是系统通知会一起进来，靠专注助手与关闭其它发声应用来隔离。
    """
    if mode == "process":
        return ("游戏音频", "桌面音频")
    return ("桌面音频", "游戏音频")


class ObsClient:
    """极简 obs-websocket v5 客户端：请求/响应 + 可选事件订阅。"""

    EVENT_OUTPUTS = 1 << 6   # RecordStateChanged 属于 Outputs 事件类别

    def __init__(self, url: str = WS_URL, events: int = 0) -> None:
        import websocket  # websocket-client

        self._ws = websocket.create_connection(url, timeout=30)
        hello = json.loads(self._ws.recv())
        if hello.get("op") != 0:
            raise RuntimeError(f"未收到 Hello：{hello}")
        data = hello["d"]
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": events}
        auth = data.get("authentication")
        if auth:
            identify["authentication"] = auth_string(
                self._password(), auth["salt"], auth["challenge"]
            )
        self._send_raw({"op": 1, "d": identify})
        identified = json.loads(self._ws.recv())
        if identified.get("op") != 2:
            raise RuntimeError(f"鉴权失败：{identified}")
        self.version = data.get("obsWebSocketVersion", "?")

    def poll_event(self, timeout: float = 0.3) -> dict[str, Any] | None:
        """非阻塞取一条事件；超时返回 None。

        实现要点：**不能用 socket 超时来轮询**——websocket 的帧是分片读的，
        在帧中途超时会把读取状态搞乱，后续事件就会静默丢失（实测：STARTED 收到、
        STOPPED 丢）。改用 select 等「可读」再读，读到的总是完整帧。
        同时只在**没有待响应请求**时调用，避免把请求响应当事件吃掉。
        """
        import select

        sock = getattr(self._ws, "sock", None)
        if sock is None:
            return None
        try:
            ready, _, _ = select.select([sock], [], [], timeout)
        except Exception:
            return None
        if not ready:
            return None
        try:
            while True:
                message = json.loads(self._ws.recv())
                if message.get("op") == 5:
                    return message["d"]
        except Exception:
            return None

    @staticmethod
    def _password() -> str:
        import os

        config = Path(os.environ["APPDATA"]) / "obs-studio/plugin_config/obs-websocket/config.json"
        if config.is_file():
            return json.loads(config.read_text(encoding="utf-8")).get("server_password", "")
        return ""

    def _send_raw(self, payload: dict[str, Any]) -> None:
        self._ws.send(json.dumps(payload))

    def call(self, request_type: str, **request_data: Any) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        self._send_raw({"op": 6, "d": {"requestType": request_type, "requestId": request_id,
                                       "requestData": request_data}})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = json.loads(self._ws.recv())
            if message.get("op") != 7:
                continue
            body = message["d"]
            if body.get("requestId") != request_id:
                continue
            status = body.get("requestStatus", {})
            if not status.get("result"):
                raise RuntimeError(f"{request_type} 失败：{status.get('comment') or status.get('code')}")
            return body.get("responseData", {})
        raise TimeoutError(f"{request_type} 超时")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def configure(client: ObsClient, audio_mode: str = "desktop") -> list[str]:
    notes: list[str] = []
    RECORD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        mode = client.call("GetProfileParameter", parameterCategory="Output",
                           parameterName="Mode").get("parameterValue", "Simple")
    except Exception:
        mode = "Simple"
    notes.append(f"输出模式：{mode}")

    for category, name, value in profile_params_for(mode):
        try:
            client.call("SetProfileParameter", parameterCategory=category,
                        parameterName=name, parameterValue=value)
            notes.append(f"输出参数 {category}/{name} = {value}")
        except Exception as exc:
            notes.append(f"!! 输出参数 {category}/{name} 未设置：{exc}")

    scenes = [item["sceneName"] for item in client.call("GetSceneList").get("scenes", [])]
    if SCENE_NAME not in scenes:
        client.call("CreateScene", sceneName=SCENE_NAME)
        notes.append(f"新建场景「{SCENE_NAME}」")
    client.call("SetCurrentProgramScene", sceneName=SCENE_NAME)
    notes.append(f"当前场景 → {SCENE_NAME}")

    inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}

    # 清掉调试时随手建的临时源，避免它们混进录像画面
    for name in [n for n in inputs if n.startswith(("_临时", "_调试"))]:
        client.call("RemoveInput", inputName=name)
        notes.append(f"清理临时源「{name}」")
        inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}

    # 声音源：先建出来——窗口/进程列表只有在源存在之后才能查询
    if AUDIO_SOURCE not in inputs:
        client.call("CreateInput", sceneName=SCENE_NAME, inputName=AUDIO_SOURCE,
                    inputKind="wasapi_process_output_capture", inputSettings={},
                    sceneItemEnabled=False)
        inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}
        notes.append(f"新建声音源「{AUDIO_SOURCE}」")

    # 从窗口列表里锁定游戏进程；画面源与声音源共用同一个值
    window_value = None
    for probe_name in (AUDIO_SOURCE, VIDEO_SOURCE):
        if probe_name not in inputs:
            continue
        try:
            probe = client.call("GetInputPropertiesListPropertyItems",
                                inputName=probe_name, propertyName="window")
            window_value = pick_window_item(probe.get("propertyItems", []), PROCESS_PATTERN)
            if window_value:
                notes.append(f"从「{probe_name}」的窗口列表锁定目标进程：{window_value}")
                break
        except Exception as exc:
            notes.append(f"（{probe_name} 的窗口列表不可查：{exc}）")
    if window_value is None:
        notes.append("!! 没在窗口列表里找到目标进程：确认游戏在运行且有声音，然后重跑 configure")

    if window_value:
        client.call("SetInputSettings", inputName=AUDIO_SOURCE,
                    inputSettings={"window": window_value}, overlay=True)
        notes.append(f"声音源 {AUDIO_SOURCE} → 只抓该进程音频")
    try:
        client.call("SetInputAudioTracks", inputName=AUDIO_SOURCE, inputAudioTracks=TRACK_ONE_ONLY)
        notes.append(f"声音源 {AUDIO_SOURCE}：只写轨道 1")
    except Exception as exc:
        notes.append(f"!! 音轨设置失败：{exc}")

    # 画面源：按窗口是否铺满屏幕选种类，**建源时就把窗口值带上**，并且**回头验证**。
    # 实测教训：window 为空字符串时 OBS 会把窗口采集源丢掉——API 报成功但源并不存在，
    # 只信返回值就会得到「配好了」的假象，最后录出全黑。
    rect = game_window_rect("原神")
    fullscreen = rect is not None and looks_fullscreen(rect, screen_size())
    candidates = video_candidates(fullscreen)
    notes.append(f"游戏窗口 {rect} / 屏幕 {screen_size()} → 候选捕获方式 {candidates}")

    for kind in candidates:
        if VIDEO_SOURCE in inputs and inputs[VIDEO_SOURCE].get("inputKind") != kind:
            client.call("RemoveInput", inputName=VIDEO_SOURCE)
            inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}
            notes.append(f"移除旧画面源，改用 {kind}")
        if kind == "game_capture":
            settings: dict[str, Any] = dict(VIDEO_SETTINGS)
        elif kind == "window_capture":
            settings = {"window": window_value or "", "method": 2, "priority": 2,
                        "cursor": False, "client_area": False}
        else:
            settings = {"capture_cursor": False}
        try:
            if VIDEO_SOURCE in inputs:
                client.call("SetInputSettings", inputName=VIDEO_SOURCE,
                            inputSettings=settings, overlay=True)
            else:
                client.call("CreateInput", sceneName=SCENE_NAME, inputName=VIDEO_SOURCE,
                            inputKind=kind, inputSettings=settings, sceneItemEnabled=True)
            if kind == "monitor_capture":
                # 源刚建好时属性列表还没就绪（只返回 DUMMY），必须等真实显示器出现再取值
                value = None
                for _ in range(8):
                    value = monitor_value(client, VIDEO_SOURCE)
                    if value:
                        break
                    time.sleep(0.5)
                if value:
                    client.call("SetInputSettings", inputName=VIDEO_SOURCE,
                                inputSettings={"monitor_id": value}, overlay=True)
                    notes.append(f"采集显示器 → {value.split('#')[1] if '#' in value else value}")
                    time.sleep(1.5)   # 等第一次桌面复制出来，否则截图会失败
                else:
                    notes.append("!! 没等到可用的显示器列表")
        except Exception as exc:
            notes.append(f"!! {kind} 设置失败：{exc}")
            continue

        inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}
        if VIDEO_SOURCE not in inputs:
            notes.append(f"!! OBS 没保留 {kind} 源（窗口值为空时会被丢弃），换下一种方式")
            continue
        size = 0
        last_error = ""
        for _attempt in range(4):   # 首帧可能要等一会儿才渲染出来
            try:
                size = source_screenshot_size(client, VIDEO_SOURCE)
                last_error = ""
            except Exception as exc:
                size = 0
                last_error = str(exc)[:60]
            if size > 20000:
                break
            time.sleep(1.0)
        if size <= 20000:
            notes.append(f"!! {kind} 画面仍不可用（截图 {size} 字节{('，' + last_error) if last_error else ''}），换下一种方式")
            try:
                client.call("RemoveInput", inputName=VIDEO_SOURCE)
                inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}
            except Exception:
                pass
            continue
        if size > 20000:
            notes.append(f"画面源 {VIDEO_SOURCE} = {kind} · 源截图 {size // 1024} KB → 有画面 ✓")
            try:
                item_id = client.call("GetSceneItemId", sceneName=SCENE_NAME,
                                      sourceName=VIDEO_SOURCE)["sceneItemId"]
                client.call("SetSceneItemTransform", sceneName=SCENE_NAME, sceneItemId=item_id,
                            sceneItemTransform={"positionX": 0, "positionY": 0,
                                                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                                                "boundsAlignment": 0,
                                                "boundsWidth": 1920, "boundsHeight": 1080})
                notes.append("画面已缩放到 1920x1080 画布（避免被裁切）")
            except Exception as exc:
                notes.append(f"（画面缩放未设置：{exc}）")
            break
        notes.append(f"画面源 {kind} 的源截图只有 {size} 字节（黑屏/纯色），换下一种方式")
        client.call("RemoveInput", inputName=VIDEO_SOURCE)
        inputs = {item["inputName"]: item for item in client.call("GetInputList").get("inputs", [])}

    # 音频通路：按实测结论选（默认系统混音），启用的那个只写轨道 1，另一个静音避免叠声
    enable_name, mute_name = audio_plan(audio_mode)
    if enable_name not in inputs:
        notes.append(f"!! 找不到音频源「{enable_name}」，请确认 OBS 的音频设备设置")
    else:
        client.call("SetInputMute", inputName=enable_name, inputMuted=False)
        try:
            client.call("SetInputAudioTracks", inputName=enable_name, inputAudioTracks=TRACK_ONE_ONLY)
            notes.append(f"音频用「{enable_name}」（{audio_mode}）· 只写轨道 1")
        except Exception as exc:
            notes.append(f"!! 音轨设置失败：{exc}")
    if mute_name in inputs:
        client.call("SetInputMute", inputName=mute_name, inputMuted=True)
        notes.append(f"已静音「{mute_name}」，避免两路叠声")
    notes.append("提示：用系统混音时，请开 Windows 专注助手并关掉其它发声应用（通知会一起被录进来）")
    return notes


def verify(client: ObsClient, seconds: int, keep: bool) -> int:
    # 连录时 OBS 可能还没从上一次停止中缓过来（StartRecord 返回 500），重试几次
    last_error = ""
    for attempt in range(4):
        try:
            client.call("StartRecord")
            last_error = ""
            break
        except Exception as exc:
            last_error = str(exc)[:80]
            time.sleep(2.0)
    if last_error:
        print(f"  无法开始录制：{last_error}", file=sys.stderr)
        return 2
    print(f"  开始录制，请**留在游戏里** {seconds} 秒（有操作更好）…")
    time.sleep(seconds)
    stopped = client.call("StopRecord")
    output = Path(stopped.get("outputPath") or "")
    if not output.is_file():
        print(f"  录制文件没找到：{output}", file=sys.stderr)
        return 2
    print(f"  录像：{output}（{output.stat().st_size / 1024 / 1024:.1f} MB）")
    try:
        import audio_qa
        verdict, problems, hints, measure = check_capture.check(output, audio_qa.find_ffmpeg(None))
    except Exception as exc:
        print(f"  无法分析录像：{exc}", file=sys.stderr)
        return 2
    print(f"  音频格式：{measure.get('codec')} · {measure.get('sample_rate')} Hz · "
          f"{measure.get('channels')} 声道 · {measure.get('duration_s'):.1f} s")
    print(f"  峰值：{measure.get('peak_dbfs'):.1f} dBFS · 响度：{measure.get('lufs')} LUFS")
    print(f"  采集自检结论：{verdict}")
    for item in problems:
        print(f"    ! {item}")
    for item in hints:
        print(f"    → {item}")
    if not keep and verdict == "可用":
        output.unlink()
        print("  已验证，测试录像已删除（加 --keep 保留）")
    elif not keep:
        print(f"  自检未通过，保留录像以便排查：{output}")
    return 0 if verdict == "可用" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obs_setup", description="远程配置 OBS 并验证")
    parser.add_argument("action", choices=["check", "configure", "verify", "all"])
    parser.add_argument("--seconds", type=int, default=12, help="验证录制时长")
    parser.add_argument("--keep", action="store_true", help="保留验证录像")
    parser.add_argument("--audio", choices=["desktop", "process"], default="desktop",
                        help="音频通路：desktop=系统混音（实测正常，默认）；"
                             "process=应用程序音频捕获（本机实测电平异常低）")
    args = parser.parse_args(argv)

    try:
        client = ObsClient()
    except Exception as exc:
        print(f"连不上 obs-websocket（{WS_URL}）：{exc}", file=sys.stderr)
        print("请在 OBS 里：工具 → WebSocket 服务器设置 → 勾选「启用 WebSocket 服务器」，然后重试。",
              file=sys.stderr)
        return 2

    try:
        version = client.call("GetVersion")
        profile = client.call("GetProfileList")
        scenes = client.call("GetSceneList")
        print(f"OBS {version.get('obsVersion')} · websocket {client.version}")
        print(f"配置：{profile.get('currentProfileName')} · 场景集合：{profile.get('currentSceneCollectionName')}")
        print(f"场景：{[s['sceneName'] for s in scenes.get('scenes', [])]}")
        print(f"录制中：{client.call('GetRecordStatus').get('outputActive')}")

        if args.action in {"configure", "all"}:
            print("\n配置：")
            for note in configure(client, args.audio):
                print(f"  {note}")
        if args.action in {"verify", "all"}:
            print("\n验证：")
            code = verify(client, args.seconds, args.keep)
            if code != 0:
                return code
        return 0
    except Exception as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
