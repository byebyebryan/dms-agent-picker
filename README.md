# DMS Agent Picker

A DankMaterialShell launcher plugin for Codex CLI and Claude Code sessions
across local and SSH hosts.

The picker uses Codex's app-server protocol and Claude's local project
transcripts for session metadata. It inspects running agent processes to map
them back to tmux sessions. Remote hosts do not need this project installed.

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

Claude Code is optional on every host. Its saved conversations are included
automatically where it is installed.

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

Launcher results use the right-side badge to identify Claude and Codex, while
the subtitle shows the host, working directory, and session age.

## CLI

List the 20 most recently prompted sessions:

```sh
dms-agent-picker list --host laptop.lan --limit 20 | jq
```

Inspect active local agent sessions:

```sh
dms-agent-picker active | jq
```

Open a saved session:

```sh
dms-agent-picker open \
  --host local \
  --id 00000000-0000-0000-0000-000000000000
```

Open a saved Claude Code session:

```sh
dms-agent-picker open-claude \
  --host laptop.lan \
  --id 00000000-0000-0000-0000-000000000000
```

If the session is active in tmux, the picker attaches to that tmux session. If
it is inactive, the picker creates a tmux session in the recorded working
directory and resumes the selected UUID with `codex resume` or
`claude --resume`. New agent processes wait for the terminal to attach before
startup so terminal capability and color probes reach the actual terminal.

On a local systemd desktop, session creation runs in a transient user scope so
a newly created tmux server does not inherit `dms.service` and survives DMS
reloads or restarts. Systems without `systemd-run` retain the direct-launch
fallback, and remote session creation remains owned by the remote host.

Claude conversations are discovered from
`$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`, or `~/.claude/projects/*/*.jsonl` when
that variable is unset. Sessions created by this plugin carry their Claude UUID
as tmux metadata, allowing later launcher queries to identify and reuse the
exact active conversation. Headless and Agent SDK sessions are omitted, matching
Claude Code's interactive session picker.

Under niri, the picker first focuses an existing terminal window attached to
the same host and tmux session. It opens a new terminal only when no matching
window is present.

## Test

```sh
python -m unittest discover -s tests -v
```
