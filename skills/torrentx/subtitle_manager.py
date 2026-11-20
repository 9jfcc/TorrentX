# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import time
import bisect
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


def _is_balanced_pair(s, o="(", c=")"):
    bal = 0
    for ch in s:
        if ch == o:
            bal += 1
        elif ch == c:
            bal -= 1
            if bal < 0:
                return False
    return bal == 0


def fix_arabic_rtl_text(sub_text):
    """
    Cleans OpenSubtitles ads/watermarks, strips ASS/WebVTT cue tags, un-reverses legacy
    LTR visual-hack punctuation/parentheses/dashes/quotes, and wraps Arabic lines with
    Unicode RLE (\u202B) + RLM (\u200F) + PDF (\u202C) so punctuation renders accurately.
    """
    sub_text = clean_subtitle_ads(sub_text)
    sub_text = re.sub(r"\{\\[^}]*\}", "", sub_text)
    # Strip WebVTT inline class/voice/lang tags like <c.white>, </c>, <v Name>, </v>
    sub_text = re.sub(r"</?(?:c(?:\.[^>]*)?|v(?:\s+[^>]*)?|lang(?:\s+[^>]*)?)>", "", sub_text, flags=re.I)
    RLM = "\u200F"
    RLE = "\u202B"
    PDF = "\u202C"
    BIDI_STRIP = " \t\u200E\u200F\u202A\u202B\u202C\u202D\u202E\u2066\u2067\u2068\u2069"
    lines = sub_text.splitlines()
    new_lines = []

    ts_pattern = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
    arabic_pattern = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
    trailing_punct = re.compile(r"([.!?؟:;،,\-\'\"\)\]\}]+)$")
    lead_el_re = re.compile(r"^(\.{2,3})(?:\s+|(?=[(\[\-–—ـ]))(.+)$")
    lead_punct_re = re.compile(r"^([.!؟:;،,]+)\s*(.+)$")

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.isdigit() or ts_pattern.match(stripped):
            new_lines.append(line)
            continue

        if not arabic_pattern.search(line):
            new_lines.append(line)
            continue

        l = line.strip(BIDI_STRIP)

        # Peel outer HTML formatting tags (e.g., <font ...>...</font>, <i>...</i>)
        prefix_tags = ""
        suffix_tags = ""
        core = l
        m_pre = re.match(r"^((?:<(?:font|i|b|u)\b[^>]*>\s*)+)", core, re.I)
        if m_pre:
            prefix_tags = m_pre.group(1).strip()
            core = core[m_pre.end():]
        m_suf = re.search(r"((?:\s*</(?:font|i|b|u)>)+)$", core, re.I)
        if m_suf:
            suffix_tags = m_suf.group(1).strip()
            core = core[:m_suf.start()]
        core = core.strip(BIDI_STRIP)
        if not core:
            new_lines.append(line)
            continue

        # Rule 1: Leading ellipsis + space moved from end (e.g. "... لكن" -> "لكن...")
        m_el = lead_el_re.match(core)
        if m_el and not re.search(r"(\.{2,3}|[.!؟:;،,])$", m_el.group(2)):
            core = f"{m_el.group(2)}{m_el.group(1)}"

        # Rule 2: Leading sentence punctuation moved from end (up to 2 passes)
        for _ in range(2):
            m_lead = lead_punct_re.match(core)
            if m_lead and m_lead.group(1) not in ("..", "..."):
                core = f"{m_lead.group(2).strip(BIDI_STRIP)}{m_lead.group(1)}"
            else:
                break

        # Rule 2b: Leading % moved from trailing number (e.g. "%على أتعابهم الـ 2" -> "على أتعابهم الـ 2%")
        if re.match(r"^%[\u0600-\u06FF]", core) and re.search(r"[\d\u0660-\u0669]$", core):
            core = f"{core[1:]}%"

        # Rule 3: Trailing dialogue dash moved from start (e.g. "ثانية -" -> "- ثانية")
        if not re.match(r"^[-–—ـ]", core):
            m_td = re.search(r"^(.+?)\s+([-–—])$", core)
            if m_td and arabic_pattern.search(m_td.group(1)):
                core = f"{m_td.group(2)} {m_td.group(1)}"

        # Rule 4: Peel optional leading dialogue dash
        m_ld = re.match(r"^([-–—ـ]\s*)(.+)$", core)
        if m_ld:
            lead_dash, inner = m_ld.group(1), m_ld.group(2).strip(BIDI_STRIP)
        else:
            lead_dash, inner = "", core

        # Rule 5: Un-reverse LTR-hacked () and []
        for o, c in (("(", ")"), ("[", "]")):
            if not _is_balanced_pair(inner, o, c) and inner and inner[0] in (o, c):
                rest = inner[1:]
                if rest.count(o) == rest.count(c) + 1 and _is_balanced_pair(rest + c, o, c):
                    m_tp = re.search(r"(\.{2,3}|[.!؟:;،,]+)$", rest)
                    if m_tp:
                        inner = f"{rest[:m_tp.start()]}{c}{m_tp.group(1)}"
                    else:
                        inner = f"{rest}{c}"

            if not _is_balanced_pair(inner, o, c):
                m_end = re.search(r"([.!؟:;،,]*)" + re.escape(c) + r"(\.{2,3}|[.!؟:;،,]*)$", inner)
                if m_end:
                    cand_no_tail = inner[:m_end.start()] + m_end.group(1) + m_end.group(2)
                    cand_opts = [o + cand_no_tail]
                    if cand_no_tail and cand_no_tail[0] in ('"', "'"):
                        cand_opts.append(cand_no_tail[0] + o + cand_no_tail[1:])
                    for cand_fixed in cand_opts:
                        if _is_balanced_pair(cand_fixed, o, c):
                            inner = cand_fixed
                            break

            if inner.count(o) == inner.count(c) + 1 and _is_balanced_pair(inner + c, o, c):
                m_tp = re.search(r"(\.{2,3}|[.!؟:;،,]+)$", inner)
                if m_tp:
                    inner = f"{inner[:m_tp.start()]}{c}{m_tp.group(1)}"
                else:
                    inner = f"{inner}{c}"

        # Rule 6: Un-reverse LTR-hacked double quotes " (ONLY when first " is NOT followed by a letter/digit,
        # so Arabic attached prefixes like بـ"لويس رانيري"، / ﺒ"لويس رانيري"، remain intact!)
        if inner.count('"') == 2 and not re.search(r'(?:^|\s)"', inner):
            first_q = inner.find('"')
            next_ch = inner[first_q + 1] if (first_q >= 0 and first_q + 1 < len(inner)) else ""
            if first_q > 0 and not (next_ch.isalnum() or next_ch in "_ـ"):
                m_q = re.search(r'([.!؟:;،,]*)"(\.{2,3}|[.!؟:;،,]*)$', inner)
                if m_q:
                    inner = '"' + inner[:m_q.start()] + m_q.group(1) + m_q.group(2)

        # Rule 7: Re-attach dash, trailing RLM, and RLE/PDF inside outer HTML tags
        if lead_dash:
            if lead_dash[0] in ("-", "–", "—"):
                core = f"{RLM}{lead_dash[0]} {inner.lstrip()}"
            else:
                core = f"{lead_dash}{inner}"
        else:
            core = inner

        if trailing_punct.search(core) and not core.endswith(RLM):
            core = f"{core}{RLM}"

        new_lines.append(f"{prefix_tags}{RLE}{core}{PDF}{suffix_tags}")

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
def search_subdl(query, imdb_id=None, season=None, episode=None, lang="ar", api_key=None):
    cfg = load_config()
    api_key = api_key or cfg.get("services", {}).get("subdl", {}).get("api_key")
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
def search_subsource(query, imdb_id=None, season=None, episode=None, lang="arabic", api_key=None):
    cfg = load_config()
    api_key = api_key or cfg.get("services", {}).get("subsource", {}).get("api_key")
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
                    "download_url": f"https://api.subsource.net/api/v1/subtitles/{sub_id}/download",
                    "_api_key": api_key
                }
                if not is_banned_subtitle(entry):
                    results.append(entry)
            return {"success": True, "provider": "subsource", "count": len(results), "subtitles": results}
    except Exception as e:
        return {"error": True, "provider": "subsource", "message": f"Subtitles query failed: {e}"}


