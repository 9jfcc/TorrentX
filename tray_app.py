# -*- coding: utf-8 -*-
"""
TorrentX Standalone Windows System Tray Application
- Runs the TorrentX Stremio / Harbor Addon server locally.
- Sits in the Windows Notification Area (System Tray) just like Jackett.
- Left-click or menu item opens the dashboard at http://127.0.0.1:7123/configure.
- Toggle "Start with Windows" in 1 click.
"""
import os
import sys
import time
import winreg
import threading
import webbrowser
import uuid
import json
import base64
import logging
import hashlib
import subprocess
import zipfile
import tempfile
import shutil
import re
import unicodedata
import struct
import traceback
import io
import collections
import concurrent.futures
import urllib.request
import urllib.parse
import urllib.error
from PIL import Image, ImageDraw
import pystray

# Ensure local skill modules can be imported
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

for base in [BUNDLE_DIR, APP_DIR]:
    sdir = os.path.join(base, "skills", "torrentx")
    if os.path.exists(sdir) and sdir not in sys.path:
        sys.path.insert(0, sdir)
    if base not in sys.path:
        sys.path.insert(0, base)

import stremio_addon

PORT = stremio_addon.PORT
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "TorrentX"

server_thread = None
http_server = None
is_running = True


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except Exception:
        return False


def set_autostart(enable=True):
    try:
        exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except Exception:
                    pass
    except Exception:
        pass


def toggle_autostart(icon, item):
    current = is_autostart_enabled()
    set_autostart(not current)


def open_dashboard(icon=None, item=None):
    webbrowser.open(f"http://127.0.0.1:{PORT}/configure")


def start_server():
    global http_server
    try:
        http_server = stremio_addon.ThreadingHTTPServer(("0.0.0.0", PORT), stremio_addon.AddonHandler)
        http_server.serve_forever()
    except Exception as e:
        with open(os.path.expanduser(r"~\torrentx_error.log"), "w", encoding="utf-8") as f:
            import traceback
            f.write(traceback.format_exc())


def restart_server(icon=None, item=None):
    global http_server, server_thread
    if http_server:
        try:
            http_server.shutdown()
            http_server.server_close()
        except Exception:
            pass
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()


def on_exit(icon, item):
    global http_server, is_running
    is_running = False
    if http_server:
        try:
            http_server.shutdown()
            http_server.server_close()
        except Exception:
            pass
    icon.stop()
    os._exit(0)


def create_tray_icon():
    for p in [os.path.join(APP_DIR, "icon.ico"), os.path.join(BUNDLE_DIR, "icon.ico")]:
        if os.path.exists(p):
            try:
                return Image.open(p)
            except Exception:
                pass

    # Fallback generated icon (Dark blue with cyan lightning bolt)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 62, 62], fill=(15, 23, 42), outline=(56, 189, 248), width=3)
    draw.polygon([(36, 8), (20, 34), (32, 34), (26, 56), (46, 28), (34, 28)], fill=(56, 189, 248))
    return img


def main():
    global server_thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Automatically open dashboard on first run
    threading.Thread(target=lambda: (time.sleep(1), open_dashboard()), daemon=True).start()

    icon_img = create_tray_icon()
    menu = pystray.Menu(
        pystray.MenuItem("⚡ فتح إعدادات TorrentX", open_dashboard, default=True),
        pystray.MenuItem("🚀 تشغيل تلقائي مع بدء ويندوز", toggle_autostart, checked=lambda item: is_autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🔄 إعادة تشغيل الخادم", restart_server),
        pystray.MenuItem("❌ إغلاق البرنامج (Exit)", on_exit),
    )

    tray_icon = pystray.Icon("TorrentX", icon_img, "TorrentX Addon Server (127.0.0.1:7123)", menu)
    tray_icon.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        with open(os.path.expanduser(r"~\torrentx_crash.log"), "w", encoding="utf-8") as f:
            import traceback
            f.write(traceback.format_exc())
        raise
