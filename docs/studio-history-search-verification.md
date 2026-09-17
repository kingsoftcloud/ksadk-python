# Studio 当前会话查找与历史定位验收

## 行为边界

`Cmd/Ctrl+F` 在会话工作台内查找当前会话正文。其他页面保留浏览器默认查找。
历史侧栏的「搜索会话」继续按会话标题筛选，两种操作使用独立状态。

查找使用 `ksadk-web` 的消息投影和既有历史分页接口，覆盖未挂载的消息、正文块、工具结果和附件名称。
不扫描隐藏推理或任意原始协议对象，也不创建或恢复原生执行。读取期间可继续编辑下一条输入。
切换会话、更新关键词、关闭查找或停止查找会中止此查询；迟到数据必须通过 owner 与 AbortSignal 校验。

成功读取全部历史后才显示「已查找全部历史」。网络失败或游标未推进时保留部分结果并允许重试。
如果最初的正文或规范事件读取失败，查找会提示先刷新会话，不把可读回退内容声称为完整历史。
结果预览最多显示 200 条匹配消息；匹配计数仍覆盖完整扫描范围，超过上限时提示缩小关键词范围。
选择结果后先停止继续读取，再定位消息；共享虚拟列表等待高度测量稳定，紧凑时间线保留原始消息到展示分组的映射。
初次打开会话时，可读正文可以先显示；查找会等规范事件重建完成后再使用最终消息 ID，避免结果指向已替换的临时行。

查找入口与结果使用现有 Studio 语义颜色、字号、圆角及焦点样式，不引入外部图形资产。

## 自动化

配对开发时先构建并打包当前 `ksadk-web` 工作树的库，将该 tarball 通过
`npm install --no-save --package-lock=false --ignore-scripts <tarball>` 安装到 Studio 的 `react-ui` 目录。
这样不会修改公开依赖版本或 lockfile。

在 `ksadk/studio/react-ui` 中执行：

```sh
npm run test:ui -- --run src/components/ConversationFind.test.tsx src/components/ChatWorkspace.behavior.test.tsx src/compactHarnessMessages.test.ts
npm run test:e2e:history-search
# 本机已有 Chrome、未安装 Playwright Chromium 时：
STUDIO_BROWSER_CHANNEL=chrome npm run test:e2e:history-search
```

浏览器用例先运行 Vite 生产构建，在独立端口使用脱敏合成的 2,000 条消息回放：

- 目标消息最初不在已加载的 50 条尾部窗口中。
- 查找遍历更早历史并识别中文正文。
- 点击结果后，目标位于可见区域并获得焦点。
- 使用 Studio 实际样式与集成顶栏布局，结果面板和消息区不重叠，鼠标可以选中结果。
- 共享虚拟列表的已挂载消息保持小于 100 条。
- KsADK 紧凑展示中的合并消息仍能定位到原始匹配内容。
- 原有输入内容在查找和跳转后保持不变。

报告位于 `output/playwright/history-search-report.json`，包含 Studio 源码和本地 Web 库的 SHA-256 指纹。
截图、失败轨迹及上下文位于 `output/playwright/history-search-results/`。上述目录为生成产物，不提交源码仓库。

这是当前会话查找与渲染的独立门禁，尚不证明真实云端、原生运行时恢复、全页面视觉或全部性能预算通过。
