'use strict';
// Electron main process: one window, the IPC surface, and the session controller.

const path = require('node:path');
const {
  app, BrowserWindow, clipboard, dialog, ipcMain, nativeTheme, session, shell,
} = require('electron');
const { SettingsStore } = require('./lib/settings');
const { SessionController } = require('./lib/session');

if (process.env.IOS_CI_CONFIG_DIR) {
  app.setPath('userData', path.resolve(process.env.IOS_CI_CONFIG_DIR));
}

let mainWindow = null;
let controller = null;

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function openExternalUrl(url) {
  try {
    const parsed = new URL(String(url));
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  } catch {
    return false;
  }
  shell.openExternal(String(url));
  return true;
}

function denyPermissions(target) {
  target.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
}

function guardContents(contents) {
  contents.on('will-navigate', (event, url) => {
    event.preventDefault();
    openExternalUrl(url);
  });
  contents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url);
    return { action: 'deny' };
  });
  // Esc must leave fullscreen even when the key lands in the guest desktop.
  contents.on('before-input-event', (_event, input) => {
    if (input.type === 'keyDown' && input.key === 'Escape'
        && mainWindow && !mainWindow.isDestroyed() && mainWindow.isFullScreen()) {
      mainWindow.setFullScreen(false);
    }
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    show: false,
    title: 'ios-ci',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#0c0e11' : '#f6f6f3',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: true,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('enter-full-screen', () => send('iosci:fullscreen-changed', { on: true }));
  mainWindow.on('leave-full-screen', () => send('iosci:fullscreen-changed', { on: false }));
  mainWindow.webContents.on('will-attach-webview', (event, webPreferences, params) => {
    delete webPreferences.preload;
    webPreferences.nodeIntegration = false;
    webPreferences.contextIsolation = true;
    webPreferences.sandbox = true;
    try {
      const parsed = new URL(params.src);
      if (parsed.protocol !== 'http:' || parsed.port !== '6080') {
        event.preventDefault();
      }
    } catch {
      event.preventDefault();
    }
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function registerIpc() {
  ipcMain.handle('iosci:get-state', () => controller.snapshot());
  ipcMain.handle('iosci:check-setup', async () => {
    await controller.checkSetup();
    return controller.snapshot();
  });
  ipcMain.handle('iosci:auto-fix', async () => {
    await controller.autoFix();
    return controller.snapshot();
  });
  ipcMain.handle('iosci:fix-check', (_event, request) =>
    controller.fixCheck(String(request && request.id)));
  ipcMain.handle('iosci:set-secret', async (_event, request) => {
    const result = await controller.saveSecret(String((request && request.value) || ''));
    return { ...controller.snapshot(), error: result.error || '' };
  });
  ipcMain.handle('iosci:save-settings', (_event, partial) =>
    controller.saveSettings(partial && typeof partial === 'object' ? partial : {}));
  ipcMain.handle('iosci:choose-dir', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: 'Copy a folder into the VM',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });
  ipcMain.handle('iosci:start', (_event, request) => controller.start(request || {}));
  ipcMain.handle('iosci:refresh', async () => {
    await controller.refresh();
    return controller.snapshot();
  });
  ipcMain.handle('iosci:cancel', (_event, request) =>
    controller.cancel(request && request.runId));
  ipcMain.handle('iosci:copy', (_event, request) => controller.copy(request || {}));
  ipcMain.handle('iosci:retry-git', () => controller.retryGit());
  ipcMain.handle('iosci:open-external', (_event, request) => ({
    ok: openExternalUrl(request && request.url),
  }));
  ipcMain.handle('iosci:copy-text', (_event, request) => {
    clipboard.writeText(String((request && request.text) || ''));
    return { ok: true };
  });
  ipcMain.handle('iosci:fullscreen', (_event, request) => {
    if (!mainWindow) return { on: false };
    mainWindow.setFullScreen(Boolean(request && request.on));
    const on = mainWindow.isFullScreen();
    send('iosci:fullscreen-changed', { on });
    return { on };
  });
}

app.whenReady().then(() => {
  denyPermissions(session.defaultSession);
  denyPermissions(session.fromPartition('ios-ci'));
  app.on('web-contents-created', (_event, contents) => guardContents(contents));

  const store = new SettingsStore(path.join(app.getPath('userData'), 'settings.json'));
  controller = new SessionController({
    store,
    version: app.getVersion(),
    onState: (state) => send('iosci:state', state),
    onSetupLog: (payload) => send('iosci:setup-log', payload),
    onAuthCode: (payload) => send('iosci:auth-code', payload),
    onOpenDesktop: (payload) => send('iosci:open-desktop', payload),
    openExternal: openExternalUrl,
  });

  registerIpc();
  createWindow();
  controller.init();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
