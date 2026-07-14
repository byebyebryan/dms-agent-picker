# DMS Agent Picker

A DankMaterialShell launcher plugin for recent Codex CLI sessions across local
and SSH hosts.

The picker uses Codex's app-server protocol for session metadata and inspects
running Codex processes to map active session UUIDs back to tmux sessions.
Remote hosts do not need this project installed.

## Requirements

Local desktop:

- Codex CLI
- DankMaterialShell
- Ghostty, or another terminal with `-e` support
- Python 3.11+
- tmux

Remote hosts:

- Passwordless SSH
- Codex CLI with `app-server` and `recency_at` thread sorting
- Python 3
- tmux

## Install

```sh
./install.sh
dms ipc call plugin-scan rescan
dms ipc call plugins enable agentSessions
```

Configure the launcher trigger and SSH hosts under DMS plugin settings. The
local host is always included.

## CLI

List the 20 most recently prompted sessions:

```sh
dms-agent-picker list --host laptop.lan --limit 20 | jq
```

Inspect active local Codex sessions:

```sh
dms-agent-picker active | jq
```

Open a saved session:

```sh
dms-agent-picker open \
  --host local \
  --id 00000000-0000-0000-0000-000000000000
```

If the session is active in tmux, the picker attaches to that tmux session. If
it is inactive, the picker creates a tmux session in the recorded working
directory and runs `codex resume` with the session UUID.

## Test

```sh
python -m unittest discover -s tests -v
```
