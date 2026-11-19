'use strict';
// Setup checks, identity resolution and the automatic fixes behind the wizard.
// Every check is a row: a failure never throws out of runChecks.

const { spawn } = require('node:child_process');
const { Backend, BackendError, usefulLine } = require('./backend');

const WORKFLOW_PATH = '.github/workflows/ios-ci.yml';
const SECRET_NAME = 'TAILSCALE_AUTH_KEY';
const GH_HOST_DEFAULT = 'github.com';
const GH_INSTALL = 'https://cli.github.com';
const TAILSCALE_INSTALL = 'https://tailscale.com/download';
const TAILSCALE_KEYS = 'https://login.tailscale.com/admin/settings/keys';
const DEVICE_URL = 'https://github.com/login/device';
const SCOPES = ['repo', 'workflow', 'read:org', 'gist'];
const WRITE_ROLES = new Set(['ADMIN', 'MAINTAIN', 'WRITE']);

function clean(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function scopesOf(entry) {
  return String(entry.scopes || '').split(/[,\s]+/).filter(Boolean);
}

/** Per-field precedence: explicit settings, local git config, then the GitHub user. */
function pickIdentity({ settings = {}, gitName = '', gitEmail = '', ghUser = null }) {
  const user = ghUser && typeof ghUser === 'object' ? ghUser : null;
  let name = clean(settings.gitName);
  let nameSource = 'settings';
  if (!name) {
    name = clean(gitName);
    nameSource = 'git config';
  }
  if (!name && user) {
    name = clean(user.name) || clean(user.login);
    nameSource = 'GitHub';
  }
  let email = clean(settings.gitEmail);
  let emailSource = 'settings';
  if (!email) {
    email = clean(gitEmail);
    emailSource = 'git config';
  }
  if (!email && user) {
    email = clean(user.email);
    if (!email && user.id && user.login) {
      email = `${user.id}+${user.login}@users.noreply.github.com`;
    }
    emailSource = 'GitHub';
  }
  return { name, email, nameSource, emailSource };
}

async function authEntry(backend) {
  const output = await backend.gh(['auth', 'status', '--json', 'hosts'], { timeout: 20_000 });
  const parsed = JSON.parse(output || '{}');
  const hosts = parsed.hosts && typeof parsed.hosts === 'object' ? parsed.hosts : parsed;
  const entries = [];
  for (const [host, accounts] of Object.entries(hosts || {})) {
    if (!Array.isArray(accounts)) continue;
    for (const account of accounts) entries.push({ ...account, host: account.host || host });
  }
  const active = entries.find((entry) => entry.active === true);
  const chosen = active || entries.find((entry) => entry.state === 'success') || null;
  return { chosen, active: Boolean(active) };
}

async function resolveIdentity({ settings = {}, backend, host = GH_HOST_DEFAULT } = {}) {
  const client = backend || new Backend(clean(settings.runnerRepo) || undefined);
  let gitName = '';
  let gitEmail = '';
  try {
    gitName = (await client.run('git', ['config', '--global', 'user.name'], { timeout: 5000 })).trim();
  } catch {
    gitName = '';
  }
  try {
    gitEmail = (await client.run('git', ['config', '--global', 'user.email'], { timeout: 5000 })).trim();
  } catch {
    gitEmail = '';
  }
  let ghUser = null;
  try {
    ghUser = JSON.parse(await client.gh([
      'api', '--hostname', host, 'user', '--jq', '{name,login,id,email}',
    ], { timeout: 20_000 }) || 'null');
  } catch {
    ghUser = null;
  }
  return pickIdentity({ settings, gitName, gitEmail, ghUser });
}

/** Every required item, as rows. Never throws; a failure is a row. */
async function runChecks({ runnerRepo, settings = {}, backend } = {}) {
  const repo = clean(runnerRepo) || clean(settings.runnerRepo) || 'eshahrabani/ios-ci';
  const client = backend || new Backend(repo);
  const checks = [];
  const row = (id, label, required) => ({
    id, label, required, ok: false, detail: '',
  });

  const gh = row('gh', 'GitHub CLI', true);
  checks.push(gh);
  const tailscale = row('tailscale', 'Tailscale', true);
  checks.push(tailscale);
  const auth = row('gh-auth', 'GitHub sign-in', true);
  checks.push(auth);
  const tailnet = row('tailnet', 'Tailnet connection', true);
  checks.push(tailnet);
  const repository = row('repo', 'Runner repository', true);
  checks.push(repository);
  const secret = row('secret', 'Tailscale auth key', true);
  checks.push(secret);
  const git = row('git', 'Git identity', true);
  checks.push(git);

  const ghVersion = (async () => {
    try {
      const output = await client.run('gh', ['--version'], { timeout: 5000 });
      gh.ok = true;
      gh.detail = usefulLine(output);
    } catch (error) {
      gh.detail = usefulLine(error.message) || 'not installed';
      gh.reason = 'missing';
    }
  })();

  const tailscaleVersion = (async () => {
    try {
      const output = await client.run('tailscale', ['version'], { timeout: 5000 });
      tailscale.ok = true;
      tailscale.detail = usefulLine(output);
    } catch (error) {
      tailscale.detail = usefulLine(error.message) || 'not installed';
      tailscale.reason = 'missing';
    }
  })();

  let host = GH_HOST_DEFAULT;
  let login = '';
  const authCheck = (async () => {
    try {
      const { chosen } = await authEntry(client);
      if (!chosen) {
        auth.detail = 'not signed in (gh auth login)';
        auth.reason = 'no-auth';
        return;
      }
      host = clean(chosen.host) || GH_HOST_DEFAULT;
      login = clean(chosen.login);
      const scopes = scopesOf(chosen);
      const missing = ['repo', 'workflow'].filter((scope) => !scopes.includes(scope));
      if (missing.length) {
        auth.detail = `@${login} on ${host} is missing ${missing.join(', ')}`;
        auth.reason = 'scopes';
        return;
      }
      let token = '';
      try {
        token = await client.ghToken(host);
      } catch {
        token = '';
      }
      if (!token) {
        auth.detail = `@${login} on ${host} has no usable token`;
        auth.reason = 'no-auth';
        return;
      }
      auth.ok = true;
      auth.detail = `@${login} on ${host} (scopes: ${scopes.join(', ') || 'none'})`;
    } catch (error) {
      auth.detail = usefulLine(error.message) || 'gh auth status failed';
      auth.reason = 'no-auth';
    }
  })();

  const tailnetCheck = (async () => {
    try {
      const output = await client.run('tailscale', ['status', '--json'], { timeout: 5000 });
      const status = JSON.parse(output || '{}');
      if (status.BackendState === 'Running') {
        tailnet.ok = true;
        const self = status.Self || {};
        const name = String(self.DNSName || '').replace(/\.$/, '') || self.HostName || 'this device';
        tailnet.detail = `running · ${name}`;
      } else {
        tailnet.detail = `Tailscale is ${status.BackendState || 'not running'}`;
        tailnet.reason = 'not-running';
      }
    } catch (error) {
      tailnet.detail = usefulLine(error.message) || 'tailscale status failed';
      tailnet.reason = 'not-running';
    }
  })();

  const repoCheck = (async () => {
    let permission = '';
    try {
      const output = await client.gh([
        'repo', 'view', repo, '--json', 'viewerPermission',
      ], { timeout: 20_000 });
      permission = JSON.parse(output || '{}').viewerPermission || '';
    } catch (error) {
      repository.detail = usefulLine(error.message) || `${repo} is not accessible`;
      repository.reason = 'missing';
      return;
    }
    if (!WRITE_ROLES.has(permission)) {
      repository.detail = `${repo}: your role is ${permission || 'read-only'}`;
      repository.reason = 'permission';
      return;
    }
    try {
      const output = await client.gh([
        'workflow', 'list', '--repo', repo, '--all', '--json', 'path,state',
      ], { timeout: 20_000 });
      const workflows = JSON.parse(output || '[]');
      const entry = workflows.find((workflow) => workflow.path === WORKFLOW_PATH);
      if (!entry) {
        repository.detail = `${WORKFLOW_PATH} is not in ${repo}`;
        repository.reason = 'missing';
        return;
      }
      if (entry.state !== 'active') {
        repository.detail = `${WORKFLOW_PATH} is ${entry.state}`;
        repository.reason = 'workflow-disabled';
        return;
      }
      repository.ok = true;
      repository.detail = `${repo} · ${permission} · workflow active`;
    } catch (error) {
      repository.detail = usefulLine(error.message) || 'gh workflow list failed';
      repository.reason = 'missing';
    }
  })();

  const secretCheck = (async () => {
    try {
      const output = await client.gh([
        'secret', 'list', '--repo', repo, '--json', 'name',
      ], { timeout: 20_000 });
      const names = new Set((JSON.parse(output || '[]')).map((item) => item.name));
      if (names.has(SECRET_NAME)) {
        secret.ok = true;
        secret.detail = SECRET_NAME;
      } else {
        secret.detail = `${SECRET_NAME} is missing`;
        secret.reason = 'missing';
      }
    } catch {
      secret.ok = true;
      secret.required = false;
      secret.warning = true;
      secret.detail = 'could not verify; needs repo admin';
    }
  })();

  await Promise.all([ghVersion, tailscaleVersion, authCheck, tailnetCheck, repoCheck, secretCheck]);

  let identity = { name: '', email: '', nameSource: 'settings', emailSource: 'settings' };
  try {
    identity = await resolveIdentity({ settings, backend: client, host });
    git.detail = `${identity.name || 'no name'} ${identity.email || 'no email'}`
      + ` (from ${identity.emailSource})`;
    git.ok = Boolean(identity.name && identity.email);
    if (!git.ok) git.reason = 'identity';
  } catch (error) {
    git.detail = usefulLine(error.message) || 'could not resolve an identity';
    git.reason = 'identity';
  }

  return {
    complete: checks.every((check) => !check.required || check.ok),
    checks,
    host,
    login,
    identity,
  };
}

/** Interactive gh login/refresh, including the one-time device code. */
async function fixAuth({ host, reason, emit, openExternal }) {
  const scopes = SCOPES.join(',');
  const args = reason === 'scopes'
    ? ['auth', 'refresh', '--hostname', host, '--scopes', scopes]
    : ['auth', 'login', '--hostname', host, '--git-protocol', 'https', '--web',
      '--clipboard', '--scopes', scopes];
  emit({ text: `gh ${args.join(' ')}` });

  const child = spawn('gh', args, { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = [];
  let buffer = '';
  let codeSeen = false;
  const answered = { done: false };
  const answer = () => {
    if (answered.done) return;
    answered.done = true;
    try {
      child.stdin.write('\n');
    } catch {
      // The child may already have exited.
    }
  };
  const finishPromise = new Promise((resolve) => {
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL');
      } catch {
        // Already gone.
      }
      resolve({ code: -1, timedOut: true });
    }, 300_000);
    const read = (chunk) => {
      buffer += chunk.toString('utf8');
      let index = buffer.indexOf('\n');
      while (index >= 0) {
        const line = buffer.slice(0, index).replace(/\r$/, '');
        buffer = buffer.slice(index + 1);
        lines.push(line);
        if (line.trim()) emit({ text: line });
        const match = /one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})/i.exec(line);
        if (match && !codeSeen) {
          codeSeen = true;
          emit({ url: DEVICE_URL, code: match[1].toUpperCase() });
          openExternal(DEVICE_URL);
          setTimeout(answer, 400);
        }
        index = buffer.indexOf('\n');
      }
    };
    child.stdout.on('data', read);
    child.stderr.on('data', read);
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ code: -1, error: error.message });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (buffer.trim()) {
        lines.push(buffer);
        emit({ text: buffer });
      }
      resolve({ code });
    });
    if (child.stdin) child.stdin.on('error', () => {});
    // gh prompts for "Press Enter" even when stdin is a pipe; without this the
    // device flow waits for a terminal that will never type.
    setTimeout(answer, 1500);
  });
  const result = await finishPromise;
  if (result.error || result.code !== 0) {
    for (const line of lines.slice(-20)) emit({ text: line });
    return {
      ok: false,
      needs: 'terminal-gh-auth',
      error: result.error || (result.timedOut
        ? 'gh auth login timed out'
        : 'gh auth login did not finish; run it in a terminal'),
    };
  }
  return { ok: true };
}

