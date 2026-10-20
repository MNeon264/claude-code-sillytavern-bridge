#!/bin/sh
# Install the Bridge Memory extension into a SillyTavern user's data folder.
# SillyTavern finds it when the page is next loaded; no restart needed.
#
#   st-extension/install.sh                          # ../SillyTavern/data/default-user
#   st-extension/install.sh /path/to/SillyTavern/data/<user>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
user_dir=${1:-"$here/../../SillyTavern/data/default-user"}
if [ ! -f "$user_dir/settings.json" ]; then
    echo "No SillyTavern user folder at $user_dir (no settings.json); pass its path." >&2
    exit 1
fi
dest="$user_dir/extensions/bridge-memory"
mkdir -p "$dest"
cp "$here/manifest.json" "$here/index.js" "$here/style.css" "$dest/"
echo "Installed to $dest. Reload SillyTavern in the browser."
