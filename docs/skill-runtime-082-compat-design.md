# 沙箱 KsADK 0.8.2 的本地 Skill 兼容方案

状态：已在本地分支 `codex/skill-runtime-082-compat` 实施，尚未对真实 0.8.2 沙箱执行端到端验证。适用于当前外层 KsADK 0.8.4 集成代码与 Base Agent，沙箱暂时保留 0.8.2。

## 1. 结论与已核对事实

保留 Base Agent 按请求空间 ID/名称获取列表、下载、缓存和选择 Skill 的实现。外层仍接收 `pinned_packages: list[SkillPackage]`，只增加向旧沙箱交付目录和回收产物的后端适配，不要求沙箱重新访问 Skill Service。

0.8.2 发布候选 `57a82782` 的 `runtime/agent.py` SHA-256 与此前实际沙箱诊断一致：`451688fb063d56edbfd6e10ba4ba37b56e969cf469cbc2565c3e881f31d7ce4e`。这能确认入口文件对应版本，不能代替对实际沙箱所有依赖的能力检查。

源码核对结果：

- 0.8.2 支持 `--request-file`，读取 `workflow_prompt` 和 `skill_names`。
- `KSADK_LOCAL_SKILLS_DIR` 指向父目录，其直接子目录包含 `SKILL.md` 时可加载完整 Skill。
- `KSADK_SKILL_WORKDIR` 控制工作目录；通用工作流设置其下 `artifacts/` 为 `KSADK_SKILL_OUTPUT_DIR`。
- 旧版返回 `workflow_result`，含 status、executed_skill、commands、output_files、artifacts 等；路径属于沙箱文件系统。
- 缺少 pinned manifest 解析、包协议校验和 artifact_bundle ZIP 回执。
- 旧版执行器仍只支持其已有入口（例如 `scripts/run-workflow.sh` 和 web-artifacts-builder）；适配不扩展工作流执行能力。

## 2. 模式选择

新增外层配置 `KSADK_SKILL_SANDBOX_PROTOCOL`：

- `pinned_v1`：默认，保持现有包交付和 ZIP 回执流程。
- `legacy_local_082`：此次部署显式启用；入口能力检测、目录交付和文件回收走兼容实现。

本轮不自动把任意协议探测失败降级为旧版。网络异常、命令超时、包损坏与明确的旧版能力不同，不能混为同一种兼容条件。

模式选择在任何包上传或工作流执行前完成；不在同一次执行失败后换模式重跑，以免重复执行有副作用的工作流。所有诊断和结果记录标明实际模式。

## 3. Skill 交付和执行

1. Base Agent 沿用现有 `skill_source`、包缓存/句柄生命周期和 `pinned_packages` 传参。
2. 外层校验包引用、归档哈希、大小、重复名称和所选 Skill 是否在本次包集合内。使用现有安全解包能力，从已校验归档生成独立的交付目录，不直接信任可变缓存目录中的后来内容。
3. 创建沙箱，执行旧版能力探测，确认使用的 Python、KsADK 版本、本地目录 loader 和请求入口。失败则返回明确阶段错误并按现有流程清理。
4. 使用随机请求目录，分开保存 `skills/`、`work/`、请求文件和日志；完整上传脚本、资源及 `SKILL.md`，保留必要可执行权限。按现有限额分批传输；拒绝越界、符号链接及其他特殊文件。
5. 在运行命令的环境里设置 `KSADK_LOCAL_SKILLS_DIR=<request>/skills`、`KSADK_SKILL_WORKDIR=<request>/work`、所选名称及执行超时。输入文件继续使用现有 `/workspace/inputs` 约定，不能覆盖交付目录。
6. 兼容命令明确清除旧版支持的所有 Skill 空间/服务配置别名，覆盖模板继承值；不向旧沙箱传递下载 Skill 所需的账号凭证。避免 loader 在本地加载之后又列举远端空间。其他工作流自身确实需要的环境按原约定处理。
7. 上传后、执行前对交付文件读取核对哈希/大小，记录外层交付清单。此记录仅证明上传内容核对，不作为原生 pinned 回执或受信任执行证明。
8. 使用已探测的隔离 Python 入口 `python -I -u -m ksadk.skills.runtime.agent --request-file ...`，旧版请求只含支持的字段。stdout/stderr 继续重定向到独立日志，便于非零退出和超时后恢复。

目录示意：

```text
/tmp/ksadk-legacy-<nonce>/
  skills/<unique-entry>/SKILL.md
  skills/<unique-entry>/scripts/...
  work/artifacts/...
  request.json
  stdout.log
  stderr.log
```

## 4. 产物回收（必须在销毁沙箱前）

新版流程本身也是外层主动下载：沙箱内生成 ZIP/回执，外层下载校验 ZIP，然后销毁。兼容模式改为外层取得旧版文件清单、校验并逐个下载文件，不等待不存在的 `artifact_bundle`。

