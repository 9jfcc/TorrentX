---
name: torrentx
description: Find, stream, and download movies, series, anime, and episodes with instant Real-Debrid cloud caching, high-speed Aria2 downloads, Arabic subtitles, and alass audio synchronization.
---

# 🎬 TorrentX Autonomous Entertainment Skill

## ⚡ THE SINGLE-COMMAND PROTOCOL (القاعدة الصارمة لتنفيذ الأوامر فوراً)

**CRITICAL INSTRUCTION FOR ALL AI ASSISTANTS & AGENTS:**
When the user asks in natural language to download, stream without downloading, search, or play any movie, series, episode, or anime:
> **YOU MUST PERFORM EXACTLY ONE TOOL CALL:** Run the single command below via PowerShell.
> **DO NOT write text or pretend you downloaded it without actually executing the tool!**

```powershell
python "%USERPROFILE%\.gemini\config\skills\torrentx\ai_assistant.py" "<user prompt exactly>"
```

### 🛑 CRITICAL WARNING AGAINST HALLUCINATION (تحذير صارم ضد الردود الوهمية):
- **NEVER** output a message saying "أبشر تم تجهيز طلبك وبدء التحميل" without actually invoking `run_command`!
- You MUST call `run_command` with the exact python command above.
- The python script will perform the real download in 2 seconds and print the verified status.
- Return the EXACT output printed by the script to the user.

### 🚫 STRICT PROHIBITIONS (ممنوع منعاً باتاً):
1. **DO NOT** inspect or read `ai_assistant.py`, `debrid_manager.py`, or any `.py` files.
2. **DO NOT** manually test `aria2c`, check background processes, or verify configs.
3. **DO NOT** manually query Torrentio, Real-Debrid APIs, or scrape subtitle sites.
4. **DO NOT** write scratch scripts, test curl commands, or run step-by-step loops.
5. **DO NOT** call MCP peer-to-peer probing tools (`find`, `inspect_torrent`), as P2P takes minutes and times out.

---

### 🌟 Why this SINGLE command handles EVERYTHING in 1.5 seconds:
`ai_assistant.py` is the unified master engine. In a single execution, it autonomously performs:
1. **Natural Language Understanding:** Extracts title, season, and episode with 100% deterministic accuracy.
2. **Quality & Codec Scoring:** Selects the highest quality release (Remux / 4K / 1080p untouched), excludes foreign audio dubs, and avoids low-quality AV1 unless requested.
3. **Instant Cloud Streaming vs Download:**
   - **Stream Request** (`شغل`, `بدون تحميل`, `stream`, `بث مباشر`): Resolves the direct Real-Debrid HTTPS link and opens **MPC-HC** immediately with Arabic subtitles loaded!
   - **Download Request** (`حمل`, `نزله`, `تحميل`): Pulls the Real-Debrid cloud cache and launches 16-connection **Aria2** download directly into `%USERPROFILE%\Downloads`.
4. **Arabic Subtitles & Audio Sync:** Fetches matching Arabic subtitles, names them identically to the video, and triggers `alass` waveform audio synchronization.
5. **Report Generation:** Generates a structured, beautiful Arabic Markdown report. **Simply return its stdout output to the user!**

---

## 🛠️ Reference CLI Utilities (For Specific Admin Tasks Only)

The following commands are ONLY for explicit user administrative requests:

### 1. Media Player & Configuration Wizard
- Launch Dark-Mode Setup Wizard:
  ```powershell
  python "%USERPROFILE%\.gemini\config\skills\torrentx\setup_wizard.py"
  ```
- Preferred Player: Configured in `~/.gemini/config/debrid.json` (Default: `mpc` for MPC-HC).

### 2. Multi-Bot Remote Manager (Telegram & Discord)
- Run bots concurrently in background (~0% CPU/RAM):
  ```powershell
  python "%USERPROFILE%\.gemini\config\skills\torrentx\run_all_bots.py" --silent
  ```

### 3. Weekly Episode Tracking (Series Monitor)
- Add a show to automated tracking:
  ```powershell
  python "%USERPROFILE%\.gemini\config\skills\torrentx\series_monitor.py" add "<title>" <season> <last_ep>
  ```
- Check tracked shows:
  ```powershell
  python "%USERPROFILE%\.gemini\config\skills\torrentx\series_monitor.py" check
  ```

### 4. Audio Subtitle Synchronization (alass)
- Manually sync subtitle to video:
  ```powershell
  python "%USERPROFILE%\.gemini\config\skills\torrentx\subtitle_manager.py" sync "<video_path>" "<subtitle_path>"
  ```
