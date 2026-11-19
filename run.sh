#!/usr/bin/env bash
# Launch the ios-ci desktop GUI from this checkout.
#
# bin/ios-ci-gui picks a Python, creates ~/.cache/ios-ci-gui/venv on first run and
# installs PySide6 there; this wrapper just reaches it from the repository root.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$HERE/bin/ios-ci-gui" "$@"
