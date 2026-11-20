'use strict';
/* global window, document, setTimeout, clearTimeout, setInterval */

const api = window.iosci;

const GH_INSTALL = 'https://cli.github.com';
const TAILSCALE_INSTALL = 'https://tailscale.com/download';
const TAILSCALE_KEYS = 'https://login.tailscale.com/admin/settings/keys';
const GH_DEVICE = 'https://github.com/login/device';
const PRESETS = [15, 30, 60, 120];

const ICON_PATHS = {
  play: '<path d="M8.5 5.5v13L18 12z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M9 20h6M12 16v4"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  sliders: '<path d="M4 7h9M18 7h2M4 17h5M13 17h7"/><circle cx="15.5" cy="7" r="2"/><circle cx="10.5" cy="17" r="2"/>',
  check: '<path d="M4.5 12.5l5 5 10-11"/>',
  alert: '<path d="M12 3.5L21 20H3z"/><path d="M12 9v5M12 17.2v.01"/>',
  'arrow-right': '<path d="M4 12h15M13 6l6 6-6 6"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 4H5.5A1.5 1.5 0 004 5.5V15"/>',
  external: '<path d="M14 4h6v6M20 4l-8 8"/><path d="M19 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1h5"/>',
  expand: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  key: '<circle cx="8" cy="12" r="4"/><path d="M12 12h9M18 12v4M15 12v3"/>',
  github: '<path d="M9 19c-4 1.5-4-2-6-2.5M15 21v-3.4c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.7 4.7 0 00-1.3-3.2 4.4 4.4 0 00-.1-3.2s-1-.3-3.3 1.3a11.4 11.4 0 00-6 0C7.1 2.9 6.1 3.2 6.1 3.2a4.4 4.4 0 00-.1 3.2A4.7 4.7 0 004.7 9.6c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>',
  terminal: '<path d="M4 6l5 6-5 6M11 18h9"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  wand: '<path d="M4 20L15 9"/><path d="M15 4l1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/>',
  refresh: '<path d="M20 11a8 8 0 10-2.3 6.3"/><path d="M20 5v6h-6"/>',
};

function icon(name) {
  const path = ICON_PATHS[name] || '';
  return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"`
    + ` stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}

const $ = (id) => document.getElementById(id);

const ui = {
  state: null,
  view: 'session',
  forceWizard: false,
  authCode: null,
  setupLog: [],
  logOpen: false,
  secretError: '',
  rowPending: '',
  starting: false,
  startError: '',
  copyPending: false,
  duration: 30,
  custom: false,
  desktopTarget: null,
  webviewUrl: '',
  webviewLoaded: false,
  webviewFailed: '',
  webviewRetry: false,
  webviewTimer: null,
  hintShown: false,
  hintTimer: null,
  fullscreen: false,
  transitionTimer: null,
  checksSignature: '',
  timelineSignature: '',
  chipsSignature: '',
  gitSignature: '',
  historySignature: '',
  settingsTimer: null,
  savedTimer: null,
  focusKey: false,
};

function shortDuration(value) {
  const total = Math.round(Number(value) || 0);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m${String(total % 60).padStart(2, '0')}s`;
  return `${Math.floor(total / 3600)}h${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}m`;
}

function hhmm(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function isTyping() {
  const active = document.activeElement;
  return Boolean(active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA'
    || active.tagName === 'SELECT' || active.isContentEditable));
}

function setValue(input, value) {
  if (!input || document.activeElement === input) return;
  if (input.value !== value) input.value = value;
}

function setChecked(input, value) {
  if (!input) return;
  if (input.checked !== value) input.checked = value;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message, iconName = 'check') {
  const container = $('toasts');
  while (container.children.length >= 2) container.firstElementChild.remove();
  const node = el('div', 'toast');
  node.innerHTML = icon(iconName);
  node.appendChild(el('span', '', message));
  container.appendChild(node);
  setTimeout(() => node.remove(), 2500);
}

function copyText(text, message = 'Copied') {
  api.copyText(text);
  toast(message);
}

function openExternal(url) {
  api.openExternal(url);
}

// -- state ------------------------------------------------------------------

function applyState(next) {
  if (!next || typeof next !== 'object') return;
  const previous = ui.state;
  ui.state = next;
  if (!previous) {
    const minutes = Number(next.settings.minutes) || 30;
    if (PRESETS.includes(minutes)) {
      ui.duration = minutes;
      ui.custom = false;
    } else {
      ui.duration = minutes;
      ui.custom = true;
      setValue($('custom-minutes-input'), String(minutes));
    }
    setValue($('copy-dir'), next.settings.copyDir || '');
    setValue($('copy-name'), next.settings.copyName || '');
    setChecked($('copy-excludes'), next.settings.excludeDefaults !== false);
    if (next.session.light === 'ready' && next.session.url) {
      ui.desktopTarget = { runId: next.session.runId, url: next.session.url };
      setView('desktop');
    }
  }
  if (!next.setup.checking && next.setup.complete && previous
      && !previous.setup.complete && !ui.transitionTimer) {
    showWizardSuccessThenSession();
  }
  render();
}

function showWizardSuccessThenSession() {
  const finish = () => {
    ui.transitionTimer = null;
    ui.forceWizard = false;
    render();
  };
  if (reducedMotion()) {
    finish();
    return;
  }
  ui.transitionTimer = setTimeout(finish, 600);
}

function snapshot() {
  return ui.state;
}

// -- chrome -----------------------------------------------------------------

function render() {
  if (!ui.state) return;
  renderChrome();
  renderSession();
  renderDesktop();
  renderHistory();
  renderSettings();
}

const VIEW_TITLES = {
  session: 'Session', desktop: 'Desktop', history: 'History', settings: 'Settings',
};

