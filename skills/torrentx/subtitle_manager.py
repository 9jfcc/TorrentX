# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import time
import struct
import hashlib
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
import zipfile
import io
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

CONFIG_PATH = os.path.expanduser(r"~/.gemini/config/subtitles.json")
DEFAULT_SUBS_DIR = os.path.expanduser(r"~\Downloads\Movies\Subtitles")

# Strict ban list: Never use OpenSubtitles or machine translations
BANNED_SUB_KEYWORDS = [
    "opensubtitles", "open subtitles", "osdb.link", "osdb",
    "google translate", "machine translated", "ai translated", "ai-translated",
    "translated by ai", "chatgpt", "ترجمة آلية", "ترجمة قوقل", "ترجمة جوجل", "ترجمة الذكاء"
]

# Ad/Watermark patterns inside SRT cues to strip automatically
AD_CUE_PATTERNS = [
    r"opensubtitles",
    r"osdb\.link",
    r"advertise\s+your\s+product",
    r"become\s+vip\s+member",
    r"support\s+us\s+and\s+become\s+vip",
    r"www\.opensubtitles\.org",
    r"subtitles\s+by\s+opensubtitles",
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "services": {
            "subdl": {"api_key": "", "api_url": "https://subdl.com/panel/api"},
            "subsource": {"api_key": "", "api_url": "https://subsource.net/dashboard/api"}
        },
        "autosubsync_path": os.path.expanduser(r"~\Downloads\AutoSubSync\AutoSubSync.exe"),
        "default_language": "ar",
        "preferred_encoding": "utf-8-sig"
    }


