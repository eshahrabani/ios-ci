#!/usr/bin/env bash
# ios-ci.sh — run a Capacitor app through the public ios-ci macOS pipeline.
#
# The pipeline lives in a public repo (unmetered macOS minutes) and builds a private
# repo that it reads with a deploy key. Those two facts are repo secrets on the runner
# (APP_REPO, APP_DEPLOY_KEY), so "any app" means: point the runner at the app once
# (`configure`), then dispatch builds (`run`).
#
#   ios-ci.sh prepare [app-dir]     # add a default .maestro flow if the app has none
#   ios-ci.sh configure [app-dir]   # point the runner repo at this app
#   ios-ci.sh run [app-dir]         # dispatch, wait, download logs/screenshots/video
#
# Requires: git, gh (authenticated), jq. Writes nothing into the app repo except the
# flow `prepare` creates.
set -euo pipefail

RUNNER_REPO="${IOS_CI_RUNNER:-eshahrabani/ios-ci}"
WORKFLOW="ios-ci.yml"
TIMEOUT_MIN=60
TIMEOUT_SET=false
OUT_DIR=""
SMOKE_TEXT=""
REF=""
APP_DIR=""
DEBUG_LOGS=false
DRY_RUN=false
DEPLOY_KEY=""
MANUAL_MINUTES=0

usage() {
  cat <<'USAGE'
usage: ios-ci.sh <command> [app-dir] [options]

commands:
  prepare   [app-dir]   add .maestro/smoke.yaml when the app has no flows
  configure [app-dir]   set APP_REPO + APP_DEPLOY_KEY on the runner repo
  run       [app-dir]   dispatch the workflow, wait, download the run's artifacts

options:
  --smoke-text TEXT   text the flow asserts (default: repo variable SMOKE_TEXT,
                      else package.json name when no flow uses ${SMOKE_TEXT})
  --ref REF           branch/tag name recorded in the log summary (default: current branch)
  --debug-logs        also print raw build/test output to the public log (be careful)
  --out DIR           where to download artifacts (default: ./ci-run-<run-id>)
  --deploy-key PATH   private key for the app repo (default: ~/.ssh/ios-ci-<repo>.key,
                      generated and installed as a write deploy key when missing)
  --runner OWNER/NAME runner repo (default: eshahrabani/ios-ci or $IOS_CI_RUNNER)
  --timeout MIN       minutes to wait for the run (default: 60)
  --manual MINUTES    hold a hosted VNC desktop for manual testing (1-300)
  --dry-run           print the commands instead of executing them
  -h, --help          this text

manual sessions:
  the tester's machine must be on the same Tailscale tailnet; set repo secrets
  TAILSCALE_AUTH_KEY, VNC_PASSWORD (8 characters) and VNC_USER_PASSWORD.
  The CLI prints the VNC address when the desktop is ready; connect with any VNC
  client using VNC_PASSWORD. --manual accepts 1-300 minutes; the hosted job cap is
  360.

examples:
  ios-ci.sh prepare && ios-ci.sh run
  ios-ci.sh configure ~/git/my-app && ios-ci.sh run ~/git/my-app --smoke-text "Welcome"
  ios-ci.sh run ~/git/my-app --manual 30 --smoke-text "Welcome"
USAGE
}

die() { printf 'error: %s\n' "$*" >&2; exit 2; }
note() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

need_tools() {
  local t
  for t in git gh jq; do
    command -v "$t" >/dev/null 2>&1 || die "missing dependency: $t"
  done
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
}

# --- app discovery ---------------------------------------------------------------

app_repo_of() {
  local url
  url=$(git -C "$APP_DIR" remote get-url origin 2>/dev/null) || die "$APP_DIR has no 'origin' remote"
  printf '%s\n' "$url" \
    | sed -E 's#^git@([^:]+):#\1/#; s#^ssh://git@([^/]+)/#\1/#; s#^https?://([^/]+)/#\1/#' \
    | sed -E 's#\.git$##' \
    | awk -F/ 'NF>=3 {print $(NF-1)"/"$NF; exit}'
}

app_name_of() {
  if [[ -f "$APP_DIR/package.json" ]]; then
    jq -r '.name // empty' "$APP_DIR/package.json" 2>/dev/null || true
  fi
}

flow_uses_smoke_text() {
  [[ -d "$APP_DIR/.maestro" ]] || return 1
  grep -rq 'SMOKE_TEXT' "$APP_DIR/.maestro" 2>/dev/null
}

