const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const crypto = require("crypto");
const http = require("http");
const { WebSocketServer } = require("ws");
const fs = require("fs");
const os = require("os");

let mainWindow;
let claudeProcess = null;

// ============================================================
// Settings (persisted to ~/.claude-code-gui.json)
// ============================================================

const SETTINGS_PATH = path.join(os.homedir(), ".claude-code-gui.json");

const defaults = {
  webEnabled: false,     // Web server OFF by default
  webPort: 3456,
  webPassword: "",       // empty = no password (but server is off anyway)

  // Launch config
  launchMode: "auto",    // "auto" | "native" | "wsl" | "ssh"
  elevate: false,        // Windows: run as admin (UAC prompt on startup)
  wslDistro: "",         // WSL distro name (empty = default)
  sshHost: "",           // SSH host for remote claude
  sshUser: "",           // SSH user
  defaultCwd: "",        // default working directory
  defaultPermission: "default", // default permission mode
};

let settings = { ...defaults };

function loadSettings() {
  try {
    const data = fs.readFileSync(SETTINGS_PATH, "utf-8");
    settings = { ...defaults, ...JSON.parse(data) };
  } catch {
    // first run or corrupt file
  }
}

function saveSettings() {
  fs.writeFileSync(SETTINGS_PATH, JSON.stringify(settings, null, 2));
}

// Active auth tokens: password -> Set<token>
// Tokens expire after 24h
const authTokens = new Map(); // token -> expiry timestamp

function generateToken() {
  const token = crypto.randomBytes(32).toString("hex");
  authTokens.set(token, Date.now() + 24 * 60 * 60 * 1000);
  return token;
}

function isValidToken(token) {
  if (!token) return false;
  const expiry = authTokens.get(token);
  if (!expiry) return false;
  if (Date.now() > expiry) {
    authTokens.delete(token);
    return false;
  }
  return true;
}

function requiresAuth() {
  return settings.webPassword && settings.webPassword.length > 0;
}

// ============================================================
// Electron window
// ============================================================

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 700,
    minWidth: 600,
    minHeight: 500,
    backgroundColor: "#0d1117",
    titleBarStyle: "hiddenInset",
    frame: process.platform === "darwin",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

app.whenReady().then(async () => {
  loadSettings();

  // Windows elevation: if requested and not already admin, re-launch elevated
  if (settings.elevate && process.platform === "win32" && !isElevated()) {
    try {
      const { execSync } = require("child_process");
      // Use PowerShell to re-launch with admin rights
      execSync(
        `powershell -Command "Start-Process '${process.execPath}' -ArgumentList '${process.argv.slice(1).join("' '")}' -Verb RunAs"`,
        { windowsHide: true }
      );
      app.quit();
      return;
    } catch {
      // User declined UAC or error - continue without elevation
      console.log("Elevation failed or declined, continuing without admin rights");
    }
  }

  createWindow();
  if (settings.webEnabled) {
    startWebServer();
  }
});

