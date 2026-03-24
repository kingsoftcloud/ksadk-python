---
name: agentengine-openclaw-oneclick-deploy
description: "使用 AgentEngine（ksadk）在 macOS/Linux/Windows 一键部署 OpenClaw/Agent 的单文件操作手册。优先中国可访问方式（清华/阿里 PyPI 镜像，默认 cn-beijing-6），自动处理 Python 3.10+、全局 PATH、首次全局配置、工作目录创建、OpenClaw 重名规避，并统一使用当前 canonical CLI（dashboard open、config show/set、agent/openclaw status）。Use when user asks: 一键部署 openclaw、agentengine 部署、无 python 环境安装、国内源安装 ksadk、减少交互自动化部署。"
---

# AgentEngine OpenClaw Oneclick Deploy

## Goal

用最少询问完成 OpenClaw 部署，默认行为：

1. 先静默验证现有全局配置/环境变量可用性；可用则直接下一步，不再询问。
2. 自动准备工作目录。
3. 自动安装或升级 Python 3.10+（推荐 3.12；按系统包管理器）。
4. 自动安装 `ksadk`（或 `agentengine-sdk-python`）最新版；仅在“非最新版”时覆盖更新。
5. 首次使用自动写入 `~/.agentengine/settings.json` 全局配置。
6. OpenClaw 名称默认自动加时间戳，无需询问。
7. OpenClaw `RUNNING` 后执行 `agentengine dashboard open`；人类交互场景可自动打开浏览器，AI/非 TTY 场景优先 `--no-open --output json`。

## Hard Rules

- 不使用 Docker。
- 默认区域使用 `cn-beijing-6`（除非用户明确指定）。
- 优先金山云星流模型服务：只要求 `OPENAI_API_KEY`，`OPENAI_BASE_URL/OPENAI_MODEL_NAME` 可选。
- 如果用户已提供 OpenAI 兼容 endpoint/model，直接复用，不覆盖。
- OpenClaw 不要求先 `init` 项目，直接 `openclaw deploy` 即可。
- OpenClaw 名称默认 `openclaw-gateway-$(date +%m%d%H%M%S)`，不询问名称。
- 如果用户已有足够全局配置或环境变量并且验证通过，直接执行后续步骤，不重复确认。
- 如果本机已安装 `ksadk` 且已是最新版，不执行升级安装。
- 只使用当前 canonical 命令，不使用历史兼容别名，例如 `agentengine model`、`agentengine status`、`agentengine dashboard --share`。
- 在 AI Agent、非 TTY 或需要稳定解析的场景，优先使用 `--output json`；需要分享链接时优先 `agentengine dashboard open --share --no-open`。
- 除非关键信息缺失，不向用户追问，直接执行默认策略。

## Official Links

- AK/SK（IAM AccessKey）：`https://uc.console.ksyun.com/pro/iam/#/user/list` 打开进去这找到自己用户，如果没有权限就找主账号管理员要。
- 模型 API 说明：`https://docs.ksyun.com/documents/44742?type=3`
- 星流控制台：`https://ksp.console.ksyun.com/#/apiKey` 如果没有权限就找主账号管理员要。

## Step 0: Zero-Question Fast Path (Required)

先做无交互验证，若已可用则直接进入部署，不再询问：

```bash
# 1) 检查可用性（可用于判断 KOP 鉴权是否通过）
agentengine openclaw list --region "${KSYUN_REGION:-cn-beijing-6}" --output json >/dev/null 2>&1

# 2) 若上一步成功：直接跳过“补全配置提问”，继续 Step 1+
# 3) 若失败：再进入后续配置与安装步骤
```

对必须变量采用“环境变量优先、全局配置回退”策略：

- `KSYUN_ACCESS_KEY`
- `KSYUN_SECRET_KEY`
- `KSYUN_REGION`（默认 `cn-beijing-6`）
- `OPENAI_API_KEY`（或 `OPENCLAW_MODEL_API_KEY`）

