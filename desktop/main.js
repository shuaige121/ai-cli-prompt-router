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

app.whenReady().then(() => {
  loadSettings();
  createWindow();
  if (settings.webEnabled) {
    startWebServer();
  }
});

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

function runClaude({ message, cwd, permissionMode }, onChunk) {
  killClaude();

  return new Promise((resolve) => {
    const args = ["--print", "--output-format", "json"];

    if (permissionMode === "dangerously-skip-permissions") {
      args.push("--dangerously-skip-permissions");
    } else if (permissionMode && permissionMode !== "default") {
      args.push("--permission-mode", permissionMode);
    }

    args.push(message);

    claudeProcess = spawn("claude", args, {
      cwd: cwd || process.env.HOME,
      env: { ...process.env },
      shell: true,
    });

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
  };
});

ipcMain.handle("save-settings", (event, newSettings) => {
  const needRestart = (
    newSettings.webEnabled !== settings.webEnabled ||
    newSettings.webPort !== settings.webPort
  );

  settings.webEnabled = !!newSettings.webEnabled;
  settings.webPort = parseInt(newSettings.webPort, 10) || 3456;

  // Only update password if explicitly changed (not the masked "***")
  if (newSettings.webPassword !== "***") {
    settings.webPassword = newSettings.webPassword || "";
  }

  saveSettings();

  // Restart web server if needed
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
