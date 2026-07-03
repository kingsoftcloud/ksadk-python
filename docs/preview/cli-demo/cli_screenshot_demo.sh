#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://127.0.0.1:8765"
OUT_DIR="/Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python/docs/preview/cli-demo"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

"$CHROME" --headless=new --disable-gpu --virtual-time-budget=7000 --window-size=1600,2600 \
  --screenshot="$OUT_DIR/openclaw_client_one_click_deploy_cli.png" \
  "$BASE_URL/openclaw_client_one_click_deploy.html"

"$CHROME" --headless=new --disable-gpu --virtual-time-budget=9000 --window-size=1600,3000 \
  --screenshot="$OUT_DIR/openclaw_gateway_technical_cli.png" \
  "$BASE_URL/openclaw_gateway_technical.html"
