---
name: torrentx
description: Ultra-fast 1-step movie downloader via debrid_manager.py fast-movie (TorBox/Debrid instant cache, 4-tier anti-upscale Remux/WEB-DL selection, auto best-drive selection, Aria2, embedded/SubSource/SubDL Arabic subtitles, auto alass sync & MKV muxing).
---

# TorrentX — 1-Step Fast Movie Execution Protocol

> [!CAUTION]
> **ABSOLUTE 1-TOOL-CALL CONTRACT (ALL MODELS — Gemini 3.8 / Pro / Flash / Claude / Hydrogen):**
> 1. **Tool Call Budget = 1:** Fulfill every movie download request in **EXACTLY ONE** shell command.
> 2. **ZERO PRE-CHECKS:** Do **NOT** read any `.py`, `.json`, or `.md` files, check paths, check disk space, check tool versions, or run `--help`. Everything is pre-configured and automated inside `fast-movie`.
> 3. **ZERO POST-CHECKS (HARD STOP):** Once the command returns JSON, immediately print the response using the Output Template below and **END YOUR TURN**. Do **NOT** inspect folders, check `aria2c`, or read logs.

---

## COMMANDS (COPY & RUN IMMEDIATELY)

### Case A — Single Movie (1 shell call)
Include the year inside the title string if known (e.g. `"Interstellar 2014"`). Omit the output directory unless the user explicitly specified a custom path.

```powershell
python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" fast-movie "<Title or IMDb ID>"
```

*(Only if the user explicitly requested a custom folder: append `"<Custom Output Dir>"` as the 2nd argument.)*

### Case B — Multiple Movies (1 single parallel shell call)
Pass **all** requested movie titles as arguments at the end of this exact one-liner. It launches all `fast-movie` processes simultaneously in one turn:

```powershell
python -c "import os,sys,subprocess,concurrent.futures as cf; s=os.path.expanduser('~/.gemini/config/skills/torrentx/debrid_manager.py'); args=sys.argv[1:]; f=lambda m: (lambda r: r.stdout or r.stderr)(subprocess.run([sys.executable,s,'fast-movie',m],capture_output=True,text=True,encoding='utf-8',errors='replace')); [print(res,flush=True) for res in cf.ThreadPoolExecutor(max(1,len(args))).map(f,args)]" "Movie 1" "Movie 2" "Movie 3"
```

---

## OUTPUT TEMPLATE (PRINT FROM JSON & STOP)

Map values directly from the returned JSON (`movie`, `selected_release`, `download`, `subtitles`):

### For Multiple Movies (Compact Table):

| الفلم | النسخة والجودة | الحجم | القرص والمسار (المساحة الحرة) | الترجمة والمزامنة |
| :--- | :--- | :--- | :--- | :--- |
| `title (year)` | `quality_tier` | `size_gb` | `selected_drive` (`drive_free_gb` GB) — `output_dir` | مدمجة (`embedded_arabic_found`) أو عدد المنصات/المترجمين + مزامنة `alass` تلقائية |

List exact `target_video_path` lines briefly if helpful, then stop.

### For a Single Movie:
- **الفلم:** `movie.title` (`movie.year`)
- **النسخة:** `selected_release.quality_tier` — `selected_release.size_gb` (`selected_release.filename`)
- **القرص المختار:** `download.selected_drive` (مساحة حرة: `download.drive_free_gb` GB)
- **مسار الحفظ:** `download.target_video_path`
- **الترجمة:** إن كانت `subtitles.has_embedded_arabic` = `true` اذكر وجود ترجمة عربية مدمجة رسمياً؛ وإلا اذكر عدد ترجمات المنصات (`platforms_count`) والمترجمين (`translators_count`) وحالة المزامنة التلقائية (`alass_auto_sync_scheduled`).

If any movie returns `"error": true`, show its `message` directly without retrying automatically unless the title was misspelled.

---

## ON-DEMAND COMMANDS (ONLY IF EXPLICITLY REQUESTED — NEVER DURING DOWNLOADS)

| User Intent | Command |
| :--- | :--- |
| Search only (no download) | `python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" search-movie "<Title>"` |
| Subtitles only | `python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" smart-fetch "<Title>" "<IMDb ID>" "<OutDir>" "<VideoFilename>"` |
| Sync existing subtitle | `python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" sync "<VideoPath>" "<SubPath>"` |
| Stream movie without downloading | `python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" stream-movie "<Title>" [mpc\|potplayer\|mpv\|vlc]` |
| Stream / Play | `python "$env:USERPROFILE\.gemini\config\skills\torrentx\debrid_manager.py" play "<URL or Path>" "[SubPath]"` |
| Rare P2P inspection (explicit request only) | `mcp_torrentx_search` / `mcp_torrentx_find` / `mcp_torrentx_inspect_media` |
