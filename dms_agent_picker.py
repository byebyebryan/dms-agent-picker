#!/usr/bin/env python3
"""Aggregate and open Codex CLI sessions from local and SSH hosts."""

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
from typing import Any, Sequence


DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT = 4.0
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
                    "version": "0.1.0",
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


def _ssh_prefix(timeout: float) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout))}",
        "-o",
        "LogLevel=ERROR",
    ]


def _app_server_command(target: HostTarget, timeout: float) -> list[str]:
    if target.connect_host is None:
        codex = shutil.which("codex")
        if not codex:
            raise PickerError("codex is not installed")
        return [codex, "app-server", "--stdio"]
    return _ssh_prefix(timeout) + [target.connect_host, "codex app-server --stdio"]


def list_codex_threads(target: HostTarget, limit: int, timeout: float) -> list[dict[str, Any]]:
    with AppServerClient(_app_server_command(target, timeout), timeout) as client:
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


def read_codex_thread(target: HostTarget, thread_id: str, timeout: float) -> dict[str, Any]:
    with AppServerClient(_app_server_command(target, timeout), timeout) as client:
        client.initialize()
        result = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        raise PickerError(f"Codex session {thread_id} was not found")
    return thread


def _process_table() -> tuple[dict[int, int], set[int]]:
    result = subprocess.run(
        ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    parents: dict[int, int] = {}
    codex_pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid, parent = int(parts[0]), int(parts[1])
        parents[pid] = parent
        if "codex" in parts[2].lower():
            codex_pids.add(pid)
    return parents, codex_pids


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


def active_snapshot() -> dict[str, Any]:
    parents, codex_pids = _process_table()
    pane_sessions, option_sessions = _tmux_panes()
    active: dict[str, dict[str, Any]] = {}

    for pid in codex_pids:
        thread_id = _thread_id_for_process(pid)
        if thread_id is None:
            continue

        tmux_session = option_sessions.get(thread_id)
        current = pid
        visited: set[int] = set()
        while tmux_session is None and current > 1 and current not in visited:
            visited.add(current)
            tmux_session = pane_sessions.get(current)
            current = parents.get(current, 0)

        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(thread_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[thread_id] = item

    return {"host": socket.gethostname(), "active": active}


ACTIVE_PROBE = r"""
import json
import os
import re
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
for line in ps.stdout.splitlines():
    parts = line.split(None, 2)
    if len(parts) != 3:
        continue
    pid, parent = int(parts[0]), int(parts[1])
    parents[pid] = parent
    if "codex" in parts[2].lower():
        codex_pids.add(pid)

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
    current = pid
    visited = set()
    while tmux_session is None and current > 1 and current not in visited:
        visited.add(current)
        tmux_session = pane_sessions.get(current)
        current = parents.get(current, 0)
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = active.get(thread_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        active[thread_id] = item

print(json.dumps({"host": socket.gethostname(), "active": active}, separators=(",", ":")))
"""


def remote_active_snapshot(host: str, timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _ssh_prefix(timeout) + [host, "python3 -"],
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
    if not isinstance(payload, dict) or not isinstance(payload.get("active"), dict):
        raise PickerError(f"active-session probe returned invalid data on {host}")
    return payload


def get_active_snapshot(target: HostTarget, timeout: float) -> dict[str, Any]:
    if target.connect_host is None:
        return active_snapshot()
    return remote_active_snapshot(target.connect_host, timeout)


def merge_host_results(
    host_results: Sequence[
        tuple[HostTarget, list[dict[str, Any]] | Exception, dict[str, Any] | Exception]
    ],
    limit: int,
) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for target, threads_result, active_result in host_results:
        if isinstance(threads_result, Exception):
            errors.append({"host": target.key, "stage": "threads", "message": str(threads_result)})
            continue

        if isinstance(active_result, Exception):
            errors.append({"host": target.key, "stage": "active", "message": str(active_result)})
            canonical_host = target.key
            active_map: dict[str, Any] = {}
        else:
            canonical_host = str(active_result.get("host") or target.key)
            active_map = active_result.get("active", {})

        for thread in threads_result:
            thread_id = str(thread.get("id") or "").lower()
            if not UUID_PATTERN.fullmatch(thread_id):
                continue
            active_info = active_map.get(thread_id)
            name = str(thread.get("name") or "").strip()
            cwd = str(thread.get("cwd") or "").strip()
            sessions.append(
                {
                    "id": thread_id,
                    "name": name or Path(cwd).name or thread_id[:8],
                    "cwd": cwd,
                    "host": canonical_host,
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
        key = (session["host"], session["id"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(session)
        if len(deduplicated) >= limit:
            break

    return {"generatedAt": int(time.time()), "sessions": deduplicated, "errors": errors}


def aggregate_sessions(
    hosts: Sequence[str], limit: int, timeout: float, include_local: bool = True
) -> dict[str, Any]:
    targets: list[HostTarget] = []
    if include_local:
        targets.append(HostTarget(None))
    targets.extend(HostTarget(host) for host in hosts if host.strip())

    thread_results: dict[str, list[dict[str, Any]] | Exception] = {}
    active_results: dict[str, dict[str, Any] | Exception] = {}
    workers = max(1, min(12, len(targets) * 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        thread_futures = {
            target.key: pool.submit(list_codex_threads, target, limit, timeout)
            for target in targets
        }
        active_futures = {
            target.key: pool.submit(get_active_snapshot, target, timeout) for target in targets
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
    )


def _safe_tmux_name(name: str, thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned[:48] or f"codex-{thread_id[:8]}"


def _ensure_session_script(thread_id: str, name: str, cwd: str) -> str:
    base = _safe_tmux_name(name, thread_id)
    short_id = thread_id[:8]
    return f"""set -eu
thread_id={shlex.quote(thread_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
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
codex_command="exec \"$codex_bin\" resume \"$thread_id\""
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @codex_thread_id "$thread_id"
tmux set-option -t "$candidate" @codex_name "$display_name"
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$codex_command"
printf '%s\n' "$candidate"
"""


def ensure_tmux_session(
    target: HostTarget, thread_id: str, name: str, cwd: str, timeout: float
) -> str:
    script = _ensure_session_script(thread_id, name, cwd)
    command = ["sh", "-lc", script]
    if target.connect_host is not None:
        command = _ssh_prefix(timeout) + [
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
) -> str:
    snapshot = get_active_snapshot(target, timeout)
    active_info = snapshot.get("active", {}).get(thread_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Codex session {thread_id[:8]} is active on {target.key} outside tmux")

    if not name or cwd is None:
        thread = read_codex_thread(target, thread_id, timeout)
        name = name or str(thread.get("name") or "")
        cwd = cwd if cwd is not None else str(thread.get("cwd") or "")
    return ensure_tmux_session(target, thread_id, name or "codex", cwd or "", timeout)


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


def launch_attach(target: HostTarget, session: str, terminal: str, timeout: float) -> None:
    if target.connect_host is None:
        inner = ["tmux", "attach-session", "-t", f"={session}"]
    else:
        remote_command = "exec tmux attach-session -t " + shlex.quote(f"={session}")
        inner = _ssh_prefix(timeout) + ["-t", target.connect_host, remote_command]

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
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list recent Codex sessions as JSON")
    list_parser.add_argument("--host", action="append", default=[])
    list_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    list_parser.add_argument("--no-local", action="store_true")

    active_parser = subparsers.add_parser("active", help="show active local Codex sessions as JSON")
    active_parser.set_defaults(command="active")

    open_parser = subparsers.add_parser("open", help="open or resume a Codex session")
    open_parser.add_argument("--host", default="local")
    open_parser.add_argument("--id", required=True, type=_valid_thread_id)
    open_parser.add_argument("--name")
    open_parser.add_argument("--cwd")
    open_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            if args.limit < 1 or args.limit > 200:
                raise PickerError("limit must be between 1 and 200")
            payload = aggregate_sessions(
                args.host,
                args.limit,
                args.timeout,
                include_local=not args.no_local,
            )
            print(json.dumps(payload, separators=(",", ":")))
            return 0

        if args.command == "active":
            print(json.dumps(active_snapshot(), separators=(",", ":")))
            return 0

        target = HostTarget(None if args.host in {"", "local"} else args.host)
        session = resolve_open_target(target, args.id, args.name, args.cwd, args.timeout)
        launch_attach(target, session, args.terminal, args.timeout)
        return 0
    except PickerError as exc:
        print(f"dms-agent-picker: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