function renderChrome() {
  const state = snapshot();
  if (!state) return;
  const { setup, session } = state;
  $('page-title').textContent = VIEW_TITLES[ui.view] || 'Session';

  document.querySelectorAll('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === ui.view);
    item.setAttribute('aria-current', item.dataset.view === ui.view ? 'page' : 'false');
  });

  const header = $('header-status');
  const map = {
    idle: ['Idle', ''],
    starting: ['Starting', 'starting'],
    ready: ['Ready', 'ready'],
    amber: ['Needs attention', 'warning'],
    ended: ['Ended', 'ended'],
  };
  const [text, stateKey] = map[session.light] || map.idle;
  $('header-status-text').textContent = text;
  header.dataset.state = stateKey;

  $('header-timer').textContent = session.light === 'ready' ? remainingText(activeEntry()) : '';
  $('header-open').hidden = !session.url || ui.view === 'desktop';

  const pill = $('rail-setup');
  if (setup.checking && !setup.checks.length) {
    $('rail-setup-text').textContent = 'Checking…';
    pill.dataset.state = '';
  } else if (setup.complete) {
    $('rail-setup-text').textContent = 'Ready';
    pill.dataset.state = 'ready';
  } else {
    $('rail-setup-text').textContent = 'Setup needed';
    pill.dataset.state = 'warning';
  }
  pill.title = setup.complete ? 'Setup is complete' : 'Open setup';

  $('rail-repo').textContent = state.settings.runnerRepo;
  $('rail-login').textContent = setup.login ? `@${setup.login}` : '';
  $('rail-version').textContent = state.version ? `v${state.version}` : '';
}

function activeEntry() {
  const state = snapshot();
  if (!state || !state.session.runId) return null;
  return state.sessions.find((entry) => entry.runId === state.session.runId) || null;
}

function remainingText(entry) {
  if (!entry || !entry.readyAt) return '';
  const end = Date.parse(entry.readyAt) + (Number(entry.minutes) || 0) * 60_000;
  if (!Number.isFinite(end)) return '';
  const left = end - Date.now();
  if (left <= 0) return 'ending';
  return `${Math.ceil(left / 60_000)} min left`;
}

// -- session view -----------------------------------------------------------

function renderSession() {
  const state = snapshot();
  if (!state) return;
  const { setup } = state;
  const loading = setup.checking && !setup.checks.length && !setup.complete;
  $('session-loading').hidden = !loading;
  const wizard = !loading && (!setup.complete || ui.forceWizard);
  $('wizard').hidden = !wizard;
  $('session-main').hidden = wizard || loading;
  if (wizard) renderWizard();
  else if (!loading) renderSessionMain();
}

function renderWizard() {
  const state = snapshot();
  const { setup } = state;
  const checks = setup.checks || [];
  const required = checks.filter((check) => check.required);
  const passed = required.filter((check) => check.ok).length;
  $('check-count').textContent = checks.length
    ? `${passed} of ${required.length} ready`
    : 'Checking…';

  const segments = $('check-segments');
  segments.textContent = '';
  for (const check of required) {
    const segment = el('span', 'segment');
    if (check.ok) segment.classList.add('done');
    else segment.classList.add('failed');
    segments.appendChild(segment);
  }

  $('wizard-success').hidden = !setup.complete;
  $('wizard-fix').hidden = setup.complete;
  $('wizard-done').hidden = !setup.complete;
  const fix = $('wizard-fix');
  fix.disabled = setup.fixing;
  fix.innerHTML = setup.fixing
    ? '<span class="spinner" aria-hidden="true"></span><span>Working…</span>'
    : `${icon('wand')}<span>Fix what I can</span>`;
  $('wizard-recheck').disabled = setup.fixing || setup.checking;
  $('setup-log').hidden = !ui.logOpen;
  $('setup-log-toggle').textContent = ui.logOpen ? 'Hide details' : 'Show details';
  $('setup-log-toggle').setAttribute('aria-expanded', String(ui.logOpen));
  $('setup-log').textContent = ui.setupLog.join('\n') || 'No output yet.';

  renderChecks(checks);
}

function checkActionLabel(check) {
  if (check.id === 'gh') return 'Install';
  if (check.id === 'gh-auth') return 'Sign in';
  if (check.id === 'tailscale') return 'Download';
  if (check.id === 'tailnet') return 'Connect';
  if (check.id === 'repo') return check.reason === 'workflow-disabled' ? 'Enable' : 'Change';
  if (check.id === 'secret') return 'Enter key';
  if (check.id === 'git') return 'Edit';
  return '';
}

function checksSignature(checks) {
  return JSON.stringify([
    checks.map((check) => [check.id, check.ok, check.required, check.detail,
      check.reason, check.warning]),
    ui.authCode && ui.authCode.code,
    snapshot().setup.needs,
    snapshot().setup.fixing,
    ui.rowPending,
    ui.secretError,
  ]);
}

function renderChecks(checks) {
  const signature = checksSignature(checks || []);
  if (signature === ui.checksSignature) return;
  ui.checksSignature = signature;
  const list = $('check-list');
  list.textContent = '';
  for (const check of checks || []) {
    const row = el('div', `check-row ${check.ok ? 'passed' : 'failed'}`);
    row.dataset.id = check.id;
    const main = el('div', 'check-main');
    const state = el('span', 'check-state');
    state.innerHTML = check.ok ? icon('check') : icon('alert');
    main.appendChild(state);
    const text = el('div', 'check-text');
    text.appendChild(el('span', 'check-title', check.label));
    text.appendChild(el('span', 'check-detail', check.detail || ''));
    main.appendChild(text);
    const actions = el('div', 'check-action');
    if (!check.ok) {
      const label = checkActionLabel(check);
      const button = el('button', 'btn secondary small', label);
      if (ui.rowPending === check.id) {
        button.disabled = true;
        button.textContent = '';
        button.appendChild(el('span', 'spinner'));
      } else if (snapshot().setup.fixing) {
        button.disabled = true;
      }
      button.addEventListener('click', () => runCheckAction(check));
      actions.appendChild(button);
    }
    main.appendChild(actions);
    row.appendChild(main);

    if (!check.ok) {
      const extra = el('div', 'check-extra');
      if (check.id === 'gh-auth') renderAuthExtra(extra);
      else if (check.id === 'secret') renderSecretExtra(extra);
      else if (check.id === 'repo' && check.reason !== 'workflow-disabled') {
        extra.appendChild(el('span', 'field-note', 'Change the repository in Settings.'));
      } else if (check.id === 'git') {
        extra.appendChild(el('span', 'field-note', 'Set the identity in Settings.'));
      }
      if (extra.childNodes.length) row.appendChild(extra);
    }
    list.appendChild(row);
  }
  if (ui.focusKey) {
    ui.focusKey = false;
    const input = list.querySelector('input[type="password"]');
    if (input) input.focus();
  }
}

