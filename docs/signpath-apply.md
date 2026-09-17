# SignPath Foundation 开源签名申请文案

提交地址:https://signpath.org/apply (HubSpot 表单)

## 申请要填的字段(建议值)

### Project name
KsADK (Kingsoft Cloud Agent Development Kit)

### Project / Source repository URL
https://github.com/kingsoftcloud/ksadk-python

### Organization / Maintainer
KsADK Team, Beijing Kingsoft Cloud Network Technology Corporation Limited

### License
Apache-2.0 (OSI-approved)

### Project description(英文,填表用)

KsADK is an open-source agent runtime platform providing unified runtime, debugging, deployment, and observability for AI agents. The repository ships a desktop application, **AgentKit Studio**, built with Electron. It bundles a self-contained Python runtime, a Node toolchain, and the DSH plugin system, letting developers build, run, and inspect AI agents locally.

We request free code signing for the **Windows x64 NSIS installer** (`AgentKitStudio-Setup-x64.exe`) produced by our GitHub Actions release workflow. Today the installer is unsigned, so Windows users see SmartScreen warnings. SignPath signing would let us ship Authenticode-signed installers without managing private keys ourselves.

### What will be signed
Windows x64 NSIS installer (.exe) — Authenticode signature.
The build runs entirely on GitHub-hosted runners (`windows-latest`), triggered by `release: published`. The unsigned installer is uploaded as a GitHub Actions artifact, then submitted to SignPath via `signpath/github-action-submit-signing-request@v2`. No self-hosted runners are used.

### Distribution
Public GitHub Releases at https://github.com/kingsoftcloud/ksadk-python/releases

### Maintenance status
Actively maintained. (这里填你的发版频率,如 "regular releases every 2-4 weeks")

### Uninstall
Yes — the NSIS installer registers an entry in Add/Remove Programs and includes a full uninstaller that removes the install directory.

## 提交前自查(SignPath Foundation 条件)

- [x] 仓库公开:github.com/kingsoftcloud/ksadk-python ✓
- [x] OSI 许可证:Apache-2.0 ✓
- [x] 无商业双授权 ✓
- [x] 积极维护 ✓(你确认发版频率)
- [x] 已发布(有待签名的产物)✓
- [x] 提供卸载功能 ✓(NSIS uninstaller)
- [x] 无 hacking tools / 尊重用户隐私 ✓
- [x] 只签自己源码构建的产物 ✓(GitHub Actions build)
- [x] 签名前的 job 全在 GitHub-hosted runner ✓(SignPath OSS 要求)

## 审批通过后要做的(我帮你)

1. SignPath 后台创建 project,slug = `AgentKitStudio`
2. 关联 GitHub 仓库 URL,链接 GitHub.com Trusted Build System,装 SignPath GitHub App 到仓库
3. 创建 signing policy,slug = `release-signing`(生产)和 `test-signing`(测试)
4. Artifact Configuration:Authenticode 签 PE 文件(NSIS .exe)
5. 生成 API Token
6. 在 GitHub repo Settings → Environments → `studio-signing` 加:
   - secret `SIGNPATH_API_TOKEN`
   - variable `SIGNPATH_ORGANIZATION_ID`
   - (可选)variable `SIGNPATH_SIGNING_POLICY_SLUG` = `release-signing`(审批前用 `test-signing`)
