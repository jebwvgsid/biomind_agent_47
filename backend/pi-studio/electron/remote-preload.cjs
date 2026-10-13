"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("bioMindConnection", {
  list: () => ipcRenderer.invoke("biomind-connection-list"),
  save: profile => ipcRenderer.invoke("biomind-connection-save", profile),
  remove: id => ipcRenderer.invoke("biomind-connection-delete", id),
  clearAuth: id => ipcRenderer.invoke("biomind-connection-clear-auth", id),
  connect: (profile, password) => ipcRenderer.invoke("biomind-connection-connect", profile, password),
  disconnect: () => ipcRenderer.invoke("biomind-connection-disconnect"),
  onState: listener => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on("biomind-connection-state", handler);
    return () => ipcRenderer.removeListener("biomind-connection-state", handler);
  },
});