function renderAuthExtra(extra) {
  if (ui.authCode && ui.authCode.code) {
    const line = el('div', 'auth-code');
    line.appendChild(el('span', 'code', ui.authCode.code));
    const copy = el('button', 'btn secondary small', 'Copy code');
    copy.addEventListener('click', () => copyText(ui.authCode.code, 'Code copied'));
    line.appendChild(copy);
    const open = el('button', 'btn secondary small', 'Open GitHub');
    open.addEventListener('click', () => openExternal(ui.authCode.url || GH_DEVICE));
    line.appendChild(open);
    extra.appendChild(line);
  }
  if (snapshot().setup.needs === 'terminal-gh-auth') {
    const line = el('div', 'command-line');
    line.appendChild(el('span', 'command', 'gh auth login'));
    const copy = el('button', 'btn secondary small', 'Copy command');
    copy.addEventListener('click', () => copyText('gh auth login', 'Command copied'));
    line.appendChild(copy);
    const recheck = el('button', 'btn ghost small', 'Re-check');
    recheck.addEventListener('click', async () => {
      const state = await api.checkSetup();
      applyState(state);
    });
    line.appendChild(recheck);
    extra.appendChild(line);
    extra.appendChild(el('span', 'field-note',
      'The device flow needs a terminal. Run the command, then re-check.'));
  } else if (!ui.authCode) {
    extra.appendChild(el('span', 'field-note',
      'Sign in opens GitHub and shows a one-time code to copy.'));
  }
}

function renderSecretExtra(extra) {
  const field = el('div', 'field');
  field.appendChild(el('label', 'field-label', 'Reusable auth key'));
  const row = el('div', 'input-row');
  const input = document.createElement('input');
  input.type = 'password';
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.placeholder = 'tskey-auth-…';
  input.id = 'setup-key';
  row.appendChild(input);
  const save = el('button', 'btn secondary', 'Save key');
  save.addEventListener('click', () => saveKey(input.value));
  row.appendChild(save);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') saveKey(input.value);
  });
  field.appendChild(row);
  if (ui.secretError) {
    const note = el('p', 'field-note error-text', ui.secretError);
    field.appendChild(note);
  }
  const create = el('button', 'link-button', 'Create a key');
  create.addEventListener('click', () => openExternal(TAILSCALE_KEYS));
  field.appendChild(create);
  extra.appendChild(field);
}

async function saveKey(value) {
  const key = (value || '').trim();
  if (!key) {
    ui.secretError = 'Paste a reusable auth key (tskey-auth-…).';
    renderChecks(snapshot().setup.checks || []);
    return;
  }
  ui.secretError = '';
  const result = await api.setSecret(key);
  if (result && result.error) {
    ui.secretError = result.error;
    const state = { ...result };
    delete state.error;
    applyState(state);
    renderChecks(snapshot().setup.checks || []);
    return;
  }
  applyState(result);
  toast('Key saved');
}

async function runCheckAction(check) {
  if (check.id === 'gh') {
    openExternal(GH_INSTALL);
    return;
  }
  if (check.id === 'tailscale') {
    openExternal(TAILSCALE_INSTALL);
    return;
  }
  if (check.id === 'repo' && check.reason !== 'workflow-disabled') {
    openSettings('repo');
    return;
  }
  if (check.id === 'git') {
    openSettings('git-name');
    return;
  }
  if (check.id === 'secret') {
    const input = document.querySelector('.check-row[data-id="secret"] input');
    if (input) input.focus();
    return;
  }
  ui.rowPending = check.id;
  ui.secretError = '';
  renderWizard();
  try {
    const result = await api.fixCheck(check.id);
    if (result && result.error === 'tailscale-auth-key') ui.focusKey = true;
    const state = await api.getState();
    applyState(state);
  } finally {
    ui.rowPending = '';
    renderWizard();
  }
}

