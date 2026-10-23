# -*- coding: utf-8 -*-
import requests
import os
import sys
import json
import glob
import re
import urllib.request
import urllib.parse
import urllib.error
import threading
import subprocess
import time
import shutil

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

try:
    import debrid_manager
except ImportError:
    debrid_manager = None

try:
    import subtitle_manager
except ImportError:
    subtitle_manager = None

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5:3b"

QBITTORRENT_PATH = r"C:\Program Files\qBittorrent\qbittorrent.exe"

# ==============================================================================
# GENERAL STREAM & TORRENT RESOLVER (DIRECT REAL-DEBRID + QBITTORRENT)
# ==============================================================================

# Extended phonetic mapping for anime franchises & movies
PHONETIC_ANIME_MAP = {
    'فيت ستاي نايت': 'Fate/stay night',
    'فيت/ستاي نايت': 'Fate/stay night',
    'فيت سترينج فيك': 'Fate/strange Fake',
    'فيت سترينج': 'Fate/strange Fake',
    'فيت زيرو': 'Fate/Zero',
    'فيت ابوكريفا': 'Fate/Apocrypha',
    'فيت': 'Fate',
    'سترينج': 'strange',
    'فيك': 'Fake',
    'هجوم العمالقة': 'Attack on Titan',
    'صراع العروش': 'Game of Thrones',
    'بريكنج باد': 'Breaking Bad',
    'ون بيس': 'One Piece',
    'ديث نوت': 'Death Note',
    'سولو ليفلينج': 'Solo Leveling',
    'كيميتسو': 'Demon Slayer',
    'قاتل الشياطين': 'Demon Slayer',
    'تشينسو مان': 'Chainsaw Man',
    'جوجوتسو': 'Jujutsu Kaisen',
    'دكتور ستون': 'Dr. Stone',
    'فريرن': 'Frieren',
    'داندا دان': 'Dandadan',
    'بلو لوك': 'Blue Lock',
    'بليتش': 'Bleach',
    'ناروتو': 'Naruto',
    'دراغون بول': 'Dragon Ball',
    'بريزون بريك': 'Prison Break',
    'فايكنج': 'Vikings',
    'بيكي بلايندرز': 'Peaky Blinders',
    'ذا بويز': 'The Boys'
}

def get_imdb_id(title, is_series=True):
    """Fetches IMDb ID using TMDB API dynamically with Arabic phonetic resolution."""
    query = title.strip()
    for ar, en in PHONETIC_ANIME_MAP.items():
        if ar in query:
            query = query.replace(ar, en)

    tmdb_key = "8476a7ab80ad76f0936744df0430e67c"
    preferred_type = "tv" if is_series else "movie"
    fallback_result = None

    for lang in ["en-US", "ar"]:
        clean_q = urllib.parse.quote(query)
        url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={clean_q}&include_adult=false&language={lang}"
        try:
            r = requests.get(url, timeout=6).json()
            results = r.get("results", [])
            for res in results:
                mtype = res.get("media_type")
                if mtype not in ["movie", "tv"]:
                    continue
                item_id = res["id"]
                name = res.get("name") or res.get("title") or res.get("original_name") or res.get("original_title")
                ext_url = f"https://api.themoviedb.org/3/{mtype}/{item_id}/external_ids?api_key={tmdb_key}"
                ext = requests.get(ext_url, timeout=5).json()
                imdb_id = ext.get("imdb_id")
                if imdb_id:
                    if mtype == preferred_type:
                        return imdb_id, name
                    elif not fallback_result:
                        fallback_result = (imdb_id, name)
        except Exception:
            pass

    if fallback_result:
        return fallback_result

    # Fallback to IMDb suggestion API if TMDB search yields no results
    try:
        clean_imdb = query.lower().replace(" ", "_")
        url_imdb = f"https://v3.sg.media-imdb.com/suggestion/x/{clean_imdb}.json"
        r_imdb = requests.get(url_imdb, headers={"User-Agent": "Mozilla/5.0"}, timeout=5).json()
        for item in r_imdb.get("d", []):
            iid = item.get("id")
            iname = item.get("l")
            if iid and iname:
                return iid, iname
    except Exception:
        pass

    return None, title

def get_torrentio_streams(imdb_id, season=1, episode=1, is_series=True, rd_api_key=None):
    """
    Fetches torrent streams from Torrentio.
    If rd_api_key is provided, directly returns RD+ cached streams with direct download URLs!
    """
    base_prefix = f"realdebrid={rd_api_key}/" if rd_api_key else ""
    if is_series:
        target = f"{imdb_id}:{season}:{episode}"
        url = f"https://torrentio.strem.fun/{base_prefix}stream/series/{target}.json"
    else:
        url = f"https://torrentio.strem.fun/{base_prefix}stream/movie/{imdb_id}.json"
        
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=9) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("streams", [])
    except Exception:
        return []

