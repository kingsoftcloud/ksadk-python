# AgentKit Studio 签名候选包

新版 Studio 的 macOS 签名入口是
`.github/workflows/release-agentkit-studio.yml`。它只做固定源码的签名、公证、产物校验和
Actions artifact 上传，不发布线上更新源。

## Environment 配置

工作流使用 `studio-signing` Environment。已有的 Apple Secret 名称保持不变：

- `STUDIO_APPLE_P12_BASE64`
- `STUDIO_APPLE_P12_PASSWORD`
- `STUDIO_APPLE_ID`
- `STUDIO_APPLE_APP_PASSWORD`
- `STUDIO_APPLE_TEAM_ID`

新版 Studio 仓库是私有仓库，必须额外配置 `AGENTKIT_STUDIO_SOURCE_TOKEN`。这个 token
只需要对 `kingsoftcloud/agentkit-studio` 有 read-only Contents 权限，用于 checkout
工作流输入的 40 位 commit SHA。它不用于 Apple 签名、公证或更新源发布，也不能把 token
值写入仓库、workflow input 或日志。

旧仓库的默认 `GITHUB_TOKEN` 不作为跨仓库 Studio checkout 的隐式凭证；缺少
`AGENTKIT_STUDIO_SOURCE_TOKEN` 时，工作流应在 checkout 阶段失败。

## 触发和验收

手动运行时填写：

1. `studio_ref`：新版 Studio 的 40 位 immutable commit SHA；
2. `dsh_ref` 与 `dsh_commit`：DSH tag/ref 及其期望 commit SHA；
3. `channel`：`stable` 或 `preview`；
4. `update_origin`：无路径的 HTTPS origin，例如 `https://updates.example.com`。

工作流会再次校验两个 checkout 的实际 SHA，并在隔离 DSH checkout 中应用完整 patch
series。签名后会校验 App ID、`app-update.yml`、channel metadata 的版本、ZIP 大小和
SHA-512、`codesign`、`stapler` 以及 `spctl`，再把 DMG、ZIP、ZIP blockmap、metadata
和 `SHA256SUMS` 上传为非隐藏路径下的 artifact。

本地可先做不接触 Secret 的配置验收：

```sh
python - <<'PY'
import yaml
from pathlib import Path
yaml.safe_load(Path('.github/workflows/release-agentkit-studio.yml').read_text())
print('workflow YAML parsed')
PY
bash -n <(sed -E 's/\$\{\{[^}]+\}\}//g' .github/workflows/release-agentkit-studio.yml)
```

真正的签名运行仍需要有效的 Apple Secret、私有 Studio checkout token 和可写的更新源
配置；本工作流不会把“候选包已验证”和“线上 feed 已发布”合并成一个步骤。
