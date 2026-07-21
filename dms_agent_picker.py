#!/usr/bin/env python3
"""Aggregate and open Codex and Claude Code sessions from local and SSH hosts."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT = 4.0
DEFAULT_SSH_CONNECT_TIMEOUT = 2
DEFAULT_SSH_CONNECTION_ATTEMPTS = 1
VERSION = "0.3.2"
UUID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


class PickerError(RuntimeError):
    """An expected failure that can be reported without a traceback."""


@dataclass(frozen=True)
class HostTarget:
    connect_host: str | None

    @property
    def key(self) -> str:
        return self.connect_host or "local"


@dataclass(frozen=True)
class SshPolicy:
    connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT
    connection_attempts: int = DEFAULT_SSH_CONNECTION_ATTEMPTS


DEFAULT_SSH_POLICY = SshPolicy()


def _short_hostname(host: str) -> str:
    return host.rstrip(".").split(".", 1)[0].casefold()


def parse_host_aliases(values: Sequence[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for value in values:
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            source, separator, display = entry.partition("=")
            source = source.strip()
            display = display.strip()
            if not separator or not source or not display:
                raise PickerError(f"invalid host alias {entry!r}; expected source=display")
            aliases[_short_hostname(source)] = display
    return aliases


CLAUDE_SESSION_PROBE = r"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

uuid_pattern = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
limit = max(1, int(sys.argv[1]))
requested_id = sys.argv[2].lower() if len(sys.argv) > 2 and sys.argv[2] else None
installed = shutil.which("claude") is not None
config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
projects_dir = config_dir / "projects"
if not installed:
    print(json.dumps({"installed": False, "sessions": []}, separators=(",", ":")))
    raise SystemExit(0)


def clean_text(value):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[:117] + "..." if len(value) > 120 else value


def message_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return clean_text(content)
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return clean_text(" ".join(parts))


def timestamp_seconds(value):
    if not isinstance(value, (int, float)):
        return 0
    return int(value / 1000 if value > 100000000000 else value)


def read_json_lines(path, maximum_bytes=2097152):
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size <= maximum_bytes:
                data = stream.read()
            else:
                edge = maximum_bytes // 2
                head = stream.read(edge)
                stream.seek(-edge, os.SEEK_END)
                tail = stream.read(edge)
                if b"\n" in head:
                    head = head.rsplit(b"\n", 1)[0]
                if b"\n" in tail:
                    tail = tail.split(b"\n", 1)[1]
                data = head + b"\n" + tail
    except (OSError, ValueError):
        return []

    entries = []
    for raw_line in data.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


transcripts = []
if projects_dir.is_dir():
    for path in projects_dir.glob("*/*.jsonl"):
        session_id = path.stem.lower()
        if not uuid_pattern.fullmatch(session_id):
            continue
        if requested_id is not None and session_id != requested_id:
            continue
        try:
            modified = int(path.stat().st_mtime)
        except OSError:
            continue
        transcripts.append((modified, session_id, path))
transcripts.sort(key=lambda item: (item[0], item[1]), reverse=True)

candidate_ids = {item[1] for item in transcripts}
history = {}
history_path = config_dir / "history.jsonl"
if candidate_ids and history_path.is_file():
    try:
        stream = history_path.open(encoding="utf-8", errors="replace")
    except OSError:
        stream = None
    if stream is not None:
        with stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(entry.get("sessionId") or "").lower()
                if session_id not in candidate_ids:
                    continue
                item = history.setdefault(session_id, {"name": "", "cwd": "", "recencyAt": 0})
                if not item["name"]:
                    item["name"] = clean_text(entry.get("display"))
                if not item["cwd"] and isinstance(entry.get("project"), str):
                    item["cwd"] = entry["project"]
                item["recencyAt"] = max(
                    item["recencyAt"], timestamp_seconds(entry.get("timestamp"))
                )

sessions = []
for modified, session_id, path in transcripts:
    history_item = history.get(session_id, {})
    cwd = str(history_item.get("cwd") or "")
    first_prompt = ""
    custom_title = ""
    entrypoint = ""
    for entry in read_json_lines(path):
        if not cwd and isinstance(entry.get("cwd"), str):
            cwd = entry["cwd"]
        if not entrypoint and isinstance(entry.get("entrypoint"), str):
            entrypoint = entry["entrypoint"]
        if entry.get("type") == "custom-title":
            title = clean_text(entry.get("customTitle"))
            if title:
                custom_title = title
        if (
            not first_prompt
            and entry.get("type") == "user"
            and not entry.get("isMeta")
        ):
            first_prompt = message_text(entry.get("message"))
    if entrypoint == "sdk-cli":
        continue
    name = custom_title or str(history_item.get("name") or "") or first_prompt
    if not name:
        name = Path(cwd).name if cwd else "Claude " + session_id[:8]
    sessions.append(
        {
            "id": session_id,
            "name": name,
            "cwd": cwd,
            "recencyAt": max(modified, int(history_item.get("recencyAt") or 0)),
            "updatedAt": modified,
        }
    )
    if len(sessions) >= limit:
        break

print(json.dumps({"installed": installed, "sessions": sessions}, separators=(",", ":")))
"""