function renderSessionMain() {
  const state = snapshot();
  const { session, setup } = state;
  const light = session.light;

  $('ready-banner').hidden = !(light === 'ready' && session.url);

  const showStart = light === 'idle' || light === 'ended';
  $('start-card').hidden = !showStart;
  $('inclusions').hidden = !showStart;
  $('session-card').hidden = light === 'idle';
  $('start-error').hidden = !(showStart && (ui.startError || session.error));
  $('start-error').textContent = ui.startError || session.error || '';
  $('start-repo').textContent = state.settings.runnerRepo;

  for (const pill of document.querySelectorAll('.duration-pill')) {
    const preset = Number(pill.dataset.minutes);
    const selected = ui.custom
      ? pill.dataset.custom === 'true'
      : preset === ui.duration;
    pill.setAttribute('aria-checked', String(selected));
    pill.tabIndex = selected ? 0 : -1;
  }
  $('custom-minutes').hidden = !ui.custom;

  const startButton = $('start-button');
  startButton.disabled = ui.starting || !setup.complete;
  startButton.innerHTML = ui.starting
    ? '<span class="spinner" aria-hidden="true"></span><span class="btn-label">Starting…</span>'
    : `<span class="btn-label">Start session</span>${icon('arrow-right')}`;

  $('copy-now').disabled = ui.copyPending || !session.url;
  $('copy-status').textContent = copyStatus(session);
  const upload = session.upload || {};
  const copyBar = $('copy-progress');
  copyBar.hidden = !(upload.state === 'uploading' || upload.state === 'done' || upload.state === 'failed');
  $('copy-progress-fill').style.width = `${Math.max(0, Math.min(100, upload.percent || 0))}%`;

  if (light === 'idle') return;

  const statusMap = {
    starting: ['Starting', 'starting'],
    ready: ['Ready', 'ready'],
    amber: ['Needs attention', 'warning'],
    ended: ['Ended', 'ended'],
  };
  const [statusText, statusState] = statusMap[light] || statusMap.starting;
  $('session-status-text').textContent = statusText;
  $('session-status').dataset.state = statusState;
  $('session-run').textContent = session.runId ? `Run #${session.runId}` : '';
  $('session-cancel').hidden = light === 'ended';
  $('session-cancel').disabled = !session.runId;
  $('session-phase').textContent = session.phase || 'Starting';
  const percent = Math.max(0, Math.min(100, Math.round(session.percent || 0)));
  const eta = session.eta === null || session.eta === undefined
    ? 'ETA unknown'
    : `ETA ~${shortDuration(session.eta)}`;
  $('session-meta').textContent =
    `${percent}% · ${shortDuration(session.elapsed || 0)} · ${eta}`;

  const progress = $('session-progress');
  const indeterminate = !(session.steps || []).length && light === 'starting';
  progress.classList.toggle('indeterminate', indeterminate);
  progress.setAttribute('aria-valuenow', String(percent));
  progress.setAttribute('aria-valuetext', `${session.phase || 'Starting'} (${percent}%)`);
  $('session-progress-fill').style.width = `${indeterminate ? 35 : percent}%`;
  $('session-percent').textContent = `${percent}%`;

  renderTimeline(session);
  renderChips(session);
  renderGitLine(session);

  const error = session.error;
  $('session-error').hidden = !error;
  $('session-error-text').textContent = error || '';
  $('error-run').hidden = !session.runId;
  $('error-run').onclick = () => {
    if (session.runId) {
      openExternal(`https://github.com/${state.settings.runnerRepo}/actions/runs/${session.runId}`);
    }
  };
  $('error-retry').onclick = () => api.refresh().then(applyState);
  $('error-setup').onclick = () => {
    openSetup();
    api.checkSetup().then(applyState);
  };

  const uploadLine = $('session-upload');
  uploadLine.hidden = !(upload.state && upload.state !== 'idle');
  if (upload.state && upload.state !== 'idle') {
    $('session-upload-fill').style.width = `${Math.max(0, Math.min(100, upload.percent || 0))}%`;
    const prefix = upload.state === 'failed' ? 'Copy failed: '
      : upload.state === 'done' ? 'Copied · ' : '';
    $('session-upload-text').textContent = `${prefix}${upload.message || ''}`;
    $('session-upload-text').classList.toggle('error-text', upload.state === 'failed');
  }
}

function copyStatus(session) {
  const upload = session.upload || {};
  if (upload.state === 'uploading') return upload.message || 'Copying…';
  if (upload.state === 'done') return `Copied ${upload.message || ''}`.trim();
  if (upload.state === 'failed') return upload.message || 'Copy failed';
  if (!session.url) return 'Available once the desktop is ready.';
  return '';
}

function renderTimeline(session) {
  const signature = JSON.stringify((session.steps || []).map((step) =>
    [step.name, step.status, step.conclusion, step.detail]));
  if (signature === ui.timelineSignature) return;
  ui.timelineSignature = signature;
  const list = $('session-timeline');
  list.textContent = '';
  const after = [];
  for (const step of session.steps || []) {
    if (step.detail === 'after the session') {
      after.push(step.name);
      continue;
    }
    list.appendChild(timelineRow(step));
  }
  if (after.length) {
    const collapsed = {
      name: after.join(', '),
      status: 'pending',
      detail: 'after the session',
      elapsed: null,
    };
    const row = timelineRow(collapsed);
    row.classList.add('after');
    list.appendChild(row);
  }
}

function timelineRow(step) {
  const row = el('li', 'timeline-row');
  const statusText = step.status === 'completed' || step.status === 'skipped' ? 'completed'
    : step.status === 'in_progress' ? 'in progress' : 'pending';
  row.setAttribute('aria-label', `${step.name || ''}: ${statusText}`);
  if (step.status === 'in_progress') row.classList.add('current');
  else if (step.status === 'completed' || step.status === 'skipped') row.classList.add('done');
  const state = el('span', 'timeline-state');
  if (step.status === 'completed' || step.status === 'skipped') {
    state.innerHTML = icon('check');
  } else if (step.status === 'in_progress') {
    state.appendChild(el('span', 'spinner'));
  } else {
    state.appendChild(el('span', 'dot'));
  }
  row.appendChild(state);
  row.appendChild(el('span', 't-name', step.name || ''));
  if (step.detail) row.appendChild(el('span', 't-detail', step.detail));
  return row;
}

function renderChips(session) {
  const tools = session.tools || {};
  const hasFacts = Object.keys(tools).length > 0;
  const signature = JSON.stringify([hasFacts, tools.xcode, tools.ios_runtime,
    tools.simulator, tools.simulator_booted, tools.node, tools.yarn, tools.git]);
  if (signature === ui.chipsSignature) return;
  ui.chipsSignature = signature;
  const box = $('session-chips');
  box.textContent = '';
  if (!hasFacts) {
    for (let index = 0; index < 3; index += 1) box.appendChild(el('span', 'skeleton'));
    return;
  }
  const chip = (label, value, ok) => {
    const node = el('span', `chip${ok ? '' : ' warning'}`);
    node.innerHTML = ok ? icon('check') : icon('alert');
    node.appendChild(el('span', '', label));
    if (value) node.appendChild(el('span', 'chip-version', value));
    return node;
  };
  box.appendChild(chip('Xcode', tools.xcode || 'missing', Boolean(tools.xcode)));
  const runtime = tools.simulator_booted
    ? `booted${tools.ios_runtime ? ` · ${tools.ios_runtime}` : ''}`
    : 'not booted';
  box.appendChild(chip('iOS simulator', runtime, Boolean(tools.simulator_booted)));
  box.appendChild(chip('Node', tools.node || 'missing', Boolean(tools.node)));
  const yarnOk = /^4\./.test(tools.yarn || '');
  box.appendChild(chip('Yarn', tools.yarn || 'missing', yarnOk));
  box.appendChild(chip('Git', tools.git || 'missing', Boolean(tools.git)));
}

