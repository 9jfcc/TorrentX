# -*- coding: utf-8 -*-
"""
TorrentX Stremio / Harbor / Nuvio Addon Server
- Pure Python HTTP server (ZERO AI model required).
- Dual-mode:
  1. Debrid Mode: Returns ONLY cached streams on the user's chosen debrid service.
  2. P2P Mode (No Debrid): Returns ONLY active-seeder torrents (excludes 0-seeder dead torrents).
- Strictly sorted from Best Tier to Worst Tier (4K Remux -> 1080p Remux -> 4K WEB-DL -> 1080p WEB-DL -> Encode).
- Subtitles: Fetches Arabic subtitles exclusively from SubSource & SubDL (Official Platforms + Top Translators).
"""
import os
import re
import sys
import json
import time
import uuid
import base64
import logging
import threading
import subprocess
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TorrentX")

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import debrid_manager
import subtitle_manager

PORT = int(os.environ.get("PORT") or os.environ.get("TORRENTX_ADDON_PORT", "7123"))
SUB_CACHE = {}  # token -> {"bytes": b"...", "ts": float, "candidate": dict, "ref_times": list}
STREAM_META_CACHE = {}  # media_id -> {"ts": float, "items": [{"title", "release_type", "url"}]}
REF_TIMES_CACHE = {}  # media_url -> {"ts": float, "times": list}


def build_manifest(mode_label="Auto"):
    return {
        "id": "com.9jfcc.torrentx.addon",
        "version": "2.1.0",
        "name": f"TorrentX ({mode_label})",
        "description": (
            "Strict 4-Tier Anti-Upscale Movie/Series Streams (Cached-Only for Debrid, "
            "High-Seeder Only for P2P) + Exclusive SubSource/SubDL Arabic Subtitles."
        ),
        "resources": [
            "stream",
            {
                "name": "subtitles",
                "types": ["movie", "series"],
                "idPrefixes": ["tt"],
            },
        ],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False,
        },
    }


def parse_config_segment(seg):
    """Decodes optional base64 JSON config segment from Stremio addon URL."""
    if not seg or seg in ("manifest.json", "stream", "subtitles", "configure", "sub_dl", "install_harbor", "open_harbor"):
        return None
    try:
        padded = seg + "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def resolve_runtime_debrid(url_cfg=None):
    """Returns (mode, service_id, api_key) from URL config or local debrid.json."""
    if url_cfg:
        mode = (url_cfg.get("mode") or "debrid").lower()
        svc = (url_cfg.get("service") or "torbox").lower()
        key = (url_cfg.get("api_key") or "").strip()
        if mode == "p2p" or not key:
            return "p2p", "p2p", ""
        return "debrid", svc, key

    if hasattr(debrid_manager, "get_active_service"):
        svc, key = debrid_manager.get_active_service()
        if svc != "p2p" and key:
            return "debrid", svc, key
        return "p2p", "p2p", ""

    cfg = debrid_manager.load_config()
    svc = (cfg.get("default_service") or "torbox").lower()
    key = (cfg.get("services", {}).get(svc, {}).get("api_key") or "").strip()
    if svc not in ("none", "p2p") and key:
        return "debrid", svc, key
    return "p2p", "p2p", ""


def fetch_torrentio_streams(media_type, media_id, svc_id, api_key):
    prefix = f"{svc_id}={api_key}/" if (svc_id != "p2p" and api_key) else ""
    url = f"https://torrentio.strem.fun/{prefix}stream/{media_type}/{media_id}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            streams = data.get("streams", [])
            for s in streams:
                s["_debrid_service"] = svc_id
            return streams
    except Exception as e:
        logger.warning("Failed fetching torrentio streams: %s", e)
        return []


def _format_clean_stream_title(c, media_type="movie"):
    """Builds a short, clean stream title: Name • Res Quality • Group + Size • ~Bitrate • Arabic Sub."""
    raw_name = os.path.basename(c.get("title") or "Stream").strip()
    stem = re.sub(r"\.(mkv|mp4|avi|ts|m2ts)$", "", raw_name, flags=re.I)
    grp_m = re.search(r"(?:-([A-Za-z0-9_]+)|\.by\.([A-Za-z0-9_]+))$", stem, re.I)
    group = (grp_m.group(1) or grp_m.group(2)) if grp_m else ""

    ep_m = re.search(r"\b(S\d{1,2}E\d{1,2}|\d{1,2}x\d{2})\b", stem, re.I)
    yr_m = re.search(r"\b(19\d{2}|20\d{2})\b", stem)
    cut_idx = len(stem)
    if ep_m:
        cut_idx = ep_m.end()
    elif yr_m:
        cut_idx = yr_m.end()
    else:
        q_m = re.search(r"\b(2160p|1080p|720p|4k|uhd|remux|web-dl|webdl|bluray|blu-ray)\b", stem, re.I)
        if q_m:
            cut_idx = q_m.start()

    short_name = re.sub(r"[._]+", " ", stem[:cut_idx]).strip(" -.") or stem[:40]

    res = c.get("res") or "1080p"
    if c.get("is_remux"):
        qual = "Remux"
    elif c.get("is_webdl"):
        qual = "WEB-DL"
    elif c.get("is_webrip"):
        qual = "WEBRip"
    elif "bluray" in c.get("comb", "") or "blu-ray" in c.get("comb", ""):
        qual = "BluRay"
    else:
        qual = "Encode"

    line1_parts = [short_name, f"{res} {qual}"]
    if group:
        line1_parts.append(group)
    line1 = " • ".join(line1_parts)

    size_gb = c.get("size_gb") or 0.0
    size_str = f"💾 {size_gb:.1f} GB" if size_gb > 0 else "💾 ?"

    dur_sec = 2700 if (media_type == "series" or ep_m) else 7200
    est_mbps = round((size_gb * 8192) / dur_sec, 1) if size_gb > 0 else 0
    bitrate_str = f"⚡ ~{est_mbps} Mbps" if est_mbps > 0 else ""

    ar_str = "🇸🇦 ترجمة عربية مدمجة" if c.get("has_arabic_flag") else "🌐 ترجمة خارجية تلقائية"
    line2_parts = [p for p in [size_str, bitrate_str, ar_str] if p]

    return f"{line1}\n{' | '.join(line2_parts)}"


