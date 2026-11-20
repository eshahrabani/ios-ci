'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { desktopPhase } = require('../lib/session');

const LIVE = { light: 'starting', live: true, toolchainOk: true, gitState: 'pending' };

test('a live desktop never keeps an earlier workflow phase', () => {
  assert.equal(desktopPhase(LIVE), 'Desktop available');
});

test('the live phase names the remaining Git work', () => {
  assert.equal(desktopPhase({ ...LIVE, gitState: 'configuring' }),
    'Configuring Git credentials');
  assert.equal(desktopPhase({ ...LIVE, gitState: 'failed' }),
    'Desktop available · Git credentials failed');
});

test('the live phase names an incomplete toolchain and ready state', () => {
  assert.equal(desktopPhase({ ...LIVE, toolchainOk: false }),
    'Desktop available · finishing setup');
  assert.equal(desktopPhase({ ...LIVE, light: 'ready' }), 'Desktop ready');
});

test('no live desktop means the workflow phase is left alone', () => {
  assert.equal(desktopPhase({ ...LIVE, live: false }), '');
});