class AppServerClient:
    """Small JSON-RPC client for one Codex app-server process."""

    def __init__(self, command: Sequence[str], timeout: float) -> None:
        self.timeout = timeout
        self._next_id = 1
        self._buffer = b""
        self._stderr = bytearray()
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise PickerError("failed to open Codex app-server pipes")

        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        if self.process.stderr is not None:
            self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")

    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "dms-agent-picker",
                    "title": "DMS Agent Picker",
                    "version": VERSION,
                }
            },
        )
        self.notify("initialized", {})

    def call(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        return self._read_response(request_id)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise PickerError("Codex app-server stdin is closed")
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise PickerError(self._process_error("Codex app-server exited")) from exc

    def _read_response(self, request_id: int) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    detail = message["error"]
                    if isinstance(detail, dict):
                        detail = detail.get("message", json.dumps(detail))
                    raise PickerError(f"Codex app-server error: {detail}")
                return message.get("result")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PickerError(self._process_error("Codex app-server timed out"))

            events = self._selector.select(remaining)
            if not events:
                raise PickerError(self._process_error("Codex app-server timed out"))

            for key, _ in events:
                stream = key.fileobj
                file_descriptor = stream if isinstance(stream, int) else stream.fileno()
                chunk = os.read(file_descriptor, 65536)
                if not chunk:
                    try:
                        self._selector.unregister(stream)
                    except KeyError:
                        pass
                    if key.data == "stdout":
                        raise PickerError(self._process_error("Codex app-server closed stdout"))
                    continue
                if key.data == "stdout":
                    self._buffer += chunk
                else:
                    self._stderr.extend(chunk)

    def _process_error(self, prefix: str) -> str:
        stderr = self._stderr.decode(errors="replace").strip()
        return f"{prefix}: {stderr}" if stderr else prefix

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=0.5)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        finally:
            self._selector.close()

    def __enter__(self) -> AppServerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _ssh_prefix(policy: SshPolicy) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={policy.connect_timeout}",
        "-o",
        f"ConnectionAttempts={policy.connection_attempts}",
        "-o",
        "LogLevel=ERROR",
    ]


def _local_scope_command(command: list[str]) -> list[str]:
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        return command
    return [
        systemd_run,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--",
    ] + command


def _tmux_creation_command(
    target: HostTarget,
    script: str,
    ssh_policy: SshPolicy,
) -> list[str]:
    if target.connect_host is not None:
        return _ssh_prefix(ssh_policy) + [
            target.connect_host,
            "sh -lc " + shlex.quote(script),
        ]
    return _local_scope_command(["sh", "-lc", script])


def _app_server_command(target: HostTarget, ssh_policy: SshPolicy) -> list[str]:
    if target.connect_host is None:
        codex = shutil.which("codex")
        if not codex:
            raise PickerError("codex is not installed")
        return [codex, "app-server", "--stdio"]
    return _ssh_prefix(ssh_policy) + [target.connect_host, "codex app-server --stdio"]