def normalize_fancy_unicode(text):
    """
    Converts fancy Unicode mathematical bold/italic letters and enclosed squared/negative squared letters
    (e.g. 🅽🅴🆃🅵🅻🅸🆇 -> netflix, 🅾🆂🅽+ -> osn+, 𝐁𝐥𝐮-𝐫𝐚𝐲 -> blu-ray) into standard lowercase ASCII/Arabic.
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        cp = ord(ch)
        # Squared Latin Capital Letters (🅰 U+1F130 .. 🅩 U+1F149)
        if 0x1F130 <= cp <= 0x1F149:
            out.append(chr(ord('a') + (cp - 0x1F130)))
        # Negative Squared Latin Capital Letters (🅰 U+1F170 .. 🆉 U+1F189)
        elif 0x1F170 <= cp <= 0x1F189:
            out.append(chr(ord('a') + (cp - 0x1F170)))
        # Regional Indicator Symbols (🇦 U+1F1E6 .. 🇿 U+1F1FF)
        elif 0x1F1E6 <= cp <= 0x1F1FF:
            out.append(chr(ord('a') + (cp - 0x1F1E6)))
        else:
            out.append(ch)
    joined = "".join(out)
    # Normalize mathematical bold/italic/script characters via NFKD
    normalized = unicodedata.normalize("NFKD", joined)
    return normalized.lower()


def clean_subtitle_ads(sub_text):
    """Removes any cue blocks containing OpenSubtitles or promotional watermarks."""
    if not sub_text:
        return sub_text
    ad_regex = re.compile("|".join(AD_CUE_PATTERNS), re.IGNORECASE)
    blocks = re.split(r'\n\s*\n', sub_text.strip())
    cleaned_blocks = []
    cue_num = 1
    ts_re = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")

    for block in blocks:
        if ad_regex.search(block):
            continue
        lines = block.splitlines()
        if len(lines) >= 2 and lines[0].strip().isdigit() and ts_re.search(lines[1]):
            lines[0] = str(cue_num)
            cue_num += 1
            cleaned_blocks.append("\n".join(lines))
        else:
            cleaned_blocks.append(block)
    return "\n\n".join(cleaned_blocks) + "\n"


def fix_arabic_rtl_text(sub_text):
    """
    Cleans OpenSubtitles ads/watermarks and fixes Arabic RTL punctuation & dialogue dashes
    using Unicode RLM (\u200F) so punctuation never flips in media players.
    """
    sub_text = clean_subtitle_ads(sub_text)
    RLM = "\u200F"
    lines = sub_text.splitlines()
    new_lines = []

    ts_pattern = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
    arabic_pattern = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
    trailing_punct = re.compile(r"([.!?؟:;،,\-\'\"\)\]\}]+)$")
    leading_punct = re.compile(r"^([.!?؟:;،,]+)([\u0600-\u06FF].*)")

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.isdigit() or ts_pattern.match(stripped):
            new_lines.append(line)
            continue

        if not arabic_pattern.search(line):
            new_lines.append(line)
            continue

        l = line.strip(" \t\u200E\u200F\u202A\u202B\u202C\u2066\u2067\u2069")

        m_lead = leading_punct.match(l)
        if m_lead:
            punct, rest = m_lead.group(1), m_lead.group(2)
            l = f"{rest}{punct}"

        if l.startswith("-") or l.startswith("–") or l.startswith("—"):
            dash = l[0]
            rest = l[1:].strip()
            l = f"{RLM}{dash} {rest}"

        if trailing_punct.search(l) and not l.endswith(RLM):
            l = f"{l}{RLM}"

        new_lines.append(l)

    return "\n".join(new_lines)


def fix_arabic_rtl_file(file_path):
    """Applies RTL punctuation fix and ad removal directly to a subtitle file."""
    return convert_to_utf8_bom(file_path, apply_rtl_fix=True)


def convert_to_utf8_bom(file_path, apply_rtl_fix=True):
    """Ensures subtitle is saved in UTF-8 with BOM, clean line endings, and automated Arabic RTL fixes."""
    if not os.path.exists(file_path):
        return False
    with open(file_path, "rb") as f:
        raw = f.read()

    encodings = ["utf-8-sig", "utf-8", "windows-1256", "cp1256", "iso-8859-6"]
    for enc in encodings:
        try:
            text = raw.decode(enc)
            text = text.replace('\r\r\n', '\n').replace('\r\n', '\n').replace('\r', '\n')
            if apply_rtl_fix:
                text = fix_arabic_rtl_text(text)
            with open(file_path, "w", encoding="utf-8-sig", newline="\n") as out_f:
                out_f.write(text)
            return True
        except Exception:
            continue
    return False


# ==========================================
# Dialogue Fingerprinting & Deduplication
# ==========================================
def extract_clean_dialogue_lines(sub_text):
    """
    Extracts normalized Arabic dialogue lines (ignoring timestamps, ASS/HTML tags, and translator credit lines)
    for accurate duplicate detection across SubSource and SubDL.
    """
    if not sub_text:
        return []
    ts_re = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->")
    credit_re = re.compile(r"(ترجمة|تعديل|تدقيق|إعداد|مزامنة|رفع|فريق|subsource|subdl|subscene|opensubtitles|@|http|www\.)", re.I)
    ar_word_re = re.compile(r"[\u0621-\u064A]+")

    # Handle ASS format
    if "[Events]" in sub_text and "Dialogue:" in sub_text:
        raw_lines = []
        for line in sub_text.splitlines():
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) == 10:
                    raw_lines.append(parts[9].replace("\\N", "\n").replace("\\n", "\n"))
        lines_to_scan = "\n".join(raw_lines).splitlines()
    else:
        lines_to_scan = sub_text.splitlines()

    clean_lines = []
    for line in lines_to_scan:
        s = line.strip()
        if not s or s.isdigit() or ts_re.search(s):
            continue
        # Remove ASS and HTML tags
        s = re.sub(r"\{[^}]*\}", "", s)
        s = re.sub(r"<[^>]*>", "", s)
        if credit_re.search(s):
            continue
        words = ar_word_re.findall(s)
        if len(words) >= 2:
            # Normalize alef/ya/ta-marbuta for robust matching
            norm = "".join(words)
            norm = re.sub(r"[أإآ]", "ا", norm)
            norm = re.sub(r"ة", "ه", norm)
            norm = re.sub(r"ى", "ي", norm)
            if len(norm) >= 5:
                clean_lines.append(norm)
    return clean_lines


def compute_dialogue_signature(sub_text_or_path, is_path=True):
    """Returns a set of sampled normalized dialogue lines and an exact hash for deduplication."""
    text = ""
    if is_path:
        if not os.path.exists(sub_text_or_path):
            return {"hash": "", "sample_set": set(), "line_count": 0}
        try:
            with open(sub_text_or_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                text = f.read()
        except Exception:
            return {"hash": "", "sample_set": set(), "line_count": 0}
    else:
        text = sub_text_or_path or ""

    lines = extract_clean_dialogue_lines(text)
    if not lines:
        return {"hash": "", "sample_set": set(), "line_count": 0}

    # Sample up to 120 lines evenly across the subtitle (skipping first 2 and last 2 cues which may differ)
    core = lines[2:-2] if len(lines) > 10 else lines
    step = max(1, len(core) // 120)
    sampled = set(core[::step])
    full_hash = hashlib.md5("|".join(core[:80]).encode("utf-8")).hexdigest()
    return {
        "hash": full_hash,
        "sample_set": sampled,
        "line_count": len(lines)
    }


def are_signatures_duplicate(sig1, sig2, threshold=0.80):
    """Returns True if two subtitle signatures represent the exact same translation."""
    if not sig1 or not sig2:
        return False
    if sig1.get("hash") and sig1.get("hash") == sig2.get("hash"):
        return True
    s1 = sig1.get("sample_set") or set()
    s2 = sig2.get("sample_set") or set()
    if not s1 or not s2:
        return False
    smaller = min(len(s1), len(s2))
    if smaller < 3:
        return s1 == s2
    overlap = len(s1.intersection(s2)) / float(smaller)
    return overlap >= threshold


# ==========================================
# Smart Classification: Platform vs Translator
# ==========================================
PLATFORM_DEFINITIONS = [
    ("Netflix", [r"\bnf\b", r"\bnetflix\b", r"نتفلكس", r"نتفليكس"]),
    ("iTunes", [r"\bitunes\b", r"\batvp\b", r"\bapple\s*tv\+?\b", r"\bapple\b", r"ايتونز", r"آيتونز", r"ابل\s*تي\s*في"]),
    ("OSN+", [r"\bosn\+?\b", r"أو\s*إس\s*إن"]),
    ("Amazon", [r"\bamzn\b", r"\bamazon\b", r"\bprime\s*video\b", r"امازون", r"أمازون"]),
    ("Disney+", [r"\bdsnp\b", r"\bdisney\+?\b", r"ديزني"]),
    ("Max", [r"\bhmax\b", r"\bhbo\s*max\b", r"\bhbo\b", r"\bmax\b"]),
    ("Shahid", [r"\bshahid\b", r"شاهد"]),
    ("StarzPlay", [r"\bstarzplay\b", r"\bstarz\b", r"ستارز"]),
    ("Hulu", [r"\bhulu\b", r"هولو"]),
]


def classify_subtitle_entry(item):
    """
    Intelligently classifies a subtitle entry into:
    - category: 'platform' (Tier 1: Official Streaming/Retail Platform) or 'translator' (Tier 2: Individual/Team)
    - platform_name: e.g. 'Netflix', 'iTunes', 'OSN+', 'Amazon', 'Disney+', 'BluRay-Retail', or None
    - translator_name: e.g. 'Ali Talal', 'Haider Almadany', etc.
    """
    raw_name = item.get("name") or ""
    raw_comm = item.get("commentary") or ""
    raw_author = item.get("author") or ""
    prod_type = (item.get("production_type") or "").lower()
    rel_type = (item.get("release_type") or "").lower()
    preview = item.get("preview") or ""
    all_rels = " ".join(item.get("all_releases") or [])

    norm_comm = normalize_fancy_unicode(raw_comm)
    norm_name = normalize_fancy_unicode(raw_name)
    norm_preview = normalize_fancy_unicode(preview)

    # Check if commentary or preview explicitly declares a fan translation / team
    fan_credit_re = re.compile(r"(ترجمة\s*وتعديل|ترجمة\s*وتدقيق|تـرجـمـة|ترجمه\s*و\s*تعديل|ترجمة\s*:\s*|ترجمة\s+[^\s]+\s+[^\s]+)", re.I)
    has_fan_credits_in_preview = bool(fan_credit_re.search(norm_preview))
    has_fan_credits_in_comm = bool(
        re.search(r"(ترجمة\s*وتعديل|ترجمة\s*وتدقيق|فريق|ترجمة\s*حيدر|ترجمة\s*علي|ترجمة\s*أحمد|ترجمة\s*محمد|&)", norm_comm)
        and "ترجمة أصلية" not in norm_comm
        and "ترجمة المنصة" not in norm_comm
        and "ترجمة رسمية" not in norm_comm
    )

    # Detect platform from commentary first (most reliable on SubSource)
    detected_platform = None
    for plat_label, patterns in PLATFORM_DEFINITIONS:
        if any(re.search(p, norm_comm) for p in patterns):
            detected_platform = plat_label
            break

    # Check if commentary or productionType explicitly marks it as official/retail
    is_explicit_retail = (
        prod_type == "retail"
        or any(w in norm_comm for w in ["ترجمة أصلية", "ترجمة اصلية", "ترجمة المنصة", "ترجمة رسمية", "نسخة أصلية", "retail", "official"])
    )

    # If not found in commentary, check release name for explicit platform tags ONLY when retail or single clean release
    if not detected_platform and not has_fan_credits_in_comm and not has_fan_credits_in_preview:
        # Avoid false platform match on SubDL where uploaders concatenate 5+ release names (e.g. XviD...AMZN...YTS...PSA)
        is_concatenated_spam = len(raw_name) > 120 or sum(1 for tag in ["webrip", "x264", "x265", "bluray", "yts", "psa"] if tag in norm_name) >= 3
        if prod_type == "retail" or (prod_type != "translated" and not is_concatenated_spam and item.get("provider") == "subsource"):
            for plat_label, patterns in PLATFORM_DEFINITIONS:
                if any(re.search(p, norm_name) for p in patterns):
                    detected_platform = plat_label
                    break

    if not detected_platform and is_explicit_retail and not has_fan_credits_in_preview:
        if "bluray" in norm_comm or "blu-ray" in norm_comm or rel_type == "bluray":
            detected_platform = "BluRay-Retail"
        else:
            detected_platform = "Official-Retail"

    # Final category decision
    if detected_platform and (is_explicit_retail or (prod_type != "translated" and not has_fan_credits_in_comm and not has_fan_credits_in_preview)):
        category = "platform"
    else:
        category = "translator"
        detected_platform = None

    # Extract clean translator/team name for Tier 2
    translator_name = raw_author.strip()
    if norm_comm and category == "translator":
        # Clean decorative symbols from commentary to get team/translator names if present
        cleaned_comm = re.sub(r"[^\u0600-\u06FFa-zA-Z0-9\s&\-\.]", " ", raw_comm)
        cleaned_comm = re.sub(r"\b(web|bluray|blu-ray|remux|hdr|1080p|2160p|720p)\b", "", cleaned_comm, flags=re.I)
        cleaned_comm = re.sub(r"\s+", " ", cleaned_comm).strip(" -|.")
        if len(cleaned_comm) >= 3:
            translator_name = cleaned_comm[:50]

    return {
        "category": category,
        "platform": detected_platform,
        "translator": translator_name or "Unknown"
    }


def is_banned_subtitle(item):
    """Returns True if subtitle comes from OpenSubtitles or is machine-translated."""
    combined = " ".join([
        str(item.get("name") or ""),
        str(item.get("commentary") or ""),
        str(item.get("author") or ""),
        str(item.get("preview") or "")
    ])
    norm = normalize_fancy_unicode(combined)
    return any(b in norm for b in BANNED_SUB_KEYWORDS)


# ==========================================
# 1. SubDL API Integration (Official API Only)
# ==========================================
def search_subdl(query, imdb_id=None, season=None, episode=None, lang="ar"):
    cfg = load_config()
    api_key = cfg.get("services", {}).get("subdl", {}).get("api_key")
    if not api_key:
        return {"error": True, "provider": "subdl", "message": "SubDL API key not configured"}

    params = {"api_key": api_key, "languages": lang, "subs_per_page": 30}
    if imdb_id:
        clean_imdb = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
        params["imdb_id"] = clean_imdb
    else:
        params["film_name"] = query
    if season:
        params["season_number"] = season
    if episode:
        params["episode_number"] = episode

    url = f"https://api.subdl.com/api/v1/subtitles?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            subs_list = data.get("subtitles", [])
            if not subs_list and imdb_id and query:
                fparams = {"api_key": api_key, "languages": lang, "film_name": query, "subs_per_page": 30}
                if season:
                    fparams["season_number"] = season
                if episode:
                    fparams["episode_number"] = episode
                furl = f"https://api.subdl.com/api/v1/subtitles?{urllib.parse.urlencode(fparams)}"
                freq = urllib.request.Request(furl, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(freq, timeout=10) as fresp:
                    fdata = json.loads(fresp.read().decode("utf-8"))
                    subs_list = fdata.get("subtitles", [])

            results = []
            for s in subs_list:
                dl_url = s.get("url", "")
                if dl_url.startswith("/"):
                    dl_url = f"https://dl.subdl.com{dl_url}"
                item = {
                    "provider": "subdl",
                    "id": s.get("subtitlePage") or dl_url,
                    "name": s.get("release_name") or s.get("name") or "",
                    "author": s.get("author") or "",
                    "commentary": s.get("comment") or "",
                    "production_type": "",
                    "release_type": "",
                    "all_releases": [s.get("release_name") or ""],
                    "downloads": 0,
                    "rating_good": 0,
                    "preview": "",
                    "language": s.get("lang") or s.get("language") or "ar",
                    "download_url": dl_url,
                    "hi": bool(s.get("hi", False))
                }
                if not is_banned_subtitle(item):
                    results.append(item)
            return {"success": True, "provider": "subdl", "count": len(results), "subtitles": results}
    except Exception as e:
        return {"error": True, "provider": "subdl", "message": str(e)}


# ==========================================
# 2. SubSource API Integration (Official API Only)
# ==========================================
def search_subsource(query, imdb_id=None, season=None, episode=None, lang="arabic"):
    cfg = load_config()
    api_key = cfg.get("services", {}).get("subsource", {}).get("api_key")
    if not api_key:
        return {"error": True, "provider": "subsource", "message": "SubSource API key not configured"}

    headers = {"User-Agent": "Mozilla/5.0", "X-API-Key": api_key}

    movie_id = None
    if imdb_id:
        clean_imdb = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
        search_url = f"https://api.subsource.net/api/v1/movies/search?searchType=imdb&imdb={clean_imdb}"
    else:
        search_url = f"https://api.subsource.net/api/v1/movies/search?searchType=text&q={urllib.parse.quote(query)}"

    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            mdata = json.loads(resp.read().decode("utf-8"))
            items = mdata.get("data", [])
            if items:
                if season:
                    for it in items:
                        if it.get("season") == int(season):
                            movie_id = it.get("movieId")
                            break
                if not movie_id:
                    movie_id = items[0].get("movieId")
            elif imdb_id and query:
                q_url = f"https://api.subsource.net/api/v1/movies/search?searchType=text&q={urllib.parse.quote(query)}"
                q_req = urllib.request.Request(q_url, headers=headers)
                with urllib.request.urlopen(q_req, timeout=10) as q_resp:
                    qmdata = json.loads(q_resp.read().decode("utf-8"))
                    qitems = qmdata.get("data", [])
                    if qitems:
                        movie_id = qitems[0].get("movieId")
    except Exception as e:
        return {"error": True, "provider": "subsource", "message": f"Search failed: {e}"}

    if not movie_id:
        return {"success": True, "provider": "subsource", "count": 0, "subtitles": []}

    sub_lang = "arabic" if lang.lower() in ["ar", "ara", "arabic"] else lang.lower()
    subs_url = f"https://api.subsource.net/api/v1/subtitles?movieId={movie_id}&language={sub_lang}"
    try:
        req = urllib.request.Request(subs_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            sdata = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in sdata.get("data", []):
                sub_id = item.get("subtitleId")
                rel_info = item.get("releaseInfo") or []
                name = rel_info[0] if rel_info else f"Subtitle {sub_id}"
                contribs = item.get("contributors") or []
                author = contribs[0].get("displayname", "") if contribs else ""
                rating = item.get("rating") or {}
                entry = {
                    "provider": "subsource",
                    "id": str(sub_id),
                    "name": name,
                    "author": author,
                    "release_type": item.get("releaseType") or "",
                    "production_type": item.get("productionType") or "",
                    "all_releases": rel_info,
                    "downloads": item.get("downloads", 0),
                    "rating_good": rating.get("good", 0),
                    "rating_bad": rating.get("bad", 0),
                    "commentary": item.get("commentary") or "",
                    "preview": item.get("preview") or "",
                    "hi": bool(item.get("hearingImpaired", False)),
                    "download_url": f"https://api.subsource.net/api/v1/subtitles/{sub_id}/download"
                }
                if not is_banned_subtitle(entry):
                    results.append(entry)
            return {"success": True, "provider": "subsource", "count": len(results), "subtitles": results}
    except Exception as e:
        return {"error": True, "provider": "subsource", "message": f"Subtitles query failed: {e}"}


def deduce_release_type(name, all_releases=None, commentary=None):
    """Deduces if a subtitle is timed for BluRay/Remux, WEB-DL, or DVD."""
    text = (name or "").lower()
    if all_releases:
        text += " " + " ".join([str(r).lower() for r in all_releases])
    if commentary:
        text += " " + normalize_fancy_unicode(commentary)

    bluray_signals = ["bluray", "blu-ray", "bdrip", "brrip", "remux", "uhd", "framestor", "cinephiles", "epsilon", "fgt", "ctrlhd", "wildcat"]
    web_signals = ["web-dl", "webrip", "web", "atvp", "amzn", "nf", "dsnp", "max", "hbo", "itunes", "flux", "ethel", "ntb"]

    has_bd = any(s in text for s in bluray_signals)
    has_web = any(s in text for s in web_signals)
    if has_bd and has_web:
        return "both"
    if has_bd:
        return "bluray"
    if has_web:
        return "web"
    return "other"


# ==========================================
# Parallel Search & Two-Tier Ranking (SubSource + SubDL ONLY)
# ==========================================
def search_all_subtitles(query, imdb_id=None, lang="ar", release_type=None, season=None, episode=None):
    """
    Searches ONLY SubSource and SubDL in parallel (takes ~1 second),
    classifies into Tier 1 (Official Platforms) and Tier 2 (Translators/Teams),
    and deduplicates previews.
    """
    results = {
        "query": query,
        "imdb_id": imdb_id,
        "language": lang,
        "release_type": release_type,
        "sources": {}
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_subsource = pool.submit(search_subsource, query, imdb_id, season, episode, "arabic" if lang in ["ar", "ara"] else lang)
        fut_subdl = pool.submit(search_subdl, query, imdb_id, season, episode, lang)
        results["sources"]["subsource"] = fut_subsource.result()
        results["sources"]["subdl"] = fut_subdl.result()

    combined = []
    target_rel = (release_type or "").lower()
    want_bluray = any(k in target_rel for k in ["remux", "bluray", "blu-ray", "uhd", "bdrip"])
    want_web = any(k in target_rel for k in ["web", "web-dl", "webrip"])

    # Prioritize SubSource first because it has rich metadata (productionType, commentary, preview, ratings)
    for prov in ["subsource", "subdl"]:
        data = results["sources"].get(prov, {})
        if not data.get("success"):
            continue
        for s in data.get("subtitles", []):
            cls_info = classify_subtitle_entry(s)
            s["category"] = cls_info["category"]
            s["platform"] = cls_info["platform"]
            s["translator"] = cls_info["translator"]

            deduced = deduce_release_type(s.get("name"), s.get("all_releases"), s.get("commentary"))
            raw_rel = (s.get("release_type") or "").lower()
            s["deduced_type"] = deduced if deduced != "other" else (raw_rel if raw_rel in ["bluray", "web"] else "other")

            score = 0
            # Tier 1: Official Platform Subtitles get highest base priority (+1000)
            if s["category"] == "platform":
                score += 1000
                if s.get("production_type") == "retail":
                    score += 200
            else:
                # Tier 2: Translator / Team Subtitles (+300 base)
                score += 300

            # Release type alignment bonus (even though alass can sync anything, matching release type needs 0 shift)
            eff_type = s["deduced_type"]
            if want_bluray:
                if eff_type in ["bluray", "both"]:
                    score += 150
                elif eff_type == "other":
                    score += 40
            elif want_web:
                if eff_type in ["web", "both"]:
                    score += 150
                elif eff_type == "other":
                    score += 40

            # Exact release group match in all_releases
            if release_type:
                all_rel_text = " ".join(s.get("all_releases") or [s.get("name", "")]).lower()
                if release_type.lower() in all_rel_text:
                    score += 120

            # Trusted translators / teams boost
            trans_lower = (s["translator"] + " " + (s.get("author") or "") + " " + (s.get("commentary") or "")).lower()
            trusted_names = [
                "علي طلال", "ali talal", "dr.ali", "فؤاد الخفاجي", "محمد النعيمي",
                "حيدر المدني", "haider", "زياد ياسر", "كريم الحاج", "أحمد السيد",
                "ahmed elsayed", "nizar", "loki", "عمر", "شيرين", "إسلام", "محمود"
            ]
            if any(t in trans_lower for t in trusted_names):
                score += 90

            # Downloads & Good Ratings bonus
            score += min(int(s.get("downloads", 0)) // 20, 80)
            score += min(int(s.get("rating_good", 0)) * 5, 60)
            if s.get("rating_bad", 0) > s.get("rating_good", 0):
                score -= 100

            # Slight preference for non-HI (normal dialogue without sound effects)
            if s.get("hi"):
                score -= 25

            # Compute preview signature if preview exists (for pre-download deduplication)
            if s.get("preview"):
                s["preview_sig"] = compute_dialogue_signature(s["preview"], is_path=False)

            s["score"] = score
            combined.append(s)

    combined.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Deduplicate entries that have identical preview signatures (keeping the highest-scored one, while inheriting specific platform name if present)
    unique_ranked = []
    seen_preview_entries = []
    for item in combined:
        psig = item.pop("preview_sig", None)
        if psig and psig.get("sample_set"):
            dup_master = None
            for existing_sig, existing_item in seen_preview_entries:
                if are_signatures_duplicate(psig, existing_sig, threshold=0.85):
                    dup_master = existing_item
                    break
            if dup_master is not None:
                # If the duplicate has a specific platform name (e.g. Netflix) and the master had generic BluRay-Retail, inherit the specific platform name!
                if item.get("platform") and item["platform"] not in ["BluRay-Retail", "Official-Retail"]:
                    if dup_master.get("platform") in [None, "BluRay-Retail", "Official-Retail"]:
                        dup_master["platform"] = item["platform"]
                        dup_master["category"] = "platform"
                continue
            seen_preview_entries.append((psig, item))
        unique_ranked.append(item)

    results["ranked_subtitles"] = unique_ranked
    results["platform_subtitles"] = [s for s in unique_ranked if s.get("category") == "platform"]
    results["translator_subtitles"] = [s for s in unique_ranked if s.get("category") == "translator"]
    return results


def vtt_to_srt(vtt_text):
    """Converts WebVTT format to standard SubRip SRT."""
    lines = vtt_text.splitlines()
    srt_lines = []
    counter = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            ts = re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', line)
            srt_lines.append(str(counter))
            srt_lines.append(ts)
            counter += 1
            i += 1
            while i < len(lines) and lines[i].strip():
                clean_text = re.sub(r'<[^>]+>', '', lines[i])
                srt_lines.append(clean_text)
                i += 1
            srt_lines.append("")
        i += 1
    return "\n".join(srt_lines)


def ass_to_srt(ass_text):
    """Converts an ASS/SSA subtitle file into clean SRT so all players and alass handle it uniformly."""
    lines = ass_text.splitlines()
    in_events = False
    format_fields = []
    cues = []

    def parse_ass_time(t_str):
        m = re.match(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", t_str.strip())
        if not m:
            return 0
        h, mn, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return ((h * 3600 + mn * 60 + s) * 1000) + (cs * 10)

    def fmt_srt_time(ms):
        h = ms // 3600000
        mn = (ms % 3600000) // 60000
        s = (ms % 60000) // 1000
        rem = ms % 1000
        return f"{h:02d}:{mn:02d}:{s:02d},{rem:03d}"

    for line in lines:
        s = line.strip()
        if s.lower() == "[events]":
            in_events = True
            continue
        if in_events:
            if s.startswith("[") and s.endswith("]"):
                break
            if s.lower().startswith("format:"):
                format_fields = [f.strip().lower() for f in s[7:].split(",")]
            elif s.lower().startswith("dialogue:") and format_fields:
                vals = s[9:].split(",", len(format_fields) - 1)
                if len(vals) == len(format_fields):
                    row = dict(zip(format_fields, vals))
                    t_start = parse_ass_time(row.get("start", "0:00:00.00"))
                    t_end = parse_ass_time(row.get("end", "0:00:00.00"))
                    raw_txt = row.get("text", "")
                    # Strip ASS override tags like {\pos(...)} or {\c&H...}
                    clean_txt = re.sub(r"\{[^}]*\}", "", raw_txt)
                    clean_txt = clean_txt.replace("\\N", "\n").replace("\\n", "\n").strip()
                    if clean_txt and t_end > t_start:
                        cues.append((t_start, t_end, clean_txt))

    cues.sort(key=lambda x: x[0])
    out = []
    for idx, (st, et, txt) in enumerate(cues, 1):
        out.append(f"{idx}\n{fmt_srt_time(st)} --> {fmt_srt_time(et)}\n{txt}\n")
    return "\n".join(out)


def download_subtitle_file(sub_item, output_dir, file_basename=None):
    os.makedirs(output_dir, exist_ok=True)
    provider = sub_item.get("provider")
    cfg = load_config()

    headers = {"User-Agent": "Mozilla/5.0"}
    if provider == "subsource":
        api_key = cfg.get("services", {}).get("subsource", {}).get("api_key")
        if api_key:
            headers["X-API-Key"] = api_key

    dl_url = sub_item.get("download_url")
    if not dl_url:
        return {"error": True, "message": "No download URL found for subtitle"}

    try:
        req = urllib.request.Request(dl_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
    except Exception as e:
        return {"error": True, "message": f"Download failed: {e}"}

    extracted_files = []
    if content.startswith(b"PK\x03\x04"):
        z = zipfile.ZipFile(io.BytesIO(content))
        valid_entries = [n for n in z.namelist() if n.lower().endswith((".srt", ".ass", ".ssa", ".vtt")) and not n.startswith("__MACOSX")]
        # Prefer .srt over .ass if both are in the zip
        srt_entries = [n for n in valid_entries if n.lower().endswith(".srt")]
        pool_entries = srt_entries if srt_entries else valid_entries

        chosen_entry = None
        if file_basename and pool_entries:
            ep_match = re.search(r'\b[sS](\d{1,2})[eE](\d{1,2})\b', file_basename)
            if ep_match:
                s_int, e_int = int(ep_match.group(1)), int(ep_match.group(2))
                patterns = [
                    rf'[sS]{s_int:02d}[eE]{e_int:02d}',
                    rf'\b[eE]{e_int:02d}\b',
                    rf'\b{s_int}x{e_int:02d}\b',
                ]
                for p in patterns:
                    for ve in pool_entries:
                        if re.search(p, ve, re.IGNORECASE):
                            chosen_entry = ve
                            break
                    if chosen_entry:
                        break

        if not chosen_entry and pool_entries:
            chosen_entry = pool_entries[0]

        if chosen_entry:
            raw_bytes = z.read(chosen_entry)
            ext = os.path.splitext(chosen_entry)[1].lower()
            target_name = f"{file_basename}.srt" if file_basename else os.path.splitext(os.path.basename(chosen_entry))[0] + ".srt"
            target_path = os.path.join(output_dir, target_name)

            if ext in [".ass", ".ssa"]:
                # Decode and convert ASS to clean SRT
                for enc in ["utf-8-sig", "utf-8", "windows-1256", "cp1256"]:
                    try:
                        ass_txt = raw_bytes.decode(enc)
                        srt_txt = ass_to_srt(ass_txt)
                        if srt_txt.strip():
                            with open(target_path, "w", encoding="utf-8-sig", newline="\n") as out_f:
                                out_f.write(srt_txt)
                            break
                    except Exception:
                        continue
            else:
                with open(target_path, "wb") as out_f:
                    out_f.write(raw_bytes)

            convert_to_utf8_bom(target_path, apply_rtl_fix=True)
            extracted_files.append(target_path)
    else:
        target_name = f"{file_basename}.srt" if file_basename else "subtitle.srt"
        target_path = os.path.join(output_dir, target_name)
        if content.startswith(b"WEBVTT"):
            text = content.decode("utf-8", errors="replace")
            srt_text = vtt_to_srt(text)
            with open(target_path, "w", encoding="utf-8-sig", newline="\n") as out_f:
                out_f.write(srt_text)
        else:
            with open(target_path, "wb") as out_f:
                out_f.write(content)
        convert_to_utf8_bom(target_path, apply_rtl_fix=True)
        extracted_files.append(target_path)

    primary_file = extracted_files[0] if extracted_files else None
    return {"success": bool(primary_file), "files": extracted_files, "file_path": primary_file}


# ==========================================
# Embedded Subtitle Inspection (Local or Remote URL)
# ==========================================
def check_embedded_arabic_sub(video_path_or_url, timeout=12):
    """
    Fast check using ffprobe (or mkvmerge for local files) to see if the movie
    already contains an embedded Arabic subtitle track.
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json",
            video_path_or_url
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            streams = data.get("streams", [])
            arabic_tracks = []
            for s in streams:
                tags = s.get("tags") or {}
                lang = str(tags.get("language") or "").lower()
                title = str(tags.get("title") or "").lower()
                if lang in ["ar", "ara", "arabic"] or "arabic" in title or "عربي" in title or "عربية" in title:
                    arabic_tracks.append({
                        "index": s.get("index"),
                        "codec": s.get("codec_name"),
                        "language": lang or "ara",
                        "title": tags.get("title") or "Arabic"
                    })
            return {
                "success": True,
                "has_embedded_arabic": len(arabic_tracks) > 0,
                "arabic_tracks": arabic_tracks,
                "all_subtitle_streams": streams
            }
    except Exception as e:
        return {"success": False, "has_embedded_arabic": False, "error": str(e)}
    return {"success": False, "has_embedded_arabic": False}


