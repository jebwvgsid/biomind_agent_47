"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("bioMindConnection", {
  list: () => ipcRenderer.invoke("biomind-connection-list"),
  save: profile => ipcRenderer.invoke("biomind-connection-save", profile),
  remove: id => ipcRenderer.invoke("biomind-connection-delete", id),
  clearAuth: id => ipcRenderer.invoke("biomind-connection-clear-auth", id),
  connect: (profile, password, remember) => ipcRenderer.invoke("biomind-connection-connect", profile, password, remember),
  install: (profile, setup, password) => ipcRenderer.invoke("biomind-connection-install", profile, setup, password),
  preset: () => ipcRenderer.invoke("biomind-connection-preset"),
  selectKey: () => ipcRenderer.invoke("biomind-connection-select-key"),
  disconnect: () => ipcRenderer.invoke("biomind-connection-disconnect"),
  onState: listener => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on("biomind-connection-state", handler);
    return () => ipcRenderer.removeListener("biomind-connection-state", handler);
  },
});
