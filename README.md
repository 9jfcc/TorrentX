# TorrentX ⚡🎬

**المحرك الذكي للبحث والتحميل السحابي فائق السرعة للأفلام، المسلسلات، الأنمي، والألعاب**  
*The AI-Powered Media & Game Skill + MCP Server for Antigravity, Claude Code, and Codex.*

---

## 🌟 المميزات الرئيسية (Core Features)

- **⚡ دعم جميع خدمات الديبرد (Multi-Debrid + Aria2 + qBittorrent):**
  - يدعم اختيار أي خدمة ديبرد تفضلها عبر `debrid.json`: **TorBox**، **Real-Debrid**، **AllDebrid**، **Premiumize**، **Debrid-Link**، **Offcloud**، **Put.io** — أو التحميل المجاني عبر **qBittorrent (P2P)** بدون اشتراك ديبرد.
  - فحص الكاش السحابي الفوري وتنزيل مباشر بأقصى سرعة عبر `aria2c` مع حماية ذكية من أخطاء `HTTP 429` وفك توجيه الروابط تلقائياً.
  - **أمان صارم ضد الملفات الخبيثة:** حظر تلقائي لملفات `.exe` والسكربتات والأرشيفات المشبوهة عند تحميل الأفلام والمسلسلات.
- **🎬 اختيار جودة الأفلام بدون ترقية وهمية (4-Tier Anti-Upscale):**
  - أولوية ذكية للنسخ الأصلية غير المضغوطة: `4K Remux` ← `1080p Remux` ← `4K WEB-DL` ← `1080p WEB-DL`.
  - استبعاد نسخ الكاميرا والترقيات المزيفة (Fake Upscales) والـ AV1/الدبلجات غير المطلوبة.
- **💬 محرك الترجمة العربية الذكي (`SubSource` + `SubDL`):**
  - جلب الترجمات العربية حصرياً من **SubSource** و **SubDL** (بدون OpenSubtitles أو الترجمات الآلية الضعيفة).
  - تصنيف ذكي: **المنصات الرسمية** (Netflix, Prime, Apple TV+, Disney+, Shahid, OSN...) ثم **المترجمون الموثوقون**، مع إزالة التكرار على مستوى الحوارات وتنظيف الإعلانات.
  - مزامنة تلقائية دقيقة عبر `alass` (تدعم الصوت ومسارات PGS `.sup` المدمجة) ودمج في حاوية `.mkv`، مع حفظ الترجمات في مجلد مخصص ومنظم.
- **🎮 أمان الألعاب (Strict Megathread Policy):**
  - حصر البحث والتحميل في المصادر الموثوقة المعتمدة في `r/PiratedGames` و `r/Piracy` (`FitGirl Repacks`, `DODI Repacks`, `GOG-Games`, `Online-Fix`, `RuTracker`, `SteamRIP`).
  - حظر تام للمواقع والرافعين غير الموثوقين ومواقع مجموعات المشهد المزيفة.
- **🔌 متوافق مع بروتوكول MCP وإضافات Stremio / Harbor / Nuvio:**
  - يعمل كـ **MCP Server** مع **Google Antigravity** و **Claude Code** و **Codex**.
  - ويتضمن إضافة مستقلة (`stremio_addon.py`) تعمل بدون AI لبرامج **Harbor** و **Stremio** و **Nuvio** مع صفحة إعدادات لاختيار خدمة الديبرد أو P2P، السماح/منع الأبسكيل، وإضافة مفاتيح الترجمة (`SubDL`, `SubSource`, `Wyzie`).

> **⚠️ ملاحظة لمستخدمي برنامج Harbor:** يفعل Harbor افتراضياً خيار `صارم` في فلتر أمان البث مما قد يحجب الأفلام الحديثة خلف زر "بحث أوسع". لتجنب ذلك، ادخل: **الإعدادات ← المصادر والمكتبة ← تفضيلات المصادر ← فلتر أمان البث** واختر **متوازن** (أو **إيقاف**) بدلاً من **صارم**.

---

## ⚡ تشغيل إضافة Stremio / Harbor بنقرة واحدة (One-Click Local Launcher)

لأي مستخدم يريد تشغيل الإضافة على جهازه وفتح صفحة الإعدادات فوراً في المتصفح:

- **على ويندوز:** فقط اضغط مرتين (Double Click) على ملف:
  👉 **`TorrentX-Addon.bat`**
- **على لينكس / ماك:**
  ```bash
  bash run_addon.sh
  ```
- **أو عبر بايثون مباشرة:**
  ```bash
  python skills/torrentx/stremio_addon.py
  ```
سيقوم تلقائياً بتشغيل السيرفر في الخلفية وفتح صفحة الإعداد فوراً في متصفحك الافتراضي:
👉 **`http://127.0.0.1:7123/configure`**

---

## 🚀 التثبيت السريع (Quick Installation)

### 1. عبر PowerShell (ويندوز)
```powershell
irm https://raw.githubusercontent.com/9jfcc/TorrentX/main/install.ps1 | iex
```
سيقوم تلقائياً بتنزيل ملفات المهارة إلى `~/.gemini/config/skills/torrentx` وتثبيت `Aria2` إذا لم يكن موجوداً.

### 2. اختيار خدمة الديبرد ومفاتيح الترجمة
أنشئ ملف الإعدادات `~/.gemini/config/debrid.json` واختر خدمتك في `default_service` (`torbox` أو `realdebrid` أو `alldebrid` أو `premiumize` أو `debridlink` أو `offcloud` أو `putio` أو `p2p` بدون ديبرد):
```json
{
  "default_service": "torbox",
  "services": {
    "torbox": { "api_key": "YOUR_API_KEY" },
    "realdebrid": { "api_key": "" },
    "alldebrid": { "api_key": "" },
    "premiumize": { "api_key": "" },
    "debridlink": { "api_key": "" }
  }
}
```
وملف `~/.gemini/config/subtitles.json` لمفاتيح **SubDL** و **SubSource** (اختياري لزيادة عدد الطلبات).

### 3. استخدام المهارة والـ MCP في Claude Code / Codex
- **Claude Code:**
  ```powershell
  claude mcp add torrentx -- "path\to\torrentx.exe" mcp
  ```
  وانسخ مجلد `skills/torrentx` إلى `~/.claude/skills/torrentx`.
- **Codex:**
  أضف في `~/.codex/config.toml`:
  ```toml
  [mcp_servers.torrentx]
  command = "path\\to\\torrentx.exe"
  args = ["mcp"]
  ```
  وانسخ مجلد `skills/torrentx` إلى `~/.codex/skills/torrentx`.

---

## 📄 الترخيص (License)
هذا المشروع مرخص تحت رخصة [MIT License](https://github.com/9jfcc/TorrentX/blob/main/LICENSE).
