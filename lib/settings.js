'use strict';
// Settings for the local client: one JSON file under Electron's userData directory.
// Pure helpers live here so tests never import Electron.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DEFAULT_RUNNER = process.env.IOS_CI_RUNNER || 'eshahrabani/ios-ci';
const MAX_SESSIONS = 20;

function defaults() {
  return {
    runnerRepo: DEFAULT_RUNNER,
    minutes: 30,
    copyDir: '',
    copyName: '',
    excludeDefaults: true,
    lastRunId: null,
    gitName: '',
    gitEmail: '',
    sessions: {},
  };
}

function clampMinutes(value, fallback = 30) {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(300, Math.max(1, Math.round(number)));
}

function text(value) {
  return typeof value === 'string' ? value : '';
}

function sanitizeSession(entry) {
  if (!entry || typeof entry !== 'object' || typeof entry.token !== 'string') return null;
  return {
    token: entry.token,
    minutes: clampMinutes(entry.minutes, 0),
    startedAt: text(entry.startedAt),
    readyAt: text(entry.readyAt),
    url: text(entry.url),
  };
}

/** Keep only known keys; drop session entries without a token; keep the 20 newest. */
function sanitizeSettings(raw) {
  const settings = defaults();
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return settings;

  if (typeof raw.runnerRepo === 'string' && raw.runnerRepo.trim()) {
    settings.runnerRepo = raw.runnerRepo.trim();
  }
  settings.minutes = clampMinutes(raw.minutes, settings.minutes);
  settings.copyDir = text(raw.copyDir);
  settings.copyName = text(raw.copyName);
  if (typeof raw.excludeDefaults === 'boolean') settings.excludeDefaults = raw.excludeDefaults;
  const last = Number(raw.lastRunId);
  settings.lastRunId = Number.isInteger(last) && last > 0 ? last : null;
  settings.gitName = text(raw.gitName);
  settings.gitEmail = text(raw.gitEmail);

  const sessions = raw.sessions && typeof raw.sessions === 'object' ? raw.sessions : {};
  const numbered = [];
  for (const [key, value] of Object.entries(sessions)) {
    if (!/^\d+$/.test(key)) continue;
    const id = Number(key);
    if (!Number.isSafeInteger(id) || id <= 0) continue;
    const session = sanitizeSession(value);
    if (!session) continue;
    numbered.push([id, session]);
  }
  numbered.sort((a, b) => b[0] - a[0]);
  for (const [id, session] of numbered.slice(0, MAX_SESSIONS)) {
    settings.sessions[String(id)] = session;
  }
  return settings;
}

/** Map the retired PySide6 client's settings file onto this shape. */
function migrateSettings(old) {
  const raw = old && typeof old === 'object' && !Array.isArray(old) ? old : {};
  const sessions = {};
  const legacy = raw.sessions && typeof raw.sessions === 'object' ? raw.sessions : {};
  for (const [key, value] of Object.entries(legacy)) {
    if (!value || typeof value !== 'object') continue;
    sessions[key] = {
      token: value.token,
      minutes: value.minutes,
      startedAt: value.started_at,
      readyAt: value.ready_at,
      url: value.url,
    };
  }
  return sanitizeSettings({
    runnerRepo: raw.runner_repo,
    minutes: raw.minutes,
    copyDir: raw.upload_dir,
    excludeDefaults: raw.exclude_defaults,
    lastRunId: raw.last_run_id,
    sessions,
  });
}

function legacyPaths() {
  const configHome = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return [
    path.join(configHome, 'ios-ci-gui', 'settings.json'),
    path.join(os.homedir(), 'Library', 'Application Support', 'ios-ci-gui', 'settings.json'),
  ];
}

class SettingsStore {
  constructor(file) {
    this.file = file;
  }

  /** The first readable legacy settings file, mapped, or null. */
  migrateOnce() {
    for (const candidate of legacyPaths()) {
      try {
        return migrateSettings(JSON.parse(fs.readFileSync(candidate, 'utf8')));
      } catch {
        // Missing or corrupt: try the next location.
      }
    }
    return null;
  }

  load() {
    let raw;
    try {
      raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch (error) {
      if (error && error.code === 'ENOENT') {
        const migrated = this.migrateOnce();
        if (migrated) {
          try {
            this.save(migrated);
          } catch {
            // A read-only home still gets usable in-memory settings.
          }
          return migrated;
        }
      }
      return defaults();
    }
    return sanitizeSettings(raw);
  }

  /** Temp file in the same directory, mode 0600, then rename over the target. */
  save(settings) {
    const clean = sanitizeSettings(settings);
    const directory = path.dirname(this.file);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const temporary = path.join(
      directory,
      `.settings-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.json`,
    );
    try {
      fs.writeFileSync(temporary, `${JSON.stringify(clean, null, 2)}\n`, { mode: 0o600 });
      fs.chmodSync(temporary, 0o600);
      fs.renameSync(temporary, this.file);
    } catch (error) {
      try {
        fs.unlinkSync(temporary);
      } catch {
        // Already gone.
      }
      throw error;
    }
    return clean;
  }
}

module.exports = {
  DEFAULT_RUNNER,
  MAX_SESSIONS,
  SettingsStore,
  clampMinutes,
  defaults,
  migrateSettings,
  sanitizeSettings,
};
