#!/bin/bash
#
# WPS 授权工具 - 获取 skill_hub token (优化版)
#
# 流程：
#   1. 生成 code，构造登录链接（callback 指向 api.wps.cn）
#   2. 用户在浏览器打开链接登录
#   3. WPS 登录成功后回调服务端，将 wps_sid 转为 skill_hub token
#   4. 本脚本轮询 exchange 接口获取 token
#
# 用法：bash get-token.sh [--json] [--notify]
# 优化点：
#   - 轮询间隔从 3 秒缩短到 1 秒
#   - 使用 jq 解析 JSON（如果可用），否则回退到 grep
#   - 获取 token 后可选发送通知

generate_uuid() {
  if command -v uuidgen &>/dev/null; then
    uuidgen | tr 'A-Z' 'a-z'
  elif [ -f /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid
  elif command -v python3 &>/dev/null; then
    python3 -c "import uuid; print(uuid.uuid4())"
  elif command -v python &>/dev/null; then
    python -c "import uuid; print(uuid.uuid4())"
  else
    echo "$(date +%s%N)-$$-$RANDOM" | md5sum | sed 's/\(..\)/\1/g' | cut -c1-32 |
      sed 's/\(........\)\(....\)\(....\)\(....\)\(............\)/\1-\2-4\3-\4-\5/' | cut -c1-36
  fi
}

urlencode() {
  local string="$1"
  python3 -c "import urllib.parse; print(urllib.parse.quote('$string', safe=''))" 2>/dev/null ||
  python -c "import urllib.parse; print(urllib.parse.quote('$string', safe=''))" 2>/dev/null ||
  echo "$string" | sed \
    -e 's/%/%25/g' \
    -e 's/ /%20/g' \
    -e 's/:/%3A/g' \
    -e 's/\//%2F/g' \
    -e 's/?/%3F/g' \
    -e 's/=/%3D/g' \
    -e 's/&/%26/g' \
    -e 's/#/%23/g'
}

# 优先使用 jq 解析 JSON，否则回退到 grep
extract_json_value() {
  local json="$1" key="$2"
  if command -v jq &>/dev/null; then
    # 尝试从 .data 中提取（WPS API 返回格式）
    local value=$(echo "$json" | jq -r ".data.$key // .$key // empty" 2>/dev/null)
    if [ -n "$value" ] && [ "$value" != "null" ]; then
      echo "$value"
    else
      # jq 失败时回退到 grep
      echo "$json" | grep -o "\"${key}\":[^,}]*" | head -1 | sed "s/\"${key}\"://; s/\"//g; s/ //g"
    fi
  else
    echo "$json" | grep -o "\"${key}\":[^,}]*" | head -1 | sed "s/\"${key}\"://; s/\"//g; s/ //g"
  fi
}

# 发送通知（如果启用）
send_notify() {
  local message="$1"
  if echo "$@" | grep -q "\-\-notify"; then
    # 尝试通过 openclaw 发送消息
    if command -v openclaw &>/dev/null; then
      openclaw agent --message "🔑 WPS Token 获取成功！$message" 2>/dev/null || true
    fi
    # 尝试发送系统通知
    if command -v osascript &>/dev/null; then
      osascript -e "display notification \"$message\" with title \"WPS Token 已获取\"" 2>/dev/null || true
    fi
  fi
}

CODE=$(generate_uuid)
CB="https://api.wps.cn/office/v5/ai/skill_hub/users/callback?code=${CODE}"
ENCODED_CB=$(urlencode "$CB")
LOGIN_URL="https://account.wps.cn/login?cb=${ENCODED_CB}"

echo ""
echo "======================================================================"
echo "  WPS 授权 - 获取 skill_hub token (优化版)"
echo "======================================================================"
echo ""
echo "📱 请在浏览器中打开以下链接登录："
echo ""
echo "   ${LOGIN_URL}"
echo ""
echo "🔑 auth_code: ${CODE}"
echo ""
echo "======================================================================"
echo ""
echo "⏳ 等待登录... (轮询间隔 1 秒，最长 5 分钟)"

TIMEOUT=300
INTERVAL=1  # 优化：从 3 秒缩短到 1 秒
START=$(date +%s)
LAST_DOT=0

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))

  if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
    echo ""
    echo ""
    echo "❌ 超时未登录（${TIMEOUT}秒）"
    exit 1
  fi

  RESPONSE=$(curl -s -X POST \
    "https://api.wps.cn/office/v5/ai/skill_hub/wps_auth/exchange" \
    -H "Content-Type: application/json" \
    -d "{\"code\": \"${CODE}\"}")

  RESP_CODE=$(extract_json_value "$RESPONSE" "code")
  TOKEN=$(extract_json_value "$RESPONSE" "token")
  EXPIRES=$(extract_json_value "$RESPONSE" "expires_in")

  if [ "$RESP_CODE" = "200" ] && [ -n "$TOKEN" ]; then
    echo ""
    echo ""
    echo "✅ 登录成功！"
    echo ""
    echo "======================================================================"
    echo "  授权信息"
    echo "======================================================================"
    echo ""
    echo "🔑 skill_hub token:"
    echo "${TOKEN}"
    echo ""
    echo "⏰ expires_in: ${EXPIRES}s (约 $((EXPIRES / 3600)) 小时)"
    echo ""
    echo "======================================================================"
    echo ""

    # 截断显示 token（前 20 位 + ...）
    TOKEN_SHORT="${TOKEN:0:20}..."
    send_notify "Token: ${TOKEN_SHORT}, 有效期：$((EXPIRES / 3600))小时"

    if echo "$@" | grep -q "\-\-json"; then
      echo "{\"token\":\"${TOKEN}\",\"expires_in\":${EXPIRES}}"
    fi
    exit 0

  elif [ "$RESP_CODE" = "202" ]; then
    # 每秒显示一个进度点，但每 5 秒显示一次耗时
    if [ $((ELAPSED % 5)) -eq 0 ] && [ "$ELAPSED" -ne "$LAST_DOT" ]; then
      printf "."
      LAST_DOT=$ELAPSED
    fi
  else
    echo ""
    echo "[DEBUG] body=${RESPONSE}"
  fi

  sleep $INTERVAL
done
