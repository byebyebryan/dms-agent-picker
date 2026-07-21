from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import dms_agent_picker as picker


THREAD_A = "00000000-0000-0000-0000-000000000001"
THREAD_B = "00000000-0000-0000-0000-000000000002"
THREAD_C = "00000000-0000-0000-0000-000000000003"
THREAD_D = "00000000-0000-0000-0000-000000000004"


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
                    {"installed": False, "sessions": []},
                    {"host": "desktop", "active": {}},
                ),
                (
                    picker.HostTarget("laptop.lan"),
                    laptop_threads,
                    {"installed": False, "sessions": []},
                    laptop_active,
                ),
                (
                    picker.HostTarget("laptop-alias"),
                    laptop_threads,
                    {"installed": False, "sessions": []},
                    laptop_active,
                ),
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
                    {"installed": False, "sessions": []},
                    picker.PickerError("probe failed"),
                )
            ],
            limit=20,
        )

        self.assertFalse(result["sessions"][0]["active"])
        self.assertEqual("active", result["errors"][0]["stage"])

    def test_claude_session_survives_codex_list_failure(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("snap.lan"),
                    picker.PickerError("codex unavailable"),
                    {
                        "installed": True,
                        "sessions": [
                            {
                                "id": THREAD_C,
                                "name": "Improve auth flow",
                                "cwd": "/home/test/code/app",
                                "recencyAt": 80,
                                "updatedAt": 80,
                            }
                        ],
                    },
                    {
                        "host": "80H1VV3",
                        "active": {},
                        "claudeActive": {
                            THREAD_C: {"pid": 42, "tmuxSession": "improve-auth-flow"}
                        },
                    },
                )
            ],
            limit=20,
            aliases={"80h1vv3": "snap"},
        )

        self.assertEqual(
            {
                "kind": "claude",
                "id": THREAD_C,
                "name": "Improve auth flow",
                "cwd": "/home/test/code/app",
                "host": "snap",
                "windowHost": "80H1VV3",
                "connectHost": "snap.lan",
                "recencyAt": 80,
                "updatedAt": 80,
                "active": True,
                "tmuxSession": "improve-auth-flow",
            },
            result["sessions"][0],
        )
        self.assertEqual("threads", result["errors"][0]["stage"])

    def test_unavailable_claude_sessions_are_omitted(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [],
                    {"installed": False, "sessions": [{"id": THREAD_C}]},
                    {
                        "host": "desktop",
                        "active": {},
                        "claudeActive": {},
                    },
                )
            ],
            limit=20,
        )

        self.assertEqual([], result["sessions"])

    def test_codex_and_claude_sessions_share_recency_order(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [
                        {
                            "id": THREAD_A,
                            "name": "Codex task",
                            "cwd": "/home/test/code/app",
                            "recencyAt": 10,
                        }
                    ],
                    {
                        "installed": True,
                        "sessions": [
                            {
                                "id": THREAD_C,
                                "name": "Claude task",
                                "cwd": "/home/test/code/app",
                                "recencyAt": 20,
                            }
                        ],
                    },
                    {"host": "desktop", "active": {}, "claudeActive": {}},
                )
            ],
            limit=20,
        )

        self.assertEqual(["claude", "codex"], [item["kind"] for item in result["sessions"]])


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


