#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

make sync-ksadk-web-static

if [ ! -f ksadk/server/static/index.html ]; then
  echo "FAIL: ksadk/server/static/index.html missing"
  exit 1
fi

if ! ls ksadk/server/static/assets/*.js >/dev/null 2>&1; then
  echo "FAIL: synced static bundle is missing JS assets"
  exit 1
fi

if ! ls ksadk/server/static/assets/*.css >/dev/null 2>&1; then
  echo "FAIL: synced static bundle is missing CSS assets"
  exit 1
fi

echo "PASS: KsADK Web static sync check OK"
