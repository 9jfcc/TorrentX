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
import sys
import json
import time
import uuid
import base64
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import debrid_manager
import subtitle_manager

PORT = int(os.environ.get("TORRENTX_ADDON_PORT", "7123"))
SUB_CACHE = {}          # token -> {"bytes": b"...", "ts": float}
STREAM_META_CACHE = {}  # media_id -> {"ts": float, "items": [{"title", "release_type", "url"}]}


def build_manifest(mode_label="Auto"):
    return {
        "id": "com.9jfcc.torrentx.addon",
        "version": "2.0.0",
        "name": f"TorrentX ({mode_label})",
        "description": (
            "Strict 4-Tier Anti-Upscale Movie/Series Streams (Cached-Only for Debrid, "
            "High-Seeder Only for P2P) + Exclusive SubSource/SubDL Arabic Subtitles."
        ),
        "resources": ["stream", "subtitles"],
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
    if not seg:
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
    except Exception:
        return []


def format_streams(raw_streams, mode, svc_id, media_id=None):
    """Filters, ranks by Tier (Best -> Worst), and formats into Stremio stream objects."""
    if not raw_streams:
        return []

    candidates = _rank_all_candidates(raw_streams, mode)
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
        size_str = f"{c['size_gb']:.1f} GB" if c["size_gb"] > 0 else "?"
        tier_short = {
            1: "Tier 1 • 4K Remux",
            2: "Tier 2 • 1080p Remux",
            3: "Tier 3 • 4K WEB-DL",
            4: "Tier 4 • 1080p WEB-DL",
            5: f"Tier 5 • {res} Encode",
        }.get(tier, f"Tier {tier}")

        if mode == "debrid" and item.get("is_cached"):
            badge = f"⚡ {svc_id.upper()} CACHED"
        else:
            badge = f"🌱 {c['seeders']} Seeders" + (" (Uncached)" if mode == "debrid" else "")

        stream_obj = {
            "name": f"TorrentX\n{tier_short}\n{badge}",
            "title": f"{c['title']}\n💾 {size_str} | {c['tier_label']}",
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


def _rank_all_candidates(raw_streams, mode):
    """Ranks all safe, anti-upscale candidates strictly by Tier (1->5) then quality/seeders."""
    remaining = list(raw_streams)
    ranked_cached = []
    ranked_p2p = []
    seen = set()

    for _ in range(30):
        if not remaining:
            break
        best, top6 = debrid_manager.select_best_movie_release(remaining)
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

    # P2P mode OR Debrid fallback when 0 cached torrents exist on the server
    ranked_p2p.sort(key=lambda x: (x["tier"], -x.get("seeders", 0), -x.get("q_score", 0)))
    return ranked_p2p


def _extract_first_2min_ref_timestamps(media_url, timeout=8):
    """Reads ONLY the first ~3 minutes of the best non-forced embedded subtitle PTS timestamps from an HTTP CDN URL."""
    import shutil
    import subprocess
    if not media_url or not media_url.startswith("http"):
        return []
    ffprobe = shutil.which("ffprobe") or os.path.expanduser(r"~\AppData\Local\Harbor\ffprobe.exe")
    if not os.path.exists(ffprobe) and not shutil.which("ffprobe"):
        return []
    try:
        # Step A: Pick best non-forced English (or first) subtitle stream index quickly
        stream_sel = "s:0"
        probe_s = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-select_streams", "s", media_url],
            capture_output=True, text=True, timeout=4, encoding="utf-8", errors="replace"
        )
        if probe_s.returncode == 0 and probe_s.stdout:
            s_list = json.loads(probe_s.stdout).get("streams") or []
            for idx, st in enumerate(s_list):
                tags = st.get("tags") or {}
                lang = str(tags.get("language") or "").lower()
                title = str(tags.get("title") or "").lower()
                forced = (st.get("disposition") or {}).get("forced") == 1 or "forced" in title
                if not forced and lang in ("eng", "en", "und", ""):
                    stream_sel = f"s:{idx}"
                    break

        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-select_streams", stream_sel, "-show_packets",
            "-read_intervals", "%+180", media_url,
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
        return clean[:50]
    except Exception:
        return []


def _parse_srt_first_cues(srt_text):
    """Returns start timestamps (seconds) of the first 35 cues in an SRT string."""
    import re
    cues = []
    for m in re.finditer(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->", srt_text):
        h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        sec = h * 3600 + mn * 60 + s + ms / 1000.0
        if 1.0 <= sec <= 185.0:
            cues.append(sec)
        if len(cues) >= 35:
            break
    return cues


def _compute_fast_offset(ref_times, sub_times):
    """Finds median consensus offset (ref - sub) in seconds with ~0.05s precision within [-45s, +45s]."""
    if len(ref_times) < 4 or len(sub_times) < 4:
        return 0.0
    best_cand = 0.0
    best_inliers = []
    for r0 in ref_times[:10]:
        for s0 in sub_times[:10]:
            cand = r0 - s0
            if abs(cand) > 45.0:
                continue
            inliers = []
            for st in sub_times:
                # Find closest ref timestamp to (st + cand)
                closest = min(ref_times, key=lambda rt: abs((st + cand) - rt))
                if abs((st + cand) - closest) <= 0.45:
                    inliers.append(closest - st)
            if len(inliers) > len(best_inliers):
                best_inliers = inliers
                best_cand = cand
    if len(best_inliers) >= max(4, min(len(sub_times), 8) // 2):
        best_inliers.sort()
        median_off = best_inliers[len(best_inliers) // 2]
        if abs(median_off) >= 0.15:
            return round(median_off, 3)
    return 0.0


def fetch_subtitles_for_stremio(imdb_id, host_base, season=None, episode=None, filename=None, full_media_id=None):
    """Fetches Arabic subtitles (SubSource + SubDL) matched by release_type + fast 2-min CDN offset sync."""
    import tempfile
    now = time.time()
    for k in [k for k, v in SUB_CACHE.items() if now - v["ts"] > 7200]:
        SUB_CACHE.pop(k, None)

    # 1. Match stream metadata (release_type + CDN URL) from STREAM_META_CACHE
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

    # 2. Extract first ~2.5 min reference timestamps from CDN URL (if available)
    ref_times = _extract_first_2min_ref_timestamps(cdn_url) if cdn_url else []

    # 3. Resolve canonical movie title for better SubDL/SubSource matching
    query_title = imdb_id
    try:
        _, canonical_title, _ = debrid_manager.resolve_movie_identity(imdb_id)
        if canonical_title:
            query_title = canonical_title
    except Exception:
        pass

    subs = []
    try:
        res = subtitle_manager.search_all_subtitles(
            query_title, imdb_id=imdb_id, lang="ar", release_type=release_type, season=season, episode=episode
        )
        candidates = res if isinstance(res, list) else (res.get("ranked_subtitles") or res.get("results") or [])
    except Exception:
        candidates = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, c in enumerate(candidates[:10]):
            try:
                token = uuid.uuid4().hex[:16]
                dl = subtitle_manager.download_subtitle_file(c, tmpdir, file_basename=token)
                saved_path = (dl.get("file_path") or dl.get("path")) if isinstance(dl, dict) else (dl if isinstance(dl, str) else None)
                if not saved_path or not os.path.exists(saved_path):
                    continue
                with open(saved_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = subtitle_manager.clean_subtitle_ads(f.read())

                sync_tag = ""
                if ref_times and hasattr(subtitle_manager, "shift_subtitle_timestamps"):
                    sub_times = _parse_srt_first_cues(text)
                    offset = _compute_fast_offset(ref_times, sub_times)
                    if offset != 0.0:
                        text = subtitle_manager.shift_subtitle_timestamps(text, offset)
                        sync_tag = f" [Synced {offset:+.1f}s]"

                SUB_CACHE[token] = {"bytes": text.encode("utf-8"), "ts": now}
                label = c.get("release_name") or c.get("source") or f"Arabic #{idx+1}"
                subs.append({
                    "id": f"tx-{token}",
                    "lang": "ara",
                    "url": f"{host_base}/sub_dl/{token}.srt",
                    "SubEncoding": "UTF-8",
                    "label": f"[TorrentX{sync_tag}] {label}",
                })
            except Exception:
                continue
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
  p { color: #cbd5e1; font-size: 16px; }
  label { display: block; margin-top: 18px; font-weight: 600; font-size: 15px; }
  select, input { width: 100%; padding: 14px; margin-top: 8px; border-radius: 10px; border: 1px solid #475569; background: #0f172a; color: #fff; font-size: 15px; }
  .api-row { display: flex; gap: 10px; align-items: stretch; margin-top: 8px; }
  .api-row input { margin-top: 0; flex: 1; }
  .btn-api { display: inline-flex; align-items: center; justify-content: center; white-space: nowrap; padding: 0 18px; border-radius: 10px; background: #10b981; color: #fff; font-weight: 700; text-decoration: none; font-size: 14px; transition: background 0.15s; }
  .btn-api:hover { background: #059669; }
  button { margin-top: 24px; width: 100%; padding: 15px; border: none; border-radius: 10px; background: #3b82f6; color: #fff; font-size: 17px; font-weight: bold; cursor: pointer; }
  button:hover { background: #2563eb; }
  .out { margin-top: 20px; padding: 18px; background: #0f172a; border: 1px solid #334155; border-radius: 10px; word-break: break-all; display: none; }
  .actions { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
  .actions button, .actions a { margin-top: 0; flex: 1; min-width: 160px; padding: 12px; border-radius: 8px; text-align: center; text-decoration: none; font-weight: 700; font-size: 14px; }
  .btn-copy { background: #475569; color: #fff; }
  .btn-install { background: #8b5cf6; color: #fff; }
</style>
</head>
<body>
<div class="page">
  <div class="card">
    <h2>⚡ إعداد إضافة TorrentX</h2>
    <p>اختر وضع التشغيل (مع خدمة ديبرد للكاش السحابي، أو بدون ديبرد لنسخ التورنت ذات السييدرز النشطين فقط):</p>
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
      <label>مفتاح الـ API</label>
      <div class="api-row">
        <input id="apikey" type="password" placeholder="ضع مفتاح API هنا">
        <a id="apiLink" class="btn-api" href="https://torbox.app/settings?section=account" target="_blank" rel="noopener">🔑 جلب مفتاح API</a>
      </div>
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
    api_key: document.getElementById('apikey').value.trim()
  };
  const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(cfg)))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
  lastUrl = window.location.origin + '/' + b64 + '/manifest.json';
  const stremioUrl = lastUrl.replace(/^https?:\\/\\//, 'stremio://');
  const el = document.getElementById('out');
  el.style.display = 'block';
  el.innerHTML = '<b>رابط الإضافة (انسخه في Harbor / Stremio / Nuvio):</b><br><br>' + lastUrl +
    '<div class="actions"><button class="btn-copy" onclick="navigator.clipboard.writeText(lastUrl)">📋 نسخ الرابط</button>' +
    '<a class="btn-install" href="' + stremioUrl + '">⚡ تثبيت مباشر</a></div>';
}
</script>
</body>
</html>
"""


class AddonHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type="text/plain; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.strip("/")
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]

        if not parts or parts == ["configure"]:
            return self._send_bytes(CONFIGURE_HTML.encode("utf-8"), "text/html; charset=utf-8")

        if parts[0] == "sub_dl" and len(parts) == 2:
            token = parts[1].replace(".srt", "")
            item = SUB_CACHE.get(token)
            if not item:
                return self._send_bytes(b"", status=404)
            return self._send_bytes(item["bytes"], "application/x-subrip; charset=utf-8")

        # Optional config segment at parts[0]
        url_cfg = parse_config_segment(parts[0])
        if url_cfg is not None:
            parts = parts[1:]

        mode, svc_id, api_key = resolve_runtime_debrid(url_cfg)
        mode_label = f"{svc_id.upper()} Cached" if mode == "debrid" else "P2P Seeders"

        if parts == ["manifest.json"]:
            return self._send_json(build_manifest(mode_label))

        if len(parts) == 3 and parts[0] == "stream" and parts[2].endswith(".json"):
            media_type = parts[1]
            media_id = parts[2][:-5]
            raw = fetch_torrentio_streams(media_type, media_id, svc_id, api_key)
            streams = format_streams(raw, mode, svc_id, media_id=media_id)
            return self._send_json({"streams": streams})

        if len(parts) == 3 and parts[0] == "subtitles" and parts[2].endswith(".json"):
            media_type = parts[1]
            media_id = parts[2][:-5]
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            filename = (qs.get("filename") or [None])[0]
            id_parts = media_id.split(":")
            imdb_id = id_parts[0]
            season = int(id_parts[1]) if len(id_parts) > 1 and id_parts[1].isdigit() else None
            episode = int(id_parts[2]) if len(id_parts) > 2 and id_parts[2].isdigit() else None
            host_base = f"http://{self.headers.get('Host') or f'127.0.0.1:{PORT}'}"
            subs = fetch_subtitles_for_stremio(
                imdb_id, host_base, season=season, episode=episode, filename=filename, full_media_id=media_id
            )
            return self._send_json({"subtitles": subs})

        return self._send_json({"error": "Not found"}, status=404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), AddonHandler)
    print(f"TorrentX Stremio/Harbor/Nuvio Addon running at http://127.0.0.1:{PORT}/manifest.json")
    print(f"Configure page: http://127.0.0.1:{PORT}/configure")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
