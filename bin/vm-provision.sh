#!/usr/bin/env bash
# Provision the ios-ci VM: Xcode, an iOS runtime, a booted iPhone simulator, Node.js,
# Yarn 4 and Git. Idempotent; every assertion fails loudly. The app reads the facts it
# leaves in /tmp/ios-ci-env.json through the desktop server's /stats.
#
# Usage: bin/vm-provision.sh [logfile]
set -euo pipefail

LOG=${1:-/tmp/ios-ci-provision.log}
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

log() { printf '[provision] %s\n' "$*"; }
fail() { printf '[provision] FAILED: %s\n' "$*" >&2; exit 1; }

# The console user owns the desktop the session is streamed from. The step itself runs
# as the runner user, so privileged work goes through `sudo -n` and never prompts.
CONSOLE_USER=$(stat -f%Su /dev/console 2>/dev/null || true)
if [[ -z "$CONSOLE_USER" || "$CONSOLE_USER" == "root" ]]; then
  CONSOLE_USER=$(whoami)
fi
CONSOLE_UID=$(id -u "$CONSOLE_USER" 2>/dev/null || echo 501)
log "console user: ${CONSOLE_USER} (${CONSOLE_UID})"

# 1. Xcode selection ---------------------------------------------------------
SELECTED=$(xcode-select -p 2>/dev/null || true)
if [[ "$SELECTED" != /Applications/Xcode*.app/Contents/Developer* ]]; then
  if [[ -d /Applications/Xcode.app ]]; then
    log "selecting /Applications/Xcode.app"
    sudo -n xcode-select -s /Applications/Xcode.app/Contents/Developer \
      || fail "could not select /Applications/Xcode.app"
  else
    NEWEST=$(ls -d /Applications/Xcode*.app 2>/dev/null | sort -V | tail -1 || true)
    [[ -n "$NEWEST" ]] || fail "no Xcode installation found in /Applications"
    log "selecting $NEWEST"
    sudo -n xcode-select -s "$NEWEST/Contents/Developer" \
      || fail "could not select $NEWEST"
  fi
fi

sudo -n xcodebuild -license accept >/dev/null 2>&1 || true
sudo -n xcodebuild -runFirstLaunch >/dev/null 2>&1 || true

if ! xcodebuild -version >/dev/null 2>&1; then
  fail "xcodebuild -version failed after selecting $(xcode-select -p)"
fi
XCODE_VERSION=$(xcodebuild -version | awk 'NR==1 {print $2}')
XCODE_PATH=$(xcode-select -p)
[[ -n "$XCODE_VERSION" ]] || fail "could not read the Xcode version"
log "Xcode ${XCODE_VERSION} at ${XCODE_PATH}"

# 2. iOS runtime -------------------------------------------------------------
has_ios_runtime() {
  xcrun simctl list runtimes -j 2>/dev/null | jq -e '
    [.runtimes[] | select(.isAvailable and
      (.identifier | startswith("com.apple.CoreSimulator.SimRuntime.iOS-")))]
    | length > 0' >/dev/null 2>&1
}

if ! has_ios_runtime; then
  log "no available iOS runtime; downloading the iOS platform (this can take minutes)"
  sudo -n xcodebuild -downloadPlatform iOS || fail "could not download the iOS platform"
  has_ios_runtime || fail "no available iOS runtime after xcodebuild -downloadPlatform iOS"
fi

