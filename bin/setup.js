#!/usr/bin/env node
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

console.log("⚡ Starting TorrentX Installer...");

const isWin = process.platform === "win32";

if (isWin) {
  // Windows Setup: Powershell & install.ps1
  const localPs1 = path.join(__dirname, "..", "install.ps1");
  let psArgs;

  if (fs.existsSync(localPs1)) {
    psArgs = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", localPs1];
  } else {
    const remoteCmd = "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; & ([ScriptBlock]::Create((Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/9jfcc/TorrentX/main/install.ps1' -UseBasicParsing).Content))";
    psArgs = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", remoteCmd];
  }

  const ps = spawn("powershell", psArgs, { stdio: "inherit" });
  ps.on("exit", (code) => {
    process.exit(code || 0);
  });
} else {
  // macOS & Linux Setup: Bash & install.sh
  const localSh = path.join(__dirname, "..", "install.sh");
  let proc;

  if (fs.existsSync(localSh)) {
    proc = spawn("bash", [localSh], { stdio: "inherit" });
  } else {
    const remoteCmd = "curl -fsSL https://raw.githubusercontent.com/9jfcc/TorrentX/main/install.sh | bash";
    proc = spawn("bash", ["-c", remoteCmd], { stdio: "inherit" });
  }

  proc.on("exit", (code) => {
    process.exit(code || 0);
  });
}
