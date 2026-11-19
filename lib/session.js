'use strict';
// The session controller: dispatch, poll, ready detection, Git bootstrap, auto-copy.
// It owns all state and pushes snapshots; it never imports Electron.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { Backend, DEFAULT_EXCLUDES, UPLOAD_NAME, humanBytes } = require('./backend');
const { estimate } = require('./progress');
const { runChecks, resolveIdentity, autoFix, saveSecret } = require('./setup');
const { sanitizeSettings } = require('./settings');

const POLL_MS = 2000;
const NAME_SAFE = /[^A-Za-z0-9._-]+/g;

function utcNow() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function idleUpload() {
  return {
    state: 'idle', sent: 0, total: 0, percent: 0, message: '', path: '',
  };
}

function idleGit() {
  return {
    state: 'pending', message: '', name: '', email: '', host: '', login: '',
  };
}

function copyNameFor(directory, name) {
  const explicit = (name || '').trim();
  if (explicit) return explicit;
  return path.basename(directory || '').replace(NAME_SAFE, '-').slice(0, 64);
}

function desktopOk(health) {
  return Boolean(health
    && health.alive === true
    && Number(health.frames) > 0
    && Number(health.width) > 0
    && Number(health.height) > 0
    && health.input_ok === true);
}

class SessionController {
  constructor({
    store, version = '', onState = () => {}, onSetupLog = () => {},
    onAuthCode = () => {}, onOpenDesktop = () => {}, openExternal = () => {},
  }) {
    this.store = store;
    this.version = version;
    this.onState = onState;
    this.onSetupLog = onSetupLog;
    this.onAuthCode = onAuthCode;
    this.onOpenDesktop = onOpenDesktop;
    this.openExternal = openExternal;
    this.settings = store.load();
    this.backend = new Backend(this.settings.runnerRepo);
    this.setup = {
      checking: true,
      complete: false,
      checks: [],
      host: '',
      login: '',
      identity: null,
      needs: null,
      fixing: false,
    };
    this.checkCache = null;
    this.detail = null;
    this.ip = null;
    this.token = '';
    this.activeRunId = null;
    this.runs = [];
    this.runsError = '';
    this.polling = false;
    this.pollTimer = null;
    this.pendingCopy = null;
    this.autoCopied = false;
    this.session = this.emptySession(this.settings.minutes);
  }

  emptySession(minutes) {
    return {
      runId: null,
      minutes,
      url: '',
      light: 'idle',
      percent: 0,
      eta: null,
      phase: '',
      elapsed: 0,
      steps: [],
      health: null,
      tools: {},
      git: idleGit(),
      error: '',
      upload: idleUpload(),
    };
  }

  snapshot() {
    const sessions = [];
    for (const [key, entry] of Object.entries(this.settings.sessions || {})) {
      sessions.push({
        runId: Number(key),
        minutes: entry.minutes,
        startedAt: entry.startedAt,
        readyAt: entry.readyAt,
        url: entry.url,
      });
    }
    sessions.sort((a, b) => b.runId - a.runId);
    return {
      version: this.version,
      setup: { ...this.setup },
      settings: {
        runnerRepo: this.settings.runnerRepo,
        minutes: this.settings.minutes,
        copyDir: this.settings.copyDir,
        copyName: this.settings.copyName,
        excludeDefaults: this.settings.excludeDefaults,
        gitName: this.settings.gitName,
        gitEmail: this.settings.gitEmail,
      },
      session: { ...this.session },
      runs: this.runs,
      runsError: this.runsError,
      sessions,
    };
  }

  push() {
    this.onState(this.snapshot());
  }

  persist() {
    try {
      this.settings = this.store.save(this.settings);
    } catch (error) {
      console.error(`could not save settings: ${error.message}`);
    }
  }

  applyChecks(result) {
    this.checkCache = result;
    this.setup = {
      checking: false,
      complete: result.complete,
      checks: result.checks,
      host: result.host,
      login: result.login,
      identity: result.identity || null,
      needs: result.complete ? null : this.setup.needs,
      fixing: false,
    };
  }

  async init() {
    this.push();
    await this.checkSetup();
    const entry = this.settings.lastRunId
      ? this.settings.sessions[String(this.settings.lastRunId)]
      : null;
    if (entry && entry.token) {
      this.activeRunId = this.settings.lastRunId;
      this.token = entry.token;
      this.ip = null;
      this.autoCopied = Boolean(entry.readyAt);
      this.session = {
        ...this.emptySession(entry.minutes || this.settings.minutes),
        runId: this.activeRunId,
        url: entry.url || '',
        light: entry.readyAt ? 'ready' : 'starting',
        phase: entry.readyAt ? 'Desktop ready' : 'Connecting to your Mac…',
        percent: entry.readyAt ? 100 : 0,
      };
      this.startPolling();
      this.poll();
      this.push();
    }
    await this.refreshRuns();
  }