RUNTIME=$(xcrun simctl list runtimes -j | jq -r '
  [.runtimes[] | select(.isAvailable and
    (.identifier | startswith("com.apple.CoreSimulator.SimRuntime.iOS-")))]
  | sort_by(.version | split(".") | map(tonumber)) | last | .identifier')
[[ -n "$RUNTIME" && "$RUNTIME" != "null" ]] || fail "could not select an iOS runtime"
RUNTIME_VERSION=$(xcrun simctl list runtimes -j | jq -r --arg r "$RUNTIME" '
  .runtimes[] | select(.identifier == $r) | .version')
log "iOS runtime: ${RUNTIME_VERSION} (${RUNTIME})"

# 3. Simulator ---------------------------------------------------------------
UDID=$(xcrun simctl list devices available -j | jq -r --arg r "$RUNTIME" '
  .devices[$r] // []
  | (map(select(.name == "iPhone 17 Pro")) + map(select(.name | startswith("iPhone"))))
  | (map(select(.udid)) | .[0].udid) // empty')
[[ -n "$UDID" ]] || fail "no iPhone simulator is available in ${RUNTIME}"
SIMULATOR_NAME=$(xcrun simctl list devices -j | jq -r --arg u "$UDID" '
  .devices[][] | select(.udid == $u) | .name' | head -1)
log "booting simulator ${SIMULATOR_NAME} (${UDID})"

# "boot" fails when the device is already booted; that is the idempotent case.
xcrun simctl boot "$UDID" >/dev/null 2>&1 || true
xcrun simctl bootstatus "$UDID" -b || fail "simulator ${UDID} did not finish booting"
STATE=$(xcrun simctl list devices -j | jq -r --arg u "$UDID" '
  .devices[][] | select(.udid == $u) | .state' | head -1)
[[ "$STATE" == "Booted" ]] || fail "simulator ${UDID} is ${STATE:-unknown}, not Booted"
log "simulator state: ${STATE}"

# Best effort: put the Simulator window on the streamed desktop.
SIMULATOR_APP="$(xcode-select -p)/Applications/Simulator.app"
sudo -n launchctl asuser "$CONSOLE_UID" sudo -u "$CONSOLE_USER" \
  open -a "$SIMULATOR_APP" >/dev/null 2>&1 \
  || log "warning: could not open Simulator.app for ${CONSOLE_USER}"

# 4. Node --------------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || fail "node is not installed and Homebrew is unavailable"
  log "installing Node.js with Homebrew"
  brew install node || fail "brew install node failed"
fi
NODE_BIN=$(command -v node)
NODE_VERSION=$(node --version)
NODE_MAJOR=${NODE_VERSION#v}
NODE_MAJOR=${NODE_MAJOR%%.*}
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || fail "could not read the Node.js major version from ${NODE_VERSION}"
(( NODE_MAJOR >= 20 )) || fail "Node.js ${NODE_VERSION} is too old (need >= 20)"
NODE_DIR=$(dirname "$NODE_BIN")
# GUI terminals do not inherit this step's PATH.
printf '%s\n' "$NODE_DIR" | sudo -n tee /etc/paths.d/ios-ci-node >/dev/null \
  || log "warning: could not write /etc/paths.d/ios-ci-node"
log "Node.js ${NODE_VERSION} at ${NODE_BIN}"

# 5. Yarn 4 ------------------------------------------------------------------
if ! command -v corepack >/dev/null 2>&1; then
  log "installing corepack"
  npm install -g corepack@0.36.0 >/dev/null 2>&1 \
    || sudo -n npm install -g corepack@0.36.0 >/dev/null 2>&1 \
    || fail "could not install corepack"
fi
if ! corepack enable >/dev/null 2>&1; then
  sudo -n corepack enable --install-directory /usr/local/bin >/dev/null 2>&1 \
    || log "warning: corepack could not install its shims"
fi
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack prepare yarn@4.18.1 --activate >/dev/null 2>&1 \
  || log "warning: corepack prepare yarn@4.18.1 failed; falling back to npm"
hash -r
YARN_VERSION=$(yarn --version 2>/dev/null || true)
if [[ "$YARN_VERSION" != 4.* ]]; then
  log "installing Yarn 4 with npm"
  npm install -g @yarnpkg/cli-dist@4.18.1 >/dev/null 2>&1 \
    || sudo -n npm install -g @yarnpkg/cli-dist@4.18.1 >/dev/null 2>&1 \
    || fail "could not install Yarn 4"
  hash -r
  YARN_VERSION=$(yarn --version 2>/dev/null || true)
fi
[[ "$YARN_VERSION" == 4.* ]] || fail "yarn --version returned '${YARN_VERSION:-nothing}' (need 4.x)"
log "Yarn ${YARN_VERSION}"

# 6. Git ---------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || fail "git is not installed and Homebrew is unavailable"
  log "installing Git with Homebrew"
  brew install git || fail "brew install git failed"
fi
GIT_VERSION=$(git --version 2>/dev/null | awk '{print $3}')
[[ -n "$GIT_VERSION" ]] || fail "git --version failed"
log "Git ${GIT_VERSION}"

# 7. Publish facts -----------------------------------------------------------
ENV_FILE=/tmp/ios-ci-env.json
TMP_ENV=$(mktemp /tmp/ios-ci-env.XXXXXX)
jq -n \
  --arg xcode "$XCODE_VERSION" \
  --arg xcode_path "$XCODE_PATH" \
  --arg ios_runtime "$RUNTIME_VERSION" \
  --arg simulator "$SIMULATOR_NAME" \
  --arg simulator_udid "$UDID" \
  --arg node "$NODE_VERSION" \
  --arg yarn "$YARN_VERSION" \
  --arg git "$GIT_VERSION" \
  '{
    xcode: $xcode,
    xcode_path: $xcode_path,
    ios_runtime: $ios_runtime,
    simulator: $simulator,
    simulator_udid: $simulator_udid,
    simulator_booted: true,
    node: $node,
    yarn: $yarn,
    git: $git
  }' > "$TMP_ENV"
chmod 0644 "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"

log "xcode=${XCODE_VERSION}"
log "xcode_path=${XCODE_PATH}"
log "ios_runtime=${RUNTIME_VERSION}"
log "simulator=${SIMULATOR_NAME}"
log "simulator_udid=${UDID}"
log "simulator_booted=true"
log "node=${NODE_VERSION}"
log "yarn=${YARN_VERSION}"
log "git=${GIT_VERSION}"
log "wrote ${ENV_FILE}"
exit 0
