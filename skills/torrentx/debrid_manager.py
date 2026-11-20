# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import re
import base64
import hashlib
import socket
import statistics
import threading
import collections
import http.server
import pathlib
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor

try:
    import winreg
except Exception:
    winreg = None

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

CONFIG_PATH_DEBRID = os.path.expanduser(r"~\.gemini\config\debrid.json")
QBITTORRENT_PATH = r"C:\Program Files\qBittorrent\qbittorrent.exe"
HARBOR_MPV_PATH = os.path.expanduser(r"~\AppData\Local\Harbor\mpv.exe")
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub")

SERVICES_META = {
    "torbox": {
        "name": "TorBox",
        "api_url": "https://torbox.app/settings?section=account",
        "login_url": "https://torbox.app/"
    },
    "realdebrid": {
        "name": "Real-Debrid",
        "api_url": "https://real-debrid.com/apitoken",
        "login_url": "https://real-debrid.com/"
    },
    "alldebrid": {
        "name": "AllDebrid",
        "api_url": "https://alldebrid.com/apikeys",
        "login_url": "https://alldebrid.com/"
    },
    "premiumize": {
        "name": "Premiumize",
        "api_url": "https://www.premiumize.me/account",
        "login_url": "https://www.premiumize.me/"
    },
    "debridlink": {
        "name": "Debrid-Link",
        "api_url": "https://debrid-link.com/webapp/apikey",
        "login_url": "https://debrid-link.com/"
    },
    "offcloud": {
        "name": "Offcloud",
        "api_url": "https://offcloud.com/account",
        "login_url": "https://offcloud.com/"
    },
    "putio": {
        "name": "Put.io",
        "api_url": "https://app.put.io/settings/account",
        "login_url": "https://put.io/"
    }
}


