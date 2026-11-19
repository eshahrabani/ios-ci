#!/usr/bin/env bash
# Launch the ios-ci desktop app from this checkout.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

if [[ ! -x node_modules/.bin/electron ]]; then
  npm install
fi

exec npm start -- "$@"