# ==========================================
# PGS (.sup) & Text Reference Subtitle Extractor for Instant alass
# ==========================================
def convert_pgs_sup_to_srt(sup_path, out_srt_path):
    """
    Parses a Blu-ray PGS (.sup) subtitle file's binary headers (magic 'PG' + 90kHz PTS)
    into a frame-accurate dummy SRT file in <0.05s so alass can align against Blu-ray Remuxes!
    """
    if not os.path.exists(sup_path):
        return None
    try:
        with open(sup_path, "rb") as f:
            data = f.read()
    except Exception:
        return None

    n = len(data)
    pos = 0
    cues = []
    active_start_ms = None

    while pos + 13 <= n:
        if data[pos:pos+2] != b"PG":
            next_pg = data.find(b"PG", pos + 1)
            if next_pg == -1:
                break
            pos = next_pg
            continue

        pts_ticks, _dts_ticks, seg_type, seg_size = struct.unpack(">IIBH", data[pos+2:pos+13])
        pts_ms = int(round((pts_ticks / 90.0)))
        payload_start = pos + 13
        payload_end = payload_start + seg_size

        # 0x16 = Presentation Composition Segment (PCS)
        if seg_type == 0x16 and payload_start + 11 <= n:
            num_objects = data[payload_start + 10]
            if num_objects > 0:
                if active_start_ms is None:
                    active_start_ms = pts_ms
            else:
                if active_start_ms is not None and pts_ms > active_start_ms:
                    dur = pts_ms - active_start_ms
                    if 150 <= dur <= 15000:
                        cues.append((active_start_ms, pts_ms))
                    active_start_ms = None

        pos = payload_end

    if len(cues) < 20:
        return None

    def fmt_ms(ms):
        h = ms // 3600000
        mn = (ms % 3600000) // 60000
        s = (ms % 60000) // 1000
        rem = ms % 1000
        return f"{h:02d}:{mn:02d}:{s:02d},{rem:03d}"

    lines = []
    for idx, (st, et) in enumerate(cues, 1):
        lines.append(f"{idx}\n{fmt_ms(st)} --> {fmt_ms(et)}\nDialogue {idx}\n")

    with open(out_srt_path, "w", encoding="utf-8", newline="\n") as out_f:
        out_f.write("\n".join(lines))
    return out_srt_path if os.path.exists(out_srt_path) and os.path.getsize(out_srt_path) > 1000 else None