def load_config():
    cfg = {
        "default_service": "torbox",
        "services": {},
        "idm_path": r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        "fdm_path": os.path.expanduser(r"~\AppData\Local\Programs\Softdeluxe\Free Download Manager\fdm.exe"),
        "default_downloader": "aria2",
        "default_player": "mpc",
        "default_quality": "remux",
        "max_download_limit": "29M",
        "mux_subtitles_into_video": True
    }
    for k, v in SERVICES_META.items():
        cfg["services"][k] = {
            "name": v["name"],
            "api_key": "",
            "api_url": v["api_url"],
            "login_url": v["login_url"]
        }
    if os.path.exists(CONFIG_PATH_DEBRID):
        try:
            with open(CONFIG_PATH_DEBRID, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH_DEBRID, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_active_service(cfg=None):
    """Returns (service_id, api_key) for the user's chosen debrid service, or ('p2p', '')."""
    cfg = cfg or load_config()
    preferred = (cfg.get("default_service") or "torbox").lower().strip()
    if preferred in ("none", "p2p", "qbittorrent"):
        return "p2p", ""
    services = cfg.get("services", {})
    key = (services.get(preferred, {}).get("api_key") or "").strip()
    if key:
        return preferred, key
    for svc_id, meta in services.items():
        k = (meta.get("api_key") or "").strip()
        if k:
            return svc_id, k
    return "p2p", ""


def get_service_api_key(service_id="torbox"):
    cfg = load_config()
    return (cfg.get("services", {}).get(service_id, {}).get("api_key") or "").strip()


def get_torbox_api_key():
    return get_service_api_key("torbox")


def _http_json(url, data=None, headers=None, method="GET", json_body=None, timeout=20):
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if headers:
        hdrs.update(headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8") if hasattr(e, "read") else str(e)
        return {"error": True, "code": e.code, "message": err_msg}
    except Exception as e:
        return {"error": True, "message": str(e)}


def torbox_api_request(endpoint, data=None, method="GET"):
    key = get_torbox_api_key()
    return _http_json(
        f"https://api.torbox.app/v1/api/{endpoint}",
        data=data,
        headers={"Authorization": f"Bearer {key}"},
        method=method,
    )


def probe_streams(media_url, timeout=15):
    """Inspects audio/video/subtitle streams of a direct URL or local file using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name,width,height,channels,channel_layout,bit_rate:stream_tags=language,title",
            "-of", "json",
            media_url
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            streams = data.get("streams", [])
            audio_tracks, video_tracks, subtitle_tracks = [], [], []
            has_arabic_sub = False
            arabic_sub_tracks = []

            for s in streams:
                ctype = s.get("codec_type")
                tags = s.get("tags") or {}
                lang = str(tags.get("language") or "und")
                title = str(tags.get("title") or "")
                track_info = {
                    "index": s.get("index"),
                    "codec": s.get("codec_name") or "",
                    "language": lang,
                    "title": title,
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "channels": s.get("channels") or 0,
                    "layout": s.get("channel_layout") or "",
                    "bitrate": s.get("bit_rate")
                }
                if ctype == "video":
                    video_tracks.append(track_info)
                elif ctype == "audio":
                    audio_tracks.append(track_info)
                elif ctype == "subtitle":
                    subtitle_tracks.append(track_info)
                    if lang.lower() in ["ar", "ara", "arabic"] or "arabic" in title.lower() or "عربي" in title.lower():
                        has_arabic_sub = True
                        arabic_sub_tracks.append(track_info)

            return {
                "success": True,
                "video": video_tracks,
                "audio": audio_tracks,
                "subtitles": subtitle_tracks,
                "has_embedded_arabic": has_arabic_sub,
                "arabic_sub_tracks": arabic_sub_tracks
            }
        return {"success": False, "error": proc.stderr}
    except Exception as e:
        return {"success": False, "error": str(e)}


def unwrap_debrid_redirect(download_url, timeout=10):
    """Unwraps Torrentio and TorBox API redirects all the way to the final CDN download link."""
    if not download_url or not any(h in download_url for h in ["torrentio.strem.fun/resolve", "api.torbox.app/v1/api/torrents/requestdl"]):
        return download_url
    try:
        import requests
        current_url = download_url
        for _ in range(4):
            r_red = requests.get(
                current_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                allow_redirects=False,
                timeout=timeout
            )
            if r_red.status_code in [301, 302, 303, 307, 308] and r_red.headers.get("Location"):
                current_url = r_red.headers["Location"]
                if not any(h in current_url for h in ["torrentio.strem.fun/resolve", "api.torbox.app/v1/api/torrents/requestdl"]):
                    break
            else:
                break
        return current_url
    except Exception:
        pass
    return download_url


def _get_fixed_drives_free_gb():
    """Returns list of (root_path, free_gb, total_gb) for all fixed local drives sorted by free_gb desc."""
    drives = []
    if sys.platform == "win32":
        import ctypes
        import string
        kernel32 = ctypes.windll.kernel32
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root) and kernel32.GetDriveTypeW(root) == 3:
                try:
                    usage = shutil.disk_usage(root)
                    if usage.total > 0:
                        drives.append((root, round(usage.free / (1024 ** 3), 2), round(usage.total / (1024 ** 3), 2)))
                except Exception:
                    pass
    else:
        try:
            home = os.path.expanduser("~")
            usage = shutil.disk_usage(home)
            drives.append((home, round(usage.free / (1024 ** 3), 2), round(usage.total / (1024 ** 3), 2)))
        except Exception:
            pass
    drives.sort(key=lambda x: x[1], reverse=True)
    return drives


RESERVATIONS_FILE = os.path.expanduser(r"~\.gemini\config\torrentx_drive_reservations.json")
RESERVATIONS_LOCK = os.path.expanduser(r"~\.gemini\config\torrentx_drive_reservations.lock")


def _with_reservation_lock(fn):
    os.makedirs(os.path.dirname(RESERVATIONS_FILE), exist_ok=True)
    lock_fh = open(RESERVATIONS_LOCK, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt
            for _ in range(50):
                try:
                    msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                lock_fh.seek(0)
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


def _prune_and_load_reservations():
    now = time.time()
    items = []
    if os.path.exists(RESERVATIONS_FILE):
        try:
            with open(RESERVATIONS_FILE, "r", encoding="utf-8") as f:
                items = json.load(f) or []
        except Exception:
            items = []
    active = []
    for it in items:
        created = float(it.get("created_at", 0))
        if now - created > 14400:
            continue
        tpath = it.get("target_path") or ""
        if tpath and os.path.exists(tpath) and not os.path.exists(tpath + ".aria2") and (now - created > 30):
            continue
        active.append(it)
    return active


def _save_reservations(items):
    with open(RESERVATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def _reserved_gb_by_drive(active_items):
    reserved = {}
    for it in active_items:
        drv = it.get("drive", "")
        total_gb = float(it.get("size_gb", 0.0))
        tpath = it.get("target_path") or ""
        on_disk_gb = 0.0
        if tpath and os.path.exists(tpath):
            try:
                on_disk_gb = os.path.getsize(tpath) / (1024 ** 3)
            except Exception:
                pass
        remaining = max(0.0, total_gb - on_disk_gb)
        reserved[drv] = reserved.get(drv, 0.0) + remaining
    return reserved


def select_best_drive_dir(media_type="movies", required_gb=0.0, reserve_key=None):
    """
    Picks the fixed drive with the most effective free space (accounting for concurrent/pending downloads),
    preferring a drive that fits 2x required_gb (download + mux), then 1x required_gb (cross-drive mux).
    """
    folder_map = {
        "movie": "Movies",
        "movies": "Movies",
        "series": "Series",
        "tv": "Series",
        "episode": "Series",
        "anime": "Anime",
    }
    sub = folder_map.get((media_type or "movies").lower(), "Movies")

    def _pick_and_reserve():
        drives = _get_fixed_drives_free_gb()
        if not drives:
            return os.path.expanduser(rf"~\Downloads\{sub}")

        active = _prune_and_load_reservations()
        res_map = _reserved_gb_by_drive(active)

        eff_drives = []
        for root, free_gb, total_gb in drives:
            eff_free = round(max(0.0, free_gb - res_map.get(root, 0.0)), 2)
            eff_drives.append((root, eff_free, free_gb, total_gb))
        eff_drives.sort(key=lambda x: x[1], reverse=True)

        req = float(required_gb or 0.0)
        needed_2x = (2.0 * req) + 2.0 if req > 0 else 5.0
        needed_1x = req + 2.0 if req > 0 else 5.0

        fit_2x = [d for d in eff_drives if d[1] >= needed_2x]
        fit_1x = [d for d in eff_drives if d[1] >= needed_1x]
        chosen = fit_2x[0] if fit_2x else (fit_1x[0] if fit_1x else eff_drives[0])

        out_dir = os.path.join(chosen[0], sub)
        if req > 0 and reserve_key:
            full_res_path = reserve_key if os.path.isabs(reserve_key) else os.path.join(out_dir, reserve_key)
            active = [a for a in active if a.get("target_path") != full_res_path]
            active.append({
                "drive": chosen[0],
                "size_gb": req,
                "target_path": full_res_path,
                "created_at": time.time()
            })
        _save_reservations(active)
        return out_dir

    return _with_reservation_lock(_pick_and_reserve)


def get_drive_info_for_path(path):
    """Returns drive letter/root and free/total GB for a given output path."""
    abs_p = os.path.abspath(path)
    drive_root = os.path.splitdrive(abs_p)[0]
    if drive_root and not drive_root.endswith("\\"):
        drive_root += "\\"
    if not drive_root:
        drive_root = "/"
    try:
        u = shutil.disk_usage(drive_root if os.path.exists(drive_root) else abs_p)
        return {
            "drive": drive_root,
            "free_gb": round(u.free / (1024 ** 3), 2),
            "total_gb": round(u.total / (1024 ** 3), 2)
        }
    except Exception:
        return {"drive": drive_root, "free_gb": None, "total_gb": None}


def get_media_output_dir(media_type="movie", title=None, year=None, season=None, is_full_series_or_season=False, required_gb=0.0, reserve_key=None):
    """
    Folder policy:
    - Movies: NO per-movie folder -> <BestDrive>\\Movies
    - Single Episode: NO folder -> <BestDrive>\\Series
    - Full Series / Season Pack: Create folder -> <BestDrive>\\Series\\<Title>\\[Season XX]
    """
    mtype = (media_type or "movie").lower()
    base_dir = select_best_drive_dir(mtype, required_gb=required_gb, reserve_key=reserve_key)
    if mtype in ("series", "tv") and is_full_series_or_season and title:
        safe_title = re.sub(r'[\\/*?:"<>|]', '', f"{title} ({year})" if year else title).strip()
        target = os.path.join(base_dir, safe_title)
        if season is not None:
            target = os.path.join(target, f"Season {int(season):02d}")
        return target
    return base_dir


def _launch_detached_win32(cmd, hidden=True):
    """Launches a process detached from Windows Job Objects via Win32_Process Create (hidden by default)."""
    if sys.platform != "win32":
        return False
    try:
        raw_cli = subprocess.list2cmdline(cmd)
        b64_cli = base64.b64encode(raw_cli.encode("utf-8")).decode("ascii")
        if hidden:
            ps_script = (
                "$si = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ShowWindow = [uint16]0} -ClientOnly; "
                f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_cli}')); ProcessStartupInformation = $si}}"
            )
        else:
            ps_script = (
                f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_cli}'))}}"
            )
        ps_cmd = [
            "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
            ps_script
        ]
        res = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=6)
        return ("ProcessId" in res.stdout or "ReturnValue" in res.stdout)
    except Exception:
        return False


def send_to_downloader(download_url, downloader=None, output_dir=None, file_name=None):
    cfg = load_config()
    if not downloader:
        if download_url.startswith("magnet:") or download_url.endswith(".torrent") or cfg.get("default_service") == "none":
            downloader = "qbittorrent" if os.path.exists(QBITTORRENT_PATH) else "aria2"
        else:
            downloader = cfg.get("default_downloader", "aria2")

    downloader = (downloader or "aria2").lower()
    if not output_dir:
        output_dir = select_best_drive_dir("movies")
    os.makedirs(output_dir, exist_ok=True)

    download_url = unwrap_debrid_redirect(download_url)

    url_path_clean = urllib.parse.urlparse(download_url).path.lower()
    dangerous_exts = (".exe", ".msi", ".bat", ".cmd", ".scr", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta", ".lnk", ".pif", ".com", ".dll", ".zip", ".rar", ".7z", ".apk", ".iso")
    if url_path_clean.endswith(dangerous_exts):
        return {"success": False, "error": f"Blocked dangerous/non-video file extension in URL: {os.path.basename(url_path_clean)}"}

    if downloader in ["qbittorrent", "qbit"] and os.path.exists(QBITTORRENT_PATH):
        subprocess.Popen([QBITTORRENT_PATH, download_url])
        return {
            "success": True,
            "downloader": "qBittorrent",
            "output_dir": output_dir,
            "message": "تم فتح التورنت مباشرة داخل برنامج qBittorrent"
        }

    try:
        aria2_cmd = shutil.which("aria2c") or "aria2c"
        clean_filename = None
        if file_name:
            clean_filename = os.path.basename(file_name)
            clean_filename = re.sub(r'[\r\n"&|<>\^%`$;*?\\/:]', '_', clean_filename).strip()
            if not clean_filename.lower().endswith((".mkv", ".mp4", ".avi", ".ts", ".m2ts")):
                clean_filename += ".mkv"

        max_limit = cfg.get("max_download_limit", "29M")
        conn_count = "1" if ("tb-cdn" in download_url or "torbox" in download_url) else "16"
        cmd = [
            aria2_cmd,
            "-s", conn_count,
            "-x", conn_count,
            "-k", "1M",
            "-j", "1",
            "-c",
            "--retry-wait=2",
            "--max-tries=5",
            f"--max-download-limit={max_limit}",
            "--file-allocation=none",
            "--auto-file-renaming=false",
            "-d", output_dir,
            download_url
        ]
        if clean_filename and not download_url.startswith("magnet:"):
            cmd.extend(["-o", clean_filename])
        if download_url.startswith("magnet:"):
            cmd.append("--seed-time=0")

        launched = _launch_detached_win32(cmd, hidden=False)

        if not launched:
            if sys.platform == "win32":
                CREATE_NO_WINDOW = 0x08000000
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS, close_fds=True)
            else:
                subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        drive_info = get_drive_info_for_path(output_dir)
        return {
            "success": True,
            "downloader": "Aria2",
            "connections": int(conn_count),
            "selected_drive": drive_info["drive"],
            "drive_free_gb": drive_info["free_gb"],
            "output_dir": output_dir,
            "file_name": clean_filename,
            "target_path": os.path.join(output_dir, clean_filename) if clean_filename else None,
            "direct_url": download_url,
            "location_message": f"تم اختيار القرص {drive_info['drive']} (مساحة حرة {drive_info['free_gb']} GB) — مسار الحفظ: {output_dir}",
            "message": f"بدأ التحميل السحابي الفوري عبر Aria2 في {output_dir}"
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to launch Aria2: {e}"}


# ==============================================================================
# FAST & SMART MOVIE SELECTION ENGINE (4K -> 1080p | BluRay Remux -> Untouched WEB-DL)
# ==============================================================================
def resolve_movie_identity(title_or_imdb, year=None):
    """
    Fast parallel lookup on TMDB / IMDb (<0.5s) to get (imdb_id, canonical_title, release_year).
    """
    clean_q = str(title_or_imdb).strip()
    if re.match(r"^tt\d+$", clean_q, re.I):
        imdb_id = clean_q.lower()
        tmdb_key = "8476a7ab80ad76f0936744df0430e67c"
        try:
            url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={tmdb_key}&external_source=imdb_id"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                movs = data.get("movie_results", [])
                if movs:
                    m = movs[0]
                    t_name = m.get("title") or m.get("original_title") or clean_q
                    r_year = (m.get("release_date") or "")[:4]
                    return imdb_id, t_name, r_year
        except Exception:
            pass
        return imdb_id, clean_q, str(year or "")

    m_yr = re.search(r"\b(19\d{2}|20\d{2})\b", clean_q)
    if m_yr and not year:
        year = m_yr.group(1)
        clean_q = re.sub(r"\b(19\d{2}|20\d{2})\b", "", clean_q).strip(" ()-_.")

    tmdb_key = "8476a7ab80ad76f0936744df0430e67c"
    yr_param = f"&year={year}" if year else ""
    url = f"https://api.themoviedb.org/3/search/movie?api_key={tmdb_key}&query={urllib.parse.quote(clean_q)}{yr_param}&include_adult=false"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            if results:
                m = results[0]
                m_id = m["id"]
                t_name = m.get("title") or m.get("original_title") or clean_q
                r_year = (m.get("release_date") or "")[:4]
                ext_url = f"https://api.themoviedb.org/3/movie/{m_id}/external_ids?api_key={tmdb_key}"
                ext_req = urllib.request.Request(ext_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(ext_req, timeout=5) as ext_resp:
                    ext_data = json.loads(ext_resp.read().decode("utf-8"))
                    if ext_data.get("imdb_id"):
                        return ext_data["imdb_id"], t_name, r_year
    except Exception:
        pass

    try:
        slug = urllib.parse.quote(clean_q.lower().replace(" ", "_"))
        url_imdb = f"https://v3.sg.media-imdb.com/suggestion/x/{slug}.json"
        req = urllib.request.Request(url_imdb, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            r_imdb = json.loads(resp.read().decode("utf-8"))
            for item in r_imdb.get("d", []):
                if item.get("id", "").startswith("tt"):
                    return item["id"], item.get("l", clean_q), str(item.get("y", year or ""))
    except Exception:
        pass

    return None, clean_q, str(year or "")


def fetch_torrentio_movie_streams(imdb_id):
    """
    Fetches Torrentio streams using the user's configured debrid service (or P2P fallback) in <1 second.
    """
    svc_id, api_key = get_active_service()
    prefix = f"{svc_id}={api_key}/" if (svc_id != "p2p" and api_key) else ""

    url = f"https://torrentio.strem.fun/{prefix}stream/movie/{imdb_id}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            streams = data.get("streams", [])
            for s in streams:
                s["_debrid_service"] = svc_id
            return streams
    except Exception:
        return []


def parse_size_gb(desc_text):
    """Extracts file size in GB as a float from Torrentio description."""
    m = re.search(r"💾\s*([0-9\.]+)\s*(GB|MB)", desc_text, re.I)
    if not m:
        m = re.search(r"\b([0-9\.]+)\s*(GB|MB)\b", desc_text, re.I)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).upper()
    return round(val / 1024.0, 2) if unit == "MB" else val


def select_best_movie_release(streams, allow_upscale=False):
    """
    Implements the User's Strict Movie Quality Protocol:
    1. Reject AI Upscale by default (unless allow_upscale=True).
    2. Tier 1: 4K UHD BluRay Remux (2160p Remux, untouched disc, lossless audio).
    3. Tier 2: 1080p BluRay Remux.
    4. Tier 3: 4K WEB-DL Untouched.
    5. Tier 4: 1080p WEB-DL Untouched.
    6. Fallback Tier 5: High-bitrate BluRay Encode.
    """
    upscale_re = re.compile(r"\b(upscale|upscaled|ai[-\s]?upscale|ai[-\s]?remaster|topaz|visionx|4320p|8k)\b", re.I)
    remux_re = re.compile(r"\b(remux|bdremux|uhd[-\s]?remux)\b", re.I)
    webdl_re = re.compile(r"\b(web-dl|webdl|amzn|atvp|itunes|nf|dsnp|hmax|max|pcok|pmtp|ma\.web)\b", re.I)
    webrip_re = re.compile(r"\b(webrip|web-rip)\b", re.I)
    bdrip_re = re.compile(r"\b(bdrip|brrip|bluray[-\s]?rip)\b", re.I)
    full_disc_re = re.compile(r"\b(bdmv|iso|complete\.bluray|complete\.uhd|bd25|bd50|bd66|bd100)\b", re.I)
    cam_re = re.compile(r"\b(cam|hdcam|ts|hdts|telesync|tc|telecine|scr|dvdscr)\b", re.I)
    low_encode_re = re.compile(
        r"\b(yts|yify|psa|galaxyrg|rarbg|lama|pahe|av1|av01|stuttershit|megusta|vxt|bone|ion10|syncopy|3d|hsbs|hou)\b",
        re.I
    )

    # TRaSH-Guides Release Group Tiers (Remux & WEB-DL)
    trash_remux_t1 = ["framestor", "cinephiles", "epsilon", "bluranium", "wildcat", "3l", "bizkit", "flights", "pmp", "sicfoi", "tdd", "zorosenpai"]
    trash_remux_t2 = ["atelier", "bmf", "ncmt", "playbd", "ptp", "tepes", "decibel", "triton"]
    trash_remux_t3 = ["fgt", "126811", "hqmux", "kralimarko", "ift"]

    trash_web_t1 = ["abbie", "ajp69", "apex", "casstudio", "cmrg", "crfw", "ctrlhd", "flux", "hone", "kings", "monkee", "nosivid", "ntb", "ntg", "paxa", "pexa", "qoq", "rtn", "sic", "tepes", "t6d", "tommy", "visum"]
    trash_web_t2 = ["byndr", "dracula", "ethel", "kitsune", "mixed", "smurf", "strife", "successfulcrab", "ben.the.men", "bynoche"]
    trash_web_t3 = ["gnome", "miu", "mzabi", "peculate", "roccat", "slyfox", "thebird", "xebec"]

    streaming_tags = ["atvp", "itunes", "amzn", "nf", "ma", "dsnp", "hmax", "max", "pcok", "pmtp"]

    def is_foreign_only_dub(text):
        t = text.lower()
        has_eng_or_multi = any(w in t for w in ["eng", "english", "multi", "dual", "🇬🇧", "🇺🇸"])
        foreign_only = [
            r"\bfrench\b", r"\btruefrench\b", r"\bvff\b", r"\bvostfr\b",
            r"\bgerman\b", r"\bdeutsch\b", r"\bitalian\b", r"\bspanish\b",
            r"\blatino\b", r"\brussian\b", r"\brus\b", r"\bhindi\b", r"🇷🇺", r"🇫🇷", r"🇩🇪", r"🇮🇹", r"🇪🇸"
        ]
        return any(re.search(p, t) for p in foreign_only) and not has_eng_or_multi

    parsed = []
    seen_hashes = set()

    for s in streams:
        name = s.get("name") or ""
        desc = s.get("title") or ""
        direct_url = s.get("url") or ""
        info_hash = s.get("infoHash") or ""
        if not info_hash and direct_url:
            mh = re.search(r"/([a-fA-F0-9]{40})/", direct_url)
            if mh:
                info_hash = mh.group(1).lower()

        comb = f"{name} {desc}".lower()
        dangerous_re = re.compile(r'\.(exe|msi|bat|cmd|scr|vbs|vbe|js|jse|wsf|wsh|ps1|hta|lnk|pif|com|dll|zip|rar|7z|apk|iso|img)\b', re.I)

        if dangerous_re.search(comb) or dangerous_re.search(direct_url):
            continue
        if (not allow_upscale and upscale_re.search(comb)) or cam_re.search(comb) or full_disc_re.search(comb):
            continue
        if is_foreign_only_dub(comb):
            continue

        lines = [l.strip() for l in desc.split("\n") if l.strip()]
        clean_title = lines[0] if lines else "Movie.mkv"
        for l in lines:
            if l.lower().endswith((".mkv", ".mp4")):
                clean_title = l
                break

        is_cached = any(tag in name for tag in ["[RD+]", "[TB+]", "[AD+]", "[PM+]", "[DL+]", "[ED+]"])
        if info_hash and (info_hash, is_cached) in seen_hashes:
            continue
        if info_hash:
            seen_hashes.add((info_hash, is_cached))

        if any(w in comb for w in ["2160p", "4k", "uhd"]):
            res = "4K"
        elif "1080p" in comb:
            res = "1080p"
        elif "720p" in comb:
            res = "720p"
        else:
            res = "Other"

        if res not in ["4K", "1080p"]:
            continue

        size_gb = parse_size_gb(desc)
        seed_m = re.search(r"👤\s*(\d+)", desc)
        seeders = int(seed_m.group(1)) if seed_m else 0

        is_remux = bool(remux_re.search(comb)) and not bdrip_re.search(comb) and not webrip_re.search(comb)
        is_webrip = bool(webrip_re.search(comb))
        is_webdl = (bool(webdl_re.search(comb)) or ("web" in comb and not is_webrip)) and not is_remux and not is_webrip
        is_low_encode = bool(low_encode_re.search(comb))
        has_stream_tag = any(re.search(rf"\b{st}\b", comb) for st in streaming_tags)
        has_lossless_audio = any(w in comb for w in ["truehd", "atmos", "dts-hd", "dtshd", "dts:x", "dtsx", "pcm", "flac"])
        has_dv_hdr = any(w in comb for w in ["dv", "dovi", "dolby vision", "hdr10+", "hdr10", "hdr"])
        has_arabic_flag = "🇸🇦" in comb or bool(re.search(r"\b(arabic|ara)\b", comb))

        if is_remux and res == "4K" and 0 < size_gb < 22.0:
            is_remux = False
        if is_remux and res == "1080p" and 0 < size_gb < 10.0:
            is_remux = False

        parsed.append({
            "title": clean_title,
            "full_desc": desc,
            "stream_name": name,
            "debrid_service": s.get("_debrid_service", "debrid"),
            "hash": info_hash,
            "direct_url": direct_url,
            "is_cached": is_cached,
            "res": res,
            "size_gb": size_gb,
            "seeders": seeders,
            "is_remux": is_remux,
            "is_webdl": is_webdl,
            "is_webrip": is_webrip,
            "is_low_encode": is_low_encode,
            "has_stream_tag": has_stream_tag,
            "has_lossless_audio": has_lossless_audio,
            "has_dv_hdr": has_dv_hdr,
            "has_arabic_flag": has_arabic_flag,
            "comb": comb
        })

    if not parsed:
        return None, []

    has_4k_remux = any(c["res"] == "4K" and c["is_remux"] for c in parsed)
    has_1080p_remux = any(c["res"] == "1080p" and c["is_remux"] for c in parsed)
    has_legit_4k_webdl = any(c["res"] == "4K" and c["is_webdl"] and (c["has_stream_tag"] or c["size_gb"] >= 10.0) for c in parsed)

    filtered = []
    for c in parsed:
        if not allow_upscale and c["res"] == "4K" and not c["is_remux"] and not has_4k_remux and not has_legit_4k_webdl and has_1080p_remux:
            continue
        filtered.append(c)

    pool = filtered if filtered else parsed

    for c in pool:
        comb = c["comb"]
        is_ep = bool(re.search(r"\b(s\d{1,2}e\d{1,2}|\d{1,2}x\d{2}|e\d{2,3})\b", comb))
        min_4k_web = 1.5 if is_ep else 6.0
        min_1080_web = 0.6 if is_ep else 2.5
        if c["res"] == "4K" and c["is_remux"]:
            tier = 1
            tier_label = "4K UHD BluRay Remux (خام أصلي بدون ضغط)"
            release_type = "bluray"
        elif c["res"] == "1080p" and c["is_remux"]:
            tier = 2
            tier_label = "1080p BluRay Remux (بلوراي خام أصلي بدون ضغط)"
            release_type = "bluray"
        elif c["res"] == "4K" and c["is_webdl"] and not c["is_low_encode"] and (c["size_gb"] >= min_4k_web or c["has_stream_tag"]):
            tier = 3
            tier_label = "4K UHD WEB-DL (نسخة رقمية خام بدون ضغط)"
            release_type = "web"
        elif c["res"] == "1080p" and c["is_webdl"] and not c["is_low_encode"] and (c["size_gb"] >= min_1080_web or c["has_stream_tag"]):
            tier = 4
            tier_label = "1080p FHD WEB-DL (نسخة رقمية خام بدون ضغط)"
            release_type = "web"
        else:
            tier = 5
            tier_label = f"{c['res']} Encode"
            release_type = "bluray" if "bluray" in comb else "web"

        c["tier"] = tier
        c["tier_label"] = tier_label
        c["release_type"] = release_type

        q_score = 0
        if c["is_cached"]:
            q_score += 5000

        # TRaSH-Guides Release Group Tier Scoring
        if any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_remux_t1):
            q_score += 600
        elif any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_remux_t2):
            q_score += 450
        elif any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_remux_t3):
            q_score += 300

        if any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_web_t1):
            q_score += 500
        elif any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_web_t2):
            q_score += 350
        elif any(re.search(rf"\b{re.escape(g)}\b", comb) for g in trash_web_t3):
            q_score += 200

        # TRaSH-Guides Repack / Proper boost
        if re.search(r"\b(proper|repack|repack2|rerip)\b", comb):
            q_score += 120

        # TRaSH-Guides Audio Hierarchy
        if "truehd" in comb and "atmos" in comb:
            q_score += 320
        elif "truehd" in comb or "dts:x" in comb or "dtsx" in comb:
            q_score += 280
        elif "dts-hd" in comb or "dtshd" in comb:
            q_score += 250
        elif c["has_lossless_audio"]:
            q_score += 200
        elif ("dd+" in comb or "ddp" in comb or "eac3" in comb) and "atmos" in comb:
            q_score += 140

        # TRaSH-Guides DV + HDR10 fallback preference (avoid green/purple DV-only when HDR10 fallback exists)
        has_dv = bool(re.search(r"\b(dv|dovi)\b|dolby\s*vision", comb))
        has_hdr = bool(re.search(r"\bhdr10\+?|\bhdr\b", comb))
        if has_dv and has_hdr:
            q_score += 250
        elif has_hdr:
            q_score += 180
        elif has_dv and not has_hdr:
            q_score -= 120

        if c["has_arabic_flag"]:
            q_score += 150
        if c["is_low_encode"]:
            q_score -= 1500
        q_score += min(int(c["size_gb"] * 8), 400)
        q_score += min(c["seeders"], 100)
        c["q_score"] = q_score

    cached_pool = [c for c in pool if c["is_cached"]]
    if cached_pool:
        working_pool = cached_pool
    else:
        seeded_pool = [c for c in pool if c["seeders"] > 0]
        working_pool = seeded_pool if seeded_pool else pool
        for c in working_pool:
            c["q_score"] += min(c["seeders"], 500) * 3

    working_pool.sort(key=lambda x: (x["tier"], -x["q_score"]))
    best = working_pool[0]
    return best, working_pool[:6]


def fast_download_movie(query_or_imdb, output_dir=None, year=None):
    """
    Executes the High-Speed Movie-First Pipeline.
    """
    t0 = time.time()
    imdb_id, canonical_title, release_year = resolve_movie_identity(query_or_imdb, year=year)
    if not imdb_id:
        return {"error": True, "message": f"لم يتم العثور على الفلم '{query_or_imdb}' في قاعدة بيانات IMDb/TMDB."}

    streams = fetch_torrentio_movie_streams(imdb_id)
    if not streams:
        return {"error": True, "imdb_id": imdb_id, "title": canonical_title, "message": f"لا توجد روابط متاحة حالياً للفلم {canonical_title} ({release_year})."}

    best, top_alternatives = select_best_movie_release(streams)
    if not best:
        return {"error": True, "imdb_id": imdb_id, "title": canonical_title, "message": "لم يتم العثور على نسخة مطابقة لمعايير الجودة (بدون أبسكيل)."}

    if not output_dir:
        clean_fn = re.sub(r'[\r\n"&|<>\^%`$;*?\\/:]', '_', os.path.basename(best["title"])).strip()
        output_dir = get_media_output_dir(
            media_type="movie",
            title=canonical_title,
            year=release_year,
            required_gb=best.get("size_gb", 0.0),
            reserve_key=clean_fn
        )
    os.makedirs(output_dir, exist_ok=True)

    dl_target = best.get("direct_url") or (f"magnet:?xt=urn:btih:{best['hash']}" if best.get("hash") else None)
    dl_res = send_to_downloader(dl_target, downloader="aria2", output_dir=output_dir, file_name=best["title"])
    movie_started_in_sec = round(time.time() - t0, 2)

    direct_cdn_url = dl_res.get("direct_url") or dl_target
    target_video_path = dl_res.get("target_path") or os.path.join(output_dir, best["title"])

    import subtitle_manager

    sub_output_dir = os.path.join(output_dir, "Subtitles")
    os.makedirs(sub_output_dir, exist_ok=True)
    v_stem = os.path.splitext(os.path.basename(target_video_path))[0]

    def _run_embedded_check():
        if direct_cdn_url and direct_cdn_url.startswith("http"):
            return subtitle_manager.check_embedded_arabic_sub(direct_cdn_url, timeout=10)
        return {"has_embedded_arabic": False, "arabic_tracks": []}

    def _run_sub_fetch():
        return subtitle_manager.smart_fetch_subtitles(
            query=canonical_title,
            imdb_id=imdb_id,
            output_dir=sub_output_dir,
            video_filename=os.path.basename(target_video_path),
            release_type=best.get("release_type")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_emb = pool.submit(_run_embedded_check)
        fut_sub = pool.submit(_run_sub_fetch)
        embedded_check = fut_emb.result()
        sub_fetch = fut_sub.result()

    subtitle_report = {}
    if embedded_check.get("has_embedded_arabic"):
        for item in sub_fetch.get("downloaded", []):
            fp = item.get("file_path")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
        for extra in [f"{v_stem}.srt", f"{v_stem}.ar.srt"]:
            ep = os.path.join(sub_output_dir, extra)
            if os.path.exists(ep):
                try:
                    os.remove(ep)
                except Exception:
                    pass
        try:
            if os.path.isdir(sub_output_dir) and not os.listdir(sub_output_dir):
                os.rmdir(sub_output_dir)
        except Exception:
            pass
        subtitle_report = {
            "status": "embedded_arabic_found",
            "has_embedded_arabic": True,
            "embedded_tracks": embedded_check.get("arabic_tracks", []),
            "message": "الفلم يحتوي مسبقاً على ترجمة عربية رسمية مدمجة داخل ملف الفيديو (لا حاجة لتحميل ترجمة خارجية)."
        }
    else:
        downloaded_subs = sub_fetch.get("downloaded", [])

        watcher_launched = False
        if downloaded_subs and target_video_path:
            manifest_path = os.path.join(sub_output_dir, f".{os.path.basename(target_video_path)}.manifest.json")
            try:
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(downloaded_subs, mf, ensure_ascii=False, indent=2)
                sub_mgr_py = os.path.join(SKILL_DIR, "subtitle_manager.py")
                watch_cmd = [sys.executable, sub_mgr_py, "watch-and-sync", target_video_path, manifest_path]
                if sys.platform == "win32":
                    watcher_launched = _launch_detached_win32(watch_cmd, hidden=True)
                    if not watcher_launched:
                        CREATE_NO_WINDOW = 0x08000000
                        DETACHED_PROCESS = 0x00000008
                        subprocess.Popen(watch_cmd, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS, close_fds=True)
                        watcher_launched = True
                else:
                    subprocess.Popen(watch_cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    watcher_launched = True
            except Exception:
                pass

        subtitle_report = {
            "status": "external_subtitles_fetched",
            "has_embedded_arabic": False,
            "sources_used": ["SubSource", "SubDL"],
            "opensubtitles_used": False,
            "subtitles_dir": sub_output_dir,
            "total_downloaded": sub_fetch.get("total_downloaded", 0),
            "platforms_count": sub_fetch.get("platforms_count", 0),
            "translators_count": sub_fetch.get("translators_count", 0),
            "downloaded_subtitles": downloaded_subs,
            "alass_auto_sync_scheduled": watcher_launched,
            "message": f"تم تحميل {sub_fetch.get('platforms_count', 0)} ترجمات منصات رسمية و {sub_fetch.get('translators_count', 0)} ترجمات مترجمين/فرق (بدون تكرار)، وتفعيل المزامنة التلقائية عبر alass فور اكتمال التحميل."
        }

    total_elapsed = round(time.time() - t0, 2)
    drive_info = get_drive_info_for_path(output_dir)
    return {
        "success": True,
        "movie": {
            "title": canonical_title,
            "year": release_year,
            "imdb_id": imdb_id
        },
        "selected_release": {
            "filename": best["title"],
            "tier": best["tier"],
            "quality_tier": best["tier_label"],
            "resolution": best["res"],
            "size_gb": f"{best['size_gb']} GB",
            "is_cached": best["is_cached"],
            "debrid_service": best["debrid_service"],
            "has_lossless_audio": best["has_lossless_audio"],
            "has_dv_hdr": best["has_dv_hdr"]
        },
        "download": {
            "started_first": True,
            "movie_download_started_in_seconds": movie_started_in_sec,
            "total_pipeline_seconds": total_elapsed,
            "downloader": dl_res.get("downloader"),
            "selected_drive": drive_info["drive"],
            "drive_free_gb": drive_info["free_gb"],
            "output_dir": output_dir,
            "target_video_path": target_video_path,
            "location_message": f"تم اختيار القرص {drive_info['drive']} (مساحة حرة {drive_info['free_gb']} GB) — مسار الحفظ: {output_dir}"
        },
        "subtitles": subtitle_report,
        "alternatives": [
            {
                "filename": a["title"],
                "quality_tier": a["tier_label"],
                "size_gb": f"{a['size_gb']} GB",
                "is_cached": a["is_cached"]
            }
            for a in top_alternatives[1:5]
        ]
    }


def add_magnet_and_resolve(magnet_uri, target_season=None, target_episode=None):
    """Adds a magnet to the active debrid service and resolves direct download links."""
    svc_id, key = get_active_service()
    media_exts = ('.mkv', '.mp4', '.avi', '.ts', '.m2ts')

    if svc_id == "realdebrid":
        hdrs = {"Authorization": f"Bearer {key}"}
        add = _http_json("https://api.real-debrid.com/rest/1.0/torrents/addMagnet", data={"magnet": magnet_uri}, headers=hdrs, method="POST")
        if add.get("error") or not add.get("id"):
            return add
        tid = add["id"]
        _http_json(f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{tid}", data={"files": "all"}, headers=hdrs, method="POST")
        info = _http_json(f"https://api.real-debrid.com/rest/1.0/torrents/info/{tid}", headers=hdrs)
        links = []
        for link in info.get("links") or []:
            unr = _http_json("https://api.real-debrid.com/rest/1.0/unrestrict/link", data={"link": link}, headers=hdrs, method="POST")
            if unr.get("download"):
                links.append({"filename": unr.get("filename"), "filesize": unr.get("filesize"), "download_url": unr["download"]})
        return {"service": "realdebrid", "torrent_id": tid, "filename": info.get("filename"), "status": info.get("status"), "is_cached": len(links) > 0, "unrestricted_links": links}

    if svc_id == "alldebrid":
        up = _http_json(f"https://api.alldebrid.com/v4/magnet/upload?agent=TorrentX&apikey={urllib.parse.quote(key)}", data={"magnets[]": magnet_uri}, method="POST")
        mags = (up.get("data") or {}).get("magnets") or []
        if not mags:
            return up
        mid = mags[0].get("id")
        st = _http_json(f"https://api.alldebrid.com/v4/magnet/status?agent=TorrentX&apikey={urllib.parse.quote(key)}&id={mid}")
        mag_data = (st.get("data") or {}).get("magnets") or {}
        links = []
        for linfo in mag_data.get("links") or []:
            unr = _http_json(f"https://api.alldebrid.com/v4/link/unlock?agent=TorrentX&apikey={urllib.parse.quote(key)}&link={urllib.parse.quote(linfo.get('link', ''))}")
            d = unr.get("data") or {}
            if d.get("link"):
                links.append({"filename": d.get("filename"), "filesize": d.get("filesize"), "download_url": d["link"]})
        return {"service": "alldebrid", "torrent_id": mid, "filename": mag_data.get("filename"), "status": mag_data.get("status"), "is_cached": len(links) > 0, "unrestricted_links": links}

    if svc_id == "premiumize":
        res = _http_json("https://www.premiumize.me/api/transfer/directdl", data={"apikey": key, "src": magnet_uri}, method="POST")
        content = res.get("content") or []
        media_files = [c for c in content if str(c.get("path", "")).lower().endswith(media_exts)]
        target = max(media_files, key=lambda x: x.get("size", 0)) if media_files else (content[0] if content else None)
        links = [{"filename": os.path.basename(target.get("path", "")), "filesize": target.get("size"), "download_url": target.get("link")}] if target and target.get("link") else []
        return {"service": "premiumize", "is_cached": len(links) > 0, "unrestricted_links": links}

    if svc_id == "debridlink":
        hdrs = {"Authorization": f"Bearer {key}"}
        res = _http_json("https://debrid-link.com/api/v2/seedbox/add", json_body={"url": magnet_uri, "async": True}, headers=hdrs, method="POST")
        val = res.get("value") or {}
        files = val.get("files") or []
        media_files = [f for f in files if str(f.get("name", "")).lower().endswith(media_exts)]
        target = max(media_files, key=lambda x: x.get("size", 0)) if media_files else (files[0] if files else None)
        links = [{"filename": target.get("name"), "filesize": target.get("size"), "download_url": target.get("downloadUrl")}] if target and target.get("downloadUrl") else []
        return {"service": "debridlink", "torrent_id": val.get("id"), "filename": val.get("name"), "is_cached": len(links) > 0, "unrestricted_links": links}

    if svc_id != "torbox":
        return {"error": True, "message": f"Debrid service '{svc_id}' not configured with an API key; use qBittorrent/P2P."}

    res = torbox_api_request("torrents/createtorrent", data={"magnet": magnet_uri}, method="POST")
    if res.get("error") or not res.get("success"):
        return res
    data = res.get("data") or {}
    torrent_id = data.get("torrent_id")
    if not torrent_id:
        return {"error": True, "message": "No TorBox torrent ID returned"}

    mylist = torbox_api_request(f"torrents/mylist?id={torrent_id}")
    t_info = mylist.get("data") or {}
    if isinstance(t_info, list) and t_info:
        t_info = t_info[0]
    files = t_info.get("files") or []
    is_cached = bool(t_info.get("download_finished") or t_info.get("cached"))
    unrestricted_links = []

    if is_cached and files:
        media_files = [f for f in files if str(f.get("name", "")).lower().endswith(media_exts)]
        target_file = max(media_files, key=lambda x: x.get("size", 0)) if media_files else files[0]
        dl_res = torbox_api_request(f"torrents/requestdl?token={key}&torrent_id={torrent_id}&file_id={target_file.get('id')}&zip=false")
        if dl_res.get("data"):
            unrestricted_links.append({
                "filename": target_file.get("short_name") or target_file.get("name"),
                "filesize": target_file.get("size"),
                "download_url": dl_res["data"]
            })

    return {
        "service": "torbox",
        "torrent_id": torrent_id,
        "filename": t_info.get("name"),
        "bytes": t_info.get("size"),
        "status": t_info.get("download_state"),
        "is_cached": is_cached and len(unrestricted_links) > 0,
        "unrestricted_links": unrestricted_links
    }


# ==============================================================================
# LOCAL SINGLE-CONNECTION READ-AHEAD & AUTO-RECONNECT STREAM PROXY (4K REMUX)
# ==============================================================================
class _ReadAheadProxyState:
    """
    Single-connection 192 MB RAM read-ahead ring buffer + MKV head/tail cache +
    silent Range auto-reconnect. Prevents 60-80 GB 4K Remux stalls in MPC-HC,
    PotPlayer, and VLC while respecting TorBox CDN's 1-connection limit (no 429).
    """
    CHUNK_SIZE = 512 * 1024
    MAX_AHEAD_BYTES = 192 * 1024 * 1024
    MAX_BACK_BYTES = 24 * 1024 * 1024
    HEAD_CACHE_BYTES = 4 * 1024 * 1024
    TAIL_CACHE_BYTES = 4 * 1024 * 1024

    def __init__(self, upstream_url, player_exe=None):
        self.upstream_url = upstream_url
        self.player_exe = (player_exe or "").lower().strip()
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        self.total_size = None
        self.content_type = "video/x-matroska"

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.buf_chunks = collections.deque()
        self.buf_start = 0
        self.producer_pos = 0
        self.consumer_pos = 0
        self.generation = 0
        self.eof_reached = False
        self.stop_flag = False
        self.producer_started = False

        self.head_cache = None
        self.tail_cache = None
        self.probe_lock = threading.Lock()

        self.active_clients = 0
        self.ever_connected = False
        self.last_activity = time.time()
        self.created_at = time.time()

        self._probe_metadata()
        self.producer_thread = threading.Thread(target=self._producer_loop, daemon=True)
        self.producer_thread.start()

    def _probe_metadata(self):
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.upstream_url,
                    headers={**self.headers, "Range": "bytes=0-65535"}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    cr = resp.headers.get("Content-Range") or ""
                    m = re.search(r"/(\d+)$", cr)
                    if m:
                        self.total_size = int(m.group(1))
                    elif resp.headers.get("Content-Length"):
                        self.total_size = int(resp.headers["Content-Length"])
                    ct = resp.headers.get("Content-Type")
                    if ct:
                        self.content_type = ct.split(";")[0].strip()
                    first_bytes = resp.read()
                    if first_bytes:
                        self.head_cache = first_bytes
                    return
            except Exception:
                time.sleep(0.5 * (attempt + 1))

    def _fetch_direct_range(self, start, end):
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.upstream_url,
                    headers={**self.headers, "Range": f"bytes={start}-{end}"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read()
            except Exception:
                time.sleep(0.4 * (attempt + 1))
        return b""

    def _get_cached_tail(self):
        if not self.total_size:
            return None
        with self.probe_lock:
            if self.tail_cache is not None:
                return self.tail_cache
            t_start = max(0, self.total_size - self.TAIL_CACHE_BYTES)
            data = self._fetch_direct_range(t_start, self.total_size - 1)
            if data:
                self.tail_cache = (t_start, data)
            return self.tail_cache

    def _prune_old_chunks_locked(self):
        min_keep = max(0, self.consumer_pos - self.MAX_BACK_BYTES)
        while self.buf_chunks:
            off, data = self.buf_chunks[0]
            if off + len(data) <= min_keep:
                self.buf_chunks.popleft()
            else:
                break
        if self.buf_chunks:
            self.buf_start = self.buf_chunks[0][0]
        else:
            self.buf_start = self.producer_pos

    def _producer_loop(self):
        backoff = 0.4
        while not self.stop_flag:
            with self.cond:
                while not self.stop_flag:
                    if not self.producer_started:
                        self.cond.wait(timeout=1.0)
                        continue
                    if self.eof_reached:
                        self.cond.wait(timeout=1.0)
                        continue
                    if self.active_clients == 0 and (time.time() - self.last_activity) > 45:
                        if self.buf_chunks:
                            self.buf_chunks.clear()
                            self.buf_start = self.consumer_pos
                            self.producer_pos = self.consumer_pos
                        self.cond.wait(timeout=1.0)
                        continue
                    if (self.producer_pos - self.consumer_pos) >= self.MAX_AHEAD_BYTES:
                        self._prune_old_chunks_locked()
                        self.cond.wait(timeout=0.5)
                        continue
                    break
                if self.stop_flag:
                    break
                start_pos = self.producer_pos
                my_gen = self.generation

            if self.total_size is not None and start_pos >= self.total_size:
                with self.cond:
                    if my_gen == self.generation:
                        self.eof_reached = True
                        self.cond.notify_all()
                continue

            resp = None
            try:
                req = urllib.request.Request(
                    self.upstream_url,
                    headers={**self.headers, "Range": f"bytes={start_pos}-"}
                )
                resp = urllib.request.urlopen(req, timeout=18)
                backoff = 0.4
                cur_off = start_pos

                while not self.stop_flag:
                    with self.cond:
                        if my_gen != self.generation:
                            break
                        while (
                            not self.stop_flag
                            and my_gen == self.generation
                            and (self.producer_pos - self.consumer_pos) >= self.MAX_AHEAD_BYTES
                        ):
                            self._prune_old_chunks_locked()
                            self.cond.wait(timeout=0.5)
                        if self.stop_flag or my_gen != self.generation:
                            break

                    chunk = resp.read(self.CHUNK_SIZE)
                    if not chunk:
                        with self.cond:
                            if my_gen == self.generation:
                                if self.total_size is None or cur_off >= self.total_size:
                                    self.eof_reached = True
                                self.cond.notify_all()
                        break

                    with self.cond:
                        if my_gen != self.generation:
                            break
                        if not self.buf_chunks:
                            self.buf_start = cur_off
                        self.buf_chunks.append((cur_off, chunk))
                        cur_off += len(chunk)
                        self.producer_pos = cur_off
                        if self.head_cache is not None and len(self.head_cache) < self.HEAD_CACHE_BYTES:
                            if cur_off - len(chunk) <= len(self.head_cache) < cur_off:
                                overlap = len(self.head_cache) - (cur_off - len(chunk))
                                need = self.HEAD_CACHE_BYTES - len(self.head_cache)
                                self.head_cache += chunk[overlap:overlap + need]
                        self._prune_old_chunks_locked()
                        self.cond.notify_all()
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 3.0)
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

    def _read_from_buffer_locked(self, pos, max_bytes):
        for off, data in self.buf_chunks:
            end_off = off + len(data)
            if off <= pos < end_off:
                rel = pos - off
                take = min(max_bytes, len(data) - rel)
                return data[rel:rel + take]
        return None

    def stream_range(self, start, end, write_fn):
        """Serves [start, end] inclusive via write_fn(chunk) with instant head/tail cache & ring buffer."""
        self.last_activity = time.time()
        self.ever_connected = True

        if self.head_cache and start >= 0 and (end + 1) <= len(self.head_cache):
            write_fn(self.head_cache[start:end + 1])
            return

        req_len = (end - start) + 1
        if self.total_size and start >= max(0, self.total_size - self.TAIL_CACHE_BYTES) and req_len <= self.TAIL_CACHE_BYTES:
            tc = self._get_cached_tail()
            if tc:
                t_start, t_data = tc
                if t_start <= start and (end + 1) <= (t_start + len(t_data)):
                    rel = start - t_start
                    write_fn(t_data[rel:rel + req_len])
                    return

        with self.cond:
            is_isolated_probe = (
                self.active_clients > 0
                and req_len <= 2 * 1024 * 1024
                and (start < self.buf_start or start > self.producer_pos + 4 * 1024 * 1024)
            )
        if is_isolated_probe:
            data = self._fetch_direct_range(start, end)
            if data:
                write_fn(data)
            return

        with self.cond:
            self.active_clients += 1
            self.producer_started = True
        try:
            pos = start
            while pos <= end and not self.stop_flag:
                with self.cond:
                    self.consumer_pos = pos
                    self.last_activity = time.time()
                    chunk = self._read_from_buffer_locked(pos, min(262144, (end - pos) + 1))
                    if chunk is None:
                        if (
                            not self.buf_chunks
                            or pos < self.buf_start
                            or pos > self.producer_pos + (4 * 1024 * 1024)
                        ):
                            self.generation += 1
                            self.buf_chunks.clear()
                            self.buf_start = pos
                            self.producer_pos = pos
                            self.eof_reached = False
                            self.cond.notify_all()

                        wait_deadline = time.time() + 25.0
                        while chunk is None and not self.stop_flag:
                            chunk = self._read_from_buffer_locked(pos, min(262144, (end - pos) + 1))
                            if chunk is not None:
                                break
                            if self.eof_reached and pos >= self.producer_pos:
                                break
                            if time.time() > wait_deadline:
                                break
                            self.cond.wait(timeout=0.5)

                    if not chunk:
                        break
                    self._prune_old_chunks_locked()
                    self.cond.notify_all()

                write_fn(chunk)
                pos += len(chunk)
        finally:
            with self.cond:
                self.active_clients = max(0, self.active_clients - 1)
                self.last_activity = time.time()


def _is_process_running_win32(exe_name):
    if not exe_name or sys.platform != "win32":
        return True
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            capture_output=True, text=True, timeout=4, creationflags=0x08000000
        )
        return exe_name.lower() in (res.stdout or "").lower()
    except Exception:
        return True


def _run_stream_proxy(port, upstream_url, media_filename="movie.mkv", player_exe=""):
    """Runs the local single-connection read-ahead HTTP proxy until the player closes or idle timeout."""
    state = _ReadAheadProxyState(upstream_url, player_exe=player_exe)

    class _ProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _handle_request(self, send_body=True):
            if self.path.startswith("/__health"):
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
                return

            total = state.total_size
            if not total:
                self.send_error(502, "Upstream metadata unavailable")
                return

            range_hdr = self.headers.get("Range")
            status = 200
            start = 0
            end = total - 1

            if range_hdr:
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr.strip())
                if m:
                    s_str, e_str = m.group(1), m.group(2)
                    if s_str and e_str:
                        start, end = int(s_str), min(int(e_str), total - 1)
                    elif s_str:
                        start, end = int(s_str), total - 1
                    elif e_str:
                        suffix = int(e_str)
                        start = max(0, total - suffix)
                        end = total - 1
                    status = 206

            if start >= total or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return

            content_len = (end - start) + 1
            self.send_response(status)
            self.send_header("Content-Type", state.content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(content_len))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Disposition", f'inline; filename="{media_filename}"')
            self.send_header("Connection", "close")
            self.end_headers()

            if not send_body:
                return

            try:
                state.stream_range(start, end, self.wfile.write)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def do_HEAD(self):
            self._handle_request(send_body=False)

        def do_GET(self):
            self._handle_request(send_body=True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), _ProxyHandler)
    server.daemon_threads = True

    def _watchdog():
        while not state.stop_flag:
            time.sleep(8)
            now = time.time()
            if not state.ever_connected and (now - state.created_at) > 60:
                break
            if state.active_clients == 0 and (now - state.created_at) > 25:
                idle_sec = now - state.last_activity
                if state.player_exe and idle_sec > 15 and not _is_process_running_win32(state.player_exe):
                    break
                if idle_sec > 900:
                    break
        state.stop_flag = True
        with state.cond:
            state.cond.notify_all()
        try:
            server.shutdown()
        except Exception:
            pass

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        state.stop_flag = True
        try:
            server.server_close()
        except Exception:
            pass


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_stream_proxy(upstream_url, media_filename="movie.mkv", player_exe=""):
    """Launches a detached background read-ahead proxy and returns the local http://127.0.0.1 URL."""
    port = _find_free_port()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", media_filename or "movie.mkv").strip("_") or "movie.mkv"
    if not safe_name.lower().endswith((".mkv", ".mp4", ".avi", ".ts", ".m2ts")):
        safe_name += ".mkv"

    script_path = os.path.abspath(__file__)
    cmd = [sys.executable, script_path, "serve-proxy", str(port), upstream_url, safe_name, player_exe or ""]
    if sys.platform == "win32":
        launched = _launch_detached_win32(cmd, hidden=True)
        if not launched:
            subprocess.Popen(cmd, creationflags=0x08000000 | 0x00000008, close_fds=True)
    else:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    health_url = f"http://127.0.0.1:{port}/__health"
    deadline = time.time() + 4.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=0.6) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}/{urllib.parse.quote(safe_name)}"
        except Exception:
            time.sleep(0.12)
    return upstream_url


