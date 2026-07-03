# Prerequisites

Before running any `agentengine` deploy command, ensure the cloud credentials and model settings are properly configured.

## Required Credentials

| Variable | Description | Required For |
|----------|-------------|--------------|
| `KSYUN_ACCESS_KEY` | 金山云 Access Key | 所有云端部署 |
| `KSYUN_SECRET_KEY` | 金山云 Secret Key | 所有云端部署 |
| `KSYUN_ACCOUNT_ID` | 金山云账号 ID | 所有云端部署 |
| `KSYUN_REGION` | 默认区域 (如 `cn-beijing-6`) | 所有云端部署 |
| `OPENAI_API_KEY` | 模型服务 API Key | Hermes / OpenClaw 运行时 |
| `OPENAI_BASE_URL` | 模型服务地址 | Hermes / OpenClaw 运行时 (有默认值) |
| `OPENAI_MODEL_NAME` | 默认模型名称 | Hermes / OpenClaw 运行时 (有默认值) |

## Configuration Methods

### Method 1: Interactive Wizard (Recommended for First Run)

```bash
agentengine config wizard
```

This will guide through:
1. Basic project info (name, framework)
2. Model service config (API Key, Base URL)
3. Kingsoft Cloud credentials (AK/SK, Account ID)

At the end, it will ask whether to save to global config (`~/.agentengine/settings.json`) for reuse in future projects.

### Method 2: Edit `.env` Directly

Create or edit the project `.env` file:

```bash
# Kingsoft Cloud Credentials (required for deploy)
KSYUN_ACCESS_KEY=your-access-key
KSYUN_SECRET_KEY=your-secret-key
KSYUN_ACCOUNT_ID=200000xxxx
KSYUN_REGION=cn-beijing-6

# Model Service (required for Hermes/OpenClaw runtime)
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
OPENAI_MODEL_NAME=glm-5.1
```

### Method 3: Non-interactive Set Command

```bash
# Set individual values
agentengine config set KSYUN_REGION=cn-beijing-6
agentengine config set OPENAI_MODEL_NAME=glm-5.1

# Set multiple values at once
agentengine config set KSYUN_REGION=cn-beijing-6 OPENAI_MODEL_NAME=glm-5.1

# Also save to global config
agentengine config set KSYUN_REGION=cn-beijing-6 --global
```

### Method 4: Environment Variables

Export directly in shell:

```bash
export KSYUN_ACCESS_KEY=xxx
export KSYUN_SECRET_KEY=xxx
export KSYUN_ACCOUNT_ID=xxx
export KSYUN_REGION=cn-beijing-6
```

## Verify Configuration

Check current configuration:

```bash
agentengine config show
```

This displays:
- Project config (`agentengine.yaml` / `ksadk.yaml`)
- Project environment (`.env`)
- Global config (`~/.agentengine/settings.json`)
- Effective merged environment variables

## Configuration Priority

Variables are resolved in this order (highest priority first):

1. CLI flags (e.g., `--region`, `--model`)
2. Project `.env` file
3. Global config (`~/.agentengine/settings.json`)
4. Shell environment variables

## Default Values

If not configured, these defaults apply:

| Variable | Default Value |
|----------|---------------|
| `KSYUN_REGION` | `cn-beijing-6` |
| `OPENAI_BASE_URL` | `http://kspmas.ksyun.com/v1` |
| `OPENAI_MODEL_NAME` | `glm-5.1` |

## Common Issues

### Missing Credentials

If you see authentication errors:

```
Error: KSYUN_ACCESS_KEY is required
```

Run the wizard or set the missing variables:

```bash
agentengine config wizard
# OR
agentengine config set KSYUN_ACCESS_KEY=xxx KSYUN_SECRET_KEY=xxx
```

### Wrong Region

If the target is Hermes in `pre-online` but config shows `cn-beijing-6`:

```bash
agentengine config set KSYUN_REGION=pre-online
```

### Inner vs Public Endpoint

For accounts that require intranet access:

```bash
agentengine config set AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com
```

## Credential Sources

| Credential | Where to Get |
|------------|--------------|
| `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY` | https://iam.console.ksyun.com/ → AccessKey 管理 |
| `KSYUN_ACCOUNT_ID` | https://console.ksyun.com/ → 账户信息 |
| `OPENAI_API_KEY` | If using Kingsoft Cloud model service: contact your admin; otherwise use your model provider's dashboard |
