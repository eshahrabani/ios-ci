# ios-ci

CI configuration only. No application source is stored here.

## Quickstart

Requires `git`, `gh` (authenticated) and `jq`.

```bash
bin/ios-ci.sh prepare ~/git/my-app
bin/ios-ci.sh configure ~/git/my-app
bin/ios-ci.sh run ~/git/my-app --smoke-text "Welcome"
bin/ios-ci.sh run ~/git/my-app --manual 30 --smoke-text "Welcome"
```

Run `bin/ios-ci.sh --help` for options and the manual-session secrets.
