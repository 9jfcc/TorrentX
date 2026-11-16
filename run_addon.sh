#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
python3 -m pip install -q requests 2>/dev/null
nohup python3 skills/torrentx/stremio_addon.py >/dev/null 2>&1 &
sleep 1
if which xdg-open > /dev/null; then
    xdg-open http://127.0.0.1:7123/configure
elif which open > /dev/null; then
    open http://127.0.0.1:7123/configure
fi
echo "TorrentX Addon is running at http://127.0.0.1:7123/configure"