## Step 1: Prepare Working Directory

优先使用用户指定目录；未指定时使用当前目录。若目录非空且不是 AgentEngine 项目，自动创建子目录继续，避免打断用户。

```bash
# AgentEngine 项目判定：存在 agentengine.yaml 或 ksadk.yaml
# 若当前目录非项目且非空：
mkdir -p "./agentengine-openclaw-$(date +%Y%m%d-%H%M%S)"
cd "./agentengine-openclaw-$(date +%Y%m%d-%H%M%S)"
```

## Step 2: Ensure Python 3.10+ (Prefer 3.12)

先检测：

```bash
python3.12 --version || python3 --version || python --version
```

若版本小于 3.10，自动尝试安装 3.12：

### macOS

```bash
brew install python@3.12
```

### Debian/Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip
```

### Fedora/RHEL

```bash
sudo dnf install -y python3.12 python3.12-pip
```

### Windows

```powershell
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
```

## Step 3: Install SDK (China Mirrors)

优先 `ksadk`；也允许 `agentengine-sdk-python`。

最新版（清华源）：

```bash
python3 -m pip install -U ksadk -i https://pypi.tuna.tsinghua.edu.cn/simple
# 或
python3 -m pip install -U agentengine-sdk-python -i https://pypi.tuna.tsinghua.edu.cn/simple
```

最新版（阿里源）：

```bash
python3 -m pip install -U ksadk -i https://mirrors.aliyun.com/pypi/simple
# 或
python3 -m pip install -U agentengine-sdk-python -i https://mirrors.aliyun.com/pypi/simple
```

固定版本（仅在用户明确要求 pin 版本时）：

```bash
python3 -m pip install "ksadk==<VERSION>" -i https://pypi.tuna.tsinghua.edu.cn/simple
# 或
python3 -m pip install "agentengine-sdk-python==<VERSION>" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

默认不要在 skill 里硬编码旧版本号；只有用户明确要求固定版本时才替换 `<VERSION>`。

仅在非最新版时安装（示例）：

```bash
INSTALLED=$(python3 -m pip show ksadk 2>/dev/null | awk -F': ' '/^Version:/{print $2; exit}')
LATEST=$(python3 -m pip index versions ksadk -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null | awk -F': ' '/Available versions:/{print $2; exit}' | awk -F',' '{gsub(/ /, "", $1); print $1}')
if [ -z "$INSTALLED" ] || [ "$INSTALLED" != "$LATEST" ]; then
  python3 -m pip install -U ksadk -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
```

规则：如果检测到 `ksadk` 已是最新版，必须直接跳过安装步骤，不做覆盖更新；只有版本落后时才 `python3 -m pip install -U`。

## Step 4: Ensure Global PATH

如果 `agentengine` 不在 PATH，补充 Python 用户目录并持久化。`bash` 下优先按当前平台写入更常用的 profile：macOS 用 `~/.bash_profile`，Linux/WSL/Git Bash 用 `~/.bashrc`。

```bash
USER_BIN=$(python3 - <<'PY'
import site
print(site.USER_BASE + '/bin')
PY
)
export PATH="$USER_BIN:$PATH"
grep -Fq "$USER_BIN" ~/.zshrc 2>/dev/null || echo "export PATH=\"$USER_BIN:\$PATH\"" >> ~/.zshrc
if uname | grep -qi darwin; then
  BASH_PROFILE="$HOME/.bash_profile"
else
  BASH_PROFILE="$HOME/.bashrc"
fi
grep -Fq "$USER_BIN" "$BASH_PROFILE" 2>/dev/null || echo "export PATH=\"$USER_BIN:\$PATH\"" >> "$BASH_PROFILE"
```

Windows 将 `Python\Scripts` 写入用户 PATH。

## Step 5: Prepare Env

创建或补齐 `.env`（最小必需）：

