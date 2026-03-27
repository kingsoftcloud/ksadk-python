#!/bin/bash
#
# WPS 授权工具 - 获取 skill_hub token
#
# 流程：
#   1. 生成 code，构造登录链接（callback 指向 api.wps.cn）
#   2. 用户在浏览器打开链接登录
#   3. WPS 登录成功后回调服务端，将 wps_sid 转为 skill_hub token
#   4. 本脚本轮询 exchange 接口获取 token
#   5. 将 token 仅写入 mcporter，不再写入 .env 或环境变量
#
# 用法：bash get-token.sh [--json]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_FILE="$SCRIPT_DIR/SKILL.md"
LEGACY_ENV_FILE="$SCRIPT_DIR/.env"
MCP_URL="https://mcp-center.wps.cn/skill_hub/mcp"
OUTPUT_JSON=0
# 重发通知间隔（秒）
NOTIFY_INTERVAL=30

for arg in "$@"; do
  if [ "$arg" = "--json" ]; then
    OUTPUT_JSON=1
  fi
done

# ============================================
# 环境检测函数
# ============================================

detect_environment() {
  # 返回: openclaw | desktop | headless
  if command -v openclaw >/dev/null 2>&1; then
    echo "openclaw"
    return
  fi
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    echo "desktop"
    return
  fi
  echo "headless"
}

has_gui_display() {
  # 检查是否有可用的 GUI 显示
  [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]
}

# ============================================
# 工具函数
# ============================================

generate_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr 'A-Z' 'a-z'
  elif [ -f /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import uuid; print(uuid.uuid4())"
  elif command -v python >/dev/null 2>&1; then
    python -c "import uuid; print(uuid.uuid4())"
  else
    echo "$(date +%s%N)-$$-$RANDOM" | md5sum | cut -c1-32 |
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

extract_json_value() {
  local json="$1"
  local key="$2"
  if command -v jq >/dev/null 2>&1; then
    local value
    value=$(jq -r ".data.$key // .$key // empty" <<<"$json" 2>/dev/null || true)
    if [ -n "$value" ] && [ "$value" != "null" ]; then
      echo "$value"
      return
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    JSON_INPUT="$json" JSON_KEY="$key" python3 - <<'PY'
import json
import os

try:
    data = json.loads(os.environ["JSON_INPUT"])
    value = data.get("data", {}).get(os.environ["JSON_KEY"])
    if value in (None, ""):
        value = data.get(os.environ["JSON_KEY"], "")
    if value not in (None, ""):
        print(value)
except Exception:
    pass
PY
    return
  fi
  if command -v python >/dev/null 2>&1; then
    JSON_INPUT="$json" JSON_KEY="$key" python - <<'PY'
import json
import os

try:
    data = json.loads(os.environ["JSON_INPUT"])
    value = data.get("data", {}).get(os.environ["JSON_KEY"])
    if value in (None, ""):
        value = data.get(os.environ["JSON_KEY"], "")
    if value not in (None, ""):
        print(value)
except Exception:
    pass
PY
    return
  fi
  sed -n "s/.*\"${key}\":[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" <<<"$json" | head -n 1
}

get_skill_version() {
  local version=""
  if [ -f "$SKILL_FILE" ]; then
    version=$(sed -n 's/^version:[[:space:]]*//p' "$SKILL_FILE" | head -n 1)
  fi
  if [ -z "$version" ]; then
    echo "unknown"
  else
    echo "$version"
  fi
}

ensure_mcporter() {
  if command -v mcporter >/dev/null 2>&1; then
    return
  fi
  if command -v npm >/dev/null 2>&1; then
    echo "⚠️  未找到 mcporter，正在安装..."
    npm install -g mcporter >/dev/null
    echo "✅ mcporter 安装完成"
  fi
  if ! command -v mcporter >/dev/null 2>&1; then
    echo "❌ 未找到 mcporter，请先安装后重试"
    exit 1
  fi
}

set_mcporter_config() {
  local token="$1"
  local version="$2"
  local args=(
    config add kdocs "$MCP_URL"
    --header "X-Skill-Version=$version"
    --transport http
    --scope home
  )

  if [ -n "$token" ]; then
    args+=(--header "Authorization=Bearer $token")
  fi

  mcporter config remove kdocs >/dev/null 2>&1 || true
  mcporter "${args[@]}" >/dev/null
}

cleanup_legacy_env_file() {
  if [ -f "$LEGACY_ENV_FILE" ]; then
    rm -f "$LEGACY_ENV_FILE"
  fi
}

# ============================================
# 格式化时间
# ============================================

format_time() {
  local seconds="$1"
  local minutes=$((seconds / 60))
  local secs=$((seconds % 60))
  if [ "$minutes" -gt 0 ]; then
    printf "%d分%d秒" "$minutes" "$secs"
  else
    printf "%d秒" "$secs"
  fi
}

# ============================================
# 通知函数（多环境适配）
# ============================================

notify_link() {
  local url="$1"
  local env_type="$2"
  local remaining="$3"
  local remaining_str
  remaining_str=$(format_time "$remaining")

  case "$env_type" in
    openclaw)
      # OpenClaw 环境：输出特殊标记，让 AI 识别并发送可点击消息
      echo ""
      echo "==================== 授权提醒 ===================="
      echo "授权链接: ${url}"
      echo "剩余时间: ${remaining_str}"
      echo "=================================================="
      echo ""
      ;;
    desktop)
      # 桌面环境：尝试系统通知
      if command -v notify-send >/dev/null 2>&1; then
        notify-send "金山文档授权" "请在浏览器中完成登录授权（剩余 ${remaining_str}）" 2>/dev/null || true
      elif command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"请在浏览器中完成登录授权（剩余 ${remaining_str}）\" with title \"金山文档授权\"" 2>/dev/null || true
      fi
      ;;
    headless)
      # 无界面环境：仅终端输出
      ;;
  esac
}

