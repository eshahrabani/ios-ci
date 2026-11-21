'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  SettingsStore, defaults, migrateSettings, sanitizeSettings,
} = require('../lib/settings');

function workspace() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ios-ci-settings-'));
  return { directory, file: path.join(directory, 'settings.json') };
}

test('a corrupt file falls back to defaults', () => {
  const { file } = workspace();
  fs.writeFileSync(file, '{ not json');
  const settings = new SettingsStore(file).load();
  assert.deepEqual(settings, defaults());
});

test('unknown keys are dropped and known ones survive', () => {
  const { file } = workspace();
  fs.writeFileSync(file, JSON.stringify({
    runnerRepo: 'owner/repo', minutes: 12, bogus: true, sessions: {},
  }));
  const settings = new SettingsStore(file).load();
  assert.equal(settings.runnerRepo, 'owner/repo');
  assert.equal(settings.minutes, 12);
  assert.equal('bogus' in settings, false);
});

test('session entries without a string token are dropped', () => {
  const settings = sanitizeSettings({
    sessions: {
      12: { token: 'a'.repeat(10) },
      13: { token: 7 },
      14: { minutes: 30 },
    },
  });
  assert.deepEqual(Object.keys(settings.sessions), ['12']);
  assert.equal(settings.sessions['12'].token, 'a'.repeat(10));
});

test('only the 20 newest numeric sessions are kept', () => {
  const sessions = {};
  for (let id = 1; id <= 25; id += 1) sessions[id] = { token: `t${id}` };
  const settings = sanitizeSettings({ sessions });
  const ids = Object.keys(settings.sessions).map(Number).sort((a, b) => a - b);
  assert.deepEqual(ids, [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25]);
});

test('the VM architecture defaults to arm64 and rejects unknown values', () => {
  assert.equal(defaults().arch, 'arm64');
  assert.equal(sanitizeSettings({}).arch, 'arm64');
  assert.equal(sanitizeSettings({ arch: 'x64' }).arch, 'x64');
  assert.equal(sanitizeSettings({ arch: 'ppc' }).arch, 'arm64');
});

test('session entries keep a known architecture', () => {
  const settings = sanitizeSettings({
    sessions: {
      12: { token: 't', arch: 'x64' },
      13: { token: 't', arch: 'ppc' },
    },
  });
  assert.equal(settings.sessions['12'].arch, 'x64');
  assert.equal(settings.sessions['13'].arch, '');
});

test('lastRunId is numeric or null', () => {
  assert.equal(sanitizeSettings({ lastRunId: 4242 }).lastRunId, 4242);
  assert.equal(sanitizeSettings({ lastRunId: 'nope' }).lastRunId, null);
  assert.equal(sanitizeSettings({ lastRunId: -3 }).lastRunId, null);
});

test('save writes mode 0600 and round-trips', () => {
  const { file } = workspace();
  const store = new SettingsStore(file);
  store.save({ runnerRepo: 'owner/repo', minutes: 45, bogus: 1 });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  const settings = store.load();
  assert.equal(settings.minutes, 45);
  assert.equal(settings.runnerRepo, 'owner/repo');
});

test('legacy settings are mapped once', () => {
  const mapped = migrateSettings({
    runner_repo: 'owner/repo',
    minutes: 60,
    upload_dir: '/tmp/project',
    exclude_defaults: false,
    last_run_id: 99,
    sessions: {
      99: { token: 'tok', minutes: 60, started_at: '2024-01-02T03:04:05Z',
        ready_at: '2024-01-02T03:05:05Z', url: 'http://host:6080/?t=tok' },
    },
  });
  assert.equal(mapped.runnerRepo, 'owner/repo');
  assert.equal(mapped.copyDir, '/tmp/project');
  assert.equal(mapped.excludeDefaults, false);
  assert.equal(mapped.lastRunId, 99);
  assert.equal(mapped.sessions['99'].startedAt, '2024-01-02T03:04:05Z');
  assert.equal(mapped.sessions['99'].readyAt, '2024-01-02T03:05:05Z');
});