```env
KSYUN_ACCESS_KEY=...
KSYUN_SECRET_KEY=...
KSYUN_REGION=cn-beijing-6
OPENAI_API_KEY=...
# 可选：已存在则复用
# OPENAI_BASE_URL=...
# OPENAI_MODEL_NAME=...
```

如果用户不知道 AK/SK 或模型 key，先给上面的官方链接，不要阻塞后续流程描述。

模型与配置可用性验证：

### 星流（优先）

先用非交互方式确认当前生效配置：

```bash
agentengine config show --output json
```

如果只提供了 `OPENAI_API_KEY` 且未显式指定 `OPENAI_BASE_URL/OPENAI_MODEL_NAME`，不要调用交互式 `agentengine config model`；直接继续部署，由 `openclaw deploy` 复用当前生效的 `OPENAI_*` 配置。

### 第三方 OpenAI 兼容 endpoint/key/model

使用 OpenAI 格式 `curl` 验证（建议低成本请求）：

```bash
curl -sS "${OPENAI_BASE_URL%/}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d "{
    \"model\": \"${OPENAI_MODEL_NAME}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"ping\"}],
    \"max_tokens\": 1,
    \"temperature\": 0
  }"
```

返回中无鉴权或模型不存在错误即可继续部署。

## Step 6: First-Time Global Config

若 `~/.agentengine/settings.json` 不存在，自动执行全局配置（减少后续输入）：

```bash
agentengine config set \
  KSYUN_ACCESS_KEY="$KSYUN_ACCESS_KEY" \
  KSYUN_SECRET_KEY="$KSYUN_SECRET_KEY" \
  KSYUN_REGION="${KSYUN_REGION:-cn-beijing-6}" \
  OPENAI_API_KEY="$OPENAI_API_KEY" \
  --global
```

若用户有 `OPENAI_BASE_URL/OPENAI_MODEL_NAME`，追加：

```bash
agentengine config set \
  OPENAI_BASE_URL="$OPENAI_BASE_URL" \
  OPENAI_MODEL_NAME="$OPENAI_MODEL_NAME" \
  --global
```

## Step 7: Generate OpenClaw Name

部署前直接生成时间戳名称，不询问用户：

```bash
NAME="openclaw-gateway-$(date +%m%d%H%M%S)"
```

## Step 8: Deploy and Auto-Open Dashboard

部署：

```bash
agentengine openclaw deploy --name "$NAME" --region "${KSYUN_REGION:-cn-beijing-6}"
```

轮询状态。自动化场景优先读取 JSON，不要解析 pretty 表格：

```bash
for i in $(seq 1 30); do
  OUT=$(agentengine openclaw status --output json 2>&1 || true)
  echo "$OUT"
  printf '%s' "$OUT" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
item = data.get("item") or {}
status = str(item.get("status") or "")
raise SystemExit(0 if status.upper() == "RUNNING" else 1)
'
  [ $? -eq 0 ] && break
  sleep 10
done
```

人类交互终端可直接打开浏览器：

```bash
agentengine dashboard open --share
```

AI Agent、CI 或非 TTY 场景优先只返回 URL：

```bash
agentengine dashboard open --share --no-open --output json
```

## Non-OpenClaw Agent

```bash
agentengine launch . --target serverless
agentengine agent status
agentengine dashboard open --share
```

## Response Style

执行此技能时，默认给用户：

1. 你自动选择的工作目录。
2. 你自动执行的安装动作（Python、ksadk、PATH、全局配置）。
3. 最终部署结果（Agent 名称/ID、状态）。
4. 若是人类交互终端，可直接打开 Dashboard；若是 AI/非 TTY，返回 `dashboard open --share --no-open --output json` 的结果。
5. 若未成功打开，给一条可复制命令：`agentengine dashboard open --share`。

避免让用户手动拼接长命令；优先你直接执行并回报结果。
