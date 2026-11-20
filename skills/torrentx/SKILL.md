---
name: torrentx
description: High-speed, intelligent movie and media download suite with TorBox/Real-Debrid instant cloud caching, 4-Tier Remux/WEB-DL quality selection (strictly no upscales), Movie-First execution pipeline, embedded Arabic subtitle inspection, exclusive SubSource & SubDL multi-subtitle fetching (all official platforms + top translators with content-level deduplication, zero OpenSubtitles), and automatic alass timing synchronization + MKV muxing.
---

# 🎬 TorrentX High-Speed & Smart Movie Protocol

Whenever the user asks to download a movie (e.g., `حمل لي فلم ...`, `نزل فلم ...`), execute the **Fast Movie-First Protocol** immediately using `debrid_manager.py` and `subtitle_manager.py`.

---

## ⚡ 1. Speed & Execution Order (الأولوية للسرعة: تحميل الفلم أولاً ثم الترجمات)

To save maximum time, **ALWAYS start the movie download FIRST**, and then inspect/fetch subtitles while the movie is already downloading in the background:

### Single-Command Fast Pipeline (Recommended):
```powershell
python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" fast-movie "<Movie Title or IMDb ID>" "[Optional Output Dir]" "[Optional Year]"
```

What `fast-movie` does automatically in **~2–4 seconds**:
1. **Instant Movie Identification & TorBox Cache Lookup (~1.0s):**
   - Resolves the exact `imdb_id`, title, and release year via TMDB/IMDb.
   - Queries **TorBox** (`[TB+]`) cached streams directly for instant cloud speeds.
2. **Starts Movie Download FIRST (~1.8s):**
   - Selects the #1 best release using the **4-Tier Quality & Anti-Upscale Engine** and immediately dispatches the 16-connection **Aria2** download before touching subtitles.
3. **Embedded Arabic Subtitle Inspection:**
   - Probes the cloud stream header via `ffprobe` (`check_embedded_arabic_sub`) while Aria2 downloads.
   - If the movie **already contains an embedded Arabic subtitle track** (`ar` / `ara` / `arabic`), it reports the track and skips unnecessary external subtitle downloads.
4. **Smart Subtitle Fetching (Strictly `SubSource` & `SubDL` ONLY — NEVER OpenSubtitles):**
   - If no embedded Arabic subtitle exists, queries **only `SubSource` and `SubDL` APIs in parallel**.
   - **Priority 1 (All Official Platforms):** Downloads **ALL distinct official platform subtitles** available (`Netflix`, `OSN+`, `iTunes / Apple TV+`, `Amazon Prime`, `Disney+`, `Max`, `Shahid`, `BluRay-Retail`).
   - **Priority 2 (Translators / Teams):** Downloads top distinct individual translator or translation team subtitles.
   - **Zero-Duplicate Content Fingerprinting:** Compares the actual normalized Arabic dialogue lines of every downloaded `.srt` file so the same translation is **never** saved twice (even if uploaded under different names across SubSource and SubDL).
   - **Zero OpenSubtitles Tolerance:** Rejects any OpenSubtitles/machine-translated entries and strips any promotional watermarks.
   - **Dedicated Subtitles Folder:** All external `.srt` subtitles are saved cleanly inside `<BestDrive>\Movies\Subtitles` (or your configured `subtitles_dir`).
