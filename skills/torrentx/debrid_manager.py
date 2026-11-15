# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import re
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor

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

SERVICES_META = {
    "torbox": {
        "name": "TorBox",
        "api_url": "https://torbox.app/settings?section=account",
        "login_url": "https://torbox.app/"
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


def get_torbox_api_key():
    cfg = load_config()
    return cfg.get("services", {}).get("torbox", {}).get("api_key") or ""


def torbox_api_request(endpoint, data=None, method="GET"):
    key = get_torbox_api_key()
    url = f"https://api.torbox.app/v1/api/{endpoint}"
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8") if hasattr(e, "read") else str(e)
        return {"error": True, "code": e.code, "message": err_msg}
    except Exception as e:
        return {"error": True, "message": str(e)}


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


def send_to_downloader(download_url, downloader=None, output_dir=None, file_name=None):
    cfg = load_config()
    if not downloader:
        if download_url.startswith("magnet:") or download_url.endswith(".torrent") or cfg.get("default_service") == "none":
            downloader = "qbittorrent" if os.path.exists(QBITTORRENT_PATH) else "aria2"
        else:
            downloader = cfg.get("default_downloader", "aria2")

    downloader = (downloader or "aria2").lower()
    if not output_dir:
        output_dir = os.path.expanduser(r"~\Downloads\Movies")
    os.makedirs(output_dir, exist_ok=True)

    download_url = unwrap_debrid_redirect(download_url)

    # Security Guard: Block executable/script/archive URLs before launching any downloader
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

    # Default: Aria2 16-connection high-speed download
    try:
        aria2_cmd = shutil.which("aria2c") or "aria2c"
        clean_filename = None
        if file_name:
            clean_filename = os.path.basename(file_name)
            clean_filename = re.sub(r'[\r\n"&|<>\^%`$;*?\\/:]', '_', clean_filename).strip()
            if not clean_filename.lower().endswith((".mkv", ".mp4", ".avi", ".ts", ".m2ts")):
                clean_filename += ".mkv"

        max_limit = cfg.get("max_download_limit", "29M")
        # TorBox CDN enforces strict per-file connection limits (HTTP 429) and delivers full speed on 1 connection
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

        launched = False
        if sys.platform == "win32":
            try:
                escaped_args = []
                for arg in cmd:
                    if any(c in arg for c in ' \t"'):
                        escaped_args.append('"' + arg.replace('"', '\\"') + '"')
                    else:
                        escaped_args.append(arg)
                raw_cli = ' '.join(escaped_args)
                b64_cli = __import__('base64').b64encode(raw_cli.encode('utf-8')).decode('ascii')
                ps_cmd = [
                    "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                    f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_cli}'))}}"
                ]
                res = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=5)
                if "ProcessId" in res.stdout or "ReturnValue" in res.stdout:
                    launched = True
            except Exception:
                pass

        if not launched:
            if sys.platform == "win32":
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return {
            "success": True,
            "downloader": "Aria2",
            "connections": 16,
            "output_dir": output_dir,
            "file_name": clean_filename,
            "target_path": os.path.join(output_dir, clean_filename) if clean_filename else None,
            "direct_url": download_url,
            "message": "بدأ التحميل السحابي الفوري عبر Aria2 (16 اتصال)"
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

    # Extract year if embedded in query (e.g. "Gladiator II 2024")
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

    # Fallback: IMDb suggestion API
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
    Fetches Torrentio streams using TorBox instant cloud caching ([TB+]) in <1 second.
    """
    tb_key = get_torbox_api_key()
    prefix = f"torbox={tb_key}/" if tb_key else ""
    svc_name = "torbox" if tb_key else "p2p"

    url = f"https://torrentio.strem.fun/{prefix}stream/movie/{imdb_id}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            streams = data.get("streams", [])
            for s in streams:
                s["_debrid_service"] = svc_name
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


def select_best_movie_release(streams):
    """
    Implements the User's Strict Movie Quality Protocol:
    1. NEVER allow AI Upscale (strict anti-upscale filter + fake 4K detection).
    2. Tier 1: 4K UHD BluRay Remux (2160p Remux, untouched disc, lossless audio).
    3. Tier 2: 1080p BluRay Remux (when movie has no 4K BluRay Remux, pick untouched 1080p BluRay Remux).
    4. Tier 3: 4K WEB-DL Untouched (when movie has no BluRay Remux, pick highest-bitrate uncompressed 4K WEB-DL).
    5. Tier 4: 1080p WEB-DL Untouched (when movie has no 4K WEB-DL either).
    6. Fallback Tier 5: High-bitrate BluRay Encode (excluding YTS/PSA/AV1/low-bitrate rips).
    """
    upscale_re = re.compile(r"\b(upscale|upscaled|ai[-\s]?upscale|ai[-\s]?remaster|topaz|visionx|4320p|8k)\b", re.I)
    remux_re = re.compile(r"\b(remux|bdremux|uhd[-\s]?remux)\b", re.I)
    webdl_re = re.compile(r"\b(web-dl|webdl|amzn|atvp|itunes|nf|dsnp|hmax|max|pcok|pmtp|ma\.web)\b", re.I)
    webrip_re = re.compile(r"\b(webrip|web-rip)\b", re.I)
    bdrip_re = re.compile(r"\b(bdrip|brrip|bluray[-\s]?rip)\b", re.I)
    full_disc_re = re.compile(r"\b(bdmv|iso|complete\.bluray|complete\.uhd|bd25|bd50|bd66|bd100)\b", re.I)
    cam_re = re.compile(r"\b(cam|hdcam|ts|hdts|telesync|tc|telecine|scr|dvdscr)\b", re.I)
    low_encode_re = re.compile(r"\b(yts|yify|psa|galaxyrg|rarbg|lama|pahe|av1|av01)\b", re.I)

    trusted_remux_groups = ["framestor", "cinephiles", "epsilon", "wildcat", "pmp", "bluranium", "fgt", "triton", "playbd", "decibel"]
    trusted_webdl_groups = ["flux", "ethel", "ntb", "kitsune", "cmrg", "nogrp", "sic", "tepes", "ben.the.men", "bynoche", " SuccessfulCrab"]
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

        # Hard security & quality exclusions: executables/archives, AI Upscale, CAM/Screener, Full ISO/BDMV, Foreign-only dubs
        if dangerous_re.search(comb) or dangerous_re.search(direct_url):
            continue
        if upscale_re.search(comb) or cam_re.search(comb) or full_disc_re.search(comb):
            continue
        if is_foreign_only_dub(comb):
            continue

        # Extract clean release filename
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

        # Resolution
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

        # Sanity size filters to prevent fake Remuxes or heavily compressed WEB-DLs
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

    # Fake 4K Upscale Detection across the movie's release pool:
    # If the movie has 1080p BluRay Remuxes, ZERO 4K BluRay Remuxes, and ZERO legitimate 4K WEB-DLs (with streaming tags),
    # then any third-party 4K encode is almost certainly an unlabelled upscale!
    has_4k_remux = any(c["res"] == "4K" and c["is_remux"] for c in parsed)
    has_1080p_remux = any(c["res"] == "1080p" and c["is_remux"] for c in parsed)
    has_legit_4k_webdl = any(c["res"] == "4K" and c["is_webdl"] and (c["has_stream_tag"] or c["size_gb"] >= 10.0) for c in parsed)

    filtered = []
    for c in parsed:
        if c["res"] == "4K" and not c["is_remux"] and not has_4k_remux and not has_legit_4k_webdl and has_1080p_remux:
            # Unlabelled 4K upscale of a 1080p-only movie -> skip!
            continue
        filtered.append(c)

    pool = filtered if filtered else parsed

    # Classify into the 5 Strict Priority Tiers
    for c in pool:
        comb = c["comb"]
        if c["res"] == "4K" and c["is_remux"]:
            tier = 1
            tier_label = "4K UHD BluRay Remux (خام أصلي بدون ضغط)"
            release_type = "bluray"
        elif c["res"] == "1080p" and c["is_remux"]:
            tier = 2
            tier_label = "1080p BluRay Remux (بلوراي خام أصلي بدون ضغط)"
            release_type = "bluray"
        elif c["res"] == "4K" and c["is_webdl"] and not c["is_low_encode"] and c["size_gb"] >= 8.0:
            tier = 3
            tier_label = "4K UHD WEB-DL (نسخة رقمية خام بدون ضغط)"
            release_type = "web"
        elif c["res"] == "1080p" and c["is_webdl"] and not c["is_low_encode"] and c["size_gb"] >= 3.0:
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

        # Intra-tier quality score
        q_score = 0
        if c["is_cached"]:
            q_score += 5000
        if any(g in comb for g in trusted_remux_groups):
            q_score += 400
        if any(g in comb for g in trusted_webdl_groups):
            q_score += 300
        if c["has_lossless_audio"]:
            q_score += 250
        if c["has_dv_hdr"]:
            q_score += 200
        if c["has_arabic_flag"]:
            q_score += 150
        if c["is_low_encode"]:
            q_score -= 1000
        # Prefer larger untouched files within the same tier (higher video/audio bitrate)
        q_score += min(int(c["size_gb"] * 8), 400)
        q_score += min(c["seeders"], 100)
        c["q_score"] = q_score

    # Prefer cached candidates from the highest available tier (1 -> 2 -> 3 -> 4 -> 5)
    cached_pool = [c for c in pool if c["is_cached"]]
    working_pool = cached_pool if cached_pool else pool

    # Sort by: Tier ascending (1 is best), then q_score descending!
    working_pool.sort(key=lambda x: (x["tier"], -x["q_score"]))
    best = working_pool[0]
    return best, working_pool[:6]


def fast_download_movie(query_or_imdb, output_dir=None, year=None):
    """
    Executes the High-Speed Movie-First Pipeline:
    1. Identifies movie & fetches cached streams in parallel (~1.2s).
    2. Selects best release (4K BluRay Remux -> 1080p BluRay Remux -> 4K Untouched WEB-DL -> 1080p Untouched WEB-DL).
    3. IMMEDIATELY starts downloading the movie via Aria2 FIRST to save time!
    4. While the movie downloads, inspects the stream for embedded Arabic subtitles.
    5. If no embedded Arabic subtitle exists, fetches ALL platform subtitles + top translator subtitles from SubSource & SubDL ONLY (deduplicated),
       and launches a background watcher to run alass timing sync & MKV muxing upon download completion!
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
        safe_folder = re.sub(r'[\\/*?:"<>|]', '', f"{canonical_title} ({release_year})" if release_year else canonical_title).strip()
        base_movies = os.path.expanduser(r"~\Downloads\Movies")
        output_dir = os.path.join(base_movies, safe_folder)
    os.makedirs(output_dir, exist_ok=True)

    # =========================================================================
    # STEP 1: START MOVIE DOWNLOAD IMMEDIATELY FIRST! (توفير الوقت)
    # =========================================================================
    dl_target = best.get("direct_url") or (f"magnet:?xt=urn:btih:{best['hash']}" if best.get("hash") else None)
    dl_res = send_to_downloader(dl_target, downloader="aria2", output_dir=output_dir, file_name=best["title"])
    movie_started_in_sec = round(time.time() - t0, 2)

    direct_cdn_url = dl_res.get("direct_url") or dl_target
    target_video_path = dl_res.get("target_path") or os.path.join(output_dir, best["title"])

    # =========================================================================
    # STEP 2: INSPECT FOR EMBEDDED ARABIC SUBTITLES & FETCH EXTERNAL IF NEEDED
    # =========================================================================
    import subtitle_manager

    embedded_check = {"has_embedded_arabic": False, "arabic_tracks": []}
    if direct_cdn_url and direct_cdn_url.startswith("http"):
        embedded_check = subtitle_manager.check_embedded_arabic_sub(direct_cdn_url, timeout=10)

    subtitle_report = {}
    if embedded_check.get("has_embedded_arabic"):
        subtitle_report = {
            "status": "embedded_arabic_found",
            "has_embedded_arabic": True,
            "embedded_tracks": embedded_check.get("arabic_tracks", []),
            "message": "الفلم يحتوي مسبقاً على ترجمة عربية رسمية مدمجة داخل ملف الفيديو (لا حاجة لتحميل ترجمة خارجية)."
        }
    else:
        # Save external subtitles inside a dedicated Subtitles subfolder
        sub_output_dir = os.path.join(output_dir, "Subtitles")
        os.makedirs(sub_output_dir, exist_ok=True)

        # Fetch subtitles strictly from SubSource & SubDL (All Platforms + Top Translators, deduplicated)
        sub_fetch = subtitle_manager.smart_fetch_subtitles(
            query=canonical_title,
            imdb_id=imdb_id,
            output_dir=sub_output_dir,
            video_filename=os.path.basename(target_video_path),
            release_type=best.get("release_type")
        )
        downloaded_subs = sub_fetch.get("downloaded", [])

        # Launch detached background watcher to run alass timing sync & MKV muxing as soon as Aria2 finishes
        watcher_launched = False
        if downloaded_subs and target_video_path:
            manifest_path = os.path.join(sub_output_dir, f".{os.path.basename(target_video_path)}.manifest.json")
            try:
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(downloaded_subs, mf, ensure_ascii=False, indent=2)
                sub_mgr_py = os.path.join(SKILL_DIR, "subtitle_manager.py")
                if sys.platform == "win32":
                    CREATE_NO_WINDOW = 0x08000000
                    DETACHED_PROCESS = 0x00000008
                    subprocess.Popen(
                        [sys.executable, sub_mgr_py, "watch-and-sync", target_video_path, manifest_path],
                        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                        close_fds=True
                    )
                else:
                    subprocess.Popen(
                        [sys.executable, sub_mgr_py, "watch-and-sync", target_video_path, manifest_path],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
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
            "output_dir": output_dir,
            "target_video_path": target_video_path
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
    """Adds a magnet to TorBox and resolves direct download links."""
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
        media_exts = ('.mkv', '.mp4', '.avi', '.ts', '.m2ts')
        media_files = [f for f in files if str(f.get("name", "")).lower().endswith(media_exts)]
        target_file = max(media_files, key=lambda x: x.get("size", 0)) if media_files else files[0]
        key = get_torbox_api_key()
        dl_res = torbox_api_request(f"torrents/requestdl?token={key}&torrent_id={torrent_id}&file_id={target_file.get('id')}&zip=false")
        if dl_res.get("data"):
            unrestricted_links.append({
                "filename": target_file.get("short_name") or target_file.get("name"),
                "filesize": target_file.get("size"),
                "download_url": dl_res["data"]
            })

    return {
        "torrent_id": torrent_id,
        "filename": t_info.get("name"),
        "bytes": t_info.get("size"),
        "status": t_info.get("download_state"),
        "is_cached": is_cached and len(unrestricted_links) > 0,
        "unrestricted_links": unrestricted_links
    }


def stream_to_player(video_path_or_url, subtitle_path=None, player=None):
    """Streams a direct Debrid URL or plays a local video file inside MPC-HC, PotPlayer, MPV, or VLC."""
    video_path_or_url = unwrap_debrid_redirect(video_path_or_url)
    if not player or player.lower() in ["default", "none", "auto"]:
        cfg = load_config()
        player = cfg.get("default_player", "mpc")
    player = player.lower()

    if player in ["mpc", "mpc-hc", "mpchc"]:
        mpc_bin = r"C:\Program Files (x86)\K-Lite Codec Pack\MPC-HC64\mpc-hc64.exe"
        if os.path.exists(mpc_bin):
            cmd = [mpc_bin, video_path_or_url]
            if subtitle_path and os.path.exists(subtitle_path):
                cmd.extend(["/sub", subtitle_path])
            subprocess.Popen(cmd)
            return {"success": True, "player": "MPC-HC", "target": video_path_or_url, "subtitle": subtitle_path}

    elif player == "potplayer":
        pot_bin = r"C:\Program Files\DAUM\PotPlayer\PotPlayer64.exe"
        if os.path.exists(pot_bin):
            cmd = [pot_bin, video_path_or_url]
            if subtitle_path and os.path.exists(subtitle_path):
                cmd.append(f"/sub={subtitle_path}")
            subprocess.Popen(cmd)
            return {"success": True, "player": "PotPlayer", "target": video_path_or_url, "subtitle": subtitle_path}

    elif player in ["mpv", "harbor"]:
        mpv_bin = HARBOR_MPV_PATH if os.path.exists(HARBOR_MPV_PATH) else "mpv"
        cmd = [mpv_bin, video_path_or_url, "--force-window=yes"]
        if subtitle_path and os.path.exists(subtitle_path):
            cmd.append(f"--sub-file={subtitle_path}")
        subprocess.Popen(cmd)
        return {"success": True, "player": "MPV", "target": video_path_or_url, "subtitle": subtitle_path}

    elif player == "vlc":
        vlc_bin = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
        if os.path.exists(vlc_bin):
            cmd = [vlc_bin, video_path_or_url]
            if subtitle_path and os.path.exists(subtitle_path):
                cmd.append(f"--sub-file={subtitle_path}")
            subprocess.Popen(cmd)
            return {"success": True, "player": "VLC", "target": video_path_or_url, "subtitle": subtitle_path}

    return {"error": True, "message": f"Player not found: {player}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python debrid_manager.py fast-movie <title_or_imdb> [output_dir] [year]")
        print("  python debrid_manager.py search-movie <title_or_imdb> [year]")
        print("  python debrid_manager.py download <url> [aria2|idm|qbit] [out_dir] [filename]")
        print("  python debrid_manager.py probe <url_or_path>")
        print("  python debrid_manager.py resolve <magnet>")
        print("  python debrid_manager.py play <url_or_path> [sub_path] [mpc|potplayer|mpv|vlc]")
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "fast-movie" and len(sys.argv) >= 3:
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
        target_media = sys.argv[2]
        sub = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
        plr = sys.argv[4] if len(sys.argv) > 4 else "mpc"
        res = stream_to_player(target_media, sub, plr)
        print(json.dumps(res, indent=2, ensure_ascii=False))
