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
SUB_CACHE = {}  # token -> {"bytes": b"...", "ts": float}


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


def format_streams(raw_streams, mode, svc_id):
    """Filters, ranks by Tier (Best -> Worst), and formats into Stremio stream objects."""
    if not raw_streams:
        return []

    # Reuse debrid_manager's strict anti-upscale & security filter
    best, _ = debrid_manager.select_best_movie_release(raw_streams)
    # Re-run full pool ranking by calling internal logic cleanly:
    # select_best_movie_release returns top 6; let's rank all eligible candidates up to 25
    # by temporarily inspecting all parsed candidates via a clean helper or using select_best_movie_release.
    # Let's parse all raw_streams cleanly using the exact same rules:
    candidates = _rank_all_candidates(raw_streams, mode)

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

        if mode == "debrid":
            badge = f"⚡ {svc_id.upper()} CACHED"
        else:
            badge = f"🌱 {c['seeders']} Seeders"

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
    # Use select_best_movie_release in batches? Even better: parse with the exact same rules
    # by leveraging debrid_manager's select_best_movie_release on slices or directly:
    remaining = list(raw_streams)
    ranked = []
    seen = set()

    # Iteratively extract top candidates up to 25 using debrid_manager's exact engine
    for _ in range(25):
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
            if mode == "debrid" and not item.get("is_cached"):
                continue
            if mode == "p2p" and item.get("seeders", 0) <= 0:
                continue
            seen.add(h)
            ranked.append(item)
            added_any = True
        # Remove seen hashes from remaining
        remaining = [
            s for s in remaining
            if (s.get("infoHash") or s.get("url") or s.get("title")) not in seen
        ]
        if not added_any:
            break

    # Strict sort: Tier ascending (1 is best -> 5 is worst), then cached/seeders/score descending
    if mode == "p2p":
        ranked.sort(key=lambda x: (x["tier"], -x.get("seeders", 0), -x.get("q_score", 0)))
    else:
        ranked.sort(key=lambda x: (x["tier"], -x.get("q_score", 0)))
    return ranked


def fetch_subtitles_for_stremio(imdb_id, host_base, season=None, episode=None):
    """Fetches Arabic subtitles from SubSource + SubDL only and serves them via /sub_dl/<token>.srt."""
    import tempfile
    now = time.time()
    expired = [k for k, v in SUB_CACHE.items() if now - v["ts"] > 7200]
    for k in expired:
        SUB_CACHE.pop(k, None)

    subs = []
    try:
        res = subtitle_manager.search_all_subtitles(
            imdb_id, imdb_id=imdb_id, lang="ar", season=season, episode=episode
        )
        candidates = res if isinstance(res, list) else (res.get("ranked_subtitles") or res.get("results") or [])
    except Exception:
        candidates = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, c in enumerate(candidates[:10]):
            try:
                token = uuid.uuid4().hex[:16]
                dl = subtitle_manager.download_subtitle_file(c, tmpdir, file_basename=token)
                saved_path = dl.get("path") if isinstance(dl, dict) else None
                if not saved_path or not os.path.exists(saved_path):
                    continue
                with open(saved_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = subtitle_manager.clean_subtitle_ads(f.read())
                SUB_CACHE[token] = {"bytes": text.encode("utf-8"), "ts": now}
                label = c.get("release_name") or c.get("source") or f"Arabic #{idx+1}"
                subs.append({
                    "id": f"tx-{token}",
                    "lang": "ara",
                    "url": f"{host_base}/sub_dl/{token}.srt",
                    "SubEncoding": "UTF-8",
                    "label": f"[TorrentX] {label}",
                })
            except Exception:
                continue
    return subs


CONFIGURE_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<title>إعداد إضافة TorrentX</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; max-width: 560px; margin: 40px auto; padding: 24px; }
  .card { background: #1e293b; border-radius: 12px; padding: 24px; border: 1px solid #334155; }
  label { display: block; margin-top: 14px; font-weight: 600; }
  select, input { width: 100%; padding: 10px; margin-top: 6px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #fff; box-sizing: border-box; }
  button { margin-top: 20px; width: 100%; padding: 12px; border: none; border-radius: 8px; background: #3b82f6; color: #fff; font-size: 16px; font-weight: bold; cursor: pointer; }
  button:hover { background: #2563eb; }
  .out { margin-top: 16px; padding: 12px; background: #0f172a; border-radius: 8px; word-break: break-all; display: none; }
</style>
</head>
<body>
<div class="card">
  <h2>⚡ إعداد إضافة TorrentX</h2>
  <p>اختر وضع التشغيل (بـ ديبرد للكاش السحابي فقط، أو بدون ديبرد للسييدرز النشطين فقط):</p>
  <label>وضع التشغيل (Mode)</label>
  <select id="mode" onchange="toggleDebrid()">
    <option value="debrid">مع خدمة ديبرد (Cached-Only)</option>
    <option value="p2p">بدون ديبرد (P2P - Seeders Only)</option>
  </select>
  <div id="debridBox">
    <label>خدمة الديبرد</label>
    <select id="service">
      <option value="torbox">TorBox</option>
      <option value="realdebrid">Real-Debrid</option>
      <option value="alldebrid">AllDebrid</option>
      <option value="premiumize">Premiumize</option>
      <option value="debridlink">Debrid-Link</option>
      <option value="offcloud">Offcloud</option>
      <option value="putio">Put.io</option>
    </select>
    <label>مفتاح الـ API</label>
    <input id="apikey" type="password" placeholder="ضع مفتاح API هنا">
  </div>
  <button onclick="gen()">إنشاء رابط التثبيت</button>
  <div id="out" class="out"></div>
</div>
<script>
function toggleDebrid() {
  document.getElementById('debridBox').style.display =
    document.getElementById('mode').value === 'debrid' ? 'block' : 'none';
}
function gen() {
  const cfg = {
    mode: document.getElementById('mode').value,
    service: document.getElementById('service').value,
    api_key: document.getElementById('apikey').value.trim()
  };
  const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(cfg)))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
  const url = window.location.origin + '/' + b64 + '/manifest.json';
  const el = document.getElementById('out');
  el.style.display = 'block';
  el.innerHTML = '<b>رابط الإضافة (انسخه في Harbor / Stremio / Nuvio):</b><br><br>' + url;
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
            streams = format_streams(raw, mode, svc_id)
            return self._send_json({"streams": streams})

        if len(parts) == 3 and parts[0] == "subtitles" and parts[2].endswith(".json"):
            media_type = parts[1]
            media_id = parts[2][:-5]
            id_parts = media_id.split(":")
            imdb_id = id_parts[0]
            season = int(id_parts[1]) if len(id_parts) > 1 and id_parts[1].isdigit() else None
            episode = int(id_parts[2]) if len(id_parts) > 2 and id_parts[2].isdigit() else None
            host_base = f"http://{self.headers.get('Host') or f'127.0.0.1:{PORT}'}"
            subs = fetch_subtitles_for_stremio(imdb_id, host_base, season=season, episode=episode)
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
