'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { estimate } = require('../lib/progress');

const QUEUE_RUN = {
  createdAt: '2024-01-02T12:00:20Z',
  status: 'queued',
  conclusion: null,
  jobs: [],
};

const RUNNING_RUN = {
  createdAt: '2024-01-02T12:00:00Z',
  status: 'in_progress',
  conclusion: null,
  jobs: [{
    name: 'macos-desktop',
    startedAt: '2024-01-02T12:00:20Z',
    steps: [
      { name: 'Set up job', status: 'completed', conclusion: 'success',
        startedAt: '2024-01-02T12:00:20Z', completedAt: '2024-01-02T12:00:22Z' },
      { name: 'Validate the session length', status: 'completed', conclusion: 'success',
        startedAt: '2024-01-02T12:00:22Z', completedAt: '2024-01-02T12:00:23Z' },
      { name: 'Prepare the desktop', status: 'in_progress', startedAt: '2024-01-02T12:00:24Z' },
      { name: 'Open the desktop', status: 'pending' },
    ],
  }],
};

const NOW = Date.parse('2024-01-02T12:00:30Z') / 1000;

test('a queued run measures only the queue', () => {
  const result = estimate(QUEUE_RUN, { now: NOW });
  assert.equal(result.percent, 50);
  assert.equal(result.eta, 10);
  assert.equal(result.phase, 'Waiting for a macOS runner');
  assert.deepEqual(result.steps, []);
  assert.equal(result.elapsed, 10);
});

test('a running job weights completed, current and future steps', () => {
  const result = estimate(RUNNING_RUN, { now: NOW });
  assert.equal(result.percent, 23.80952380952381);
  assert.equal(result.eta, 96);
  assert.equal(result.phase, 'Prepare the desktop');
  const prepare = result.steps.find((step) => step.name === 'Prepare the desktop');
  assert.equal(prepare.estimate, 84);
  assert.equal(prepare.detail, '6s / ~1m24s');
  assert.equal(result.steps.length, 4);
});

test('ready forces 100 percent and closes the ready step', () => {
  const result = estimate(RUNNING_RUN, { now: NOW, ready: true });
  assert.equal(result.percent, 100);
  assert.equal(result.eta, 0);
  assert.equal(result.phase, 'Desktop ready');
  const open = result.steps.find((step) => step.name === 'Open the desktop');
  assert.equal(open.status, 'completed');
  assert.equal(open.detail, 'done');
});

test('after-session steps stay out of the bar', () => {
  const run = {
    createdAt: '2024-01-02T12:00:00Z',
    status: 'in_progress',
    jobs: [{
      name: 'macos-desktop',
      startedAt: '2024-01-02T12:00:00Z',
      steps: [
        { name: 'Open the desktop', status: 'completed',
          startedAt: '2024-01-02T12:00:00Z', completedAt: '2024-01-02T12:00:05Z' },
        { name: 'Clean up', status: 'in_progress', startedAt: '2024-01-02T12:00:05Z' },
      ],
    }],
  };
  const result = estimate(run, { now: NOW });
  assert.equal(result.phase, 'Wrapping up');
  assert.equal(result.eta, 0);
  const clean = result.steps.find((step) => step.name === 'Clean up');
  assert.equal(clean.detail, 'after the session');
});
