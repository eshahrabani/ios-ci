# ios-ci

CI configuration only. No application source is stored here.

A macOS runner that builds a **private** iOS app on **public** macOS minutes: this repo is
public, so its `macos-26` minutes are unmetered, while the app, its secrets and its build
output stay private. The app repo triggers it over `workflow_dispatch` and polls the run.

## Contract

`workflow_dispatch` inputs:

| Input | Required | Meaning |
|---|---|---|
| `sha` | yes | Commit in the private app repo to build (checked out directly, so no clone of `main`) |
| `ref` | no | Branch/tag name, diagnostics only |
| `correlation_id` | yes | Echoed in the run name (`run-name: ios-ci <id>`) so the dispatcher can find this run |
| `smoke_text` | yes | Text asserted by the Maestro flow; supplied by the caller, never committed here |
| `debug_public_logs` | no | `true` also prints raw build/test output to the **public** log. Default `false` |

Secrets (this repo): `APP_REPO` (`owner/name`) and `APP_DEPLOY_KEY` (write deploy key on the
app repo — needed to push logs, not to read it).

The app repo must provide: a `build` script producing `webDir` assets, a Capacitor iOS
project (created with `cap add ios` if absent), and `.maestro/` flows using `${APP_ID}` and
`${SMOKE_TEXT}`.

## What a run does

1. Checkout the requested SHA with the deploy key, then **delete the key from the runner**
   before any app code executes. npm lifecycle scripts, Vite plugins and Xcode build phases
   are untrusted relative to a key that can write the app repo, so the key is re-created only
   inside the publish step and removed by a `trap`.
2. Select Xcode, install Node 22 + web dependencies, build web assets, `cap sync ios`.
3. `xcodebuild` for `generic/platform=iOS Simulator` (no device or runtime needed at build
   time), install on the preferred iPhone simulator (500 s boot wait, 10-minute step cap).
4. `maestro test` the app's `.maestro/` flows with a pinned CLI (2.12.1). The first XCTest
   driver start gets 480 s (measured: 5.5 minutes of `xcodebuild` before the runner launches on
   a fresh simulator); if it still never opens its port, the orphaned `xcodebuild` is killed and
   the attempt is retried once with 300 s. Assertion failures are never retried. 20-minute step
   cap.
5. Always: force-push `ci-logs` to the app repo with `xcodebuild.log`, `maestro-N.log`,
   `maestro.xml`, Maestro's `--debug-output`/`--test-output-dir` trees and a `summary.txt`.

No build output, test text or app source is printed publicly unless `debug_public_logs=true`.
Logs, the report and artifacts are also deleted from this repo right after the run by
`cleanup.yml`, so retention never applies.

## Limits

- One `concurrency` group: a newer dispatch cancels a run that is still going.
- Job timeout 45 minutes; the dispatcher waits up to 60.
- The Maestro iOS driver's first start on a fresh simulator is the flakiest part of the job:
  run `43282267102` spent 5.5 minutes preparing the runner, then the retry started it in 21 s
  but had its runner killed by the un-reaped first session. Timeout, reaper and published
  driver logs are the mitigation, not a guarantee.
- Review GitHub's Additional Product Terms before relying on this: hosted runners are meant
  for the repository's own software, and this repo only builds another repository's.
