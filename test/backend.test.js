'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { Backend, parseCredential } = require('../lib/backend');

test('git-setup separates the session token from host credentials', async () => {
  let seen = null;
  const server = http.createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      seen = { url: request.url, body: JSON.parse(body) };
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        ok: true,
        name: 'Ada Lovelace',
        email: 'ada@example.com',
        host: 'github.com',
        login: 'ada',
        hosts: [{ host: 'github.com', login: 'ada' }, { host: 'gitlab.com', login: 'ada' }],
        credential_helper: true,
      }));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    const backend = new Backend('owner/repo');
    const result = await backend.gitSetup('127.0.0.1', 'session-token', {
      name: 'Ada Lovelace',
      email: 'ada@example.com',
      credentials: [
        { host: 'github.com', username: 'x-access-token', token: 'github-token' },
        { host: 'gitlab.com', username: 'ada', token: 'gitlab-token' },
      ],
      githubHost: 'github.com',
      port,
    });
    assert.equal(seen.url, '/git-setup?t=session-token');
    assert.deepEqual(seen.body.credentials, [
      { host: 'github.com', username: 'x-access-token', token: 'github-token' },
      { host: 'gitlab.com', username: 'ada', token: 'gitlab-token' },
    ]);
    assert.equal(seen.body.githubHost, 'github.com');
    assert.equal(seen.body.name, 'Ada Lovelace');
    assert.equal(result.login, 'ada');
    assert.equal(result.hosts.length, 2);
  } finally {
    server.close();
  }
});

test('dispatch sends the selected VM architecture', async () => {
  for (const arch of ['arm64', 'x64']) {
    const backend = new Backend('owner/repo');
    backend.newestRunId = async () => 0;
    const calls = [];
    let correlation = '';
    backend.gh = async (args) => {
      calls.push(args);
      if (args[0] === 'workflow') {
        correlation = args.find((arg) => arg.startsWith('correlation_id='))
          .slice('correlation_id='.length);
        return '';
      }
      return JSON.stringify([{ databaseId: 42, displayTitle: `ios-ci ${correlation}` }]);
    };
    const runId = await backend.dispatch(30, 'a'.repeat(64), arch);
    assert.equal(runId, 42);
    assert.ok(calls[0].includes(`runner_arch=${arch}`));
  }
});

test('credential parsing keeps an equals sign in the password', () => {
  assert.deepEqual(parseCredential('username=ada\npassword=abc=def\n'), {
    username: 'ada', password: 'abc=def',
  });
  assert.equal(parseCredential('username=ada\n'), null);
});

test('gitCredential reads the local helper without exposing its reply', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ios-ci-git-cred-'));
  const git = path.join(directory, 'git');
  fs.writeFileSync(git, '#!/bin/sh\ncat >/dev/null\n'
    + 'printf "username=ada\\npassword=abc=def\\n"\n');
  fs.chmodSync(git, 0o755);
  try {
    const backend = new Backend('owner/repo');
    const credential = await backend.gitCredential('gitlab.com', {
      ...process.env, PATH: directory,
    });
    assert.deepEqual(credential, { username: 'ada', password: 'abc=def' });
    assert.equal(await backend.gitCredential('../bad', {
      ...process.env, PATH: directory,
    }), null);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
