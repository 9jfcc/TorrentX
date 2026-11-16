@echo off
chcp 65001 >nul
title TorrentX Addon Server
echo ========================================================
echo   ⚡ TorrentX Stremio / Harbor Addon Server ⚡
echo ========================================================
echo.
echo [1/2] جاري تشغيل خادم الإضافة في الخلفية...

cd /d "%~dp0"

:: Check if Python is installed
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [خطأ] لم يتم العثور على بايثون مثبت في جهازك!
    echo يرجى تثبيت Python 3.10+ من https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install requests if missing
python -c "import requests" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [2/2] جاري تثبيت الحزم المطلوبة...
    pip install -q requests
)

:: Start addon in background silently with pythonw
start "" pythonw skills\torrentx\stremio_addon.py

echo.
echo [2/2] تم تشغيل الإضافة بنجاح! جاري فتح صفحة الإعداد في المتصفح...
timeout /t 1 >nul
start http://127.0.0.1:7123/configure

echo.
echo ✅ تم فتح صفحة الإعداد في متصفحك: http://127.0.0.1:7123/configure
echo يمكنك الآن إغلاق هذه النافذة، وستظل الإضافة تعمل في الخلفية.
timeout /t 3 >nul
exit
