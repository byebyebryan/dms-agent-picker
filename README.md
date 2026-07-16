# DMS Agent Picker

A DankMaterialShell launcher plugin for Codex CLI sessions and Claude Code
workspaces across local and SSH hosts.

The picker uses Codex's app-server protocol for session metadata and inspects
running agent processes to map them back to tmux sessions. Remote hosts do not
need this project installed.

Codex sessions are listed individually. Claude Code is represented by one
workspace per host and delegates conversation selection to Claude's built-in
session picker.

## Requirements

Local desktop:

- Codex CLI
- DankMaterialShell
- Ghostty, or another terminal with `-e` support
- Python 3.11+
- tmux

Remote hosts:

- Passwordless SSH
- Codex CLI with `app-server` and `recency_at` thread sorting, and/or Claude Code
- Python 3
- tmux

Claude Code is optional on every host. Hosts where it is installed gain a
`Claude Code` launcher item automatically.

## Install

```sh
./install.sh
dms ipc call plugin-scan scan
dms ipc call plugins enable agentSessions
```

Configure the launcher trigger and SSH hosts under DMS plugin settings. The
local host is always included and is skipped when it also appears in the shared
SSH host list. Optional aliases use `source=display` syntax, for example
`80h1vv3=snap`.

SSH connection timeout and retry count are configurable. Their defaults are a
2-second connection timeout and one connection attempt; batch mode is always
enabled to prevent interactive authentication prompts.

Session data is preloaded once when the plugin starts, then refreshed
asynchronously when the picker is queried and its cache is stale. The plugin
does not poll SSH hosts continuously while the picker is closed.

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

Open a host's Claude Code workspace:

```sh
dms-agent-picker open-claude --host laptop.lan
```

If the session is active in tmux, the picker attaches to that tmux session. If
it is inactive, the picker creates a tmux session in the recorded working
directory and runs `codex resume` with the session UUID. New agent processes
wait for the terminal to attach before startup so terminal capability and color
probes reach the actual terminal.

On a local systemd desktop, session creation runs in a transient user scope so
a newly created tmux server does not inherit `dms.service` and survives DMS
reloads or restarts. Systems without `systemd-run` retain the direct-launch
fallback, and remote session creation remains owned by the remote host.

For Claude Code, the picker adopts any existing Claude process running in tmux,
regardless of that tmux session's name. Otherwise it creates the canonical
`claude-code` tmux session in `~/code` when that directory exists and runs
`claude --resume`. Claude's picker initially scopes sessions to the current
project; press `Ctrl+A` there to show sessions from every project on the host.
The plugin does not inspect or track Claude conversation IDs.

Under niri, the picker first focuses an existing terminal window attached to
the same host and tmux session. It opens a new terminal only when no matching
window is present.

## Test

```sh
python -m unittest discover -s tests -v
```