# ==========================================
# 3. Wyzie Subs API Integration (Optional User Key)
# ==========================================
def search_wyzie(imdb_id, season=None, episode=None, lang="ar", api_key=None):
    if not imdb_id:
        return {"error": True, "provider": "wyzie", "message": "IMDb ID required for Wyzie"}
    cfg = load_config()
    api_key = api_key or cfg.get("services", {}).get("wyzie", {}).get("api_key")
    if not api_key:
        return {"error": True, "provider": "wyzie", "message": "Wyzie API key not configured"}

    clean_imdb = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
    params = {"id": clean_imdb, "language": "ar" if lang in ("ar", "ara", "arabic") else lang, "key": api_key}
    if season:
        params["season"] = season
    if episode:
        params["episode"] = episode

    url = f"https://sub.wyzie.io/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                return {"error": True, "provider": "wyzie", "message": "Unexpected Wyzie response"}
            results = []
            for s in data:
                dl_url = s.get("url") or ""
                if not dl_url:
                    continue
                src_tag = str(s.get("source") or "")
                rel_name = s.get("release") or s.get("media") or f"Wyzie-{src_tag}"
                item = {
                    "provider": "wyzie",
                    "id": dl_url,
                    "name": str(rel_name),
                    "author": src_tag,
                    "commentary": src_tag,
                    "production_type": "",
                    "release_type": "",
                    "all_releases": [str(rel_name)],
                    "downloads": 0,
                    "rating_good": 0,
                    "preview": "",
                    "language": s.get("language") or "ar",
                    "download_url": dl_url,
                    "hi": bool(s.get("isHearingImpaired", False)),
                }
                if not is_banned_subtitle(item):
                    results.append(item)
            return {"success": True, "provider": "wyzie", "count": len(results), "subtitles": results}
    except Exception as e:
        return {"error": True, "provider": "wyzie", "message": str(e)}


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
def _inspect_episode_match(text_blob, season, episode):
    """
    Checks if a subtitle release name/metadata matches the target (season, episode).
    Returns:
      - 'exact': Explicitly matches target episode (e.g. S01E05, 1x05, E05, Ep 5)
      - 'pack': Season pack / complete season without a conflicting episode number
      - 'conflict': Explicitly belongs to a DIFFERENT episode (e.g. S01E01 when target is E05)
      - 'unknown': No explicit episode markers found
    """
    if not episode:
        return "exact"
    s_int = int(season) if season else 1
    e_int = int(episode)
    norm = normalize_fancy_unicode(text_blob or "")

    # 1. Check all SxxExx or SxEx patterns
    se_matches = re.findall(r"(?:^|[^a-z0-9])s0*(\d{1,2})[._\-\s]*e0*(\d{1,3})(?:[._\-\s]*e?0*(\d{1,3}))?(?:$|[^a-z0-9])", norm)
    if se_matches:
        for ms, me1, me2 in se_matches:
            if int(ms) == s_int:
                e1 = int(me1)
                e2 = int(me2) if me2 else e1
                if e1 <= e_int <= e2:
                    return "exact"
        return "conflict"

    # 2. Check 1x05 patterns
    x_matches = re.findall(r"(?:^|[^a-z0-9])0*(\d{1,2})x0*(\d{1,3})(?:$|[^a-z0-9])", norm)
    if x_matches:
        for ms, me in x_matches:
            if int(ms) == s_int and int(me) == e_int:
                return "exact"
        return "conflict"

    # 3. Check standalone Episode / E05 / الحلقة 5 patterns
    ep_matches = re.findall(r"(?:^|[^a-z0-9])(?:ep|episode|e|حلقة|الحلقة)[._\-\s]*0*(\d{1,3})(?:$|[^a-z0-9])", norm)
    if ep_matches:
        if any(int(me) == e_int for me in ep_matches):
            return "exact"
        return "conflict"

    # 4. Check if it's a full season pack
    pack_signals = [
        rf"s0*{s_int}\b", rf"season\s*0*{s_int}\b", r"complete", r"pack",
        r"الموسم", r"موسم\s*كامل", r"كامل"
    ]
    if any(re.search(p, norm) for p in pack_signals):
        return "pack"

    return "unknown"