function renderGitLine(session) {
  const git = session.git || { state: 'pending' };
  const tools = session.tools || {};
  const login = git.login || tools.git_login || (snapshot().setup.login || '');
  const signature = JSON.stringify([git.state, git.message, login, tools.git_auth]);
  if (signature === ui.gitSignature) return;
  ui.gitSignature = signature;
  const line = $('session-git');
  line.textContent = '';
  line.className = 'git-line';
  if (git.state === 'failed') {
    line.classList.add('failed');
    line.innerHTML = icon('alert');
    line.appendChild(el('span', '', 'Git credentials failed'));
    if (git.message) line.appendChild(el('span', 'small muted', `· ${git.message}`));
    const retry = el('button', 'btn ghost small', 'Retry Git setup');
    retry.addEventListener('click', async () => {
      await api.retryGit();
      const state = await api.getState();
      applyState(state);
    });
    line.appendChild(retry);
    return;
  }
  if (git.state === 'configuring') {
    line.appendChild(el('span', 'spinner'));
    line.appendChild(el('span', '', 'Configuring Git credentials…'));
    return;
  }
  if (git.state === 'configured' || tools.git_auth === true) {
    line.classList.add('success');
    line.innerHTML = icon('check');
    line.appendChild(el('span', '', `${login ? `@${login} · ` : ''}commits and clones ready`));
    return;
  }
  line.innerHTML = icon('key');
  line.appendChild(el('span', '', 'Git credentials pending'));
}

// -- desktop view -----------------------------------------------------------

function renderDesktop() {
  const state = snapshot();
  const target = desktopTarget();
  const url = target ? target.url : '';
  const light = state.session.light;

  const toolbar = target
    ? (light === 'ready' ? ['Live', 'ready']
      : light === 'amber' ? ['Needs attention', 'warning']
        : light === 'ended' ? ['Ended', 'ended'] : ['Connecting', 'starting'])
    : ['', ''];
  $('desktop-live-text').textContent = toolbar[0];
  $('desktop-live').dataset.state = toolbar[1];
  $('desktop-live').hidden = !target;
  $('desktop-run').textContent = target && target.runId ? `Run #${target.runId}` : '';
  $('desktop-time').textContent = desktopTime(target);
  $('desktop-external').disabled = !url;
  $('desktop-copy').disabled = !url;
  $('desktop-stop').disabled = !(target && target.runId) || light === 'ended';
  $('desktop-fullscreen').disabled = !url;
  $('desktop-fullscreen').lastElementChild.textContent = ui.fullscreen
    ? 'Exit fullscreen' : 'Fullscreen';

  const webview = $('desktop-webview');
  const hasUrl = Boolean(url);
  if (hasUrl && url !== ui.webviewUrl) loadWebview(url);
  if (!hasUrl) {
    webview.hidden = true;
    $('desktop-empty').hidden = light === 'starting';
    $('desktop-loading').hidden = light !== 'starting';
    $('desktop-loading').querySelector('p').textContent = light === 'starting'
      ? 'Connecting to your Mac…' : '';
    $('desktop-retry').hidden = true;
    $('desktop-error').hidden = true;
    $('desktop-hint').hidden = true;
    return;
  }

  webview.hidden = !ui.webviewLoaded;
  $('desktop-empty').hidden = true;
  const failed = Boolean(ui.webviewFailed);
  $('desktop-error').hidden = !failed;
  $('desktop-error-text').textContent = ui.webviewFailed;
  const loading = !ui.webviewLoaded && !failed;
  $('desktop-loading').hidden = !loading;
  $('desktop-retry').hidden = !(loading && ui.webviewRetry);
}

function desktopTarget() {
  const state = snapshot();
  if (ui.desktopTarget) {
    if (state.session.runId === ui.desktopTarget.runId && state.session.url) {
      return { ...ui.desktopTarget, url: state.session.url };
    }
    return ui.desktopTarget;
  }
  if (state.session.url) {
    return { runId: state.session.runId, url: state.session.url };
  }
  return null;
}

function desktopTime(target) {
  if (!target) return '';
  const state = snapshot();
  const entry = state.sessions.find((item) => item.runId === target.runId);
  if (!entry) return 'Started outside this app';
  if (!entry.readyAt) return 'Waiting for the desktop';
  const remaining = remainingText(entry);
  const end = Date.parse(entry.readyAt) + (Number(entry.minutes) || 0) * 60_000;
  return `${remaining} · stops ${hhmm(end)}`;
}

function loadWebview(url) {
  ui.webviewUrl = url;
  ui.webviewLoaded = false;
  ui.webviewFailed = '';
  ui.webviewRetry = false;
  ui.hintShown = false;
  clearTimeout(ui.webviewTimer);
  clearTimeout(ui.hintTimer);
  $('desktop-hint').hidden = true;
  const webview = $('desktop-webview');
  webview.hidden = true;
  webview.src = url;
  ui.webviewTimer = setTimeout(() => {
    if (!ui.webviewLoaded && ui.webviewUrl === url) {
      ui.webviewRetry = true;
      renderDesktop();
    }
  }, 10_000);
}

function retryWebview() {
  const url = ui.webviewUrl;
  if (!url) return;
  ui.webviewLoaded = false;
  ui.webviewFailed = '';
  ui.webviewRetry = false;
  const webview = $('desktop-webview');
  webview.src = 'about:blank';
  webview.src = url;
  clearTimeout(ui.webviewTimer);
  ui.webviewTimer = setTimeout(() => {
    if (!ui.webviewLoaded) {
      ui.webviewRetry = true;
      renderDesktop();
    }
  }, 10_000);
  renderDesktop();
}

function wireWebview() {
  const webview = $('desktop-webview');
  webview.addEventListener('dom-ready', () => {
    ui.webviewLoaded = true;
    ui.webviewFailed = '';
    ui.webviewRetry = false;
    clearTimeout(ui.webviewTimer);
    if (!ui.hintShown) {
      ui.hintShown = true;
      $('desktop-hint').hidden = false;
      clearTimeout(ui.hintTimer);
      ui.hintTimer = setTimeout(() => {
        $('desktop-hint').hidden = true;
      }, 3000);
    }
    renderDesktop();
  });
  webview.addEventListener('did-fail-load', (event) => {
    if (event.errorCode === -3) return;
    ui.webviewFailed = `Could not connect to the desktop (${event.errorDescription || 'network error'}).`;
    ui.webviewLoaded = false;
    clearTimeout(ui.webviewTimer);
    renderDesktop();
  });
  webview.addEventListener('crashed', () => {
    ui.webviewFailed = 'The desktop stopped responding.';
    ui.webviewLoaded = false;
    renderDesktop();
  });
}

