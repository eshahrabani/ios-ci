'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { Backend } = require('../lib/backend');

test('git-setup separates the session token from the GitHub token', async () => {
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
      gitHost: 'github.com',
      gitToken: 'github-token',
      port,
    });
    assert.equal(seen.url, '/git-setup?t=session-token');
    assert.equal(seen.body.token, 'github-token');
    assert.equal(seen.body.host, 'github.com');
    assert.equal(seen.body.name, 'Ada Lovelace');
    assert.equal(result.login, 'ada');
  } finally {
    server.close();
  }
});