  async checkSetup() {
    this.setup.checking = true;
    this.push();
    try {
      const result = await runChecks({
        runnerRepo: this.settings.runnerRepo,
        settings: this.settings,
      });
      this.applyChecks(result);
    } catch (error) {
      this.setup = {
        checking: false,
        complete: false,
        checks: [{
          id: 'checks', label: 'Setup checks', required: true, ok: false, detail: error.message,
        }],
        host: '',
        login: '',
        identity: null,
        needs: null,
        fixing: false,
      };
      this.checkCache = null;
    }
    this.push();
    return this.setup;
  }

  async autoFix(only) {
    this.setup.fixing = true;
    this.setup.needs = null;
    this.push();
    const emit = (item) => {
      if (item.code) this.onAuthCode({ code: item.code, url: item.url });
      if (item.text) this.onSetupLog({ text: item.text });
    };
    try {
      const result = await autoFix({
        runnerRepo: this.settings.runnerRepo,
        settings: this.settings,
        emit,
        openExternal: this.openExternal,
        only,
      });
      this.applyChecks(result);
      this.setup.needs = result.needs || null;
      this.setup.fixing = false;
    } catch (error) {
      this.setup.fixing = false;
      this.setup.needs = null;
      this.onSetupLog({ text: error.message });
    }
    this.push();
    return this.setup;
  }

  async fixCheck(id) {
    const setup = await this.autoFix(id);
    const row = (setup.checks || []).find((check) => check.id === id);
    return { ok: Boolean(row && row.ok), error: row && !row.ok ? setup.needs : undefined };
  }

  async saveSecret(value) {
    this.setup.fixing = true;
    this.push();
    try {
      const result = await saveSecret({ runnerRepo: this.settings.runnerRepo, value });
      this.applyChecks(result);
      this.setup.fixing = false;
      this.setup.needs = null;
      this.push();
      return { ok: true };
    } catch (error) {
      this.setup.fixing = false;
      this.push();
      return { ok: false, error: error.message };
    }
  }

  saveSettings(partial) {
    const previousRepo = this.settings.runnerRepo;
    this.settings = sanitizeSettings({ ...this.settings, ...partial });
    this.persist();
    if (this.settings.runnerRepo !== previousRepo) {
      this.backend = new Backend(this.settings.runnerRepo);
      this.checkCache = null;
      this.checkSetup();
    }
    this.push();
    return this.snapshot();
  }

  // -- dispatch and polling ---------------------------------------------------

  async start({ minutes, copyDir, copyName, excludeDefaults } = {}) {
    const mins = Number(minutes);
    if (!Number.isInteger(mins) || mins < 1 || mins > 300) {
      return { ok: false, error: 'the session must be 1-300 whole minutes' };
    }
    const directory = typeof copyDir === 'string' ? copyDir.trim() : '';
    const name = copyNameFor(directory, copyName);
    if (directory) {
      if (name === '.' || name === '..' || !UPLOAD_NAME.test(name)) {
        return { ok: false, error: 'the copy name may only use letters, digits, dot, dash, underscore (1-64 characters)' };
      }
      let isDirectory = false;
      try {
        isDirectory = fs.statSync(directory).isDirectory();
      } catch {
        isDirectory = false;
      }
      if (!isDirectory) return { ok: false, error: `${directory} is not a directory` };
    }

    this.settings = sanitizeSettings({
      ...this.settings,
      minutes: mins,
      copyDir: directory,
      copyName: name,
      excludeDefaults: excludeDefaults !== false,
    });
    this.persist();

    const checks = await runChecks({
      runnerRepo: this.settings.runnerRepo,
      settings: this.settings,
    });
    this.applyChecks(checks);
    this.push();
    if (!checks.complete) return { ok: false, needsSetup: true };

    this.token = crypto.randomBytes(32).toString('base64url');
    const hash = crypto.createHash('sha256').update(this.token).digest('hex');
    this.activeRunId = null;
    this.detail = null;
    this.ip = null;
    this.autoCopied = false;
    this.pendingCopy = directory && name
      ? { dir: directory, name, excludeDefaults: this.settings.excludeDefaults }
      : null;
    this.session = {
      ...this.emptySession(mins),
      light: 'starting',
      phase: 'Starting',
    };
    this.push();
    this.dispatchAndWatch(mins, hash);
    return { ok: true };
  }

