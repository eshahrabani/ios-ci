'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { pickIdentity } = require('../lib/setup');

const GITHUB_USER = {
  name: 'Ada Lovelace', login: 'ada', id: 4242, email: 'ada@example.com',
};

test('settings override wins over local git config and GitHub', () => {
  const identity = pickIdentity({
    settings: { gitName: 'Grace Hopper', gitEmail: 'grace@example.com' },
    gitName: 'Local Name',
    gitEmail: 'local@example.com',
    ghUser: GITHUB_USER,
  });
  assert.equal(identity.name, 'Grace Hopper');
  assert.equal(identity.email, 'grace@example.com');
  assert.equal(identity.nameSource, 'settings');
  assert.equal(identity.emailSource, 'settings');
});

test('local git config wins when the settings are blank', () => {
  const identity = pickIdentity({
    settings: {},
    gitName: 'Local Name',
    gitEmail: 'local@example.com',
    ghUser: GITHUB_USER,
  });
  assert.equal(identity.name, 'Local Name');
  assert.equal(identity.email, 'local@example.com');
  assert.equal(identity.emailSource, 'git config');
});

test('GitHub fills both fields when nothing local exists', () => {
  const identity = pickIdentity({ settings: {}, ghUser: GITHUB_USER });
  assert.equal(identity.name, 'Ada Lovelace');
  assert.equal(identity.email, 'ada@example.com');
  assert.equal(identity.nameSource, 'GitHub');
  assert.equal(identity.emailSource, 'GitHub');
});

test('a missing GitHub email becomes a noreply address', () => {
  const identity = pickIdentity({
    settings: {},
    ghUser: { ...GITHUB_USER, name: '', email: '' },
  });
  assert.equal(identity.name, 'ada');
  assert.equal(identity.email, '4242+ada@users.noreply.github.com');
});

test('fields resolve independently', () => {
  const identity = pickIdentity({
    settings: { gitName: 'Grace Hopper' },
    gitEmail: 'local@example.com',
    ghUser: GITHUB_USER,
  });
  assert.equal(identity.name, 'Grace Hopper');
  assert.equal(identity.email, 'local@example.com');
  assert.equal(identity.nameSource, 'settings');
  assert.equal(identity.emailSource, 'git config');
});