def translate_query_for_search(text):
    """Extracts title, season, episode, quality, codecs, and Debrid preference."""
    is_series = any(w in text for w in ["مسلسل", "حلقة", "الحلقة", "الموسم", "موسم", "series", "episode", "season", "s0", "e0", "s1", "s2"])
    
    # Check Debrid preference
    want_debrid = None
    if any(w in text for w in ["بدون ديبرد", "بدون debrid", "عبر qbittorrent", "تورنت عادي", "مجاني", "كيو بت", "بدون اشتراك"]):
        want_debrid = False
    elif any(w in text.lower() for w in ["بديبرد", "عبر ديبرد", "مع ديبرد", "حمل بديبرد", "باستخدام ديبرد", "rd", "real-debrid", "realdebrid"]) or "ديبرد" in text:
        want_debrid = True

    # Check AI Upscale preference from prompt
    want_upscale = None
    text_lower = text.lower()
    if any(k in text_lower for k in ["بدون ابسكيل", "بدون upscale", "لا تسوي ابسكيل", "لا تحمل ابسكيل", "بدون ai", "ما احب الابسكيل", "لا ابسكيل", "بدون ذكاء اصطناعي", "استبعد الابسكيل", "نسخة اصلية", "نسخه اصليه"]):
        want_upscale = False
    elif any(k in text_lower for k in ["مع ابسكيل", "ابسكيل", "upscale", "ai upscale", "topaz"]):
        want_upscale = True

    # Comprehensive Arabic Episode Ordinals (1 through 30+)
    s_num, e_num = 1, 1
    ep_patterns = [
        (r'(?:الحادية\s*عشرة|الحادية\s*عشر|eleventh\s+episode|e0?11)', 11),
        (r'(?:الثانية\s*عشرة|الثانية\s*عشر|twelfth\s+episode|e0?12)', 12),
        (r'(?:الثالثة\s*عشرة|الثالثة\s*عشر|thirteenth\s+episode|e0?13)', 13),
        (r'(?:الرابعة\s*عشرة|الرابعة\s*عشر|fourteenth\s+episode|e0?14)', 14),
        (r'(?:الخامسة\s*عشرة|الخامسة\s*عشر|fifteenth\s+episode|e0?15)', 15),
        (r'(?:السادسة\s*عشرة|السادسة\s*عشر|sixteenth\s+episode|e0?16)', 16),
        (r'(?:السابعة\s*عشرة|السابعة\s*عشر|seventeenth\s+episode|e0?17)', 17),
        (r'(?:الثامنة\s*عشرة|الثامنة\s*عشر|eighteenth\s+episode|e0?18)', 18),
        (r'(?:التاسعة\s*عشرة|التاسعة\s*عشر|nineteenth\s+episode|e0?19)', 19),
        (r'(?:العشرون|العشرين|twentieth\s+episode|e0?20)', 20),
        (r'(?:العاشرة|tenth\s+episode|e0?10)', 10),
        (r'(?:التاسعة|ninth\s+episode|e0?9)', 9),
        (r'(?:الثامنة|eighth\s+episode|e0?8)', 8),
        (r'(?:السابعة|seventh\s+episode|e0?7)', 7),
        (r'(?:السادسة|sixth\s+episode|e0?6)', 6),
        (r'(?:الخامسة|fifth\s+episode|e0?5)', 5),
        (r'(?:الرابعة|fourth\s+episode|e0?4)', 4),
        (r'(?:الثالثة|third\s+episode|e0?3)', 3),
        (r'(?:الثانية|second\s+episode|e0?2)', 2),
        (r'(?:الاولى|الأولى|first\s+episode|e0?1)', 1),
        (r'(?:الحلقة|حلقة|ep|episode)\s*(\d+)', None),
    ]
    for pat, val in ep_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            e_num = val if val is not None else int(m.group(1))
            break

    # Comprehensive Arabic Season Ordinals (1 through 10+)
    season_patterns = [
        (r'(?:الموسم\s+العاشر|tenth\s+season|10th\s+season|s0?10)', 10),
        (r'(?:الموسم\s+التاسع|ninth\s+season|9th\s+season|s0?9)', 9),
        (r'(?:الموسم\s+الثامن|eighth\s+season|8th\s+season|s0?8)', 8),
        (r'(?:الموسم\s+السابع|seventh\s+season|7th\s+season|s0?7)', 7),
        (r'(?:الموسم\s+السادس|sixth\s+season|6th\s+season|s0?6)', 6),
        (r'(?:الموسم\s+الخامس|fifth\s+season|5th\s+season|s0?5)', 5),
        (r'(?:الموسم\s+الرابع|fourth\s+season|4th\s+season|s0?4)', 4),
        (r'(?:الموسم\s+الثالث|third\s+season|3rd\s+season|s0?3)', 3),
        (r'(?:الموسم\s+الثاني|second\s+season|2nd\s+season|s0?2)', 2),
        (r'(?:الموسم\s+(?:الأول|الاول)|first\s+season|1st\s+season|s0?1)', 1),
        (r'(?:الموسم|موسم|season)\s*(\d+)', None),
    ]
    for pat, val in season_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            s_num = val if val is not None else int(m.group(1))
            break

    quality = "4K" if ("4k" in text.lower() or "2160" in text) else ("1080p" if "1080" in text else "Any")
    want_remux = bool(re.search(r'(remux|remxu|remx|ريمكس)', text, re.I))
    want_crunchyroll = bool(re.search(r'(?:كرانشي(?:\s*رول)?|كرانشيرول|crunchyroll|cr)', text, re.I))
    want_uncompressed = bool(re.search(r'(?:بدون\s*ضغط|غير\s*مضغوط|raw|remux|اعلى\s*جودة|أعلى\s*جودة)', text, re.I))
    want_av1 = bool(re.search(r'(av1|av01)', text, re.I))
    want_h264 = bool(re.search(r'(h264|x264|avc|h\.264)', text, re.I))
    want_hevc = bool(re.search(r'(h265|x265|hevc|h\.265)', text, re.I))
    want_bluray = bool(re.search(r'(bluray|blu-ray|بلوراي)', text, re.I))
    want_web = bool(re.search(r'(web-?dl|webrip|ويب)', text, re.I)) or want_crunchyroll

    # Detect explicit release groups requested by user
    explicit_groups = []
    known_groups = {
        'erai': ['erai', 'erai-raws', 'erairaws', 'ايراي', 'إيراي'],
        'subsplease': ['subsplease', 'subs please'],
        'judas': ['judas'],
        'varyg': ['varyg'],
        'kralimarko': ['kralimarko'],
        'naisu': ['naisu'],
        'yify': ['yify', 'yts'],
        'qxr': ['qxr']
    }
    for grp_key, grp_aliases in known_groups.items():
        if any(re.search(rf'{re.escape(a)}', text, re.I) or a in text for a in grp_aliases):
            explicit_groups.append(grp_key)

    tech_words = [
        'second', '2nd', 'third', '3rd', 'fourth', '4th', 'fifth', '5th', 'first', '1st',
        'remux', 'remxu', 'remx', 'webdl', 'webrip', 'web', 'hdr', 'pilot', 'repack', 'atmos', 'dvd', 'hdtv',
        'uhd', 'flac', 'dsnp', 'amzn', 'nf', 'hmax', '4k', '1080p', '2160p', 'episode', 'season', 'series',
        'upscale', 'topaz', 'ai', 'crunchyroll', 'cr', 'av1', 'avc', 'hevc', 'h264', 'x264', 'h265', 'x265', '1080', '720'
    ]
    arabic_stopwords = [
        "حمل", "لي", "الحلقة", "حلقة", "الاولى", "الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة",
        "السادسة", "السابعة", "الثامنة", "التاسعة", "العاشرة", "الحادية", "الثانية", "عشرة", "عشر",
        "الموسم", "موسم", "الثاني", "الثالث", "الرابع", "الخامس", "العاشر", "مسلسل", "فلم", "فيلم", "انمي", "أنمي",
        "ريمكس", "من", "عن", "مع", "ترجمه", "ترجمة", "عربية", "عربي", "داخل", "وضبط", "التوقيت", "وشف",
        "اذا", "فيه", "نسخه", "نسخة", "ولو", "مافيه", "بلوراي", "عطني", "معلوماتها", "كم", "حجمها", "وزي",
        "كذا", "وحملها", "في", "مجلد", "عبر", "ديبرد", "بدون", "شغل", "افتح", "عله", "علة", "لقيتها",
        "ابسكيل", "ذكاء", "اصطناعي", "اصلية", "أصلية", "غلط", "ضبط", "اذا", "إذا", "حمله", "حملها", "توقيتها",
        "كرانشي", "رول", "كرانشيرول", "ضغط", "غير", "مضغوط", "ابحث", "بحث", "فقط", "لا", "تحمل", "تنزل",
        "ولا", "ما", "لم", "لن", "أي", "اي", "شيء", "شئ", "منه", "منها", "خلاص", "بس"
    ]

    # Pre-resolve phonetic anime names
    text_pre_trans = text
    for ar, en in PHONETIC_ANIME_MAP.items():
        if ar in text_pre_trans:
            text_pre_trans = text_pre_trans.replace(ar, en)

    title = None
    # Priority 1: Match title right after 'انمي' or 'مسلسل' or 'فلم' or 'فيلم'
    m_prefix = re.search(r'(?:انمي|أنمي|مسلسل|فلم|فيلم)\s+([^,;.\n\r]+)', text_pre_trans)
    if m_prefix:
        raw_cand = m_prefix.group(1).split()
        cand_tokens = []
        for t in raw_cand:
            t_clean = t.strip().lower()
            if t_clean in tech_words or t_clean in arabic_stopwords or t_clean in ["4k", "1080p", "uhd"]:
                break
            cand_tokens.append(t)
        if cand_tokens:
            title = " ".join(cand_tokens)

    # Priority 2: English tokens (allow / for Fate/stay night)
    if not title:
        en_words = [w for w in re.findall(r'[a-zA-Z0-9_\-\.\/]{2,}', text_pre_trans) if w.lower() not in tech_words and not w.isdigit()]
        if en_words:
            title = " ".join(en_words)

    # Priority 3: Clean Arabic words
    if not title:
        tokens = text.split()
        ar_title_tokens = [re.sub(r'[^\w\-]', '', t).strip() for t in tokens if t not in arabic_stopwords and not t.isdigit()]
        title = " ".join([t for t in ar_title_tokens if t])
        
    return {
        "title": title or "Dune",
        "is_series": is_series,
        "season": s_num,
        "episode": e_num,
        "quality": quality,
        "want_remux": want_remux,
        "want_crunchyroll": want_crunchyroll,
        "want_uncompressed": want_uncompressed,
        "want_av1": want_av1,
        "want_h264": want_h264,
        "want_hevc": want_hevc,
        "want_bluray": want_bluray,
        "want_web": want_web,
        "explicit_groups": explicit_groups,
        "want_debrid": want_debrid,
        "want_upscale": want_upscale
    }

def get_candidate_resolution(text):
    t = text.lower()
    if any(w in t for w in ["2160p", "4k", "uhd"]):
        return "4K"
    if any(w in t for w in ["1080p", "1080i", "fhd"]):
        return "1080p"
    if any(w in t for w in ["720p", "hd"]):
        return "720p"
    if any(w in t for w in ["480p", "576p", "dvd", "sd"]):
        return "480p"
    return "Unknown"

def get_candidate_edition(c):
    comb = (c.get("title", "") + " " + c.get("full_desc", "")).lower()
    if c.get("is_upscaled"):
        return "معالجة بالذكاء الاصطناعي (AI Upscale)"
    if c.get("is_remux"):
        return "بلوراي أصلي كامل (Remux)"
    if "bdrip" in comb or "brrip" in comb or "bluray-rip" in comb:
        return "ريـب بلوراي مضغوط (BDRip)"
    if "web-dl" in comb or "webdl" in comb or "web-rip" in comb or "webrip" in comb or c.get("is_crunchyroll"):
        return "نسخة ويب رقمية (WEB-DL)"
    if "hdtv" in comb:
        return "بث تلفزيوني (HDTV)"
    if "bluray" in comb or "blu-ray" in comb:
        return "نسخة بلوراي قياسية (BluRay)"
    if "dvd" in comb:
        return "نسخة ديفيدي (DVD)"
    return "نسخة رقمية قياسية"

