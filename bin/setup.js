#!/usr/bin/env node
"use strict";

const { spawn } = require("child_process");
const path = require("path");

console.log("⚡ Starting TorrentX Installer...");
const installPs1 = path.join(__dirname, "..", "install.ps1");

const ps = spawn("powershell", [
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", installPs1
], {
  stdio: "inherit"
});

ps.on("exit", (code) => {
  process.exit(code || 0);
});
