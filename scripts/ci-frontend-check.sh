#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../ksadk/server/web-ui"

# Build both targets
npm ci
npm run build:all

# Verify both outputs exist
if [ ! -f dist/index.html ]; then
  echo "FAIL: dist/index.html missing"
  exit 1
fi
if [ ! -f dist-hosted/index.html ]; then
  echo "FAIL: dist-hosted/index.html missing"
  exit 1
fi

# Check timestamps are close (within 60s)
if command -v stat &>/dev/null; then
  if [[ "$OSTYPE" == "darwin"* ]]; then
    dist_time=$(stat -f %m dist/index.html)
    hosted_time=$(stat -f %m dist-hosted/index.html)
  else
    dist_time=$(stat -c %Y dist/index.html)
    hosted_time=$(stat -c %Y dist-hosted/index.html)
  fi
  diff=$((hosted_time - dist_time))
  if [ "$diff" -lt 0 ]; then diff=$((-diff)); fi
  if [ "$diff" -gt 60 ]; then
    echo "FAIL: dist and dist-hosted timestamps differ by ${diff}s (>60s)"
    exit 1
  fi
fi

echo "PASS: Frontend builds and sync check OK"