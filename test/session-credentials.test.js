'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { SessionController } = require('../lib/session');
const { defaults } = require('../lib/settings');

function controllerWith(copyDir) {
  const settings = { ...defaults(), copyDir };
  return new SessionController({
    store: { load: () => ({ ...settings }), save: (value) => value },
    version: 'test',
  });
}

test('Git credentials include the gh host, GitLab, and copied remotes', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ios-ci-repo-'));
  fs.mkdirSync(path.join(directory, '.git'));
  fs.writeFileSync(path.join(directory, '.git', 'config'), [
    '[remote "origin"]',
    '\turl = git@git.example.com:ada/project.git',
    '',
  ].join('\n'));
  try {
    const controller = controllerWith(directory);
    controller.backend = {
      ghToken: async () => 'gh-token',
      gitCredential: async (host) => (host === 'git.example.com'
        ? { username: 'ada', password: 'local-token' }
        : null),
    };
    const result = await controller.gitCredentials({ host: 'github.com', login: 'ada' });
    assert.equal(result.githubHost, 'github.com');
    assert.equal(result.login, 'ada');
    assert.deepEqual(result.credentials, [
      { host: 'github.com', username: 'x-access-token', token: 'gh-token' },
      { host: 'git.example.com', username: 'ada', token: 'local-token' },
    ]);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('Git credentials fail clearly when no local credential exists', async () => {
  const controller = controllerWith('');
  controller.backend = {
    ghToken: async () => '',
    gitCredential: async () => null,
  };
  await assert.rejects(
    controller.gitCredentials({ host: 'github.com', login: 'ada' }),
    /no local HTTPS Git credential/,
  );
});