  async dispatchAndWatch(minutes, hash) {
    const token = this.token;
    try {
      const runId = await this.backend.dispatch(minutes, hash);
      if (this.token !== token) return;         // superseded by another start
      this.activeRunId = runId;
      this.session.runId = runId;
      this.settings.sessions[String(runId)] = {
        token,
        minutes,
        startedAt: utcNow(),
        readyAt: '',
        url: '',
      };
      this.settings.lastRunId = runId;
      this.persist();
      this.startPolling();
      this.push();
      this.poll();
    } catch (error) {
      this.session.light = 'idle';
      this.session.phase = '';
      this.session.error = error.message;
      this.push();
    }
  }

  startPolling() {
    this.stopPolling();
    this.pollTimer = setInterval(() => {
      this.poll();
    }, POLL_MS);
    if (this.pollTimer.unref) this.pollTimer.unref();
  }

  stopPolling() {
    clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  async poll() {
    const runId = this.activeRunId;
    if (!runId || this.polling) return;
    this.polling = true;
    try {
      const [runsResult, detailResult, ipResult] = await Promise.allSettled([
        this.backend.runs(20),
        this.backend.runDetail(runId),
        this.ip ? Promise.resolve(this.ip) : this.backend.tailnetPeer(runId),
      ]);
      if (this.activeRunId !== runId) return;

      if (runsResult.status === 'fulfilled') {
        this.runs = runsResult.value;
        this.runsError = '';
      } else {
        this.runsError = runsResult.reason.message;
      }
      if (detailResult.status === 'fulfilled') {
        this.detail = detailResult.value;
      }
      if (ipResult.status === 'fulfilled' && ipResult.value) this.ip = ipResult.value;

      if (this.detail && this.detail.status === 'completed') {
        this.finishSession(this.detail.conclusion);
        return;
      }

      let health = null;
      if (this.ip) health = await this.backend.probe(this.ip, this.token);
      if (this.activeRunId !== runId) return;
      if (health) {
        this.session.health = health;
        this.session.tools = health.tools || {};
      }
      const effective = health || this.session.health;
      const tools = this.session.tools || {};
      const gitState = this.session.git.state;
      const isDesktopOk = desktopOk(effective);
      const toolchainOk = Boolean(
        tools.xcode && tools.simulator_booted && tools.node && /^4\./.test(tools.yarn || ''),
      );
      const gitOk = tools.git_auth === true;

      if (this.ip) {
        this.session.url = `http://${this.ip}:6080/?t=${encodeURIComponent(this.token)}`;
      }

      if (isDesktopOk && toolchainOk && !gitOk
          && gitState !== 'configuring' && gitState !== 'failed') {
        this.configureGit();
      }

      let light = 'starting';
      if (isDesktopOk && toolchainOk && gitOk) {
        light = 'ready';
      } else if (isDesktopOk && ((effective.input_checked && !effective.input_ok)
        || !toolchainOk || gitState === 'failed')) {
        light = 'amber';
      }

      const wasReady = this.session.light === 'ready';
      this.session.light = light;

      const result = estimate(this.detail || {}, { now: Date.now() / 1000, ready: light === 'ready' });
      this.session.percent = result.percent;
      this.session.eta = result.eta;
      this.session.phase = result.phase;
      this.session.elapsed = result.elapsed;
      this.session.steps = result.steps;
      if (light === 'ready') {
        this.session.percent = 100;
        this.session.eta = 0;
        this.session.phase = 'Desktop ready';
      }

      const entry = this.settings.sessions[String(runId)];
      if (entry && this.session.url) entry.url = this.session.url;

      if (light === 'ready' && !wasReady) {
        if (entry) {
          entry.readyAt = entry.readyAt || utcNow();
          entry.url = this.session.url;
        }
        this.session.git = {
          state: 'configured',
          message: '',
          name: tools.git_name || this.session.git.name || '',
          email: tools.git_email || this.session.git.email || '',
          host: tools.git_host || this.session.git.host || '',
          login: tools.git_login || this.session.git.login || '',
        };
        this.persist();
        this.onOpenDesktop({ url: this.session.url });
        this.autoCopy();
      }
      this.push();
    } catch (error) {
      this.session.error = error.message;
      this.push();
    } finally {
      this.polling = false;
    }
  }

  async configureGit() {
    if (this.session.git.state === 'configuring') return;
    this.session.git = { ...this.session.git, state: 'configuring', message: '' };
    this.push();
    try {
      let checks = this.checkCache;
      if (!checks) {
        checks = await runChecks({ runnerRepo: this.settings.runnerRepo, settings: this.settings });
        this.applyChecks(checks);
      }
      const host = checks.host || this.setup.host || 'github.com';
      const identity = checks.identity
        || await resolveIdentity({ settings: this.settings, backend: this.backend, host });
      if (!identity.name || !identity.email) {
        throw new Error('the Git identity is incomplete; set it in Settings');
      }
      const gitToken = await this.backend.ghToken(host);
      if (!gitToken) throw new Error(`gh has no token for ${host}`);
      if (!this.ip) throw new Error('the desktop address is not known yet');
      const response = await this.backend.gitSetup(this.ip, this.token, {
        name: identity.name,
        email: identity.email,
        gitHost: host,
      });
      this.session.git = {
        state: 'configured',
        message: '',
        name: response.name || identity.name,
        email: response.email || identity.email,
        host: response.host || host,
        login: response.login || '',
      };
      // Re-read /stats so the light reflects the server's new tool facts.
      this.session.health = null;
      this.push();
      this.poll();
    } catch (error) {
      this.session.git = {
        ...this.session.git,
        state: 'failed',
        message: error.message,
      };
      this.push();
    }
  }

  async retryGit() {
    if (this.session.git.state !== 'failed') return { ok: true };
    this.session.git = { ...this.session.git, state: 'pending', message: '' };
    this.push();
    await this.poll();
    return { ok: true };
  }

  finishSession(conclusion) {
    const runId = this.activeRunId;
    this.stopPolling();
    this.session.light = 'ended';
    this.session.phase = `Session ended — ${conclusion || 'cancelled'}`;
    this.session.eta = null;
    const entry = runId ? this.settings.sessions[String(runId)] : null;
    if (entry && this.session.url) entry.url = this.session.url;
    this.activeRunId = null;
    this.settings.lastRunId = null;
    this.persist();
    this.push();
    this.refreshRuns();
  }

  async refresh() {
    await this.refreshRuns();
    await this.checkSetup();
    if (this.activeRunId) {
      await this.poll();
    } else {
      this.push();
    }
    return this.snapshot();
  }

  async refreshRuns() {
    try {
      this.runs = await this.backend.runs(20);
      this.runsError = '';
    } catch (error) {
      this.runsError = error.message;
    }
    this.push();
  }

  async cancel(runId) {
    const id = Number(runId);
    if (!Number.isInteger(id) || id <= 0) return { ok: false };
    try {
      await this.backend.cancel(id);
    } catch (error) {
      return { ok: false, error: error.message };
    }
    if (id === this.activeRunId) {
      await this.poll();
    } else {
      await this.refreshRuns();
    }
    return { ok: true };
  }

  // -- copy -------------------------------------------------------------------

  async copy({ src, name, excludeDefaults } = {}) {
    const directory = (src || this.settings.copyDir || '').trim();
    const target = copyNameFor(directory, name || this.settings.copyName);
    if (!this.session.url) {
      return { ok: false, error: 'the session is not ready yet' };
    }
    const excludes = excludeDefaults === false ? [] : DEFAULT_EXCLUDES;
    this.session.upload = {
      state: 'uploading', sent: 0, total: 0, percent: 0, message: '', path: '',
    };
    this.push();
    let lastPush = 0;
    try {
      const result = await this.backend.upload(
        this.session.url,
        directory,
        target,
        excludes,
        (sent, total) => {
          const now = Date.now();
          this.session.upload = {
            state: 'uploading',
            sent,
            total,
            percent: total ? Math.round((sent / total) * 100) : 0,
            message: `${humanBytes(sent)} of ${humanBytes(total)}`,
            path: '',
          };
          if (now - lastPush >= 120) {
            lastPush = now;
            this.push();
          }
        },
      );
      this.session.upload = {
        state: 'done',
        sent: result.sent || this.session.upload.total,
        total: result.sent || this.session.upload.total,
        percent: 100,
        message: `${result.files || result.count || 0} files · ${humanBytes(result.bytes || 0)}`,
        path: result.path || '',
      };
      this.push();
      return { ok: true, result };
    } catch (error) {
      this.session.upload = {
        state: 'failed',
        sent: this.session.upload.sent,
        total: this.session.upload.total,
        percent: this.session.upload.percent,
        message: error.message,
        path: '',
      };
      this.push();
      return { ok: false, error: error.message };
    }
  }

  autoCopy() {
    if (this.autoCopied || !this.pendingCopy) return;
    this.autoCopied = true;
    const pending = this.pendingCopy;
    this.copy({
      src: pending.dir,
      name: pending.name,
      excludeDefaults: pending.excludeDefaults,
    }).catch((error) => {
      this.session.upload = {
        ...this.session.upload, state: 'failed', message: error.message,
      };
      this.push();
    });
  }
}

module.exports = { SessionController, desktopOk, utcNow };
