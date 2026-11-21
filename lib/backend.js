'use strict';
// Every gh/tailnet/HTTP operation the client performs. No Electron, no UI state.

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { pipeline } = require('node:stream/promises');
const tar = require('tar');

const PORT = 6080;
const WORKFLOW = 'ios-ci.yml';
const DEFAULT_EXCLUDES = ['.git', 'node_modules', 'build', 'DerivedData', 'Pods', '.gradle'];
const UPLOAD_LIMIT = 2 * 1024 ** 3;
const UPLOAD_NAME = /^[A-Za-z0-9._-]{1,64}$/;
const PROBE_LIMIT = 1024 * 1024;

class BackendError extends Error {
  constructor(message) {
    super(message);
    this.name = 'BackendError';
  }
}

function usefulLine(value) {
  for (const line of String(value || '').split('\n')) {
    const trimmed = line.trim();
    if (trimmed) return trimmed.slice(0, 500);
  }
  return '';
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Parse the newline-separated key=value reply of `git credential fill`. */
function parseCredential(output) {
  const credential = { username: '', password: '' };
  for (const line of String(output || '').split(/\r?\n/)) {
    const index = line.indexOf('=');
    if (index < 1) continue;
    const key = line.slice(0, index);
    const value = line.slice(index + 1);
    if (key === 'username') credential.username = value;
    else if (key === 'password') credential.password = value;
  }
  return credential.username && credential.password ? credential : null;
}

function humanBytes(count) {
  let size = Number(count);
  for (const unit of ['B', 'kB', 'MB', 'GB']) {
    if (size < 1000 || unit === 'GB') {
      return unit === 'B' ? `${size.toFixed(0)} B` : `${size.toFixed(1)} ${unit}`;
    }
    size /= 1000;
  }
  return `${size.toFixed(1)} GB`;
}

class Backend {
  constructor(runnerRepo, ref) {
    this.runnerRepo = (runnerRepo || '').trim();
    this.ref = ref || process.env.IOS_CI_REF || 'main';
  }

  /** One child process with no shell; rejects with a readable line of its output. */
  run(cmd, args, options = {}) {
    const timeout = options.timeout === undefined ? 120_000 : options.timeout;
    const env = options.env ? { ...process.env, ...options.env } : process.env;
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = spawn(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'], env });
      } catch (error) {
        reject(new BackendError(`${cmd}: ${error.message}`));
        return;
      }
      const out = [];
      const err = [];
      let settled = false;
      let timer = null;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      };
      if (timeout > 0) {
        timer = setTimeout(() => {
          child.kill('SIGKILL');
          finish(reject, new BackendError(`${cmd} ${args[0] || ''} timed out`));
        }, timeout);
      }
      child.stdout.on('data', (chunk) => out.push(chunk));
      child.stderr.on('data', (chunk) => err.push(chunk));
      child.on('error', (error) => {
        finish(reject, new BackendError(error.code === 'ENOENT'
          ? `${cmd} is not installed`
          : `${cmd}: ${error.message}`));
      });
      child.on('close', (code) => {
        const stdout = Buffer.concat(out).toString('utf8');
        const stderr = Buffer.concat(err).toString('utf8');
        if (code !== 0) {
          finish(reject, new BackendError(
            usefulLine(stderr) || usefulLine(stdout) || `${cmd} exited with code ${code}`,
          ));
          return;
        }
        finish(resolve, stdout);
      });
      if (child.stdin) {
        child.stdin.on('error', () => {});
        if (options.input === undefined) child.stdin.end();
        else child.stdin.end(options.input);
      }
    });
  }

  gh(args, options) {
    return this.run('gh', args, options);
  }

  // -- dispatch and runs -------------------------------------------------------

  async newestRunId() {
    const output = await this.gh([
      'run', 'list', '--repo', this.runnerRepo, '--workflow', WORKFLOW,
      '--limit', '1', '--json', 'databaseId',
    ]);
    const runs = JSON.parse(output || '[]');
    return runs.length ? Number(runs[0].databaseId) : 0;
  }

  /** Dispatch one session and return its run id once GitHub lists it. */
  async dispatch(minutes, tokenHash, arch = 'arm64') {
    const correlation = `app-${Date.now()}-${crypto.randomBytes(3).toString('hex')}`;
    let baseline = 0;
    try {
      baseline = await this.newestRunId();
    } catch {
      baseline = 0;
    }
    await this.gh([
      'workflow', 'run', WORKFLOW, '--repo', this.runnerRepo, '--ref', this.ref,
      '-f', `correlation_id=${correlation}`,
      '-f', `session_minutes=${minutes}`,
      '-f', `session_token_hash=${tokenHash}`,
      '-f', `runner_arch=${arch}`,
    ]);
    const title = `ios-ci ${correlation}`;
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      const listed = await this.gh([
        'run', 'list', '--repo', this.runnerRepo, '--workflow', WORKFLOW,
        '--limit', '100', '--json', 'databaseId,displayTitle,conclusion',
      ]);
      const runs = JSON.parse(listed || '[]');
      for (const run of runs) {
        if (run.displayTitle === title) return Number(run.databaseId);
      }
      const stuck = runs.filter((run) => Number(run.databaseId) > baseline
        && run.conclusion === 'startup_failure');
      if (stuck.length) {
        const newest = stuck.reduce((best, run) =>
          (Number(run.databaseId) > Number(best.databaseId) ? run : best));
        throw new BackendError(
          'the run never started: GitHub rejected the workflow file.\n'
          + `https://github.com/${this.runnerRepo}/actions/runs/${newest.databaseId}`,
        );
      }
      await sleep(2000);
    }
    throw new BackendError('the dispatched run did not appear within 5 minutes');
  }

  async runs(limit = 20) {
    const output = await this.gh([
      'run', 'list', '--repo', this.runnerRepo, '--workflow', WORKFLOW,
      '--limit', String(limit),
      '--json', 'databaseId,displayTitle,status,conclusion,createdAt,startedAt,url',
    ]);
    return JSON.parse(output || '[]');
  }

  async runDetail(id) {
    const output = await this.gh([
      'run', 'view', String(id), '--repo', this.runnerRepo,
      '--json', 'createdAt,status,conclusion,url,jobs',
    ], { timeout: 60_000 });
    const detail = JSON.parse(output || '{}');
    if (!detail || !Object.keys(detail).length) {
      throw new BackendError(`run ${id} is not visible yet`);
    }
    return detail;
  }

  async cancel(id) {
    await this.gh(['run', 'cancel', String(id), '--repo', this.runnerRepo]);
  }

  // -- tailnet ----------------------------------------------------------------

  /** The peer address for a runner node, or null while it has not joined. */
  async tailnetPeer(runId) {
    let parsed;
    try {
      const output = await this.run('tailscale', ['status', '--json'], { timeout: 30_000 });
      parsed = JSON.parse(output || '{}');
    } catch {
      return null;
    }
    const hostname = `ios-ci-${runId}`;
    for (const peer of Object.values(parsed.Peer || {})) {
      if (peer.HostName !== hostname) continue;
      for (const address of peer.TailscaleIPs || []) {
        if (!address.includes(':')) return address;
      }
      const name = String(peer.DNSName || '').replace(/\.$/, '');
      return name || null;
    }
    return null;
  }

  /** The parsed /stats payload for a session, or null when it is not serving. */
  probe(host, token) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      let request;
      try {
        request = http.get({
          hostname: host,
          port: PORT,
          path: `/stats?t=${encodeURIComponent(token)}`,
          timeout: 2000,
          headers: { 'Cache-Control': 'no-store' },
        }, (response) => {
          if (response.statusCode !== 200) {
            response.resume();
            finish(null);
            return;
          }
          const chunks = [];
          let size = 0;
          response.on('data', (chunk) => {
            size += chunk.length;
            if (size > PROBE_LIMIT) {
              request.destroy();
              finish(null);
              return;
            }
            chunks.push(chunk);
          });
          response.on('end', () => {
            try {
              finish(JSON.parse(Buffer.concat(chunks).toString('utf8')));
            } catch {
              finish(null);
            }
          });
          response.on('error', () => finish(null));
        });
      } catch {
        finish(null);
        return;
      }
      request.on('timeout', () => {
        request.destroy();
        finish(null);
      });
      request.on('error', () => finish(null));
    });
  }

  setSecret(name, value) {
    return this.gh(['secret', 'set', name, '--repo', this.runnerRepo], { input: value });
  }

  async ghToken(host) {
    return (await this.gh(['auth', 'token', '--hostname', host])).trim();
  }

  // -- upload -----------------------------------------------------------------

  /** POST a tar.gz of srcDir to the session; replaces ~/<name> in the VM. */
  async upload(url, srcDir, name, excludes, onProgress) {
    if (!UPLOAD_NAME.test(name || '') || name === '.' || name === '..') {
      throw new BackendError('the name may only use letters, digits, dot, dash, '
        + 'underscore (1-64 characters)');
    }
    let stats;
    try {
      stats = fs.statSync(srcDir);
    } catch {
      throw new BackendError(`${srcDir} is not a directory`);
    }
    if (!stats.isDirectory()) throw new BackendError(`${srcDir} is not a directory`);

    const excluded = new Set(Array.isArray(excludes) ? excludes : DEFAULT_EXCLUDES);
    const found = [];
    let uncompressed = 0;
    const walk = (directory) => {
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const full = path.join(directory, entry.name);
        if (entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) {
          if (excluded.has(entry.name)) continue;
          walk(full);
          continue;
        }
        if (!entry.isFile()) continue;
        let size = 0;
        try {
          size = fs.lstatSync(full).size;
        } catch {
          continue;
        }
        found.push(full);
        uncompressed += size;
      }
    };
    walk(srcDir);
    if (!found.length) throw new BackendError('nothing to copy: the directory has no files');
    if (uncompressed > UPLOAD_LIMIT) {
      throw new BackendError(`the directory is ${humanBytes(uncompressed)}; the limit is 2 GB`);
    }

    const parsed = new URL(url);
    const token = parsed.searchParams.get('t') || '';
    if (!parsed.hostname || !token) throw new BackendError('the session URL has no host or token');
    const port = Number(parsed.port) || PORT;
    const work = fs.mkdtempSync(path.join(os.tmpdir(), 'ios-ci-upload-'));
    const archive = path.join(work, 'upload.tar.gz');
    try {
      await pipeline(
        tar.c({
          gzip: true,
          cwd: srcDir,
          portable: true,
          filter: (filePath, entry) => {
            if (entry.type === 'SymbolicLink') return false;
            if (entry.type === 'Directory' && excluded.has(path.basename(entry.path))) {
              return false;
            }
            return true;
          },
        }, ['.']),
        fs.createWriteStream(archive),
      );
      const total = fs.statSync(archive).size;
      const body = await new Promise((resolve, reject) => {
        const request = http.request({
          hostname: parsed.hostname,
          port,
          method: 'POST',
          path: `/upload?t=${encodeURIComponent(token)}&name=${encodeURIComponent(name)}`,
          headers: {
            'Content-Type': 'application/gzip',
            'Content-Length': String(total),
          },
          timeout: 600_000,
        }, (response) => {
          const chunks = [];
          response.on('data', (chunk) => chunks.push(chunk));
          response.on('end', () => resolve({
            status: response.statusCode,
            text: Buffer.concat(chunks).toString('utf8'),
          }));
          response.on('error', reject);
        });
        request.on('timeout', () => request.destroy(new BackendError('the copy timed out')));
        request.on('error', reject);
        const stream = fs.createReadStream(archive, { highWaterMark: 1024 * 1024 });
        let sent = 0;
        stream.on('data', (chunk) => {
          sent += chunk.length;
          if (onProgress) onProgress(sent, total);
        });
        stream.on('error', reject);
        stream.pipe(request);
      });
      if (body.status !== 200) {
        throw new BackendError(`copy failed (${body.status}): ${body.text.trim().slice(0, 300)}`);
      }
      let result;
      try {
        result = JSON.parse(body.text);
      } catch {
        throw new BackendError(`unexpected reply: ${body.text.trim().slice(0, 200)}`);
      }
      result.sent = total;
      result.count = found.length;
      return result;
    } finally {
      fs.rmSync(work, { recursive: true, force: true });
    }
  }

  /**
   * The HTTPS credential this machine's Git helper has for `host`, or null.
   * The credential is never put in an error message or logged.
   */
  gitCredential(host, environment = process.env) {
    if (!/^[A-Za-z0-9.-]+$/.test(host || '')) return Promise.resolve(null);
    return new Promise((resolve) => {
      let child;
      try {
        child = spawn('git', ['credential', 'fill'], {
          stdio: ['pipe', 'pipe', 'pipe'],
          env: { ...environment, GIT_TERMINAL_PROMPT: '0' },
        });
      } catch {
        resolve(null);
        return;
      }
      const output = [];
      let settled = false;
      let timer = null;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      timer = setTimeout(() => {
        try {
          child.kill('SIGKILL');
        } catch {
          // Already gone.
        }
        finish(null);
      }, 10_000);
      child.stdout.on('data', (chunk) => output.push(chunk));
      child.stderr.on('data', () => {});
      child.on('error', () => finish(null));
      child.on('close', (code) => {
        finish(code === 0 ? parseCredential(Buffer.concat(output).toString('utf8')) : null);
      });
      if (child.stdin) {
        child.stdin.on('error', () => {});
        child.stdin.end(`protocol=https\nhost=${host}\n\n`);
      }
    });
  }

  /** POST /git-setup with the local identity and one or more host credentials. */
  gitSetup(vmHost, sessionToken, {
    name, email, credentials, githubHost = '', gitToken, gitHost = '', port = PORT,
  }) {
    const list = Array.isArray(credentials)
      ? credentials
      : (gitHost && gitToken
        ? [{ host: gitHost, username: 'x-access-token', token: gitToken }]
        : []);
    const payload = Buffer.from(JSON.stringify({
      name, email, credentials: list, githubHost: githubHost || gitHost,
    }));
    return new Promise((resolve, reject) => {
      const request = http.request({
        hostname: vmHost,
        port,
        method: 'POST',
        path: `/git-setup?t=${encodeURIComponent(sessionToken)}`,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': String(payload.length),
        },
        timeout: 90_000,
      }, (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => {
          const body = Buffer.concat(chunks).toString('utf8');
          if (response.statusCode !== 200) {
            let message = body.trim();
            try {
              message = JSON.parse(body).error || message;
            } catch {
              // Not JSON; use the raw text.
            }
            reject(new BackendError(message.slice(0, 300) || 'git setup failed'));
            return;
          }
          try {
            resolve(JSON.parse(body));
          } catch {
            reject(new BackendError('git setup returned an unexpected reply'));
          }
        });
        response.on('error', reject);
      });
      request.on('timeout', () => request.destroy(new BackendError('git setup timed out')));
      request.on('error', reject);
      request.end(payload);
    });
  }
}

module.exports = {
  Backend,
  BackendError,
  DEFAULT_EXCLUDES,
  PORT,
  UPLOAD_NAME,
  WORKFLOW,
  humanBytes,
  parseCredential,
  usefulLine,
};
