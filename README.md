# DMS Agent Picker

A DankMaterialShell launcher plugin for Codex CLI and Claude Code sessions
across local and SSH hosts.

The picker uses Codex's app-server protocol and Claude's local project
transcripts for session metadata. It inspects running agent processes to map
them back to tmux sessions. Remote hosts do not need this project installed.

## Runtime scope

The supported execution model is one dedicated CLI/TUI process per interactive
session, managed through tmux.

- Codex sessions are opened with `codex resume`. Persistent or shared
  app-server runtimes, including TUIs connected with `codex --remote`, are not
  supported and are ignored by active-session discovery. The picker starts a
  short-lived `codex app-server --stdio` process only to query saved session
  metadata; it does not use that process to host interactive sessions.
- Claude sessions are opened with `claude --resume`. Claude Agent View sessions
  hosted by its per-user supervisor are not supported, and the picker does not
  communicate with that supervisor. Headless and Agent SDK sessions are also
  outside the supported runtime.

Claude's regular TUI and Agent View share the same project transcript storage.
A stopped Agent View conversation can therefore appear in saved-session
results, but the picker does not track its supervisor state or attach to it as
an Agent View job. Opening it from the picker always creates a dedicated tmux
session and invokes `claude --resume`.

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

The maintainer workstation deploys this plugin as a version-pinned chezmoi
archive. The normal release flow is: commit and push this repository, update
the archive URL and checksum in the chezmoi external declaration, then apply
chezmoi. This keeps the live DMS plugin independent from an in-progress working
tree.

`install.sh` is only for a first-time unmanaged checkout. It refuses to replace
an existing plugin directory, so it cannot accidentally bypass a pinned
deployment.

```sh
./install.sh
dms ipc call plugin-scan scan
dms ipc call plugins enable agentSessions
```

Configure the launcher trigger and host routes under DMS plugin settings. The
settings file is local to each workstation and is not managed by chezmoi, so
each machine can discover only the hosts it should consume. The local host is
always included.

Host Routes use comma-separated `name=preferred|fallback` entries, for example
`snap=snap.wg.lan|snap.lan, starship=starship.lan`. The picker first chooses a
reachable SSH path for each logical host, then runs its Codex, Claude, and
activity probes against that single path. It displays the stable logical name
(`snap` in the example) and retains the route when opening a selected session,
so a later resume can use the fallback path too. Leave Host Routes empty to use
the legacy SSH Hosts and Host Aliases settings.

SSH connection timeout and retry count are configurable. Their defaults are a
2-second connection timeout and one connection attempt; batch mode is always
enabled to prevent interactive authentication prompts.

Session data is preloaded once when the plugin starts, then refreshed
asynchronously when the picker is queried and its cache is stale. Refreshes
complete independently per host: cached sessions remain available while a host
is refreshing, and each completed host updates the picker immediately. The
plugin does not poll SSH hosts continuously while the picker is closed. The
cache TTL defaults to 30 seconds and can be configured from 5 to 300 seconds.

Launcher results use the right-side badge to identify Claude and Codex, while
the subtitle shows the host, working directory, session age, and activity
state. A single compact `Refreshing` status row names the hosts whose probes
are still in flight. If discovery cannot reach a host or only returns partial
details, that row becomes a warning for 3 seconds after refresh completion,
then disappears rather than lingering in the picker.

## CLI

List the 20 most recently prompted sessions:

```sh
dms-agent-picker list --host laptop.lan --limit 20 | jq
```

Use a logical route with an ordered fallback path:

```sh
dms-agent-picker list --route 'snap=snap.wg.lan|snap.lan' --limit 20 | jq
```

For integrations that need per-host progress, opt into JSON Lines events. The
stream starts with the configured connection targets, emits one
`host-complete` event after that host's Codex, Claude, and activity probes have
settled, then emits `refresh-finished`:

```sh
dms-agent-picker list --host laptop.lan --stream
```

The default `list` output remains one final JSON object for scripts that do
not need incremental updates.

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
startup so terminal capability and color probes reach the actual terminal. A
session that is still waiting is reused by a second launch attempt; an
unattached session expires after one minute rather than lingering indefinitely.

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
