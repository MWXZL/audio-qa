"""OBS 远程配置脚本（scripts/obs_setup.py）的纯函数验证。

真正的连接与录制只能在 OBS 运行且 WebSocket 已启用时验证（由执行者手工跑 `check`），
这里只验证那些**出错就会配错源**的纯逻辑：鉴权串算法、窗口目标匹配。
"""
from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import obs_setup  # noqa: E402


class AuthStringTestCase(unittest.TestCase):
    def test_matches_obs_websocket_v5_algorithm(self) -> None:
        """按文档算法独立算一遍，防止实现被改错导致连不上。"""
        password, salt, challenge = "jP00FFbNNSlWWXXA", "salt123", "challenge456"
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode("utf-8")).digest()
        ).decode("ascii")
        expected = base64.b64encode(
            hashlib.sha256((secret + challenge).encode("utf-8")).digest()
        ).decode("ascii")
        self.assertEqual(obs_setup.auth_string(password, salt, challenge), expected)
        self.assertEqual(len(obs_setup.auth_string(password, salt, challenge)), 44)

    def test_challenge_changes_result(self) -> None:
        first = obs_setup.auth_string("p", "s", "c1")
        second = obs_setup.auth_string("p", "s", "c2")
        self.assertNotEqual(first, second)


class PickWindowItemTestCase(unittest.TestCase):
    ITEMS = [
        {"itemName": "Program Manager", "itemValue": "Program Manager:Progman:explorer.exe"},
        {"itemName": "原神", "itemValue": "原神:UnityWndClass:YuanShen.exe"},
        {"itemName": "OBS 32.2.2", "itemValue": "OBS 32.2.2:Qt5152QWindowIcon:obs64.exe"},
    ]

    def test_picks_the_game_process(self) -> None:
        self.assertEqual(
            obs_setup.pick_window_item(self.ITEMS, "yuanshen"),
            "原神:UnityWndClass:YuanShen.exe",
        )

    def test_is_case_insensitive(self) -> None:
        self.assertIsNotNone(obs_setup.pick_window_item(self.ITEMS, "YuanShen"))

    def test_returns_none_when_absent(self) -> None:
        self.assertIsNone(obs_setup.pick_window_item(self.ITEMS, "genshinimpact"))

    def test_never_picks_obs_itself(self) -> None:
        """配错源最糟的结果是录到自己——匹配串不能命中 OBS 窗口。"""
        picked = obs_setup.pick_window_item(self.ITEMS, "yuanshen")
        self.assertNotIn("obs64.exe", picked or "")


class FullscreenDetectionTestCase(unittest.TestCase):
    def test_window_covering_screen_is_fullscreen(self) -> None:
        self.assertTrue(obs_setup.looks_fullscreen((0, 0, 1920, 1080), (1920, 1080)))

    def test_borderless_window_with_tiny_gap_still_counts(self) -> None:
        self.assertTrue(obs_setup.looks_fullscreen((0, 0, 1919, 1079), (1920, 1080)))

    def test_windowed_game_is_not_fullscreen(self) -> None:
        """1600x900 的窗口在 1920x1080 屏幕上不算全屏——此时必须用窗口捕获，
        否则 game_capture 的 any_fullscreen 会录到黑屏。"""
        self.assertFalse(obs_setup.looks_fullscreen((100, 50, 1700, 950), (1920, 1080)))


class VideoCandidatesTestCase(unittest.TestCase):
    def test_non_fullscreen_tries_display_capture_first(self) -> None:
        """原神窗口/游戏采集被反作弊挡住，非全屏时必须先试显示器采集。"""
        self.assertEqual(obs_setup.video_candidates(False)[0], "monitor_capture")

    def test_fullscreen_tries_game_capture_first(self) -> None:
        self.assertEqual(obs_setup.video_candidates(True)[0], "game_capture")

    def test_display_capture_is_always_available_as_fallback(self) -> None:
        self.assertIn("monitor_capture", obs_setup.video_candidates(True))
        self.assertIn("monitor_capture", obs_setup.video_candidates(False))


class SettingsShapeTestCase(unittest.TestCase):
    def test_video_source_does_not_double_capture_audio(self) -> None:
        """画面源必须关掉自己的音频采集，否则声音会重复。"""
        self.assertFalse(obs_setup.VIDEO_SETTINGS["capture_audio"])
        self.assertFalse(obs_setup.VIDEO_SETTINGS_WINDOWED["capture_audio"])

    def test_fullscreen_mode_needs_no_window(self) -> None:
        self.assertEqual(obs_setup.VIDEO_SETTINGS["capture_mode"], "any_fullscreen")
        self.assertNotIn("window", obs_setup.VIDEO_SETTINGS)

    def test_only_track_one_is_written(self) -> None:
        self.assertTrue(obs_setup.TRACK_ONE_ONLY["1"])
        self.assertFalse(any(v for k, v in obs_setup.TRACK_ONE_ONLY.items() if k != "1"))

    def test_simple_output_params_target_mkv_and_48k(self) -> None:
        params = {(c, n): v for c, n, v in obs_setup.profile_params_for("Simple")}
        self.assertEqual(params[("SimpleOutput", "RecFormat2")], "mkv")
        self.assertEqual(params[("Audio", "SampleRate")], "48000")
        self.assertEqual(params[("Audio", "ChannelSetup")], "Stereo")
        self.assertEqual(params[("SimpleOutput", "RecTracks")], "1")

    def test_advanced_output_uses_advout_section(self) -> None:
        """高级输出模式下把参数写到 SimpleOutput 不会生效，必须写到 AdvOut。"""
        params = {(c, n): v for c, n, v in obs_setup.profile_params_for("Advanced")}
        self.assertEqual(params[("AdvOut", "RecFormat")], "mkv")
        self.assertNotIn(("SimpleOutput", "RecFormat2"), params)
        self.assertEqual(params[("Audio", "SampleRate")], "48000")


if __name__ == "__main__":
    unittest.main()
