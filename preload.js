'use strict';
// The only bridge between the renderer and the main process. No Node APIs leak.

const { contextBridge, ipcRenderer } = require('electron');

function subscribe(channel, callback) {
  const listener = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('iosci', {
  getState: () => ipcRenderer.invoke('iosci:get-state'),
  checkSetup: () => ipcRenderer.invoke('iosci:check-setup'),
  autoFix: () => ipcRenderer.invoke('iosci:auto-fix'),
  fixCheck: (id) => ipcRenderer.invoke('iosci:fix-check', { id }),
  setSecret: (value) => ipcRenderer.invoke('iosci:set-secret', { value }),
  saveSettings: (partial) => ipcRenderer.invoke('iosci:save-settings', partial),
  chooseDir: () => ipcRenderer.invoke('iosci:choose-dir'),
  start: (request) => ipcRenderer.invoke('iosci:start', request),
  refresh: () => ipcRenderer.invoke('iosci:refresh'),
  cancel: (runId) => ipcRenderer.invoke('iosci:cancel', { runId }),
  copy: (request) => ipcRenderer.invoke('iosci:copy', request),
  retryGit: () => ipcRenderer.invoke('iosci:retry-git'),
  openExternal: (url) => ipcRenderer.invoke('iosci:open-external', { url }),
  copyText: (text) => ipcRenderer.invoke('iosci:copy-text', { text }),
  fullscreen: (on) => ipcRenderer.invoke('iosci:fullscreen', { on }),
  onState: (callback) => subscribe('iosci:state', callback),
  onSetupLog: (callback) => subscribe('iosci:setup-log', callback),
  onAuthCode: (callback) => subscribe('iosci:auth-code', callback),
  onOpenDesktop: (callback) => subscribe('iosci:open-desktop', callback),
  onFullscreen: (callback) => subscribe('iosci:fullscreen-changed', callback),
});