// -- history ----------------------------------------------------------------

function runStatus(run, entry) {
  if (run.status === 'completed') {
    const conclusion = run.conclusion || 'ended';
    const label = conclusion === 'success' ? 'Succeeded'
      : conclusion === 'failure' ? 'Failed'
        : conclusion === 'cancelled' ? 'Cancelled' : 'Ended';
    return { label, state: 'ended' };
  }
  if (entry && entry.readyAt) return { label: 'Ready', state: 'ready' };
  if (run.status === 'in_progress') return { label: 'Starting', state: 'starting' };
  return { label: 'Queued', state: '' };
}

function rowMeta(run, entry) {
  const parts = [];
  if (entry && entry.minutes) parts.push(`${entry.minutes} min`);
  const started = hhmm(run.startedAt || run.createdAt);
  if (started) parts.push(`started ${started}`);
  if (run.status !== 'completed') {
    const remaining = entry ? remainingText(entry) : '';
    if (remaining) parts.push(remaining);
  }
  return parts.join(' · ');
}

function renderHistory() {
  const state = snapshot();
  const error = state.runsError;
  $('history-error').hidden = !error;
  $('history-error-text').textContent = error || '';
  const runs = (state.runs || []).slice().sort((a, b) => Number(b.databaseId) - Number(a.databaseId));
  const sessionMap = new Map((state.sessions || []).map((entry) => [entry.runId, entry]));
  const active = runs.filter((run) => run.status !== 'completed');
  const earlier = runs.filter((run) => run.status === 'completed');

  const minuteBucket = Math.floor(Date.now() / 60_000);
  const signature = JSON.stringify([
    error,
    runs.map((run) => [run.databaseId, run.status, run.conclusion, run.startedAt,
      run.createdAt, run.url]),
    (state.sessions || []).map((entry) => [entry.runId, entry.url, entry.readyAt,
      entry.minutes]),
    active.length ? minuteBucket : 0,
    ui.desktopTarget && ui.desktopTarget.runId,
  ]);
  if (signature === ui.historySignature) {
    $('history-groups').hidden = runs.length === 0 || Boolean(error);
    $('history-empty').hidden = runs.length > 0 || Boolean(error);
    return;
  }
  ui.historySignature = signature;

  $('history-groups').hidden = runs.length === 0 || Boolean(error);
  $('history-empty').hidden = runs.length > 0 || Boolean(error);
  if (!runs.length) {
    $('history-active-group').hidden = true;
    $('history-earlier-group').hidden = true;
    return;
  }
  renderHistoryGroup($('history-active-group'), $('history-active-rows'), active, sessionMap);
  renderHistoryGroup($('history-earlier-group'), $('history-earlier-rows'), earlier, sessionMap);
}

function renderHistoryGroup(group, container, runs, sessionMap) {
  group.hidden = runs.length === 0;
  container.textContent = '';
  for (const run of runs) {
    const id = Number(run.databaseId);
    const entry = sessionMap.get(id) || null;
    const url = entry && entry.url ? entry.url : '';
    const status = runStatus(run, entry);
    const row = el('div', 'session-row');
    row.tabIndex = 0;
    const pill = el('span', 'status-pill');
    pill.dataset.state = status.state;
    pill.appendChild(el('span', 'dot'));
    pill.appendChild(el('span', '', status.label));
    row.appendChild(pill);
    row.appendChild(el('span', 'run-id mono', `Run #${id}`));
    row.appendChild(el('span', 'row-meta', rowMeta(run, entry)));
    const actions = el('div', 'row-actions');
    const open = el('button', 'btn secondary small', 'Open');
    open.disabled = !url;
    if (!url) open.title = 'Started outside this app';
    open.addEventListener('click', (event) => {
      event.stopPropagation();
      openHistory({ runId: id, url });
    });
    actions.appendChild(open);
    if (run.status !== 'completed') {
      const stop = el('button', 'btn danger-ghost small', 'Stop');
      stop.addEventListener('click', async (event) => {
        event.stopPropagation();
        await api.cancel(id);
        toast('Session stopped', 'stop');
      });
      actions.appendChild(stop);
    }
    row.appendChild(actions);
    row.addEventListener('dblclick', () => {
      if (url) openHistory({ runId: id, url });
    });
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && url) openHistory({ runId: id, url });
    });
    container.appendChild(row);
  }
}

function openHistory(target) {
  if (!target || !target.url) return;
  ui.desktopTarget = { runId: target.runId, url: target.url };
  setView('desktop');
}

// -- settings ---------------------------------------------------------------

function renderSettings() {
  const state = snapshot();
  const { setup, settings } = state;
  setValue($('settings-repo'), settings.runnerRepo);
  setValue($('settings-git-name'), settings.gitName || '');
  setValue($('settings-git-email'), settings.gitEmail || '');
  const identity = setup.identity || {};
  $('settings-git-name').placeholder = identity.name || 'Resolved from git or GitHub';
  $('settings-git-email').placeholder = identity.email || 'Resolved from git or GitHub';

  const secret = (setup.checks || []).find((check) => check.id === 'secret');
  const status = $('settings-key-status');
  status.className = 'key-status';
  if (!secret || setup.checking) {
    status.textContent = 'Checking…';
  } else if (secret.warning) {
    status.textContent = 'Could not verify · needs repo admin';
    status.classList.add('warning');
  } else if (secret.ok) {
    status.textContent = 'Configured';
    status.classList.add('ok');
  } else {
    status.textContent = 'Missing';
    status.classList.add('missing');
  }

  const checks = setup.checks || [];
  const required = checks.filter((check) => check.required);
  const passed = required.filter((check) => check.ok).length;
  $('settings-setup-summary').textContent = setup.complete
    ? 'Everything is ready.'
    : `${passed} of ${required.length} ready.`;

  $('about-version').textContent = state.version || '';
  $('about-login').textContent = setup.login ? `@${setup.login}` : 'Not signed in';
  $('about-repo').textContent = settings.runnerRepo;
}

