#!/usr/bin/env python3
"""Prepare an isolated OBS profile and send commands through its bundled Lua API."""
from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
COMMAND = RUNTIME / "obs-command.json"
STATE = RUNTIME / "obs-state.json"
NAME = "Audio QA Evidence"


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def prepare() -> None:
    config_root = Path(os.environ["APPDATA"]) / "obs-studio"
    scene_path = config_root / "basic/scenes/Audio_QA_Evidence.json"
    profile_path = config_root / "basic/profiles/Audio_QA_Evidence/basic.ini"
    if scene_path.exists() or profile_path.exists():
        raise SystemExit("QA profile already exists; use it without overwriting its settings")
    backups = RUNTIME / "obs-original-settings"
    backups.mkdir(parents=True, exist_ok=True)
    for filename in ("user.ini", "global.ini"):
        source = config_root / filename
        if source.exists() and not (backups / filename).exists():
            shutil.copy2(source, backups / filename)

    recordings = ROOT / "captures/recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    profile = configparser.ConfigParser(interpolation=None)
    profile.optionxform = str
    profile.read_dict({
        "General": {"Name": NAME},
        "Output": {"Mode": "Simple", "FilenameFormatting": "%CCYY-%MM-%DD_%hh-%mm-%ss"},
        "SimpleOutput": {"FilePath": recordings.as_posix(), "RecFormat2": "mkv", "RecEncoder": "x264", "RecQuality": "Small", "RecAudioEncoder": "aac", "RecTracks": "1", "ABitrate": "160", "VBitrate": "3500", "Preset": "veryfast"},
        "Video": {"BaseCX": "1920", "BaseCY": "1080", "OutputCX": "1920", "OutputCY": "1080", "FPSType": "0", "FPSCommon": "30", "ColorFormat": "NV12", "ColorSpace": "709", "ColorRange": "Partial", "ScaleType": "bicubic"},
        "Audio": {"SampleRate": "48000", "ChannelSetup": "Stereo"},
    })
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("w", encoding="utf-8") as handle:
        profile.write(handle, space_around_delimiters=False)
    scenes = ["Audio QA - Wwise", "Audio QA - Cube"]
    write_json(scene_path, {
        "name": NAME,
        "current_scene": scenes[0],
        "current_program_scene": scenes[0],
        "scene_order": [{"name": name} for name in scenes],
        "sources": [{"name": name, "uuid": str(uuid.uuid4()), "id": "scene", "versioned_id": "scene", "settings": {"items": [], "id_counter": 0}, "mixers": 0, "enabled": True, "muted": False} for name in scenes],
        "transitions": [],
        "transition_duration": 200,
        "modules": {"scripts-tool": [{"path": str(ROOT / "scripts/obs_evidence.lua").replace("\\", "/"), "settings": {"command_file": str(COMMAND), "state_file": str(STATE)}}]},
    })
    write_json(COMMAND, {"id": "", "command": "status", "source": "Wwise"})
    print(json.dumps({"profile": str(profile_path), "scene": str(scene_path)}, ensure_ascii=False))


def send(command: str, source: str, output: Path | None) -> None:
    request_id = uuid.uuid4().hex
    write_json(COMMAND, {"id": request_id, "command": command, "source": source})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            state = json.loads(STATE.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if state.get("id") != request_id:
            time.sleep(0.1)
            continue
        if state.get("status") == "error":
            raise SystemExit(state.get("detail", "OBS command failed"))
        if output is not None:
            file = Path(state.get("file", ""))
            if not file.is_file():
                raise SystemExit("OBS did not return a completed artifact")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, output)
            state["archived"] = str(output)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    raise SystemExit(f"OBS request {request_id} timed out; inspect the same command/state before retrying")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "status", "select", "screenshot", "start", "stop"])
    parser.add_argument("--source", choices=["Cube", "Wwise"], default="Wwise")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    else:
        send(args.command, args.source, args.out)


if __name__ == "__main__":
    main()
