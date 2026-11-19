# ios-ci

CI configuration only. No application source is stored here.

## Quickstart

Requires `git`, `gh` (authenticated) and `jq`.

```bash
bin/ios-ci.sh prepare ~/git/my-app
bin/ios-ci.sh configure ~/git/my-app
bin/ios-ci.sh run ~/git/my-app --smoke-text "Welcome"
bin/ios-ci.sh run ~/git/my-app --manual 30
```

`--manual` boots a desktop instead of building: the app is cloned into `~/<app>` with
Xcode, the iOS simulators, node and yarn ready. The CLI prints a URL; open it in a
browser to see and drive the desktop (frames refresh about once a second).

Run `bin/ios-ci.sh --help` for options and the Tailscale requirement.
