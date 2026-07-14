from __future__ import annotations

import unittest
from unittest import mock

import dms_agent_picker as picker


THREAD_A = "00000000-0000-0000-0000-000000000001"
THREAD_B = "00000000-0000-0000-0000-000000000002"


class MergeHostResultsTest(unittest.TestCase):
    def test_sorts_by_recency_marks_active_and_deduplicates_aliases(self) -> None:
        laptop_threads = [
            {
                "id": THREAD_A,
                "name": "cubey",
                "cwd": "/home/test/code/cubey",
                "recencyAt": 20,
                "updatedAt": 30,
            }
        ]
        local_threads = [
            {
                "id": THREAD_B,
                "name": "system",
                "cwd": "/home/test",
                "recencyAt": 40,
                "updatedAt": 50,
            }
        ]
        laptop_active = {
            "host": "laptop",
            "active": {THREAD_A: {"pid": 42, "tmuxSession": "cubey"}},
        }

        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    local_threads,
                    {"host": "desktop", "active": {}},
                ),
                (picker.HostTarget("laptop.lan"), laptop_threads, laptop_active),
                (picker.HostTarget("laptop-alias"), laptop_threads, laptop_active),
            ],
            limit=20,
        )

        self.assertEqual([THREAD_B, THREAD_A], [item["id"] for item in result["sessions"]])
        self.assertTrue(result["sessions"][1]["active"])
        self.assertEqual("cubey", result["sessions"][1]["tmuxSession"])
        self.assertEqual("laptop.lan", result["sessions"][1]["connectHost"])

    def test_remote_active_failure_keeps_sessions_idle(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("laptop"),
                    [
                        {
                            "id": THREAD_A,
                            "name": "codex",
                            "cwd": "/home/test",
                            "recencyAt": 5,
                            "updatedAt": 7,
                        }
                    ],
                    picker.PickerError("probe failed"),
                )
            ],
            limit=20,
        )

        self.assertFalse(result["sessions"][0]["active"])
        self.assertEqual("active", result["errors"][0]["stage"])


class OpenTargetTest(unittest.TestCase):
    def test_active_tmux_session_is_reused(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {THREAD_A: {"pid": 10, "tmuxSession": "cubey"}},
                },
            ),
            mock.patch.object(picker, "ensure_tmux_session") as ensure,
        ):
            session = picker.resolve_open_target(
                picker.HostTarget(None), THREAD_A, "cubey", "/tmp", 1.0
            )

        self.assertEqual("cubey", session)
        ensure.assert_not_called()

    def test_active_session_outside_tmux_is_not_duplicated(self) -> None:
        with mock.patch.object(
            picker,
            "get_active_snapshot",
            return_value={
                "host": "desktop",
                "active": {THREAD_A: {"pid": 10, "tmuxSession": None}},
            },
        ):
            with self.assertRaisesRegex(picker.PickerError, "outside tmux"):
                picker.resolve_open_target(picker.HostTarget(None), THREAD_A, "cubey", "/tmp", 1.0)


class TmuxNameTest(unittest.TestCase):
    def test_sanitizes_tmux_target_characters(self) -> None:
        self.assertEqual(
            "project-name-test",
            picker._safe_tmux_name("project:name.test", THREAD_A),
        )

    def test_remote_attach_quotes_exact_target_for_zsh(self) -> None:
        self.assertEqual(
            "exec tmux -u attach-session -t '=desktop-config'",
            picker._remote_attach_command("desktop-config"),
        )


class NiriWindowTest(unittest.TestCase):
    def test_matches_exact_session_and_short_hostname(self) -> None:
        windows = [
            {
                "id": 42,
                "title": "desktop-config:0 codex | bryan @ 80H1VV3",
            }
        ]

        self.assertEqual(
            42,
            picker._matching_niri_window_id(windows, "desktop-config", "80h1vv3.lan"),
        )

    def test_rejects_same_session_on_another_host(self) -> None:
        windows = [
            {
                "id": 42,
                "title": "cubey:0 codex | cubey @ starship",
            }
        ]

        self.assertIsNone(picker._matching_niri_window_id(windows, "cubey", "carbon"))


if __name__ == "__main__":
    unittest.main()
