#!/usr/bin/env bash
# ios-ci.sh — start a browser-streamed macOS desktop on a hosted runner.
#
# One command dispatches one job that joins the tailnet, starts the desktop server and
# its frame capture, proves that frames and input work, and then holds the VM for the
# requested minutes. Nothing is checked out and nothing is opened in the VM: it is a
# generic desktop that the tester drives from their browser. The page holds one
# WebSocket, so the VM streams compositor frames instead of refreshing a screenshot.
#
# The session token is generated here and never leaves this machine: only its SHA-256
# is dispatched, and the server compares hashes in constant time. Whoever holds the URL
# can drive the desktop, so keep it to yourself.
#
#   ios-ci.sh session 30            # dispatch, wait for ready, print the URL
#
# Requires: gh (authenticated), jq, tailscale (connected), python3. The runner repo
# needs one repository secret, TAILSCALE_AUTH_KEY: a reusable auth key (tskey-auth-…),
# not an OAuth client secret.
set -euo pipefail

RUNNER_REPO="${IOS_CI_RUNNER:-eshahrabani/ios-ci}"
WORKFLOW="ios-ci.yml"
PORT=6080
DEFAULT_MINUTES=30
DRY_RUN=false
MINUTES=""

usage() {
  cat <<'USAGE'
usage: ios-ci.sh session [minutes] [--runner OWNER/NAME] [--dry-run] [-h|--help]

session   dispatch a macOS desktop session and wait until it is usable, then print
          the URL and exit. The workflow holds the desktop for the requested minutes
          (1-300, default 30) counted from the moment it is ready.

options:
  --runner OWNER/NAME  runner repo (default: eshahrabani/ios-ci or $IOS_CI_RUNNER)
  --dry-run            print the dispatch command and the expected tailnet host, then
                       exit without contacting GitHub or tailscale
  -h, --help           this text

the desktop:
  the VM joins the tailnet as ios-ci-<run_id>; this script finds it there, probes
  http://<tailnet address>:6080/stats until frames and input are both verified, and
  prints http://<tailnet address>:6080/?t=<token>. Open that URL in a browser to see
  and drive the desktop. Both machines must be on the same tailnet, and the tailnet's
  ACLs must allow TCP 6080 to the runner node.

  the session token is generated locally; only its SHA-256 is dispatched. Treat the
  printed URL as a password: it grants control of the desktop.

  stop a session early with: gh run cancel <run_id> --repo <runner>

examples:
  ios-ci.sh session
  ios-ci.sh session 5
  ios-ci.sh session 60 --runner me/ios-ci
  ios-ci.sh session 5 --dry-run
USAGE
}

die() { printf 'error: %s\n' "$*" >&2; exit 2; }
note() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

need_tools() {
  local tool names
  for tool in gh jq tailscale python3; do
    command -v "$tool" >/dev/null 2>&1 || die "missing dependency: $tool"
  done
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
  tailscale status >/dev/null 2>&1 || die "tailscale is not connected (run tailscale up)"
  if names=$(gh secret list --repo "$RUNNER_REPO" --json name --jq '.[].name' 2>/dev/null); then
    grep -qx 'TAILSCALE_AUTH_KEY' <<<"$names" \
      || die "missing secret TAILSCALE_AUTH_KEY on $RUNNER_REPO.
     set it with: gh secret set TAILSCALE_AUTH_KEY --repo $RUNNER_REPO"
  else
    warn "could not list secrets on $RUNNER_REPO; ensure TAILSCALE_AUTH_KEY is set"
  fi
}