function scheduleSave(partial) {
  clearTimeout(ui.settingsTimer);
  ui.settingsTimer = setTimeout(async () => {
    const state = await api.saveSettings(partial);
    if (state) applyState(state);
    const note = $('settings-saved');
    note.hidden = false;
    clearTimeout(ui.savedTimer);
    ui.savedTimer = setTimeout(() => {
      note.hidden = true;
    }, 2000);
  }, 400);
}

function openSettings(focusId) {
  setView('settings');
  if (focusId) {
    const field = $(focusId);
    if (field) {
      field.focus();
      field.select();
    }
  }
}

// -- view and events --------------------------------------------------------

function setView(view, options = {}) {
  if (!VIEW_TITLES[view]) return;
  ui.view = view;
  if (view === 'session' && options.fromNav) ui.forceWizard = false;
  if (view === 'desktop' && options.resetTarget) ui.desktopTarget = null;
  document.querySelectorAll('.view').forEach((node) => {
    node.hidden = node.dataset.view !== view;
  });
  $('content').dataset.active = view;
  render();
}

function openSetup() {
  ui.forceWizard = true;
  setView('session');
}

function openCurrentDesktop() {
  const state = snapshot();
  if (!state.session.url) return;
  ui.desktopTarget = { runId: state.session.runId, url: state.session.url };
  setView('desktop');
}

function selectDuration(minutes, custom) {
  ui.custom = Boolean(custom);
  if (custom) {
    const input = $('custom-minutes-input');
    if (!input.value || Number(input.value) < 1) input.value = '45';
  } else {
    ui.duration = minutes;
  }
  renderSessionMain();
}

function selectedMinutes() {
  if (ui.custom) return Number($('custom-minutes-input').value);
  return Number(ui.duration);
}

async function startSession() {
  const state = snapshot();
  if (!state || ui.starting) return;
  if (!state.setup.complete) {
    openSetup();
    return;
  }
  const minutes = selectedMinutes();
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 300) {
    ui.startError = 'Choose 1-300 whole minutes.';
    renderSessionMain();
    return;
  }
  ui.starting = true;
  ui.startError = '';
  renderSessionMain();
  const request = {
    minutes,
    copyDir: $('copy-dir').value.trim(),
    copyName: $('copy-name').value.trim(),
    excludeDefaults: $('copy-excludes').checked,
  };
  let result;
  try {
    result = await api.start(request);
  } catch (error) {
    result = { ok: false, error: error.message };
  } finally {
    ui.starting = false;
  }
  if (!result || !result.ok) {
    if (result && result.needsSetup) {
      ui.forceWizard = true;
      ui.startError = '';
    } else {
      ui.startError = (result && result.error) || 'The session could not start.';
    }
    render();
    return;
  }
  ui.desktopTarget = null;
  ui.timelineSignature = '';
  ui.chipsSignature = '';
  ui.gitSignature = '';
  setView('session');
}

async function copyNow() {
  if (ui.copyPending) return;
  const name = $('copy-name').value.trim();
  const dir = $('copy-dir').value.trim();
  if (!dir) {
    ui.startError = 'Choose a folder to copy.';
    renderSessionMain();
    return;
  }
  ui.copyPending = true;
  renderSessionMain();
  const result = await api.copy({
    src: dir,
    name,
    excludeDefaults: $('copy-excludes').checked,
  });
  ui.copyPending = false;
  if (result && result.ok) toast('Folder copied');
  else if (result && result.error) toast(result.error, 'alert');
  renderSessionMain();
}