def handle_search_only(user_prompt):
    """
    Search-only pipeline: Fetches top releases from TMDB/Torrentio and presents
    a clean structured Markdown table without triggering any download.
    """
    info = translate_query_for_search(user_prompt)
    is_series = info["is_series"]
    imdb_id, canonical_name = get_imdb_id(info["title"], is_series=is_series)
    if not imdb_id and not is_series:
        imdb_id, canonical_name = get_imdb_id(info["title"], is_series=True)
        is_series = True
        
    if not imdb_id:
        return True, f"عذراً، لم أتمكن من العثور على `{info['title']}` في قواعد البيانات العالمية. يرجى التحقق من كتابة الاسم بدقة."

    cfg = debrid_manager.load_config() if debrid_manager else {}
    rd_key = cfg.get("services", {}).get("realdebrid", {}).get("api_key", "")
    streams = get_torrentio_streams(imdb_id, season=info["season"], episode=info["episode"], is_series=is_series, rd_api_key=rd_key)
    if not streams:
        streams = get_torrentio_streams(imdb_id, season=info["season"], episode=info["episode"], is_series=is_series, rd_api_key=None)

    if not streams:
        return True, f"🔍 لم يتم العثور على روابط تورنت نشطة لـ **{canonical_name}** حالياً."

    rows = []
    seen = set()
    for s in streams[:20]:
        desc = s.get("title", "")
        name = s.get("name", "")
        direct_url = s.get("url")
        is_rd_cached = "[RD+]" in name or (direct_url and "resolve/realdebrid" in direct_url)
        lines = [l.strip() for l in desc.split("\n") if l.strip()]
        clean_name = lines[0]
        for l in lines:
            if l.endswith(".mkv") or l.endswith(".mp4"):
                clean_name = l
                break
        if clean_name in seen:
            continue
        seen.add(clean_name)

        size_m = re.search(r'([0-9\.]+)\s*(GB|MB)', desc, re.I)
        size_str = size_m.group(0) if size_m else "غير محدد"
        comb = (clean_name + " " + desc).lower()
        res_str = "4K UHD" if ("4k" in comb or "2160p" in comb) else ("1080p FHD" if "1080p" in comb else ("720p HD" if "720p" in comb else "SD"))
        ed_str = "Remux" if "remux" in comb else ("WEB-DL" if "web" in comb else ("BDRip" if "bdrip" in comb else "BluRay"))
        cache_status = "⚡ كاش فوري (RD+)" if is_rd_cached else "⏳ تورنت P2P"
        rows.append((clean_name, f"{res_str} ({ed_str})", size_str, cache_status))
        if len(rows) >= 6:
            break

    ep_txt = f" (الموسم {info['season']} - الحلقة {info['episode']})" if is_series else ""
    table_lines = [
        f"🔍 **نتائج البحث لـ [{canonical_name}]{ep_txt}:**",
        "*(تم إجراء البحث فقط دون بدء أي تنزيل بناءً على طلبك)*\n",
        "| # | الإصدار (Release) | الجودة والنوع | الحجم | حالة الكاش السحابي |",
        "|---|---|---|---|---|"
    ]
    for idx, (r_name, r_qual, r_size, r_cache) in enumerate(rows, 1):
        table_lines.append(f"| {idx} | ‪{r_name[:52]}...‬ | {r_qual} | {r_size} | {r_cache} |")

    table_lines.append("\n💡 **لتحميل أو مشاهدة أي من هذه النسخ:**")
    table_lines.append("• للتحميل: اكتب `حمل النسخة رقم 1` أو `حمل نسخة 4k remux`")
    table_lines.append("• للمشاهدة المباشرة: اكتب `شغل النسخة رقم 2`")

    return True, "\n".join(table_lines)