/** `tailscale up`, opening its login URL when the daemon asks for one. */
async function fixTailnet({ emit, openExternal }) {
  emit({ text: 'tailscale up' });
  let loginUrl = '';
  const child = spawn('tailscale', ['up'], { stdio: ['ignore', 'pipe', 'pipe'] });
  const lines = [];
  let buffer = '';
  const result = await new Promise((resolve) => {
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL');
      } catch {
        // Already gone.
      }
      resolve({ code: -1, timedOut: true });
    }, 180_000);
    const read = (chunk) => {
      buffer += chunk.toString('utf8');
      let index = buffer.indexOf('\n');
      while (index >= 0) {
        const line = buffer.slice(0, index).replace(/\r$/, '');
        buffer = buffer.slice(index + 1);
        lines.push(line);
        if (line.trim()) emit({ text: line });
        const match = /(https:\/\/login\.tailscale\.com\/\S+)/.exec(line);
        if (match && !loginUrl) {
          loginUrl = match[1];
          openExternal(loginUrl);
        }
        index = buffer.indexOf('\n');
      }
    };
    child.stdout.on('data', read);
    child.stderr.on('data', read);
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ code: -1, error: error.message });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code });
    });
  });
  if (result.error || result.code !== 0) {
    for (const line of lines.slice(-20)) emit({ text: line });
    emit({ text: 'if this needs privileges, run: sudo tailscale up' });
    return { ok: false, error: result.error || 'tailscale up failed' };
  }
  return { ok: true };
}