1. 读取日志并严格解析唯一的 `workflow_result`。保留旧版实际工作流状态、执行名称及命令输出。
2. 以 `output_files`（兼容 artifacts 字段）为产物来源；相对路径按请求工作目录解析。仅接受本次 work/ 下普通文件，不读取输入、Skill 源码、凭证目录或任意绝对路径。
3. 若执行失败/超时、没有完整结果，可有界枚举本次 `work/artifacts/` 找回部分产物；明确记录来源为目录恢复，不将其标成工作流声明的完整结果。不扫描整个沙箱或其他请求目录。
4. 外层驱动的独立收集 helper 只依赖 Python 标准库，不安装或覆盖沙箱 KsADK。它校验路径、目录链和文件类型，用禁止跟随链接的文件打开方式读取，在独立收集目录生成稳定快照和文件哈希/大小清单。随后外层通过沙箱文件 API 逐个读取快照字节并核对。避免只做字符串前缀检查就读取工作流给出的任意路径，也避免边写边回收导致内容不一致。
5. 沿用现有上限：100 个文件、单文件 20 MiB、总计 100 MiB；枚举、日志读取和回收时间另有明确上限。单个文件未完整读取或哈希不符时不发布它，已验证文件可按 partial 状态保留。
6. 将文件写入本次 Agent 提供的宿主 artifact_directory 下的独立目录，只有完成校验才暴露路径。结果中的 output_files 及供后续解析的 workflow_result 路径统一替换为宿主路径，远端路径只保留作诊断。
7. 然后读取可用事件、记录收集状态并销毁沙箱。执行错误、回收错误、清理错误分别保留，后发生的错误不能覆盖最初故障。
8. Base Agent 沿用现有宿主文件读取：生成 output_text、截断信息和评测产物条目，消费后清理其拥有的宿主临时根目录。包缓存继续遵循既有租约/回收策略，与产物目录分开。

回收在成功、非零退出和超时路径都应尝试；沙箱已不可达时只能返回回收失败，不能宣称无产物。超时后先尝试停止该请求仍运行的进程，限定收集时间；无法形成稳定快照时明确标记不完整，最终仍执行沙箱清理。

## 5. 结果与证据边界

对 Base Agent 保持同一个运行结果接口，output_files 始终为已下载的宿主文件：

- workflow_status、executed_skill、commands 来自旧版真实结果；整体 Python 非零退出不覆盖已解析的工作流信息。
- sandbox 创建、执行、收集、清理状态沿用外层已有字段和诊断。
- 兼容模式及收集方式在外层诊断/结果元数据中显式记录，不伪造原生 ZIP 回执。
- 旧版没有的 instructions、内部 skill_events、invocation/version 回执保持缺失或 not_evaluable。Base Agent 已读取的 SKILL.md 可继续用于提示，不冒充沙箱返回字段。
- 外层选择、下载、上传可以关联原有 invocation；实际执行名称只作为旧版自报证据，不能仅凭上传成功、名称相同或单计划推断原生 pinned 版本验证成功。
- 执行成功但回收失败时，分别呈现 workflow 成功和 collection 失败；不声称产物已交付。

## 6. 代码接入范围

- `ksadk/skills/runtime/backends/e2b.py`：模式选择、共同生命周期、失败/超时回收和结果归一化。
- 新增小型兼容交付模块：独立处理 0.8.2 能力检查、环境隔离、目录上传和旧请求，不混入原生 pinned.py 的版本语义。
- 新增兼容产物模块及标准库 helper：边界检查、快照、限额下载、宿主落盘；复用现有限额及安全路径工具。
- 必要时扩展 runtime 结果的可选收集元数据；Base Agent 仅适配这些状态，动态 Skill 获取和执行入口无需重做。
- 增加配置注册/文档。保留现有 0.8.4 路径及测试，不整体回滚到旧的 result_collection、correlation 或注入旧 SDK 的实现。

## 7. 验证与切回

测试覆盖：明确选择两种模式；探测失败不偷偷降级；完整包资源和权限上传；阻断远端二次下载；名称冲突/坏包；正常及非零退出；超时日志恢复；无产物；文本/二进制/子目录产物；越界/符号链接/过大文件；部分下载与清理失败；宿主路径归一化及 Agent 消费后清理；旧版缺失证据不误报版本验证成功。

实现后先运行本地旧版夹具及现有 pinned 回归，再用真实 0.8.2 沙箱请求确认完整链路。之后沙箱升级时将配置切回 `pinned_v1` 并复验，不必撤销 Agent 的动态 Skill 设计。

## 8. 当前实现记录

兼容代码位于 `ksadk/skills/runtime/legacy_local.py`，由 E2B Skill Runtime 在
`KSADK_SKILL_SANDBOX_PROTOCOL=legacy_local_082` 时调用。默认值仍为 `pinned_v1`。

当前实现已经覆盖：归档重新校验及安全解包、完整目录上传和逐文件回读核对、可执行权限恢复、0.8.2 能力探测、Skill Service 环境隔离、旧请求执行、正常/非零退出/超时产物回收、沙箱路径替换为宿主路径、产物链接/越界/大小限制，以及执行、收集、清理状态分别保留。

隔离安装 `57a82782` 对应的真实 KsADK 0.8.2 后，兼容测试发现旧版不会预创建 `KSADK_SKILL_OUTPUT_DIR`；实现已在运行前创建本次请求的 `work/artifacts`，随后真实旧版入口执行、宿主产物下载和内容核对通过。

本地验证结果：

- KsADK `tests/resource_runtime`、`tests/skills`、环境变量注册及通用沙箱测试：675 passed，13 skipped。
- 上述范围包含隔离 KsADK 0.8.2 入口测试、新版 pinned 回归，以及正常、非零退出、超时恢复和符号链接拒绝场景。
- Base Agent：54 passed。
- Ruff、Python 编译和 `git diff --check` 通过。

跳过项依赖官方 DSH/Cordis 测试安装、独立 E2B 2.15.3 检查环境、真实远端沙箱或外部 fixture，与本次本地兼容实现的失败无关。

本方案尚未运行新的远端请求。真实验证还依赖使用当前代码重新构建外层 Agent 包并部署到现有 0.8.2 沙箱模板。