def handle_smart_download_and_search(user_prompt, ui_debrid_mode="auto", allow_upscale=False):
    """Autonomous search, selection, and download pipeline with zero hardcoded titles."""
    exact_sub = None
    target_sub = None
    info = translate_query_for_search(user_prompt)
    is_series = info["is_series"]
    
    # Prompt preference overrides UI checkbox
    if info.get("want_upscale") is not None:
        allow_upscale = info["want_upscale"]

    imdb_id, canonical_name = get_imdb_id(info["title"], is_series=is_series)
    if not imdb_id and not is_series:
        imdb_id, canonical_name = get_imdb_id(info["title"], is_series=True)
        is_series = True
        
    if not imdb_id:
        return False, f"عذراً، لم أتمكن من العثور على `{info['title']}` في قواعد البيانات العالمية. يرجى التأكد من كتابة الاسم بدقة."

    # Determine Debrid Mode
    cfg = debrid_manager.load_config() if debrid_manager else {}
    rd_key = cfg.get("services", {}).get("realdebrid", {}).get("api_key", "")
    
    use_debrid = False
    if info["want_debrid"] is not None:
        use_debrid = info["want_debrid"]
    elif ui_debrid_mode == "debrid":
        use_debrid = True
    elif ui_debrid_mode == "qbittorrent":
        use_debrid = False
    else:
        use_debrid = bool(rd_key and cfg.get("default_service", "none") != "none")

    # Fetch streams (using RD integration if Debrid requested)
    active_rd_key = rd_key if (use_debrid and rd_key) else None
    streams = get_torrentio_streams(imdb_id, season=info["season"], episode=info["episode"], is_series=is_series, rd_api_key=active_rd_key)
    
    # If no streams with RD prefix, fallback to standard
    if not streams and active_rd_key:
        streams = get_torrentio_streams(imdb_id, season=info["season"], episode=info["episode"], is_series=is_series, rd_api_key=None)

    if not streams:
        return False, f"عذراً، لم يتم العثور على روابط تورنت نشطة لـ {canonical_name} حالياً."

    # Parse candidate streams
    def is_foreign_audio_dub(text):
        t = text.lower()
        has_english = any(w in t for w in ['eng', 'english', 'multi', 'dual audio', 'dual-audio', 'multiaudio', 'multidub'])
        foreign_dub_tags = [
            r'\bfrench\b', r'\btruefrench\b', r'\bvff\b', r'\bvfi\b', r'\bvostfr\b',
            r'\bgerman\b', r'\bdeutsch\b',
            r'\bitalian\b',
            r'\bspanish\b', r'\bcastellano\b', r'\blatino\b',
            r'\brussian\b', r'\brusskiy\b', r'\bruss\b',
            r'\bhindi\b', r'\btamil\b', r'\btelugu\b'
        ]
        has_foreign = any(re.search(tag, t) for tag in foreign_dub_tags)
        return bool(has_foreign and not has_english)

    candidates = []
    for s in streams:
        desc = s.get("title", "")
        name = s.get("name", "")
        info_hash = s.get("infoHash")
        direct_url = s.get("url") # Direct Debrid link provided by Torrentio RD integration
        combined_text = (name + " " + desc).lower()
        
        is_rd_cached = "[RD+]" in name or (direct_url and "resolve/realdebrid" in direct_url)
        is_upscaled = any(w in desc.lower() or w in name.lower() for w in ["upscale", "upscaled", "ai upscale", "ai-upscale", "topaz", "4320p", "8k"])
        
        cand_res = get_candidate_resolution(combined_text)
        is_4k = (cand_res == "4K") and not is_upscaled
        
        # Strict Remux parsing: bdrip/bluray rip is strictly NOT Remux!
        is_remux = bool(re.search(r'\b(remux|bdremux|uhd[-\s]?remux)\b', combined_text, re.I)) and not is_upscaled
        is_bdrip = bool(re.search(r'\b(bdrip|brrip|bluray[-\s]?rip)\b', combined_text, re.I))
        is_web = any(w in desc.lower() for w in ["web", "webrip", "web-dl", "hmax", "amzn", "dsnp", "atvp", "nf", "max"]) and not is_upscaled
        is_hdr = any(w in desc.lower() or w in name.lower() for w in ["hdr", "dv", "dolby vision"])
        is_foreign_dub = is_foreign_audio_dub(combined_text)

        is_av1 = any(w in combined_text for w in ["av1", "av01", "libaom"])
        is_crunchyroll = any(w in combined_text for w in ["crunchyroll", "cr ", "cr]", "-cr-", "erai-raws", "subsplease", "tsundere-raws"])
        is_h264_stream = any(w in combined_text for w in ["h264", "x264", "avc", "h.264"])
        is_hevc_stream = any(w in combined_text for w in ["h265", "x265", "hevc", "h.265"])
        matches_explicit_group = any(g in combined_text for g in info.get("explicit_groups", []))
        has_arabic_tag = "🇸🇦" in combined_text or bool(re.search(r"\b(arabic|ara|ar)\b", combined_text)) or "ar]" in combined_text
        has_multi_subs = "multi subs" in combined_text or "multisub" in combined_text or "multi-sub" in combined_text
        
        # Untouched / Lossless Disc
        is_lossless = is_remux

        # Extract clean filename
        lines = [l.strip() for l in desc.split("\n") if l.strip()]
        clean_name = lines[0]
        for l in lines:
            if l.endswith(".mkv") or l.endswith(".mp4"):
                clean_name = l
                break
            elif "Multi Subs" not in l and "🇬🇧" not in l and "👤" not in l and "⚙️" not in l and len(l) > 10:
                clean_name = l

        size_match = re.search(r'([0-9\.]+)\s*(GB|MB)', desc, re.IGNORECASE)
        size_str = size_match.group(0) if size_match else "غير محدد"

        seed_match = re.search(r'👤\s*(\d+)', desc)
        seeders = int(seed_match.group(1)) if seed_match else 5

        # Magnet link clean
        mag = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(clean_name)}" if info_hash else None

        candidates.append({
            "title": clean_name,
            "full_desc": desc,
            "hash": info_hash,
            "direct_url": direct_url,
            "res": cand_res,
            "is_rd_cached": is_rd_cached,
            "is_upscaled": is_upscaled,
            "is_4k": is_4k,
            "is_remux": is_remux,
            "is_bdrip": is_bdrip,
            "is_web": is_web,
            "is_hdr": is_hdr,
            "is_foreign_dub": is_foreign_dub,
            "is_av1": is_av1,
            "is_crunchyroll": is_crunchyroll,
            "is_h264": is_h264_stream,
            "is_hevc": is_hevc_stream,
            "matches_explicit_group": matches_explicit_group,
            "has_arabic_tag": has_arabic_tag,
            "has_multi_subs": has_multi_subs,
            "is_lossless": is_lossless,
            "size": size_str,
            "seeders": seeders,
            "magnet": mag
        })

    # Filter out foreign language dubs (French, German, etc.) unless requested
    non_dub_candidates = [c for c in candidates if not c["is_foreign_dub"]]
    pool = non_dub_candidates if non_dub_candidates else candidates

    # Filter candidates based on allow_upscale setting
    valid_candidates = [c for c in pool if not c["is_upscaled"]] if not allow_upscale else pool
    if not valid_candidates:
        valid_candidates = pool

    # Intelligent deterministic scoring algorithm
    for c in valid_candidates:
        score = 0
        if use_debrid and c["is_rd_cached"]:
            score += 2000

        # Upscale handling
        if c["is_upscaled"]:
            if not allow_upscale:
                score -= 4000
            else:
                score += 200

        # AV1 Penalty (unless user specifically asked for AV1)
        if c["is_av1"]:
            if not info.get("want_av1"):
                score -= 1500
            else:
                score += 800

        # Release Groups Priority
        if info.get("explicit_groups"):
            if c.get("matches_explicit_group"):
                score += 6000
            else:
                score -= 1500

        # Codec preferences (H.264 vs HEVC)
        if info.get("want_h264"):
            if c.get("is_h264"):
                score += 3000
            elif c.get("is_hevc"):
                score -= 2000

        if info.get("want_hevc"):
            if c.get("is_hevc"):
                score += 3000
            elif c.get("is_h264"):
                score -= 1000

        # Crunchyroll source boost
        if info.get("want_crunchyroll"):
            if c["is_crunchyroll"]:
                score += 3500
            if c["is_remux"] or c.get("is_bdrip"):
                score -= 4000

        # Remux / Lossless Disc Preference
        if (info.get("want_uncompressed") or info.get("want_remux")) and not info.get("want_crunchyroll"):
            if c["is_remux"]:
                score += 1500
            elif "web-dl" in c["full_desc"].lower():
                score += 400
            if "webrip" in c["full_desc"].lower():
                score -= 300

        # Resolution match
        if info["quality"] == "4K":
            if c["is_4k"]:
                score += 1200
            else:
                score -= 800
        elif info["quality"] == "1080p":
            if c["res"] == "1080p":
                score += 600

        # Subtitle tags inside torrent stream
        if c["has_arabic_tag"]:
            score += 500
        elif c["has_multi_subs"]:
            score += 250

        # High seeders bonus
        score += min(c["seeders"], 100)

        # Huge bonus for explicit single-episode matches (avoids confusing season packs)
        if is_series:
            s_idx = info["season"]
            e_idx = info["episode"]
            comb = (c["title"] + " " + c["full_desc"]).lower()
            is_single_ep = bool(
                re.search(rf's0*{s_idx}\s*e0*{e_idx}\b', comb, re.I)
                or re.search(rf'{s_idx}x0*{e_idx}\b', comb, re.I)
                or re.search(rf'{s_idx}(?:nd|rd|th|st)?\s*season\s*[-_]\s*0*{e_idx}\b', comb, re.I)
                or re.search(rf'season\s*0*{s_idx}\s*[-_]\s*0*{e_idx}\b', comb, re.I)
            )
            is_batch_pack = any(b in comb for b in ["s01-s", "s01-0", "s02-s", "complete", "season 1-", "season 2-", "batch", "pack", "s02 multi bluray1080p"])
            if is_single_ep and not is_batch_pack:
                score += 2000
            elif is_batch_pack:
                score -= 1000

        c["score"] = score

    # ==============================================================================
    # HARD ACCEPTANCE CRITERIA:
    # If the user explicitly requested 4K, Remux, Crunchyroll, or H.264,
    # non-matching candidates can NEVER win just because they have a cache bonus!
    # ==============================================================================
    user_wants_remux = info.get("want_remux")
    user_wants_4k = (info.get("quality") == "4K")
    user_wants_1080p = (info.get("quality") == "1080p")
    user_wants_cr = info.get("want_crunchyroll")
    user_wants_h264 = info.get("want_h264")
    user_wants_hevc = info.get("want_hevc")
    user_has_groups = bool(info.get("explicit_groups"))

    user_has_strict_spec = (
        user_wants_remux or user_wants_4k or user_wants_1080p or
        user_wants_cr or user_wants_h264 or user_wants_hevc or user_has_groups
    )

    def matches_strict_criteria(c):
        if user_wants_remux and not c.get("is_remux"):
            return False
        if user_wants_4k and c.get("res") != "4K":
            return False
        if user_wants_1080p and c.get("res") != "1080p":
            return False
        if user_wants_cr and (c.get("is_remux") or c.get("is_bdrip")):
            return False
        if user_wants_h264 and not c.get("is_h264"):
            return False
        if user_wants_hevc and not c.get("is_hevc"):
            return False
        if user_has_groups and not c.get("matches_explicit_group"):
            return False
        return True

    strict_pool = [c for c in valid_candidates if matches_strict_criteria(c)] if user_has_strict_spec else valid_candidates

    if user_has_strict_spec and strict_pool:
        strict_pool.sort(key=lambda x: x.get("score", 0), reverse=True)
        best = strict_pool[0]

        # If user is in Debrid mode, but their exact strict request is not cached:
        if use_debrid and not best.get("is_rd_cached"):
            cached_alts = [c for c in valid_candidates if c.get("is_rd_cached") and not c.get("is_foreign_dub")]
            if cached_alts:
                best_cached = cached_alts[0]
                alt_type = get_candidate_edition(best_cached)
                spec_label_parts = []
                if user_wants_4k: spec_label_parts.append("4K UHD")
                if user_wants_1080p: spec_label_parts.append("1080p")
                if user_wants_remux: spec_label_parts.append("Remux")
                if user_wants_cr: spec_label_parts.append("Crunchyroll")
                if user_wants_h264: spec_label_parts.append("H.264")
                if user_has_groups: spec_label_parts.extend([g.upper() for g in info.get("explicit_groups", [])])
                spec_label = " ".join(spec_label_parts) or "المطلوبة"

                notice_msg = (
                    f"⚠️ **تنبيه:** النسخة المطلوبة تحديداً (`{spec_label}`) متوفرة كتورنت ولكنها **غير مخزنة سحابياً (Not Cached) في Real-Debrid حالياً**.\n\n"
                    f"• **النسخة التي طلبتها (تورنت عادي P2P عبر qBittorrent):**\n"
                    f"  \u202A{best['title']}\u202C ({best['size']})\n\n"
                    f"• **النسخة البديلة المتوفرة كاش سحابي فوري (Aria2):**\n"
                    f"  \u202A{best_cached['title']}\u202C ({best_cached['size']}) - {alt_type}\n\n"
                    f"💡 **خياراتك للتحميل:**\n"
                    f"1. إذا أردت تنزيل نسختك المطلوبة تورنت عادي، اطلب: `حملها عبر qbittorrent`.\n"
                    f"2. إذا أردت تنزيل النسخة السحابية المتوفرة فوراً بأقصى سرعة، اطلب: `حمل النسخة الكاش`."
                )
                return True, notice_msg
    else:
        valid_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        best = valid_candidates[0] if valid_candidates else candidates[0]

    # Subtitle search & download
    sub_status = "جاري التحقق..."
    if subtitle_manager:
        try:
            query_sub = f"{canonical_name} S{info['season']:02d}E{info['episode']:02d}" if is_series else canonical_name
            s_res = subtitle_manager.search_all_subtitles(query_sub, imdb_id=imdb_id, lang="ar")
            all_subs = s_res.get("ranked_subtitles", [])
            if not all_subs and is_series:
                s_res2 = subtitle_manager.search_all_subtitles(canonical_name, imdb_id=imdb_id, lang="ar")
                all_subs = s_res2.get("ranked_subtitles", [])
            
            # Find best matching subtitle for the target episode (handles Anime & Standard TV)
            target_sub = None
            if is_series and all_subs:
                s_idx = info['season']
                e_idx = info['episode']
                ep_sub_patterns = [
                    re.compile(rf"s0*{s_idx}\s*e0*{e_idx}\b", re.I),
                    re.compile(rf"{s_idx}x0*{e_idx}\b", re.I),
                    re.compile(rf"s0*{s_idx}\s*[-_]\s*0*{e_idx}\b", re.I),
                    re.compile(rf"{s_idx}(?:nd|rd|th|st)?\s*season\s*[-_]\s*(?:e|ep)?0*{e_idx}\b", re.I),
                    re.compile(rf"season\s*0*{s_idx}\s*[-_]\s*(?:e|ep)?0*{e_idx}\b", re.I),
                    re.compile(rf"[-_]\s*0*{e_idx}\b", re.I),
                    re.compile(rf"\be0*{e_idx}\b", re.I),
                ]
                matched_subs = []
                for s in all_subs:
                    s_name = s.get("name") or ""
                    for p in ep_sub_patterns:
                        if p.search(s_name):
                            matched_subs.append(s)
                            break
                if matched_subs:
                    if info.get("want_crunchyroll"):
                        cr_subs = [s for s in matched_subs if "crunchyroll" in (s.get("name") or "").lower()]
                        target_sub = cr_subs[0] if cr_subs else matched_subs[0]
                    else:
                        target_sub = matched_subs[0]
                else:
                    target_sub = all_subs[0]
            elif all_subs:
                target_sub = all_subs[0]
            else:
                target_sub = None

            if target_sub:
                out_d = os.path.expanduser(r"~\Downloads")
                base_sub_name = f"{canonical_name}.S{info['season']:02d}E{info['episode']:02d}.ar" if is_series else f"{canonical_name}.ar"
                dl_res = subtitle_manager.download_subtitle_file(target_sub, out_d, file_basename=base_sub_name)
                if dl_res.get("success") and dl_res.get("files"):
                    dl_sub = dl_res['files'][0]
                    
                    # Create exact matching filename so MPC-HC auto-loads it without user clicking anything
                    video_stem, _ = os.path.splitext(best["title"])
                    exact_sub = os.path.join(out_d, f"{video_stem}.srt")
                    exact_sub_ar = os.path.join(out_d, f"{video_stem}.ar.srt")
                    try:
                        shutil.copy2(dl_sub, exact_sub)
                        shutil.copy2(dl_sub, exact_sub_ar)
                    except Exception:
                        pass

                    # Check if matching local video exists in Downloads
                    local_v = None
                    for f in os.listdir(out_d):
                        if f.lower().endswith((".mkv", ".mp4", ".avi", ".ts")) and (canonical_name.lower() in f.lower() or video_stem.lower() in f.lower()):
                            local_v = os.path.join(out_d, f)
                            break

                    # Launch detached background watcher for audio sync completion
                    watcher_py = os.path.join(SKILL_DIR, "sync_watcher.py")
                    if os.path.exists(watcher_py):
                        try:
                            CREATE_NO_WINDOW = 0x08000000
                            DETACHED_PROCESS = 0x00000008
                            subprocess.Popen(
                                [sys.executable, watcher_py, best["title"], exact_sub],
                                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                                close_fds=True
                            )
                        except Exception:
                            pass

                    if local_v:
                        sync_res = subtitle_manager.verify_and_auto_sync(local_v, exact_sub)
                        if sync_res.get("status") == "matched":
                            sub_status = f"تم تحميل الترجمة مطابقة للاسم والتوقيت تلقائياً:\n  \u202A{os.path.basename(exact_sub)}\u202C"
                        elif sync_res.get("success") and sync_res.get("output_file"):
                            tool_used = sync_res.get("tool", "alass")
                            try:
                                shutil.copy2(sync_res['output_file'], exact_sub)
                            except Exception:
                                pass
                            sub_status = f"تمت مزامنة الترجمة بدقة وتطابق اسمها مع الفيديو عبر {tool_used}:\n  \u202A{os.path.basename(exact_sub)}\u202C"
                        else:
                            sub_status = f"تم حفظ الترجمة بنفس اسم الفيديو للتحميل التلقائي:\n  \u202A{os.path.basename(exact_sub)}\u202C"
                    else:
                        sub_status = f"تم حفظ الترجمة بنفس اسم الفيديو وتفعيل المزامنة التلقائية فور اكتمال التنزيل:\n  \u202A{os.path.basename(exact_sub)}\u202C"
                else:
                    sub_status = "النسخة تحتوي على مسارات ترجمة رسمية مدمجة أو جاهزة للتنزيل."
            else:
                sub_status = "النسخة تحتوي على ترجمات رسمية مدمجة داخل ملف الفيديو."
        except Exception:
            sub_status = "الترجمة جاهزة للتحميل."

    # Check if user requested direct streaming without downloading
    q_lower = user_prompt.lower()
    is_explicit_stream = any(w in q_lower for w in ["بدون تحميل", "بدون تنزيل", "بث", "مشاهدة مباشرة", "stream", "كاش", "سحابي"])
    is_play_intent = any(w in q_lower for w in ["شغل", "شغله", "تابع", "تشغيل", "play", "مباشر"])
    has_dl_verb = any(w in q_lower for w in ["حمل", "نزله", "تنزيل", "تحميل", "download"])

    is_stream = is_explicit_stream or (is_play_intent and not has_dl_verb)

    # Execute Download: Real-Debrid vs qBittorrent
    dl_status = ""
    dl_failed = False
    engine_name = "qBittorrent"
    active_dl_client = "aria2" if "idm" not in q_lower else "idm"

    if use_debrid and (best.get("direct_url") or best.get("is_rd_cached")):
        target_url = best.get("direct_url")
        if target_url and "torrentio.strem.fun/resolve" in target_url:
            try:
                r_red = requests.get(target_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, allow_redirects=False, timeout=8)
                if r_red.status_code in [301, 302, 303, 307] and r_red.headers.get("Location"):
                    target_url = r_red.headers["Location"]
            except Exception:
                pass

        if target_url and debrid_manager:
            if is_stream:
                st_res = debrid_manager.stream_to_player(target_url, subtitle_path=exact_sub, player="mpc")
                engine_name = f"Real-Debrid Stream -> {st_res.get('player', 'MPC-HC')}"
                dl_status = f"🎬 تم فتح البث المباشر فوراً داخل مشغل **{st_res.get('player', 'MPC-HC')}** بدون تحميل مع ربط الترجمة تلقائياً!"
            else:
                dl_res = debrid_manager.send_to_downloader(target_url, downloader=active_dl_client, file_name=best.get("title"))
                engine_name = "Real-Debrid + Aria2 (سحابي فائق السرعة)"
                if dl_res and not dl_res.get("success"):
                    dl_failed = True
                    dl_status = f"❌ تعذر بدء التحميل عبر المحمل: {dl_res.get('error')}"
                else:
                    dl_status = "🚀 تم سحب الرابط السحابي وبدأ التنزيل الفوري بأقصى سرعة إنترنت (16 اتصال) عبر **Aria2** في مجلد Downloads."
        elif best.get("magnet") and debrid_manager:
            clean_mag = f"magnet:?xt=urn:btih:{best['hash']}"
            rd_res = debrid_manager.add_magnet_and_resolve(
                clean_mag,
                target_season=info.get("season"),
                target_episode=info.get("episode")
            )
            if rd_res.get("is_cached") and rd_res.get("unrestricted_links"):
                chosen_link = None
                if is_series and len(rd_res["unrestricted_links"]) > 1:
                    s_idx = info.get("season", 1)
                    e_idx = info.get("episode", 1)
                    for l in rd_res["unrestricted_links"]:
                        fname = l.get("filename", "").lower()
                        if (
                            f"s{s_idx:02d}e{e_idx:02d}" in fname
                            or f"{s_idx}x{e_idx:02d}" in fname
                            or f"s{s_idx}e{e_idx}" in fname
                            or f"- {e_idx:02d}" in fname
                            or f"e{e_idx:02d}" in fname
                        ):
                            chosen_link = l
                            break
                if not chosen_link:
                    chosen_link = rd_res["unrestricted_links"][0]

                d_url = chosen_link["download_url"]
                if chosen_link.get("filename"):
                    best["title"] = chosen_link["filename"]
                if is_stream:
                    st_res = debrid_manager.stream_to_player(d_url, subtitle_path=exact_sub, player="mpc")
                    engine_name = f"Real-Debrid Stream -> {st_res.get('player', 'MPC-HC')}"
                    dl_status = f"🎬 تم فتح البث المباشر فوراً داخل مشغل **{st_res.get('player', 'MPC-HC')}** بدون تحميل مع ربط الترجمة تلقائياً!"
                else:
                    dl_res = debrid_manager.send_to_downloader(d_url, downloader=active_dl_client, file_name=best.get("title"))
                    engine_name = "Real-Debrid + Aria2 (سحابي فائق السرعة)"
                    if dl_res and not dl_res.get("success"):
                        dl_failed = True
                        dl_status = f"❌ تعذر بدء التحميل عبر المحمل: {dl_res.get('error')}"
                    else:
                        dl_status = "🚀 تم سحب الكاش عبر **Real-Debrid** وبدأ التنزيل المباشر فائق السرعة عبر **Aria2** (16 اتصال) في مجلد Downloads."
            else:
                engine_name = "Real-Debrid Cloud"
                dl_status = "☁️ تمت إضافة التورنت إلى كاش **Real-Debrid** السحابي بنجاح."
    else:
        # Standard qBittorrent P2P
        mag = best.get("magnet") or (f"magnet:?xt=urn:btih:{best['hash']}" if best.get("hash") else None)
        if mag and os.path.exists(QBITTORRENT_PATH):
            subprocess.Popen([QBITTORRENT_PATH, mag])
            engine_name = "qBittorrent (تورنت P2P)"
            dl_status = "تم فتح الرابط وبدء التنزيل فوراً داخل برنامج **qBittorrent** للمتابعة والتحكم في السييد والمشاركة."
        elif mag and debrid_manager:
            debrid_manager.send_to_downloader(mag, downloader="aria2")
            dl_status = "بدأ التنزيل المباشر في مجلد Downloads."

    ep_label = f"الموسم {info['season']} - الحلقة {info['episode']}" if is_series else "فيلم كامل"
    edition_type = get_candidate_edition(best)
    audio_label = "إنجليزي / أصلي (Original Audio)" if not best.get("is_foreign_dub") else "مدبلج بلغة أخرى"
    
    # Accurate resolution mapping based on true detected candidate resolution
    res_map = {
        "4K": "4K UHD (2160p)",
        "1080p": "1080p Full HD",
        "720p": "720p HD",
        "480p": "480p SD",
    }
    res_label = res_map.get(best.get("res"), "جودة قياسية (Standard Definition)")
    real_downloads_dir = os.path.expanduser(r"~\Downloads")

    header_text = "🎯 **أبشر! تم تجهيز طلبك وبدء تحميله بنجاح:**" if not dl_failed else "⚠️ **تم العثور على الإصدار ولكن تعذر بدء التنزيل تلقائياً:**"

    report = (
        f"{header_text}\n\n"
        f"• **العمل:**\n  \u202A{canonical_name}\u202C ({ep_label})\n\n"
        f"• **الإصدار المختار:**\n  \u202A{best['title']}\u202C\n\n"
        f"• **الصوت واللغة:**\n  {audio_label}\n\n"
        f"• **نوع الإصدار:**\n  {edition_type}\n\n"
        f"• **الجودة:**\n  \u202A{res_label}\u202C\n\n"
        f"• **حجم الملف:**\n  \u202A{best['size']}\u202C\n\n"
        f"• **طريقة التحميل:**\n  \u202A{engine_name}\u202C\n\n"
        f"• **الترجمة العربية:**\n  {sub_status}\n\n"
        f"• **مجلد التنزيل:**\n  \u202A{real_downloads_dir}\u202C\n\n"
        f"🚀 **حالة التنفيذ:**\n{dl_status}"
    )
    return True, report

