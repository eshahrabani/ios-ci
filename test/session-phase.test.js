'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { desktopPhase, toolchainReady } = require('../lib/session');

const LIVE = { light: 'starting', live: true, toolchainOk: true, gitState: 'pending' };
const TOOLS = {
  xcode: '26.0',
  ios_runtime: '26.0',
  simulator_udid: 'ABC-123',
  simulator_booted: false,
  node: 'v22.11.0',
  yarn: '4.18.1',
};

test('a shut-down simulator still completes the toolchain', () => {
  assert.equal(toolchainReady(TOOLS), true);
  assert.equal(toolchainReady({ ...TOOLS, simulator_booted: true }), true);
  assert.equal(toolchainReady({ ...TOOLS, simulator_udid: '' }), false);
  assert.equal(toolchainReady({ ...TOOLS, ios_runtime: '' }), false);
});

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
