#!/usr/bin/env bash
# Fail if any embedded version literal has drifted from ./VERSION.
# Manifests can't reference $(VERSION), so `make set-version` propagates it into each
# file; this guard (run by `make check`) ensures they never silently diverge.
set -euo pipefail

version="$(cat VERSION)"
fail=0

require() {
  local file="$1" pattern="$2"
  if ! grep -qF -- "$pattern" "$file"; then
    echo "version drift: $file is missing '$pattern' (VERSION=$version)"
    fail=1
  fi
}

require pyproject.toml "version = \"$version\""
require app/main.py "version=\"$version\""
require compat/pyproject.toml "version = \"$version\""
require compat/app/main.py "version=\"$version\""
require deploy/helm/taas/Chart.yaml "version: $version"
require deploy/helm/taas/Chart.yaml "appVersion: \"$version\""

if [ "$fail" -ne 0 ]; then
  echo "-> run: make set-version VERSION=$version"
  exit 1
fi
echo "version OK: all literals match $version"