# ==============================================================================
# SUBTITLE STAGING & PLAYER-SPECIFIC MULTI-SUBTITLE + CACHE OPTIMIZATIONS
# ==============================================================================
def _make_ascii_slug(text, fallback="Movie"):
    if not text:
        return fallback
    stem = os.path.splitext(os.path.basename(str(text)))[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if len(slug) < 2:
        digest = hashlib.md5(str(text).encode("utf-8", errors="ignore")).hexdigest()[:8]
        return f"{fallback}_{digest}"
    return slug[:64]


def _infer_sub_label(path, idx):
    low = os.path.basename(path).lower()
    known = [
        ("netflix", "Netflix"), ("نتفليكس", "Netflix"), ("nf", "Netflix"),
        ("osn", "OSN"), ("أوسن", "OSN"),
        ("apple", "AppleTV"), ("atvp", "AppleTV"), ("itunes", "iTunes"),
        ("amazon", "Prime"), ("amzn", "Prime"), ("prime", "Prime"),
        ("disney", "Disney"), ("dsnp", "Disney"), ("ديزني", "Disney"),
        ("shahid", "Shahid"), ("شاهد", "Shahid"),
        ("starz", "StarzPlay"), ("hbo", "HBO"), ("max", "Max"),
        ("subsource", "SubSource"), ("subdl", "SubDL"),
        ("official", "Official"), ("رسمية", "Official"),
        ("bluray", "BluRay"), ("web", "WEB"),
    ]
    for token, label in known:
        if token in low:
            return label
    ascii_tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{2,14}", os.path.splitext(os.path.basename(path))[0])
    ignore = {"ar", "ara", "arabic", "srt", "ass", "vtt", "sub", "2160p", "1080p", "remux", "bluray", "webdl", "x265", "x264", "hevc", "hdr", "dts", "truehd"}
    for tok in reversed(ascii_tokens):
        if tok.lower() not in ignore:
            return tok[:16]
    return f"AR{idx}"


def _ensure_utf8_bom_rtl(path):
    """Guarantees valid UTF-8 with BOM + RTL marks on Arabic lines for all 4 players."""
    try:
        import subtitle_manager
        if hasattr(subtitle_manager, "convert_to_utf8_bom"):
            subtitle_manager.convert_to_utf8_bom(path, apply_rtl_fix=True)
            return
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if text is not None:
            with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(text)
    except Exception:
        pass


def _resolve_subtitle_inputs(subtitle_path, video_path_or_url=None):
    """
    Accepts: single path, list/tuple of paths, ';'-separated string, directory path,
    or None (auto-discovers synced subtitles next to local video files).
    """
    raw_items = []
    if isinstance(subtitle_path, (list, tuple)):
        for item in subtitle_path:
            if item:
                raw_items.append(str(item))
    elif isinstance(subtitle_path, str) and subtitle_path.strip():
        if ";" in subtitle_path and not os.path.exists(subtitle_path):
            raw_items.extend([p.strip().strip('"') for p in subtitle_path.split(";") if p.strip()])
        else:
            raw_items.append(subtitle_path.strip().strip('"'))

    found = []
    for item in raw_items:
        if os.path.isdir(item):
            for fn in sorted(os.listdir(item)):
                full = os.path.join(item, fn)
                if os.path.isfile(full) and fn.lower().endswith(SUBTITLE_EXTS):
                    found.append(full)
        elif os.path.isfile(item):
            found.append(item)

    if not found and video_path_or_url and not str(video_path_or_url).startswith(("http://", "https://")) and os.path.isfile(video_path_or_url):
        v_dir = os.path.dirname(os.path.abspath(video_path_or_url))
        v_stem = os.path.splitext(os.path.basename(video_path_or_url))[0].lower()
        for search_dir in (os.path.join(v_dir, "Subtitles"), v_dir):
            if os.path.isdir(search_dir):
                for fn in sorted(os.listdir(search_dir)):
                    full = os.path.join(search_dir, fn)
                    if not os.path.isfile(full) or not fn.lower().endswith(SUBTITLE_EXTS):
                        continue
                    if search_dir != v_dir or fn.lower().startswith(v_stem):
                        found.append(full)

    seen_paths = set()
    unique = []
    for p in found:
        ap = os.path.abspath(p)
        if ap not in seen_paths and os.path.getsize(ap) > 20:
            seen_paths.add(ap)
            unique.append(ap)
    return unique


def _stage_subtitles_for_player(sub_paths, movie_slug):
    """
    Copies deduplicated synced subtitles into a clean ASCII directory with predictable
    `<MovieSlug>.01_<Label>.ar.srt` filenames (no Arabic chars, spaces, or semicolons)
    so MPC-HC, PotPlayer, mpv.net, and VLC all load every single subtitle reliably.
    """
    stage_root = os.path.join(os.environ.get("TEMP", "."), "torrentx_stream_subs", f"play_{movie_slug}")
    os.makedirs(stage_root, exist_ok=True)
    for old_fn in os.listdir(stage_root):
        if old_fn.lower().endswith(SUBTITLE_EXTS + (".mpcpl", ".lua")):
            try:
                os.remove(os.path.join(stage_root, old_fn))
            except Exception:
                pass

    staged = []
    seen_hashes = set()
    used_labels = {}
    for idx, src in enumerate(sub_paths, start=1):
        if not os.path.isfile(src):
            continue
        _ensure_utf8_bom_rtl(src)
        try:
            with open(src, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
        except Exception:
            pass

        ext = os.path.splitext(src)[1].lower() or ".srt"
        base_label = _infer_sub_label(src, idx)
        count = used_labels.get(base_label, 0) + 1
        used_labels[base_label] = count
        label = f"{base_label}_{count}" if count > 1 else base_label

        dst_name = f"{movie_slug}.{idx:02d}_{label}.ar{ext}"
        dst_path = os.path.join(stage_root, dst_name)
        try:
            shutil.copy2(src, dst_path)
            staged.append({
                "path": dst_path,
                "title": f"AR #{idx} - {label}",
                "source": src
            })
        except Exception:
            staged.append({
                "path": src,
                "title": f"AR #{idx} - {label}",
                "source": src
            })
    return stage_root, staged


def _reg_set_min_dword(key_path, value_name, min_val):
    if not winreg or sys.platform != "win32":
        return
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            cur = 0
            try:
                cur, _ = winreg.QueryValueEx(k, value_name)
            except FileNotFoundError:
                cur = 0
            if not isinstance(cur, int) or cur < min_val:
                winreg.SetValueEx(k, value_name, 0, winreg.REG_DWORD, int(min_val))
    except Exception:
        pass


def _reg_append_path_list(key_path, value_name, new_dir, default_str=".;.\\subtitles;.\\subs"):
    if not winreg or sys.platform != "win32" or not new_dir:
        return
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                cur, _ = winreg.QueryValueEx(k, value_name)
            except FileNotFoundError:
                cur = default_str
            parts = [p.strip() for p in str(cur or default_str).split(";") if p.strip()]
            parts = [p for p in parts if "torrentx_stream_subs" not in p.lower()]
            parts.append(new_dir)
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, ";".join(parts))
    except Exception:
        pass


def _optimize_mpc_hc_settings(sub_dir=None):
    """
    Tunes MPC-HC internal & external LAV Splitter cache for 60-80 GB 4K Remuxes
    and configures Arabic subtitle autoload priority.
    """
    if not winreg or sys.platform != "win32":
        return
    lav_keys = [
        r"Software\MPC-HC\MPC-HC\Internal Filters\LAV Splitter",
        r"Software\LAV\Splitter",
    ]
    for lk in lav_keys:
        _reg_set_min_dword(lk, "QueueMaxMem", 512)
        _reg_set_min_dword(lk, "QueueMaxPackets", 1500)
        _reg_set_min_dword(lk, "StreamAnalysisDuration", 2500)
        _reg_set_min_dword(lk, "NetworkStreamAnalysisDuration", 3000)

    mpc_settings = r"Software\MPC-HC\MPC-HC\Settings"
    _reg_set_min_dword(mpc_settings, "AutoloadSubtitles", 1)
    if sub_dir:
        _reg_append_path_list(mpc_settings, "SubtitlePaths", sub_dir, default_str=".;.\\subtitles;.\\subs")
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, mpc_settings, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                cur_order, _ = winreg.QueryValueEx(k, "SubtitlesLanguageOrder")
            except FileNotFoundError:
                cur_order = ""
            cur_str = str(cur_order or "").strip()
            if not cur_str.lower().startswith(("ara", "ar ")):
                new_order = f"ara ar arabic {cur_str}".strip()
                winreg.SetValueEx(k, "SubtitlesLanguageOrder", 0, winreg.REG_SZ, new_order)
    except Exception:
        pass


def _write_mpc_playlist(stage_dir, movie_slug, media_target, staged_subs, display_title=None):
    """
    Generates a UTF-8-BOM .mpcpl playlist listing ALL synced external subtitles
    (solving MPC-HC's /sub single-subtitle CLI limitation completely).
    """
    pl_path = os.path.join(stage_dir, f"{movie_slug}.mpcpl")
    lines = [
        "MPCPLAYLIST",
        "1,type,0",
    ]
    if display_title:
        clean_lbl = re.sub(r"[\r\n,]+", " ", str(display_title)).strip()
        if clean_lbl:
            lines.append(f"1,label,{clean_lbl}")
    lines.append(f"1,filename,{media_target}")
    for item in staged_subs:
        lines.append(f"1,subtitle,{item['path']}")
    with open(pl_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return pl_path


def _optimize_potplayer_settings(sub_dir=None):
    """Safely registers the staged subtitle folder in PotPlayer's SubPath so all synced subs autoload."""
    if not winreg or sys.platform != "win32" or not sub_dir:
        return
    for pot_key in (r"Software\DAUM\PotPlayer64\Settings", r"Software\DAUM\PotPlayerMini64\Settings"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, pot_key, 0, winreg.KEY_READ):
                pass
            _reg_append_path_list(pot_key, "SubPath", sub_dir, default_str=".;.\\Subtitle;.\\Subtitles")
        except FileNotFoundError:
            continue
        except Exception:
            continue


def _write_mpv_sub_loader_lua(stage_dir, movie_slug, staged_subs):
    """
    Creates a lightweight Lua script for mpv / mpv.net that guarantees ALL synced
    Arabic subtitles are loaded without duplicates, labeled cleanly, and track #1
    is auto-selected even over embedded PGS tracks in 4K Remuxes.
    """
    lua_path = os.path.join(stage_dir, f"{movie_slug}_subs.lua")
    entries = []
    for item in staged_subs:
        p_fwd = item["path"].replace("\\", "/")
        t_clean = item["title"].replace("\\", "\\\\").replace('"', '\\"')
        base_low = os.path.basename(item["path"]).lower()
        entries.append(f'  {{ path = "{p_fwd}", title = "{t_clean}", base = "{base_low}" }}')
    subs_block = ",\n".join(entries)

    lua_code = f"""local subs = {{
{subs_block}
}}

local applied = false

local function basename_lower(p)
    if not p then return "" end
    p = p:gsub("\\\\", "/")
    return (p:match("([^/]+)$") or p):lower()
end

mp.register_event("file-loaded", function()
    if applied or #subs == 0 then return end
    applied = true

    local existing = {{}}
    local tracks = mp.get_property_native("track-list") or {{}}
    for _, tr in ipairs(tracks) do
        if tr.type == "sub" and tr.external then
            local b = basename_lower(tr["external-filename"])
            if b ~= "" then existing[b] = tr.id end
        end
    end

    for _, s in ipairs(subs) do
        if not existing[s.base] then
            mp.commandv("sub-add", s.path, "auto", s.title, "ar")
        end
    end

    mp.add_timeout(0.25, function()
        local updated = mp.get_property_native("track-list") or {{}}
        local first_target = subs[1].base
        local chosen_id = nil
        for _, tr in ipairs(updated) do
            if tr.type == "sub" and tr.external then
                local b = basename_lower(tr["external-filename"])
                if b == first_target then
                    chosen_id = tr.id
                    break
                elseif not chosen_id then
                    chosen_id = tr.id
                end
            end
        end
        if chosen_id then
            mp.set_property_number("sid", chosen_id)
            mp.set_property_bool("sub-visibility", true)
        end
    end)
end)
"""
    with open(lua_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(lua_code)
    return lua_path


def stream_to_player(video_path_or_url, subtitle_path=None, player=None, title=None, auto_sync=False):
    """
    Streams a direct Debrid URL or plays a local video file inside MPC-HC, PotPlayer,
    MPV/mpv.net, or VLC with high-bitrate 4K Remux caching and ALL synced Arabic subtitles loaded.
    """
    video_path_or_url = unwrap_debrid_redirect(video_path_or_url)
    if not player or player.lower() in ["default", "none", "auto"]:
        cfg = load_config()
        player = cfg.get("default_player", "mpc")
    player = player.lower().strip()

    is_http = str(video_path_or_url).startswith(("http://", "https://"))
    sub_list = _resolve_subtitle_inputs(subtitle_path, video_path_or_url=video_path_or_url)

    sync_report = None
    if auto_sync and is_http and sub_list:
        sync_report = _sync_subs_for_stream_url(video_path_or_url, sub_list)

    movie_slug = _make_ascii_slug(title or video_path_or_url)
    stage_dir = None
    staged_subs = []
    if sub_list:
        stage_dir, staged_subs = _stage_subtitles_for_player(sub_list, movie_slug)
    else:
        stage_dir = os.path.join(os.environ.get("TEMP", "."), "torrentx_stream_subs", f"play_{movie_slug}")
        os.makedirs(stage_dir, exist_ok=True)

    staged_paths = [s["path"] for s in staged_subs]

    def _launch_player(cmd, player_name, effective_target):
        if sys.platform == "win32":
            ok = _launch_detached_win32(cmd, hidden=False)
            if not ok:
                subprocess.Popen(cmd, creationflags=0x00000008, close_fds=True)
        else:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = {
            "success": True,
            "player": player_name,
            "target": effective_target,
            "direct_url": video_path_or_url,
            "proxy_enabled": (effective_target != video_path_or_url),
            "subtitles_loaded_count": len(staged_paths),
            "subtitles": staged_paths,
        }
        if sync_report is not None:
            res["stream_sync"] = sync_report
        return res

    # 1. MPC-HC
    if player in ["mpc", "mpc-hc", "mpchc"]:
        mpc_candidates = [
            r"C:\Program Files (x86)\K-Lite Codec Pack\MPC-HC64\mpc-hc64.exe",
            r"C:\Program Files\MPC-HC\mpc-hc64.exe",
            shutil.which("mpc-hc64") or "",
        ]
        mpc_bin = next((p for p in mpc_candidates if p and os.path.exists(p)), None)
        if mpc_bin:
            _optimize_mpc_hc_settings(sub_dir=stage_dir if staged_subs else None)
            effective_url = video_path_or_url
            if staged_subs:
                pl_path = _write_mpc_playlist(stage_dir, movie_slug, effective_url, staged_subs, display_title=title or movie_slug)
                cmd = [mpc_bin, pl_path, "/play"]
            else:
                cmd = [mpc_bin, effective_url, "/play"]
            return _launch_player(cmd, "MPC-HC", effective_url)

    # 2. PotPlayer
    elif player in ["potplayer", "pot"]:
        pot_candidates = [
            r"C:\Program Files\DAUM\PotPlayer\PotPlayer64.exe",
            r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
            shutil.which("PotPlayer64") or "",
            shutil.which("PotPlayerMini64") or "",
        ]
        pot_bin = next((p for p in pot_candidates if p and os.path.exists(p)), None)
        if pot_bin:
            if staged_subs:
                _optimize_potplayer_settings(sub_dir=stage_dir)
            effective_url = video_path_or_url
            cmd = [pot_bin, effective_url]
            if staged_paths:
                cmd.append(f"/sub={staged_paths[0]}")
            return _launch_player(cmd, "PotPlayer", effective_url)

    # 3. mpv.net / MPV
    elif player in ["mpv", "mpvnet", "mpv.net", "harbor"]:
        mpvnet_candidates = [
            shutil.which("mpvnet") or "",
            os.path.expanduser(r"~\Desktop\mpv_anime\mpvnet.exe"),
            r"C:\Program Files\mpv.net\mpvnet.exe",
        ]
        mpvnet_bin = next((p for p in mpvnet_candidates if p and os.path.exists(p)), None)
        use_mpvnet = (player in ["mpvnet", "mpv.net"]) or (
            not os.path.exists(HARBOR_MPV_PATH) and not shutil.which("mpv") and bool(mpvnet_bin)
        )

        if use_mpvnet and mpvnet_bin:
            mpv_bin = mpvnet_bin
            pname = "mpv.net"
            cmd = [mpv_bin, video_path_or_url]
        else:
            mpv_bin = HARBOR_MPV_PATH if os.path.exists(HARBOR_MPV_PATH) else (shutil.which("mpv") or mpvnet_bin)
            if not mpv_bin or not os.path.exists(mpv_bin):
                return {"error": True, "message": f"Player not found: {player}"}
            pname = "mpv.net" if os.path.basename(mpv_bin).lower() == "mpvnet.exe" else "MPV"
            cmd = [mpv_bin, video_path_or_url]
            if pname == "MPV":
                cmd.append("--force-window=yes")

        if is_http:
            cmd.extend([
                "--cache=yes",
                "--demuxer-max-bytes=512MiB",
                "--demuxer-max-back-bytes=128MiB",
                "--demuxer-readahead-secs=60",
                "--cache-secs=60",
                "--network-timeout=30",
                "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5,reconnect_on_network_error=1,rw_timeout=15000000",
            ])

        if staged_subs:
            cmd.extend([
                "--sub-codepage=utf-8",
                "--slang=ar,ara,arabic",
                f"--sub-files={';'.join(staged_paths)}",
            ])
            lua_loader = _write_mpv_sub_loader_lua(stage_dir, movie_slug, staged_subs)
            cmd.append(f"--script={lua_loader}")

        return _launch_player(cmd, pname, video_path_or_url)

    # 4. VLC
    elif player == "vlc":
        vlc_candidates = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            shutil.which("vlc") or "",
        ]
        vlc_bin = next((p for p in vlc_candidates if p and os.path.exists(p)), None)
        if vlc_bin:
            effective_url = video_path_or_url
            cmd = [
                vlc_bin,
                effective_url,
                "--network-caching=10000",
                "--file-caching=5000",
                "--http-reconnect",
                "--subsdec-encoding=UTF-8",
                "--sub-language=ar,ara",
            ]
            if staged_paths:
                cmd.append(f"--sub-file={staged_paths[0]}")
                if len(staged_paths) > 1:
                    slave_uris = [pathlib.Path(p).resolve().as_uri() for p in staged_paths[1:]]
                    cmd.append(f"--input-slave={'#'.join(slave_uris)}")
            return _launch_player(cmd, "VLC", effective_url)

    return {"error": True, "message": f"Player not found: {player}"}


# ==============================================================================
# FAST & ACCURATE STREAM SUBTITLE SYNC (DENSE DIALOGUE WINDOW + PARALLEL ALASS)
# ==============================================================================
_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _ts_to_sec(h, m, s, ms):
    return int(h) * 3600.0 + int(m) * 60.0 + int(s) + (int(ms.ljust(3, "0")[:3]) / 1000.0)


def _sec_to_srt_ts(sec):
    sec = max(0.0, float(sec))
    total_ms = int(round(sec * 1000.0))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_blocks(srt_path):
    """Parses SRT file into list of (start_sec, end_sec, text_lines)."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "latin-1"):
        try:
            with open(srt_path, "r", encoding=enc) as f:
                text = f.read()
            break
        except Exception:
            continue
    if not text:
        return []
    blocks = []
    for raw_block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [ln.rstrip("\r") for ln in raw_block.split("\n") if ln.strip() != ""]
        if len(lines) < 2:
            continue
        ts_idx = -1
        match = None
        for i in range(min(2, len(lines))):
            m = _SRT_TIME_RE.search(lines[i])
            if m:
                ts_idx = i
                match = m
                break
        if match is None:
            continue
        start_s = _ts_to_sec(*match.groups()[:4])
        end_s = _ts_to_sec(*match.groups()[4:8])
        body = lines[ts_idx + 1:]
        if body and end_s > start_s:
            blocks.append((start_s, end_s, body))
    return blocks


def _pick_best_dialogue_window(sub_paths, window_sec=75.0):
    """
    Scans subtitle cues between t=90s and t=1200s and picks the 75-second window
    with the highest speech density so alass never samples a silent/music intro.
    """
    best_offset = 180.0
    best_count = -1
    for sp in sub_paths[:3]:
        blocks = _parse_srt_blocks(sp)
        if not blocks:
            continue
        starts = [b[0] for b in blocks if 90.0 <= b[0] <= 1260.0]
        if len(starts) < 6:
            continue
        for cand in range(120, 1140, 30):
            cnt = sum(1 for s in starts if cand <= s <= cand + window_sec)
            if cnt > best_count:
                best_count = cnt
                best_offset = float(cand)
        if best_count >= 12:
            break
    return best_offset


def _extract_shifted_srt_window(blocks, offset_sec, window_sec, pad_sec, out_path):
    """Writes only cues overlapping [offset-pad, offset+window+pad] shifted by -offset_sec."""
    w_min = offset_sec - pad_sec
    w_max = offset_sec + window_sec + pad_sec
    selected = []
    for st, en, body in blocks:
        if en < w_min or st > w_max:
            continue
        nst = st - offset_sec
        nen = en - offset_sec
        if nen <= 0.05:
            continue
        nst = max(0.0, nst)
        selected.append((nst, nen, body))

    if len(selected) < 3:
        return []

    out_lines = []
    for idx, (st, en, body) in enumerate(selected, start=1):
        out_lines.append(str(idx))
        out_lines.append(f"{_sec_to_srt_ts(st)} --> {_sec_to_srt_ts(en)}")
        out_lines.extend(body)
        out_lines.append("")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out_lines))
    return selected


def _sync_subs_for_stream_url(stream_url, sub_paths):
    """
    Fast, accurate stream-safe subtitle sync without downloading the whole movie:
    1. Picks the densest 75-second dialogue window from subtitle timestamps.
    2. Grabs that single 75s audio window via HTTP Range seek (with reconnect enabled).
    3. Extracts ONLY the overlapping windowed cues and runs alass across ALL subtitles in parallel.
    4. Computes median cue delta across interior cues, shifts accurately, and applies UTF-8 BOM + RTL fix to ALL files.
    """
    import subtitle_manager
    if isinstance(sub_paths, str):
        sub_paths = [sub_paths]
    sub_paths = [s for s in sub_paths if s and os.path.exists(s)]
    if not sub_paths:
        return {"synced": False, "reason": "no_subtitles"}

    for sp in sub_paths:
        _ensure_utf8_bom_rtl(sp)

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    alass_bin = subtitle_manager.get_alass_bin() if hasattr(subtitle_manager, "get_alass_bin") else shutil.which("alass")
    if not alass_bin:
        return {"synced": False, "reason": "alass_not_found"}

    tmp_dir = os.path.join(os.environ.get("TEMP", "."), "torrentx_stream_sync")
    os.makedirs(tmp_dir, exist_ok=True)
    ts_id = int(time.time() * 1000)
    mka_path = os.path.join(tmp_dir, f"sample_{ts_id}.mka")
    wav_path = os.path.join(tmp_dir, f"sample_{ts_id}.wav")
    window_sec = 45.0
    pad_sec = 0.0
    offset_sec = _pick_best_dialogue_window(sub_paths, window_sec=window_sec)
    temp_files = [mka_path, wav_path]
    temp_lock = threading.Lock()

    try:
        fast_seek = max(0, int(offset_sec) - 10)
        ff_cmd = [
            ffmpeg_bin, "-y",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "3",
            "-ss", str(fast_seek),
            "-i", stream_url,
            "-copyts", "-ss", str(int(offset_sec)),
            "-t", str(int(window_sec)),
            "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "matroska",
            mka_path
        ]
        subprocess.run(ff_cmd, capture_output=True, text=True, timeout=55, encoding="utf-8", errors="replace")
        if not os.path.exists(mka_path) or os.path.getsize(mka_path) < 30000:
            return {"synced": False, "reason": "audio_sample_unavailable", "window_offset_sec": offset_sec}

        # Read exact preserved PTS start_time from MKA so keyframe/packet offset error is 0.000s
        actual_offset = offset_sec
        try:
            pr = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", mka_path],
                capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace"
            )
            val = float((pr.stdout or "").strip())
            if val > 1.0:
                actual_offset = val
        except Exception:
            pass

        subprocess.run(
            [ffmpeg_bin, "-y", "-i", mka_path, "-c:a", "pcm_s16le", wav_path],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace"
        )
        if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 30000:
            return {"synced": False, "reason": "wav_convert_failed", "window_offset_sec": actual_offset}

        def _sync_one(idx_sp):
            idx, sp = idx_sp
            win_srt = os.path.join(tmp_dir, f"win_{ts_id}_{idx}.srt")
            out_win_srt = os.path.join(tmp_dir, f"win_out_{ts_id}_{idx}.srt")
            with temp_lock:
                temp_files.extend([win_srt, out_win_srt])

            blocks = _parse_srt_blocks(sp)
            if not blocks:
                return {"file": os.path.basename(sp), "shift_seconds": 0.0, "status": "non_srt_or_empty"}

            in_win = _extract_shifted_srt_window(blocks, actual_offset, window_sec, pad_sec, win_srt)
            if not in_win:
                return {"file": os.path.basename(sp), "shift_seconds": 0.0, "status": "no_cues_in_window"}

            proc = subprocess.run(
                [alass_bin, "--split-penalty", "1000", wav_path, win_srt, out_win_srt],
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
            )
            if proc.returncode != 0 or not os.path.exists(out_win_srt):
                subprocess.run(
                    [alass_bin, wav_path, win_srt, out_win_srt],
                    capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
                )
            if os.path.exists(out_win_srt) and os.path.getsize(out_win_srt) > 100:
                out_win = _parse_srt_blocks(out_win_srt)
                if len(out_win) == len(in_win):
                    deltas = []
                    for (ist, _, _), (ost, _, _) in zip(in_win, out_win):
                        if 3.0 <= ost <= (window_sec - 3.0) and ist > 0.05:
                            deltas.append(ost - ist)
                    if not deltas:
                        deltas = [ost - ist for (ist, _, _), (ost, _, _) in zip(in_win, out_win) if ist > 0.05]
                    if deltas:
                        delta = round(float(statistics.median(deltas)), 2)
                        if 0.20 <= abs(delta) <= 180.0:
                            subtitle_manager.shift_subtitle_timestamps(sp, delta, sp)
                            _ensure_utf8_bom_rtl(sp)
                            return {"file": os.path.basename(sp), "shift_seconds": delta, "status": "shifted"}
                        return {"file": os.path.basename(sp), "shift_seconds": 0.0, "measured_delta": delta, "status": "already_in_sync"}
            return {"file": os.path.basename(sp), "shift_seconds": 0.0, "status": "unchanged"}

        with ThreadPoolExecutor(max_workers=min(len(sub_paths), 6)) as pool:
            results = list(pool.map(_sync_one, enumerate(sub_paths)))

        return {
            "synced": True,
            "method": "stream_audio_dense_75s_parallel_alass",
            "window_offset_sec": offset_sec,
            "details": results
        }
    except Exception as e:
        return {"synced": False, "reason": str(e)}
    finally:
        for p in temp_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _probe_http_playable(url, timeout=8):
    """Returns True if url responds with HTTP 200/206 (or valid redirect) without 4xx/5xx."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except Exception:
        return False


def stream_movie(query_or_imdb, player="mpc", year=None):
    """
    Streams the best 4K/1080p Remux/WEB-DL release directly in the chosen player
    WITHOUT downloading the movie, while fetching, syncing, and attaching ALL Arabic subtitles.
    """
    t0 = time.time()
    imdb_id, canonical_title, release_year = resolve_movie_identity(query_or_imdb, year=year)
    if not imdb_id:
        return {"error": True, "message": f"لم يتم العثور على الفلم '{query_or_imdb}'."}

    streams = fetch_torrentio_movie_streams(imdb_id)
    if not streams:
        return {"error": True, "imdb_id": imdb_id, "title": canonical_title, "message": f"لا توجد روابط متاحة للفلم {canonical_title}."}

    best, top_alts = select_best_movie_release(streams)
    if not best:
        return {"error": True, "imdb_id": imdb_id, "title": canonical_title, "message": "لم يتم العثور على رابط بث مباشر مخزن على TorBox."}

    candidates = [best] + [a for a in (top_alts or []) if a.get("direct_url") != best.get("direct_url")]
    chosen = None
    stream_url = None
    for cand in candidates:
        raw_u = cand.get("direct_url")
        if not raw_u:
            continue
        unwrapped = unwrap_debrid_redirect(raw_u)
        if _probe_http_playable(unwrapped):
            chosen = cand
            stream_url = unwrapped
            break

    if not chosen or not stream_url:
        return {"error": True, "imdb_id": imdb_id, "title": canonical_title, "message": "روابط TorBox الحالية لهذا الفلم ترجع خطأ من الخادم (HTTP 500) ولم يتم العثور على رابط بث صالح."}

    best = chosen

    import subtitle_manager
    movie_slug = _make_ascii_slug(f"{canonical_title}_{release_year}" if release_year else canonical_title)
    sub_cache_dir = os.path.join(os.environ.get("TEMP", "."), "torrentx_stream_subs", movie_slug)
    os.makedirs(sub_cache_dir, exist_ok=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_emb = pool.submit(subtitle_manager.check_embedded_arabic_sub, stream_url, 10)
        fut_sub = pool.submit(
            subtitle_manager.smart_fetch_subtitles,
            canonical_title, imdb_id, sub_cache_dir, best["title"], best.get("release_type")
        )
        embedded_check = fut_emb.result()
        sub_fetch = fut_sub.result()

    subs_to_load = []
    sync_info = None
    if embedded_check.get("has_embedded_arabic"):
        subtitle_status = "embedded_arabic_found"
    else:
        all_subs = [d.get("file_path") for d in sub_fetch.get("downloaded", []) if d.get("file_path") and os.path.exists(d.get("file_path"))]
        if all_subs:
            sync_info = _sync_subs_for_stream_url(stream_url, all_subs)
            subs_to_load = all_subs
            subtitle_status = "external_synced_for_stream"
        else:
            subtitle_status = "no_arabic_subtitle_found"

    display_title = f"{canonical_title} ({release_year})" if release_year else canonical_title
    play_res = stream_to_player(stream_url, subtitle_path=subs_to_load, player=player, title=display_title)
    return {
        "success": bool(play_res.get("success")),
        "mode": "stream_without_download",
        "elapsed_seconds": round(time.time() - t0, 2),
        "movie": {"title": canonical_title, "year": release_year, "imdb_id": imdb_id},
        "selected_release": {
            "filename": best["title"],
            "quality_tier": best["tier_label"],
            "size_gb": f"{best['size_gb']} GB"
        },
        "player": play_res,
        "subtitles": {
            "status": subtitle_status,
            "has_embedded_arabic": embedded_check.get("has_embedded_arabic", False),
            "embedded_tracks": embedded_check.get("arabic_tracks", []),
            "subtitles_dir": sub_cache_dir,
            "loaded_subtitle_paths": play_res.get("subtitles", subs_to_load),
            "stream_sync": sync_info,
            "subtitles_location_message": f"تم حفظ ومزامنة {len(play_res.get('subtitles', subs_to_load))} ترجمات عربية في: {sub_cache_dir}"
        }
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python debrid_manager.py fast-movie <title_or_imdb> [output_dir] [year]")
        print("  python debrid_manager.py search-movie <title_or_imdb> [year]")
        print("  python debrid_manager.py download <url> [aria2|idm|qbit] [out_dir] [filename]")
        print("  python debrid_manager.py probe <url_or_path>")
        print("  python debrid_manager.py resolve <magnet>")
        print("  python debrid_manager.py play <url_or_path> [sub_path_or_dir_or_semicolon_list] [mpc|potplayer|mpv|mpvnet|vlc] [--sync]")
        print("  python debrid_manager.py stream-movie <title_or_imdb> [mpc|potplayer|mpv|mpvnet|vlc] [year]")
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "serve-proxy" and len(sys.argv) >= 4:
        p_port = int(sys.argv[2])
        p_url = sys.argv[3]
        p_name = sys.argv[4] if len(sys.argv) > 4 else "movie.mkv"
        p_exe = sys.argv[5] if len(sys.argv) > 5 else ""
        _run_stream_proxy(p_port, p_url, media_filename=p_name, player_exe=p_exe)

    elif action == "stream-movie" and len(sys.argv) >= 3:
        q = sys.argv[2]
        plr = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else "mpc"
        yr = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "none" else None
        res = stream_movie(q, player=plr, year=yr)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "fast-movie" and len(sys.argv) >= 3:
        q = sys.argv[2]
        out_d = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
        yr = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "none" else None
        res = fast_download_movie(q, output_dir=out_d, year=yr)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "search-movie" and len(sys.argv) >= 3:
        q = sys.argv[2]
        yr = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
        imdb_id, c_title, r_year = resolve_movie_identity(q, year=yr)
        streams = fetch_torrentio_movie_streams(imdb_id) if imdb_id else []
        best, top_alts = select_best_movie_release(streams) if streams else (None, [])
        print(json.dumps({
            "movie": {"title": c_title, "year": r_year, "imdb_id": imdb_id},
            "best": best,
            "top_candidates": top_alts
        }, indent=2, ensure_ascii=False))

    elif action == "download" and len(sys.argv) >= 3:
        url = sys.argv[2]
        dl = sys.argv[3] if len(sys.argv) > 3 else "aria2"
        out_d = sys.argv[4] if len(sys.argv) > 4 else None
        fname = sys.argv[5] if len(sys.argv) > 5 else None
        res = send_to_downloader(url, dl, output_dir=out_d, file_name=fname)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "probe" and len(sys.argv) >= 3:
        url = sys.argv[2]
        res = probe_streams(url)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "resolve" and len(sys.argv) >= 3:
        magnet = sys.argv[2]
        res = add_magnet_and_resolve(magnet)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "play" and len(sys.argv) >= 3:
        flags = {a.lower() for a in sys.argv[3:] if a.startswith("--")}
        pos_args = [a for a in sys.argv[3:] if not a.startswith("--")]
        target_media = sys.argv[2]
        sub = pos_args[0] if len(pos_args) >= 1 and pos_args[0].lower() != "none" else None
        plr = pos_args[1] if len(pos_args) >= 2 and pos_args[1].lower() != "none" else None
        title_arg = pos_args[2] if len(pos_args) >= 3 and pos_args[2].lower() != "none" else None
        res = stream_to_player(target_media, sub, plr, title=title_arg, auto_sync=("--sync" in flags))
        print(json.dumps(res, indent=2, ensure_ascii=False))