resolve_app_dir() {
  APP_DIR="${1:-.}"
  [[ -d "$APP_DIR" ]] || die "not a directory: $APP_DIR"
  APP_DIR=$(cd "$APP_DIR" && pwd)
  git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1 || die "$APP_DIR is not a git repository"
}

# The runner hard-codes `npm run build`, `npx cap sync ios` and a checkout of $SHA.
# Anything missing here fails inside a ten-minute macOS job; check it first.
preflight() {
  local build_ok=false cap_ok=false
  if [[ -f "$APP_DIR/package.json" ]]; then
    [[ "$(jq -r '.scripts.build // empty' "$APP_DIR/package.json")" != "" ]] && build_ok=true
    [[ "$(jq -r '(.dependencies // {}) + (.devDependencies // {}) | has("@capacitor/cli")' "$APP_DIR/package.json")" == "true" ]] && cap_ok=true
  fi
  [[ "$build_ok" == true ]] || die "package.json has no \"build\" script; the runner runs 'npm run build'"
  [[ "$cap_ok" == true ]] || warn "@capacitor/cli is not a dependency; the runner's 'npx cap sync ios' will try to fetch it"
  if [[ -z "$(git -C "$APP_DIR" branch -r --contains HEAD 2>/dev/null)" ]]; then
    warn "HEAD ($(git -C "$APP_DIR" rev-parse --short HEAD)) is not on any remote-tracking branch;
     push it first or the private checkout will fail"
  fi
}

current_branch() {
  git -C "$APP_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main
}

# --- commands --------------------------------------------------------------------

cmd_prepare() {
  resolve_app_dir "$1"
  local flow="$APP_DIR/.maestro/smoke.yaml"
  if [[ -f "$flow" ]]; then
    note "flow already exists: $flow"
    return 0
  fi
  local count=0
  if [[ -d "$APP_DIR/.maestro" ]]; then
    count=$(find "$APP_DIR/.maestro" -name '*.yaml' -o -name '*.yml' | wc -l | tr -d ' ')
  fi
  if [[ "$count" -gt 0 ]]; then
    note ".maestro already has $count flow(s); not touching them"
    return 0
  fi
  mkdir -p "$APP_DIR/.maestro"
  # Assert nothing: a generic flow has to work for an app whose copy it cannot know.
  # It still proves the app launches and gives you a screenshot and a recording.
  cat > "$flow" <<'YAML'
appId: ${APP_ID}
---
- startRecording: smoke
- launchApp:
    clearState: true
- waitForAnimationToEnd:
    timeout: 10000
- takeScreenshot: home-screen
- stopRecording
YAML
  note "created $flow (launches the app, screenshots it, records it; asserts nothing)"
  note "commit it, then: ios-ci.sh run"
}

cmd_configure() {
  resolve_app_dir "$1"
  [[ "$DRY_RUN" == true ]] || need_tools
  local app repo key key_pub title
  app=$(app_repo_of) || exit 2
  [[ -n "$app" ]] || die "could not derive OWNER/NAME from $APP_DIR's origin remote"
  repo=$(git -C "$APP_DIR" rev-parse --show-toplevel)

  if [[ -n "$DEPLOY_KEY" ]]; then
    key="$DEPLOY_KEY"
  else
    local slug
    slug=$(printf '%s' "$app" | tr '/' '-')
    key="$HOME/.ssh/ios-ci-${slug}.key"
  fi
  key_pub="${key}.pub"

  if [[ ! -f "$key" ]]; then
    command -v ssh-keygen >/dev/null 2>&1 || die "missing dependency: ssh-keygen (or pass --deploy-key)"
    if [[ "$DRY_RUN" == true ]]; then
      note "[dry-run] ssh-keygen -t ed25519 -N '' -C ios-ci:$app -f $key"
    else
      mkdir -p "$(dirname "$key")"
      ssh-keygen -q -t ed25519 -N '' -C "ios-ci:$app" -f "$key"
      note "generated $key"
    fi
  fi
  if [[ ! -f "$key_pub" && -f "$key" && "$DRY_RUN" != true ]]; then
    ssh-keygen -y -f "$key" > "$key_pub"
  fi

  title="ios-ci-$(date +%Y%m%d%H%M%S)"
  note "app repo : $app (from $repo)"
  note "runner   : $RUNNER_REPO"
  note "deploy key: $key (write — the runner pushes its log branch with it)"

  if [[ "$DRY_RUN" == true ]]; then
    note "[dry-run] gh repo deploy-key add $key_pub --repo $app --allow-write --title $title"
    note "[dry-run] gh secret set APP_REPO --repo $RUNNER_REPO --body $app"
    note "[dry-run] gh secret set APP_DEPLOY_KEY --repo $RUNNER_REPO < $key"
    return 0
  fi

  if gh api "repos/$app/keys" --jq '.[].title' 2>/dev/null | grep -qx "$title"; then
    note "deploy key '$title' already installed"
  else
    gh repo deploy-key add "$key_pub" --repo "$app" --allow-write --title "$title" \
      || die "could not install the deploy key on $app (admin rights needed there)"
  fi
  gh secret set APP_REPO --repo "$RUNNER_REPO" --body "$app" >/dev/null
  gh secret set APP_DEPLOY_KEY --repo "$RUNNER_REPO" < "$key" >/dev/null
  note "pointed $RUNNER_REPO at $app"
}