def list_codex_threads(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> list[dict[str, Any]]:
    with AppServerClient(_app_server_command(target, ssh_policy), timeout) as client:
        client.initialize()
        result = client.call(
            "thread/list",
            {
                "archived": False,
                "limit": limit,
                "sortDirection": "desc",
                "sortKey": "recency_at",
                "sourceKinds": ["cli"],
                "useStateDbOnly": True,
            },
        )
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise PickerError("Codex app-server returned an invalid thread list")
    return [item for item in result["data"] if isinstance(item, dict)]


def read_codex_thread(
    target: HostTarget,
    thread_id: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    with AppServerClient(_app_server_command(target, ssh_policy), timeout) as client:
        client.initialize()
        result = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        raise PickerError(f"Codex session {thread_id} was not found")
    return thread


def query_claude_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
    session_id: str | None = None,
) -> dict[str, Any]:
    arguments = [str(limit), session_id or ""]
    if target.connect_host is None:
        command = [sys.executable, "-"] + arguments
    else:
        remote_command = "python3 - " + " ".join(shlex.quote(value) for value in arguments)
        command = _ssh_prefix(ssh_policy) + [target.connect_host, remote_command]
    try:
        result = subprocess.run(
            command,
            input=CLAUDE_SESSION_PROBE,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"Claude session query timed out on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"Claude session query failed on {target.key}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PickerError(f"Claude session query returned invalid JSON on {target.key}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise PickerError(f"Claude session query returned invalid data on {target.key}")
    return payload


def list_claude_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    return query_claude_sessions(target, limit, timeout, ssh_policy)


def read_claude_session(
    target: HostTarget,
    session_id: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    result = query_claude_sessions(target, 1, timeout, ssh_policy, session_id)
    if not result.get("installed"):
        raise PickerError(f"Claude Code is not installed on {target.key}")
    sessions = result["sessions"]
    if not sessions or not isinstance(sessions[0], dict):
        raise PickerError(f"Claude session {session_id} was not found on {target.key}")
    return sessions[0]


def _process_table() -> tuple[dict[int, int], set[int], set[int]]:
    result = subprocess.run(
        ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    parents: dict[int, int] = {}
    codex_pids: set[int] = set()
    claude_pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid, parent = int(parts[0]), int(parts[1])
        parents[pid] = parent
        command = parts[2].lower()
        if "codex" in command:
            codex_pids.add(pid)
        if "claude" in command:
            claude_pids.add(pid)
    return parents, codex_pids, claude_pids


def _thread_id_for_process(pid: int) -> str | None:
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError):
        return None
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if "rollout-" not in target or ".jsonl" not in target:
            continue
        match = UUID_PATTERN.search(target)
        if match:
            return match.group(1).lower()
    return None


def _tmux_panes() -> tuple[dict[int, str], dict[str, str], dict[str, str]]:
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}\t#{@claude_session_id}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}, {}, {}

    pane_sessions: dict[int, str] = {}
    codex_option_sessions: dict[str, str] = {}
    claude_option_sessions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) >= 3 and UUID_PATTERN.fullmatch(parts[2]):
            codex_option_sessions[parts[2].lower()] = parts[0]
        if len(parts) == 4 and UUID_PATTERN.fullmatch(parts[3]):
            claude_option_sessions[parts[3].lower()] = parts[0]
    return pane_sessions, codex_option_sessions, claude_option_sessions


def _tmux_session_for_process(
    pid: int, parents: Mapping[int, int], pane_sessions: Mapping[int, str]
) -> str | None:
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        tmux_session = pane_sessions.get(current)
        if tmux_session is not None:
            return tmux_session
        current = parents.get(current, 0)
    return None


def _claude_session_id_from_args(arguments: Sequence[str]) -> str | None:
    value_flags = {"--resume", "-r", "--session-id"}
    for index, argument in enumerate(arguments):
        if argument in value_flags and index + 1 < len(arguments):
            candidate = arguments[index + 1].lower()
            if UUID_PATTERN.fullmatch(candidate):
                return candidate
        for flag in ("--resume=", "--session-id="):
            if argument.startswith(flag):
                candidate = argument[len(flag) :].lower()
                if UUID_PATTERN.fullmatch(candidate):
                    return candidate
    return None


def _claude_session_id_for_process(pid: int) -> str | None:
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    session_id = _claude_session_id_from_args(arguments)
    if session_id is not None:
        return session_id

    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError):
        return None
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        path = Path(target)
        if path.suffix != ".jsonl" or "projects" not in path.parts:
            continue
        candidate = path.stem.lower()
        if UUID_PATTERN.fullmatch(candidate):
            return candidate
    return None


def _claude_active_sessions(
    parents: Mapping[int, int],
    claude_pids: set[int],
    pane_sessions: Mapping[int, str],
    option_sessions: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    option_ids = {tmux_session: session_id for session_id, tmux_session in option_sessions.items()}
    active: dict[str, dict[str, Any]] = {}
    for pid in claude_pids:
        tmux_session = _tmux_session_for_process(pid, parents, pane_sessions)
        session_id = option_ids.get(tmux_session or "")
        if session_id is None:
            session_id = _claude_session_id_for_process(pid)
        if session_id is None:
            continue
        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(session_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[session_id] = item
    return active


def active_snapshot() -> dict[str, Any]:
    parents, codex_pids, claude_pids = _process_table()
    pane_sessions, option_sessions, claude_option_sessions = _tmux_panes()
    active: dict[str, dict[str, Any]] = {}

    for pid in codex_pids:
        thread_id = _thread_id_for_process(pid)
        if thread_id is None:
            continue

        tmux_session = option_sessions.get(thread_id)
        if tmux_session is None:
            tmux_session = _tmux_session_for_process(pid, parents, pane_sessions)

        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(thread_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[thread_id] = item

    return {
        "host": socket.gethostname(),
        "active": active,
        "claudeInstalled": shutil.which("claude") is not None,
        "claudeActive": _claude_active_sessions(
            parents,
            claude_pids,
            pane_sessions,
            claude_option_sessions,
        ),
    }


ACTIVE_PROBE = r"""
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

uuid_pattern = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
ps = subprocess.run(
    ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm="],
    check=True,
    capture_output=True,
    text=True,
)
parents = {}
codex_pids = set()
claude_pids = set()
for line in ps.stdout.splitlines():
    parts = line.split(None, 2)
    if len(parts) != 3:
        continue
    pid, parent = int(parts[0]), int(parts[1])
    parents[pid] = parent
    command = parts[2].lower()
    if "codex" in command:
        codex_pids.add(pid)
    if "claude" in command:
        claude_pids.add(pid)

panes = subprocess.run(
    [
        "tmux",
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}\t#{@claude_session_id}",
    ],
    capture_output=True,
    text=True,
)
pane_sessions = {}
option_sessions = {}
claude_option_sessions = {}
if panes.returncode == 0:
    for line in panes.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) >= 3 and uuid_pattern.fullmatch(parts[2]):
            option_sessions[parts[2].lower()] = parts[0]
        if len(parts) == 4 and uuid_pattern.fullmatch(parts[3]):
            claude_option_sessions[parts[3].lower()] = parts[0]

active = {}
def tmux_session_for_process(pid):
    current = pid
    visited = set()
    while current > 1 and current not in visited:
        visited.add(current)
        tmux_session = pane_sessions.get(current)
        if tmux_session is not None:
            return tmux_session
        current = parents.get(current, 0)
    return None

for pid in codex_pids:
    thread_id = None
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, PermissionError):
        entries = []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if "rollout-" not in target or ".jsonl" not in target:
            continue
        match = uuid_pattern.search(target)
        if match:
            thread_id = match.group(1).lower()
            break
    if thread_id is None:
        continue

    tmux_session = option_sessions.get(thread_id)
    if tmux_session is None:
        tmux_session = tmux_session_for_process(pid)
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = active.get(thread_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        active[thread_id] = item

def claude_session_id_from_args(arguments):
    for index, argument in enumerate(arguments):
        if argument in {"--resume", "-r", "--session-id"} and index + 1 < len(arguments):
            candidate = arguments[index + 1].lower()
            if uuid_pattern.fullmatch(candidate):
                return candidate
        for flag in ("--resume=", "--session-id="):
            if argument.startswith(flag):
                candidate = argument[len(flag):].lower()
                if uuid_pattern.fullmatch(candidate):
                    return candidate
    return None

def claude_session_id_for_process(pid):
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    session_id = claude_session_id_from_args(arguments)
    if session_id is not None:
        return session_id
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, PermissionError):
        entries = []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        path = Path(target)
        if path.suffix != ".jsonl" or "projects" not in path.parts:
            continue
        candidate = path.stem.lower()
        if uuid_pattern.fullmatch(candidate):
            return candidate
    return None

claude_active = {}
claude_option_ids = {
    tmux_session: session_id for session_id, tmux_session in claude_option_sessions.items()
}
for pid in claude_pids:
    tmux_session = tmux_session_for_process(pid)
    session_id = claude_option_ids.get(tmux_session or "")
    if session_id is None:
        session_id = claude_session_id_for_process(pid)
    if session_id is None:
        continue
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = claude_active.get(session_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        claude_active[session_id] = item

print(json.dumps(
    {
        "host": socket.gethostname(),
        "active": active,
        "claudeInstalled": shutil.which("claude") is not None,
        "claudeActive": claude_active,
    },
    separators=(",", ":"),
))
"""


def remote_active_snapshot(
    host: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _ssh_prefix(ssh_policy) + [host, "python3 -"],
            input=ACTIVE_PROBE,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"active-session probe timed out on {host}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"active-session probe failed on {host}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PickerError(f"active-session probe returned invalid JSON on {host}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("active"), dict)
        or not isinstance(payload.get("claudeActive"), dict)
    ):
        raise PickerError(f"active-session probe returned invalid data on {host}")
    return payload


def get_active_snapshot(
    target: HostTarget,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    if target.connect_host is None:
        return active_snapshot()
    return remote_active_snapshot(target.connect_host, timeout, ssh_policy)


def merge_host_results(
    host_results: Sequence[
        tuple[
            HostTarget,
            list[dict[str, Any]] | Exception,
            dict[str, Any] | Exception,
            dict[str, Any] | Exception,
        ]
    ],
    limit: int,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    aliases = aliases or {}
    sessions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for target, threads_result, claude_result, active_result in host_results:
        if isinstance(active_result, Exception):
            errors.append({"host": target.key, "stage": "active", "message": str(active_result)})
            canonical_host = target.key
            active_map: dict[str, Any] = {}
            claude_active_map: dict[str, Any] = {}
        else:
            canonical_host = str(active_result.get("host") or target.key)
            active_map = active_result.get("active", {})
            claude_active_map = active_result.get("claudeActive", {})
        display_host = aliases.get(_short_hostname(canonical_host), canonical_host)

        if isinstance(threads_result, Exception):
            errors.append({"host": target.key, "stage": "threads", "message": str(threads_result)})
        else:
            for thread in threads_result:
                thread_id = str(thread.get("id") or "").lower()
                if not UUID_PATTERN.fullmatch(thread_id):
                    continue
                active_info = active_map.get(thread_id)
                name = str(thread.get("name") or "").strip()
                cwd = str(thread.get("cwd") or "").strip()
                sessions.append(
                    {
                        "kind": "codex",
                        "id": thread_id,
                        "name": name or Path(cwd).name or thread_id[:8],
                        "cwd": cwd,
                        "host": display_host,
                        "windowHost": canonical_host,
                        "connectHost": target.key,
                        "recencyAt": int(thread.get("recencyAt") or 0),
                        "updatedAt": int(thread.get("updatedAt") or 0),
                        "active": active_info is not None,
                        "tmuxSession": (
                            active_info.get("tmuxSession")
                            if isinstance(active_info, dict)
                            else None
                        ),
                    }
                )

        if isinstance(claude_result, Exception):
            errors.append({"host": target.key, "stage": "claude", "message": str(claude_result)})
        elif claude_result.get("installed"):
            for conversation in claude_result.get("sessions", []):
                if not isinstance(conversation, dict):
                    continue
                session_id = str(conversation.get("id") or "").lower()
                if not UUID_PATTERN.fullmatch(session_id):
                    continue
                active_info = claude_active_map.get(session_id)
                name = str(conversation.get("name") or "").strip()
                cwd = str(conversation.get("cwd") or "").strip()
                sessions.append(
                    {
                        "kind": "claude",
                        "id": session_id,
                        "name": name or Path(cwd).name or session_id[:8],
                        "cwd": cwd,
                        "host": display_host,
                        "windowHost": canonical_host,
                        "connectHost": target.key,
                        "recencyAt": int(conversation.get("recencyAt") or 0),
                        "updatedAt": int(conversation.get("updatedAt") or 0),
                        "active": active_info is not None,
                        "tmuxSession": (
                            active_info.get("tmuxSession")
                            if isinstance(active_info, dict)
                            else None
                        ),
                    }
                )

    sessions.sort(key=lambda item: (item["recencyAt"], item["id"]), reverse=True)
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for session in sessions:
        key = (session["windowHost"], session["kind"], session["id"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(session)
        if len(deduplicated) >= limit:
            break

    return {
        "generatedAt": int(time.time()),
        "sessions": deduplicated,
        "errors": errors,
    }


def aggregate_sessions(
    hosts: Sequence[str],
    limit: int,
    timeout: float,
    include_local: bool = True,
    aliases: Mapping[str, str] | None = None,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    aliases = aliases or {}
    targets: list[HostTarget] = []
    if include_local:
        targets.append(HostTarget(None))
    local_names = {_short_hostname(socket.gethostname())}
    local_alias = aliases.get(_short_hostname(socket.gethostname()))
    if local_alias:
        local_names.add(_short_hostname(local_alias))
    targets.extend(
        HostTarget(host.strip())
        for host in hosts
        if host.strip() and _short_hostname(host.strip()) not in local_names
    )

    thread_results: dict[str, list[dict[str, Any]] | Exception] = {}
    claude_results: dict[str, dict[str, Any] | Exception] = {}
    active_results: dict[str, dict[str, Any] | Exception] = {}
    workers = max(1, min(12, len(targets) * 3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        thread_futures = {
            target.key: pool.submit(list_codex_threads, target, limit, timeout, ssh_policy)
            for target in targets
        }
        claude_futures = {
            target.key: pool.submit(list_claude_sessions, target, limit, timeout, ssh_policy)
            for target in targets
        }
        active_futures = {
            target.key: pool.submit(get_active_snapshot, target, timeout, ssh_policy)
            for target in targets
        }
        for target in targets:
            try:
                thread_results[target.key] = thread_futures[target.key].result()
            except Exception as exc:  # Keep other hosts usable.
                thread_results[target.key] = exc
            try:
                claude_results[target.key] = claude_futures[target.key].result()
            except Exception as exc:  # Claude is optional on every host.
                claude_results[target.key] = exc
            try:
                active_results[target.key] = active_futures[target.key].result()
            except Exception as exc:  # Active state is optional list metadata.
                active_results[target.key] = exc

    return merge_host_results(
        [
            (
                target,
                thread_results[target.key],
                claude_results[target.key],
                active_results[target.key],
            )
            for target in targets
        ],
        limit,
        aliases,
    )


def _safe_tmux_name(name: str, thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned[:48] or f"agent-{thread_id[:8]}"


def _tmux_client_wait_script() -> str:
    return (
        'while :; do attached=$(tmux display-message -p -t "$TMUX_PANE" '
        '"#{session_attached}" 2>/dev/null || true); '
        'case "$attached" in ""|0) sleep 0.05 ;; *) break ;; esac; done; '
        'sleep 0.05; exec "$@"'
    )


def _ensure_session_script(thread_id: str, name: str, cwd: str) -> str:
    base = _safe_tmux_name(name, thread_id)
    short_id = thread_id[:8]
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
thread_id={shlex.quote(thread_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
wait_script={shlex.quote(wait_script)}
codex_bin=$(command -v codex)
if [ -z "$requested_cwd" ] || [ ! -d "$requested_cwd" ]; then
    requested_cwd=$HOME
fi
candidate=$base
counter=0
while tmux has-session -t "=$candidate" 2>/dev/null; do
    counter=$((counter + 1))
    if [ "$counter" -eq 1 ]; then
        candidate="${{base}}-${{short_id}}"
    else
        candidate="${{base}}-${{short_id}}-$counter"
    fi
done
codex_command="exec sh -c '$wait_script' sh \"$codex_bin\" resume \"$thread_id\""
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @codex_thread_id "$thread_id"
tmux set-option -t "$candidate" @codex_name "$display_name"
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$codex_command"
printf '%s\n' "$candidate"
"""


def ensure_tmux_session(
    target: HostTarget,
    thread_id: str,
    name: str,
    cwd: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_session_script(thread_id, name, cwd)
    command = _tmux_creation_command(target, script, ssh_policy)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating tmux session on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create tmux session on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return a session name on {target.key}")
    return session


def resolve_open_target(
    target: HostTarget,
    thread_id: str,
    name: str | None,
    cwd: str | None,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    active_info = snapshot.get("active", {}).get(thread_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Codex session {thread_id[:8]} is active on {target.key} outside tmux")

    if not name or cwd is None:
        thread = read_codex_thread(target, thread_id, timeout, ssh_policy)
        name = name or str(thread.get("name") or "")
        cwd = cwd if cwd is not None else str(thread.get("cwd") or "")
    return ensure_tmux_session(
        target,
        thread_id,
        name or "codex",
        cwd or "",
        timeout,
        ssh_policy,
    )


def _ensure_claude_session_script(session_id: str, name: str, cwd: str) -> str:
    base = _safe_tmux_name(name, session_id)
    short_id = session_id[:8]
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
session_id={shlex.quote(session_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
wait_script={shlex.quote(wait_script)}
claude_bin=$(command -v claude || true)
if [ -z "$claude_bin" ]; then
    printf '%s\n' 'claude is not installed' >&2
    exit 1
fi
if [ -z "$requested_cwd" ] || [ ! -d "$requested_cwd" ]; then
    requested_cwd=$HOME
fi
candidate=$base
counter=0
while tmux has-session -t "=$candidate" 2>/dev/null; do
    counter=$((counter + 1))
    if [ "$counter" -eq 1 ]; then
        candidate="${{base}}-${{short_id}}"
    else
        candidate="${{base}}-${{short_id}}-$counter"
    fi
done
claude_command="exec sh -c '$wait_script' sh \"$claude_bin\" --resume \"$session_id\""
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @claude_session_id "$session_id"
tmux set-option -t "$candidate" @claude_name "$display_name"
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$claude_command"
printf '%s\n' "$candidate"
"""


def ensure_claude_tmux_session(
    target: HostTarget,
    session_id: str,
    name: str,
    cwd: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_claude_session_script(session_id, name, cwd)
    command = _tmux_creation_command(target, script, ssh_policy)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating Claude session on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create Claude session on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return a Claude session on {target.key}")
    return session


def resolve_claude_open_target(
    target: HostTarget,
    session_id: str,
    name: str | None,
    cwd: str | None,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    if not snapshot.get("claudeInstalled"):
        raise PickerError(f"Claude Code is not installed on {target.key}")

    active_info = snapshot.get("claudeActive", {}).get(session_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Claude session {session_id[:8]} is active on {target.key} outside tmux")

    if not name or cwd is None:
        conversation = read_claude_session(target, session_id, timeout, ssh_policy)
        name = name or str(conversation.get("name") or "")
        cwd = cwd if cwd is not None else str(conversation.get("cwd") or "")
    return ensure_claude_tmux_session(
        target,
        session_id,
        name or "claude",
        cwd or "",
        timeout,
        ssh_policy,
    )


def _terminal_command(terminal: str, inner: list[str]) -> list[str]:
    command = shlex.split(terminal)
    if not command:
        raise PickerError("terminal command is empty")
    executable = shutil.which(command[0])
    if executable is None:
        raise PickerError(f"terminal is not installed: {command[0]}")
    command[0] = executable
    if "-e" not in command:
        command.append("-e")
    return command + inner


def _remote_attach_command(session: str) -> str:
    # zsh expands an unquoted leading "=" as a command path. tmux uses it to
    # request an exact session-name match, so force quotes even for safe names.
    target = "'" + f"={session}".replace("'", "'\"'\"'") + "'"
    # Noninteractive SSH commands may have no locale; force a UTF-8 client so
    # tmux does not replace Unicode cells with underscores while attaching.
    return "exec tmux -u attach-session -t " + target


def _matching_niri_window_id(windows: Sequence[object], session: str, host: str) -> int | None:
    session_prefix = f"{session}:"
    expected_host = _short_hostname(host)
    for window in windows:
        if not isinstance(window, dict):
            continue
        title = window.get("title")
        window_id = window.get("id")
        if not isinstance(title, str) or not isinstance(window_id, int):
            continue
        if not title.startswith(session_prefix) or " @ " not in title:
            continue
        title_host = title.rsplit(" @ ", 1)[1]
        if _short_hostname(title_host) == expected_host:
            return window_id
    return None


def focus_existing_window(session: str, host: str, timeout: float) -> bool:
    niri = shutil.which("niri")
    if not niri or not os.environ.get("NIRI_SOCKET"):
        return False
    command_timeout = min(max(timeout, 0.5), 2.0)
    try:
        result = subprocess.run(
            [niri, "msg", "--json", "windows"],
            capture_output=True,
            text=True,
            timeout=command_timeout,
            check=False,
        )
        if result.returncode != 0:
            return False
        windows = json.loads(result.stdout)
        if not isinstance(windows, list):
            return False
        window_id = _matching_niri_window_id(windows, session, host)
        if window_id is None:
            return False
        focused = subprocess.run(
            [niri, "msg", "action", "focus-window", "--id", str(window_id)],
            capture_output=True,
            text=True,
            timeout=command_timeout,
            check=False,
        )
        return focused.returncode == 0
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return False


def launch_attach(
    target: HostTarget,
    session: str,
    terminal: str,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> None:
    if target.connect_host is None:
        inner = ["tmux", "attach-session", "-t", f"={session}"]
    else:
        remote_command = _remote_attach_command(session)
        inner = _ssh_prefix(ssh_policy) + ["-t", target.connect_host, remote_command]

    command = _terminal_command(terminal, inner)
    command = _local_scope_command(command)
    os.execvp(command[0], command)


def _valid_thread_id(value: str) -> str:
    value = value.lower()
    if not UUID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a session UUID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--ssh-connect-timeout",
        type=int,
        default=DEFAULT_SSH_CONNECT_TIMEOUT,
    )
    parser.add_argument(
        "--ssh-connection-attempts",
        type=int,
        default=DEFAULT_SSH_CONNECTION_ATTEMPTS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list agent sessions as JSON")
    list_parser.add_argument("--host", action="append", default=[])
    list_parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="display hostname mapping in source=display form",
    )
    list_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    list_parser.add_argument("--no-local", action="store_true")

    active_parser = subparsers.add_parser("active", help="show active local agent sessions as JSON")
    active_parser.set_defaults(command="active")

    open_parser = subparsers.add_parser("open", help="open or resume a Codex session")
    open_parser.add_argument("--host", default="local")
    open_parser.add_argument("--window-host")
    open_parser.add_argument("--id", required=True, type=_valid_thread_id)
    open_parser.add_argument("--name")
    open_parser.add_argument("--cwd")
    open_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))

    claude_parser = subparsers.add_parser("open-claude", help="open or resume a Claude session")
    claude_parser.add_argument("--host", default="local")
    claude_parser.add_argument("--window-host")
    claude_parser.add_argument("--id", required=True, type=_valid_thread_id)
    claude_parser.add_argument("--name")
    claude_parser.add_argument("--cwd")
    claude_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.timeout <= 0:
            raise PickerError("timeout must be positive")
        if args.ssh_connect_timeout < 1 or args.ssh_connect_timeout > 30:
            raise PickerError("SSH connect timeout must be between 1 and 30 seconds")
        if args.ssh_connection_attempts < 1 or args.ssh_connection_attempts > 5:
            raise PickerError("SSH connection attempts must be between 1 and 5")
        ssh_policy = SshPolicy(args.ssh_connect_timeout, args.ssh_connection_attempts)

        if args.command == "list":
            if args.limit < 1 or args.limit > 200:
                raise PickerError("limit must be between 1 and 200")
            aliases = parse_host_aliases(args.alias)
            payload = aggregate_sessions(
                args.host,
                args.limit,
                args.timeout,
                include_local=not args.no_local,
                aliases=aliases,
                ssh_policy=ssh_policy,
            )
            print(json.dumps(payload, separators=(",", ":")))
            return 0

        if args.command == "active":
            print(json.dumps(active_snapshot(), separators=(",", ":")))
            return 0

        target = HostTarget(None if args.host in {"", "local"} else args.host)
        if args.command == "open-claude":
            session = resolve_claude_open_target(
                target,
                args.id,
                args.name,
                args.cwd,
                args.timeout,
                ssh_policy,
            )
        else:
            session = resolve_open_target(
                target,
                args.id,
                args.name,
                args.cwd,
                args.timeout,
                ssh_policy,
            )
        window_host = args.window_host or target.connect_host or socket.gethostname()
        if focus_existing_window(session, window_host, args.timeout):
            return 0
        launch_attach(target, session, args.terminal, ssh_policy)
        return 0
    except PickerError as exc:
        print(f"dms-agent-picker: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
