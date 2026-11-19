# ios-ci

CI configuration only. No application source is stored here.

## Quickstart

Requires `git`, `gh` (authenticated) and `jq`.

```bash
bin/ios-ci.sh prepare ~/git/my-app
bin/ios-ci.sh configure ~/git/my-app
bin/ios-ci.sh run ~/git/my-app --smoke-text "Welcome"
bin/ios-ci.sh run ~/git/my-app --manual 30
bin/ios-ci-gui
```

`--manual` boots a desktop instead of building: the app is cloned into `~/<app>` with
Xcode, the iOS simulators, node and yarn ready. The CLI prints a URL; open it in a
browser to see and drive the desktop. The page holds one WebSocket: the VM streams
compositor frames and the browser paints only the changed rectangles, so input feels
local instead of refreshing a screenshot about once a second.

`bin/ios-ci-gui` is the same session with a window around it: it starts and stops
runs, shows live and recent sessions with the time left, applies a boot config (Xcode,
simulator device/runtime/platform, node, brew packages, a setup script) and uploads a
local directory into the VM's home. The first start creates a venv under
`~/.cache/ios-ci-gui` and installs PySide6; it needs the same repo secrets as the CLI
and a machine on the same tailnet.

Run `bin/ios-ci.sh --help` for options and the Tailscale requirement.