def format_streams(raw_streams, mode, svc_id, media_id=None, allow_upscale=False, media_type="movie"):
    """Filters, ranks by Tier (Best -> Worst), and formats into Stremio stream objects."""
    if not raw_streams:
        return []

    candidates = _rank_all_candidates(raw_streams, mode, allow_upscale=allow_upscale)
    if media_id:
        STREAM_META_CACHE[media_id] = {
            "ts": time.time(),
            "items": [
                {
                    "title": c.get("title", ""),
                    "release_type": c.get("release_type"),
                    "url": c.get("direct_url") or "",
                }
                for c in candidates
            ],
        }

    out = []
    for c in candidates[:25]:
        tier = c["tier"]
        res = c["res"]
        tier_short = {
            1: "Tier 1 • 4K Remux",
            2: "Tier 2 • 1080p Remux",
            3: "Tier 3 • 4K WEB-DL",
            4: "Tier 4 • 1080p WEB-DL",
            5: f"Tier 5 • {res} Encode",
        }.get(tier, f"Tier {tier}")

        if mode == "debrid" and c.get("is_cached"):
            badge = f"⚡ {svc_id.upper()} CACHED"
        else:
            badge = f"🌱 {c['seeders']} Seeders" + (" (Uncached)" if mode == "debrid" else "")

        clean_title = _format_clean_stream_title(c, media_type=media_type)
        stream_obj = {
            "name": f"TorrentX\n{tier_short}\n{badge}",
            "description": clean_title,
            "behaviorHints": {
                "bingeGroup": f"torrentx-tier-{tier}",
                "filename": c["title"],
            },
        }
        if c.get("direct_url"):
            stream_obj["url"] = c["direct_url"]
        elif c.get("hash"):
            stream_obj["infoHash"] = c["hash"]
        else:
            continue

        out.append(stream_obj)
    return out


def _rank_all_candidates(raw_streams, mode, allow_upscale=False):
    """Ranks all safe, anti-upscale (unless allow_upscale=True) candidates strictly by Tier (1->5)."""
    remaining = list(raw_streams)
    ranked_cached = []
    ranked_p2p = []
    seen = set()

    for _ in range(30):
        if not remaining:
            break
        best, top6 = debrid_manager.select_best_movie_release(remaining, allow_upscale=allow_upscale)
        if not best:
            break
        batch = [best] + [x for x in top6 if x is not best]
        added_any = False
        for item in batch:
            h = item.get("hash") or item.get("direct_url") or item.get("title")
            if h in seen:
                continue
            seen.add(h)
            added_any = True
            if item.get("is_cached"):
                ranked_cached.append(item)
            if item.get("seeders", 0) > 0:
                ranked_p2p.append(item)

        remaining = [
            s for s in remaining
            if (s.get("infoHash") or s.get("url") or s.get("title")) not in seen
        ]
        if not added_any:
            break

    if mode == "debrid" and ranked_cached:
        ranked_cached.sort(key=lambda x: (x["tier"], -x.get("q_score", 0)))
        return ranked_cached

    ranked_p2p.sort(key=lambda x: (x["tier"], -x.get("seeders", 0), -x.get("q_score", 0)))
    return ranked_p2p


def _extract_first_2min_ref_timestamps(media_url, timeout=5):
    """Reads ONLY the first ~3 minutes of the best non-forced embedded subtitle PTS timestamps from an HTTP CDN URL."""
    import shutil
    if not media_url or not media_url.startswith("http"):
        return []

    now = time.time()
    cached = REF_TIMES_CACHE.get(media_url)
    if cached and (now - cached.get("ts", 0)) < 3600:
        return cached.get("times") or []

    ffprobe = shutil.which("ffprobe") or os.path.expanduser(r"~\AppData\Local\Harbor\ffprobe.exe")
    if not os.path.exists(ffprobe) and not shutil.which("ffprobe"):
        return []

    try:
        stream_sel = "s:0"
        probe_s = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-select_streams", "s", media_url],
            capture_output=True, text=True, timeout=4, encoding="utf-8", errors="replace"
        )
        if probe_s.returncode == 0 and probe_s.stdout:
            s_list = json.loads(probe_s.stdout).get("streams") or []
            bitmap_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
            best_idx = None
            for idx, st in enumerate(s_list):
                codec = str(st.get("codec_name") or "").lower()
                tags = st.get("tags") or {}
                title = str(tags.get("title") or "").lower()
                forced = (st.get("disposition") or {}).get("forced") == 1 or "forced" in title
                if forced or codec in bitmap_codecs:
                    continue
                best_idx = idx
                break
            if best_idx is None:
                return []
            stream_sel = f"s:{best_idx}"

        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-select_streams", stream_sel,
            "-show_packets", "-read_intervals", "%+180",
            media_url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if res.returncode != 0 or not res.stdout:
            return []

        pkts = json.loads(res.stdout).get("packets") or []
        pts_list = []
        for p in pkts:
            t = p.get("pts_time")
            if t is not None:
                val = float(t)
                if 1.0 <= val <= 185.0:
                    pts_list.append(val)

        clean = []
        for val in sorted(pts_list):
            if not clean or (val - clean[-1]) > 0.35:
                clean.append(val)
        result = clean[:50]
        REF_TIMES_CACHE[media_url] = {"ts": now, "times": result}
        return result
    except Exception:
        return []


