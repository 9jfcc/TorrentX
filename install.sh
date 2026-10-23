#!/usr/bin/env bash
# TorrentX One-Line Installer for macOS & Linux (Google Antigravity & Claude Code)

set -e

# ANSI Color Codes
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "==================================================================="
echo "     _____                            _   __   __"
echo "    |_   _|                          | |  \ \ / /"
echo "      | | ___  _ __ _ __ ___ _ __   | |_  \ V / "
echo "      | |/ _ \| '__| '__/ _ \ '_ \  | __|  > <  "
echo "      | | (_) | |  | | |  __/ | | | | |_  / . \ "
echo "      \_/\___/|_|  |_|  \___|_| |_|  \__|/_/ \_\\"
echo "      "
echo "   [+] AI-Powered Media Engine & Antigravity Skill"
echo "   [+] macOS & Linux Edition (Real-Debrid, Aria2, IINA/VLC)"
echo "==================================================================="
echo -e "${NC}"

TARGET_DIR="$HOME/.gemini/config/skills/torrentx"
echo -e "${YELLOW}[*] Preparing skill directory: $TARGET_DIR${NC}"
mkdir -p "$TARGET_DIR"

# Download latest repo zip from GitHub or copy locally if repository exists
TEMP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'torrentx')
ZIP_FILE="$TEMP_DIR/TorrentX-main.zip"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SCRIPT_DIR/skills/torrentx" ]; then
    echo -e "${YELLOW}[*] Deploying from local source...${NC}"
    cp -R "$SCRIPT_DIR/skills/torrentx/"* "$TARGET_DIR/"
else
    echo -e "${YELLOW}[*] Downloading latest TorrentX bundle from GitHub...${NC}"
    curl -fsSL "https://github.com/9jfcc/TorrentX/archive/refs/heads/main.zip" -o "$ZIP_FILE"
    unzip -q "$ZIP_FILE" -d "$TEMP_DIR"
    cp -R "$TEMP_DIR/TorrentX-main/skills/torrentx/"* "$TARGET_DIR/"
fi

# Clean up temp
rm -rf "$TEMP_DIR"
echo -e "${GREEN}[+] Skill files deployed successfully!${NC}"

# Check Python 3
PYTHON_CMD="python3"
if ! command -v python3 &> /dev/null; then
    if command -v python &> /dev/null; then
        PYTHON_CMD="python"
    else
        echo -e "${RED}[!] Python 3 is required. Please install Python 3.${NC}"
    fi
fi

# Check / Install Aria2
echo -e "${YELLOW}[*] Checking Aria2 high-speed downloader...${NC}"
if ! command -v aria2c &> /dev/null; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &> /dev/null; then
            echo -e "${CYAN}[*] Aria2 not found. Installing via Homebrew...${NC}"
            brew install aria2 || echo -e "${YELLOW}[!] Please run: brew install aria2${NC}"
        else
            echo -e "${YELLOW}[!] Homebrew not found. To install aria2 on Mac, install Homebrew then run: brew install aria2${NC}"
        fi
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo -e "${YELLOW}[*] To install aria2 on Linux: sudo apt install aria2 (Ubuntu/Debian) or sudo pacman -S aria2 (Arch)${NC}"
    fi
else
    echo -e "${GREEN}[+] Aria2 is detected and ready!${NC}"
fi

# Check Media Player on macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo -e "${YELLOW}[*] Checking macOS media players...${NC}"
    if [ -d "/Applications/IINA.app" ]; then
        echo -e "${GREEN}[+] Detected IINA (Default premium macOS player)!${NC}"
    elif [ -d "/Applications/VLC.app" ]; then
        echo -e "${GREEN}[+] Detected VLC media player!${NC}"
    else
        if command -v brew &> /dev/null; then
            echo -e "${CYAN}[*] Recommended player (IINA) not found. You can install it with: brew install --cask iina${NC}"
        fi
    fi
fi

echo -e "\n${GREEN}==================================================================="
echo -e "  [SUCCESS] TorrentX installed successfully for macOS & Unix!  "
echo -e "===================================================================${NC}"

# Launch setup wizard if in interactive terminal
if [ -t 0 ] && [ -f "$TARGET_DIR/setup_wizard.py" ] && command -v "$PYTHON_CMD" &> /dev/null; then
    echo -e "${CYAN}\n[*] Launching Interactive Setup Wizard...\n${NC}"
    "$PYTHON_CMD" "$TARGET_DIR/setup_wizard.py" || true
fi