cmd_run() {
  resolve_app_dir "$1"
  preflight
  if [[ "$MANUAL_MINUTES" != "0" ]]; then
    [[ "$MANUAL_MINUTES" =~ ^[0-9]+$ ]] || die "--manual needs a whole number of minutes"
    # 10# avoids bash reading a leading zero as octal (`--manual 08`).
    MANUAL_MINUTES=$((10#$MANUAL_MINUTES))
    (( MANUAL_MINUTES >= 1 && MANUAL_MINUTES <= 300 )) \
      || die "--manual must be 1-300 minutes"
    # The job cap is 60 + manual_minutes, so the wait must outlast that.
    if [[ "$TIMEOUT_SET" != true ]]; then TIMEOUT_MIN=$((75 + MANUAL_MINUTES)); fi
  fi
  local app sha export_ref text
  app=$(app_repo_of) || exit 2
  [[ -n "$app" ]] || die "could not derive OWNER/NAME from $APP_DIR's origin remote"
  sha=$(git -C "$APP_DIR" rev-parse HEAD)
  export_ref="${REF:-$(current_branch)}"

  if [[ -z "$SMOKE_TEXT" && "$DRY_RUN" != true ]]; then
    SMOKE_TEXT=$(gh variable get SMOKE_TEXT --repo "$app" 2>/dev/null || true)
  fi
  if [[ -z "$SMOKE_TEXT" ]] && flow_uses_smoke_text; then
    die "a flow in $APP_DIR/.maestro uses \${SMOKE_TEXT} but no text was given.
     pass --smoke-text \"...\", or set it once:
     gh variable set SMOKE_TEXT --repo $app --body \"the text on your first screen\""
  fi
  if [[ -z "$SMOKE_TEXT" ]]; then
    SMOKE_TEXT=$(app_name_of)
    [[ -n "$SMOKE_TEXT" ]] || SMOKE_TEXT="-"
    warn "no --smoke-text and no SMOKE_TEXT variable; passing '$SMOKE_TEXT' (only used if the flow asserts it)"
  fi

  local correlation
  correlation="cli-$(date +%s)-$$"
  note "app    : $app @ ${sha:0:7} ($export_ref)"
  note "runner : $RUNNER_REPO"

  if [[ "$DRY_RUN" == true ]]; then
    note "[dry-run] gh workflow run $WORKFLOW --repo $RUNNER_REPO --ref main \\"
    note "            -f sha=$sha -f ref=$export_ref \\"
    note "            -f correlation_id=$correlation -f smoke_text='$SMOKE_TEXT' \\"
    note "            -f debug_public_logs=$DEBUG_LOGS \\"
    note "            -f manual_minutes=$MANUAL_MINUTES"
    note "timeout : ${TIMEOUT_MIN} min"
    if [[ "$MANUAL_MINUTES" != "0" ]]; then
      note "manual : a hosted VNC desktop for ${MANUAL_MINUTES} min; the address is published to the private ci-logs branch"
    fi
    return 0
  fi

  need_tools
  if [[ "$MANUAL_MINUTES" != "0" ]]; then
    local secret_names
    if secret_names=$(gh secret list --repo "$RUNNER_REPO" --json name --jq '.[].name' 2>/dev/null); then
      for s in TAILSCALE_AUTH_KEY VNC_PASSWORD VNC_USER_PASSWORD; do
        grep -qx "$s" <<<"$secret_names" || die "missing secret $s on $RUNNER_REPO.
     set it with: gh secret set $s --repo $RUNNER_REPO"
      done
    else
      warn "could not list secrets on $RUNNER_REPO; ensure TAILSCALE_AUTH_KEY, VNC_PASSWORD and VNC_USER_PASSWORD are set"
    fi
  fi
  local baseline_id
  baseline_id=$(gh run list --repo "$RUNNER_REPO" --workflow "$WORKFLOW" --limit 1 \
    --json databaseId 2>/dev/null | jq -r '.[0].databaseId // 0' 2>/dev/null || echo 0)
  [[ "$baseline_id" =~ ^[0-9]+$ ]] || baseline_id=0
  gh workflow run "$WORKFLOW" --repo "$RUNNER_REPO" --ref main \
    -f "sha=$sha" -f "ref=$export_ref" \
    -f "correlation_id=$correlation" -f "smoke_text=$SMOKE_TEXT" \
    -f "debug_public_logs=$DEBUG_LOGS" \
    -f "manual_minutes=$MANUAL_MINUTES" >/dev/null \
    || die "dispatch failed (does the runner accept workflow_dispatch on main?)"

  # Exact title match: correlations are unique per dispatch, but a substring match
  # would also hit a longer id that happens to start with this one.
  local title run_id="" waited=0 manual_ready=false runs stuck
  title="ios-ci $correlation"
  while [[ "$waited" -lt 300 ]]; do
    runs=$(gh run list --repo "$RUNNER_REPO" --workflow "$WORKFLOW" --limit 100 \
      --json databaseId,displayTitle,conclusion 2>/dev/null || true)
    run_id=$(printf '%s' "$runs" | jq -r --arg t "$title" \
      'map(select(.displayTitle == $t)) | first | .databaseId // empty' 2>/dev/null || true)
    [[ -n "$run_id" ]] && break
    # A workflow-file error (bad expression, an action the repo's policy forbids)
    # creates a run that never gets its run-name and has no jobs, so the title match
    # above can never hit. Report it now instead of waiting out the five minutes.
    stuck=$(printf '%s' "$runs" | jq -r --argjson b "${baseline_id:-0}" \
      'map(select(.databaseId > $b and .conclusion == "startup_failure")) | first | .databaseId // empty' 2>/dev/null || true)
    if [[ -n "$stuck" ]]; then
      die "the run never started: GitHub rejected the workflow file.
     https://github.com/$RUNNER_REPO/actions/runs/$stuck"
    fi
    sleep 5; waited=$((waited + 5))
  done
  [[ -n "$run_id" ]] || die "dispatched run '$title' did not appear within 5 minutes"
  note "run    : https://github.com/$RUNNER_REPO/actions/runs/$run_id"

  local status conclusion elapsed=0
  while [[ "$elapsed" -lt $((TIMEOUT_MIN * 60)) ]]; do
    if [[ "$MANUAL_MINUTES" != "0" && "$manual_ready" != true ]]; then
      local manual_file vnc_address vnc_user
      manual_file=$(gh api "repos/$app/contents/ci-logs/manual-ready.txt?ref=ci-logs" \
        -H 'Accept: application/vnd.github.raw' 2>/dev/null || true)
      if [[ -n "$manual_file" ]]; then
        vnc_address=$(printf '%s\n' "$manual_file" | sed -n 's/^vnc_address: //p' | head -1)
        vnc_user=$(printf '%s\n' "$manual_file" | sed -n 's/^vnc_user: //p' | head -1)
        if [[ -n "$vnc_address" ]]; then
          manual_ready=true
          note "manual : VNC desktop ready at $vnc_address"
          note "         VNC password: the 8-character VNC_PASSWORD secret"
          note "         Apple Screen Sharing / RealVNC: user ${vnc_user:-runner}, password VNC_USER_PASSWORD"
        fi
      fi
    fi
    status=$(gh api "repos/$RUNNER_REPO/actions/runs/$run_id" --jq '.status')
    [[ "$status" == "completed" ]] && break
    sleep 20; elapsed=$((elapsed + 20))
    if (( elapsed % 120 == 0 )); then note "  ... $status (${elapsed}s)"; fi
  done
  [[ "$status" == "completed" ]] || die "run did not finish within ${TIMEOUT_MIN} minutes"

  conclusion=$(gh api "repos/$RUNNER_REPO/actions/runs/$run_id" --jq '.conclusion')
  local out="${OUT_DIR:-ci-run-${run_id}}"
  fetch_artifacts "$app" "$out"
  report "$run_id" "$app" "$out" "$app"
  case "$conclusion" in
    success) return 0 ;;
    *)
      warn "public run concluded: $conclusion"
      gh api "repos/$RUNNER_REPO/actions/runs/$run_id/jobs" \
        --jq '.jobs[] | "job \(.name): \(.conclusion // "unknown")", (.steps[] | select(.conclusion == "failure") | "  failed step: \(.name)")' \
        2>/dev/null || true
      note "logs: $out (and https://github.com/$app/tree/ci-logs)"
      return 1 ;;
  esac
}

