# -*- coding: utf-8 -*-
"""
TorrentX Detached Background Subtitle Sync Watcher
Monitors the Downloads directory for Aria2/Real-Debrid download completion of the target video,
then automatically invokes subtitle timing verification and waveform audio synchronization (alass/ffsubsync).
Runs as a detached process so it survives CLI termination.
"""

import os
import sys
import time

def watch_and_sync(target_filename, subtitle_path, download_dir=None):
    if not download_dir:
        download_dir = os.path.expanduser(r"~\Downloads")

    if not os.path.exists(download_dir):
        return

    # Derive stem for matching
    video_stem = os.path.splitext(target_filename)[0].lower()
    skill_dir = os.path.dirname(os.path.abspath(__file__))
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)

    # Monitor every 8 seconds for up to 25 minutes (180 iterations)
    for _ in range(180):
        time.sleep(8)
        try:
            for f in os.listdir(download_dir):
                f_lower = f.lower()
                if (video_stem in f_lower or f_lower in video_stem) and f_lower.endswith((".mkv", ".mp4", ".avi", ".ts", ".m2ts")):
                    aria2_ctl = os.path.join(download_dir, f"{f}.aria2")
                    v_full = os.path.join(download_dir, f)
                    # Check if file has finished downloading:
                    # 1. No .aria2 control file present
                    # 2. File size exceeds 30 MB
                    if not os.path.exists(aria2_ctl) and os.path.exists(v_full):
                        try:
                            f_size = os.path.getsize(v_full)
                        except OSError:
                            continue

                        if f_size > 30 * 1024 * 1024:
                            try:
                                import subtitle_manager
                                if os.path.exists(subtitle_path):
                                    subtitle_manager.verify_and_auto_sync(v_full, subtitle_path)
                            except Exception:
                                pass
                            return
        except Exception:
            pass

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        target_v = sys.argv[1]
        sub_p = sys.argv[2]
        d_dir = sys.argv[3] if len(sys.argv) > 3 else None
        watch_and_sync(target_v, sub_p, d_dir)
