'use strict';
// Weighted progress and ETA for one ios-ci desktop session.
//
// The weights come from measured runs on macos-26: the runner queue dominates when the
// service is busy, `Prepare the desktop` dominates the job (Xcode, an iOS runtime and
// the simulator now provision there), and `Open the desktop` is short once the dead
// waits are gone. A step that overruns its weight keeps adding to the ETA at 20% of its
// own elapsed time, so the estimate grows instead of sticking at the prior.

const QUEUE_PRIOR = 20.0;
const STEP_PRIORS = {
  'Set up job': 3.0,
  'Validate the session length': 1.0,
  'Restore the screen streamer': 3.0,
  'Prepare the desktop': 90.0,
  'Open the desktop': 12.0,
};
const READY_STEP = 'Open the desktop';
const DEFAULT_PRIOR = 5.0;
// Steps that run after the session was handed over: they are not part of "ready" and
// would otherwise hold the bar below 100% for the whole hold.
const AFTER_STEPS = ['Clean up', 'Complete job'];
const UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function parseUtc(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== 'string' || !UTC.test(value)) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function seconds(start, end) {
  if (!start || !end) return null;
  return Math.max(0, (end.getTime() - start.getTime()) / 1000);
}

/** Python's round(): halves go to the even integer, so durations match the old model. */
function roundHalfEven(value) {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** A compact duration: 8s, 1m20s, 1h05m. */
function shortDuration(value) {
  const total = roundHalfEven(Number(value) || 0);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m${String(total % 60).padStart(2, '0')}s`;
  return `${Math.floor(total / 3600)}h${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}m`;
}

function stepPrior(name) {
  return STEP_PRIORS[name] ?? DEFAULT_PRIOR;
}

function jobOf(run) {
  for (const job of run.jobs || []) {
    if (job.name !== 'plan') return job;
  }
  return null;
}

function asDate(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  return parseUtc(value);
}

/** Percent/ETA/phase for one `gh run view --json createdAt,status,conclusion,jobs`. */
function estimate(run, options = {}) {
  const now = options.now === undefined ? Date.now() / 1000 : options.now;
  const ready = options.ready === true;
  const createdAt = asDate(run.createdAt);
  const elapsed = seconds(createdAt, new Date(now * 1000)) || 0;
  const job = jobOf(run);
  const started = job ? asDate(job.startedAt) : null;
  const status = run.status || 'queued';

  if (!job || !started) {
    // Nothing has been scheduled yet: only the queue is measurable. 95% is the
    // ceiling, so the bar never claims ready before a step has run.
    let phase = 'Waiting for a macOS runner';
    const fraction = Math.min(0.95, elapsed / QUEUE_PRIOR);
    const remaining = Math.max(QUEUE_PRIOR - elapsed, elapsed * 0.2);
    let eta = status === 'completed' && !ready ? null : remaining;
    if (ready) {
      return { percent: 100, eta: 0, phase: 'Desktop ready', elapsed, steps: [] };
    }
    if (status === 'completed') {
      eta = null;
      phase = `Session ${run.conclusion || 'ended'}`;
    }
    return {
      percent: 100 * fraction, eta, phase, elapsed, steps: [],
    };
  }

  // The job is running, so the queue is behind us and counts as complete.
  const steps = [];
  let weightTotal = QUEUE_PRIOR;
  let earned = QUEUE_PRIOR;
  let eta = 0;
  let pastReady = false;
  let phase = 'Starting';
  let phaseSet = false;
  for (const entry of job.steps || []) {
    const name = entry.name || '';
    const stepStatus = entry.status || 'pending';
    const prior = stepPrior(name);
    const after = pastReady || AFTER_STEPS.includes(name) || name.startsWith('Post ');
    if (name === READY_STEP) pastReady = true;
    const stepStarted = asDate(entry.startedAt);
    const stepDone = asDate(entry.completedAt);
    const stepElapsed = seconds(stepStarted, stepDone
      || (stepStatus === 'in_progress' ? new Date(now * 1000) : null));
    let stepEstimate = 0;
    let detail;
    if (after) {
      detail = 'after the session';
    } else if (stepStatus === 'completed' || stepStatus === 'skipped') {
      detail = shortDuration(stepElapsed || 0);
    } else {
      let fraction;
      if (stepStatus === 'in_progress') {
        const spent = stepElapsed || 0;
        fraction = prior ? Math.min(0.95, spent / prior) : 0.95;
        stepEstimate = Math.max(prior - spent, spent * 0.2);
        detail = `${shortDuration(spent)} / ~${shortDuration(stepEstimate)}`;
      } else {
        fraction = 0;
        stepEstimate = prior;
        detail = `~${shortDuration(prior)}`;
      }
      if (!phaseSet) {
        phase = name || phase;
        phaseSet = true;
      }
    }
    if (!after) {
      weightTotal += prior;
      if (stepStatus === 'completed' || stepStatus === 'skipped') {
        earned += prior;
      } else if (stepStatus === 'in_progress') {
        const spent = stepElapsed || 0;
        earned += prior * (prior ? Math.min(0.95, spent / prior) : 0.95);
      }
      eta += stepEstimate;
    }
    steps.push({
      name,
      status: stepStatus,
      conclusion: entry.conclusion,
      elapsed: stepElapsed,
      estimate: stepEstimate,
      detail,
    });
  }

  if (!phaseSet && phase === 'Starting') {
    // Every listed step is done but the job is not: the next one is not published
    // yet, so say what is happening rather than show a stale step name.
    phase = 'Wrapping up';
  }

  let percent = weightTotal ? 100 * earned / weightTotal : 0;
  if (ready) {
    percent = 100;
    eta = 0;
    phase = 'Desktop ready';
    for (const step of steps) {
      if (step.name === READY_STEP) {
        step.status = 'completed';
        step.conclusion = 'success';
        step.estimate = 0;
        step.detail = 'done';
      }
    }
  } else if (status === 'completed') {
    eta = null;
    phase = `Session ${run.conclusion || 'ended'}`;
  }
  return { percent: Math.min(100, percent), eta, phase, elapsed, steps };
}

module.exports = {
  AFTER_STEPS,
  DEFAULT_PRIOR,
  QUEUE_PRIOR,
  READY_STEP,
  STEP_PRIORS,
  estimate,
  jobOf,
  parseUtc,
  seconds,
  shortDuration,
  stepPrior,
};
