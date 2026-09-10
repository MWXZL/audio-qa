#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""waapi_probe —— 纯标准库 WAAPI 客户端，把 Wwise Profiler 的数值抓成结构化数据

为什么要有这个东西
    Profiler 截图只能证明「我打开过这个面板」。要证明「注入故障后 Voice Count
    被压平」，需要的是注入前后两组可比的数字，而不是两张要靠肉眼比对的图。
    WAAPI（Wwise Authoring API）能把 Performance Monitor 的全部计数器、
    当前 Voice 列表、Bus 列表读成 JSON，于是「前后对照」变成 diff 而不是看图。

    截图仍然要截——它证明现象在真实 UI 里可见；数值负责证明量级。两者互补。

传输层
    WAAPI 的 HTTP 端口（默认 8080）走 WebSocket 上的 JSON-RPC 2.0，
    路径 /waapi。标准库没有 WebSocket 客户端，所以这里自己实现了
    RFC 6455 的握手与帧编解码——只实现客户端需要的部分：
    掩码发送、分片重组、ping/pong、close。够用且不引第三方依赖。

用法
    python waapi_probe.py info                     连通性与远端连接状态
    python waapi_probe.py perf                     Performance Monitor 全部计数器
    python waapi_probe.py capture --label baseline  抓一份完整快照存盘
    python waapi_probe.py diff a.json b.json       两份快照对照

退出码
    0 成功   1 连不上 / 未连接被测目标   2 脚本自身错误
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
WAAPI_PATH = "/waapi"


