#!/usr/bin/env bash
# Runs an arbitrary script headlessly in one version of Blender via podman.
# Usage: run_blender_script.sh <version> <script-path-relative-to-repo> [-- <args> ...]
#   e.g. run_blender_script.sh 4.1 tests/tools/generate_simpleskel_nla_blend.py
#
# Repo root is derived from this script's own location, not the caller's cwd, so the
# invocation never needs a $(pwd)-style substitution at the call site -- that's what made
# the equivalent ad-hoc commands unsafe to whitelist as a fixed pattern.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $(basename "$0") <version> <script-path-relative-to-repo> [-- <args> ...]" >&2
  echo "  e.g. $(basename "$0") 4.1 tests/tools/generate_simpleskel_nla_blend.py" >&2
  exit 2
fi

VERSION="$1"
SCRIPT_PATH="$2"
shift 2

# an optional leading "--" separating our own args from the target script's is conventional
# but not required -- drop it if present so both forms work.
if [ "$#" -gt 0 ] && [ "$1" = "--" ]; then
  shift
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

podman run --rm \
  --entrypoint /home/headless/blender/blender \
  -v "$REPO_ROOT:/repo:Z" \
  "docker.io/blenderkit/headless-blender:blender-${VERSION}-stable" \
  --background --python-exit-code 1 --python "/repo/$SCRIPT_PATH" -- "$@"