def search_all_subtitles(query, imdb_id=None, lang="ar", release_type=None, season=None, episode=None, subdl_key=None, subsource_key=None, wyzie_key=None):
    """
    Searches SubSource, SubDL, and optionally Wyzie in parallel,
    strictly filters out wrong episodes for TV series, classifies into Tier 1 (Official Platforms)
    and Tier 2 (Translators/Teams), and deduplicates previews.
    """
    results = {
        "query": query,
        "imdb_id": imdb_id,
        "language": lang,
        "release_type": release_type,
        "sources": {}
    }

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_subsource = pool.submit(search_subsource, query, imdb_id, season, episode, "arabic" if lang in ["ar", "ara"] else lang, subsource_key)
        fut_subdl = pool.submit(search_subdl, query, imdb_id, season, episode, lang, subdl_key)
        fut_wyzie = pool.submit(search_wyzie, imdb_id, season, episode, lang, wyzie_key) if (wyzie_key or load_config().get("services", {}).get("wyzie", {}).get("api_key")) else None
        results["sources"]["subsource"] = fut_subsource.result()
        results["sources"]["subdl"] = fut_subdl.result()
        if fut_wyzie is not None:
            results["sources"]["wyzie"] = fut_wyzie.result()

    combined = []
    target_rel = (release_type or "").lower()
    want_bluray = any(k in target_rel for k in ["remux", "bluray", "blu-ray", "uhd", "bdrip", "brrip"])
    want_web = any(k in target_rel for k in ["web", "web-dl", "webrip", "amzn", "nf", "atvp", "itunes", "dsnp", "hmax"])

    grp_match = re.search(r"-([a-zA-Z0-9_]{2,15})(?:\.[a-z0-9]{2,4})?$", target_rel)
    target_group = grp_match.group(1).lower() if grp_match else ""

    for prov in ["subsource", "subdl", "wyzie"]:
        data = results["sources"].get(prov, {})
        if not data.get("success"):
            continue
        for s in data.get("subtitles", []):
            all_rel_str = " ".join(s.get("all_releases") or [])
            full_blob = f"{s.get('name', '')} {all_rel_str} {s.get('commentary', '')}"

            ep_status = "exact"
            if episode is not None:
                ep_status = _inspect_episode_match(full_blob, season, episode)
                if ep_status == "conflict":
                    continue
            s["ep_match"] = ep_status

            cls_info = classify_subtitle_entry(s)
            s["category"] = cls_info["category"]
            s["platform"] = cls_info["platform"]
            s["translator"] = cls_info["translator"]

            deduced = deduce_release_type(s.get("name"), s.get("all_releases"), s.get("commentary"))
            raw_rel = (s.get("release_type") or "").lower()
            s["deduced_type"] = deduced if deduced != "other" else (raw_rel if raw_rel in ["bluray", "web"] else "other")

            score = 0
            if episode is not None:
                if ep_status == "exact":
                    score += 450
                elif ep_status == "pack":
                    score += 200

            if s["category"] == "platform":
                score += 1000
                if s.get("production_type") == "retail":
                    score += 200
            else:
                score += 300

            eff_type = s["deduced_type"]
            if want_bluray:
                if eff_type in ["bluray", "both"]:
                    score += 220
                elif eff_type == "web":
                    score -= 40
            elif want_web:
                if eff_type in ["web", "both"]:
                    score += 220
                elif eff_type == "bluray":
                    score -= 40

            norm_blob = full_blob.lower()
            if target_group and len(target_group) >= 3 and re.search(rf"\b{re.escape(target_group)}\b", norm_blob):
                score += 260
            for ptag in ["amzn", "nf", "netflix", "atvp", "itunes", "dsnp", "hmax", "bluray", "remux", "web-dl", "webrip"]:
                if ptag in target_rel and ptag in norm_blob:
                    score += 90

            trans_lower = (s["translator"] + " " + (s.get("author") or "") + " " + (s.get("commentary") or "")).lower()
            trusted_names = [
                "علي طلال", "ali talal", "dr.ali", "فؤاد الخفاجي", "محمد النعيمي",
                "حيدر المدني", "haider", "زياد ياسر", "كريم الحاج", "أحمد السيد",
                "ahmed elsayed", "nizar", "loki", "عمر", "شيرين", "إسلام", "محمود"
            ]
            if any(t in trans_lower for t in trusted_names):
                score += 90

            score += min(int(s.get("downloads", 0)) // 20, 80)
            score += min(int(s.get("rating_good", 0)) * 5, 60)
            if s.get("rating_bad", 0) > s.get("rating_good", 0):
                score -= 100

            if s.get("hi"):
                score -= 25

            if s.get("preview"):
                s["preview_sig"] = compute_dialogue_signature(s["preview"], is_path=False)

            s["score"] = score
            combined.append(s)

    combined.sort(key=lambda x: x.get("score", 0), reverse=True)

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
        api_key = sub_item.get("_api_key") or cfg.get("services", {}).get("subsource", {}).get("api_key")
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
        valid_entries = [
            n for n in z.namelist()
            if n.lower().endswith((".srt", ".ass", ".ssa", ".vtt"))
            and not n.startswith("__MACOSX")
            and not os.path.basename(n).startswith(".")
        ]
        srt_entries = [n for n in valid_entries if n.lower().endswith(".srt")]
        pool_entries = srt_entries if srt_entries else valid_entries

        chosen_entry = None
        if file_basename and pool_entries:
            ep_match = re.search(r'(?:^|[^a-zA-Z0-9])[sS]0*(\d{1,2})[._\-\s]*[eE]0*(\d{1,3})(?:$|[^a-zA-Z0-9])', file_basename)
            if ep_match:
                s_int, e_int = int(ep_match.group(1)), int(ep_match.group(2))
                patterns = [
                    rf'(?:^|[^a-zA-Z0-9])[sS]0*{s_int}[._\-\s]*[eE]0*{e_int}(?!\d)',
                    rf'(?:^|[^0-9])0*{s_int}[xX]0*{e_int}(?!\d)',
                    rf'(?:^|[^a-zA-Z0-9])[eE][pP]?[._\-\s]*0*{e_int}(?!\d)',
                    rf'(?:episode|حلقة|الحلقة)[._\-\s]*0*{e_int}(?!\d)',
                    rf'(?:^|[/\\._\-\s])0*{s_int}{e_int:02d}(?!\d)',
                    rf'(?:^|[/\\._\-\s])0*{e_int:02d}(?=\.(?:srt|ass|ssa|vtt)$)',
                ]
                for p in patterns:
                    for ve in pool_entries:
                        if re.search(p, ve, re.IGNORECASE):
                            chosen_entry = ve
                            break
                    if chosen_entry:
                        break

                if not chosen_entry and len(pool_entries) > 1:
                    return {"error": True, "message": f"Episode S{s_int:02d}E{e_int:02d} not found inside ZIP archive"}
                if not chosen_entry and len(pool_entries) == 1:
                    if _inspect_episode_match(pool_entries[0], s_int, e_int) == "conflict":
                        return {"error": True, "message": f"Single file in ZIP belongs to a different episode: {pool_entries[0]}"}

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
                # 4-tier priority: 1) eng/en/ara/ar TEXT, 2) any non-forced/commentary TEXT,
                # 3) eng/en/ara/ar PGS, 4) any non-forced/commentary PGS
                text_p1, text_p2 = None, None
                pgs_p3, pgs_p4 = None, None

                for t in data.get("tracks", []):
                    if t.get("type") != "subtitles":
                        continue
                    props = t.get("properties") or {}
                    lang = str(props.get("language") or "").lower()
                    tname = str(props.get("track_name") or "").lower()
                    codec = str(t.get("codec") or "").lower()
                    if props.get("forced_track") or "forced" in tname or "commentary" in tname or "signs" in tname:
                        continue
                    is_pref_lang = lang in ("eng", "en", "ara", "ar")
                    if ("subrip" in codec or "text" in codec or "utf8" in codec or "ass" in codec or "ssa" in codec):
                        if is_pref_lang and text_p1 is None:
                            text_p1 = (t.get("id"), codec)
                        elif text_p2 is None:
                            text_p2 = (t.get("id"), codec)
                    elif "pgs" in codec or "hdmv" in codec:
                        if is_pref_lang and pgs_p3 is None:
                            pgs_p3 = t.get("id")
                        elif pgs_p4 is None:
                            pgs_p4 = t.get("id")

                text_track_id = text_p1 or text_p2
                pgs_track_id = pgs_p3 or pgs_p4

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

    # Fallback with ffmpeg for text streams (e.g., MP4 mov_text)
    for map_spec in ("0:s:m:language:eng?", "0:s:0"):
        try:
            cmd = ["ffmpeg", "-y", "-i", video_path, "-map", map_spec, "-c:s", "srt", out_path]
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


def _nearest_sorted_val(sorted_vals, target):
    """Returns the closest value to target in a non-empty sorted list in O(log N)."""
    idx = bisect.bisect_left(sorted_vals, target)
    if idx == 0:
        return sorted_vals[0]
    if idx == len(sorted_vals):
        return sorted_vals[-1]
    before = sorted_vals[idx - 1]
    after = sorted_vals[idx]
    return before if (target - before) <= (after - target) else after


def check_timing_against_reference(sub_path, ref_sub_path):
    """
    Compares external Arabic subtitle timestamps against extracted reference subtitle (.ref_eng.srt)
    across 3 balanced segments (Early, Mid, Late) using 0.1s histogram cross-correlation
    + 1-to-1 per-reference-cue median refinement.
    Returns (is_matched, offset_seconds, drift_seconds).
    """
    _, _, sub_starts = parse_sub_timestamps(sub_path)
    _, _, ref_starts = parse_sub_timestamps(ref_sub_path)
    if not sub_starts or not ref_starts:
        return False, 0.0, 0.0

    sub_sorted = sorted(sub_starts)
    ref_sorted = sorted(ref_starts)
    t_min, t_max = ref_sorted[0], ref_sorted[-1]
    span = max(1.0, t_max - t_min)

    seg_early = [t for t in ref_sorted if (t_min + 0.05 * span) <= t <= (t_min + 0.33 * span)]
    seg_mid = [t for t in ref_sorted if (t_min + 0.33 * span) < t <= (t_min + 0.66 * span)]
    seg_late = [t for t in ref_sorted if (t_min + 0.66 * span) < t <= (t_min + 0.95 * span)]
    segs = [seg_early, seg_mid, seg_late]

    if any(len(s) < 8 for s in segs) and len(ref_sorted) >= 9:
        n_ref = len(ref_sorted)
        segs = [
            ref_sorted[: n_ref // 3],
            ref_sorted[n_ref // 3 : (2 * n_ref) // 3],
            ref_sorted[(2 * n_ref) // 3 :],
        ]

    nonempty_segs = [s for s in segs if s]
    if not nonempty_segs:
        return False, 0.0, 99.0

    def _solve_segment_peak(seg_ref):
        buckets = Counter()
        for r in seg_ref:
            lo = bisect.bisect_left(sub_sorted, r - 90.0)
            hi = bisect.bisect_right(sub_sorted, r + 90.0)
            for s in sub_sorted[lo:hi]:
                buckets[round(r - s, 1)] += 1
        if not buckets:
            return None, 0

        top_cands = [b for b, _ in buckets.most_common(5)]
        if 0.0 not in top_cands:
            top_cands.append(0.0)

        best_inliers = []
        best_med_abs = float("inf")
        for cand in top_cands:
            inliers = []
            for r in seg_ref:
                closest_s = _nearest_sorted_val(sub_sorted, r - cand)
                if abs((r - cand) - closest_s) <= 0.45:
                    inliers.append(r - closest_s)
            if inliers:
                med_val = sorted(inliers)[len(inliers) // 2]
                if len(inliers) > len(best_inliers) or (len(inliers) == len(best_inliers) and abs(med_val) < best_med_abs):
                    best_inliers = inliers
                    best_med_abs = abs(med_val)

        if not best_inliers:
            return None, 0

        best_inliers.sort()
        med_off = best_inliers[len(best_inliers) // 2]
        refined = []
        for r in seg_ref:
            closest_s = _nearest_sorted_val(sub_sorted, r - med_off)
            if abs((r - med_off) - closest_s) <= 0.45:
                refined.append(r - closest_s)
        if not refined:
            return None, 0
        refined.sort()
        return refined[len(refined) // 2], len(refined)

    valid_peaks = []
    inlier_cnts = []
    for seg_ref in nonempty_segs:
        pk, cnt = _solve_segment_peak(seg_ref)
        min_req = max(4, int(0.12 * len(seg_ref)))
        if pk is not None and cnt >= min_req:
            valid_peaks.append(pk)
            inlier_cnts.append(cnt)

    if len(valid_peaks) < min(2, len(nonempty_segs)):
        return False, 0.0, 99.0

    total_seg_cues = max(1, sum(len(seg) for seg in nonempty_segs))
    match_ratio = sum(inlier_cnts) / float(total_seg_cues)
    med_offset = sorted(valid_peaks)[len(valid_peaks) // 2]
    max_abs_off = max(abs(p) for p in valid_peaks)
    drift = max(valid_peaks) - min(valid_peaks)
    is_matched = (max_abs_off <= 0.35 and drift <= 0.25 and match_ratio >= 0.30)
    return is_matched, round(med_offset, 3), round(drift, 3)


def _build_local_ref_data_from_srt(ref_sub_path):
    """
    Builds a 5-window ref_data dict directly from a local reference SRT file (.ref_eng.srt)
    so _solve_stream_sync_model can solve constant, FPS affine, and piecewise shifts in <0.05s.
    """
    _, last_end, ref_starts = parse_sub_timestamps(ref_sub_path)
    if not ref_starts or len(ref_starts) < 15:
        return None

    ref_sorted = sorted(ref_starts)
    dur = float(last_end or ref_sorted[-1] or 0.0)
    if dur <= 30.0:
        return None

    fractions = (0.08, 0.27, 0.48, 0.68, 0.86)
    half_win = 22.5
    windows = []
    seen_cues = []

    for frac in fractions:
        center = dur * frac
        ws = max(0.0, center - half_win)
        we = min(dur + 5.0, center + half_win)
        cues_in_w = [r for r in ref_sorted if ws <= r <= we]

        # Expand window if sparse, or snap center to nearest dense cue cluster inside this quintile band
        if len(cues_in_w) < 4:
            ws_exp = max(0.0, center - 60.0)
            we_exp = min(dur + 5.0, center + 60.0)
            exp_cues = [r for r in ref_sorted if ws_exp <= r <= we_exp]
            if len(exp_cues) >= 4:
                ws, we = ws_exp, we_exp
                cues_in_w = exp_cues
            elif ref_sorted:
                nearest_r = _nearest_sorted_val(ref_sorted, center)
                ws = max(0.0, nearest_r - half_win)
                we = min(dur + 5.0, nearest_r + half_win)
                cues_in_w = [r for r in ref_sorted if ws <= r <= we]

        if len(cues_in_w) >= 2:
            # Ensure non-overlapping with previous window
            if windows and ws <= windows[-1][1]:
                ws = windows[-1][1] + 0.1
                cues_in_w = [r for r in ref_sorted if ws <= r <= we]
            if len(cues_in_w) >= 2:
                windows.append((round(ws, 2), round(we, 2)))
                seen_cues.extend(cues_in_w)

    if len(windows) < 2 or len(seen_cues) < 6:
        return None

    clean_times = []
    for val in sorted(seen_cues):
        if not clean_times or (val - clean_times[-1]) > 0.25:
            clean_times.append(val)

    return {
        "times": clean_times,
        "windows": windows,
        "duration": dur,
        "fps": 23.976,
        "source": "local_srt",
    }


def _get_lapse_bin():
    skill_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        shutil.which("lapse"),
        os.path.join(skill_dir, "bin", "lapse", "lapse.exe"),
        os.path.expanduser(r"~\Downloads\AutoSubSync\_internal\resources\lapse\lapse.exe"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _get_ffsubsync_bin():
    for candidate in [
        shutil.which("ffsubsync"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python313\Scripts\ffsubsync.exe"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python311\Scripts\ffsubsync.EXE"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _get_autosubsync_bin():
    for candidate in [
        shutil.which("autosubsync"),
        shutil.which("subsync"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python311\Scripts\subsync.exe"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def sync_subtitle_with_alass(video_path, subtitle_path, output_path=None, ref_sub_path=None):
    """
    Synchronizes subtitle using a hybrid mathematical + verified multi-tool waterfall:
    0.5. Local Multi-Window Math Solver (solves constant, 25<->23.976 FPS affine, piecewise in <0.05s)
    1. alass (runs on FPS pre-corrected subtitle first if affine+cut detected, else original)
    2. lapse (Fallback if alass fails or fails post-sync verification)
    3. ffsubsync --gss (Fallback with full-file FPS golden-section search)
    4. autosubsync / subsync (Fallback)
    Verifies full-movie 3-segment timing after each stage and picks the first verified match
    (or the lowest-error candidate across all stages).
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

    fps_precorrected_path = None
    best_candidate = None
    tools_tried = []

    def _cleanup_temps():
        if cleanup_ref and ref_sub_path and os.path.exists(ref_sub_path):
            try:
                os.remove(ref_sub_path)
            except Exception:
                pass
        if fps_precorrected_path and os.path.exists(fps_precorrected_path):
            try:
                os.remove(fps_precorrected_path)
            except Exception:
                pass

    try:
        # Fast check: Short-circuit ONLY when already ultra-tight (<0.15s offset, <0.18s drift)
        if ref_sub_path and os.path.exists(ref_sub_path):
            is_matched, offset_s, drift_s = check_timing_against_reference(subtitle_path, ref_sub_path)
            if is_matched and abs(offset_s) <= 0.15 and drift_s <= 0.18:
                shutil.copy2(subtitle_path, output_path)
                return {
                    "success": True,
                    "output_file": output_path,
                    "tool": "already_matched",
                    "sync_mode": "natural",
                    "shift_seconds": offset_s,
                    "drift_seconds": drift_s,
                    "verified_matched": True,
                    "post_offset_seconds": offset_s,
                    "post_drift_seconds": drift_s,
                    "message": f"التوقيت متطابق مسبقاً بدقة عالية (الفرق {offset_s:+.2f} ثانية)"
                }

        sync_source = ref_sub_path if (ref_sub_path and os.path.exists(ref_sub_path)) else video_path
        ref_type = "embedded_sub" if (ref_sub_path and os.path.exists(ref_sub_path)) else "audio"
        orig_first, _, _ = parse_sub_timestamps(subtitle_path)
        base_out, ext_out = os.path.splitext(output_path)

        def _evaluate_candidate(tool_name, cand_path, sync_mode=None):
            nonlocal best_candidate
            if not os.path.exists(cand_path) or os.path.getsize(cand_path) <= 1000:
                return False
            convert_to_utf8_bom(cand_path, apply_rtl_fix=True)
            synced_first, _, _ = parse_sub_timestamps(cand_path)
            shift = round((synced_first or 0) - (orig_first or 0), 2) if (orig_first is not None and synced_first is not None) else 0.0
            if ref_sub_path and os.path.exists(ref_sub_path):
                post_matched, post_offset, post_drift = check_timing_against_reference(cand_path, ref_sub_path)
            else:
                post_matched, post_offset, post_drift = (True, 0.0, 0.0)
            score = abs(post_offset) + abs(post_drift)
            cand_info = {
                "tool": tool_name,
                "sync_mode": sync_mode,
                "path": cand_path,
                "shift_seconds": shift,
                "verified_matched": post_matched,
                "post_offset_seconds": post_offset,
                "post_drift_seconds": post_drift,
                "score": score,
            }
            if best_candidate is None or score < best_candidate["score"]:
                if best_candidate and best_candidate["path"] != cand_path and os.path.exists(best_candidate["path"]):
                    try:
                        os.remove(best_candidate["path"])
                    except Exception:
                        pass
                best_candidate = cand_info
            elif os.path.exists(cand_path):
                try:
                    os.remove(cand_path)
                except Exception:
                    pass
            return post_matched

        def _return_cand(cand):
            shutil.move(cand["path"], output_path)
            res = {
                "success": True,
                "output_file": output_path,
                "tool": cand["tool"],
                "shift_seconds": cand["shift_seconds"],
                "ref_type": ref_type,
                "verified_matched": cand["verified_matched"],
                "post_offset_seconds": cand["post_offset_seconds"],
                "post_drift_seconds": cand["post_drift_seconds"],
                "tools_tried": tools_tried,
            }
            if cand.get("sync_mode"):
                res["sync_mode"] = cand["sync_mode"]
            return res

        # 0.5. Local Multi-Window Mathematical Solver (handles FPS a*t+b, constant, piecewise)
        if ref_sub_path and os.path.exists(ref_sub_path):
            try:
                local_ref_data = _build_local_ref_data_from_srt(ref_sub_path)
                if local_ref_data:
                    raw_sub_text = None
                    for enc in ("utf-8-sig", "utf-8", "cp1256", "windows-1256", "iso-8859-6"):
                        try:
                            with open(subtitle_path, "r", encoding=enc) as sf:
                                raw_sub_text = sf.read().replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
                            break
                        except Exception:
                            continue
                    if raw_sub_text:
                        sub_cues = _parse_srt_all_cues_from_text(raw_sub_text)
                        math_model = _solve_stream_sync_model(local_ref_data, sub_cues)
                        m_mode = math_model.get("mode")
                        if math_model.get("tier") in (0, 1) and m_mode in ("constant", "affine", "piecewise"):
                            tools_tried.append(f"math_solver_{m_mode}")
                            transformed_txt = _apply_stream_sync_model(raw_sub_text, math_model)
                            cand_math = f"{base_out}.math{ext_out}"
                            with open(cand_math, "w", encoding="utf-8-sig", newline="\n") as mf:
                                mf.write(transformed_txt)
                            if m_mode == "affine":
                                fps_precorrected_path = f"{base_out}.fps_pre{ext_out}"
                                shutil.copy2(cand_math, fps_precorrected_path)
                            matched_math = _evaluate_candidate(f"math_solver_{m_mode}", cand_math, sync_mode=m_mode)
                            if (
                                matched_math
                                and best_candidate
                                and abs(best_candidate["post_offset_seconds"]) <= 0.25
                                and best_candidate["post_drift_seconds"] <= 0.20
                            ):
                                return _return_cand(best_candidate)
            except Exception:
                pass

        # 1. Tool 1: alass (try FPS pre-corrected first when affine detected, then original)
        alass_bin = get_alass_bin()
        if alass_bin:
            if fps_precorrected_path and os.path.exists(fps_precorrected_path):
                tools_tried.append("alass_post_fps")
                cand_alass_fps = f"{base_out}.alass_fps{ext_out}"
                try:
                    subprocess.run([alass_bin, sync_source, fps_precorrected_path, cand_alass_fps], capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
                    if _evaluate_candidate("alass_post_fps", cand_alass_fps, sync_mode="affine"):
                        return _return_cand(best_candidate)
                except Exception:
                    pass

            tools_tried.append("alass")
            cand_alass = f"{base_out}.alass{ext_out}"
            try:
                subprocess.run([alass_bin, sync_source, subtitle_path, cand_alass], capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
                if _evaluate_candidate("alass", cand_alass):
                    return _return_cand(best_candidate)
            except Exception:
                pass

        # 2. Tool 2: lapse (AutoSubSync bundled engine)
        lapse_bin = _get_lapse_bin()
        if lapse_bin:
            tools_tried.append("lapse")
            cand_lapse = f"{base_out}.lapse{ext_out}"
            try:
                subprocess.run(
                    [lapse_bin, sync_source, subtitle_path, "auto", "--output", cand_lapse, "--no-backup", "--no-sidecar", "--encoding", "utf8-bom", "--quiet"],
                    capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace"
                )
                if _evaluate_candidate("lapse", cand_lapse):
                    return _return_cand(best_candidate)
            except Exception:
                pass

        # 3. Tool 3: ffsubsync (--gss across full file for FPS + offset search)
        ffs_bin = _get_ffsubsync_bin()
        if ffs_bin:
            tools_tried.append("ffsubsync")
            cand_ffs = f"{base_out}.ffsubsync{ext_out}"
            try:
                max_off = "180" if ref_type == "embedded_sub" else "120"
                subprocess.run(
                    [ffs_bin, sync_source, "-i", subtitle_path, "-o", cand_ffs, "--gss", "--max-offset-seconds", max_off],
                    capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace"
                )
                if _evaluate_candidate("ffsubsync", cand_ffs):
                    return _return_cand(best_candidate)
            except Exception:
                pass

        # 4. Tool 4: autosubsync / subsync
        auto_bin = _get_autosubsync_bin()
        if auto_bin:
            tools_tried.append("autosubsync")
            cand_auto = f"{base_out}.autosubsync{ext_out}"
            try:
                subprocess.run(
                    [auto_bin, video_path, subtitle_path, cand_auto],
                    capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace"
                )
                if _evaluate_candidate("autosubsync", cand_auto):
                    return _return_cand(best_candidate)
            except Exception:
                pass

        if best_candidate and os.path.exists(best_candidate["path"]):
            return _return_cand(best_candidate)

        return {"error": True, "message": f"تعذر ضبط التوقيت عبر الأدوات ({', '.join(tools_tried)})"}
    finally:
        _cleanup_temps()


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
# Multi-Window Ground-Truth Stream Sync Engine (Text / PGS / Audio VAD)
# ==========================================
def extract_stream_reference_timestamps(media_url, is_series=False, timeout=28):
    """
    Extracts ground-truth reference timestamps directly from an HTTP stream (or local file)
    using ONE single ffprobe connection (respecting TorBox limits):
      - Single metadata probe detects embedded Arabic tracks + best Text or Blu-ray PGS stream.
      - Tier 1: Extracts packet PTS timestamps across 3 strategic 18s windows (filtering PGS DISPLAY size > 100).
      - Tier 2: Audio VAD fallback (2 x 25s sequential chunks with exact first-packet PTS alignment).
    """
    if not media_url:
        return None

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    try:
        meta_cmd = [
            ffprobe_bin, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", media_url
        ]
        meta_res = subprocess.run(
            meta_cmd, capture_output=True, text=True,
            timeout=max(12, min(timeout, 20)), encoding="utf-8", errors="replace"
        )
        if meta_res.returncode != 0 or not meta_res.stdout:
            return None
        meta = json.loads(meta_res.stdout)
    except Exception:
        return None

    fmt_info = meta.get("format") or {}
    all_streams = meta.get("streams") or []
    try:
        duration = float(fmt_info.get("duration") or 0.0)
    except Exception:
        duration = 0.0
    try:
        bit_rate = float(fmt_info.get("bit_rate") or 0.0)
    except Exception:
        bit_rate = 0.0

    video_fps = 23.976
    video_fps_set = False
    sub_streams = []
    has_embedded_arabic = False
    arabic_tracks = []

    for st in all_streams:
        ctype = st.get("codec_type")
        cname = str(st.get("codec_name") or "").lower()
        if ctype == "video" and not video_fps_set and cname not in ("mjpeg", "png", "bmp", "gif", "webp"):
            r_fps = st.get("r_frame_rate") or st.get("avg_frame_rate") or ""
            if "/" in r_fps:
                num, den = r_fps.split("/", 1)
                try:
                    if float(den) > 0:
                        video_fps = round(float(num) / float(den), 3)
                        video_fps_set = True
                except Exception:
                    pass
        elif ctype == "subtitle":
            sub_streams.append(st)
            tags = st.get("tags") or {}
            lang = str(tags.get("language") or "und").lower()
            title = str(tags.get("title") or "")
            if lang in ("ar", "ara", "arabic") or "arabic" in title.lower() or "عربي" in title.lower():
                has_embedded_arabic = True
                arabic_tracks.append({
                    "index": st.get("index"),
                    "codec": st.get("codec_name") or "",
                    "language": lang,
                    "title": title,
                })
        if not duration and st.get("duration"):
            try:
                duration = max(duration, float(st.get("duration")))
            except Exception:
                pass

    if has_embedded_arabic:
        return {
            "has_embedded_arabic": True,
            "arabic_tracks": arabic_tracks,
            "times": [],
            "windows": [],
            "duration": duration,
            "fps": video_fps,
            "source": "embedded_arabic",
        }

    eff_dur = duration or (2520.0 if is_series else 6600.0)
    text_codecs = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt"}
    pgs_codecs = {"hdmv_pgs_subtitle"}

    # Strict priority: 1) eng/en/ara/ar TEXT, 2) any TEXT, 3) eng/en/ara/ar PGS, 4) any PGS
    cand_p1, cand_p2, cand_p3, cand_p4 = None, None, None, None
    for pos, st in enumerate(sub_streams):
        codec = str(st.get("codec_name") or "").lower()
        tags = st.get("tags") or {}
        lang = str(tags.get("language") or "").lower()
        title = str(tags.get("title") or "").lower()
        if (st.get("disposition") or {}).get("forced") == 1 or "forced" in title or "commentary" in title or "signs" in title:
            continue
        is_pref_lang = lang in ("eng", "en", "ara", "ar")
        if codec in text_codecs:
            if is_pref_lang and cand_p1 is None:
                cand_p1 = (pos, False)
            elif cand_p2 is None:
                cand_p2 = (pos, False)
        elif codec in pgs_codecs:
            if is_pref_lang and cand_p3 is None:
                cand_p3 = (pos, True)
            elif cand_p4 is None:
                cand_p4 = (pos, True)

    chosen = cand_p1 or cand_p2 or cand_p3 or cand_p4

    # Tier 1: Text or Blu-ray PGS packet extraction in ONE single ffprobe connection
    if chosen is not None:
        chosen_pos, is_pgs = chosen
        if bit_rate > 50_000_000:
            win_dur = 12
        elif bit_rate > 20_000_000 or bit_rate <= 0:
            win_dur = 15
        else:
            win_dur = 18

        if is_series or eff_dur < 3600:
            raw_wins = [
                (max(75, int(eff_dur * 0.10)), win_dur),
                (int(eff_dur * 0.36), win_dur),
                (int(eff_dur * 0.62), win_dur),
                (int(eff_dur * 0.82), win_dur),
            ]
        else:
            raw_wins = [
                (max(150, int(eff_dur * 0.10)), win_dur),
                (int(eff_dur * 0.33), win_dur),
                (int(eff_dur * 0.56), win_dur),
                (int(eff_dur * 0.80), win_dur),
            ]
        intervals_arg = ",".join(f"{ws}%+{dur_w}" for ws, dur_w in raw_wins)
        try:
            pkt_cmd = [
                ffprobe_bin, "-v", "quiet", "-print_format", "json",
                "-select_streams", f"s:{chosen_pos}",
                "-show_packets", "-show_entries", "packet=pts_time,size",
                "-read_intervals", intervals_arg,
                media_url,
            ]
            pkt_res = subprocess.run(
                pkt_cmd, capture_output=True, text=True,
                timeout=max(75, timeout), encoding="utf-8", errors="replace"
            )
            if pkt_res.returncode == 0 and pkt_res.stdout:
                pkts = json.loads(pkt_res.stdout).get("packets") or []
                max_valid_t = eff_dur + 60.0
                pts_list = []
                for p in pkts:
                    if is_pgs and int(p.get("size") or 0) <= 100:
                        continue
                    t_val = p.get("pts_time")
                    if t_val is not None:
                        fv = float(t_val)
                        if 1.0 <= fv <= max_valid_t:
                            pts_list.append(fv)
                clean = []
                for val in sorted(pts_list):
                    if not clean or (val - clean[-1]) > 0.30:
                        clean.append(val)
                if len(clean) >= 8:
                    windows = [(max(0.0, ws - 5.0), ws + dur_w + 5.0) for ws, dur_w in raw_wins]
                    return {
                        "has_embedded_arabic": False,
                        "arabic_tracks": [],
                        "times": clean,
                        "windows": windows,
                        "duration": eff_dur,
                        "fps": video_fps,
                        "source": "pgs" if is_pgs else "text",
                    }
        except Exception:
            pass

    # Tier 2: Audio VAD fallback (ONLY when no Text/PGS stream yielded >= 8 cues)
    try:
        import tempfile
        import wave
        import webrtcvad
    except ImportError:
        return {
            "has_embedded_arabic": False,
            "arabic_tracks": [],
            "times": [],
            "windows": [],
            "duration": eff_dur,
            "fps": video_fps,
            "source": "none",
        }

    aud_dur = 16 if bit_rate > 50_000_000 else 20
    if is_series or eff_dur < 3600:
        aud_wins = [(60, aud_dur), (max(180, int(eff_dur * 0.62)), aud_dur)]
    else:
        aud_wins = [(150, aud_dur), (max(600, int(eff_dur * 0.65)), aud_dur)]

    all_onsets = []
    valid_windows = []
    vad = webrtcvad.Vad(2)
    frame_ms = 30
    frame_bytes = int(16000 * (frame_ms / 1000.0) * 2)

    for ws, dur_w in aud_wins:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                mka_path = os.path.join(tmpdir, "chunk.mka")
                wav_path = os.path.join(tmpdir, "chunk.wav")
                fast_seek = max(0, int(ws) - 8)
                cmd = [
                    ffmpeg_bin, "-y", "-v", "quiet",
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "3",
                    "-ss", str(fast_seek), "-i", media_url,
                    "-copyts", "-ss", str(int(ws)), "-t", str(int(dur_w)),
                    "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-f", "matroska", mka_path,
                ]
                res = subprocess.run(cmd, capture_output=True, timeout=60)
                if res.returncode != 0 or not os.path.exists(mka_path) or os.path.getsize(mka_path) < 30000:
                    continue

                actual_start = float(ws)
                try:
                    p_pts = subprocess.run(
                        [ffprobe_bin, "-v", "error", "-select_streams", "a:0",
                         "-show_packets", "-read_intervals", "%+#1",
                         "-show_entries", "packet=pts_time", "-of", "csv=p=0", mka_path],
                        capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace"
                    )
                    pts_lines = [ln.strip() for ln in (p_pts.stdout or "").splitlines() if ln.strip()]
                    if pts_lines:
                        actual_start = float(pts_lines[0])
                    else:
                        p_fmt = subprocess.run(
                            [ffprobe_bin, "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", mka_path],
                            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace"
                        )
                        actual_start = float((p_fmt.stdout or "").strip())
                except Exception:
                    actual_start = float(ws)

                subprocess.run(
                    [ffmpeg_bin, "-y", "-v", "quiet", "-i", mka_path, "-c:a", "pcm_s16le", wav_path],
                    capture_output=True, timeout=15
                )
                if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 30000:
                    continue

                win_onsets = []
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
                                onset = round(actual_start + t_rel - (3 * frame_ms / 1000.0), 3)
                                if not win_onsets or (onset - win_onsets[-1]) > 0.45:
                                    win_onsets.append(onset)
                        else:
                            silence_run += 1
                            speech_run = 0
                            if in_speech and silence_run >= 10:
                                in_speech = False
                        t_rel += frame_ms / 1000.0

                if len(win_onsets) >= 3:
                    all_onsets.extend(win_onsets)
                    valid_windows.append((max(0.0, actual_start - 5.0), actual_start + dur_w + 5.0))
        except Exception:
            continue

    if len(all_onsets) >= 5:
        return {
            "has_embedded_arabic": False,
            "arabic_tracks": [],
            "times": sorted(all_onsets),
            "windows": valid_windows,
            "duration": eff_dur,
            "fps": video_fps,
            "source": "audio",
        }
    return {
        "has_embedded_arabic": False,
        "arabic_tracks": [],
        "times": [],
        "windows": [],
        "duration": eff_dur,
        "fps": video_fps,
        "source": "none",
    }


def _parse_srt_all_cues_from_text(srt_text):
    """Returns list of (start_sec, end_sec) for all valid dialogue cues in an SRT string."""
    cues = []
    pattern = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    for m in re.finditer(pattern, srt_text or ""):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        st = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        et = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        if st >= 0.5 and et > st:
            cues.append((st, et))
    return cues


def _find_window_offset_consensus(ref_w, sub_w, max_shift=90.0, tol=0.55):
    """Finds the best constant offset (ref - sub) inside a single time window using 1-to-1 consensus voting."""
    if len(ref_w) < 2 or len(sub_w) < 2:
        return 0.0, 0, 0.0

    sub_w_sorted = sorted(sub_w)
    candidates = []
    for r in ref_w:
        lo = bisect.bisect_left(sub_w_sorted, r - max_shift)
        hi = bisect.bisect_right(sub_w_sorted, r + max_shift)
        for s in sub_w_sorted[lo:hi]:
            candidates.append(r - s)

    if not candidates:
        return 0.0, 0, 0.0

    bins = Counter(round(c * 5) / 5.0 for c in candidates)
    top_centers = [b for b, _ in bins.most_common(8)]
    if 0.0 not in top_centers:
        top_centers.append(0.0)
    best_inliers = []
    best_med_abs = float("inf")

    for cand in top_centers:
        inliers = []
        for r in ref_w:
            closest_s = _nearest_sorted_val(sub_w_sorted, r - cand)
            if abs((r - cand) - closest_s) <= tol:
                inliers.append(r - closest_s)
        if inliers:
            med_abs = abs(sorted(inliers)[len(inliers) // 2])
            if len(inliers) > len(best_inliers) or (len(inliers) == len(best_inliers) and med_abs < best_med_abs):
                best_inliers = inliers
                best_med_abs = med_abs

    if not best_inliers:
        return 0.0, 0, 0.0

    best_inliers.sort()
    median_off = best_inliers[len(best_inliers) // 2]
    refined = []
    for r in ref_w:
        closest_s = _nearest_sorted_val(sub_w_sorted, r - median_off)
        if abs((r - median_off) - closest_s) <= tol:
            refined.append(r - closest_s)
    if refined:
        refined.sort()
        median_off = refined[len(refined) // 2]
        best_inliers = refined

    denom = max(1, min(len(ref_w), len(sub_w)))
    ratio = len(best_inliers) / float(denom)
    return round(median_off, 3), len(best_inliers), ratio


def _evaluate_transform_windows(ref_data, sub_starts, transform_fn, tol=0.55):
    """
    Evaluates a timestamp transformation f(t) across all sampled reference windows
    using 1-to-1 per-reference-cue matching (0 <= total_inliers <= len(ref_times)).
    Returns (total_inliers, matched_windows_count, active_windows_count, median_residual_ms).
    """
    ref_times = ref_data.get("times") or []
    windows = ref_data.get("windows") or [(10.0, 180.0)]
    transformed = [transform_fn(s) for s in sub_starts]

    total_inliers = 0
    matched_windows = 0
    active_windows = 0
    residuals = []

    for ws, we in windows:
        ref_w = [r for r in ref_times if ws <= r <= we]
        if len(ref_w) < 2:
            continue
        active_windows += 1
        sub_w = sorted(t for t in transformed if (ws - 1.5) <= t <= (we + 1.5))
        inliers = 0
        if sub_w:
            for r in ref_w:
                closest_t = _nearest_sorted_val(sub_w, r)
                err = abs(r - closest_t)
                if err <= tol:
                    inliers += 1
                    residuals.append(err * 1000.0)
        req_w = max(2, min(len(ref_w), max(2, len(sub_w))) // 3)
        if inliers >= req_w:
            matched_windows += 1
        total_inliers += inliers

    med_res_ms = round(sorted(residuals)[len(residuals) // 2], 1) if residuals else None
    return total_inliers, matched_windows, max(1, active_windows), med_res_ms


def _solve_stream_sync_model(ref_data, sub_cues):
    """
    Solves for the optimal full-video sync model across all sampled windows:
      - Model 0 ('natural' / micro-offset): 0 or tiny uniform shift verified across windows.
      - Model 1 ('constant'): t' = t + b verified across windows.
      - Model 2 ('affine'): t' = a * t + b (fixes 25fps <-> 23.976fps/24fps PAL/NTSC drift).
      - Model 3 ('piecewise'): per-act shifts snapped to scene gaps.
    Includes anti-damage guardrails for single-window fallback, audio VAD, natural sync, and affine fitting.
    """
    if not ref_data or not ref_data.get("times") or len(sub_cues) < 5:
        return {"mode": "none", "a": 1.0, "b": 0.0, "segments": [], "tier": 2, "score": 0, "win_ok": 0, "active_windows": 0, "post_inliers": 0, "median_residual_ms": None, "label": ""}

    ref_times = ref_data["times"]
    windows = ref_data.get("windows") or [(10.0, 180.0)]
    source = ref_data.get("source", "")
    is_sub_ref = source in ("text", "pgs", "local_srt")
    tight_tol = 0.35 if source == "audio" else 0.25
    loose_tol = 0.68 if source == "audio" else 0.48
    tol = loose_tol
    sub_starts = [c[0] for c in sub_cues]
    sub_starts_sorted = sorted(sub_starts)

    def _score_model(transform_fn):
        t_inl, t_wok, aw, t_res = _evaluate_transform_windows(ref_data, sub_starts, transform_fn, tol=tight_tol)
        l_inl, l_wok, _, l_res = _evaluate_transform_windows(ref_data, sub_starts, transform_fn, tol=loose_tol)
        res_pen = (l_res if l_res is not None else 500.0) / 50.0
        sc = round((t_inl * 4.0) + (l_inl * 1.5) + (t_wok * 12.0) + (l_wok * 6.0) - res_pen, 2)
        return t_inl, l_inl, t_wok, l_wok, aw, t_res, l_res, sc

    nat_t_inl, nat_inliers, nat_t_wok, nat_win_ok, active_wins, nat_t_res, nat_res_ms, nat_score = _score_model(
        lambda t: t
    )

    win_stats = []
    for ws, we in windows:
        ref_w = [r for r in ref_times if ws <= r <= we]
        if len(ref_w) < 2:
            continue
        sub_w = [s for s in sub_starts if (ws - 90.0) <= s <= (we + 90.0)]
        off_w, cnt_w, rat_w = _find_window_offset_consensus(ref_w, sub_w, max_shift=90.0, tol=tol)
        req_w = max(2, min(len(ref_w), 8) // 3)
        if cnt_w >= req_w:
            center_t = 0.5 * (ws + we) - off_w
            win_stats.append((center_t, off_w, cnt_w, rat_w, ws, we))

    if win_stats:
        all_small_offsets = all(abs(w[1]) <= 0.35 for w in win_stats)
        if all_small_offsets and len(win_stats) >= max(1, active_wins - 1):
            avg_off = sum(w[1] * w[2] for w in win_stats) / max(1, sum(w[2] for w in win_stats))
            if abs(avg_off) <= 0.12:
                return {
                    "mode": "natural",
                    "a": 1.0,
                    "b": 0.0,
                    "segments": [],
                    "tier": 0,
                    "score": nat_score + 50.0,
                    "win_ok": nat_win_ok,
                    "active_windows": active_wins,
                    "tight_inliers": nat_t_inl,
                    "post_inliers": nat_inliers,
                    "median_residual_ms": nat_res_ms,
                    "label": "✅ متزامنة أصلياً",
                }
            if active_wins >= 2 or (is_sub_ref and len(win_stats) >= 1 and abs(avg_off) <= 0.35):
                micro_off = round(avg_off, 3)
                m_t_inl, m_inl, _, m_ok, _, _, m_res, m_sc = _score_model(lambda t, b=micro_off: t + b)
                if m_sc >= nat_score and m_ok >= nat_win_ok:
                    return {
                        "mode": "constant",
                        "a": 1.0,
                        "b": micro_off,
                        "segments": [],
                        "tier": 0,
                        "score": m_sc + 50.0,
                        "win_ok": m_ok,
                        "active_windows": active_wins,
                        "tight_inliers": m_t_inl,
                        "post_inliers": m_inl,
                        "median_residual_ms": m_res,
                        "label": f"✅ متزامنة (ضبط دقيق {micro_off:+.2f}s)",
                    }

    best_model = {
        "mode": "natural",
        "a": 1.0,
        "b": 0.0,
        "segments": [],
        "tier": 0 if (nat_win_ok == active_wins and active_wins > 0) else 2,
        "score": nat_score,
        "win_ok": nat_win_ok,
        "active_windows": active_wins,
        "tight_inliers": nat_t_inl,
        "post_inliers": nat_inliers,
        "median_residual_ms": nat_res_ms,
        "label": "✅ متزامنة أصلياً" if nat_win_ok == active_wins else "",
    }

    nat_is_strong = (
        nat_win_ok == active_wins
        and active_wins >= 2
        and nat_t_inl >= max(5, int(0.30 * len(ref_times)))
        and nat_res_ms is not None
        and nat_res_ms <= 180.0
    )

    # Global Constant Consensus across ALL windows together
    cand_offsets = set(round(w[1], 3) for w in win_stats)
    if win_stats:
        weighted_b = sum(w[1] * w[2] for w in win_stats) / max(1, sum(w[2] for w in win_stats))
        cand_offsets.add(round(weighted_b, 3))

    global_diffs = []
    for r in ref_times:
        lo = bisect.bisect_left(sub_starts_sorted, r - 90.0)
        hi = bisect.bisect_right(sub_starts_sorted, r + 90.0)
        for s in sub_starts_sorted[lo:hi]:
            global_diffs.append(r - s)
    if global_diffs:
        g_bins = Counter(round(d * 10) / 10.0 for d in global_diffs)
        g_centers = [b for b, _ in g_bins.most_common(10)]
        if 0.0 not in g_centers:
            g_centers.append(0.0)
        for gc in g_centers:
            g_inliers = []
            for r in ref_times:
                closest_s = _nearest_sorted_val(sub_starts_sorted, r - gc)
                if abs((r - gc) - closest_s) <= tol:
                    g_inliers.append(r - closest_s)
            if g_inliers:
                g_inliers.sort()
                cand_offsets.add(round(g_inliers[len(g_inliers) // 2], 3))

    best_const_inliers = nat_inliers
    for b_cand in sorted(cand_offsets, key=lambda x: abs(x)):
        if active_wins < 2:
            if source == "audio" and abs(b_cand) > 1.0:
                continue
            if abs(b_cand) > 10.0:
                continue

        t_inl, inl, t_wok, w_ok, _, t_res, r_ms, sc = _score_model(lambda t, b=b_cand: t + b)
        if inl > best_const_inliers:
            best_const_inliers = inl

        if active_wins < 2:
            if abs(b_cand) > 0.005:
                req_single_inl = max(nat_inliers + 4, int(0.65 * len(ref_times)), 6)
                if inl < req_single_inl or r_ms is None or r_ms > 220.0:
                    continue
        elif source == "audio" and abs(b_cand) > 0.5:
            req_aud_inl = max(nat_inliers + 3, int(0.45 * len(ref_times)), 6)
            if w_ok < 2 or inl < req_aud_inl:
                continue

        if nat_is_strong and abs(b_cand) > 1.0:
            if sc <= (nat_score + 20.0) or t_inl < (nat_t_inl + 3):
                continue

        if w_ok >= min(2, active_wins) and inl >= nat_inliers:
            if w_ok == active_wins and abs(b_cand) <= 0.12 and nat_win_ok == active_wins and nat_score >= (sc - 2.0):
                if best_model["score"] <= nat_score:
                    best_model = {
                        "mode": "natural",
                        "a": 1.0,
                        "b": 0.0,
                        "segments": [],
                        "tier": 0,
                        "score": nat_score,
                        "win_ok": nat_win_ok,
                        "active_windows": active_wins,
                        "tight_inliers": nat_t_inl,
                        "post_inliers": nat_inliers,
                        "median_residual_ms": nat_res_ms,
                        "label": "✅ متزامنة أصلياً",
                    }
            elif sc > best_model["score"] or (w_ok > best_model["win_ok"] and t_inl >= best_model.get("tight_inliers", 0)):
                sign = "+" if b_cand >= 0 else ""
                is_micro = abs(b_cand) <= 0.35 and w_ok == active_wins
                best_model = {
                    "mode": "constant",
                    "a": 1.0,
                    "b": b_cand,
                    "segments": [],
                    "tier": 0 if is_micro else 1,
                    "score": sc,
                    "win_ok": w_ok,
                    "active_windows": active_wins,
                    "tight_inliers": t_inl,
                    "post_inliers": inl,
                    "median_residual_ms": r_ms,
                    "label": f"✅ متزامنة (ضبط دقيق {sign}{b_cand:.2f}s)" if is_micro else f"🔄 تمت المزامنة ({sign}{b_cand:.2f}s)",
                }

    # Strict Natural/Micro Protection Gate: ONLY protect genuine natural or <=0.50s micro-offset!
    # NEVER short-circuit affine for large shifts (like +20.49s or -69.87s).
    if (
        active_wins >= 2
        and abs(best_model.get("b", 0.0)) <= 0.50
        and best_model["win_ok"] == active_wins
        and best_model.get("tight_inliers", 0) >= max(5, int(0.30 * len(ref_times)))
        and best_model["post_inliers"] >= max(6, int(0.40 * len(ref_times)))
        and best_model["median_residual_ms"] is not None
        and best_model["median_residual_ms"] <= 200.0
    ):
        return best_model

    # Affine FPS (25fps <-> 23.976fps / 24fps) & Piecewise: strictly require >= 2 active windows
    if active_wins >= 2:
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
                sub_w = [a_rat * s for s in sub_starts if (ws - 90.0) <= (a_rat * s) <= (we + 90.0)]
                b_est, cnt_est, _ = _find_window_offset_consensus(ref_w, sub_w, max_shift=90.0, tol=tol)
                if cnt_est >= 2:
                    affine_candidates.append((a_rat, b_est))

            # Global consensus across all ref_times for a_rat within +-60s (protects against sparse window 1 + intro logo offset)
            scaled_subs = sorted(a_rat * s for s in sub_starts)
            g_aff_diffs = []
            for r in ref_times:
                lo = bisect.bisect_left(scaled_subs, r - 60.0)
                hi = bisect.bisect_right(scaled_subs, r + 60.0)
                for ss in scaled_subs[lo:hi]:
                    g_aff_diffs.append(r - ss)
            if g_aff_diffs and scaled_subs:
                aff_bins = Counter(round(d * 5) / 5.0 for d in g_aff_diffs)
                for pk, _ in aff_bins.most_common(3):
                    inl_b = []
                    for r in ref_times:
                        cs = _nearest_sorted_val(scaled_subs, r - pk)
                        if abs((r - pk) - cs) <= tol:
                            inl_b.append(r - cs)
                    if len(inl_b) >= 3:
                        inl_b.sort()
                        affine_candidates.append((a_rat, round(inl_b[len(inl_b) // 2], 3)))

        if len(win_stats) >= 3 and is_sub_ref:
            for i in range(len(win_stats)):
                for j in range(i + 1, len(win_stats)):
                    t1, off1 = win_stats[i][0], win_stats[i][1]
                    t2, off2 = win_stats[j][0], win_stats[j][1]
                    if (t2 - t1) >= 250.0 and abs(off2 - off1) >= 0.45:
                        a_emp = 1.0 + (off2 - off1) / (t2 - t1)
                        if 0.95 <= a_emp <= 1.05:
                            b_emp = off1 - (a_emp - 1.0) * t1
                            if abs(b_emp) <= 60.0:
                                affine_candidates.append((a_emp, round(b_emp, 3)))

        min_aff_wins = max(2, active_wins - 1)
        min_aff_tight = max(5, int(0.35 * len(ref_times)))
        min_aff_loose = max(nat_inliers + 3, int(0.45 * len(ref_times)), 6)
        seen_aff = set()
        for a_cand, b_cand in affine_candidates:
            if abs(b_cand) > 65.0:
                continue
            key_aff = (round(a_cand, 7), round(b_cand, 3))
            if key_aff in seen_aff:
                continue
            seen_aff.add(key_aff)
            t_inl, l_inl, t_wok, l_wok, _, t_res, l_res, sc = _score_model(
                lambda t, a=a_cand, b=b_cand: a * t + b
            )
            if (
                l_wok >= min_aff_wins
                and t_inl >= min_aff_tight
                and l_inl >= min_aff_loose
                and l_res is not None
                and l_res <= 200.0
                and sc > (best_model["score"] + 12.0)
            ):
                best_model = {
                    "mode": "affine",
                    "a": round(a_cand, 7),
                    "b": b_cand,
                    "segments": [],
                    "tier": 1,
                    "score": sc,
                    "win_ok": l_wok,
                    "active_windows": active_wins,
                    "tight_inliers": t_inl,
                    "post_inliers": l_inl,
                    "median_residual_ms": l_res,
                    "label": "🔄 تمت المزامنة (ضبط FPS + التوقيت)",
                }

        if is_sub_ref and len(win_stats) >= 2 and best_model.get("win_ok", 0) < len(win_stats):
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

                t_inl, l_inl, t_wok, l_wok, _, t_res, l_res, sc = _score_model(_piecewise_fn)
                if (
                    l_wok >= 2
                    and l_wok > best_model.get("win_ok", 0)
                    and l_inl >= max(nat_inliers, best_const_inliers) + 3
                    and sc > (best_model["score"] + 15.0)
                ):
                    best_model = {
                        "mode": "piecewise",
                        "a": 1.0,
                        "b": 0.0,
                        "segments": segments,
                        "tier": 1,
                        "score": sc,
                        "win_ok": l_wok,
                        "active_windows": active_wins,
                        "tight_inliers": t_inl,
                        "post_inliers": l_inl,
                        "median_residual_ms": l_res,
                        "label": "🔄 تمت المزامنة (متعددة الفصول)",
                    }

    return best_model


def _apply_stream_sync_model(srt_text, model):
    """Applies Constant, Affine (FPS a*t+b), or Piecewise shift to all SRT timestamps, then applies fix_arabic_rtl_text."""
    if not model or model.get("mode") in (None, "none", "natural"):
        return fix_arabic_rtl_text(srt_text)

    mode = model.get("mode")
    a = float(model.get("a", 1.0))
    b = float(model.get("b", 0.0))
    segments = model.get("segments") or []

    if mode == "constant" and abs(b) < 0.005:
        return fix_arabic_rtl_text(srt_text)

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
    shifted_text = re.sub(pattern, _shift_match, srt_text)
    return fix_arabic_rtl_text(shifted_text)


def solve_and_apply_stream_sync(sub_paths, ref_data):
    """
    Applies the multi-window sync model (Natural / Constant / FPS Affine / Piecewise)
    + bulletproof Arabic RTL un-reverser across all downloaded stream subtitles.
    """
    if isinstance(sub_paths, str):
        sub_paths = [sub_paths]
    sub_paths = [s for s in sub_paths if s and os.path.exists(s)]
    if not sub_paths:
        return {"synced": False, "reason": "no_subtitles", "details": []}

    details = []
    for sp in sub_paths:
        raw_text = None
        for enc in ("utf-8-sig", "utf-8", "windows-1256", "cp1256", "iso-8859-6"):
            try:
                with open(sp, "r", encoding=enc) as f:
                    raw_text = f.read().replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
                break
            except Exception:
                continue
        if not raw_text:
            details.append({"file": os.path.basename(sp), "path": sp, "mode": "unreadable", "tier": 2, "win_ok": 0})
            continue

        cleaned_text = fix_arabic_rtl_text(raw_text)
        cues = _parse_srt_all_cues_from_text(cleaned_text)
        model = _solve_stream_sync_model(ref_data, cues) if (ref_data and ref_data.get("times")) else {
            "mode": "none", "a": 1.0, "b": 0.0, "tier": 2, "win_ok": 0, "active_windows": 0, "post_inliers": 0, "median_residual_ms": None, "label": ""
        }

        if model.get("tier") in (0, 1):
            final_text = _apply_stream_sync_model(cleaned_text, model)
        else:
            final_text = cleaned_text

        with open(sp, "w", encoding="utf-8-sig", newline="\n") as out_f:
            out_f.write(final_text)

        details.append({
            "file": os.path.basename(sp),
            "path": sp,
            "mode": model.get("mode", "none"),
            "a": model.get("a", 1.0),
            "b": model.get("b", 0.0),
            "tier": model.get("tier", 2),
            "win_ok": model.get("win_ok", 0),
            "active_windows": model.get("active_windows", 0),
            "post_inliers": model.get("post_inliers", 0),
            "median_residual_ms": model.get("median_residual_ms"),
            "label": model.get("label", ""),
        })

    return {
        "synced": bool(ref_data and ref_data.get("times")),
        "source": (ref_data or {}).get("source", "none"),
        "windows_count": len((ref_data or {}).get("windows", [])),
        "ref_cues_count": len((ref_data or {}).get("times", [])),
        "details": details,
    }


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

    base_no_ext, v_ext = os.path.splitext(video_path)
    out_ext = ".mkv" if v_ext.lower() != ".mkv" else v_ext
    cross_drive_tmp_dir = None

    if output_path:
        temp_output = output_path
    else:
        video_size_bytes = os.path.getsize(video_path)
        needed_bytes = video_size_bytes + (2 * 1024 ** 3)  # video + 2 GB safety buffer
        same_drive_free = shutil.disk_usage(os.path.dirname(os.path.abspath(video_path))).free
        if same_drive_free >= needed_bytes:
            temp_output = base_no_ext + ".mux_temp" + out_ext
        else:
            # Cross-Drive Temp Muxing: look for another fixed drive with enough space
            alt_root = None
            if sys.platform == "win32":
                import ctypes
                import string
                kernel32 = ctypes.windll.kernel32
                src_drive = os.path.splitdrive(os.path.abspath(video_path))[0].upper()
                best_free = -1
                for letter in string.ascii_uppercase:
                    root = f"{letter}:\\"
                    if root[:2].upper() == src_drive:
                        continue
                    if os.path.exists(root) and kernel32.GetDriveTypeW(root) == 3:
                        try:
                            u = shutil.disk_usage(root)
                            if u.total > 0 and u.free >= needed_bytes and u.free > best_free:
                                best_free = u.free
                                alt_root = root
                        except Exception:
                            pass
            if alt_root:
                cross_drive_tmp_dir = os.path.join(alt_root, ".torrentx_mux_tmp")
                os.makedirs(cross_drive_tmp_dir, exist_ok=True)
                temp_output = os.path.join(cross_drive_tmp_dir, os.path.basename(base_no_ext) + ".mux_temp" + out_ext)
            else:
                return {
                    "error": True,
                    "insufficient_space": True,
                    "fallback_mode": "companion_srt",
                    "message": "لا توجد مساحة كافية على أي قرص لإنشاء نسخة دمج مؤقتة؛ تم تفعيل خطة الطوارئ لحفظ الترجمة بجانب الفلم."
                }

    def _finalize_mux(tool_name):
        final_target = video_path
        if replace_original and not output_path:
            if v_ext.lower() != ".mkv":
                final_target = base_no_ext + ".mkv"
                if cross_drive_tmp_dir:
                    os.remove(video_path)
                    shutil.move(temp_output, final_target)
                else:
                    shutil.move(temp_output, final_target)
                    try:
                        os.remove(video_path)
                    except Exception:
                        pass
            else:
                if cross_drive_tmp_dir:
                    # Free space on original drive first (temp_output is already verified on other drive)
                    os.remove(video_path)
                    shutil.move(temp_output, video_path)
                else:
                    bak = video_path + ".orig_bak"
                    shutil.move(video_path, bak)
                    shutil.move(temp_output, video_path)
                    if os.path.exists(bak):
                        os.remove(bak)
        else:
            final_target = temp_output

        if cross_drive_tmp_dir and os.path.isdir(cross_drive_tmp_dir):
            try:
                if not os.listdir(cross_drive_tmp_dir):
                    os.rmdir(cross_drive_tmp_dir)
            except Exception:
                pass

        emb_verify = check_embedded_arabic_sub(final_target, timeout=15)
        return {
            "success": True,
            "output_file": final_target,
            "tool": tool_name,
            "cross_drive_mux": bool(cross_drive_tmp_dir),
            "tracks_muxed": len(normalized_subs),
            "verified_embedded_arabic": emb_verify.get("has_embedded_arabic", False),
            "verified_arabic_tracks": emb_verify.get("arabic_tracks", [])
        }

    mkvmerge_bin = shutil.which("mkvmerge") or (r"C:\Program Files\MKVToolNix\mkvmerge.exe" if os.path.exists(r"C:\Program Files\MKVToolNix\mkvmerge.exe") else None)
    if mkvmerge_bin:
        cmd = [mkvmerge_bin, "-o", temp_output, video_path]
        for idx, (sp, lbl) in enumerate(normalized_subs):
            default_flag = "yes" if idx == 0 else "no"
            cmd.extend(["--language", "0:ara", "--track-name", f"0:{lbl}", "--default-track-flag", f"0:{default_flag}", sp])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
            if res.returncode in [0, 1] and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                return _finalize_mux("mkvmerge")
        except Exception:
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass

    # Fallback: ffmpeg lossless remux into MKV
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    existing_sub_count = 0
    try:
        pr = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace"
        )
        existing_sub_count = len([ln for ln in pr.stdout.splitlines() if ln.strip()])
    except Exception:
        existing_sub_count = 0

    ff_cmd = [ffmpeg_bin, "-y", "-i", video_path]
    for sp, _ in normalized_subs:
        ff_cmd.extend(["-i", sp])
    ff_cmd.extend(["-map", "0"])
    for idx in range(len(normalized_subs)):
        ff_cmd.extend(["-map", f"{idx + 1}:0"])
    ff_cmd.extend(["-c", "copy"])
    for idx, (_, lbl) in enumerate(normalized_subs):
        s_idx = existing_sub_count + idx
        ff_cmd.extend([
            f"-metadata:s:s:{s_idx}", "language=ara",
            f"-metadata:s:s:{s_idx}", f"title={lbl}",
            f"-disposition:s:{s_idx}", "default" if idx == 0 else "0"
        ])
    ff_cmd.append(temp_output)
    try:
        res = subprocess.run(ff_cmd, capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        if res.returncode == 0 and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0.9 * os.path.getsize(video_path):
            return _finalize_mux("ffmpeg")
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except Exception:
                pass
        return {"error": True, "message": f"ffmpeg muxing failed (code {res.returncode}): {(res.stderr or '')[-400:]}"}
    except Exception as e:
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except Exception:
                pass
        return {"error": True, "message": f"MKVMerge & ffmpeg muxing failed: {e}"}


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
            sync_mode = sync_res.get("sync_mode", "")
            if sync_mode == "affine" or "affine" in str(tool_u):
                label_synced = f"{label} (Synced FPS)"
            elif tool_u != "already_matched" and abs(shift_s) > 0.35:
                label_synced = f"{label} (Synced {shift_s:+.1f}s)"
            else:
                label_synced = label
            synced_results.append({
                "file_path": sub_path,
                "track_label": label_synced,
                "sync_tool": tool_u,
                "shift_seconds": shift_s,
                "verified_matched": sync_res.get("verified_matched", tool_u == "already_matched"),
                "post_offset_seconds": sync_res.get("post_offset_seconds", sync_res.get("shift_seconds", 0.0)),
                "post_drift_seconds": sync_res.get("post_drift_seconds", sync_res.get("drift_seconds", 0.0))
            })
        else:
            synced_results.append({
                "file_path": sub_path,
                "track_label": label,
                "sync_tool": "unchanged",
                "shift_seconds": 0.0,
                "verified_matched": False,
                "post_offset_seconds": None,
                "post_drift_seconds": None
            })

    if ref_sub and os.path.exists(ref_sub):
        try:
            os.remove(ref_sub)
        except Exception:
            pass

    # Verified-first track ordering so default MKV track (#1) is always verified in-sync
    if synced_results:
        synced_results.sort(key=lambda r: 0 if r.get("verified_matched") else 1)

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
    cleaned_external_subs = False
    fallback_companion_sub = None
    if mux_into_mkv and synced_results:
        mux_res = mux_subtitle_into_video(video_path, synced_results, replace_original=True)
        if mux_res and mux_res.get("fallback_mode") == "companion_srt":
            # Emergency fallback: save synced .ar.srt right next to the video (<200 KB)
            v_dir = os.path.dirname(os.path.abspath(video_path))
            v_stem = os.path.splitext(os.path.basename(video_path))[0]
            best_synced = synced_results[0]["file_path"]
            companion_target = os.path.join(v_dir, f"{v_stem}.ar.srt")
            try:
                shutil.copy2(best_synced, companion_target)
                fallback_companion_sub = companion_target
            except Exception:
                pass
        if (mux_res and mux_res.get("success")) or fallback_companion_sub:
            v_stem = os.path.splitext(os.path.basename(video_path))[0]
            sub_dirs_to_check = set()
            for r in synced_results:
                sp = r.get("file_path")
                if sp:
                    sub_dirs_to_check.add(os.path.dirname(sp))
                    try:
                        if os.path.exists(sp):
                            os.remove(sp)
                    except Exception:
                        pass
            for sd in sub_dirs_to_check:
                if not sd:
                    continue
                for extra_name in [f"{v_stem}.srt", f"{v_stem}.ar.srt", f".{os.path.basename(video_path)}.manifest.json"]:
                    ep = os.path.join(sd, extra_name)
                    if fallback_companion_sub and os.path.abspath(ep) == os.path.abspath(fallback_companion_sub):
                        continue
                    try:
                        if os.path.exists(ep):
                            os.remove(ep)
                    except Exception:
                        pass
                if os.path.basename(sd).lower() == "subtitles" and os.path.isdir(sd):
                    try:
                        if not os.listdir(sd):
                            os.rmdir(sd)
                    except Exception:
                        pass
            cleaned_external_subs = True

    all_verified = bool(synced_results) and all(r.get("verified_matched", False) for r in synced_results)
    return {
        "success": True,
        "video_path": (mux_res or {}).get("output_file") or video_path,
        "all_verified_synced": all_verified,
        "cleaned_external_subs": cleaned_external_subs,
        "fallback_companion_sub": fallback_companion_sub,
        "synced_subtitles": synced_results,
        "mux_result": mux_res
    }


def watch_and_sync_video(target_video_path, manifest_or_sub_paths, timeout_minutes=180):
    """
    Waits in the background for Aria2 to finish downloading target_video_path (no .aria2 file + stable size),
    then automatically runs alass timing synchronization on all downloaded subtitles, verifies sync, and muxes them!
    """
    max_checks = (timeout_minutes * 60) // 6
    aria2_ctl = f"{target_video_path}.aria2"
    log_path = os.path.join(os.path.dirname(target_video_path) or ".", f".{os.path.basename(target_video_path)}.sync.log")

    def _log(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    _log(f"Watcher started for: {target_video_path}")
    prev_size = -1
    stable_hits = 0

    for _ in range(max_checks):
        time.sleep(6)
        if os.path.exists(target_video_path) and not os.path.exists(aria2_ctl):
            try:
                cur_size = os.path.getsize(target_video_path)
                if cur_size > 30 * 1024 * 1024:
                    if cur_size == prev_size:
                        stable_hits += 1
                    else:
                        stable_hits = 0
                    prev_size = cur_size
                    if stable_hits < 1:
                        continue

                    _log(f"Download complete ({cur_size} bytes). Starting sync_all_and_mux...")
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

                    res = sync_all_and_mux(target_video_path, sub_items, mux_into_mkv=True)
                    if (res.get("mux_result") or {}).get("success") or res.get("fallback_companion_sub"):
                        try:
                            if os.path.exists(log_path):
                                os.remove(log_path)
                        except Exception:
                            pass
                    else:
                        _log(f"sync_all_and_mux finished: {json.dumps(res, ensure_ascii=False)}")
                    return res
            except OSError as e:
                _log(f"OSError while checking file: {e}")
                continue
    _log("Timeout waiting for video download completion")
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