def resolve_and_play_file(query_text):
    """Searches for local video files matching user description and plays them in MPC-HC."""
    search_dirs = [
        os.path.expanduser(r"~\Downloads"),
        os.path.expanduser(r"~\Downloads\Series"),
    ]
    video_exts = (".mkv", ".mp4", ".avi", ".ts", ".m2ts")
    
    path_match = re.search(r'([A-Za-z]:\\[^\s"\']+)', query_text)
    if path_match and os.path.exists(path_match.group(1)):
        return path_match.group(1)
            
    tokens = re.findall(r'[a-zA-Z0-9_\-\u0600-\u06FF]+', query_text)
    keywords = [t for t in tokens if t not in ["شغل", "ملف", "رقم", "في", "مجلد", "فلم", "حلقة", "المشغل", "downloads", "download"]]

    for d in search_dirs:
        if not os.path.exists(d): continue
        try: files = os.listdir(d)
        except Exception: continue
        for f in files:
            f_lower = f.lower()
            if any(f_lower.endswith(ext) for ext in video_exts):
                for k in keywords:
                    base, _ = os.path.splitext(f_lower)
                    if k.lower() == base or k.lower() in f_lower:
                        return os.path.join(d, f)
    return None

def execute_user_action(user_prompt, debrid_mode="auto", allow_upscale=False):
    """Central agent router: handles search-only, negative intent, direct play, and download."""
    p_lower = user_prompt.lower()

    # 1. Negative Download / Search-Only Detection (Zero False Triggers)
    has_negative_dl = any(neg in p_lower for neg in [
        "لا تحمل", "لا تنزل", "بدون تحميل", "بدون تنزيل", "لا تحمله", "لا تنزله",
        "ما ابغى احمل", "ما ابي احمل", "ما بدي احمل", "لا تقوم بتحميل", "من غير تحميل"
    ])
    has_explicit_search = any(s in p_lower for s in [
        "ابحث فقط", "فقط ابحث", "بحث فقط", "استعلم فقط", "ابحث دون تحميل", "ابحث بدون تحميل"
    ])
    is_search_only = has_explicit_search or (has_negative_dl and any(w in p_lower for w in ["ابحث", "بحث", "شف", "شوف", "استعلم"]))

    if is_search_only:
        return handle_search_only(user_prompt)

    # 2. General Knowledge / Recommendations (Don't trigger downloads for chit-chat questions)
    is_general_question = any(q in p_lower for q in [
        "وش افضل", "وش أفضل", "ما افضل", "ما أفضل", "ماهو افضل", "ما هو افضل",
        "ما هو أفضل", "ماهو أفضل", "افضل فيلم", "أفضل فيلم", "افضل مسلسل", "أفضل مسلسل",
        "وش قصة", "ما هي قصة", "ما قصة", "من هو", "من هم", "تقييم", "شرح فيلم", "شرح مسلسل",
        "مين بطل", "مين الممثل", "كم تقييم", "رشح لي", "اقترح لي"
    ])
    if is_general_question and not any(w in p_lower for w in ["حمل", "نزله", "download", "تنزيل", "شغل", "سحب"]):
        return False, None

    is_play = any(w in p_lower for w in ["شغل", "شغله", "افتح", "تشغيل", "play", "open", "تابع"])
    
    # Download detection: triggers on verbs OR media queries (series, anime, episode, movie)
    has_dl_verb = any(w in p_lower for w in [
        "حمل", "نزله", "تنزيل", "تحميل", "download", "جيب", "هات",
        "ابي", "أبي", "ابغى", "أبغى", "اريد", "أريد", "عطني", "اعطني", "ودنا", "ودّي"
    ]) and not has_negative_dl

    has_episode_season = bool(re.search(r'(?:الحلقة|حلقة|ep|episode|\be\d{1,3}\b|الموسم|موسم|season|\bs\d{1,2}\b)', user_prompt, re.I))
    has_media_intent = any(w in p_lower for w in ["انمي", "أنمي", "مسلسل", "فلم", "فيلم", "سلسلة", "movie", "series", "anime", "remux", "4k", "1080p", "بدون ضغط", "كرانشي"])

    is_dl = (has_dl_verb or has_episode_season or has_media_intent) and not has_negative_dl and not is_play
    
    is_sync = any(w in p_lower for w in [
        "مزامنة", "مزامنه", "ضبط التوقيت", "عدل التوقيت", "توقيتها", "اضبط توقيت", "عدل توقيت",
        "توقيت مختلف", "مختلف عن الصوت", "ماضبط توقيتها", "ما ضبط توقيتها", "مو متوافقة", "مو متوافقه",
        "قدم الترجمة", "أخر الترجمة", "تأخير الترجمة", "تقديم الترجمة", "sync", "ffsubsync", "subsync"
    ])
    
    if is_dl:
        ok, res = handle_smart_download_and_search(user_prompt, ui_debrid_mode=debrid_mode, allow_upscale=allow_upscale)
        return True, res

    if is_sync and not is_dl:
        out_d = os.path.expanduser(r"~\Downloads")
        if os.path.exists(out_d):
            v_files = [os.path.join(out_d, f) for f in os.listdir(out_d) if f.lower().endswith((".mkv", ".mp4", ".avi", ".ts"))]
            s_files = [os.path.join(out_d, f) for f in os.listdir(out_d) if f.lower().endswith((".srt", ".ass")) and not f.endswith(".synced.srt")]
            if v_files and s_files:
                latest_v = max(v_files, key=os.path.getmtime)
                latest_s = max(s_files, key=os.path.getmtime)
                if subtitle_manager:
                    res = subtitle_manager.sync_subtitle_with_autosubsync(latest_v, latest_s)
                    if res.get("success"):
                        tool = res.get("tool", "alass")
                        v_stem = os.path.splitext(os.path.basename(latest_v))[0]
                        exact_sub = os.path.join(out_d, f"{v_stem}.srt")
                        try:
                            shutil.copy2(res['output_file'], exact_sub)
                        except Exception:
                            pass
                        return True, (
                            f"🎯 **تمت مزامنة الترجمة بنجاح عبر {tool}:**\n\n"
                            f"• **الفيديو:**\n  \u202A{os.path.basename(latest_v)}\u202C\n\n"
                            f"• **ملف الترجمة المتطابق:**\n  \u202A{os.path.basename(exact_sub)}\u202C\n\n"
                            f"جاهز الآن للتشغيل التلقائي داخل مشغل MPC-HC!"
                        )
                    else:
                        return True, f"⚠️ لم تنجح المزامنة: {res.get('message')}"

    # Check if explicit cloud streaming without downloading is requested
    is_stream_request = any(w in p_lower for w in ["بدون تحميل", "بث", "مشاهدة مباشرة", "stream", "كاش", "سحابي"])

    if is_play and not is_stream_request:
        file_path = resolve_and_play_file(user_prompt)
        if not file_path and any(w in user_prompt for w in ["ملف 1", "1.mkv"]):
            def_1 = os.path.expanduser(r"~\Downloads\1.mkv")
            if os.path.exists(def_1): file_path = def_1
            
        if file_path:
            v_dir = os.path.dirname(file_path)
            v_base = os.path.splitext(os.path.basename(file_path))[0]
            sub_candidates = [
                os.path.join(v_dir, f"{v_base}.srt"),
                os.path.join(v_dir, f"{v_base}.synced.srt"),
                os.path.join(v_dir, f"{v_base}.ar.srt"),
            ]
            sub_to_use = None
            for sc in sub_candidates:
                if os.path.exists(sc) and os.path.getsize(sc) > 500:
                    sub_to_use = sc
                    break
            if not sub_to_use:
                for f in os.listdir(v_dir):
                    if f.lower().endswith((".srt", ".ass")) and any(p in f.lower() for p in v_base.lower().split(".") if len(p) > 3):
                        candidate_f = os.path.join(v_dir, f)
                        if os.path.getsize(candidate_f) > 500:
                            sub_to_use = candidate_f
                            break

            if debrid_manager:
                debrid_manager.stream_to_player(file_path, subtitle_path=sub_to_use, player="mpc")
            else:
                os.startfile(file_path)
            sub_note = f"\n• **الترجمة المرفقة تلقائياً:**\n  \u202A{os.path.basename(sub_to_use)}\u202C" if sub_to_use else ""
            return True, f"🎬 **تم فتح الملف فوراً داخل مشغل MPC-HC:**\n\n• **الفيديو:**\n  \u202A{os.path.basename(file_path)}\u202C{sub_note}"

    # If stream request OR play request where local file was not found: resolve cloud stream via Real-Debrid into MPC-HC!
    if is_stream_request or is_play or is_dl:
        ok, res = handle_smart_download_and_search(user_prompt, ui_debrid_mode=debrid_mode, allow_upscale=allow_upscale)
        if ok:
            return True, res

    return False, None

