#!/usr/bin/env node
// Launch Electron from this checkout.
//
// Chromium's Linux sandbox needs either a root-owned, setuid
// node_modules/electron/dist/chrome-sandbox or unprivileged user namespaces.
// npm installs the helper as the invoking user (0755), and Ubuntu 23.10+
// restricts user namespaces through AppArmor, so a fresh checkout can fail
// before any app code runs. When both paths are unavailable, launch with
// --no-sandbox and print the one-time fix. IOS_CI_REQUIRE_SANDBOX=1 makes the
// fallback an error instead.
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const electron = require('electron');
const helper = path.join(path.dirname(electron), 'chrome-sandbox');

function sandboxHelperOk() {
  try {
    const stat = fs.statSync(helper);
    return stat.uid === 0 && (stat.mode & 0o4000) !== 0 && (stat.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

function userNamespacesWork() {
  // -Ur is the capability Chromium's namespace sandbox uses. A missing
  // unshare(1) means we cannot prove it works.
  const probe = spawnSync('unshare', ['-Ur', 'true'], { stdio: 'ignore', timeout: 2000 });
  return probe.status === 0;
}

const userArgs = process.argv.slice(2);
const explicitlyDisabled = userArgs.includes('--no-sandbox')
  || Boolean(process.env.ELECTRON_DISABLE_SANDBOX);
const args = ['.', ...userArgs];

if (process.platform === 'linux' && !explicitlyDisabled
  && !sandboxHelperOk() && !userNamespacesWork()) {
  if (process.env.IOS_CI_REQUIRE_SANDBOX === '1') {
    console.error('ios-ci: Chromium cannot sandbox this checkout.');
    console.error(`  sudo chown root:root ${helper}`);
    console.error(`  sudo chmod 4755 ${helper}`);
    process.exit(1);
  }
  console.warn('ios-ci: Chromium cannot sandbox this checkout; launching with --no-sandbox.');
  console.warn('ios-ci: restore the sandbox with:');
  console.warn(`  sudo chown root:root ${helper}`);
  console.warn(`  sudo chmod 4755 ${helper}`);
  console.warn('ios-ci: set IOS_CI_REQUIRE_SANDBOX=1 to fail instead of falling back.');
  args.unshift('--no-sandbox');
}

const child = spawn(electron, args, { stdio: 'inherit' });
child.on('error', (error) => {
  console.error(`ios-ci: could not start Electron: ${error.message}`);
  process.exit(1);
});
child.on('exit', (code) => process.exit(code ?? 1));