# --- artifacts -------------------------------------------------------------------

fetch_artifacts() {
  local app="$1" out="$2" path size
  mkdir -p "$out"
  local tree
  if ! tree=$(gh api "repos/$app/git/trees/ci-logs?recursive=1" 2>/dev/null); then
    warn "no ci-logs branch on $app yet"
    return 0
  fi
  printf '%s' "$tree" | jq -r '.tree[] | select(.type == "blob") | .path' | while IFS= read -r path; do
    [[ "$path" == ci-logs/* || "$path" == README.md ]] || continue
    mkdir -p "$out/$(dirname "$path")"
    if gh api "repos/$app/contents/$path?ref=ci-logs" -H 'Accept: application/vnd.github.raw' \
         > "$out/$path" 2>/dev/null; then
      size=$(wc -c < "$out/$path" | tr -d ' ')
      note "  downloaded $path (${size} bytes)"
    else
      warn "  could not download $path"
    fi
  done
}

report() {
  local run_id="$1" app="$2" out="$3"
  local summary="$out/ci-logs/summary.txt"
  local reported_repo="" manual=""
  [[ -f "$summary" ]] && reported_repo=$(sed -n 's/^app_repo: //p' "$summary" | head -1)
  [[ -f "$summary" ]] && manual=$(sed -n 's/^manual_minutes: //p' "$summary" | head -1)

  note ""
  note "run $run_id → $out"
  if [[ -n "$reported_repo" && "$reported_repo" != "$4" ]]; then
    warn "this run built '$reported_repo', not '$4' — the runner is pointed elsewhere:"
    warn "  $0 configure $APP_DIR"
  fi
  if [[ -f "$summary" ]]; then
    note "  attempts : $(sed -n 's/^maestro_attempts: //p' "$summary" | head -1)"
    note "  simulator: $(sed -n 's/^simulator: //p' "$summary" | head -1)"
    note "  xcode    : $(sed -n 's/^xcode: //p' "$summary" | head -1 | sed 's/ *$//')"
    if [[ -n "$manual" && "$manual" != "0" ]]; then note "  duration : ${manual} min"; fi
  fi
  if [[ -f "$out/ci-logs/maestro.log" ]]; then
    note "  flow     : $(grep -E 'Flow (Passed|Failed)|flow passed|flow failed' "$out/ci-logs/maestro.log" | tail -1 || echo 'see maestro.log')"
  fi
  if [[ -f "$out/ci-logs/manual-ready.txt" ]]; then
    note "  manual   : $(sed -n 's/^vnc_address: //p' "$out/ci-logs/manual-ready.txt" | head -1)"
  fi
  local media
  media=$(find "$out" -type f \( -name '*.png' -o -name '*.mp4' -o -name '*.mov' \) 2>/dev/null | sort)
  if [[ -n "$media" ]]; then
    note "  media    :"
    printf '%s\n' "$media" | while IFS= read -r f; do note "    $f"; done
  else
    note "  media    : none (the flow has to call takeScreenshot/startRecording)"
  fi
  note "  rendered : https://github.com/$app/tree/ci-logs"
}

# --- entry point ----------------------------------------------------------------

command=""
if [[ $# -gt 0 ]]; then
  case "$1" in
    prepare|configure|run) command="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown command: $1 (try --help)" ;;
  esac
fi

APP_DIR="."
positional=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke-text) SMOKE_TEXT="${2:?--smoke-text needs a value}"; shift 2 ;;
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --out) OUT_DIR="${2:?--out needs a value}"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="${2:?--deploy-key needs a path}"; shift 2 ;;
    --runner) RUNNER_REPO="${2:?--runner needs OWNER/NAME}"; shift 2 ;;
    --timeout) TIMEOUT_MIN="${2:?--timeout needs minutes}"; TIMEOUT_SET=true; shift 2 ;;
    --manual) MANUAL_MINUTES="${2:?--manual needs minutes}"; shift 2 ;;
    --debug-logs) DEBUG_LOGS=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) positional="$1"; shift ;;
  esac
done
[[ -n "$positional" ]] && APP_DIR="$positional"

case "$command" in
  prepare) cmd_prepare "$APP_DIR" ;;
  configure) cmd_configure "$APP_DIR" ;;
  run) cmd_run "$APP_DIR" ;;
  *) usage; exit 2 ;;
esac