def _extract_audio_speech_ref_timestamps(media_url, timeout=10):
    """Fallback when video has no embedded text subtitles: extracts first 3 min of audio via ffmpeg and detects speech onsets via ffsubsync's webrtcvad."""
    import shutil, tempfile, wave
    if not media_url or not media_url.startswith("http"):
        return []

    now = time.time()
    cache_key = f"aud:{media_url}"
    cached = REF_TIMES_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 3600:
        return cached.get("times") or []

    ffmpeg = shutil.which("ffmpeg") or os.path.expanduser(r"~\AppData\Local\Harbor\ffmpeg.exe")
    if not os.path.exists(ffmpeg) and not shutil.which("ffmpeg"):
        return []

    try:
        import webrtcvad
    except ImportError:
        return []

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = os.path.join(tmpdir, "sample.wav")
            cmd = [
                ffmpeg, "-y", "-v", "quiet",
                "-t", "180", "-i", media_url,
                "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                wav_path,
            ]
            res = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if res.returncode != 0 or not os.path.exists(wav_path):
                return []

            vad = webrtcvad.Vad(2)
            frame_ms = 30
            frame_bytes = int(16000 * (frame_ms / 1000.0) * 2)
            speech_onsets = []
            in_speech = False
            speech_run = 0
            silence_run = 0

            with wave.open(wav_path, "rb") as wf:
                t_sec = 0.0
                while True:
                    buf = wf.readframes(frame_bytes // 2)
                    if len(buf) < frame_bytes:
                        break
                    is_sp = vad.is_speech(buf, 16000)
                    if is_sp:
                        speech_run += 1
                        silence_run = 0
                        if not in_speech and speech_run >= 4 and t_sec >= 1.0:
                            in_speech = True
                            onset = round(t_sec - (3 * frame_ms / 1000.0), 3)
                            if not speech_onsets or (onset - speech_onsets[-1]) > 0.5:
                                speech_onsets.append(onset)
                    else:
                        silence_run += 1
                        speech_run = 0
                        if in_speech and silence_run >= 10:
                            in_speech = False
                    t_sec += frame_ms / 1000.0

            result = speech_onsets[:50]
            REF_TIMES_CACHE[cache_key] = {"ts": now, "times": result}
            return result
    except Exception:
        return []


def _parse_srt_first_cues(srt_text):
    """Returns start timestamps (seconds) of the first 35 cues in an SRT string."""
    cues = []
    for m in re.finditer(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->", srt_text):
        h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        sec = h * 3600 + mn * 60 + s + ms / 1000.0
        if 1.0 <= sec <= 185.0:
            cues.append(sec)
        if len(cues) >= 35:
            break
    return cues


def _analyze_offset_and_inliers(ref_times, sub_times):
    """Returns (best_offset, natural_inliers_count, synced_inliers_count, required_min)."""
    if len(ref_times) < 4 or len(sub_times) < 4:
        return 0.0, 0, 0, 4
    req_min = max(4, min(len(sub_times), 8) // 2)
    nat_inliers = sum(1 for st in sub_times if min(ref_times, key=lambda rt: abs(st - rt)) and abs(st - min(ref_times, key=lambda rt: abs(st - rt))) <= 0.45)

    best_inliers = []
    for r0 in ref_times[:12]:
        for s0 in sub_times[:12]:
            cand = r0 - s0
            if abs(cand) > 60.0:
                continue
            inliers = []
            for st in sub_times:
                closest = min(ref_times, key=lambda rt: abs((st + cand) - rt))
                if abs((st + cand) - closest) <= 0.45:
                    inliers.append(closest - st)
            if len(inliers) > len(best_inliers):
                best_inliers = inliers

    if len(best_inliers) >= req_min:
        best_inliers.sort()
        median_off = best_inliers[len(best_inliers) // 2]
        off = round(median_off, 3) if abs(median_off) >= 0.15 else 0.0
        return off, nat_inliers, len(best_inliers), req_min
    return 0.0, nat_inliers, len(best_inliers), req_min


def _compute_fast_offset(ref_times, sub_times):
    """Finds median consensus offset (ref - sub) in seconds within [-60s, +60s]."""
    off, _, synced_cnt, req_min = _analyze_offset_and_inliers(ref_times, sub_times)
    return off if synced_cnt >= req_min else 0.0


def _score_and_sync_subtitle(cand, ref_times):
    """Downloads subtitle, measures natural vs shifted sync against ref_times, syncs if needed, and returns (tier, score, offset, bytes)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = subtitle_manager.download_subtitle_file(cand, tmpdir, file_basename="sub")
            saved_path = (dl.get("file_path") or dl.get("path")) if isinstance(dl, dict) else (dl if isinstance(dl, str) else None)
            if not saved_path or not os.path.exists(saved_path):
                return 3, 0, 0.0, None
            with open(saved_path, "r", encoding="utf-8", errors="ignore") as f:
                text = subtitle_manager.clean_subtitle_ads(f.read())

            if not ref_times:
                b = ("\ufeff" + text.lstrip("\ufeff")).encode("utf-8")
                return 2, 0, 0.0, b

            sub_times = _parse_srt_first_cues(text)
            off, nat_cnt, sync_cnt, req_min = _analyze_offset_and_inliers(ref_times, sub_times)
            if nat_cnt >= req_min and abs(off) < 0.35:
                tier = 0  # Already naturally synced
                score = nat_cnt
                applied_off = 0.0
            elif sync_cnt >= req_min and hasattr(subtitle_manager, "shift_subtitle_timestamps"):
                tier = 1  # Auto-synced by shifting
                score = sync_cnt
                applied_off = off
                if off != 0.0:
                    text = subtitle_manager.shift_subtitle_timestamps(text, off)
            else:
                tier = 2  # Could not verify sync against embedded ref
                score = max(nat_cnt, sync_cnt)
                applied_off = 0.0

            b = ("\ufeff" + text.lstrip("\ufeff")).encode("utf-8")
            return tier, score, applied_off, b
    except Exception:
        return 3, 0, 0.0, None


def _resolve_and_cache_sub(token):
    item = SUB_CACHE.get(token)
    if not item:
        return None
    if item.get("bytes"):
        return item["bytes"]

    cand = item.get("candidate")
    if not cand:
        return None

    ref_times = item.get("ref_times") or []
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = subtitle_manager.download_subtitle_file(cand, tmpdir, file_basename=token)
            saved_path = (dl.get("file_path") or dl.get("path")) if isinstance(dl, dict) else (dl if isinstance(dl, str) else None)
            if not saved_path or not os.path.exists(saved_path):
                return None
            with open(saved_path, "r", encoding="utf-8", errors="ignore") as f:
                text = subtitle_manager.clean_subtitle_ads(f.read())

            if ref_times and hasattr(subtitle_manager, "shift_subtitle_timestamps"):
                sub_times = _parse_srt_first_cues(text)
                offset = _compute_fast_offset(ref_times, sub_times)
                if offset != 0.0:
                    text = subtitle_manager.shift_subtitle_timestamps(text, offset)

            # UTF-8 with BOM helps MPV/Windows players detect Arabic encoding reliably
            b = ("\ufeff" + text.lstrip("\ufeff")).encode("utf-8")
            item["bytes"] = b
            return b
    except Exception as e:
        logger.warning("Error resolving sub %s: %s", token, e)
        return None


def _inject_harbor_addon(manifest_url, mode_label="TorrentX", url_cfg=None):
    """Safely updates Harbor configuration files while Harbor is closed."""
    try:
        if url_cfg is None and manifest_url:
            path_parts = [p for p in urllib.parse.urlparse(manifest_url).path.strip("/").split("/") if p]
            if path_parts:
                url_cfg = parse_config_segment(path_parts[0])

        if url_cfg:
            mode, svc_id, _ = resolve_runtime_debrid(url_cfg)
            mode_label = f"{svc_id.upper()} Cached" if mode == "debrid" else "P2P Seeders"

        app_dir = os.path.expanduser(r"~\AppData\Roaming\app.harbor")
        user_data_path = os.path.join(app_dir, "user-data.json")
        secrets_path = os.path.join(app_dir, "secrets.json")
        settings_path = os.path.join(app_dir, "settings.json")

        # 1. Update secrets.json with SubSource, SubDL, and Debrid keys
        if os.path.exists(secrets_path):
            try:
                with open(secrets_path, "r", encoding="utf-8") as sf:
                    s_data = json.load(sf)
                raw_inner = s_data.get("harbor.settings-secrets.v1.shared", "{}")
                inner = json.loads(raw_inner) if isinstance(raw_inner, str) else (raw_inner or {})
                if url_cfg:
                    if url_cfg.get("subsource_key"):
                        inner["subsourceApiKey"] = url_cfg["subsource_key"]
                    if url_cfg.get("subdl_key"):
                        inner["subdlApiKey"] = url_cfg["subdl_key"]
                    if url_cfg.get("api_key") and url_cfg.get("service") == "torbox":
                        inner["tbKey"] = url_cfg["api_key"]
                s_data["harbor.settings-secrets.v1.shared"] = json.dumps(inner, ensure_ascii=False)
                with open(secrets_path, "w", encoding="utf-8") as sf:
                    json.dump(s_data, sf, ensure_ascii=False)
            except Exception as e:
                logger.warning("Could not update secrets.json: %s", e)

        # 2. Update settings.json to enable addon subtitles and Arabic preference
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as stf:
                    st_data = json.load(stf)
                sp = st_data.get("subProvidersEnabled") or {}
                sp["subsource"] = True
                sp["subdl"] = True
                sp["addons"] = True
                st_data["subProvidersEnabled"] = sp
                st_data["preferredSubLangs"] = ["Arabic", "ara", "ar"]
                with open(settings_path, "w", encoding="utf-8") as stf:
                    json.dump(st_data, stf, ensure_ascii=False)
            except Exception as e:
                logger.warning("Could not update settings.json: %s", e)

        # 3. Update user-data.json with latest manifest (including subtitles resource)
        if not os.path.exists(user_data_path):
            return False
        with open(user_data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        new_entry = {
            "id": "com.9jfcc.torrentx.addon",
            "transportUrl": manifest_url,
            "installedAt": int(time.time() * 1000),
            "enabled": True,
            "manifest": build_manifest(mode_label),
        }

        updated_any = False
        for k in list(data.keys()):
            if "installed-addons" in k:
                raw_addons = data.get(k, "[]")
                addons_list = json.loads(raw_addons) if isinstance(raw_addons, str) else (raw_addons or [])
                addons_list = [a for a in addons_list if a.get("id") != "com.9jfcc.torrentx.addon"]
                addons_list.append(new_entry)
                data[k] = json.dumps(addons_list, ensure_ascii=False) if isinstance(raw_addons, str) else addons_list
                updated_any = True

        if not updated_any:
            data["harbor.installed-addons.v1.shared"] = json.dumps([new_entry], ensure_ascii=False)

        with open(user_data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return True
    except Exception as e:
        logger.warning("Failed injecting addon into Harbor: %s", e)
        return False


def fetch_subtitles_for_stremio(imdb_id, host_base, season=None, episode=None, filename=None, full_media_id=None, url_cfg=None, fast_mode=False):
    """Arabic subtitle search (SubSource + SubDL + Wyzie) with on-demand streaming."""
    now = time.time()
    for k in [k for k, v in list(SUB_CACHE.items()) if now - v.get("ts", 0) > 7200]:
        SUB_CACHE.pop(k, None)

    release_type = None
    cdn_url = None
    meta_entry = STREAM_META_CACHE.get(full_media_id or imdb_id) or STREAM_META_CACHE.get(imdb_id)
    if meta_entry and meta_entry.get("items"):
        items = meta_entry["items"]
        chosen = items[0]
        if filename:
            fn_low = filename.lower()
            for it in items:
                if it.get("title", "").lower() == fn_low or fn_low in it.get("title", "").lower():
                    chosen = it
                    break
        release_type = chosen.get("release_type")
        cdn_url = chosen.get("url")

    # Avoid blocking /stream/ requests with ffprobe/ffmpeg network calls when fast_mode=True
    ref_times = []
    if not fast_mode and cdn_url:
        ref_times = _extract_first_2min_ref_timestamps(cdn_url)
        if not ref_times:
            ref_times = _extract_audio_speech_ref_timestamps(cdn_url)

    query_title = imdb_id
    try:
        _, canonical_title, _ = debrid_manager.resolve_movie_identity(imdb_id)
        if canonical_title:
            query_title = canonical_title
    except Exception:
        pass

    subdl_key = (url_cfg or {}).get("subdl_key") or None
    subsource_key = (url_cfg or {}).get("subsource_key") or None
    wyzie_key = (url_cfg or {}).get("wyzie_key") or None

    target_lang = (url_cfg or {}).get("sub_lang") or "ar"
    stremio_lang = "ara" if target_lang == "ar" else ("eng" if target_lang == "en" else target_lang)

    subs = []
    try:
        res = subtitle_manager.search_all_subtitles(
            query_title, imdb_id=imdb_id, lang=target_lang, release_type=release_type,
            season=season, episode=episode,
            subdl_key=subdl_key, subsource_key=subsource_key, wyzie_key=wyzie_key,
        )
        candidates = res if isinstance(res, list) else (res.get("ranked_subtitles") or res.get("results") or [])
    except Exception as e:
        logger.warning("Subtitle search error for %s: %s", imdb_id, e)
        candidates = []

    if target_lang == "ar":
        clean_cand = []
        for c in candidates:
            c_lang = str(c.get("language") or c.get("lang") or "").lower()
            if c_lang in ("ar", "ara", "arabic", ""):
                clean_cand.append(c)
        candidates = clean_cand

    candidates = candidates[:8]
    ranked_entries = []

    if not fast_mode and ref_times:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        eval_pool = candidates[:6]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_score_and_sync_subtitle, c, ref_times): (idx, c) for idx, c in enumerate(eval_pool)}
            for fut in as_completed(futs):
                idx, c = futs[fut]
                try:
                    tier, score, applied_off, b = fut.result()
                except Exception:
                    tier, score, applied_off, b = 3, 0, 0.0, None
                if b:
                    ranked_entries.append((tier, -score, idx, c, applied_off, b))
        ranked_entries.sort(key=lambda x: (x[0], x[1], x[2]))
        verified = [e for e in ranked_entries if e[0] in (0, 1)]
        if len(verified) >= 2:
            ranked_entries = verified[:6]
        else:
            ranked_entries = ranked_entries[:6]
    else:
        for idx, c in enumerate(candidates[:6]):
            ranked_entries.append((2, 0, idx, c, 0.0, None))

    to_prefetch = []
    for pos, (tier, _neg_score, idx, c, applied_off, pre_bytes) in enumerate(ranked_entries):
        token = uuid.uuid4().hex[:16]
        trans = c.get("translator") or c.get("author") or ""
        prov = (c.get("provider") or "").upper()
        name = c.get("name") or c.get("release_name") or ""
        label_parts = []
        if tier == 0:
            label_parts.append("✅ متزامنة")
        elif tier == 1:
            sign = "+" if applied_off >= 0 else ""
            label_parts.append(f"🔄 تمت المزامنة ({sign}{applied_off:.1f}s)")
        if prov:
            label_parts.append(prov)
        if trans:
            label_parts.append(trans)
        elif name:
            label_parts.append(name[:28])
        label = " • ".join(label_parts) if label_parts else f"Arabic #{pos+1}"

        SUB_CACHE[token] = {
            "candidate": c,
            "ref_times": ref_times,
            "bytes": pre_bytes,
            "ts": now,
        }
        sub_url = f"{host_base}/sub_dl/{token}.srt"
        subs.append({
            "id": f"tx-{token}",
            "lang": stremio_lang,
            "url": sub_url,
            "SubEncoding": "UTF-8",
            "label": f"[TorrentX] {label}",
        })
        if pre_bytes is None and pos < 2:
            to_prefetch.append(token)

    def _do_prefetch():
        for tok in to_prefetch:
            _resolve_and_cache_sub(tok)

    if to_prefetch:
        threading.Thread(target=_do_prefetch, daemon=True).start()
    return subs


CONFIGURE_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>إعداد إضافة TorrentX</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; width: 100%; min-height: 100vh; font-family: system-ui, -apple-system, sans-serif; background: #0b1120; color: #f8fafc; }
  .page { width: 100%; min-height: 100vh; display: flex; flex-direction: column; justify-content: center; padding: 32px 6vw; }
  .card { width: 100%; background: #1e293b; border-radius: 16px; padding: 36px 40px; border: 1px solid #334155; box-shadow: 0 20px 50px rgba(0,0,0,0.45); }
  h2 { margin-top: 0; font-size: 28px; }
  h3 { margin: 28px 0 8px; font-size: 18px; color: #93c5fd; border-bottom: 1px solid #334155; padding-bottom: 6px; }
  p { color: #cbd5e1; font-size: 15px; }
  label { display: block; margin-top: 14px; font-weight: 600; font-size: 14px; }
  select, input { width: 100%; padding: 13px; margin-top: 6px; border-radius: 10px; border: 1px solid #475569; background: #0f172a; color: #fff; font-size: 14px; }
  .api-row { display: flex; gap: 10px; align-items: stretch; margin-top: 6px; }
  .api-row input { margin-top: 0; flex: 1; }
  .btn-api { display: inline-flex; align-items: center; justify-content: center; white-space: nowrap; padding: 0 18px; border-radius: 10px; background: #10b981; color: #fff; font-weight: 700; text-decoration: none; font-size: 13px; transition: background 0.15s; min-width: 155px; }
  .btn-api:hover { background: #059669; }
  .note { margin-top: 18px; padding: 14px 16px; background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.45); border-radius: 10px; font-size: 13px; color: #fde68a; line-height: 1.6; }
  button { margin-top: 24px; width: 100%; padding: 15px; border: none; border-radius: 10px; background: #3b82f6; color: #fff; font-size: 17px; font-weight: bold; cursor: pointer; }
  button:hover { background: #2563eb; }
  .out { margin-top: 20px; padding: 18px; background: #0f172a; border: 1px solid #334155; border-radius: 10px; word-break: break-all; display: none; }
  .actions { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
  .actions button, .actions a { margin-top: 0; flex: 1; min-width: 160px; padding: 12px; border-radius: 8px; text-align: center; text-decoration: none; font-weight: 700; font-size: 14px; cursor: pointer; }
  .btn-copy { background: #475569; color: #fff; }
  .btn-install { background: #8b5cf6; color: #fff; }
</style>
</head>
<body>
<div class="page">
  <div class="card">
    <h2>⚡ إعداد إضافة TorrentX</h2>
    <p>اختر وضع التشغيل (مع خدمة ديبرد للكاش السحابي، أو بدون ديبرد لنسخ التورنت ذات السييدرز النشطين فقط):</p>

    <h3>1. وضع التشغيل والديبرد</h3>
    <label>وضع التشغيل (Mode)</label>
    <select id="mode" onchange="toggleDebrid()">
      <option value="debrid">مع خدمة ديبرد (Cached-Only + P2P Fallback)</option>
      <option value="p2p">بدون ديبرد (P2P - Seeders Only)</option>
    </select>
    <div id="debridBox">
      <label>خدمة الديبرد</label>
      <select id="service" onchange="updateApiLink()">
        <option value="torbox">TorBox</option>
        <option value="realdebrid">Real-Debrid</option>
        <option value="alldebrid">AllDebrid</option>
        <option value="premiumize">Premiumize</option>
        <option value="debridlink">Debrid-Link</option>
        <option value="offcloud">Offcloud</option>
        <option value="putio">Put.io</option>
      </select>
      <label>مفتاح الـ API لخدمة الديبرد</label>
      <div class="api-row">
        <input id="apikey" type="password" placeholder="ضع مفتاح API هنا">
        <a id="apiLink" class="btn-api" href="https://torbox.app/settings?section=account" target="_blank" rel="noopener">🔑 جلب مفتاح API</a>
      </div>
    </div>

    <h3>2. خيارات الجودة (Quality Filter)</h3>
    <label>نسخ الأبسكيل (AI Upscale / 4K-8K Upscaled)</label>
    <select id="allowUpscale">
      <option value="false">لا — منع الأبسكيل والالتزام بالنسخ الأصلية فقط (موصى به)</option>
      <option value="true">نعم — السماح بعرض نسخ الأبسكيل</option>
    </select>

    <h3>3. مفاتيح مواقع الترجمة (اختياري)</h3>
    <label>مفتاح SubDL API</label>
    <div class="api-row">
      <input id="subdlKey" type="password" placeholder="مفتاح SubDL (اختياري)">
      <a class="btn-api" href="https://subdl.com/panel/api" target="_blank" rel="noopener">🔑 جلب من SubDL</a>
    </div>
    <label>مفتاح SubSource API</label>
    <div class="api-row">
      <input id="subsourceKey" type="password" placeholder="مفتاح SubSource (اختياري)">
      <a class="btn-api" href="https://subsource.net/" target="_blank" rel="noopener">🔑 جلب من SubSource</a>
    </div>
    <label>مفتاح Wyzie Subs API</label>
    <div class="api-row">
      <input id="wyzieKey" type="password" placeholder="مفتاح Wyzie (اختياري)">
      <a class="btn-api" href="https://sub.wyzie.io/redeem" target="_blank" rel="noopener">🔑 جلب من Wyzie</a>
    </div>

    <h3>4. لغة الترجمة (Subtitle Language)</h3>
    <label>لغة الترجمات المستهدفة</label>
    <select id="subLang">
      <option value="ar" selected>🇸🇦 العربية فقط (Arabic Only) — SubSource + SubDL (موصى به)</option>
      <option value="en">🇺🇸 الإنجليزية فقط (English Only)</option>
      <option value="all">🌐 كل اللغات المتاحة (All Languages)</option>
    </select>

    <div class="note">
      ⚠️ <b>ملاحظات هامة لمستخدمي Harbor:</b><br>
      • عند الضغط على <b>«⚡ التثبيت المباشر في Harbor»</b> سيقوم السيرفر بتحديث إعدادات Harbor وتفعيل الإضافة والترجمة العربية تلقائياً وإعادة تشغيل Harbor.<br>
      • إذا أردت إضافة الرابط يدوياً من داخل Harbor (إضافة من رابط)، الصق رابط <b>http://127.0.0.1:...</b> المنسوخ مباشرةً <b>وليس</b> رابط <code>stremio://</code> لأن Harbor يحول <code>stremio://</code> إلى <code>https://</code> مما يفشل مع السيرفر المحلي.
    </div>

    <button onclick="gen()">إنشاء رابط التثبيت</button>
    <div id="out" class="out"></div>
  </div>
</div>
<script>
const API_URLS = {
  torbox: 'https://torbox.app/settings?section=account',
  realdebrid: 'https://real-debrid.com/apitoken',
  alldebrid: 'https://alldebrid.com/apikeys',
  premiumize: 'https://www.premiumize.me/account',
  debridlink: 'https://debrid-link.com/webapp/apikey',
  offcloud: 'https://offcloud.com/account',
  putio: 'https://app.put.io/settings/account'
};
function updateApiLink() {
  const svc = document.getElementById('service').value;
  document.getElementById('apiLink').href = API_URLS[svc] || '#';
}
function toggleDebrid() {
  document.getElementById('debridBox').style.display =
    document.getElementById('mode').value === 'debrid' ? 'block' : 'none';
}
let lastUrl = '';
function gen() {
  const cfg = {
    mode: document.getElementById('mode').value,
    service: document.getElementById('service').value,
    api_key: document.getElementById('apikey').value.trim(),
    allow_upscale: document.getElementById('allowUpscale').value === 'true',
    sub_lang: document.getElementById('subLang').value,
    subdl_key: document.getElementById('subdlKey').value.trim(),
    subsource_key: document.getElementById('subsourceKey').value.trim(),
    wyzie_key: document.getElementById('wyzieKey').value.trim()
  };
  const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(cfg)))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
  lastUrl = 'http://127.0.0.1:' + window.location.port + '/' + b64 + '/manifest.json';
  const el = document.getElementById('out');
  el.style.display = 'block';
  el.innerHTML = '<b style="color:#93c5fd;font-size:15px;">رابط الإضافة المولد (HTTP):</b>' +
    '<div style="word-break:break-all;font-size:12px;color:#cbd5e1;background:#090d16;padding:12px;border-radius:8px;border:1px solid #334155;margin:10px 0;user-select:all;">' + lastUrl + '</div>' +
    '<div class="actions">' +
    '<button type="button" class="btn-install" onclick="smartInstall()">⚡ التثبيت المباشر في Harbor</button>' +
    '<button type="button" class="btn-copy" onclick="copyLink()">📋 نسخ رابط HTTP</button>' +
    '</div>' +
    '<p id="hint" style="margin-top:12px;font-size:14px;color:#38bdf8;display:none;"></p>';
}
function copyLink() {
  navigator.clipboard.writeText(lastUrl);
  const h = document.getElementById('hint');
  h.style.display = 'block';
  h.textContent = '✅ تم نسخ رابط HTTP للحافظة! يمكنك لصقه مباشرة في خانة (إضافة من رابط) داخل Harbor.';
}
async function smartInstall() {
  const h = document.getElementById('hint');
  h.style.display = 'block';
  h.textContent = '⏳ جاري حقن الإضافة وتفعيل الترجمة في Harbor...';
  try { await navigator.clipboard.writeText(lastUrl); } catch(e) {}
  try {
    const res = await fetch('/install_harbor?manifest=' + encodeURIComponent(lastUrl));
    const data = await res.json();
    if (data.ok) {
      h.textContent = '✅ تم تثبيت الإضافة وتفعيل الترجمة العربية في Harbor بنجاح! وتم نسخ رابط HTTP للحافظة احتياطياً.';
    } else {
      h.textContent = '⚠️ تعذر الحقن التلقائي، تم نسخ رابط HTTP للحافظة — الصقه يدوياً داخل Harbor في نافذة (إضافة من رابط).';
    }
  } catch(e) {
    h.textContent = '⚠️ تم نسخ رابط HTTP للحافظة — الصقه داخل Harbor في نافذة (إضافة من رابط).';
  }
}
</script>
</body>
</html>
"""


class AddonHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send_json(self, obj, status=200, head_only=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_bytes(self, data, content_type="text/plain; charset=utf-8", status=200, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        self._handle_request(head_only=True)

    def do_GET(self):
        self._handle_request(head_only=False)

    def _handle_request(self, head_only=False):
        try:
            with open(r"C:\Users\9jhp9\addon_requests.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.command} {self.path}\n")
        except Exception:
            pass

        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.strip("/")
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]

        if not parts or parts == ["configure"]:
            return self._send_bytes(CONFIGURE_HTML.encode("utf-8"), "text/html; charset=utf-8", head_only=head_only)

        if parts == ["open_harbor"] or (len(parts) == 1 and parts[0].startswith("install_harbor")):
            query = urllib.parse.parse_qs(parsed_url.query)
            m_url = (query.get("manifest") or [None])[0]
            harbor_exe = os.path.expanduser(r"~\AppData\Local\Harbor\harbor.exe")

            # Close Harbor briefly so it doesn't overwrite user-data.json while we inject the addon
            try:
                subprocess.run(["taskkill", "/F", "/IM", "harbor.exe"], capture_output=True, timeout=3)
                time.sleep(0.4)
            except Exception:
                pass

            injected = False
            if m_url:
                injected = _inject_harbor_addon(m_url)

            if os.path.exists(harbor_exe):
                os.startfile(harbor_exe)
                return self._send_json({"ok": True, "injected": injected}, head_only=head_only)
            return self._send_json({"ok": injected, "error": "Harbor executable not found"}, status=200, head_only=head_only)

        if parts[0] == "sub_dl" and len(parts) == 2:
            token = parts[1].replace(".srt", "")
            item = SUB_CACHE.get(token)
            if not item:
                return self._send_bytes(b"", status=404, head_only=head_only)
            data = item.get("bytes")
            if not data:
                data = _resolve_and_cache_sub(token)
            if not data:
                return self._send_bytes(b"", status=404, head_only=head_only)
            return self._send_bytes(data, "application/x-subrip; charset=utf-8", head_only=head_only)

        # Optional config segment at parts[0]
        url_cfg = parse_config_segment(parts[0])
        if url_cfg is not None:
            parts = parts[1:]

        mode, svc_id, api_key = resolve_runtime_debrid(url_cfg)
        mode_label = f"{svc_id.upper()} Cached" if mode == "debrid" else "P2P Seeders"

        if parts == ["manifest.json"]:
            return self._send_json(build_manifest(mode_label), head_only=head_only)

        if len(parts) == 2 and parts[1] == "configure":
            return self._send_bytes(CONFIGURE_HTML.encode("utf-8"), "text/html; charset=utf-8", head_only=head_only)

        if len(parts) == 3 and parts[0] == "stream" and parts[2].endswith(".json"):
            media_type = parts[1]
            media_id = parts[2][:-5]
            allow_upscale = bool((url_cfg or {}).get("allow_upscale", False))
            raw = fetch_torrentio_streams(media_type, media_id, svc_id, api_key)
            streams = format_streams(raw, mode, svc_id, media_id=media_id, allow_upscale=allow_upscale, media_type=media_type)

            # Attach Arabic subtitles directly inside stream objects (fast_mode=True avoids ffprobe delay)
            try:
                host_base = f"http://{self.headers.get('Host') or f'127.0.0.1:{PORT}'}"
                id_parts = media_id.split(":")
                imdb_id = id_parts[0]
                season = int(id_parts[1]) if len(id_parts) > 1 and id_parts[1].isdigit() else None
                episode = int(id_parts[2]) if len(id_parts) > 2 and id_parts[2].isdigit() else None
                subs = fetch_subtitles_for_stremio(
                    imdb_id, host_base, season=season, episode=episode, full_media_id=media_id, url_cfg=url_cfg, fast_mode=True
                )
                if subs:
                    for s in streams:
                        s["subtitles"] = subs
            except Exception as e:
                logger.warning("Error attaching subs to streams: %s", e)

            return self._send_json({"streams": streams}, head_only=head_only)

        if len(parts) >= 3 and parts[0] == "subtitles":
            media_type = parts[1]
            if len(parts) == 3:
                media_id = parts[2][:-5] if parts[2].endswith(".json") else parts[2]
                extra_str = ""
            else:
                media_id = parts[2]
                extra_part = "/".join(parts[3:])
                extra_str = extra_part[:-5] if extra_part.endswith(".json") else extra_part

            extra_params = urllib.parse.parse_qs(extra_str) if extra_str else {}
            query_params = urllib.parse.parse_qs(parsed_url.query)
            filename = (extra_params.get("filename") or query_params.get("filename") or [None])[0]

            id_parts = media_id.split(":")
            imdb_id = id_parts[0]
            season = int(id_parts[1]) if len(id_parts) > 1 and id_parts[1].isdigit() else None
            episode = int(id_parts[2]) if len(id_parts) > 2 and id_parts[2].isdigit() else None
            host_base = f"http://{self.headers.get('Host') or f'127.0.0.1:{PORT}'}"
            subs = fetch_subtitles_for_stremio(
                imdb_id, host_base, season=season, episode=episode, filename=filename, full_media_id=media_id, url_cfg=url_cfg, fast_mode=False
            )
            return self._send_json({"subtitles": subs}, head_only=head_only)

        return self._send_json({"error": "Not found"}, status=404, head_only=head_only)


if __name__ == "__main__":
    import webbrowser

    def _auto_open_browser():
        time.sleep(0.6)
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}/configure")
        except Exception:
            pass

    threading.Thread(target=_auto_open_browser, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), AddonHandler)
    print(f"TorrentX Stremio/Harbor/Nuvio Addon running at http://127.0.0.1:{PORT}/manifest.json")
    print(f"Configure page: http://127.0.0.1:{PORT}/configure")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
