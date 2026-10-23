<#
.SYNOPSIS
    TorrentX One-Line Installer for Windows & Antigravity
.DESCRIPTION
    Installs the TorrentX skill, configures Aria2, and sets up Real-Debrid with zero hassle.
#>

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 > $null
} catch {}

$Host.UI.RawUI.WindowTitle = "TorrentX Installer"

Write-Host @"
===================================================================
     _____                            _   __   __
    |_   _|                          | |  \ \ / /
      | | ___  _ __ _ __ ___ _ __   | |_  \ V / 
      | |/ _ \| '__| '__/ _ \ '_ \  | __|  > <  
      | | (_) | |  | | |  __/ | | | | |_  / . \ 
      \_/\___/|_|  |_|  \___|_| |_|  \__|/_/ \_\
      
   [+] AI-Powered Media Engine & Antigravity Skill
   [+] Real-Debrid Cloud Streaming, Aria2 & Subtitles
===================================================================
"@ -ForegroundColor Cyan

$targetDir = Join-Path $env:USERPROFILE ".gemini\config\skills\torrentx"
Write-Host "`n[*] Preparing skill directory: $targetDir" -ForegroundColor Yellow

if (!(Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

# Download latest repo zip from GitHub
$zipUrl = "https://github.com/9jfcc/TorrentX/archive/refs/heads/main.zip"
$tempZip = Join-Path $env:TEMP "TorrentX-main.zip"
$tempExtract = Join-Path $env:TEMP "TorrentX-Extract"

Write-Host "[*] Downloading latest TorrentX bundle from GitHub..." -ForegroundColor Yellow
Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip -UseBasicParsing

Write-Host "[*] Extracting and deploying skill files..." -ForegroundColor Yellow
if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force

$sourceSkills = Join-Path $tempExtract "TorrentX-main\skills\torrentx"
if (Test-Path $sourceSkills) {
    Copy-Item -Path "$sourceSkills\*" -Destination $targetDir -Recurse -Force
    Write-Host "[+] Skill files deployed successfully!" -ForegroundColor Green
} else {
    Write-Host "[!] Warning: Could not locate skills directory inside bundle." -ForegroundColor Red
}

# Clean temp files
Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue

# Verify Aria2
Write-Host "[*] Checking Aria2 high-speed downloader..." -ForegroundColor Yellow
$ariaPath = (Get-Command aria2c -ErrorAction SilentlyContinue)
if (-not $ariaPath) {
    Write-Host "[*] Aria2 not found. Installing automatically via Winget..." -ForegroundColor Cyan
    try {
        winget install aria2.aria2 --source winget --silent --accept-source-agreements --accept-package-agreements
        Write-Host "[+] Aria2 installed successfully!" -ForegroundColor Green
    } catch {
        Write-Host "[!] Could not auto-install Aria2. You can install it manually: winget install aria2.aria2" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "[+] Aria2 is detected and ready!" -ForegroundColor Green
}

Write-Host "`n===================================================================" -ForegroundColor Green
Write-Host "  [SUCCESS] TorrentX installed successfully in Google Antigravity!  " -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green

# Launch setup wizard if python is installed
$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if ($pythonCmd) {
    $wizardScript = Join-Path $targetDir "setup_wizard.py"
    if (Test-Path $wizardScript) {
        Write-Host "`n[*] Launching Interactive Setup Wizard (معالج الإعداد)...`n" -ForegroundColor Cyan
        & python $wizardScript
    }
} else {
    Write-Host "`n[!] Please install Python 3 to run the AI assistant." -ForegroundColor Red
}