function isElevated() {
  if (process.platform !== "win32") return true;
  try {
    const { execSync } = require("child_process");
    execSync("net session", { windowsHide: true, stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

app.on("window-all-closed", () => {
  killClaude();
  stopWebServer();
  app.quit();
});

// ============================================================
// Claude CLI interaction via spawn
// ============================================================

function killClaude() {
  if (claudeProcess) {
    claudeProcess.kill();
    claudeProcess = null;
  }
}

function resolveMode() {
  if (settings.launchMode !== "auto") return settings.launchMode;
  // Auto-detect: if Windows and not in WSL already, use wsl
  if (process.platform === "win32") return "wsl";
  return "native";
}

function buildSpawn({ message, cwd, permissionMode }) {
  const claudeArgs = ["--print", "--output-format", "json"];

  const perm = permissionMode || settings.defaultPermission || "default";
  if (perm === "dangerously-skip-permissions") {
    claudeArgs.push("--dangerously-skip-permissions");
  } else if (perm && perm !== "default") {
    claudeArgs.push("--permission-mode", perm);
  }

  claudeArgs.push(message);

  const mode = resolveMode();
  const effectiveCwd = cwd || settings.defaultCwd || process.env.HOME;

  if (mode === "wsl") {
    // Run claude inside WSL
    const distroArgs = settings.wslDistro ? ["-d", settings.wslDistro] : [];
    const wslCmd = `cd ${shellEscape(winToWslPath(effectiveCwd))} && claude ${claudeArgs.map(shellEscape).join(" ")}`;
    return {
      cmd: "wsl.exe",
      args: [...distroArgs, "--", "bash", "-lc", wslCmd],
      opts: { env: { ...process.env }, shell: false },
    };
  }

  if (mode === "ssh") {
    // Run claude on remote host via SSH
    const sshTarget = settings.sshUser
      ? `${settings.sshUser}@${settings.sshHost}`
      : settings.sshHost;
    const remoteCmd = `cd ${shellEscape(effectiveCwd)} && claude ${claudeArgs.map(shellEscape).join(" ")}`;
    return {
      cmd: "ssh",
      args: ["-tt", sshTarget, remoteCmd],
      opts: { env: { ...process.env }, shell: false },
    };
  }

  // Native mode
  return {
    cmd: "claude",
    args: claudeArgs,
    opts: { cwd: effectiveCwd, env: { ...process.env }, shell: true },
  };
}

function shellEscape(s) {
  if (!s) return "''";
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

function winToWslPath(p) {
  if (!p || process.platform !== "win32") return p;
  // Convert C:\foo\bar -> /mnt/c/foo/bar
  const m = p.match(/^([A-Za-z]):\\/);
  if (m) {
    return "/mnt/" + m[1].toLowerCase() + p.slice(2).replace(/\\/g, "/");
  }
  return p.replace(/\\/g, "/");
}

function runClaude(opts, onChunk) {
  killClaude();

  return new Promise((resolve) => {
    const { cmd, args, opts: spawnOpts } = buildSpawn(opts);

    claudeProcess = spawn(cmd, args, spawnOpts);

    let output = "";
    let error = "";

    claudeProcess.stdout.on("data", (data) => {
      const chunk = data.toString();
      output += chunk;
      if (onChunk) onChunk(chunk);
    });

    claudeProcess.stderr.on("data", (data) => {
      error += data.toString();
    });

    claudeProcess.on("close", (code) => {
      claudeProcess = null;
      let result = output;
      try {
        const parsed = JSON.parse(output);
        result = parsed.result || output;
      } catch {
        // plain text is fine
      }
      resolve({ result, error, code });
    });

    claudeProcess.on("error", (err) => {
      claudeProcess = null;
      resolve({ result: "", error: err.message, code: 1 });
    });
  });
}

// ============================================================
// IPC for Electron renderer
// ============================================================

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("send-message", async (event, opts) => {
  return runClaude(opts, (chunk) => {
    mainWindow.webContents.send("claude-chunk", chunk);
  });
});

ipcMain.handle("stop-claude", () => {
  killClaude();
  return { stopped: true };
});

ipcMain.handle("get-settings", () => {
  return {
    webEnabled: settings.webEnabled,
    webPort: settings.webPort,
    webPassword: settings.webPassword ? "***" : "",
    webUrl: settings.webEnabled ? `http://${getLocalIP()}:${settings.webPort}` : null,
    launchMode: settings.launchMode,
    elevate: settings.elevate,
    wslDistro: settings.wslDistro,
    sshHost: settings.sshHost,
    sshUser: settings.sshUser,
    defaultCwd: settings.defaultCwd,
    defaultPermission: settings.defaultPermission,
    platform: process.platform,
    isAdmin: process.platform === "win32" ? isElevated() : process.getuid?.() === 0,
  };
});

ipcMain.handle("save-settings", (event, newSettings) => {
  const needRestart = (
    newSettings.webEnabled !== settings.webEnabled ||
    newSettings.webPort !== settings.webPort
  );

  settings.webEnabled = !!newSettings.webEnabled;
  settings.webPort = parseInt(newSettings.webPort, 10) || 3456;

  if (newSettings.webPassword !== "***") {
    settings.webPassword = newSettings.webPassword || "";
  }

  // Launch settings
  if (newSettings.launchMode) settings.launchMode = newSettings.launchMode;
  if (newSettings.elevate !== undefined) settings.elevate = !!newSettings.elevate;
  if (newSettings.wslDistro !== undefined) settings.wslDistro = newSettings.wslDistro;
  if (newSettings.sshHost !== undefined) settings.sshHost = newSettings.sshHost;
  if (newSettings.sshUser !== undefined) settings.sshUser = newSettings.sshUser;
  if (newSettings.defaultCwd !== undefined) settings.defaultCwd = newSettings.defaultCwd;
  if (newSettings.defaultPermission !== undefined) settings.defaultPermission = newSettings.defaultPermission;

  saveSettings();

  if (needRestart) {
    stopWebServer();
    if (settings.webEnabled) {
      startWebServer();
    }
  }

  return {
    webEnabled: settings.webEnabled,
    webPort: settings.webPort,
    webPassword: settings.webPassword ? "***" : "",
    webUrl: settings.webEnabled ? `http://${getLocalIP()}:${settings.webPort}` : null,
    launchMode: settings.launchMode,
    elevate: settings.elevate,
    wslDistro: settings.wslDistro,
    sshHost: settings.sshHost,
    sshUser: settings.sshUser,
    defaultCwd: settings.defaultCwd,
    defaultPermission: settings.defaultPermission,
    platform: process.platform,
    isAdmin: process.platform === "win32" ? isElevated() : process.getuid?.() === 0,
  };
});

// ============================================================
// HTTP + WebSocket server for mobile access
// ============================================================

let httpServer = null;

function getLocalIP() {
  const interfaces = os.networkInterfaces();
  for (const name of Object.keys(interfaces)) {
    for (const iface of interfaces[name]) {
      if (iface.family === "IPv4" && !iface.internal) {
        return iface.address;
      }
    }
  }
  return "127.0.0.1";
}

function stopWebServer() {
  if (httpServer) {
    httpServer.close();
    httpServer = null;
  }
}

function startWebServer() {
  stopWebServer();

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);

    // --- Auth endpoint ---
    if (url.pathname === "/auth" && req.method === "POST") {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        try {
          const { password } = JSON.parse(body);
          if (!requiresAuth()) {
            // No password set, grant access
            const token = generateToken();
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ ok: true, token }));
          } else if (password === settings.webPassword) {
            const token = generateToken();
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ ok: true, token }));
          } else {
            res.writeHead(401, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ ok: false, error: "Wrong password" }));
          }
        } catch {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "Bad request" }));
        }
      });
      return;
    }

    // --- Check if auth is needed ---
    if (url.pathname === "/auth-status") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ needsAuth: requiresAuth() }));
      return;
    }

    // --- Serve HTML ---
    if (url.pathname === "/" || url.pathname === "/index.html") {
      const htmlPath = path.join(__dirname, "renderer", "index.html");
      fs.readFile(htmlPath, (err, data) => {
        if (err) {
          res.writeHead(500);
          res.end("Error loading page");
          return;
        }
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(data);
      });
      return;
    }

    res.writeHead(404);
    res.end("Not found");
  });

  const wss = new WebSocketServer({ server });

  wss.on("connection", (ws, req) => {
    let authenticated = !requiresAuth();

    ws.on("message", async (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }

      // --- Auth via WebSocket ---
      if (msg.type === "auth") {
        if (!requiresAuth()) {
          authenticated = true;
          ws.send(JSON.stringify({ type: "auth", ok: true }));
        } else if (msg.token && isValidToken(msg.token)) {
          authenticated = true;
          ws.send(JSON.stringify({ type: "auth", ok: true }));
        } else {
          ws.send(JSON.stringify({ type: "auth", ok: false, error: "Invalid token" }));
        }
        return;
      }

      // --- Reject unauthenticated requests ---
      if (!authenticated) {
        ws.send(JSON.stringify({ type: "error", error: "Not authenticated" }));
        return;
      }

      if (msg.type === "send") {
        ws.send(JSON.stringify({ type: "status", busy: true }));

        const result = await runClaude(
          {
            message: msg.message,
            cwd: msg.cwd || null,
            permissionMode: msg.permissionMode || "default",
          },
          (chunk) => {
            ws.send(JSON.stringify({ type: "chunk", data: chunk }));
          }
        );

        ws.send(JSON.stringify({ type: "done", ...result }));
        ws.send(JSON.stringify({ type: "status", busy: false }));
      } else if (msg.type === "stop") {
        killClaude();
        ws.send(JSON.stringify({ type: "stopped" }));
        ws.send(JSON.stringify({ type: "status", busy: false }));
      }
    });
  });

  server.listen(settings.webPort, "0.0.0.0", () => {
    const ip = getLocalIP();
    console.log(`Web server running at http://${ip}:${settings.webPort}`);
    if (requiresAuth()) {
      console.log("Password protection: ON");
    } else {
      console.log("Password protection: OFF (set a password in settings!)");
    }
  });

  httpServer = server;
}