# --------------------------------------------------------------------------
# WebSocket 客户端（RFC 6455，只实现客户端侧必需部分）
# --------------------------------------------------------------------------
class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """最小 WebSocket 客户端。

    只支持 text 帧收发，这是 WAAPI 用到的全部。二进制帧、扩展、压缩都不实现——
    实现了也没人用，反而增加出错面。
    """

    OP_CONT = 0x0
    OP_TEXT = 0x1
    OP_BIN = 0x2
    OP_CLOSE = 0x8
    OP_PING = 0x9
    OP_PONG = 0xA

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port, path)

    # -- 握手 --------------------------------------------------------------
    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(request.encode("ascii"))

        # 读到空行为止就是响应头，剩下的字节可能已经是第一个帧，留在缓冲里
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest

        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketError(f"握手失败，服务端返回：{status}")

    # -- 收发 --------------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)              # FIN=1
        length = len(payload)
        # 客户端发送必须掩码（MASK=1），这是 RFC 强制要求，不是可选项
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._recv_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def send_text(self, text: str) -> None:
        self._send_frame(self.OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str:
        """读一条完整消息，自动处理分片与控制帧。"""
        chunks: list[bytes] = []
        opcode_of_message = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == self.OP_PING:
                self._send_frame(self.OP_PONG, payload)
                continue
            if opcode == self.OP_PONG:
                continue
            if opcode == self.OP_CLOSE:
                raise WebSocketError("对端发送 close 帧")
            if opcode in (self.OP_TEXT, self.OP_BIN):
                opcode_of_message = opcode
            chunks.append(payload)
            if fin:
                break
        data = b"".join(chunks)
        if opcode_of_message == self.OP_BIN:
            raise WebSocketError("收到二进制帧，WAAPI 不应使用")
        return data.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(self.OP_CLOSE, b"\x03\xe8")  # 1000 normal
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# WAAPI JSON-RPC
# --------------------------------------------------------------------------
class WaapiError(RuntimeError):
    def __init__(self, uri: str, payload: Any) -> None:
        self.uri = uri
        self.payload = payload
        super().__init__(f"{uri} 调用失败：{json.dumps(payload, ensure_ascii=False)}")


class Waapi:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 10.0) -> None:
        self._ws = WebSocket(host, port, WAAPI_PATH, timeout=timeout)
        self._next_id = 1
        self._session = None
        self._join_realm()

    def _join_realm(self) -> None:
        """Start the WAMP-JSON session used by Wwise WAAPI."""
        self._ws.send_text(json.dumps([1, "realm1", {"roles": {"caller": {}}}]))
        message = json.loads(self._ws.recv_text())
        if not isinstance(message, list) or not message or message[0] != 2:
            raise WebSocketError(f"WAAPI WELCOME 无效：{message!r}")
        self._session = message[1]

    def call(self, uri: str, args: dict[str, Any] | None = None,
             options: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        # WAAPI uses WAMP positional message fields. WAAPI arguments are
        # conventionally carried as the final keyword-argument object.
        request = [48, request_id, options or {}, uri]
        if args is not None:
            request.extend([[], args])
        self._ws.send_text(json.dumps(request, separators=(",", ":")))

        # WAAPI 可能在响应之前推送订阅消息，跳过不带匹配 id 的消息
        want = request_id
        for _ in range(20):
            message = json.loads(self._ws.recv_text())
            if not isinstance(message, list) or len(message) < 2:
                continue
            if message[0] == 50 and message[1] == want:
                if len(message) >= 5 and message[4] is not None:
                    return message[4]
                if len(message) >= 4 and message[3] is not None:
                    return message[3]
                return {}
            if (message[0] == 8 and len(message) >= 5
                    and message[1] == 48 and message[2] == want):
                error = {"error": message[4]}
                if len(message) > 5:
                    error["args"] = message[5]
                if len(message) > 6:
                    error["kwargs"] = message[6]
                raise WaapiError(uri, error)
            if message[0] not in (50, 8):
                continue
        raise WebSocketError(f"{uri}：等不到匹配的响应")

    def try_call(self, uri: str, args: dict[str, Any] | None = None,
                 options: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        """调用但不抛异常，返回 (结果, 错误摘要)。

        探测阶段用得上：不同 Wwise 版本的 profiler 接口有增删，
        一个接口不存在不该让整次抓取失败——记下来继续抓别的。
        """
        try:
            return self.call(uri, args, options), None
        except WaapiError as exc:
            payload = exc.payload
            if isinstance(payload, dict):
                detail = payload.get("message") or json.dumps(payload, ensure_ascii=False)
                uri_detail = payload.get("uri")
                if uri_detail:
                    detail = f"{detail} ({uri_detail})"
            else:
                detail = str(payload)
            return None, detail

    def close(self) -> None:
        self._ws.close()


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def connection_info(client: Waapi) -> dict[str, Any]:
    """被测目标的连接状态。

    这是所有性能数据的前提：没连上被测程序，Performance Monitor 里全是 0，
    而 0 看起来和「一切正常」很像。所以每次采集都把连接状态一并存下来，
    事后才能分辨「真的没有声音」和「根本没连上」。
    """
    out: dict[str, Any] = {}
    info, err = client.try_call("ak.wwise.core.getInfo")
    out["wwise"] = info if err is None else {"error": err}

    status, err = client.try_call("ak.wwise.core.remote.getConnectionStatus")
    out["remote"] = status if err is None else {"error": err}

    consoles, err = client.try_call("ak.wwise.core.remote.getAvailableConsoles")
    out["consoles"] = consoles if err is None else {"error": err}
    return out


def is_connected(conn: dict[str, Any]) -> bool:
    remote = conn.get("remote")
    return bool(isinstance(remote, dict) and remote.get("isConnected"))


def profiler_snapshot(client: Waapi, *, include_voices: bool = True) -> dict[str, Any]:
    """Collect comparable profiler values at the latest capture cursor."""
    snapshot: dict[str, Any] = {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "connection": connection_info(client),
    }
    if not is_connected(snapshot["connection"]):
        return snapshot

    perf, perf_err = client.try_call(
        "ak.wwise.core.profiler.getPerformanceMonitor", {"time": "capture"}
    )
    snapshot["performanceMonitor"] = perf if perf_err is None else {"error": perf_err}
    if include_voices:
        voices, voices_err = client.try_call(
            "ak.wwise.core.profiler.getVoices",
            {"time": "capture"},
            {"return": ["objectName", "gameObjectName", "pipelineID"]},
        )
        snapshot["voices"] = voices if voices_err is None else {"error": voices_err}
    return snapshot


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _performance_map(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = snapshot.get("performanceMonitor", {}).get("return", [])
    return {item["id"]: item.get("value") for item in result if isinstance(item, dict) and "id" in item}


def diff_snapshots(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, numeric diff for two JSON snapshots."""
    before = _performance_map(left)
    after = _performance_map(right)
    counters = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        item: dict[str, Any] = {"before": old, "after": new}
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            item["delta"] = new - old
        counters[key] = item
    return {
        "left": left.get("capturedAt"),
        "right": right.get("capturedAt"),
        "counters": counters,
        "voiceCount": {
            "before": len(left.get("voices", {}).get("return", [])),
            "after": len(right.get("voices", {}).get("return", [])),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="waapi_probe",
        description="通过 WAAPI 抓取 Wwise Profiler 数值",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=10.0)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("info", help="连通性与远端连接状态")
    subs.add_parser("perf", help="读取最新 Performance Monitor 计数器")
    capture = subs.add_parser("capture", help="抓取并保存一份 JSON 快照")
    capture.add_argument("--label", default="capture", help="快照名称")
    capture.add_argument("--out-dir", default="captures", help="输出目录")
    diff = subs.add_parser("diff", help="对比两份 JSON 快照")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)

    args = parser.parse_args(argv)

    try:
        client = Waapi(args.host, args.port, args.timeout)
    except (OSError, WebSocketError) as exc:
        print(f"连不上 WAAPI {args.host}:{args.port}{WAAPI_PATH}：{exc}", file=sys.stderr)
        print("检查：Wwise 是否在运行、User Preferences 里 WAAPI 是否启用、端口是否被占",
              file=sys.stderr)
        return 1

    try:
        if args.command == "info":
            conn = connection_info(client)
            print(json.dumps(conn, ensure_ascii=False, indent=2))
            return 0 if is_connected(conn) else 1
        if args.command == "perf":
            snapshot = profiler_snapshot(client, include_voices=False)
            print(json.dumps(snapshot.get("performanceMonitor", snapshot), ensure_ascii=False, indent=2))
            return 0 if is_connected(snapshot["connection"]) and "error" not in snapshot.get("performanceMonitor", {}) else 1
        if args.command == "capture":
            snapshot = profiler_snapshot(client)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.label).strip("_") or "capture"
            path = Path(args.out_dir) / f"{safe_label}-{timestamp}.json"
            _write_json(path, snapshot)
            print(json.dumps({"file": str(path), "snapshot": snapshot}, ensure_ascii=False, indent=2))
            return 0 if is_connected(snapshot["connection"]) else 1
        if args.command == "diff":
            left = json.loads(args.left.read_text(encoding="utf-8"))
            right = json.loads(args.right.read_text(encoding="utf-8"))
            print(json.dumps(diff_snapshots(left, right), ensure_ascii=False, indent=2))
            return 0
        raise AssertionError(f"unknown command: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError, WaapiError, WebSocketError) as exc:
        print(f"执行 {args.command} 失败：{exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