/**
 * Fix every fixable row in order, or only the row named by `only`.
 * Streams log lines (`{text}`) and device codes (`{code,url}`) through `emit`.
 */
async function autoFix({ runnerRepo, settings = {}, emit = () => {}, openExternal = () => {}, only } = {}) {
  const repo = clean(runnerRepo) || clean(settings.runnerRepo) || 'eshahrabani/ios-ci';
  const backend = new Backend(repo);
  const log = (text) => emit({ text });
  let needs = null;
  const wanted = only || null;

  let current = await runChecks({ runnerRepo: repo, settings });
  const get = (id) => current.checks.find((check) => check.id === id);
  const wants = (id) => !wanted || wanted === id;
  const refresh = async () => {
    current = await runChecks({ runnerRepo: repo, settings });
  };

  if (wants('gh-auth') && !get('gh').ok) {
    log('GitHub CLI is not installed; opening the download page');
    openExternal(GH_INSTALL);
    needs = 'gh-install';
  }

  if (!needs && wants('gh-auth') && !get('gh-auth').ok) {
    const result = await fixAuth({
      host: current.host || GH_HOST_DEFAULT,
      reason: get('gh-auth').reason,
      emit,
      openExternal,
    });
    if (!result.ok) needs = result.needs || 'terminal-gh-auth';
    else await refresh();
  }

  if (!needs && wants('tailnet') && !get('tailscale').ok) {
    log('Tailscale is not installed; opening the download page');
    openExternal(TAILSCALE_INSTALL);
    needs = 'tailscale-install';
  }

  if (!needs && wants('tailnet') && !get('tailnet').ok) {
    const result = await fixTailnet({ emit, openExternal });
    if (!result.ok) needs = 'tailnet-up';
    else await refresh();
  }

  if (!needs && wants('repo') && !get('repo').ok) {
    const row = get('repo');
    if (row.reason === 'workflow-disabled') {
      log('enabling the ios-ci workflow');
      try {
        await backend.gh(['workflow', 'enable', 'ios-ci.yml', '--repo', repo], { timeout: 20_000 });
        await refresh();
      } catch (error) {
        log(usefulLine(error.message) || 'could not enable the workflow');
        needs = 'repo';
      }
    } else {
      log('the runner repository needs to be changed in Settings');
      needs = 'repo';
    }
  }

  if (!needs && !wanted && !get('secret').ok) {
    needs = 'tailscale-auth-key';
  }

  await refresh();
  emit({ text: current.complete ? 'everything is ready' : 'some items still need attention' });
  return { ...current, needs };
}

/** Save the one manual setup value: a reusable Tailscale auth key. */
async function saveSecret({ runnerRepo, value }) {
  const secret = typeof value === 'string' ? value.trim() : '';
  if (!secret) {
    throw new BackendError('paste a reusable auth key (tskey-auth-…)');
  }
  if (!secret.startsWith('tskey-auth-')) {
    throw new BackendError(
      'paste a reusable auth key (tskey-auth-…), not an OAuth client secret',
    );
  }
  const backend = new Backend(runnerRepo);
  await backend.setSecret(SECRET_NAME, `${secret}\n`);
  return runChecks({ runnerRepo });
}

module.exports = {
  DEVICE_URL,
  GH_INSTALL,
  GH_HOST_DEFAULT,
  SECRET_NAME,
  SCOPES,
  TAILSCALE_INSTALL,
  TAILSCALE_KEYS,
  WORKFLOW_PATH,
  autoFix,
  pickIdentity,
  resolveIdentity,
  runChecks,
  saveSecret,
};