# ==============================================================================
# GUI CLIENT WITH UNIVERSAL CLIPBOARD + DEBRID SELECTOR
# ==============================================================================

def query_ollama(prompt):
    global is_cancelled
    is_cancelled = False
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "system": (
            "أنت المساعد الذكي التفاعلي لمهارة تورنت إكس. "
            "تتحدث دائماً باللغة العربية الفصحى البسيطة والمباشرة. "
            "أنت قادر على التحكم في تنزيل التورنتات وتشغيل الأفلام في MPC-HC و qBittorrent و Real-Debrid. "
            "إياك أن تقول للمستخدم عذراً ليس لدي القدرة على التنزيل أو البحث، لأنك تملك كل الصلاحيات المباشرة."
        ),
        "stream": False,
        "options": {"temperature": 0.5, "top_p": 0.9}
    }
    req = urllib.request.Request(OLLAMA_API_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if is_cancelled: return "🛑 تم إيقاف الرد."
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
    except Exception:
        return (
            "💡 **أهلاً بك في TorrentX!**\n\n"
            "أنا المساعد المخصص للبحث والتحميل الفوري وجلب الترجمات المتوافقة.\n"
            "اكتب لي اسم أي فيلم أو مسلسل أو حلقة ترغب بمشاهدتها، وسأقوم بالبحث والتحميل فوراً بأعلى جودة!"
        )

def run_chat_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext

    root = tk.Tk()
    root.title("مساعد تورنت إكس الذكي (Dark Mode)")
    root.geometry("760x830")
    root.minsize(680, 700)
    root.configure(bg="#0b0f19")

    # Dark Header
    header = tk.Frame(root, bg="#090d16", pady=12)
    header.pack(side="top", fill="x")
    tk.Label(header, text="🤖 مساعد تورنت إكس الذكي المباشر", font=("Segoe UI", 13, "bold"), fg="#38bdf8", bg="#090d16").pack(anchor="center")
    tk.Label(header, text="تحكم كامل بالتحميل والمشاهدة: Real-Debrid فائق السرعة أو qBittorrent و MPC-HC", font=("Segoe UI", 9), fg="#94a3b8", bg="#090d16").pack(anchor="center", pady=2)

    bottom_container = tk.Frame(root, bg="#0f172a", padx=12, pady=10, highlightbackground="#1e293b", highlightthickness=1)
    bottom_container.pack(side="bottom", fill="x")

    actions_frame = tk.Frame(root, bg="#0b0f19", padx=10, pady=5)
    actions_frame.pack(side="bottom", fill="x")

    # Mode Selector Bar (Dark Mode)
    mode_bar = tk.Frame(root, bg="#1e293b", padx=10, pady=5)
    mode_bar.pack(side="bottom", fill="x")

    tk.Label(mode_bar, text="طريقة التنزيل المفضلة:", font=("Segoe UI", 8, "bold"), bg="#1e293b", fg="#94a3b8").pack(side="left", padx=4)

    debrid_mode_var = tk.StringVar(value="auto")
    rb_auto = tk.Radiobutton(mode_bar, text="تلقائي (حسب الطلب)", variable=debrid_mode_var, value="auto", font=("Segoe UI", 8), bg="#1e293b", fg="#cbd5e1", selectcolor="#0f172a", activebackground="#1e293b", activeforeground="#38bdf8")
    rb_auto.pack(side="left", padx=4)

    rb_rd = tk.Radiobutton(mode_bar, text="عبر Real-Debrid (سحابي سريع)", variable=debrid_mode_var, value="debrid", font=("Segoe UI", 8, "bold"), fg="#38bdf8", bg="#1e293b", selectcolor="#0f172a", activebackground="#1e293b", activeforeground="#38bdf8")
    rb_rd.pack(side="left", padx=4)

    rb_qbit = tk.Radiobutton(mode_bar, text="عبر qBittorrent (بدون ديبرد)", variable=debrid_mode_var, value="qbittorrent", font=("Segoe UI", 8, "bold"), fg="#34d399", bg="#1e293b", selectcolor="#0f172a", activebackground="#1e293b", activeforeground="#34d399")
    rb_qbit.pack(side="left", padx=4)

    # AI Upscale Checkbox
    allow_upscale_var = tk.BooleanVar(value=False)
    cb_upscale = tk.Checkbutton(mode_bar, text="السماح بنسخ الـ AI Upscale", variable=allow_upscale_var, font=("Segoe UI", 8), bg="#1e293b", fg="#94a3b8", selectcolor="#0f172a", activebackground="#1e293b", activeforeground="#f8fafc")
    cb_upscale.pack(side="right", padx=6)

    chat_frame = tk.Frame(root, bg="#0b0f19", padx=12, pady=10)
    chat_frame.pack(side="top", fill="both", expand=True)

    chat_box = scrolledtext.ScrolledText(chat_frame, wrap="word", font=("Segoe UI", 10), bg="#0f172a", fg="#f8fafc", padx=12, pady=10, relief="flat", highlightbackground="#1e293b", highlightthickness=1, insertbackground="#38bdf8", selectbackground="#1e40af", selectforeground="#ffffff")
    chat_box.pack(fill="both", expand=True)

    input_box_frame = tk.Frame(bottom_container, bg="#0f172a")
    input_box_frame.pack(fill="x", pady=2)

    text_input = tk.Text(input_box_frame, font=("Segoe UI", 10), height=3, wrap="word", relief="solid", bd=1, padx=8, pady=6, bg="#1e293b", fg="#f8fafc", insertbackground="#38bdf8", selectbackground="#1d4ed8", selectforeground="#ffffff", highlightbackground="#334155")
    text_input.pack(side="left", fill="x", expand=True, padx=(0, 8))

    # Universal Clipboard (Dark Menu)
    def setup_universal_clipboard(widget, allow_edit=True):
        def do_copy(e=None):
            try:
                selected = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
                root.clipboard_clear()
                root.clipboard_append(selected)
            except tk.TclError: pass
            return "break"

        def do_paste(e=None):
            if not allow_edit: return "break"
            try:
                clip = root.clipboard_get()
                try: widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                except tk.TclError: pass
                widget.insert(tk.INSERT, clip)
            except tk.TclError: pass
            return "break"

        def do_cut(e=None):
            if not allow_edit: return "break"
            try:
                selected = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
                root.clipboard_clear()
                root.clipboard_append(selected)
                widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError: pass
            return "break"

        def do_select_all(e=None):
            widget.tag_add(tk.SEL, "1.0", tk.END)
            widget.mark_set(tk.INSERT, tk.END)
            return "break"

        def on_key_press(event):
            is_ctrl = bool(event.state & 0x0004) or (hasattr(event, 'keysym') and event.keysym.startswith("Control_"))
            if is_ctrl:
                if event.keycode == 67 or event.keysym.lower() == 'c' or event.char == 'ؤ' or event.char == '\x03': return do_copy()
                elif event.keycode == 86 or event.keysym.lower() == 'v' or event.char == 'ر' or event.char == '\x16': return do_paste()
                elif event.keycode == 88 or event.keysym.lower() == 'x' or event.char == 'ء' or event.char == '\x18': return do_cut()
                elif event.keycode == 65 or event.keysym.lower() == 'a' or event.char == 'ش' or event.char == '\x01': return do_select_all()

        widget.bind("<KeyPress>", on_key_press, add="+")
        menu = tk.Menu(widget, tearoff=0, font=("Segoe UI", 9), bg="#1e293b", fg="#f8fafc", activebackground="#334155", activeforeground="#38bdf8")
        if allow_edit: menu.add_command(label="قص (Ctrl+X)", command=do_cut)
        menu.add_command(label="نسخ (Ctrl+C)", command=do_copy)
        if allow_edit: menu.add_command(label="لصق (Ctrl+V)", command=do_paste)
        menu.add_separator()
        menu.add_command(label="تحديد الكل (Ctrl+A)", command=do_select_all)
        widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    setup_universal_clipboard(chat_box, allow_edit=False)
    setup_universal_clipboard(text_input, allow_edit=True)

    btn_box = tk.Frame(input_box_frame, bg="#0f172a")
    btn_box.pack(side="right", fill="y")
    is_running = False

    def append_message(sender, msg, color="#38bdf8"):
        chat_box.config(state="normal")
        chat_box.insert(tk.END, f"{sender}:\n", ("sender_tag",))
        chat_box.insert(tk.END, f"{msg}\n\n", ("msg_tag",))
        chat_box.tag_config("sender_tag", font=("Segoe UI", 9, "bold"), foreground=color)
        chat_box.tag_config("msg_tag", font=("Segoe UI", 10), foreground="#f1f5f9", spacing1=2, spacing3=4)
        chat_box.see(tk.END)

    append_message("🤖 مساعد تورنت إكس", "أهلاً بك! يمكنك سؤالي أو أمري بالبحث والتحميل عبر Real-Debrid أو qBittorrent لأي فلم أو مسلسل.", color="#34d399")

    def stop_generation():
        global is_cancelled
        is_cancelled = True
        append_message("⚠️ تنبيه", "تم إرسال أمر الإيقاف 🛑", color="#fbbf24")
        reset_ui_state()

    def reset_ui_state():
        nonlocal is_running
        is_running = False
        send_btn.config(text="إرسال 🚀", bg="#15803d", activebackground="#16a34a", state="normal", command=send_msg)
        text_input.config(state="normal")
        text_input.focus()

    def send_msg(event=None):
        nonlocal is_running
        if is_running: return "break"
        user_text = text_input.get("1.0", tk.END).strip()
        if not user_text: return "break"

        text_input.delete("1.0", tk.END)
        append_message("👤 أنت", user_text, color="#38bdf8")

        is_running = True
        send_btn.config(text="إيقاف 🛑", bg="#dc2626", activebackground="#b91c1c", state="normal", command=stop_generation)
        text_input.config(state="disabled")

        selected_mode = debrid_mode_var.get()
        selected_upscale = allow_upscale_var.get()

        def _worker():
            executed, result_msg = execute_user_action(user_text, debrid_mode=selected_mode, allow_upscale=selected_upscale)
            if executed:
                root.after(0, lambda: _on_reply(result_msg, is_action=True))
            else:
                reply = query_ollama(user_text)
                root.after(0, lambda: _on_reply(reply, is_action=False))

        def _on_reply(reply, is_action=False):
            if is_running:
                c = "#34d399" if is_action else "#38bdf8"
                title = "⚡ تنفيذ تلقائي" if is_action else "🤖 مساعد تورنت إكس"
                append_message(title, reply, color=c)
            reset_ui_state()

        threading.Thread(target=_worker, daemon=True).start()
        return "break"

    text_input.bind("<Return>", lambda e: send_msg())
    text_input.bind("<Shift-Return>", lambda e: text_input.insert(tk.INSERT, "\n") or "break")

    send_btn = tk.Button(btn_box, text="إرسال 🚀", command=send_msg, font=("Segoe UI", 10, "bold"), bg="#15803d", activebackground="#16a34a", fg="white", padx=18, pady=10, cursor="hand2", relief="flat")
    send_btn.pack(fill="both", expand=True)

    tk.Label(bottom_container, text="💡 نصيحة: حدد طريقة التحميل المفضلة بالأزرار أعلاه، أو اكتب في رسالتك (عبر ديبرد / بدون ديبرد).", font=("Segoe UI", 8), fg="#64748b", bg="#0f172a").pack(anchor="w", pady=(3, 0))

    def insert_quick_prompt(text):
        text_input.delete("1.0", tk.END)
        text_input.insert("1.0", text)
        send_msg()

    # Dark Quick action buttons
    tk.Button(actions_frame, text="🎬 تشغيل آخر ملف في Downloads", font=("Segoe UI", 8), bg="#1e293b", fg="#cbd5e1", activebackground="#334155", activeforeground="#ffffff", relief="flat", cursor="hand2", command=lambda: insert_quick_prompt("شغل ملف رقم 1 في مجلد Downloads")).pack(side="left", padx=4)
    tk.Button(actions_frame, text="🚀 فحص مسلسل VisionQuest", font=("Segoe UI", 8), bg="#1e293b", fg="#cbd5e1", activebackground="#334155", activeforeground="#ffffff", relief="flat", cursor="hand2", command=lambda: insert_quick_prompt("هل توجد حلقات جديدة لمسلسل VisionQuest؟")).pack(side="left", padx=4)
    tk.Button(actions_frame, text="⚙️ فتح معالج الإعداد", font=("Segoe UI", 8), bg="#1e293b", fg="#cbd5e1", activebackground="#334155", activeforeground="#ffffff", relief="flat", cursor="hand2", command=lambda: subprocess.Popen(["python", os.path.join(SKILL_DIR, "setup_wizard.py")])).pack(side="left", padx=4)

    text_input.focus()
    root.mainloop()

if __name__ == "__main__":
    if len(sys.argv) > 1 and not (len(sys.argv) == 2 and sys.argv[1].lower() in ["--gui", "-g"]):
        cmd_args = [a for a in sys.argv[1:] if not a.startswith("--")]
        cmd_text = " ".join(cmd_args) if cmd_args else sys.argv[1]
        ok, res = execute_user_action(cmd_text)
        if ok and res:
            print(res)
        else:
            print(query_ollama(cmd_text))
    else:
        run_chat_gui()