function wireEvents() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => setView(item.dataset.view, { fromNav: true }));
  });
  $('header-setup').addEventListener('click', openSetup);
  $('rail-setup').addEventListener('click', openSetup);
  $('header-open').addEventListener('click', openCurrentDesktop);

  $('wizard-fix').addEventListener('click', async () => {
    const state = await api.autoFix();
    if (state && state.setup && state.setup.needs === 'tailscale-auth-key') ui.focusKey = true;
    applyState(state);
  });
  $('wizard-recheck').addEventListener('click', async () => {
    const state = await api.checkSetup();
    applyState(state);
  });
  $('wizard-done').addEventListener('click', () => {
    ui.forceWizard = false;
    render();
  });
  $('setup-log-toggle').addEventListener('click', () => {
    ui.logOpen = !ui.logOpen;
    renderWizard();
  });

  const duration = $('duration');
  duration.addEventListener('click', (event) => {
    const pill = event.target.closest('.duration-pill');
    if (!pill) return;
    if (pill.dataset.custom) selectDuration(null, true);
    else selectDuration(Number(pill.dataset.minutes), false);
  });
  duration.addEventListener('keydown', (event) => {
    const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const pills = Array.from(duration.querySelectorAll('.duration-pill'));
    const current = pills.indexOf(document.activeElement);
    let next = current < 0 ? 0 : current;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = pills.length - 1;
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = (current + 1 + pills.length) % pills.length;
    } else {
      next = (current - 1 + pills.length) % pills.length;
    }
    pills[next].focus();
    pills[next].click();
  });
  $('custom-minutes-input').addEventListener('input', () => {
    ui.custom = true;
    for (const pill of document.querySelectorAll('.duration-pill')) {
      pill.setAttribute('aria-checked', String(pill.dataset.custom === 'true'));
    }
  });

  $('start-button').addEventListener('click', startSession);
  $('banner-open').addEventListener('click', openCurrentDesktop);

  $('copy-browse').addEventListener('click', async () => {
    const dir = await api.chooseDir();
    if (!dir) return;
    $('copy-dir').value = dir;
    if (!$('copy-name').value.trim()) {
      const base = dir.split('/').filter(Boolean).pop() || '';
      $('copy-name').value = base.replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 64);
    }
    toast('Folder selected', 'folder');
  });
  $('copy-now').addEventListener('click', copyNow);

  $('session-cancel').addEventListener('click', async () => {
    const state = snapshot();
    if (!state.session.runId) return;
    await api.cancel(state.session.runId);
    toast('Session cancelled', 'stop');
  });
  $('error-run').addEventListener('click', () => {
    const state = snapshot();
    if (!state.session.runId) return;
    openExternal(`https://github.com/${state.settings.runnerRepo}/actions/runs/${state.session.runId}`);
  });
  $('error-retry').addEventListener('click', async () => applyState(await api.refresh()));
  $('error-setup').addEventListener('click', async () => {
    openSetup();
    applyState(await api.checkSetup());
  });

  $('desktop-start').addEventListener('click', () => setView('session', { fromNav: true }));
  $('desktop-fullscreen').addEventListener('click', () => {
    ui.fullscreen = !ui.fullscreen;
    document.body.classList.toggle('fullscreen', ui.fullscreen);
    api.fullscreen(ui.fullscreen);
    renderDesktop();
  });
  $('desktop-external').addEventListener('click', () => {
    const target = desktopTarget();
    if (target) openExternal(target.url);
  });
  $('desktop-copy').addEventListener('click', () => {
    const target = desktopTarget();
    if (target) copyText(target.url, 'Desktop URL copied');
  });
  $('desktop-stop').addEventListener('click', async () => {
    const target = desktopTarget();
    if (!target || !target.runId) return;
    await api.cancel(target.runId);
    toast('Session stopped', 'stop');
  });
  $('desktop-retry').addEventListener('click', retryWebview);
  $('desktop-error-retry').addEventListener('click', retryWebview);

  $('history-retry').addEventListener('click', async () => applyState(await api.refresh()));
  $('history-start').addEventListener('click', () => setView('session', { fromNav: true }));

  const repoInput = $('settings-repo');
  repoInput.addEventListener('input', () => {
    const value = repoInput.value.trim();
    const valid = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(value);
    $('settings-repo-note').hidden = valid;
    $('settings-repo-note').textContent = valid ? '' : 'Use owner/repo.';
    if (valid) scheduleSave({ runnerRepo: value });
  });
  $('settings-git-name').addEventListener('input', () => {
    scheduleSave({ gitName: $('settings-git-name').value });
  });
  $('settings-git-email').addEventListener('input', () => {
    scheduleSave({ gitEmail: $('settings-git-email').value });
  });
  $('settings-save-key').addEventListener('click', async () => {
    const value = $('settings-key').value;
    const button = $('settings-save-key');
    button.disabled = true;
    const result = await api.setSecret(value);
    button.disabled = false;
    const note = $('settings-key-note');
    if (result && result.error) {
      note.hidden = false;
      note.classList.add('error-text');
      note.textContent = result.error;
      return;
    }
    note.hidden = true;
    $('settings-key').value = '';
    applyState(result);
    toast('Key saved');
  });
  $('settings-create-key').addEventListener('click', () => openExternal(TAILSCALE_KEYS));
  $('settings-recheck').addEventListener('click', async () => applyState(await api.checkSetup()));
  $('settings-open-setup').addEventListener('click', openSetup);

  document.addEventListener('keydown', (event) => {
    const command = event.metaKey || event.ctrlKey;
    if (command && ['1', '2', '3', '4'].includes(event.key)) {
      event.preventDefault();
      const view = ['session', 'desktop', 'history', 'settings'][Number(event.key) - 1];
      setView(view, { fromNav: true });
      return;
    }
    if (command && event.key === 'Enter') {
      const current = snapshot();
      const startable = current && ['idle', 'ended'].includes(current.session.light);
      if (ui.view === 'session' && current.setup.complete && startable) {
        event.preventDefault();
        startSession();
      }
      return;
    }
    if (event.key === 'Escape') {
      if (document.body.classList.contains('fullscreen')) {
        event.preventDefault();
        ui.fullscreen = false;
        document.body.classList.remove('fullscreen');
        api.fullscreen(false);
        renderDesktop();
        return;
      }
      const openDetails = Array.from(document.querySelectorAll('details[open]'));
      if (openDetails.length) {
        event.preventDefault();
        openDetails[openDetails.length - 1].open = false;
      }
    }
  });

  wireWebview();
}

function wireSubscriptions() {
  api.onState(applyState);
  api.onSetupLog((payload) => {
    if (!payload || typeof payload.text !== 'string') return;
    ui.setupLog.push(payload.text);
    if (ui.setupLog.length > 400) ui.setupLog.splice(0, ui.setupLog.length - 400);
    if (ui.logOpen && !$('setup-log').hidden) {
      $('setup-log').textContent = ui.setupLog.join('\n');
      $('setup-log').scrollTop = $('setup-log').scrollHeight;
    }
  });
  api.onAuthCode((payload) => {
    if (!payload || !payload.code) return;
    ui.authCode = payload;
    ui.forceWizard = true;
    toast('Sign-in code ready', 'key');
    render();
  });
  api.onOpenDesktop((payload) => {
    if (!ui.state) return;
    ui.desktopTarget = {
      runId: ui.state.session.runId,
      url: (payload && payload.url) || ui.state.session.url,
    };
    setTimeout(() => {
      if (ui.view === 'session' && !isTyping()) {
        setView('desktop');
      } else {
        toast('Desktop ready');
      }
    }, reducedMotion() ? 0 : 700);
  });
  api.onFullscreen((payload) => {
    ui.fullscreen = Boolean(payload && payload.on);
    document.body.classList.toggle('fullscreen', ui.fullscreen);
    renderDesktop();
  });
}

function boot() {
  document.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
  });
  wireEvents();
  wireSubscriptions();
  setView('session');
  api.getState().then(applyState).catch(() => {});
  setInterval(() => {
    const state = snapshot();
    if (!state) return;
    $('header-timer').textContent = state.session.light === 'ready'
      ? remainingText(activeEntry()) : '';
    $('desktop-time').textContent = desktopTime(desktopTarget());
  }, 1000);
}

document.addEventListener('DOMContentLoaded', boot);
