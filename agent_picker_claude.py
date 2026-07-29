"""Claude Code transcript probe executed locally or over SSH."""

SESSION_PROBE = r"""
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
