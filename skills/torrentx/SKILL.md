---
name: torrentx
description: Find, stream, and download movies, series, anime, and episodes with instant Real-Debrid cloud caching, high-speed Aria2 downloads, Arabic subtitles, alass audio synchronization, and deep MCP media inspection.
---

# 🎬 TorrentX Autonomous Entertainment Skill

TorrentX provides a comprehensive, dual-tier entertainment and torrent automation architecture designed for high speed, reliability, and precision.

---

## ⚡ Tier 1: Fast Autonomous Assistant Pipeline

For user requests in natural language to **download**, **stream without downloading**, **search only**, or **play** any movie, series, episode, or anime:

Execute the master assistant CLI via PowerShell:

```powershell
python "$env:USERPROFILE\.gemini\config\skills\torrentx\ai_assistant.py" '<user prompt exactly>'
```

### 🌟 Key Capabilities of the Master Assistant:
1. **Natural Language Understanding & Arabic Ordinals:** Extracts franchise titles (e.g., `Fate/stay night`, `Fate/strange Fake`), season numbers, and Arabic ordinal numbers (`الأولى` to `العاشرة` to `العشرون`) with 100% precision.
2. **Search-Only & Intent Guarding:** Phrases like `ابحث فقط`, `بدون تحميل`, or `لا تحمل` automatically invoke Search-Only mode, returning a structured Markdown table of top available releases with cache status, without initiating downloads.
3. **Strict Preference & Quality Matching:**
   - When the user asks for `Remux` or `4K`, matching candidates are enforced as hard acceptance criteria.
   - If the exact requested release is not cached in Real-Debrid, the user is transparently notified and offered the choice between P2P qBittorrent download for their exact version vs. instant cached alternatives.
4. **Instant Cloud Streaming vs. 16-Connection Downloads:**
   - **Stream (`شغل`, `بث`, `stream`):** Unrestricts the Real-Debrid link and launches **MPC-HC** immediately with auto-loaded Arabic subtitles.
   - **Download (`حمل`, `نزله`, `تحميل`):** Dispatches high-speed 16-connection **Aria2** download directly into `Downloads`.
5. **Arabic Subtitles & Waveform Sync:** Fetches matching Arabic subtitles, names them identically to the video file, and runs detached background audio synchronization (`alass` / `ffsubsync`).

---

## 🔬 Tier 2: Deep MCP Inspection & Verification Tools

Whenever deep inspection, bitrate analysis, track verification, or P2P torrent exploration is required, agents can invoke the native TorrentX MCP tools:

- `mcp_torrentx_inspect_media`: Inspect local or remote media streams, extracting true video codecs, bitrates, audio channel layouts, and embedded subtitle tracks.
- `mcp_torrentx_inspect_torrent`: Probe torrent metadata, file lists, piece distributions, and swarm health before downloading.
- `mcp_torrentx_find` / `mcp_torrentx_search`: Query decentralized DHT and torrent trackers directly for rare or unindexed releases.

---

## 🛠️ Management & Bot Administration

### 1. Secure Remote Bots (Telegram & Discord)
TorrentX includes zero-idle-cost Telegram and Discord bots with PIN pairing:
- Run all bots in the background:
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\run_all_bots.py" --silent
  ```
- **Security:** Each bot generates a 6-digit pairing PIN on first startup. Users authenticate via `/pair <PIN>` or `!pair <PIN>` to gain control over their personal PC.

### 2. Configuration & Player Setup Wizard
- Launch the Dark-Mode Setup Wizard:
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\setup_wizard.py"
  ```

### 3. Weekly Episode Tracking (Series Monitor)
- Track a new series:
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\series_monitor.py" add "<title>" <season> <last_ep>
  ```
- Check tracked series:
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\series_monitor.py" check
  ```

### 4. Audio Subtitle Synchronization (alass)
- Manually align subtitle to video:
  ```powershell
  python "$env:USERPROFILE\.gemini\config\skills\torrentx\subtitle_manager.py" sync "<video_path>" "<subtitle_path>"
  ```
