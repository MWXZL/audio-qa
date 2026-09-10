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


class ObsClient:
    """极简 obs-websocket v5 客户端：只做请求/响应，够用即可。"""

    def __init__(self, url: str = WS_URL) -> None:
        import websocket  # websocket-client

        self._ws = websocket.create_connection(url, timeout=8)
        hello = json.loads(self._ws.recv())
        if hello.get("op") != 0:
            raise RuntimeError(f"未收到 Hello：{hello}")
        data = hello["d"]
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
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


def configure(client: ObsClient) -> list[str]:
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

    window_value = None
    try:
        probe = client.call("GetInputPropertiesListPropertyItems",
                            inputName=(AUDIO_SOURCE if AUDIO_SOURCE in inputs else VIDEO_SOURCE),
                            propertyName="window")
        window_value = pick_window_item(probe.get("propertyItems", []), PROCESS_PATTERN)
    except Exception as exc:
        notes.append(f"!! 无法枚举窗口列表：{exc}")

    if VIDEO_SOURCE in inputs:
        client.call("SetInputSettings", inputName=VIDEO_SOURCE,
                    inputSettings=VIDEO_SETTINGS_WINDOWED if window_value else VIDEO_SETTINGS,
                    overlay=True)
    else:
        settings = dict(VIDEO_SETTINGS_WINDOWED if window_value else VIDEO_SETTINGS)
        if window_value:
            settings["window"] = window_value
        client.call("CreateInput", sceneName=SCENE_NAME, inputName=VIDEO_SOURCE,
                    inputKind="game_capture", inputSettings=settings, sceneItemEnabled=True)
    notes.append(f"画面源 {VIDEO_SOURCE}：{'指定窗口 ' + window_value if window_value else '捕获任何全屏应用'}")

    audio_settings: dict[str, Any] = {}
    if window_value:
        audio_settings["window"] = window_value
    if AUDIO_SOURCE in inputs:
        if audio_settings:
            client.call("SetInputSettings", inputName=AUDIO_SOURCE,
                        inputSettings=audio_settings, overlay=True)
    else:
        client.call("CreateInput", sceneName=SCENE_NAME, inputName=AUDIO_SOURCE,
                    inputKind="wasapi_process_output_capture", inputSettings=audio_settings,
                    sceneItemEnabled=False)
    try:
        client.call("SetInputAudioTracks", inputName=AUDIO_SOURCE, inputAudioTracks=TRACK_ONE_ONLY)
        notes.append(f"声音源 {AUDIO_SOURCE}：只写轨道 1")
    except Exception as exc:
        notes.append(f"!! 音轨设置失败：{exc}")

    # 桌面音频若不慎存在，静音掉，避免系统通知混进游戏轨道
    for name, item in inputs.items():
        if item.get("inputKind") == "wasapi_output_capture":
            client.call("SetInputMute", inputName=name, inputMuted=True)
            notes.append(f"已静音桌面音频源「{name}」（只用应用程序音频捕获）")
    return notes


def verify(client: ObsClient, seconds: int, keep: bool) -> int:
    before = client.call("GetRecordStatus").get("outputPath", "")
    client.call("StartRecord")
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
    del before
    return 0 if verdict == "可用" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obs_setup", description="远程配置 OBS 并验证")
    parser.add_argument("action", choices=["check", "configure", "verify", "all"])
    parser.add_argument("--seconds", type=int, default=12, help="验证录制时长")
    parser.add_argument("--keep", action="store_true", help="保留验证录像")
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
            for note in configure(client):
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
