#!/usr/bin/env bash
set -e

# Start dbus session daemon if not already running
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval $(dbus-launch --sh-syntax)
fi

# Ensure persistent directories exist in /agy-state volume
mkdir -p /agy-state/keyrings /agy-state/.gemini /agy-state/.config /agy-state/.cache

# Symlink persistent state to /root
mkdir -p /root/.local/share
rm -rf /root/.local/share/keyrings
ln -sfn /agy-state/keyrings /root/.local/share/keyrings
ln -sfn /agy-state/.gemini /root/.gemini
ln -sfn /agy-state/.config /root/.config
ln -sfn /agy-state/.cache /root/.cache

# Unlock and start gnome-keyring-daemon silently
eval "$(printf '\n' | gnome-keyring-daemon --unlock --components=secrets 2>/dev/null || true)"
eval "$(gnome-keyring-daemon --start --components=secrets 2>/dev/null || true)"

# Prepend testbed python environment to PATH if present
if [ -d "/opt/miniconda3/envs/testbed/bin" ]; then
    export PATH="/opt/miniconda3/envs/testbed/bin:$PATH"
fi

exec "$@"
