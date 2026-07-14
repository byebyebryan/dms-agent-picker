#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir=${HOME}/.local/bin
plugin_dir=${HOME}/.config/DankMaterialShell/plugins

mkdir -p "$bin_dir" "$plugin_dir"
ln -sfn "$repo_dir/bin/dms-agent-picker" "$bin_dir/dms-agent-picker"
ln -sfn "$repo_dir" "$plugin_dir/agentSessions"

printf 'Installed %s\n' "$bin_dir/dms-agent-picker"
printf 'Installed %s\n' "$plugin_dir/agentSessions"