5. **Best Drive Selection & Folder Policy (اختيار أفضل قرص + سياسة المجلدات):**
   - Automatically scans all fixed drives and picks the drive with the **most free space** (e.g., `D:\Movies` or `D:\Series` instead of filling `C:\`).
   - **Always report to the user** which drive was chosen (with its free space) and the exact output folder/file path (`selected_drive`, `drive_free_gb`, `output_dir`, `target_video_path`).
   - **Movies:** Saved directly in `<BestDrive>\Movies\` (**no** per-movie subfolder).
   - **Single Episode:** Saved directly in `<BestDrive>\Series\` (**no** subfolder).
   - **Full Series / Season Pack:** Saved inside `<BestDrive>\Series\<Title>\Season XX\` (**creates** a dedicated folder).
6. **Automatic `alass` Timing Sync, Verification & MKV Muxing:**
   - Spawns `subtitle_manager.py watch-and-sync` detached via `Win32_Process` so it survives session exit.
   - As soon as the movie finishes downloading, it extracts the ground-truth embedded reference subtitle (`.ref_eng.srt` from text tracks OR Blu-ray PGS `.sup`), runs **`alass`** to fix any timing offset/drift, **verifies** post-sync alignment (`verified_matched`), applies UTF-8 BOM + Arabic RTL punctuation fixes (`fix_arabic_rtl_file`), and losslessly muxes all tracks into the MKV container via `mkvmerge` or **`ffmpeg` fallback**, verifying the embedded Arabic tracks with `ffprobe`.

---

## 👑 2. Strict 4-Tier Movie Quality Ladder (بروتوكول جودة الأفلام الصارم)

1. **Strict Anti-Upscale Rule (منع الأبسكيل نهائياً):**
   - Any release tagged with `upscale`, `ai`, `topaz`, `visionx`, `4320p`, `8k` is strictly rejected.
   - **Unlabelled 4K Upscale Protection:** If a movie has `1080p BluRay Remux` releases, zero `4K BluRay Remux` releases, and zero legitimate `4K WEB-DL` releases from known streaming platforms, any lone 2160p encode is automatically rejected as a fake upscale in favor of the `1080p BluRay Remux`.
2. **Tier 1 — `4K UHD BluRay Remux`:**
   - First choice whenever the movie has a true 4K UHD Blu-ray disc (`2160p Remux`, untouched video, lossless `TrueHD Atmos` / `DTS-HD MA` audio, `DV` / `HDR`).
3. **Tier 2 — `1080p BluRay Remux`:**
   - If the movie does not have a true 4K release, choose the untouched **`1080p BluRay Remux`** (`AVC`/`VC-1` untouched disc, lossless `DTS-HD MA` / `TrueHD` audio).
4. **Tier 3 — `4K WEB-DL (Untouched / High-Bitrate)`:**
   - If the movie has no Blu-ray disc at all (e.g. streaming exclusive or pre-disc digital release), choose the highest-bitrate untouched **`4K WEB-DL`** (`ATVP`, `AMZN`, `NF`, `MA`, `DSNP`, `MAX`, `DV`/`HDR`, `Atmos`/`DDP5.1`), avoiding compressed `WEBRip`, `AV1`, `YTS`, or `PSA` encodes.
5. **Tier 4 — `1080p WEB-DL (Untouched / High-Bitrate)`:**
   - Used only if the movie has neither a Blu-ray Remux nor a 4K WEB-DL.

---

## 🛠️ 3. Direct CLI Commands Reference

- **Fast Movie Download (Movie First -> Embedded Check -> SubSource/SubDL -> Auto alass Sync):**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" fast-movie "<Title or IMDb ID>" "[Output Dir]" "[Year]"
  ```
- **Search & Rank Movie Releases Only (No Download):**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" search-movie "<Title or IMDb ID>" "[Year]"
  ```
- **Smart Fetch Subtitles Only (SubSource + SubDL, All Platforms + Translators, Deduplicated):**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" smart-fetch "<Title>" "<IMDb ID>" "<Output Dir>" "<Video Filename>" "[bluray|web]"
  ```
- **Check if Video/Stream Has Embedded Arabic Subtitle:**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" check-embedded "<Video Path or Direct HTTP URL>"
  ```
- **Synchronize Subtitle Timing with `alass` Immediately:**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" sync "<Video Path>" "<Subtitle Path>"
  ```
- **Sync All Subtitles & Mux into MKV Container:**
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" sync-all "<Video Path>" "<Sub1.srt|Sub2.srt|...>"
  ```

---

## 🔬 4. Deep MCP Inspection Tools (When Needed)

For rare P2P torrents not indexed on Torrentio or when deep bitstream verification is requested:
- `mcp_torrentx_search`: Fast release-name search across torrent trackers.
- `mcp_torrentx_find` / `mcp_torrentx_inspect_media` / `mcp_torrentx_inspect_torrent`: Deep P2P stream inspection.
