# ios-ci

CI configuration only. No application source is stored here.

One dispatch hands over a generic macOS desktop: a GitHub-hosted runner joins your
tailnet, serves its screen to a browser page, injects your clicks and keys, and holds
the VM for the minutes you asked for. Nothing is checked out, nothing is opened, and
no app is involved — it is a desktop you drive yourself.

## Quickstart

Requires `gh` (authenticated), `jq`, `tailscale` (connected to the tailnet) and
`python3`. The runner repository needs one secret, `TAILSCALE_AUTH_KEY`: a **reusable
auth key** (`tskey-auth-…`) from
<https://login.tailscale.com/admin/settings/keys>, not an OAuth client secret.

```bash
gh secret set TAILSCALE_AUTH_KEY --repo eshahrabani/ios-ci
bin/ios-ci.sh session 30          # dispatch, wait for ready, print the URL
bin/ios-ci-gui                    # the same session with a window around it
```

Both print (or show) a URL like `http://100.64.0.9:6080/?t=…`. Open it in a browser:
that is the desktop. The page holds one WebSocket, so the VM streams compositor frames
and the browser paints only what changed; clicks, drags, scrolling, typing and Cmd-key
chords go back over the same socket. Treat the URL as a password — it grants control of
the desktop.

Expect the URL about a minute after starting, depending on the GitHub runner queue.
`bin/ios-ci-gui` shows the queue and each phase with a percent and an ETA, and turns its
button green only when `/stats` proves both frames and input work. The first session at
a new commit compiles the screen streamer (~25 s); later sessions restore it from the
cache.

The session token is generated on your machine and never leaves it: only its SHA-256 is
dispatched, so a run's metadata is not a credential. The token lives in the GUI's
settings file (mode 0600) so a restart can reconnect.

## The desktop

* `bin/ios-ci.sh session [minutes]` — 1–300 minutes, default 30. The clock starts when
  the desktop is ready, not at dispatch. Stop early with
  `gh run cancel <run_id> --repo <runner>`.
* `bin/ios-ci-gui` — minutes, live progress with a per-step breakdown, the green ready
  light, recent sessions with their remaining time, and an optional local directory.
  The directory is copied into `~/<name>` in the VM as soon as the desktop is ready (or
  with **Copy now**), and the first start creates a venv under `~/.cache/ios-ci-gui`
  with PySide6.
* Both machines must be on the same tailnet, and its ACLs must allow TCP 6080 to the
  runner node. If the node appears but the probe times out, that is an ACL.

Run `bin/ios-ci.sh --help` for the full usage.
