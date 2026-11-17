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


RAW_SUB_TEXT_CACHE = {}  # (provider, id, ep_tag) -> {"ts": float, "text": str}
_SUBSOURCE_SEM = threading.Semaphore(2)
_SUBSOURCE_LAST_REQ = 0.0
_SUBSOURCE_LOCK = threading.Lock()


def _resolve_redirect_url(media_url):
    """Resolves HTTP redirects (e.g. Torrentio -> TorBox CDN) once so ffprobe/ffmpeg connect directly."""
    if not media_url or not media_url.startswith("http"):
        return media_url
    try:
        req = urllib.request.Request(media_url, method="GET", headers={"Range": "bytes=0-1", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.geturl() or media_url
    except Exception:
        return media_url


def _build_sampling_windows(duration_sec, is_series=False):
    """Builds 4 strategic time windows (start_sec, end_sec) across the entire movie/episode."""
    if not duration_sec or duration_sec < 300:
        duration_sec = 2520.0 if is_series else 6600.0

    w0_s = 8.0
    w0_e = min(135.0, duration_sec * 0.18)

    w1_s = max(w0_e + 60.0, round(duration_sec * 0.25))   # ~Min 10-12 on a 42m series
    w1_e = min(duration_sec - 60.0, w1_s + 115.0)

    w2_s = max(w1_e + 60.0, round(duration_sec * 0.52))   # ~Min 22 on a 42m series
    w2_e = min(duration_sec - 40.0, w2_s + 115.0)

    w3_s = max(w2_e + 60.0, round(duration_sec * 0.78))   # ~Min 33 on a 42m series
    w3_e = min(duration_sec - 15.0, w3_s + 115.0)

    windows = [(w0_s, w0_e)]
    for ws, we in [(w1_s, w1_e), (w2_s, w2_e), (w3_s, w3_e)]:
        if we - ws >= 45.0 and ws > windows[-1][1]:
            windows.append((ws, we))
    return windows


def _extract_multiwindow_ref_timestamps(media_url, is_series=False, timeout=11):
    """
    Extracts embedded subtitle PTS timestamps across 4 windows spanning the entire episode/movie
    (Beginning, ~25% [Min 10-12], ~52% [Min 22], ~78% [Min 33]) in a single ffprobe read_intervals call.
    Returns dict: {"times": [...], "windows": [(s, e), ...], "duration": float, "fps": float, "source": "sub"} or None.
    """
    import shutil
    if not media_url or not media_url.startswith("http"):
        return None

    now = time.time()
    cache_key = f"sub_mw:{media_url}"
    cached = REF_TIMES_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 3600:
        return cached.get("data")

    ffprobe = shutil.which("ffprobe") or os.path.expanduser(r"~\AppData\Local\Harbor\ffprobe.exe")
    if not os.path.exists(ffprobe) and not shutil.which("ffprobe"):
        return None

    direct_url = _resolve_redirect_url(media_url)

    try:
        probe_cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", direct_url
        ]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=6, encoding="utf-8", errors="replace")
        if probe_res.returncode != 0 or not probe_res.stdout:
            return None

        probe_json = json.loads(probe_res.stdout)
        fmt_info = probe_json.get("format") or {}
        all_streams = probe_json.get("streams") or []

        duration = float(fmt_info.get("duration") or 0.0)
        video_fps = 23.976
        sub_streams = []

        for st in all_streams:
            ctype = st.get("codec_type")
            if ctype == "video" and video_fps == 23.976:
                r_fps = st.get("r_frame_rate") or st.get("avg_frame_rate") or ""
                if "/" in r_fps:
                    num, den = r_fps.split("/", 1)
                    if float(den) > 0:
                        video_fps = round(float(num) / float(den), 3)
            elif ctype == "subtitle":
                sub_streams.append(st)
            if not duration and st.get("duration"):
                try:
                    duration = max(duration, float(st.get("duration")))
                except Exception:
                    pass

        bitmap_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
        best_sub_pos = None
        fallback_sub_pos = None

        for pos, st in enumerate(sub_streams):
            codec = str(st.get("codec_name") or "").lower()
            if codec in bitmap_codecs:
                continue
            tags = st.get("tags") or {}
            lang = str(tags.get("language") or "").lower()
            title = str(tags.get("title") or "").lower()
            if (st.get("disposition") or {}).get("forced") == 1 or "forced" in title or "commentary" in title or "signs" in title:
                continue
            if lang in ("eng", "en", "ara", "ar"):
                best_sub_pos = pos
                break
            if fallback_sub_pos is None:
                fallback_sub_pos = pos

        chosen_pos = best_sub_pos if best_sub_pos is not None else fallback_sub_pos
        if chosen_pos is None:
            return None

        windows = _build_sampling_windows(duration, is_series=is_series)
        intervals_arg = ",".join(f"{int(ws)}%+{int(we - ws)}" for ws, we in windows)

        pkt_cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-select_streams", f"s:{chosen_pos}",
            "-show_packets", "-show_entries", "packet=pts_time",
            "-read_intervals", intervals_arg,
            direct_url,
        ]
        pkt_res = subprocess.run(pkt_cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if pkt_res.returncode != 0 or not pkt_res.stdout:
            return None

        pkts = json.loads(pkt_res.stdout).get("packets") or []
        max_valid_t = (duration + 60.0) if duration > 0 else 14400.0
        pts_list = []
        for p in pkts:
            t = p.get("pts_time")
            if t is not None:
                val = float(t)
                if 1.0 <= val <= max_valid_t:
                    pts_list.append(val)

        clean = []
        for val in sorted(pts_list):
            if not clean or (val - clean[-1]) > 0.30:
                clean.append(val)

        if len(clean) < 5:
            return None

        data = {
            "times": clean,
            "windows": windows,
            "duration": duration or (2520.0 if is_series else 6600.0),
            "fps": video_fps,
            "source": "sub",
        }
        REF_TIMES_CACHE[cache_key] = {"ts": now, "data": data}
        return data
    except Exception:
        return None


def _extract_audio_speech_ref_timestamps(media_url, is_series=False, timeout=10):
    """
    Multi-Window Audio VAD Fallback: Uses HTTP Range seeking (-ss before -i) across 3 windows
    (Beginning, ~Minute 11, ~Minute 23) in parallel so FPS drift and mid-episode cuts are caught even without embedded subs.
    """
    import shutil, tempfile, wave
    if not media_url or not media_url.startswith("http"):
        return None

    now = time.time()
    cache_key = f"aud_mw:{media_url}"
    cached = REF_TIMES_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 3600:
        return cached.get("data")

    ffmpeg = shutil.which("ffmpeg") or os.path.expanduser(r"~\AppData\Local\Harbor\ffmpeg.exe")
    if not os.path.exists(ffmpeg) and not shutil.which("ffmpeg"):
        return None

    try:
        import webrtcvad
    except ImportError:
        return None

    direct_url = _resolve_redirect_url(media_url)
    windows = [(10.0, 95.0), (640.0, 725.0), (1300.0, 1385.0)] if is_series else [(15.0, 105.0), (1200.0, 1290.0), (3000.0, 3090.0)]

    def _probe_audio_window(win_tuple):
        ws, we = win_tuple
        dur = int(we - ws)
        onsets = []
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                wav_path = os.path.join(tmpdir, "chunk.wav")
                cmd = [
                    ffmpeg, "-y", "-v", "quiet",
                    "-ss", str(int(ws)), "-t", str(dur),
                    "-i", direct_url,
                    "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                    wav_path,
                ]
                res = subprocess.run(cmd, capture_output=True, timeout=timeout)
                if res.returncode != 0 or not os.path.exists(wav_path):
                    return onsets

                vad = webrtcvad.Vad(2)
                frame_ms = 30
                frame_bytes = int(16000 * (frame_ms / 1000.0) * 2)
                in_speech = False
                speech_run = 0
                silence_run = 0

                with wave.open(wav_path, "rb") as wf:
                    t_rel = 0.0
                    while True:
                        buf = wf.readframes(frame_bytes // 2)
                        if len(buf) < frame_bytes:
                            break
                        if vad.is_speech(buf, 16000):
                            speech_run += 1
                            silence_run = 0
                            if not in_speech and speech_run >= 4 and t_rel >= 0.5:
                                in_speech = True
                                onset = round(ws + t_rel - (3 * frame_ms / 1000.0), 3)
                                if not onsets or (onset - onsets[-1]) > 0.45:
                                    onsets.append(onset)
                        else:
                            silence_run += 1
                            speech_run = 0
                            if in_speech and silence_run >= 10:
                                in_speech = False
                        t_rel += frame_ms / 1000.0
        except Exception:
            pass
        return onsets

    try:
        from concurrent.futures import ThreadPoolExecutor
        all_onsets = []
        valid_windows = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = [(w, pool.submit(_probe_audio_window, w)) for w in windows]
            for w, fut in futs:
                w_pts = fut.result()
                if len(w_pts) >= 3:
                    all_onsets.extend(w_pts)
                    valid_windows.append(w)

        if len(all_onsets) < 5:
            return None

        data = {
            "times": sorted(all_onsets),
            "windows": valid_windows,
            "duration": 2520.0 if is_series else 6600.0,
            "fps": 23.976,
            "source": "audio",
        }
        REF_TIMES_CACHE[cache_key] = {"ts": now, "data": data}
        return data
    except Exception:
        return None


def _parse_srt_all_cues(srt_text):
    """Returns list of (start_sec, end_sec) for all valid dialogue cues in an SRT string."""
    cues = []
    pattern = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    for m in re.finditer(pattern, srt_text):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        st = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        et = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        if st >= 0.5 and et > st:
            cues.append((st, et))
    return cues


def _find_window_offset(ref_w, sub_w, max_shift=75.0, tol=0.55):
    """Finds the best constant offset (ref - sub) inside a single time window using consensus voting."""
    if len(ref_w) < 2 or len(sub_w) < 2:
        return 0.0, 0, 0.0

    best_inliers = []
    candidates = []
    for r in ref_w:
        for s in sub_w:
            diff = r - s
            if abs(diff) <= max_shift:
                candidates.append(diff)

    if not candidates:
        return 0.0, 0, 0.0

    from collections import Counter
    bins = Counter(round(c * 5) / 5.0 for c in candidates)
    top_centers = [b for b, _ in bins.most_common(8)]

    for cand in top_centers:
        inliers = []
        for s in sub_w:
            closest_r = min(ref_w, key=lambda r: abs((s + cand) - r))
            if abs((s + cand) - closest_r) <= tol:
                inliers.append(closest_r - s)
        if len(inliers) > len(best_inliers):
            best_inliers = inliers

    if not best_inliers:
        return 0.0, 0, 0.0

    best_inliers.sort()
    median_off = best_inliers[len(best_inliers) // 2]
    refined = [
        min(ref_w, key=lambda r: abs((s + median_off) - r)) - s
        for s in sub_w
        if abs((s + median_off) - min(ref_w, key=lambda r: abs((s + median_off) - r))) <= tol
    ]
    if refined:
        refined.sort()
        median_off = refined[len(refined) // 2]
        best_inliers = refined

    denom = max(1, min(len(ref_w), len(sub_w)))
    ratio = len(best_inliers) / float(denom)
    return round(median_off, 3), len(best_inliers), ratio


def _evaluate_transform_across_windows(ref_data, sub_starts, transform_fn, tol=0.55):
    """
    Evaluates a timestamp transformation f(t) across all sampled reference windows.
    Returns (total_inliers, matched_windows_count, active_windows_count, window_details).
    """
    ref_times = ref_data.get("times") or []
    windows = ref_data.get("windows") or [(10.0, 180.0)]
    transformed = [transform_fn(s) for s in sub_starts]

    total_inliers = 0
    matched_windows = 0
    active_windows = 0
    details = []

    for ws, we in windows:
        ref_w = [r for r in ref_times if ws <= r <= we]
        if len(ref_w) < 2:
            continue
        active_windows += 1
        sub_w = [t for t in transformed if (ws - 1.5) <= t <= (we + 1.5)]
        inliers = 0
        for t in sub_w:
            closest = min(ref_w, key=lambda r: abs(t - r))
            if abs(t - closest) <= tol:
                inliers += 1
        req_w = max(2, min(len(ref_w), max(2, len(sub_w))) // 3)
        is_ok = inliers >= req_w
        if is_ok:
            matched_windows += 1
        total_inliers += inliers
        details.append((ws, we, inliers, len(ref_w), is_ok))

    return total_inliers, matched_windows, max(1, active_windows), details


def _solve_full_sync_model(ref_data, sub_cues):
    """
    Solves for the optimal full-episode synchronization model across all sampled windows:
      - Model 0 ('natural'): 0 shift needed, verified across early AND mid/late windows.
      - Model 1 ('constant'): uniform shift t' = t + b verified across windows.
      - Model 2 ('affine_fps'): linear/FPS drift correction t' = a * t + b (fixes 25fps <-> 23.976fps/24fps drift at min 12+).
      - Model 3 ('piecewise'): per-act segment shifts snapped to scene silence gaps (fixes TV commercial break cut differences).
    Returns dict describing the best model, tier (0, 1, or 2), score, and human label.
    """
    if not ref_data or not ref_data.get("times") or len(sub_cues) < 5:
        return {"mode": "none", "tier": 2, "score": 0, "label": ""}

    ref_times = ref_data["times"]
    windows = ref_data.get("windows") or [(10.0, 180.0)]
    tol = 0.68 if ref_data.get("source") == "audio" else 0.55
    sub_starts = [c[0] for c in sub_cues]

    nat_inliers, nat_win_ok, active_wins, _ = _evaluate_transform_across_windows(
        ref_data, sub_starts, lambda t: t, tol=tol
    )

    win_stats = []
    for ws, we in windows:
        ref_w = [r for r in ref_times if ws <= r <= we]
        if len(ref_w) < 2:
            continue
        sub_w = [s for s in sub_starts if (ws - 80.0) <= s <= (we + 80.0)]
        off_w, cnt_w, rat_w = _find_window_offset(ref_w, sub_w, max_shift=75.0, tol=tol)
        req_w = max(2, min(len(ref_w), 8) // 3)
        if cnt_w >= req_w:
            center_t = 0.5 * (ws + we) - off_w
            win_stats.append((center_t, off_w, cnt_w, rat_w, ws, we))

    if win_stats:
        all_small_offsets = all(abs(w[1]) <= 0.35 for w in win_stats)
        has_mid_late_check = any(w[4] >= 300.0 for w in win_stats) if active_wins >= 2 else True
        if all_small_offsets and len(win_stats) >= max(1, active_wins - 1) and has_mid_late_check:
            avg_off = sum(w[1] * w[2] for w in win_stats) / max(1, sum(w[2] for w in win_stats))
            micro_off = round(avg_off, 3) if abs(avg_off) >= 0.12 else 0.0
            return {
                "mode": "constant",
                "a": 1.0,
                "b": micro_off,
                "segments": [],
                "tier": 0,
                "score": nat_inliers + (nat_win_ok * 25) + 50,
                "label": "✅ متزامنة",
            }

    best_model = {
        "mode": "none",
        "a": 1.0,
        "b": 0.0,
        "segments": [],
        "tier": 2,
        "score": nat_inliers,
        "win_ok": nat_win_ok,
        "label": "",
    }

    cand_offsets = set(round(w[1], 3) for w in win_stats)
    if win_stats:
        weighted_b = sum(w[1] * w[2] for w in win_stats) / max(1, sum(w[2] for w in win_stats))
        cand_offsets.add(round(weighted_b, 3))

    for b_cand in cand_offsets:
        inl, w_ok, _, _ = _evaluate_transform_across_windows(ref_data, sub_starts, lambda t, b=b_cand: t + b, tol=tol)
        sc = inl + (w_ok * 22)
        if sc > best_model["score"] and (w_ok >= max(1, active_wins - 1) or inl >= 6):
            sign = "+" if b_cand >= 0 else ""
            best_model = {
                "mode": "constant",
                "a": 1.0,
                "b": b_cand,
                "segments": [],
                "tier": 0 if (abs(b_cand) < 0.35 and w_ok == active_wins) else 1,
                "score": sc,
                "win_ok": w_ok,
                "label": "✅ متزامنة" if (abs(b_cand) < 0.35 and w_ok == active_wins) else f"🔄 تمت المزامنة ({sign}{b_cand:.1f}s)",
            }

    fps_ratios = [
        25.0 / 23.976024,
        23.976024 / 25.0,
        24.0 / 23.976024,
        23.976024 / 24.0,
        25.0 / 24.0,
        24.0 / 25.0,
    ]
    affine_candidates = []
    for a_rat in fps_ratios:
        for ws, we in windows[:2]:
            ref_w = [r for r in ref_times if ws <= r <= we]
            sub_w = [a_rat * s for s in sub_starts if (ws - 80.0) <= (a_rat * s) <= (we + 80.0)]
            b_est, cnt_est, _ = _find_window_offset(ref_w, sub_w, max_shift=75.0, tol=tol)
            if cnt_est >= 2:
                affine_candidates.append((a_rat, b_est))

    if len(win_stats) >= 2:
        for i in range(len(win_stats)):
            for j in range(i + 1, len(win_stats)):
                t1, off1 = win_stats[i][0], win_stats[i][1]
                t2, off2 = win_stats[j][0], win_stats[j][1]
                if (t2 - t1) >= 250.0 and abs(off2 - off1) >= 0.45:
                    a_emp = 1.0 + (off2 - off1) / (t2 - t1)
                    if 0.94 <= a_emp <= 1.06:
                        b_emp = off1 - (a_emp - 1.0) * t1
                        affine_candidates.append((a_emp, round(b_emp, 3)))

    for a_cand, b_cand in affine_candidates:
        inl, w_ok, _, _ = _evaluate_transform_across_windows(
            ref_data, sub_starts, lambda t, a=a_cand, b=b_cand: a * t + b, tol=tol
        )
        sc = inl + (w_ok * 24)
        if w_ok >= 2 and sc > (best_model["score"] + 3):
            best_model = {
                "mode": "affine",
                "a": a_cand,
                "b": b_cand,
                "segments": [],
                "tier": 1,
                "score": sc,
                "win_ok": w_ok,
                "label": "🔄 تمت المزامنة (ضبط FPS + التوقيت)",
            }

    if len(win_stats) >= 2 and best_model.get("win_ok", 0) < len(win_stats):
        offsets_seq = [w[1] for w in win_stats]
        if (max(offsets_seq) - min(offsets_seq)) >= 0.60:
            segments = []
            for idx, w in enumerate(win_stats):
                seg_off = w[1]
                if idx == 0:
                    seg_start = 0.0
                else:
                    prev_we = win_stats[idx - 1][5] - win_stats[idx - 1][1]
                    curr_ws = w[4] - w[1]
                    best_gap_mid = 0.5 * (prev_we + curr_ws)
                    best_gap_dur = 0.0
                    for c_i in range(len(sub_cues) - 1):
                        end_curr = sub_cues[c_i][1]
                        start_next = sub_cues[c_i + 1][0]
                        if prev_we - 20.0 <= end_curr <= curr_ws + 20.0:
                            gap = start_next - end_curr
                            if gap > best_gap_dur:
                                best_gap_dur = gap
                                best_gap_mid = 0.5 * (end_curr + start_next)
                    seg_start = best_gap_mid
                segments.append([seg_start, 999999.0, seg_off])
                if idx > 0:
                    segments[idx - 1][1] = seg_start

            def _piecewise_fn(t, segs=segments):
                for s_st, s_et, s_off in segs:
                    if s_st <= t < s_et:
                        return t + s_off
                return t + segs[-1][2]

            inl, w_ok, _, _ = _evaluate_transform_across_windows(ref_data, sub_starts, _piecewise_fn, tol=tol)
            sc = inl + (w_ok * 22)
            if w_ok > best_model.get("win_ok", 0) and sc > (best_model["score"] + 4):
                best_model = {
                    "mode": "piecewise",
                    "a": 1.0,
                    "b": 0.0,
                    "segments": segments,
                    "tier": 1,
                    "score": sc,
                    "win_ok": w_ok,
                    "label": "🔄 تمت المزامنة (متعددة الفصول)",
                }

    return best_model


def _apply_sync_model_to_srt(text, model):
    """Applies Constant, Affine (FPS a*t+b), or Piecewise Act-Break transformation to all SRT timestamps."""
    if not model or model.get("mode") in (None, "none"):
        return text

    mode = model.get("mode")
    a = float(model.get("a", 1.0))
    b = float(model.get("b", 0.0))
    segments = model.get("segments") or []

    if mode == "constant" and abs(b) < 0.005:
        return text

    def _transform_sec(sec):
        if mode == "constant":
            return sec + b
        if mode == "affine":
            return (a * sec) + b
        if mode == "piecewise" and segments:
            for s_st, s_et, s_off in segments:
                if s_st <= sec < s_et:
                    return sec + s_off
            return sec + segments[-1][2]
        return sec

    def _fmt(ms_val):
        nh = ms_val // 3600000
        nm = (ms_val % 3600000) // 60000
        ns = (ms_val % 60000) // 1000
        nms = ms_val % 1000
        return f"{nh:02d}:{nm:02d}:{ns:02d},{nms:03d}"

    def _shift_match(m):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        st_sec = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        et_sec = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        new_st_ms = max(0, int(round(_transform_sec(st_sec) * 1000)))
        new_et_ms = max(new_st_ms + 450, int(round(_transform_sec(et_sec) * 1000)))
        return f"{_fmt(new_st_ms)} --> {_fmt(new_et_ms)}"

    pattern = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    return re.sub(pattern, _shift_match, text)


def _download_candidate_text_cached(cand, ep_tag=""):
    """Downloads, extracts, cleans, and caches raw subtitle text with SubSource 429 rate-limit protection."""
    global _SUBSOURCE_LAST_REQ

    prov = (cand.get("provider") or "").lower()
    cid = str(cand.get("id") or cand.get("download_url") or "")
    cache_key = (prov, cid, ep_tag)

    now = time.time()
    cached = RAW_SUB_TEXT_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 7200 and cached.get("text"):
        return cached["text"]

    if prov == "subsource":
        with _SUBSOURCE_SEM:
            with _SUBSOURCE_LOCK:
                elapsed = time.time() - _SUBSOURCE_LAST_REQ
                if elapsed < 0.22:
                    time.sleep(0.22 - elapsed)
                _SUBSOURCE_LAST_REQ = time.time()
            text = _do_raw_sub_download(cand, ep_tag)
    else:
        text = _do_raw_sub_download(cand, ep_tag)

    if text:
        RAW_SUB_TEXT_CACHE[cache_key] = {"ts": time.time(), "text": text}
    return text


def _do_raw_sub_download(cand, ep_tag=""):
    import tempfile
    base_name = f"{ep_tag}_sub" if ep_tag else "sub"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = subtitle_manager.download_subtitle_file(cand, tmpdir, file_basename=base_name)
            if not isinstance(dl, dict) or not dl.get("success"):
                return None
            saved_path = dl.get("file_path") or dl.get("path")
            if not saved_path or not os.path.exists(saved_path):
                return None
            with open(saved_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                raw_text = f.read()
            cleaned = subtitle_manager.fix_arabic_rtl_text(raw_text)
            return cleaned if len(cleaned.strip()) > 100 else None
    except Exception:
        return None


def _score_and_sync_subtitle(cand, ref_data, ep_tag=""):
    """
    Downloads subtitle (cached), solves full-episode sync model (Natural / Constant / FPS Affine / Piecewise),
    applies transformation + RTL punctuation fix, and returns (tier, score, sync_label, bytes, signature).
    """
    try:
        text = _download_candidate_text_cached(cand, ep_tag=ep_tag)
        if not text:
            return 3, 0, "", None, None

        sig = subtitle_manager.compute_dialogue_signature(text, is_path=False)
        if sig.get("line_count", 0) < 10:
            return 3, 0, "", None, None

        if not ref_data or not ref_data.get("times"):
            b = ("\ufeff" + text.lstrip("\ufeff")).encode("utf-8")
            return 2, int(cand.get("score", 0)) // 20, "", b, sig

        sub_cues = _parse_srt_all_cues(text)
        model = _solve_full_sync_model(ref_data, sub_cues)
        tier = model.get("tier", 2)
        sync_score = model.get("score", 0)
        sync_label = model.get("label", "")

        if tier in (0, 1):
            text = _apply_sync_model_to_srt(text, model)

        b = ("\ufeff" + text.lstrip("\ufeff")).encode("utf-8")
        return tier, sync_score, sync_label, b, sig
    except Exception:
        return 3, 0, "", None, None


def _resolve_and_cache_sub(token):
    item = SUB_CACHE.get(token)
    if not item:
        return None
    if item.get("bytes"):
        return item["bytes"]

    cand = item.get("candidate")
    if not cand:
        return None

    ref_data = item.get("ref_data")
    ep_tag = item.get("ep_tag") or ""
    try:
        _tier, _score, _lbl, b, _sig = _score_and_sync_subtitle(cand, ref_data, ep_tag=ep_tag)
        if b:
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
    """Arabic subtitle search (SubSource + SubDL + Wyzie) with Multi-Window Full-Episode Synchronization."""
    now = time.time()
    for k in [k for k, v in list(SUB_CACHE.items()) if now - v.get("ts", 0) > 7200]:
        SUB_CACHE.pop(k, None)

    is_series = bool(season is not None and episode is not None)
    release_hint = filename or ""
    cdn_url = None

    meta_entry = STREAM_META_CACHE.get(full_media_id or imdb_id) or STREAM_META_CACHE.get(imdb_id)
    if meta_entry and meta_entry.get("items"):
        items = meta_entry["items"]
        chosen = items[0]
        if filename:
            fn_low = filename.lower()
            for it in items:
                it_title = (it.get("title") or "").lower()
                if it_title == fn_low or fn_low in it_title or it_title in fn_low:
                    chosen = it
                    break
        release_hint = filename or chosen.get("title") or chosen.get("release_type") or ""
        cdn_url = chosen.get("url")

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

    def _get_ref_data():
        if fast_mode or not cdn_url:
            return None
        rd = _extract_multiwindow_ref_timestamps(cdn_url, is_series=is_series)
        return rd if rd else _extract_audio_speech_ref_timestamps(cdn_url, is_series=is_series)

    def _search_subs():
        try:
            res = subtitle_manager.search_all_subtitles(
                query_title, imdb_id=imdb_id, lang=target_lang, release_type=release_hint,
                season=season, episode=episode,
                subdl_key=subdl_key, subsource_key=subsource_key, wyzie_key=wyzie_key,
            )
            return res if isinstance(res, list) else (res.get("ranked_subtitles") or res.get("results") or [])
        except Exception as e:
            logger.warning("Subtitle search error for %s: %s", imdb_id, e)
            return []

    if not fast_mode and cdn_url:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_ref = pool.submit(_get_ref_data)
            f_sub = pool.submit(_search_subs)
            ref_data = f_ref.result()
            candidates = f_sub.result()
    else:
        ref_data = None
        candidates = _search_subs()

    if target_lang == "ar":
        candidates = [
            c for c in candidates
            if str(c.get("language") or c.get("lang") or "").lower() in ("ar", "ara", "arabic", "")
        ]

    ep_tag = f"S{int(season):02d}E{int(episode):02d}" if is_series else ""
    ranked_entries = []

    if not fast_mode and candidates:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ss_list = [c for c in candidates if (c.get("provider") or "").lower() == "subsource"][:3]
        sd_list = [c for c in candidates if (c.get("provider") or "").lower() == "subdl"][:3]
        other_list = [c for c in candidates if c not in ss_list and c not in sd_list]
        eval_pool = (ss_list + sd_list + other_list)[:6]
        eval_pool.sort(key=lambda x: x.get("score", 0), reverse=True)

        raw_evaluated = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(_score_and_sync_subtitle, c, ref_data, ep_tag): (idx, c)
                for idx, c in enumerate(eval_pool)
            }
            for fut in as_completed(futs):
                idx, c = futs[fut]
                try:
                    tier, sync_score, sync_label, b, sig = fut.result()
                except Exception:
                    tier, sync_score, sync_label, b, sig = 3, 0, "", None, None
                if b:
                    raw_evaluated.append((tier, -sync_score, idx, c, sync_label, b, sig))

        raw_evaluated.sort(key=lambda x: (x[0], x[1], x[2]))

        seen_sigs = []
        for entry in raw_evaluated:
            tier, neg_sc, idx, c, sync_label, b, sig = entry
            if sig and sig.get("sample_set"):
                if any(subtitle_manager.are_signatures_duplicate(sig, ex_sig, threshold=0.82) for ex_sig in seen_sigs):
                    continue
                seen_sigs.append(sig)
            ranked_entries.append((tier, neg_sc, idx, c, sync_label, b))

        if not ranked_entries:
            for idx, c in enumerate(candidates[:6]):
                ranked_entries.append((2, 0, idx, c, "", None))
    else:
        for idx, c in enumerate(candidates[:6]):
            ranked_entries.append((2, 0, idx, c, "", None))

    subs = []
    to_prefetch = []
    for pos, (tier, _neg_score, idx, c, sync_label, pre_bytes) in enumerate(ranked_entries[:6]):
        token = uuid.uuid4().hex[:16]
        trans = c.get("translator") or c.get("author") or ""
        prov = (c.get("provider") or "").upper()
        name = c.get("name") or c.get("release_name") or ""
        label_parts = []
        if sync_label:
            label_parts.append(sync_label)
        if prov:
            label_parts.append(prov)
        if trans:
            label_parts.append(trans)
        elif name:
            label_parts.append(name[:28])
        label = " • ".join(label_parts) if label_parts else f"Arabic #{pos+1}"

        SUB_CACHE[token] = {
            "candidate": c,
            "ref_data": ref_data,
            "ep_tag": ep_tag,
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
