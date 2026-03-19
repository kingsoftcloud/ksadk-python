#!/bin/bash
# Setup script for 金山文档 Skill（与 OpenClaw 插件配套）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 设置金山文档 Skill（OpenClaw 版本）..."
echo ""

# 检查 mcporter
if ! command -v mcporter &> /dev/null; then
    echo "⚠️  未找到 mcporter，正在安装..."
    npm install -g mcporter
    echo "✅ mcporter 安装完成"
fi

# 获取 Token：优先使用环境变量，否则调用 get-token.sh
if [ -n "$KINGSOFT_DOCS_TOKEN" ]; then
    echo "✅ 使用环境变量中的 KINGSOFT_DOCS_TOKEN"
    TOKEN="$KINGSOFT_DOCS_TOKEN"
else
    echo "⚠️  未检测到 KINGSOFT_DOCS_TOKEN，正在通过 get-token.sh 获取..."
    echo ""
    TOKEN_OUTPUT=$(bash "$SCRIPT_DIR/get-token.sh" --json)
    TOKEN=$(echo "$TOKEN_OUTPUT" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
    if [ -z "$TOKEN" ]; then
        echo "❌ 获取 Token 失败，请手动运行：bash get-token.sh"
        exit 1
    fi
    echo "✅ Token 获取成功"
fi

# 从环境变量中读取用户填写的 Token
mcporter config add kdocs "https://mcp-center.wps.cn/skill_hub/mcp" \
    --header "Authorization=Bearer $KINGSOFT_DOCS_TOKEN" \
    --transport http \
    --scope home

echo ""
echo "✅ 配置完成！"
echo ""

# 验证配置
echo "🧪 验证配置..."
if mcporter list 2>&1 | grep -q "kdocs"; then
    echo "✅ 配置验证成功！"
    echo ""
    mcporter list | grep -A 1 "kdocs" || true
else
    echo "⚠️  配置验证失败，请检查网络或 Token 是否有效"
    echo ""
fi


echo "─────────────────────────────────────"
echo "🎉 设置完成！"
echo ""
echo "📖 使用方法："
echo "   mcporter call kdocs.scrape_url"
echo ""
echo "🏠 金山文档主页：https://365.kdocs.cn/latest"
echo ""
echo "📖 更多信息请查看 SKILL.md"
