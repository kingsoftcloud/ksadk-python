#!/usr/bin/env bash
# Setup script for 金山文档 MCP Skill（与 OpenClaw 插件配套）

set -euo pipefail

echo "🚀 设置金山文档 MCP Skill（OpenClaw 版本）..."
echo ""

if [[ -z "${KDOCS_TOKEN:-}" ]]; then
    echo "⚠️  未检测到 KDOCS_TOKEN，无法注册 kdocs MCP 服务"
    echo "   请先访问 https://365.kdocs.cn/latest 获取 Token，并导出环境变量后重试"
    exit 1
fi

# 检查 mcporter
if ! command -v mcporter &> /dev/null; then
    echo "⚠️  未找到 mcporter，正在安装..."
    npm install -g mcporter
    echo "✅ mcporter 安装完成"
fi

# 从环境变量中读取用户填写的 Token
mcporter config add kdocs "https://mcp-center.wps.cn/skill_hub/mcp" \
    --header "Authorization=Bearer $KDOCS_TOKEN" \
    --transport http \
    --scope home

echo ""
echo "✅ 配置完成！"
echo ""
echo "ℹ️  KDOCS_TOKEN 环境变量由 OpenClaw runtime 自动提供"
echo ""

# 验证配置
echo "🧪 验证配置..."
if mcporter list kdocs >/dev/null 2>&1; then
    echo "✅ 配置验证成功！"
    echo ""
    mcporter list kdocs || true
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
