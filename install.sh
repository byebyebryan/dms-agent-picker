#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
bin_dir=${HOME}/.local/bin
plugin_dir=${HOME}/.config/DankMaterialShell/plugins

mkdir -p "$bin_dir" "$plugin_dir"
if [ -e "$plugin_dir/agentPicker" ] && [ ! -L "$plugin_dir/agentPicker" ]; then
    printf '%s\n' "agentPicker is an existing directory; update its pinned deployment instead" >&2
    exit 1
fi
ln -sfn "$repo_dir/bin/dms-agent-picker" "$bin_dir/dms-agent-picker"
ln -sfn "$repo_dir" "$plugin_dir/agentPicker"

printf 'Installed %s\n' "$bin_dir/dms-agent-picker"
printf 'Installed %s\n' "$plugin_dir/agentPicker"
