# ios-ci

CI configuration only. No application source is stored here.

`ios-ci` starts a GitHub Actions run that hands over a generic macOS desktop: the runner
joins your tailnet, serves its screen to a browser page, injects your clicks and keys,
and holds the VM for the minutes you asked for. The local client is an Electron app with
a setup wizard, one-click sessions, history, and the streamed desktop in a window.

Every booted VM is provisioned before the desktop is served: Xcode, an iOS runtime with a
booted iPhone simulator, Node.js, Yarn 4 and Git. Your Git name, email and Git
credentials are installed too, so a private clone works immediately.

## Requirements

- Node.js 22.12 or newer (for the app).
- `gh`, authenticated with `repo` and `workflow` scopes.
- `tailscale`, connected to the tailnet.
- On the runner repository: the `TAILSCALE_AUTH_KEY` secret, a reusable auth key
  (`tskey-auth-…`) from <https://login.tailscale.com/admin/settings/keys> — not an OAuth
  client secret. The wizard saves it for you.

## Quickstart

```bash
npm install
npm start            # or ./run.sh
```

On Linux, Electron needs either a root-owned setuid
`node_modules/electron/dist/chrome-sandbox` or unprivileged user namespaces. When
neither is available, `npm start`/`./run.sh` warn and launch with `--no-sandbox`.
That is weaker than the sandbox; restore it once with:

```bash
sudo chown root:root node_modules/electron/dist/chrome-sandbox
sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
```

npm drops those permissions on a clean install, so repeat after reinstalling. Set
`IOS_CI_REQUIRE_SANDBOX=1` to make the fallback an error instead.

The setup wizard checks GitHub CLI, sign-in and scopes, Tailscale, the tailnet, the runner
repository and its workflow, the Tailscale secret, and the Git identity. `Fix what I can`
repairs everything except the auth key, which is the one value that cannot be derived
from your machine. Once every row passes, the session view is one click away.

```bash
gh secret set TAILSCALE_AUTH_KEY --repo <owner>/<repo>   # equivalent to the wizard's key form
```

Session settings live under Electron's `userData` directory (`settings.json`, mode 0600).
`IOS_CI_CONFIG_DIR` overrides that directory; `IOS_CI_RUNNER` changes the default runner
repository; `IOS_CI_REF` dispatches a branch instead of `main`.

## The session

* The duration is 15, 30, 60 or 120 minutes, or 1–300 custom. The clock starts when the
  desktop is ready, not at dispatch.
* The app polls `gh` and the runner's `/stats`, and turns green only when frames, input,
  the toolchain and Git credentials all report ready.
* An optional local folder is copied into `~/<name>` in the VM when the desktop is ready,
  or with **Copy now**.
* The session token is generated locally; only its SHA-256 is dispatched, so a run's
  metadata is not a credential. It lives in the settings file so a restart can reconnect.

Expect the desktop about a minute after starting, depending on the runner queue. The
first session at a new commit compiles the screen streamer (~25 s); later sessions restore
it from the cache. Provisioning adds time only when Xcode's iOS runtime must be downloaded.

Private clones work for the active `gh` host (github.com or a GHES host) and
`gitlab.com`:

* The app sends the `gh` token for the GitHub host and reads the local HTTPS credential
  for GitLab with `git credential fill`. It also tries hosts named by the remotes of the
  folder chosen under **Copy a local folder**.
* HTTPS remotes use the installed credential helper.
* `git@<host>:org/repo.git` and `ssh://git@<host>/…` are rewritten to HTTPS.

Other hosts, and SSH keys, are not copied. Both machines must be on the same tailnet,
and its ACLs must allow TCP 6080 to the runner node.

## Development

```bash
npm test                                  # progress model, settings, identity
node --check main.js lib/*.js renderer/app.js preload.js
python3 -m py_compile bin/desktop-server.py
bash -n bin/vm-provision.sh
```

Layout: `main.js` (window and IPC), `preload.js` (the only bridge), `lib/` (settings,
backend, progress, setup, session), `renderer/` (plain HTML/CSS/JS), `bin/desktop-server.py`
(the VM-side server), `bin/screen-stream.swift` (the capture process) and
`bin/vm-provision.sh` (Xcode, simulator, Node, Yarn, Git).
