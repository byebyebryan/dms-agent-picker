#!/usr/bin/env python3
"""Aggregate and open Codex and Claude Code workspaces from local and SSH hosts."""

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
CLAUDE_TMUX_SESSION = "claude-code"
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
                    "version": "0.2.0",
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
                chunk = os.read(stream.fileno(), 65536)
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


def _tmux_panes() -> tuple[dict[int, str], dict[str, str]]:
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}, {}

    pane_sessions: dict[int, str] = {}
    option_sessions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) == 3 and UUID_PATTERN.fullmatch(parts[2]):
            option_sessions[parts[2].lower()] = parts[0]
    return pane_sessions, option_sessions


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


def _claude_snapshot(
    parents: Mapping[int, int],
    claude_pids: set[int],
    pane_sessions: Mapping[int, str],
) -> dict[str, Any]:
    tmux_sessions = sorted(
        {
            session
            for pid in claude_pids
            if (session := _tmux_session_for_process(pid, parents, pane_sessions)) is not None
        }
    )
    preferred_session = None
    if CLAUDE_TMUX_SESSION in tmux_sessions:
        preferred_session = CLAUDE_TMUX_SESSION
    elif tmux_sessions:
        preferred_session = tmux_sessions[0]
    return {
        "installed": shutil.which("claude") is not None,
        "running": bool(claude_pids),
        "tmuxSession": preferred_session,
        "tmuxSessions": tmux_sessions,
    }


def active_snapshot() -> dict[str, Any]:
    parents, codex_pids, claude_pids = _process_table()
    pane_sessions, option_sessions = _tmux_panes()
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
        "claude": _claude_snapshot(parents, claude_pids, pane_sessions),
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
    ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}"],
    capture_output=True,
    text=True,
)
pane_sessions = {}
option_sessions = {}
if panes.returncode == 0:
    for line in panes.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) == 3 and uuid_pattern.fullmatch(parts[2]):
            option_sessions[parts[2].lower()] = parts[0]

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

claude_tmux_sessions = sorted({
    tmux_session
    for pid in claude_pids
    if (tmux_session := tmux_session_for_process(pid)) is not None
})
if "claude-code" in claude_tmux_sessions:
    claude_tmux_session = "claude-code"
elif claude_tmux_sessions:
    claude_tmux_session = claude_tmux_sessions[0]
else:
    claude_tmux_session = None
claude = {
    "installed": shutil.which("claude") is not None,
    "running": bool(claude_pids),
    "tmuxSession": claude_tmux_session,
    "tmuxSessions": claude_tmux_sessions,
}

