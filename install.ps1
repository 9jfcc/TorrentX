<#
.SYNOPSIS
    TorrentX One-Line Installer for Windows & Antigravity
.DESCRIPTION
    Installs the TorrentX skill, configures Aria2, and sets up Real-Debrid with zero hassle.
#>

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "TorrentX Installer"

Write-Host @"
===================================================================
     _____                            _   __   __
    |_   _|                          | |  \ \ / /
      | | ___  _ __ _ __ ___ _ __   | |_  \ V / 
      | |/ _ \| '__| '__/ _ \ '_ \  | __|  > <  
      | | (_) | |  | | |  __/ | | | | |_  / . \ 
      \_/\___/|_|  |_|  \___|_| |_|  \__|/_/ \_\
      
   ⚡ المحرك الذكي للبحث، البث المباشر، والتحميل السحابي
   AI-Powered Media Engine & Antigravity Skill
===================================================================
"@ -ForegroundColor Cyan

$targetDir = Join-Path $env:USERPROFILE ".gemini\config\skills\torrentx"
Write-Host "`n[*] جاري تجهيز مجلد المهارة في: $targetDir" -ForegroundColor Yellow

if (!(Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

# Download latest repo zip from GitHub
$zipUrl = "https://github.com/9jfcc/TorrentX/archive/refs/heads/main.zip"
$tempZip = Join-Path $env:TEMP "TorrentX-main.zip"
$tempExtract = Join-Path $env:TEMP "TorrentX-Extract"

Write-Host "[*] جاري تحميل أحدث ملفات TorrentX من GitHub..." -ForegroundColor Yellow
Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip -UseBasicParsing

Write-Host "[*] جاري فك وتثبيت المهارة..." -ForegroundColor Yellow
if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force

$sourceSkills = Join-Path $tempExtract "TorrentX-main\skills\torrentx"
if (Test-Path $sourceSkills) {
    Copy-Item -Path "$sourceSkills\*" -Destination $targetDir -Recurse -Force
} else {
    Write-Host "[!] تحذير: تعذر العثور على مجلد المهارة داخل الأرشيف." -ForegroundColor Red
}

# Clean temp files
Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue

# Verify Aria2
Write-Host "[*] جاري التحقق من أداة التحميل السريع Aria2..." -ForegroundColor Yellow
$ariaPath = (Get-Command aria2c -ErrorAction SilentlyContinue)
if (-not $ariaPath) {
    Write-Host "[*] أداة Aria2 غير مثبتة. جاري تثبيتها تلقائياً عبر Winget..." -ForegroundColor Cyan
    try {
        winget install aria2.aria2 --source winget --silent --accept-source-agreements --accept-package-agreements
        Write-Host "[+] تم تثبيت Aria2 بنجاح!" -ForegroundColor Green
    } catch {
        Write-Host "[!] تعذر التثبيت التلقائي لـ Aria2. يمكنك تثبيتها لاحقاً عبر: winget install aria2.aria2" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "[+] Aria2 متوفرة وجاهزة!" -ForegroundColor Green
}

Write-Host "`n===================================================================" -ForegroundColor Green
Write-Host "  ✅ تم تثبيت مهارة TorrentX بنجاح في Google Antigravity!" -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green

# Launch setup wizard if python is installed
$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if ($pythonCmd) {
    $wizardScript = Join-Path $targetDir "setup_wizard.py"
    if (Test-Path $wizardScript) {
        Write-Host "`n[*] جاري تشغيل معالج الإعداد التفاعلي (Setup Wizard) لربط حسابك..." -ForegroundColor Cyan
        & python $wizardScript
    }
} else {
    Write-Host "`n[!] يرجى تثبيت Python 3 لتشغيل المساعد الذكي." -ForegroundColor Red
}
