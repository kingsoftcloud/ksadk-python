#!/bin/bash
#
# 金山文档 MCP Skill 配置脚本
# 自动完成：Token 获取 → MCP 注册 → 验证配置
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_UPDATED=false

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo "======================================================================"
echo "  🚀 金山文档 MCP Skill 配置"
echo "======================================================================"
echo ""

# 1. 检查 mcporter
echo "📦 检查 mcporter..."
if ! command -v mcporter &> /dev/null; then
    echo -e "${YELLOW}⚠️  未找到 mcporter，正在安装...${NC}"
    if command -v npm &> /dev/null; then
        npm install -g mcporter
        echo -e "${GREEN}✅ mcporter 安装完成${NC}"
    else
        echo -e "${RED}❌ 未找到 npm，请先安装 Node.js${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✅ mcporter 已安装 ($(mcporter --version 2>/dev/null || echo 'unknown'))${NC}"
fi

echo ""

# 2. 获取 Token
echo "🔑 获取 Token..."
if [ -n "$KDOCS_TOKEN" ]; then
    echo -e "${GREEN}✅ 使用环境变量 KDOCS_TOKEN${NC}"
    TOKEN="$KDOCS_TOKEN"
else
    # 检查是否有缓存的 token
    CACHE_FILE="$HOME/.kdocs_token_cache"
    if [ -f "$CACHE_FILE" ]; then
        CACHED_TOKEN=$(cat "$CACHE_FILE")
        if [ -n "$CACHED_TOKEN" ]; then
            echo "📁 使用缓存的 Token..."
            TOKEN="$CACHED_TOKEN"
        else
            echo -e "${YELLOW}⚠️  缓存 Token 无效，重新获取...${NC}"
            TOKEN_OUTPUT=$(bash "$SCRIPT_DIR/get-token.sh" --json 2>&1)
            TOKEN=$(echo "$TOKEN_OUTPUT" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
        fi
    else
        echo -e "${YELLOW}⚠️  未检测到 KDOCS_TOKEN，正在获取...${NC}"
        echo ""
        TOKEN_OUTPUT=$(bash "$SCRIPT_DIR/get-token.sh" --json 2>&1)
        TOKEN=$(echo "$TOKEN_OUTPUT" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
    fi
    
    if [ -z "$TOKEN" ]; then
        echo ""
        echo -e "${RED}❌ Token 获取失败${NC}"
        echo ""
        echo "请手动运行："
        echo "  bash $SCRIPT_DIR/get-token.sh"
        echo ""
        exit 1
    else
        echo -e "${GREEN}✅ Token 获取成功${NC}"
        # 缓存 token（有效期约 24 小时）
        echo "$TOKEN" > "$CACHE_FILE"
        chmod 600 "$CACHE_FILE"
    fi
fi

echo ""

# 3. 配置 mcporter
echo "⚙️  配置 MCP 服务..."
# 先移除旧配置（如果存在），再添加新配置
if mcporter config list 2>&1 | grep -q "kdocs"; then
    echo "📝 检测到已有配置，正在更新..."
    mcporter config remove kdocs 2>/dev/null || true
fi
mcporter config add kdocs "https://mcp-center.wps.cn/skill_hub/mcp" \
    --header "Authorization=Bearer $TOKEN" \
    --transport http \
    --scope home

echo -e "${GREEN}✅ 配置完成${NC}"
echo ""

# 4. 验证配置
echo "🧪 验证配置..."
if mcporter list 2>&1 | grep -q "kdocs"; then
    echo -e "${GREEN}✅ 配置验证成功！${NC}"
    echo ""
    echo "📋 已注册工具："
    mcporter list kdocs 2>/dev/null | head -5 || true
else
    echo ""
    echo -e "${YELLOW}⚠️  配置验证失败，请检查：${NC}"
    echo "   1. 网络连接是否正常"
    echo "   2. Token 是否有效（可重新运行 get-token.sh）"
    echo "   3. mcporter 版本是否最新"
    echo ""
    exit 1
fi

echo ""
echo "======================================================================"
echo "  🎉 配置完成！"
echo "======================================================================"
echo ""
echo "📖 使用方法："
echo ""
echo "   # 列出工具"
echo "   mcporter list kdocs"
echo ""
echo "   # 搜索文档"
echo "   mcporter call kdocs.search_files --args '{\"keyword\": \"发票\"}'"
echo ""
echo "   # 读取文档"
echo "   mcporter call kdocs.read_file_content --args '{\"file_id\": \"xxx\"}'"
echo ""
echo "🏠 金山文档：https://365.kdocs.cn/latest"
echo ""
echo "💡 提示：Token 缓存于 ~/.kdocs_token_cache，过期后重新运行本脚本"
echo ""

# 清理旧缓存（超过 24 小时）
if [ -f "$CACHE_FILE" ]; then
    CACHE_AGE=$(( $(date +%s) - $(stat -f%m "$CACHE_FILE" 2>/dev/null || stat -c%Y "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$CACHE_AGE" -gt 86400 ]; then
        rm -f "$CACHE_FILE"
        echo "🧹 已清理过期的 Token 缓存"
    fi
fi