# The runner joins the tailnet as ios-ci-<run_id>; prefer its IPv4 address and fall back
# to MagicDNS. An empty result means the node has not joined yet.
peer_ip() {
  tailscale status --json 2>/dev/null | jq -r --arg h "ios-ci-$1" '
    (.Peer // {}) | to_entries[] | .value
    | select(.HostName == $h)
    | ((.TailscaleIPs | map(select(test(":") | not)) | .[0])
       // (.DNSName // "" | sub("\\.$"; "")))' 2>/dev/null | head -1 || true
}

cmd_session() {
  local token hash correlation title baseline runs run_id stuck waited
  local ip="" deadline status conclusion last_note=0

  token=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
  hash=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$token")
  correlation="cli-$(date +%s)-$$"

  if [[ "$DRY_RUN" == true ]]; then
    note "runner      : $RUNNER_REPO"
    note "minutes     : $MINUTES"
    note "correlation : $correlation"
    note "gh workflow run $WORKFLOW --repo $RUNNER_REPO --ref main \\"
    note "  -f correlation_id=$correlation -f session_minutes=$MINUTES \\"
    note "  -f session_token_hash=$hash"
    note "tailnet host: ios-ci-<run_id>"
    note "desktop     : http://ios-ci-<run_id>:${PORT}/?t=<session token>"
    return 0
  fi

  need_tools
  baseline=$(gh run list --repo "$RUNNER_REPO" --workflow "$WORKFLOW" --limit 1 \
    --json databaseId 2>/dev/null | jq -r '.[0].databaseId // 0' 2>/dev/null || echo 0)
  [[ "$baseline" =~ ^[0-9]+$ ]] || baseline=0

  note "runner  : $RUNNER_REPO"
  note "minutes : $MINUTES"
  gh workflow run "$WORKFLOW" --repo "$RUNNER_REPO" --ref main \
    -f "correlation_id=$correlation" \
    -f "session_minutes=$MINUTES" \
    -f "session_token_hash=$hash" >/dev/null \
    || die "dispatch failed (does the runner accept workflow_dispatch on main?)"

  # Exact title match: correlations are unique per dispatch, but a substring match
  # would also hit a longer id that happens to start with this one.
  title="ios-ci $correlation"
  run_id=""
  waited=0
  while [[ "$waited" -lt 300 ]]; do
    runs=$(gh run list --repo "$RUNNER_REPO" --workflow "$WORKFLOW" --limit 100 \
      --json databaseId,displayTitle,conclusion 2>/dev/null || true)
    run_id=$(printf '%s' "$runs" | jq -r --arg t "$title" \
      'map(select(.displayTitle == $t)) | first | .databaseId // empty' 2>/dev/null || true)
    [[ -n "$run_id" ]] && break
    # A workflow-file error (bad expression, an action the repo's policy forbids)
    # creates a run that never gets its run-name and has no jobs, so the title match
    # above can never hit. Report it now instead of waiting out the five minutes.
    stuck=$(printf '%s' "$runs" | jq -r --argjson b "${baseline:-0}" \
      'map(select(.databaseId > $b and .conclusion == "startup_failure")) | first | .databaseId // empty' 2>/dev/null || true)
    if [[ -n "$stuck" ]]; then
      die "the run never started: GitHub rejected the workflow file.
     https://github.com/$RUNNER_REPO/actions/runs/$stuck"
    fi
    sleep 2
    waited=$((waited + 2))
  done
  [[ -n "$run_id" ]] || die "dispatched run '$title' did not appear within 5 minutes"
  note "run     : https://github.com/$RUNNER_REPO/actions/runs/$run_id"
  note "waiting for ios-ci-$run_id to join the tailnet…"

  deadline=$((SECONDS + 600))
  while (( SECONDS < deadline )); do
    status=$(gh api "repos/$RUNNER_REPO/actions/runs/$run_id" --jq '.status' 2>/dev/null || true)
    if [[ "$status" == "completed" ]]; then
      conclusion=$(gh api "repos/$RUNNER_REPO/actions/runs/$run_id" --jq '.conclusion' \
        2>/dev/null || true)
      printf 'error: the run finished (%s) before the desktop was ready: %s\n' \
        "${conclusion:-unknown}" "https://github.com/$RUNNER_REPO/actions/runs/$run_id" >&2
      return 1
    fi
    if [[ -z "$ip" ]]; then
      ip=$(peer_ip "$run_id")
    fi
    # Ready means the same thing the GUI's green light means: frames, dimensions, and
    # a self-test click that actually changed the screen.
    if [[ -n "$ip" ]] && curl -fsS -m 3 "http://${ip}:${PORT}/stats?t=${token}" 2>/dev/null \
        | jq -e '.alive == true and .input_ok == true and .frames > 0 and .width > 0 and .height > 0' \
          >/dev/null 2>&1; then
      note "desktop ready after ${SECONDS}s: http://${ip}:${PORT}/?t=${token}"
      note "the workflow holds it for ${MINUTES} minutes from ready; stop early with:"
      note "  gh run cancel $run_id --repo $RUNNER_REPO"
      return 0
    fi
    if (( SECONDS - last_note >= 10 )); then
      if [[ -z "$ip" ]]; then
        note "waiting for the tailnet… ${SECONDS}s"
      else
        note "desktop server up; waiting for frames/input… ${SECONDS}s"
      fi
      last_note=$SECONDS
    fi
    sleep 2
  done

  printf 'error: the desktop was not ready within 600s; the run is still going: %s\n' \
    "https://github.com/$RUNNER_REPO/actions/runs/$run_id" >&2
  return 1
}

command=""
if [[ $# -gt 0 ]]; then
  case "$1" in
    session) command="session"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown command: $1 (try --help)" ;;
  esac
fi
if [[ -z "$command" ]]; then
  usage >&2
  exit 2
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner) RUNNER_REPO="${2:?--runner needs OWNER/NAME}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    -[0-9]*) MINUTES="$1"; shift ;;      # fails the range check below, as a number
    -*) die "unknown option: $1" ;;
    *) MINUTES="$1"; shift ;;
  esac
done
MINUTES="${MINUTES:-$DEFAULT_MINUTES}"
[[ "$MINUTES" =~ ^[0-9]+$ ]] || die "minutes must be a whole number, got '$MINUTES'"
# 10# avoids bash reading a leading zero as octal (`session 08`).
MINUTES=$((10#$MINUTES))
(( MINUTES >= 1 && MINUTES <= 300 )) || die "minutes must be 1-300, got $MINUTES"

cmd_session