class ClaudeSessionTest(unittest.TestCase):
    def test_discovers_named_session_from_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)

            config_dir = root / ".claude"
            project_dir = config_dir / "projects" / "-home-test-code-app"
            project_dir.mkdir(parents=True)
            transcript = project_dir / f"{THREAD_C}.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": THREAD_C,
                                "cwd": "/home/test/code/app",
                                "entrypoint": "cli",
                                "message": {"role": "user", "content": "Initial request"},
                            }
                        ),
                        "not valid json",
                        json.dumps(
                            {
                                "type": "custom-title",
                                "sessionId": THREAD_C,
                                "customTitle": "Improve auth flow",
                            }
                        ),
                    ]
                )
                + "\n"
            )
            (project_dir / f"{THREAD_D}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": THREAD_D,
                        "cwd": "/home/test/.claude-mem/observer-sessions",
                        "entrypoint": "sdk-cli",
                        "message": {"role": "user", "content": "Observe sessions"},
                    }
                )
                + "\n"
            )
            (config_dir / "history.jsonl").write_text(
                json.dumps(
                    {
                        "display": "Initial request",
                        "project": "/home/test/code/app",
                        "sessionId": THREAD_C,
                        "timestamp": 2_000_000_000_000,
                    }
                )
                + "\n"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                },
            ):
                result = picker.list_claude_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertTrue(result["installed"])
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual(THREAD_C, result["sessions"][0]["id"])
        self.assertEqual("Improve auth flow", result["sessions"][0]["name"])
        self.assertEqual("/home/test/code/app", result["sessions"][0]["cwd"])
        self.assertEqual(2_000_000_000, result["sessions"][0]["recencyAt"])

    def test_active_snapshot_uses_managed_tmux_session_id(self) -> None:
        with mock.patch.object(picker, "_claude_session_id_for_process", return_value=None):
            active = picker._claude_active_sessions(
                parents={300: 200, 200: 1},
                claude_pids={300},
                pane_sessions={200: "auth-flow"},
                option_sessions={THREAD_C: "auth-flow"},
            )

        self.assertEqual({THREAD_C: {"pid": 300, "tmuxSession": "auth-flow"}}, active)

    def test_extracts_session_id_from_resume_arguments(self) -> None:
        self.assertEqual(
            THREAD_C,
            picker._claude_session_id_from_args(["claude", "--resume", THREAD_C]),
        )
        self.assertEqual(
            THREAD_C,
            picker._claude_session_id_from_args(["claude", f"--session-id={THREAD_C}"]),
        )

    def test_open_reuses_existing_claude_tmux_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "80H1VV3",
                    "active": {},
                    "claudeInstalled": True,
                    "claudeActive": {THREAD_C: {"pid": 42, "tmuxSession": "auth-flow"}},
                },
            ),
            mock.patch.object(picker, "ensure_claude_tmux_session") as ensure,
        ):
            session = picker.resolve_claude_open_target(
                picker.HostTarget("snap.lan"),
                THREAD_C,
                "Improve auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("auth-flow", session)
        ensure.assert_not_called()

    def test_active_claude_outside_tmux_is_not_duplicated(self) -> None:
        with mock.patch.object(
            picker,
            "get_active_snapshot",
            return_value={
                "host": "desktop",
                "active": {},
                "claudeInstalled": True,
                "claudeActive": {THREAD_C: {"pid": 42, "tmuxSession": None}},
            },
        ):
            with self.assertRaisesRegex(picker.PickerError, "outside tmux"):
                picker.resolve_claude_open_target(
                    picker.HostTarget(None), THREAD_C, "Improve auth flow", "/tmp", 1.0
                )

    def test_inactive_claude_creates_managed_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeInstalled": True,
                    "claudeActive": {},
                },
            ),
            mock.patch.object(
                picker, "ensure_claude_tmux_session", return_value="improve-auth-flow"
            ) as ensure,
        ):
            session = picker.resolve_claude_open_target(
                picker.HostTarget(None),
                THREAD_C,
                "Improve auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("improve-auth-flow", session)
        ensure.assert_called_once()

    def test_managed_session_resumes_exact_id_in_recorded_directory(self) -> None:
        script = picker._ensure_claude_session_script(
            THREAD_C, "Improve auth flow", "/home/test/code/app"
        )

        self.assertIn("requested_cwd=/home/test/code/app", script)
        self.assertIn("--resume", script)
        self.assertIn('"$session_id"', script)
        self.assertIn("@claude_session_id", script)


class TmuxNameTest(unittest.TestCase):
    def test_sanitizes_tmux_target_characters(self) -> None:
        self.assertEqual(
            "project-name-test",
            picker._safe_tmux_name("project:name.test", THREAD_A),
        )

    def test_agent_start_waits_for_attached_tmux_client(self) -> None:
        wait_script = picker._tmux_client_wait_script()

        self.assertIn("$TMUX_PANE", wait_script)
        self.assertIn("#{session_attached}", wait_script)
        self.assertIn('exec "$@"', wait_script)

        codex_script = picker._ensure_session_script(THREAD_A, "project", "/home/test/code/project")
        claude_script = picker._ensure_claude_session_script(
            THREAD_C, "project", "/home/test/code/project"
        )
        self.assertIn("codex_command=\"exec sh -c '$wait_script'", codex_script)
        self.assertIn("claude_command=\"exec sh -c '$wait_script'", claude_script)

    def test_remote_attach_quotes_exact_target_for_zsh(self) -> None:
        self.assertEqual(
            "exec tmux -u attach-session -t '=desktop-config'",
            picker._remote_attach_command("desktop-config"),
        )


class TmuxProcessBoundaryTest(unittest.TestCase):
    def test_local_tmux_creation_uses_a_systemd_scope(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value="/usr/bin/systemd-run"):
            command = picker._tmux_creation_command(
                picker.HostTarget(None), "echo local", picker.DEFAULT_SSH_POLICY
            )

        self.assertEqual(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--scope",
                "--collect",
                "--quiet",
                "--",
                "sh",
                "-lc",
                "echo local",
            ],
            command,
        )

    def test_local_tmux_creation_falls_back_without_systemd_run(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value=None):
            command = picker._tmux_creation_command(
                picker.HostTarget(None), "echo local", picker.DEFAULT_SSH_POLICY
            )

        self.assertEqual(["sh", "-lc", "echo local"], command)

    def test_remote_tmux_creation_stays_inside_ssh(self) -> None:
        with (
            mock.patch.object(picker, "_ssh_prefix", return_value=["ssh-prefix"]),
            mock.patch.object(picker.shutil, "which") as which,
        ):
            command = picker._tmux_creation_command(
                picker.HostTarget("remote.lan"),
                "echo remote",
                picker.DEFAULT_SSH_POLICY,
            )

        self.assertEqual(["ssh-prefix", "remote.lan", "sh -lc 'echo remote'"], command)
        which.assert_not_called()


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
                    {"installed": False, "sessions": []},
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
                "list_claude_sessions",
                return_value={"installed": False, "sessions": []},
            ),
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