def extract_embedded_reference_sub(video_path, out_path=None):
    """
    Extracts an embedded English/foreign subtitle from MKV to serve as a 100% frame-accurate
    ground-truth reference for alass. Supports both Text (SRT/ASS) AND Blu-ray PGS (.sup) tracks!
    """
    if not os.path.exists(video_path):
        return None
    if not out_path:
        out_path = os.path.splitext(video_path)[0] + ".ref_eng.srt"

    mkvmerge_bin = shutil.which("mkvmerge") or r"C:\Program Files\MKVToolNix\mkvmerge.exe"
    mkvextract_bin = shutil.which("mkvextract") or r"C:\Program Files\MKVToolNix\mkvextract.exe"

    if os.path.exists(mkvmerge_bin) and os.path.exists(mkvextract_bin):
        try:
            res = subprocess.run([mkvmerge_bin, "-J", video_path], capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                data = json.loads(res.stdout)
                text_track_id = None
                pgs_track_id = None

                for t in data.get("tracks", []):
                    if t.get("type") != "subtitles":
                        continue
                    props = t.get("properties") or {}
                    lang = str(props.get("language") or "").lower()
                    tname = str(props.get("track_name") or "").lower()
                    codec = str(t.get("codec") or "").lower()
                    if "forced" in tname or "commentary" in tname:
                        continue
                    is_pref_lang = lang in ["eng", "en", "und", "fre", "spa", "ger", "ita"]
                    if ("subrip" in codec or "text" in codec or "utf8" in codec or "ass" in codec or "ssa" in codec):
                        if is_pref_lang and text_track_id is None:
                            text_track_id = (t.get("id"), codec)
                    elif "pgs" in codec or "hdmv" in codec:
                        if is_pref_lang and pgs_track_id is None:
                            pgs_track_id = t.get("id")

                # 1. Prefer embedded text track (SRT/ASS)
                if text_track_id is not None:
                    tid, tcodec = text_track_id
                    raw_ext = out_path + (".ass" if ("ass" in tcodec or "ssa" in tcodec) else "")
                    ext_res = subprocess.run([mkvextract_bin, "tracks", video_path, f"{tid}:{raw_ext}"], capture_output=True, encoding="utf-8", errors="replace", timeout=90)
                    if ext_res.returncode == 0 and os.path.exists(raw_ext) and os.path.getsize(raw_ext) > 1000:
                        if raw_ext.endswith(".ass"):
                            with open(raw_ext, "r", encoding="utf-8-sig", errors="ignore") as f:
                                srt_conv = ass_to_srt(f.read())
                            try:
                                os.remove(raw_ext)
                            except Exception:
                                pass
                            if srt_conv.strip():
                                with open(out_path, "w", encoding="utf-8", newline="\n") as out_f:
                                    out_f.write(srt_conv)
                                return out_path
                        else:
                            return out_path

                # 2. Fallback to Blu-ray PGS (.sup) track -> convert timestamps to SRT for alass!
                if pgs_track_id is not None:
                    sup_temp = out_path + ".sup"
                    ext_res = subprocess.run([mkvextract_bin, "tracks", video_path, f"{pgs_track_id}:{sup_temp}"], capture_output=True, encoding="utf-8", errors="replace", timeout=120)
                    if ext_res.returncode == 0 and os.path.exists(sup_temp):
                        conv_res = convert_pgs_sup_to_srt(sup_temp, out_path)
                        try:
                            os.remove(sup_temp)
                        except Exception:
                            pass
                        if conv_res:
                            return conv_res
        except Exception:
            pass

    # Fallback with ffmpeg for text streams
    try:
        cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0:s:m:language:eng?", "-c:s", "srt", out_path]
        subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return out_path
    except Exception:
        pass
    return None


# ==========================================
# Timing Verification & alass Synchronization
# ==========================================
def get_alass_bin():
    skill_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        shutil.which("alass"),
        shutil.which("alass-cli"),
        os.path.join(skill_dir, "bin", "alass-windows64", "bin", "alass-cli.exe"),
        os.path.join(skill_dir, "bin", "alass-windows64", "alass.bat"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def parse_sub_timestamps(sub_path):
    """Returns (first_start_sec, last_end_sec, all_starts_list) from an SRT file."""
    try:
        with open(sub_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            text = f.read()
        ts_matches = re.findall(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", text)
        if ts_matches:
            def to_sec(h, m, s, ms):
                return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
            starts = [to_sec(*m[:4]) for m in ts_matches]
            first_start = starts[0]
            last_end = to_sec(*ts_matches[-1][4:])
            return first_start, last_end, starts
    except Exception:
        pass
    return None, None, []


def shift_subtitle_timestamps(sub_path, offset_seconds, output_path=None):
    """Shifts all timestamps in an SRT file by offset_seconds (+ for delay, - for advance)."""
    if not os.path.exists(sub_path):
        return {"error": True, "message": f"Subtitle file not found: {sub_path}"}

    with open(sub_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()

    def shift_match(match):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        ms_start = (int(h1) * 3600 + int(m1) * 60 + int(s1)) * 1000 + int(ms1)
        ms_end = (int(h2) * 3600 + int(m2) * 60 + int(s2)) * 1000 + int(ms2)

        diff_ms = int(round(offset_seconds * 1000))
        new_start = max(0, ms_start + diff_ms)
        new_end = max(new_start + 500, ms_end + diff_ms)

        def fmt(ms_val):
            nh = ms_val // 3600000
            nm = (ms_val % 3600000) // 60000
            ns = (ms_val % 60000) // 1000
            nms = ms_val % 1000
            return f"{nh:02d}:{nm:02d}:{ns:02d},{nms:03d}"

        return f"{fmt(new_start)} --> {fmt(new_end)}"

    pattern = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    new_content = re.sub(pattern, shift_match, content)

    if not output_path:
        base, ext = os.path.splitext(sub_path)
        output_path = f"{base}.shifted{ext}"

    with open(output_path, "w", encoding="utf-8-sig", newline="\n") as out_f:
        out_f.write(new_content)

    return {
        "success": True,
        "output_file": output_path,
        "offset_applied": offset_seconds,
        "message": f"Shifted subtitle by {offset_seconds:+.3f}s successfully"
    }


def check_timing_against_reference(sub_path, ref_sub_path):
    """
    Compares external Arabic subtitle timestamps against extracted reference subtitle (.ref_eng.srt)
    using cross-correlation histogram on early and mid-movie cues.
    Returns (is_matched, offset_seconds, drift_seconds).
    """
    _, _, sub_starts = parse_sub_timestamps(sub_path)
    _, _, ref_starts = parse_sub_timestamps(ref_sub_path)
    if not sub_starts or not ref_starts:
        return False, 0.0, 0.0

    early_ref = [t for t in ref_starts if 120 <= t <= 1200] or ref_starts[:50]
    mid_ref = [t for t in ref_starts if 1200 < t <= 3600] or ref_starts[len(ref_starts)//2:len(ref_starts)//2 + 50]

    def find_peak(r_list):
        buckets = Counter()
        for r in r_list:
            for s in sub_starts:
                diff = r - s
                if -60.0 <= diff <= 60.0:
                    buckets[round(diff, 1)] += 1
        if not buckets:
            return None, 0
        return buckets.most_common(1)[0]

    peak_e, cnt_e = find_peak(early_ref)
    peak_m, cnt_m = find_peak(mid_ref)
    if peak_e is None or cnt_e < 4:
        return False, 0.0, 99.0

    drift = abs((peak_m if (peak_m is not None and cnt_m >= 4) else peak_e) - peak_e)
    is_matched = (abs(peak_e) <= 0.45 and drift <= 0.25)
    return is_matched, round(peak_e, 2), round(drift, 2)


def sync_subtitle_with_alass(video_path, subtitle_path, output_path=None, ref_sub_path=None):
    """
    Synchronizes subtitle using alass (Priority 1), falling back to ffsubsync / AutoSubSync if needed.
    If ref_sub_path is provided (or can be extracted from video_path), alass aligns in <1s with 100% accuracy!
    """
    if not output_path:
        base, ext = os.path.splitext(subtitle_path)
        output_path = f"{base}.synced{ext}"

    convert_to_utf8_bom(subtitle_path, apply_rtl_fix=True)

    # Extract reference subtitle once if not already provided
    cleanup_ref = False
    if not ref_sub_path and os.path.exists(video_path):
        ref_sub_path = extract_embedded_reference_sub(video_path)
        cleanup_ref = True

    # Fast check: Is the subtitle ALREADY perfectly synced (<0.45s offset, <0.25s drift)?
    if ref_sub_path and os.path.exists(ref_sub_path):
        is_matched, offset_s, drift_s = check_timing_against_reference(subtitle_path, ref_sub_path)
        if is_matched:
            shutil.copy2(subtitle_path, output_path)
            if cleanup_ref and os.path.exists(ref_sub_path):
                try:
                    os.remove(ref_sub_path)
                except Exception:
                    pass
            return {
                "success": True,
                "output_file": output_path,
                "tool": "already_matched",
                "shift_seconds": offset_s,
                "drift_seconds": drift_s,
                "message": f"التوقيت متطابق مسبقاً بدقة عالية (الفرق {offset_s:+.2f} ثانية)"
            }

    # Run alass
    alass_bin = get_alass_bin()
    if alass_bin:
        try:
            sync_source = ref_sub_path if (ref_sub_path and os.path.exists(ref_sub_path)) else video_path
            cmd = [alass_bin, sync_source, subtitle_path, output_path]
            subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
            if cleanup_ref and ref_sub_path and os.path.exists(ref_sub_path):
                try:
                    os.remove(ref_sub_path)
                except Exception:
                    pass
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                convert_to_utf8_bom(output_path, apply_rtl_fix=True)
                orig_first, _, _ = parse_sub_timestamps(subtitle_path)
                synced_first, _, _ = parse_sub_timestamps(output_path)
                shift = round((synced_first or 0) - (orig_first or 0), 2) if (orig_first is not None and synced_first is not None) else 0.0
                return {
                    "success": True,
                    "output_file": output_path,
                    "tool": "alass",
                    "shift_seconds": shift,
                    "ref_type": "embedded_sub" if ref_sub_path else "audio"
                }
        except Exception:
            if cleanup_ref and ref_sub_path and os.path.exists(ref_sub_path):
                try:
                    os.remove(ref_sub_path)
                except Exception:
                    pass

    # Fallback: ffsubsync
    ffs_bin = shutil.which("ffsubsync") or os.path.expanduser(r"~\AppData\Local\Programs\Python\Python311\Scripts\ffsubsync.EXE")
    if ffs_bin and os.path.exists(ffs_bin):
        try:
            cmd = [ffs_bin, video_path, "-i", subtitle_path, "-o", output_path, "--max-duration-seconds", "180"]
            subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                convert_to_utf8_bom(output_path, apply_rtl_fix=True)
                return {"success": True, "output_file": output_path, "tool": "ffsubsync"}
        except Exception:
            pass

    return {"error": True, "message": "تعذر ضبط التوقيت عبر alass"}


# Keep backwards-compatible alias
sync_subtitle_with_autosubsync = sync_subtitle_with_alass


def verify_and_auto_sync(video_path, sub_path, ref_sub_path=None):
    """Verifies subtitle timing against video and automatically runs alass if timing is off."""
    return sync_subtitle_with_alass(video_path, sub_path, ref_sub_path=ref_sub_path)


def compare_embedded_vs_external_sub(video_path, sub_path, auto_fix=False):
    """Checks embedded Arabic track first; otherwise compares timing and optionally fixes with alass."""
    emb = check_embedded_arabic_sub(video_path)
    if emb.get("has_embedded_arabic"):
        t0 = emb["arabic_tracks"][0]
        return {
            "has_embedded_arabic": True,
            "embedded_arabic_stream": t0,
            "status": "EMBEDDED_ARABIC_PRESENT",
            "message": f"الفلم يحتوي مسبقاً على ترجمة عربية مدمجة رسمياً (المسار #{t0.get('index')} - {t0.get('codec')})."
        }

    ref_sub = extract_embedded_reference_sub(video_path)
    if ref_sub and os.path.exists(ref_sub):
        is_matched, offset_s, drift_s = check_timing_against_reference(sub_path, ref_sub)
        if is_matched:
            try:
                os.remove(ref_sub)
            except Exception:
                pass
            return {
                "status": "PERFECT_MATCH",
                "offset_seconds": offset_s,
                "drift_seconds": drift_s,
                "message": f"الترجمة متطابقة تماماً مع توقيت الفلم (فرق {offset_s:+.2f} ثانية)."
            }
        elif auto_fix:
            sync_res = sync_subtitle_with_alass(video_path, sub_path, ref_sub_path=ref_sub)
            try:
                os.remove(ref_sub)
            except Exception:
                pass
            return {
                "status": "SYNCED_WITH_ALASS",
                "offset_before": offset_s,
                "drift_before": drift_s,
                "auto_sync": sync_res
            }
        else:
            try:
                os.remove(ref_sub)
            except Exception:
                pass
            return {
                "status": "TIMING_MISMATCH",
                "offset_seconds": offset_s,
                "drift_seconds": drift_s,
                "message": f"يوجد اختلاف في التوقيت ({offset_s:+.2f} ثانية). استخدم alass للمزامنة."
            }

    if auto_fix:
        return sync_subtitle_with_alass(video_path, sub_path)
    return {"status": "UNVERIFIED"}


# ==========================================
# Smart Multi-Subtitle Fetcher (All Platforms + Top Translators, Zero Duplicates)
# ==========================================
def smart_fetch_subtitles(query, imdb_id=None, output_dir=None, video_filename=None, release_type=None, season=None, episode=None, max_translators=3):
    """
    Executes the Smart Subtitle Selection Protocol from SubSource & SubDL ONLY:
    1. Searches SubSource & SubDL in parallel (~1s).
    2. Priority 1 (Official Platforms): Downloads ALL distinct official platform subtitles (e.g. Netflix, OSN+, iTunes, Amazon, Disney+, BluRay-Retail).
    3. Priority 2 (Translators/Teams): Downloads top distinct individual/team translations (up to max_translators).
    4. Content Deduplication: Verifies actual dialogue text of every downloaded file and deletes any duplicate translation!
    """
    if not output_dir:
        output_dir = DEFAULT_SUBS_DIR
    os.makedirs(output_dir, exist_ok=True)

    video_stem = os.path.splitext(os.path.basename(video_filename))[0] if video_filename else re.sub(r'[\\/*?:"<>|]', '', query).strip()

    search_res = search_all_subtitles(query, imdb_id=imdb_id, lang="ar", release_type=release_type, season=season, episode=episode)
    ranked = search_res.get("ranked_subtitles", [])

    if not ranked:
        return {"success": False, "message": "لم يتم العثور على ترجمات عربية في SubSource أو SubDL", "downloaded": []}

    # Filter by episode if this is a series
    if season and episode:
        ep_pats = [
            re.compile(rf"s0*{season}\s*e0*{episode}\b", re.I),
            re.compile(rf"{season}x0*{episode}\b", re.I),
            re.compile(rf"\be0*{episode}\b", re.I),
        ]
        ep_filtered = [
            s for s in ranked
            if any(p.search(s.get("name", "") + " " + " ".join(s.get("all_releases") or [])) for p in ep_pats)
        ]
        if ep_filtered:
            ranked = ep_filtered

    # Group Tier 1 (Official Platforms): Pick the best candidate for EACH distinct platform
    platform_candidates = []
    seen_platforms = set()
    for s in ranked:
        if s.get("category") == "platform" and s.get("platform"):
            plat = s["platform"]
            if plat not in seen_platforms:
                seen_platforms.add(plat)
                platform_candidates.append(s)

    # Group Tier 2 (Individual Translators / Teams): Pick top distinct translators
    translator_candidates = []
    seen_translators = set()
    for s in ranked:
        if s.get("category") == "translator":
            trans_key = re.sub(r"\s+", " ", (s.get("translator") or "").strip().lower())
            if trans_key and trans_key != "unknown" and trans_key in seen_translators:
                continue
            if trans_key and trans_key != "unknown":
                seen_translators.add(trans_key)
            translator_candidates.append(s)
            if len(translator_candidates) >= max_translators + 2:
                break

    # Download candidates and run strict post-download dialogue deduplication
    downloaded_items = []
    accepted_signatures = []
    kept_translators_count = 0

    candidates_queue = [("platform", c) for c in platform_candidates] + [("translator", c) for c in translator_candidates]

    for idx, (tier_type, cand) in enumerate(candidates_queue, 1):
        if tier_type == "translator" and kept_translators_count >= max_translators:
            continue

        temp_base = f"{video_stem}._cand_{idx}"
        dl_res = download_subtitle_file(cand, output_dir, file_basename=temp_base)
        if not dl_res.get("success") or not dl_res.get("file_path") or not os.path.exists(dl_res["file_path"]):
            continue

        temp_path = dl_res["file_path"]

        # Read downloaded content to verify it's valid Arabic and check if a "platform" candidate actually has fan translator credits inside
        try:
            with open(temp_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                sub_text = f.read()
        except Exception:
            try:
                os.remove(temp_path)
            except Exception:
                pass
            continue

        sig = compute_dialogue_signature(sub_text, is_path=False)
        if sig.get("line_count", 0) < 15:
            # Corrupt or empty subtitle
            try:
                os.remove(temp_path)
            except Exception:
                pass
            continue

        # Check if this exact translation was already downloaded (content-level deduplication!)
        is_dup = any(are_signatures_duplicate(sig, existing_sig, threshold=0.80) for existing_sig in accepted_signatures)
        if is_dup:
            try:
                os.remove(temp_path)
            except Exception:
                pass
            continue

        # Re-verify platform vs translator based on full text content
        actual_category = cand.get("category", "translator")
        plat_name = cand.get("platform")
        trans_name = cand.get("translator") or cand.get("author") or "Translator"

        if actual_category == "platform" and cand.get("production_type") != "retail":
            first_cues = "\n".join(sub_text.splitlines()[:45])
            if re.search(r"(ترجمة\s*وتعديل|ترجمة\s*وتدقيق|تـرجـمـة\s*وتـعـديـل)", first_cues):
                actual_category = "translator"
                plat_name = None

        if actual_category == "translator":
            if kept_translators_count >= max_translators:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                continue
            kept_translators_count += 1

        # Build clean, descriptive filename & MKV track title
        if actual_category == "platform" and plat_name:
            safe_tag = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF_\-+]', '', plat_name)
            track_label = f"Arabic [{plat_name} Official]"
        else:
            safe_tag = re.sub(r'[\\/*?:"<>|]', '', trans_name).strip()
            safe_tag = re.sub(r'\s+', '_', safe_tag)[:30] or f"Translator_{kept_translators_count}"
            track_label = f"Arabic [{trans_name}]"

        final_sub_path = os.path.join(output_dir, f"{video_stem}.{safe_tag}.ar.srt")
        # Avoid overwriting if same tag name
        if os.path.exists(final_sub_path) and os.path.abspath(final_sub_path) != os.path.abspath(temp_path):
            final_sub_path = os.path.join(output_dir, f"{video_stem}.{safe_tag}_{idx}.ar.srt")

        shutil.move(temp_path, final_sub_path)
        accepted_signatures.append(sig)

        downloaded_items.append({
            "file_path": final_sub_path,
            "filename": os.path.basename(final_sub_path),
            "category": actual_category,
            "platform": plat_name,
            "translator": trans_name if actual_category == "translator" else None,
            "provider": cand.get("provider"),
            "track_label": track_label,
            "release_name": cand.get("name"),
            "deduced_type": cand.get("deduced_type")
        })

    # Copy the #1 best subtitle to <video_stem>.srt and <video_stem>.ar.srt for instant auto-loading in any media player
    primary_srt = None
    if downloaded_items:
        best_sub_path = downloaded_items[0]["file_path"]
        exact_srt = os.path.join(output_dir, f"{video_stem}.srt")
        exact_ar_srt = os.path.join(output_dir, f"{video_stem}.ar.srt")
        try:
            shutil.copy2(best_sub_path, exact_srt)
            shutil.copy2(best_sub_path, exact_ar_srt)
            primary_srt = exact_srt
        except Exception:
            primary_srt = best_sub_path

    return {
        "success": len(downloaded_items) > 0,
        "primary_subtitle": primary_srt,
        "total_downloaded": len(downloaded_items),
        "platforms_count": sum(1 for d in downloaded_items if d["category"] == "platform"),
        "translators_count": sum(1 for d in downloaded_items if d["category"] == "translator"),
        "downloaded": downloaded_items
    }


# ==========================================
# Auto-Muxing (Embed Subtitles into MKV Container)
# ==========================================
def mux_subtitle_into_video(video_path, subtitle_items, output_path=None, replace_original=True):
    """
    Losslessly embeds one or multiple subtitles into an MKV container using MKVToolNix (mkvmerge) or ffmpeg.
    subtitle_items can be a list of file paths OR a list of dicts: [{"file_path": ..., "track_label": ...}]
    """
    if isinstance(subtitle_items, (str, dict)):
        subtitle_items = [subtitle_items]

    normalized_subs = []
    for item in subtitle_items:
        if isinstance(item, dict):
            fp = item.get("file_path") or item.get("path")
            lbl = item.get("track_label") or item.get("label") or "Arabic"
            if fp and os.path.exists(fp):
                normalized_subs.append((fp, lbl))
        elif isinstance(item, str) and os.path.exists(item):
            normalized_subs.append((item, "Arabic"))

    if not os.path.exists(video_path) or not normalized_subs:
        return {"error": True, "message": "Video or subtitle files do not exist"}

    temp_output = output_path or (os.path.splitext(video_path)[0] + ".mux_temp.mkv")

    mkvmerge_bin = shutil.which("mkvmerge") or (r"C:\Program Files\MKVToolNix\mkvmerge.exe" if os.path.exists(r"C:\Program Files\MKVToolNix\mkvmerge.exe") else None)
    if mkvmerge_bin:
        cmd = [mkvmerge_bin, "-o", temp_output, video_path]
        for idx, (sp, lbl) in enumerate(normalized_subs):
            default_flag = "yes" if idx == 0 else "no"
            cmd.extend(["--language", "0:ara", "--track-name", f"0:{lbl}", "--default-track-flag", f"0:{default_flag}", sp])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
            if res.returncode in [0, 1] and os.path.exists(temp_output):
                if replace_original and not output_path:
                    bak = video_path + ".orig_bak"
                    shutil.move(video_path, bak)
                    shutil.move(temp_output, video_path)
                    if os.path.exists(bak):
                        os.remove(bak)
                    return {"success": True, "output_file": video_path, "tool": "mkvmerge", "tracks_muxed": len(normalized_subs)}
                return {"success": True, "output_file": temp_output, "tool": "mkvmerge", "tracks_muxed": len(normalized_subs)}
        except Exception:
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass

    return {"error": True, "message": "MKVMerge muxing failed"}


# ==========================================
# Sync All Downloaded Subtitles & Mux When Video Finishes
# ==========================================
def sync_all_and_mux(video_path, subtitle_items, mux_into_mkv=True):
    """
    Given a completed local video file and a list of downloaded subtitle paths/dicts:
    1. Extracts the ground-truth embedded reference subtitle (.ref_eng.srt from Text or Blu-ray PGS) once.
    2. Checks timing of every downloaded Arabic subtitle; if timing is off, aligns it using alass!
    3. Updates the external .srt files and optionally muxes all synced tracks into the MKV container.
    """
    if not os.path.exists(video_path):
        return {"error": True, "message": f"Video not found: {video_path}"}

    ref_sub = extract_embedded_reference_sub(video_path)
    synced_results = []

    if isinstance(subtitle_items, str):
        subtitle_items = [subtitle_items]

    for idx, item in enumerate(subtitle_items):
        if isinstance(item, dict):
            sub_path = item.get("file_path") or item.get("path")
            label = item.get("track_label") or item.get("label") or f"Arabic #{idx+1}"
        else:
            sub_path = str(item)
            label = f"Arabic #{idx+1}"

        if not sub_path or not os.path.exists(sub_path):
            continue

        sync_res = sync_subtitle_with_alass(video_path, sub_path, ref_sub_path=ref_sub)
        if sync_res.get("success") and sync_res.get("output_file") and os.path.exists(sync_res["output_file"]):
            synced_file = sync_res["output_file"]
            shutil.copy2(synced_file, sub_path)
            try:
                os.remove(synced_file)
            except Exception:
                pass
            shift_s = sync_res.get("shift_seconds", 0.0)
            tool_u = sync_res.get("tool", "alass")
            if tool_u != "already_matched" and abs(shift_s) > 0.45:
                label_synced = f"{label} (Synced {shift_s:+.1f}s)"
            else:
                label_synced = label
            synced_results.append({
                "file_path": sub_path,
                "track_label": label_synced,
                "sync_tool": tool_u,
                "shift_seconds": shift_s
            })
        else:
            synced_results.append({
                "file_path": sub_path,
                "track_label": label,
                "sync_tool": "unchanged",
                "shift_seconds": 0.0
            })

    if ref_sub and os.path.exists(ref_sub):
        try:
            os.remove(ref_sub)
        except Exception:
            pass

    # Update main <video_stem>.srt and <video_stem>.ar.srt inside the subtitles folder
    if synced_results:
        first_synced = synced_results[0]["file_path"]
        sub_dir = os.path.dirname(first_synced) or DEFAULT_SUBS_DIR
        v_stem = os.path.splitext(os.path.basename(video_path))[0]
        for target_copy in [os.path.join(sub_dir, f"{v_stem}.srt"), os.path.join(sub_dir, f"{v_stem}.ar.srt")]:
            if os.path.abspath(first_synced) != os.path.abspath(target_copy):
                try:
                    shutil.copy2(first_synced, target_copy)
                except Exception:
                    pass

    mux_res = None
    if mux_into_mkv and synced_results:
        mux_res = mux_subtitle_into_video(video_path, synced_results, replace_original=True)

    return {
        "success": True,
        "video_path": video_path,
        "synced_subtitles": synced_results,
        "mux_result": mux_res
    }


def watch_and_sync_video(target_video_path, manifest_or_sub_paths, timeout_minutes=120):
    """
    Waits in the background for Aria2 to finish downloading target_video_path (no .aria2 file),
    then automatically runs alass timing synchronization on all downloaded subtitles and muxes them!
    """
    max_checks = (timeout_minutes * 60) // 6
    aria2_ctl = f"{target_video_path}.aria2"

    for _ in range(max_checks):
        time.sleep(6)
        if os.path.exists(target_video_path) and not os.path.exists(aria2_ctl):
            try:
                if os.path.getsize(target_video_path) > 30 * 1024 * 1024:
                    # Wait 2 seconds to ensure file handle is released by Aria2
                    time.sleep(2)
                    sub_items = []
                    if isinstance(manifest_or_sub_paths, str) and manifest_or_sub_paths.endswith(".json") and os.path.exists(manifest_or_sub_paths):
                        with open(manifest_or_sub_paths, "r", encoding="utf-8") as mf:
                            sub_items = json.load(mf)
                        try:
                            os.remove(manifest_or_sub_paths)
                        except Exception:
                            pass
                    elif isinstance(manifest_or_sub_paths, str):
                        sub_items = [p.strip() for p in manifest_or_sub_paths.split("|") if p.strip()]
                    else:
                        sub_items = manifest_or_sub_paths

                    return sync_all_and_mux(target_video_path, sub_items, mux_into_mkv=True)
            except OSError:
                continue
    return {"error": True, "message": "Timeout waiting for video download completion"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python subtitle_manager.py search <query> [imdb_id] [lang] [release_type]")
        print("  python subtitle_manager.py smart-fetch <query> [imdb_id] [out_dir] [video_filename] [release_type]")
        print("  python subtitle_manager.py check-embedded <video_path_or_url>")
        print("  python subtitle_manager.py compare <video_path> <subtitle_path> [--fix]")
        print("  python subtitle_manager.py sync <video_path> <subtitle_path> [out_path]")
        print("  python subtitle_manager.py sync-all <video_path> <sub1|sub2|...>")
        print("  python subtitle_manager.py watch-and-sync <video_path> <manifest_json_or_subs>")
        print("  python subtitle_manager.py mux <video_path> <subtitle_path> [out_path]")
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "search":
        q = sys.argv[2]
        imdb = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
        lng = sys.argv[4] if len(sys.argv) > 4 else "ar"
        rel = sys.argv[5] if len(sys.argv) > 5 else None
        res = search_all_subtitles(q, imdb_id=imdb, lang=lng, release_type=rel)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "smart-fetch" and len(sys.argv) >= 3:
        q = sys.argv[2]
        imdb = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
        out_d = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "none" else None
        v_fn = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "none" else None
        rel = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] != "none" else None
        res = smart_fetch_subtitles(q, imdb_id=imdb, output_dir=out_d, video_filename=v_fn, release_type=rel)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "check-embedded" and len(sys.argv) >= 3:
        target = sys.argv[2]
        res = check_embedded_arabic_sub(target)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "compare" and len(sys.argv) >= 4:
        vpath = sys.argv[2]
        spath = sys.argv[3]
        fix = len(sys.argv) > 4 and sys.argv[4] in ["--fix", "-f", "true", "fix"]
        res = compare_embedded_vs_external_sub(vpath, spath, auto_fix=fix)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "shift" and len(sys.argv) >= 4:
        spath = sys.argv[2]
        offset = float(sys.argv[3])
        outp = sys.argv[4] if len(sys.argv) > 4 else None
        res = shift_subtitle_timestamps(spath, offset, outp)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "sync" and len(sys.argv) >= 4:
        vpath = sys.argv[2]
        spath = sys.argv[3]
        outp = sys.argv[4] if len(sys.argv) > 4 else None
        res = sync_subtitle_with_alass(vpath, spath, outp)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "sync-all" and len(sys.argv) >= 4:
        vpath = sys.argv[2]
        subs = [s.strip() for s in sys.argv[3].split("|") if s.strip()]
        res = sync_all_and_mux(vpath, subs, mux_into_mkv=True)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "watch-and-sync" and len(sys.argv) >= 4:
        vpath = sys.argv[2]
        manifest = sys.argv[3]
        res = watch_and_sync_video(vpath, manifest)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif action == "mux" and len(sys.argv) >= 4:
        vpath = sys.argv[2]
        spath = sys.argv[3]
        outp = sys.argv[4] if len(sys.argv) > 4 else None
        res = mux_subtitle_into_video(vpath, spath, outp)
        print(json.dumps(res, indent=2, ensure_ascii=False))
