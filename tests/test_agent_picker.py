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
        self.assertEqual("laptop", result["sessions"][1]["windowHost"])

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

    def test_claude_workspace_survives_codex_list_failure(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("snap.lan"),
                    picker.PickerError("codex unavailable"),
                    {
                        "host": "80H1VV3",
                        "active": {},
                        "claude": {
                            "installed": True,
                            "running": True,
                            "tmuxSession": "caos",
                        },
                    },
                )
            ],
            limit=20,
            aliases={"80h1vv3": "snap"},
        )

        self.assertEqual([], result["sessions"])
        self.assertEqual(
            {
                "kind": "claude",
                "id": "claude-code",
                "name": "Claude Code",
                "host": "snap",
                "windowHost": "80H1VV3",
                "connectHost": "snap.lan",
                "active": True,
                "tmuxSession": "caos",
            },
            result["workspaces"][0],
        )
        self.assertEqual("threads", result["errors"][0]["stage"])

    def test_unavailable_claude_workspace_is_omitted(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [],
                    {
                        "host": "desktop",
                        "active": {},
                        "claude": {"installed": False, "running": False},
                    },
                )
            ],
            limit=20,
        )

        self.assertEqual([], result["workspaces"])


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


class ClaudeWorkspaceTest(unittest.TestCase):
    def test_snapshot_adopts_existing_claude_tmux_session(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value="/usr/bin/claude"):
            snapshot = picker._claude_snapshot(
                parents={300: 200, 200: 1},
                claude_pids={300},
                pane_sessions={200: "caos"},
            )

        self.assertTrue(snapshot["installed"])
        self.assertTrue(snapshot["running"])
        self.assertEqual("caos", snapshot["tmuxSession"])

    def test_snapshot_prefers_managed_session_when_multiple_exist(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value="/usr/bin/claude"):
            snapshot = picker._claude_snapshot(
                parents={300: 200, 500: 400},
                claude_pids={300, 500},
                pane_sessions={200: "caos", 400: "claude-code"},
            )

        self.assertEqual("claude-code", snapshot["tmuxSession"])
        self.assertEqual(["caos", "claude-code"], snapshot["tmuxSessions"])

    def test_open_reuses_existing_claude_tmux_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "80H1VV3",
                    "active": {},
                    "claude": {
                        "installed": True,
                        "running": True,
                        "tmuxSession": "caos",
                    },
                },
            ),
            mock.patch.object(picker, "ensure_claude_tmux_session") as ensure,
        ):
            session = picker.resolve_claude_open_target(
                picker.HostTarget("snap.lan"), 1.0
            )

        self.assertEqual("caos", session)
        ensure.assert_not_called()

    def test_active_claude_outside_tmux_is_not_duplicated(self) -> None:
        with mock.patch.object(
            picker,
            "get_active_snapshot",
            return_value={
                "host": "desktop",
                "active": {},
                "claude": {"installed": True, "running": True, "tmuxSession": None},
            },
        ):
            with self.assertRaisesRegex(picker.PickerError, "outside tmux"):
                picker.resolve_claude_open_target(picker.HostTarget(None), 1.0)

    def test_inactive_claude_creates_managed_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claude": {"installed": True, "running": False, "tmuxSession": None},
                },
            ),
            mock.patch.object(
                picker, "ensure_claude_tmux_session", return_value="claude-code"
            ) as ensure,
        ):
            session = picker.resolve_claude_open_target(picker.HostTarget(None), 1.0)

        self.assertEqual("claude-code", session)
        ensure.assert_called_once()

    def test_managed_session_starts_in_code_and_opens_resume_picker(self) -> None:
        script = picker._ensure_claude_session_script()

        self.assertIn("workspace_cwd=$HOME/code", script)
        self.assertIn('--resume', script)
        self.assertIn('@agent_workspace claude', script)


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


class HostConfigTest(unittest.TestCase):
    def test_ssh_policy_builds_noninteractive_bounded_prefix(self) -> None:
        self.assertEqual(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=2",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "LogLevel=ERROR",
            ],
            picker._ssh_prefix(picker.SshPolicy()),
        )

    def test_parses_case_insensitive_alias_source(self) -> None:
        self.assertEqual(
            {"80h1vv3": "snap"},
            picker.parse_host_aliases(["80H1VV3=snap"]),
        )

    def test_rejects_invalid_alias(self) -> None:
        with self.assertRaisesRegex(picker.PickerError, "expected source=display"):
            picker.parse_host_aliases(["snap"])

    def test_alias_changes_display_host_but_preserves_window_host(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("snap.lan"),
                    [{"id": THREAD_A, "name": "dotfiles", "cwd": "/home/test"}],
                    {"host": "80H1VV3", "active": {}},
                )
            ],
            limit=20,
            aliases={"80h1vv3": "snap"},
        )

        self.assertEqual("snap", result["sessions"][0]["host"])
        self.assertEqual("80H1VV3", result["sessions"][0]["windowHost"])

    def test_shared_host_list_skips_local_alias(self) -> None:
        with (
            mock.patch.object(picker.socket, "gethostname", return_value="80H1VV3"),
            mock.patch.object(picker, "list_codex_threads", return_value=[]),
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={"host": "unused", "active": {}},
            ) as active,
        ):
            picker.aggregate_sessions(
                ["starship.lan", "snap.lan", "carbon.lan"],
                limit=20,
                timeout=1.0,
                aliases={"80h1vv3": "snap"},
            )

        queried_hosts = {call.args[0].key for call in active.call_args_list}
        self.assertEqual({"local", "starship.lan", "carbon.lan"}, queried_hosts)


if __name__ == "__main__":
    unittest.main()
