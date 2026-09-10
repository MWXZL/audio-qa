#!/usr/bin/env python3
"""Capture Cube's connected Wwise runtime and reversible fault experiments."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from waapi_probe import Waapi, connection_info, is_connected
from scripts.obs_control import send, write_json

EMITTER = 910001
LISTENER = 910002
ATTENUATION = "{9BD0FB0D-C6AC-442B-939A-B6660B2D4EC2}"
FOOTSTEPS = "{586A2ECC-C3FE-4B8A-9C97-0F3B825CDFA2}"
VOICE_FIELDS = ["objectName", "objectGUID", "gameObjectID", "gameObjectName",
                "pipelineID", "playingID", "baseVolume", "isVirtual", "isForcedVirtual"]


def now():
    return datetime.now(timezone.utc).isoformat()


def screenshot(client, path, view=None, command=None):
    if command:
        client.call("ak.wwise.ui.commands.execute", {"command": command})
        time.sleep(0.5)
    result = client.call("ak.wwise.ui.captureScreen", {"viewName": view} if view else {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(result["contentBase64"]))
    return str(path.relative_to(ROOT))


def snapshot(client):
    return {
        "capturedAt": now(),
        "performanceMonitor": client.call("ak.wwise.core.profiler.getPerformanceMonitor", {"time": "capture"}),
        "voices": client.call("ak.wwise.core.profiler.getVoices", {"time": "capture"}, {"return": VOICE_FIELDS}),
    }


def position(client, object_id, x):
    client.call("ak.soundengine.setPosition", {
        "gameObject": object_id,
        "position": {"position": {"x": x, "y": 0, "z": 0},
                     "orientationFront": {"x": 0, "y": 0, "z": 1},
                     "orientationTop": {"x": 0, "y": 1, "z": 0}},
    })


def setup_probe(client, distance):
    existing = client.call("ak.wwise.core.profiler.getGameObjects", {"time": "capture"})["return"]
    if any(item["id"] in (EMITTER, LISTENER) for item in existing):
        raise RuntimeError("Evidence Game Object IDs are already registered")
    client.call("ak.soundengine.registerGameObj", {"gameObject": EMITTER, "name": "AudioQA_Evidence_Emitter"})
    client.call("ak.soundengine.registerGameObj", {"gameObject": LISTENER, "name": "AudioQA_Evidence_Listener"})
    position(client, LISTENER, 0)
    position(client, EMITTER, distance)
    client.call("ak.soundengine.setListeners", {"emitter": EMITTER, "listeners": [LISTENER]})
    client.call("ak.soundengine.setSwitch", {"switchGroup": "Material", "switchState": "Concrete", "gameObject": EMITTER})


def project_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "CubeWwise").rglob("*"))
            if p.suffix.lower() in (".wwu", ".wproj")}


def get_property(client, target, prop):
    return client.call("ak.wwise.core.object.get", {"from": {"id": [target]}},
                       {"return": ["id", "name", "@" + prop]})["return"][0]["@" + prop]


def phase(client, label, event, count, out_dir):
    result = {"label": label, "startedAt": now(), "event": event, "trials": []}
    for trial in range(3):
        client.call("ak.soundengine.stopAll", {"gameObject": EMITTER})
        time.sleep(0.4)
        playing = [client.call("ak.soundengine.postEvent", {"event": event, "gameObject": EMITTER})["return"]
                   for _ in range(count)]
        samples = []
        for _ in range(10):
            time.sleep(0.06)
            samples.append(snapshot(client))
        probe_voices = [[v for v in s["voices"]["return"] if v["gameObjectID"] == EMITTER] for s in samples]
        result["trials"].append({
            "playingIDs": playing, "samples": samples,
            "peakProbeVoices": max(map(len, probe_voices)),
            "peakProbePhysical": max(sum(not v["isVirtual"] for v in vs) for vs in probe_voices),
        })
    result["screenshot"] = screenshot(client, out_dir / (label + ".png"))
    return result


def experiment(client, fault, record):
    target, prop, expected, changed, event, count, distance = (
        (ATTENUATION, "RadiusMax", 70, 5, "SplashIn_Monster", 1, 20)
        if fault == "attenuation" else
        (FOOTSTEPS, "MaxSoundPerInstance", 10, 1, "Foot_Knight", 8, 1)
    )
    out_dir = ROOT / "captures/faults" / fault
    out_dir.mkdir(parents=True, exist_ok=True)
    original = get_property(client, target, prop)
    if original != expected:
        raise RuntimeError(f"Unexpected baseline: {prop}={original}")
    report = {"fault": fault, "startedAt": now(), "connection": connection_info(client),
              "target": target, "property": prop, "baselineValue": original,
              "faultValue": changed, "distance": distance, "gameObject": EMITTER,
              "method": "Live Edit All in connected Cube Profile; banks are not regenerated.",
              "hashesBefore": project_hashes(), "phases": []}
    write_json(out_dir / "evidence.json", report)
    recording = False
    try:
        setup_probe(client, distance)
        client.call("ak.wwise.ui.commands.execute", {"command": "SwitchToLayoutProfiler"})
        if record:
            send("select", "Wwise", None)
            time.sleep(1)
            send("start", "Wwise", None)
            recording = True
        for label, value in (("before", original), ("fault", changed), ("restored", original)):
            client.call("ak.wwise.core.object.setProperty", {"object": target, "property": prop, "value": value})
            time.sleep(0.7)
            if get_property(client, target, prop) != value:
                raise RuntimeError("Property readback does not match")
            result = phase(client, label, event, count, out_dir)
            result["propertyValue"] = value
            report["phases"].append(result)
            write_json(out_dir / "evidence.json", report)
            print(label, "physical peaks:", [t["peakProbePhysical"] for t in result["trials"]], flush=True)
    finally:
        try:
            client.call("ak.wwise.core.object.setProperty", {"object": target, "property": prop, "value": original})
            report["restoredValue"] = get_property(client, target, prop)
            for object_id in (EMITTER, LISTENER):
                client.try_call("ak.soundengine.stopAll", {"gameObject": object_id})
                client.try_call("ak.soundengine.unregisterGameObj", {"gameObject": object_id})
        finally:
            if recording:
                send("stop", "Wwise", ROOT / "captures/recordings" / ("fault_" + fault + ".mkv"))
            report["hashesAfter"] = project_hashes()
            report["projectFilesUnchanged"] = report["hashesBefore"] == report["hashesAfter"]
            report["finishedAt"] = now()
            write_json(out_dir / "evidence.json", report)
    print(str(out_dir / "evidence.json"))


def sync_capture(client):
    out = ROOT / "captures/profiler/05_game_sync_monitor.png"
    client.call("ak.wwise.core.profiler.enableProfilerData", {"dataTypes": [
        {"dataType": "voices", "enable": True},
        {"dataType": "voiceInspector", "enable": True},
        {"dataType": "gameSyncs", "enable": True},
    ]})
    client.call("ak.soundengine.registerGameObj", {"gameObject": EMITTER, "name": "AudioQA_Sync_Probe"})
    try:
        client.call("ak.soundengine.setState", {"state": "Gameplay", "stateGroup": "{5ABE43F7-5E44-4F37-AE07-BC265DDCC34E}"})
        client.call("ak.soundengine.setSwitch", {"switchGroup": "Material", "switchState": "Metal", "gameObject": EMITTER})
        client.call("ak.soundengine.setRTPCValue", {"rtpc": "MusicVolume", "value": 65, "gameObject": EMITTER})
        for event in ("Foot_Player", "Jump", "Fire_IceGem_Player"):
            client.call("ak.soundengine.postEvent", {"event": event, "gameObject": EMITTER})
        time.sleep(0.25)
        client.call("ak.wwise.ui.commands.execute", {"command": "ShowGameSyncMonitor"})
        time.sleep(0.4)
        data = snapshot(client)
        data["gameSyncProbe"] = {"gameObject": EMITTER, "state": "Gameplay", "switch": "Material/Metal", "rtpc": "MusicVolume=65"}
        data["screenshot"] = screenshot(client, out, "GameSyncMonitor")
        write_json(ROOT / "captures/profiler/game-sync-evidence.json", data)
        print(str(out))
    finally:
        client.try_call("ak.soundengine.stopAll", {"gameObject": EMITTER})
        client.try_call("ak.soundengine.unregisterGameObj", {"gameObject": EMITTER})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["screenshot", "snapshot", "sync", "attenuation", "playlimit"])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--view")
    parser.add_argument("--command")
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    client = Waapi(timeout=20)
    try:
        if not is_connected(connection_info(client)):
            raise RuntimeError("A connected Cube Profile runtime is required")
        if args.mode == "screenshot":
            print(screenshot(client, args.out.resolve(), args.view, args.command))
        elif args.mode == "snapshot":
            data = snapshot(client)
            data["connection"] = connection_info(client)
            for key, uri in (("rtpcs", "getRTPCs"), ("streams", "getStreamedMedia"), ("loadedMedia", "getLoadedMedia")):
                data[key] = client.call("ak.wwise.core.profiler." + uri, {"time": "capture"})
            write_json(args.out, data)
            print(str(args.out))
        elif args.mode == "sync":
            sync_capture(client)
        else:
            experiment(client, args.mode, args.record)
    finally:
        client.close()


if __name__ == "__main__":
    main()