print(json.dumps(
    {"host": socket.gethostname(), "active": active, "claude": claude},
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
        or not isinstance(payload.get("claude"), dict)
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
        tuple[HostTarget, list[dict[str, Any]] | Exception, dict[str, Any] | Exception]
    ],
    limit: int,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    aliases = aliases or {}
    sessions: list[dict[str, Any]] = []
    workspaces: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for target, threads_result, active_result in host_results:
        if isinstance(active_result, Exception):
            errors.append({"host": target.key, "stage": "active", "message": str(active_result)})
            canonical_host = target.key
            active_map: dict[str, Any] = {}
            claude: dict[str, Any] = {}
        else:
            canonical_host = str(active_result.get("host") or target.key)
            active_map = active_result.get("active", {})
            claude = active_result.get("claude", {})
        display_host = aliases.get(_short_hostname(canonical_host), canonical_host)

        if claude.get("installed"):
            workspaces.append(
                {
                    "kind": "claude",
                    "id": CLAUDE_TMUX_SESSION,
                    "name": "Claude Code",
                    "host": display_host,
                    "windowHost": canonical_host,
                    "connectHost": target.key,
                    "active": bool(claude.get("running")),
                    "tmuxSession": claude.get("tmuxSession"),
                }
            )

        if isinstance(threads_result, Exception):
            errors.append({"host": target.key, "stage": "threads", "message": str(threads_result)})
            continue

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
                        active_info.get("tmuxSession") if isinstance(active_info, dict) else None
                    ),
                }
            )

    sessions.sort(key=lambda item: (item["recencyAt"], item["id"]), reverse=True)
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in sessions:
        key = (session["windowHost"], session["id"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(session)
        if len(deduplicated) >= limit:
            break

    deduplicated_workspaces: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for workspace in workspaces:
        host = _short_hostname(workspace["windowHost"])
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        deduplicated_workspaces.append(workspace)

    return {
        "generatedAt": int(time.time()),
        "workspaces": deduplicated_workspaces,
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
    active_results: dict[str, dict[str, Any] | Exception] = {}
    workers = max(1, min(12, len(targets) * 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        thread_futures = {
            target.key: pool.submit(list_codex_threads, target, limit, timeout, ssh_policy)
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
                active_results[target.key] = active_futures[target.key].result()
            except Exception as exc:  # Active state is optional list metadata.
                active_results[target.key] = exc

    return merge_host_results(
        [(target, thread_results[target.key], active_results[target.key]) for target in targets],
        limit,
        aliases,
    )


def _safe_tmux_name(name: str, thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned[:48] or f"codex-{thread_id[:8]}"


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
    command = ["sh", "-lc", script]
    if target.connect_host is not None:
        command = _ssh_prefix(ssh_policy) + [
            target.connect_host,
            "sh -lc " + shlex.quote(script),
        ]
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


def _ensure_claude_session_script() -> str:
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
session={shlex.quote(CLAUDE_TMUX_SESSION)}
wait_script={shlex.quote(wait_script)}
claude_bin=$(command -v claude || true)
if [ -z "$claude_bin" ]; then
    printf '%s\n' 'claude is not installed' >&2
    exit 1
fi
if tmux has-session -t "=$session" 2>/dev/null; then
    printf '%s\n' "$session"
    exit 0
fi
workspace_cwd=$HOME/code
if [ ! -d "$workspace_cwd" ]; then
    workspace_cwd=$HOME
fi
claude_command="exec sh -c '$wait_script' sh \"$claude_bin\" --resume"
tmux new-session -d -s "$session" -c "$workspace_cwd" "$claude_command"
tmux set-option -t "$session" @agent_workspace claude
printf '%s\n' "$session"
"""


def ensure_claude_tmux_session(
    target: HostTarget,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_claude_session_script()
    command = ["sh", "-lc", script]
    if target.connect_host is not None:
        command = _ssh_prefix(ssh_policy) + [
            target.connect_host,
            "sh -lc " + shlex.quote(script),
        ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating Claude Code workspace on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create Claude Code workspace on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return a Claude Code workspace on {target.key}")
    return session


def resolve_claude_open_target(
    target: HostTarget,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    claude = snapshot.get("claude")
    if not isinstance(claude, dict) or not claude.get("installed"):
        raise PickerError(f"Claude Code is not installed on {target.key}")

    if claude.get("running"):
        tmux_session = claude.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Claude Code is active on {target.key} outside tmux")

    return ensure_claude_tmux_session(target, timeout, ssh_policy)


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


def _matching_niri_window_id(
    windows: Sequence[object], session: str, host: str
) -> int | None:
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
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        command = [
            systemd_run,
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            "--",
        ] + command
    os.execvp(command[0], command)


def _valid_thread_id(value: str) -> str:
    value = value.lower()
    if not UUID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a Codex session UUID")
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

    active_parser = subparsers.add_parser("active", help="show active local Codex sessions as JSON")
    active_parser.set_defaults(command="active")

    open_parser = subparsers.add_parser("open", help="open or resume a Codex session")
    open_parser.add_argument("--host", default="local")
    open_parser.add_argument("--window-host")
    open_parser.add_argument("--id", required=True, type=_valid_thread_id)
    open_parser.add_argument("--name")
    open_parser.add_argument("--cwd")
    open_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))

    claude_parser = subparsers.add_parser(
        "open-claude", help="open the host's Claude Code workspace"
    )
    claude_parser.add_argument("--host", default="local")
    claude_parser.add_argument("--window-host")
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
            session = resolve_claude_open_target(target, args.timeout, ssh_policy)
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
