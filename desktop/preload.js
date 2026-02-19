const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("claude", {
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  sendMessage: (opts) => ipcRenderer.invoke("send-message", opts),
  stopClaude: () => ipcRenderer.invoke("stop-claude"),
  onChunk: (callback) => {
    ipcRenderer.on("claude-chunk", (_, chunk) => callback(chunk));
  },
});