notify_success() {
  local expires_hours="$1"
  local env_type="$2"

  case "$env_type" in
    openclaw)
      # 输出成功标记，让 AI 识别并发送通知
      echo ""
      echo "==================== 授权成功 ===================="
      echo "Token 有效期: 约 ${expires_hours} 小时"
      echo "=================================================="
      echo ""
      ;;
    desktop)
      if command -v notify-send >/dev/null 2>&1; then
        notify-send "金山文档授权成功" "Token 有效期约 ${expires_hours} 小时" 2>/dev/null || true
      elif command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"Token 有效期约 ${expires_hours} 小时\" with title \"金山文档授权成功\"" 2>/dev/null || true
      fi
      ;;
    headless)
      ;;
  esac
}

# ============================================
# 浏览器打开函数（多环境适配）
# ============================================

open_browser() {
  local url="$1"
  local env_type="$2"

  # OpenClaw 环境：不尝试自动打开，已通过消息通知
  if [ "$env_type" = "openclaw" ]; then
    return 1
  fi

  # 桌面环境：尝试打开浏览器
  if [ "$env_type" = "desktop" ] && has_gui_display; then
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$url" >/dev/null 2>&1 && return 0
    fi
    if command -v open >/dev/null 2>&1; then
      open "$url" >/dev/null 2>&1 && return 0
    fi
  fi

  return 1
}

# ============================================
# 主流程
# ============================================

ENV_TYPE=$(detect_environment)
CODE=$(generate_uuid)
CB="https://api.wps.cn/office/v5/ai/skill_hub/users/callback?code=${CODE}"
ENCODED_CB=$(urlencode "$CB")
LOGIN_URL="https://account.wps.cn/login?cb=${ENCODED_CB}"
SKILL_VERSION="$(get_skill_version)"

echo ""
echo "======================================================================"
echo "  WPS 授权 - 获取 skill_hub token"
echo "======================================================================"
echo ""
echo "🔍 检测到环境: ${ENV_TYPE}"
echo ""
echo "📱 请在浏览器中打开以下链接登录："
echo ""
echo "   ${LOGIN_URL}"
echo ""
echo "🔑 auth_code: ${CODE}"
echo ""
echo "======================================================================"
echo ""

