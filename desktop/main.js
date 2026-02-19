const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("path");
const { spawn } = require("child_process");

let mainWindow;
let claudeProcess = null;

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

app.whenReady().then(createWindow);
app.on("window-all-closed", () => {
  killClaude();
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

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("send-message", async (event, { message, cwd, permissionMode }) => {
  killClaude();

  return new Promise((resolve) => {
    const args = ["--print", "--output-format", "json"];

    if (permissionMode === "dangerously-skip-permissions") {
      args.push("--dangerously-skip-permissions");
    } else if (permissionMode) {
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
      // Stream chunks to renderer
      mainWindow.webContents.send("claude-chunk", chunk);
    });

    claudeProcess.stderr.on("data", (data) => {
      error += data.toString();
    });

    claudeProcess.on("close", (code) => {
      claudeProcess = null;
      // Try to parse final JSON output
      let result = output;
      try {
        const parsed = JSON.parse(output);
        result = parsed.result || output;
      } catch {
        // plain text output is fine
      }
      resolve({ result, error, code });
    });

    claudeProcess.on("error", (err) => {
      claudeProcess = null;
      resolve({ result: "", error: err.message, code: 1 });
    });
  });
});

ipcMain.handle("stop-claude", () => {
  killClaude();
  return { stopped: true };
});