# 定义超时时间（需要在 notify_link 之前）
TIMEOUT=300

# 发送授权链接通知（带倒计时）
notify_link "$LOGIN_URL" "$ENV_TYPE" $TIMEOUT

# 尝试打开浏览器
if open_browser "$LOGIN_URL" "$ENV_TYPE"; then
  echo "🌐 已自动打开浏览器，请完成 WPS 登录授权"
else
  echo "⚠️  请点击上方链接或手动复制到浏览器访问"
fi
echo ""

# ============================================
# 轮询等待（带进度反馈和定时重发）
# ============================================

INTERVAL=1
START=$(date +%s)
LAST_NOTIFY=$START
NOTIFY_COUNT=1

echo "⏳ 等待登录... (最长 5 分钟，每 ${NOTIFY_INTERVAL} 秒提醒一次)"
echo ""

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  REMAINING=$((TIMEOUT - ELAPSED))

  # 超时检查
  if [ "$REMAINING" -le 0 ]; then
    echo ""
    echo "❌ 超时未登录（${TIMEOUT}秒）"
    exit 1
  fi

  # 定时重发通知（每 NOTIFY_INTERVAL 秒）
  TIME_SINCE_NOTIFY=$((NOW - LAST_NOTIFY))
  if [ "$TIME_SINCE_NOTIFY" -ge "$NOTIFY_INTERVAL" ] && [ "$ENV_TYPE" = "openclaw" ]; then
    NOTIFY_COUNT=$((NOTIFY_COUNT + 1))
    echo ""
    echo "📤 重发授权提醒 #${NOTIFY_COUNT}..."
    notify_link "$LOGIN_URL" "$ENV_TYPE" "$REMAINING"
    LAST_NOTIFY=$NOW
  fi

  # 每 10 秒显示一次进度
  if [ $((ELAPSED % 10)) -eq 0 ] && [ "$ELAPSED" -gt 0 ]; then
    REMAINING_STR=$(format_time "$REMAINING")
    echo ""
    echo "⏳ 已等待 $(format_time "$ELAPSED")，剩余 ${REMAINING_STR}..."
  fi

  # 调用 exchange 接口
  RESPONSE=$(curl -s -X POST \
    "https://api.wps.cn/office/v5/ai/skill_hub/wps_auth/exchange" \
    -H "Content-Type: application/json" \
    -d "{\"code\": \"${CODE}\"}" 2>/dev/null || echo '{"code": -1}')

  RESP_CODE=$(extract_json_value "$RESPONSE" "code")
  TOKEN=$(extract_json_value "$RESPONSE" "token")
  EXPIRES=$(extract_json_value "$RESPONSE" "expires_in")

  if [ "$RESP_CODE" = "200" ] && [ -n "$TOKEN" ]; then
    echo ""
    echo ""
    echo "✅ 登录成功！"
    echo ""

    ensure_mcporter
    set_mcporter_config "$TOKEN" "$SKILL_VERSION"
    cleanup_legacy_env_file

    TOKEN_SHORT="${TOKEN:0:8}..."
    EXPIRES_HOURS=$((EXPIRES / 3600))

    echo "📝 Token 已写入 mcporter"
    echo "⏰ 有效期: 约 ${EXPIRES_HOURS} 小时"

    # 发送成功通知
    notify_success "$EXPIRES_HOURS" "$ENV_TYPE"

    if [ "$OUTPUT_JSON" -eq 1 ]; then
      echo "{\"token\":\"${TOKEN}\",\"expires_in\":${EXPIRES}}"
    else
      echo "🔒 Token 摘要: ${TOKEN_SHORT}"
    fi
    exit 0
  elif [ "$RESP_CODE" = "202" ]; then
    # 等待用户登录中，打印进度点
    printf "."
  else
    # 其他错误
    if [ "$RESP_CODE" != "-1" ]; then
      echo ""
      echo "⚠️  响应异常: code=${RESP_CODE}"
    fi
  fi

  sleep "$INTERVAL"
done
