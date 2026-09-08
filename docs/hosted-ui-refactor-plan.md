# Hosted UI 重构技术方案

> 版本: v2.1 | 日期: 2026-05-20 | 状态: 执行版

---

## 0 执行规则（给重构 agent）

这份计划不是一次性大重写。执行时必须按 Phase 顺序推进，每个 Phase 都要能独立 build、独立回归、独立 review。不要把后续 Phase 的功能提前混进当前 PR。

### 0.1 硬性规则

1. **先建分支再动代码**：从当前 `ksadk-python` 仓库新建重构分支，建议命名 `refactor/hosted-ui-v2`。
2. **每个 Phase 至少一个可 review 的提交/PR**：不要把 API、store、workspace 编辑、artifact 预览、视觉、构建链路揉成一个巨型提交。
3. **不能静默扩大范围**：本计划没有要求的能力不要顺手加，例如 Pyodide、RAG 管理、复杂 diff、多人协作、自动保存。
4. **遇到语义不确定先停下确认**：尤其是 Workspace 保存覆盖语义、share/private/owner 写权限、构建产物是否从 git 删除。
5. **每个 Phase 完成前必须跑验证**：至少跑对应前端 build/lint/test，以及该 Phase 里新增的单测或手动回归清单。
6. **不删除 `static/`、`dist-hosted/` 的已跟踪产物**：除非 Phase 5 的“决策 B”被明确批准。Phase 5 默认只做自动构建链路。
7. **优先使用 superpowers skill 和前端设计 skill**：执行每个 Phase 前先检查并使用相关 superpowers skill；涉及 UI、交互、视觉、布局、响应式、组件拆分或前端体验时，必须优先使用 frontend-design/frontend-skill 类技能。只有当前任务与技能内容确实无关时才可跳过，并在 PR/提交说明里写明跳过原因。

### 0.2 每个 PR 的交付格式

每个 PR/提交说明必须包含：

- 改了哪些模块。
- 没改哪些模块。
- 跑了哪些命令，结果是什么。
- 哪些功能做了手动回归。
- 是否有未确认语义；如果有，列出来，不要用代码猜。

## 1 背景与目标

### 1.1 现状问题

当前 Hosted UI 是一个 8157 行的单页应用（React 19 + Vite 8 + TailwindCSS 3），存在以下结构性问题：

| 问题 | 具体表现 | 影响 |
|------|---------|------|
| 巨石组件 | `App.tsx` 1768 行，15+ useState，13+ fetch 调用 | 新增功能必须修改同一文件，合并冲突频繁 |
| 无代码分割 | mermaid/cytoscape diagram chunks（~1.6MB 总计）、react-syntax-highlighter（128KB）、katex（257KB）同步加载 | 首屏必须下载 8.1MB JS |
| xterm 半拆包 | JS 主库已在组件内 dynamic import，但 `NativeTerminalPanel` 组件入口和 xterm CSS 仍在主路径 | terminal 相关体积虽不是首屏阻塞，但也没有真正按需加载 |
| API 调用散落 | 13+ 处内联 `fetch('/agentengine/api/v1/...')`，另有 `/_ksadk/terminal/*` 和 FormData 上传 | 无统一错误处理、无法 mock 测试、缓存策略无法复用 |
| Workspace 只读 | 文本文件用 `<pre><code>` 展示，无编辑能力 | agent 开发者无法在 UI 中调试修改文件 |
| HTML/JS 不渲染 | 代码块只有复制按钮，没有预览 | agent 输出的报表、图表、页面无法直接查看 |
| 视觉待打磨 | CSS 变量沿用 shadcn 默认值，消息无气泡区分，中文排版未优化 | 与成熟产品视觉差距明显 |
| 构建产物提交 git | `static/` + `dist-hosted/` 已跟踪 352 个文件，每次构建大 diff | review 噪音，且 local/hosted 两份可能不同步 |

### 1.2 目标

1. **开发效率**：新增一个 UI 功能只需修改 1-2 个文件，不需要动 `App.tsx`
2. **首屏性能**：建立 bundle budget，首屏 JS 体积可控、关键重模块按需加载
3. **渲染能力**：HTML/JS 代码块可沙箱预览，Workspace 文件可编辑保存
4. **视觉品质**：品牌化设计体系，user 气泡 + assistant 文档式阅读区
5. **构建规范**：CI 自动构建前端并打包进 wheel，再评估是否从 git 移除产物

### 1.3 不做的事

- 不换框架（React → Svelte），迁移成本远大于收益
- 不做 Pyodide 浏览器内 Python 执行，执行能力由 Sandbox Runtime 提供
- 不做通用向量库/RAG 管理，这属于 Skill Service 管辖
- 不做 WebSocket 替代 SSE，当前同步交互模型下 SSE 足够

---

## 2 架构重构

### 2.1 状态管理：按职责分层，不盲目全局化

核心原则：**跨组件共享读的状态放 store，单次 run 的瞬态用 hook/reducer，message store 只接收已归一化的 patch。**

#### 2.1.1 Store 划分

```
src/stores/
  bootstrap.ts    — agentId, agentName, capabilities, accessMode（跨组件共享）
  session.ts      — sessions[], currentId, CRUD 操作（跨组件共享）
  message.ts      — messages[], 增量 patch 更新（跨组件共享）
  streaming.ts   — isStreaming, currentRunId, stopRequested（UI 共享读状态）
  model.ts        — models[], selected, thinkingMode（跨组件共享）
  workspace.ts    — 可选；只有 workspace 状态被面板外部共享时才创建
  ui.ts           — sidebar, mobileDrawers, workspacePanel, darkMode（shell 共享）
  artifact.ts     — Phase 4 创建；content, type, visible（预览面板共享）
```

**不要为了目录结构而创建空 store。** 如果 `WorkspacePanel` 内部自己消费 `files/path/preview`，先放在 `useWorkspaceFiles()` hook 或组件 local reducer 中。只有当 Header、Composer、ArtifactsPanel 等其他组件也要读写 workspace 状态时，才把对应字段迁入 `stores/workspace.ts`。

#### 2.1.2 streaming 状态边界

这是最容易出错的地方。严格区分：

| 位置 | 存什么 | 原因 |
|------|--------|------|
| `streaming.ts` store | `isStreaming`、`currentRunId`、`stopRequested` | ChatComposer 需要读 `isStreaming` 禁用提交按钮；ChatMessageList 需要读 `stopRequested` 显示停止按钮 |
| `useRunAgent()` hook/reducer | `AbortController`、SSE ReadableStream reader、增量事件 buffer、resume cursor、queue 消费逻辑 | 这些是一次 run 的瞬态，放 store 会产生陈旧闭包和竞争问题 |
| `message.ts` store | 只接收已 normalize 的 message patch | 不直接写 raw SSE 事件，由 hook 归一化后 push |

#### 2.1.3 迁移策略

逐个 store 替换，每个是独立 PR：

1. 创建 `src/stores/ui.ts`，把 `sidebarOpen`、`mobileSidebarOpen`、`workspacePanelOpen` 等 UI 开关迁入
2. `App.tsx` 中 `const sidebarOpen = useUIStore(s => s.sidebarOpen)` 替换原 `useState`
3. 删除对应 `useState` 和 `prop drilling`
4. 验证功能不变，提交
5. 下一个 store 重复此流程

**不要**一次性把所有 useState 搬进 store。先迁 `ui` 和 `bootstrap`（最简单、最安全），验证流程顺畅后再迁 `session`、`message`。streaming store 只放 UI 共享读状态，run engine 逻辑留在 hook。

### 2.2 API 层：endpoint-specific wrappers + 统一错误模型

#### 2.2.1 错误模型（先于 API 实现定义）

```ts
// src/api/errors.ts
export class ApiError extends Error {
  constructor(
    public code: number,      // 业务码（Code 字段）或 HTTP status
    public message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export class StreamError extends Error {
  constructor(
    public event: string,     // 最后收到的 SSE event type
    public raw?: string,      // 最后收到的 raw data
    message?: string,
  ) {
    super(message || `SSE 流中断于 ${event}`);
    this.name = 'StreamError';
  }
}

export class CancelledError extends Error {
  constructor(message = '请求已取消') {
    super(message);
    this.name = 'CancelledError';
  }
}
```

覆盖的 6 类错误场景：

| 场景 | 抛出 | UI 处理 |
|------|------|---------|
| HTTP 非 2xx | `ApiError(response.status, ...)` | toast 通用错误 |
| `{ Code !== 0 }` | `ApiError(data.Code, data.Message, data)` | toast 业务错误消息 |
| JSON parse 失败 | `ApiError(-2, '响应格式异常')` | toast + console.error |
| SSE 中途断流 | `useRunAgent()` / SSE parser 抛 `StreamError(lastEvent, lastRaw)` | 显示"连接断开"可重试提示 |
| AbortError | client 统一转为 `CancelledError` | `useRunAgent()` 或 UI 层识别后静默处理 |
| blob/text 读取失败 | `ApiError(-3, '文件读取失败')` | toast |

UI 层**不再**直接判断 `data?.Code`，只 catch `ApiError`、`StreamError` 或 `CancelledError`。

错误归属要清楚：

- `client.ts` 负责 HTTP、业务 Code、JSON parse、blob/text 读取、AbortError 归一化。
- `streamAction()` 只负责发起 `RunAgent` 请求并返回 `ReadableStream`；它不知道后续 SSE 是否完整。
- `useRunAgent()` 或 SSE parser 负责读取 stream、记录最后一个 event/raw data，并在中途断流时抛 `StreamError`。
- 不允许底层 client 吞掉错误后返回 `undefined`，否则调用方会把取消、失败、空响应混在一起。

#### 2.2.2 目录结构

```
src/api/
  errors.ts       — ApiError, StreamError 定义
  client.ts       — 4 类通信原语
  session.ts      — createSession, listSessions, getSession, deleteSession
  events.ts       — listSessionEvents
  run.ts          — runAgent (返回 SSE ReadableStream)
  model.ts        — listAgentModels
  workspace.ts    — listFiles, addFile, deleteFile, getFileContent
  feedback.ts     — upsertFeedback, getFeedback, deleteFeedback
  bootstrap.ts    — getAgentUiBootstrap
  terminal.ts     — createTerminalSession, listTerminalSessions（路径 /_ksadk/terminal/*）
```

#### 2.2.3 4 类通信原语

```ts
// src/api/client.ts

/** 1. JSON POST — 普通 agentengine action */
export async function postJsonAction<T>(
  action: string,
  body: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<T>;

/** 2. FormData POST — UploadFile、AddWorkspaceFile */
export async function postFormAction<T>(
  action: string,
  formData: FormData,
  options?: { signal?: AbortSignal },
): Promise<T>;

/** 3. GET blob/text — GetWorkspaceFileContent、AttachmentContent */
export async function getResource(
  action: string,
  params: Record<string, string>,
  options?: { signal?: AbortSignal; asText?: boolean },
): Promise<Blob | string>;

/** 4. SSE stream — RunAgent */
export async function streamAction(
  action: string,
  body: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>>;
```

terminal API 路径前缀是 `/_ksadk/terminal/`，不走 `/agentengine/api/v1/`，在 `terminal.ts` 中单独封装。

#### 2.2.4 迁移策略

- 先创建 `errors.ts` + `client.ts` + `session.ts`，替换 App.tsx 中 session 域的 fetch
- 每个域一个 PR
- 全部迁移完成后删除 App.tsx 中的内联 fetch

### 2.3 代码分割

#### 2.3.1 延迟加载清单

| 模块 | 当前体积 | 触发条件 | 当前状态 |
|------|---------|---------|---------|
| mermaid + 全部 diagram chunks | ~1.6MB | 消息中遇到 mermaid 代码块 | 同步 import |
| react-syntax-highlighter + 语言定义 | ~128KB | 消息中遇到代码块 | 同步 import |
| katex + rehype-katex | ~257KB | 消息中遇到 `$...$` | 同步 import |
| @xterm/xterm JS 主库 | ~340KB | 用户打开 Terminal 面板 | 组件内 dynamic import |
| NativeTerminalPanel 组件入口 | ~2KB | 用户点击 Terminal 按钮 | 同步 import |
| xterm CSS | ~10KB | 同上 | 同步 import |
| WorkspacePanel 组件 | ~2KB | 用户点击 Workspace 按钮 | 同步 import |

**xterm 修正**：JS 主库已在 `NativeTerminalPanel.tsx` 内部 dynamic import，但组件入口和 CSS 仍在主路径。完整拆包需要：`NativeTerminalPanel` 本身改为 `React.lazy`，CSS 随组件加载或接受极小体积留在主包（在 bundle report 中单独标注）。

#### 2.3.2 实现方式

```tsx
// MessageMarkdown.tsx：轻量入口，不直接 import mermaid/katex/syntax-highlighter
const LazyCodeBlock = React.lazy(() =>
  import('./markdown/CodeBlock').then((m) => ({ default: m.CodeBlock }))
);
const LazyMermaidBlock = React.lazy(() =>
  import('./markdown/MermaidBlock').then((m) => ({ default: m.MermaidBlock }))
);
const LazyMathMarkdown = React.lazy(() =>
  import('./markdown/MathMessageMarkdown').then((m) => ({ default: m.MathMessageMarkdown }))
);

function hasMath(content: string) {
  return /\$\$[\s\S]+?\$\$|(^|[^\\])\$[^$\n]+\$/.test(content);
}

export function MessageMarkdown({ content }: { content: string }) {
  if (hasMath(content)) {
    return (
      <React.Suspense fallback={<PlainMarkdown content={content} />}>
        <LazyMathMarkdown content={content} />
      </React.Suspense>
    );
  }
  return <PlainMarkdown content={content} />;
}
```

`MathMessageMarkdown.tsx` 才允许 import `remark-math`、`rehype-katex` 和 `katex/dist/katex.min.css`。`MermaidBlock.tsx` 才允许 import `mermaid`。`CodeBlock.tsx` 才允许 import `react-syntax-highlighter`。执行 agent 必须用 bundle report 验证这些包没有进入 initial chunk。

```tsx
// App.tsx 或路由层
const WorkspacePanel = React.lazy(() =>
  import('./components/workspace/WorkspacePanel').then((m) => ({ default: m.WorkspacePanel }))
);
const NativeTerminalPanel = React.lazy(() =>
  import('./components/native/NativeTerminalPanel').then((m) => ({ default: m.NativeTerminalPanel }))
);
```

当前这些组件是 named export，不是 default export。要么按上面 `.then((m) => ({ default: m.X }))` 写，要么在组件文件里显式增加 default export。不要直接 `React.lazy(() => import(...))`，那会在运行时失败。

`NativeTerminalPanel` 现在由 `ChatHeader.tsx` 和 `NativeRuntimeLauncher.tsx` 间接打开。完整拆包时要替换这些文件里的直接 import，而不是只在 `App.tsx` 写一个 lazy 常量。`ArtifactsPanel` 在 Phase 4 创建，Phase 1 不要提前引用不存在的组件。

#### 2.3.3 预取策略

用户开始收到 assistant 消息时，预加载 SyntaxHighlighter：

```tsx
// 在首次 SSE 事件到达时
import('react-syntax-highlighter/dist/esm/prism');
```

### 2.4 App.tsx 瘦身

重构完成后 App.tsx 目标结构（200-300 行）：

```tsx
export default function App() {
  useBootstrap();
  useSessionRestore();

  const hostedChatEnabled = isHostedChatEnabled(useBootstrapStore(s => s.capabilities));
  const nativeLauncherMode = !hostedChatEnabled;

  return (
    <div className="app-shell">
      <ChatSidebar />
      <main>
        <ChatHeader />
        {nativeLauncherMode ? <NativeRuntimeLauncher /> : (
          <>
            <ChatMessageList />
            <ChatComposer />
          </>
        )}
      </main>
      <AttachmentPreview />
      <React.Suspense fallback={null}>
        <WorkspacePanel />
        <ArtifactsPanel />
      </React.Suspense>
    </div>
  );
}
```

---

## 3 渲染能力增强

### 3.1 HTML/JS 沙箱预览

#### 3.1.1 安全模型（修正版）

CSP 不再自相矛盾：

```ts
// src/utils/sandbox.ts
type SandboxedHtml = {
  html: string;
  channelId: string;
};

export function buildSandboxedHtml(
  sourceHtml: string,
  options?: { allowForms?: boolean; channelId?: string },
): SandboxedHtml {
  const channelId = options?.channelId ?? crypto.randomUUID();
  const csp = [
    "default-src 'none'",
    "script-src 'unsafe-inline' 'unsafe-eval'",     // 渲染 HTML 内联脚本必须
    "style-src 'unsafe-inline' data:",               // 内联样式 + 内嵌字体
    "img-src data: blob:",                            // 只允许内嵌资源，禁止 https: 外带
    "font-src data:",
    "media-src data: blob:",
    "connect-src 'none'",                             // 禁止一切网络请求
    options?.allowForms ? "form-action 'self'" : "form-action 'none'",
    "navigate-to 'none'",                              // 防御脚本主动导航；浏览器支持不完全，不能作为唯一保证
  ].filter(Boolean).join('; ');

  // 注入 CSP + 链接点击拦截脚本
  const interceptor = `<script>
document.addEventListener('click', function(e) {
  var target = e.target.closest('a');
  if (target && target.href) {
    e.preventDefault();
    window.parent.postMessage({
      type: 'ksadk:linkClick',
      channelId: ${JSON.stringify(channelId)},
      href: target.href,
      target: target.target || '_self'
    }, '*');
  }
}, true);
</script>`;

  const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${csp}">`;

  const html = /<head[^>]*>/i.test(sourceHtml)
    ? sourceHtml.replace(/<head[^>]*>/i, `$&${cspMeta}${interceptor}`)
    : `${cspMeta}${interceptor}${sourceHtml}`;

  return { html, channelId };
}
```

**关键修正**：
- `img-src data: blob:` — 不再允许 `https:`，堵住图片外带数据通道
- `form-action` 只能生成一条 directive：默认 `'none'`，信任模式才是 `'self'`
- 链接拦截通过 srcdoc 注入脚本 + `postMessage`，**不依赖父页面访问 iframe DOM**（默认无 `allow-same-origin` 时跨源访问不可靠）
- 父页面通过 `window.addEventListener('message', ...)` 接收链接点击，必须校验 `event.source` 和 `channelId`
- `frame-ancestors` 不写在 meta CSP 中；它通常只在 HTTP header CSP 中生效，不要把它当成这里的安全保证
- `navigate-to 'none'` 只是防御补充；脚本主动设置 `location.href` 的兼容性要通过 PoC 验证，不能承诺所有 iframe 内导航都能被拦截

#### 3.1.2 父页面消息处理

```ts
// ArtifactsPanel.tsx 或 CodeBlock.tsx
const iframeRef = useRef<HTMLIFrameElement | null>(null);
const trustedMode = Boolean(settings?.iframeSandboxTrusted);
const sandboxed = useMemo(
  () => buildSandboxedHtml(content, { allowForms: trustedMode }),
  [content, trustedMode],
);
const sandboxValue = trustedMode
  ? 'allow-scripts allow-downloads allow-forms allow-same-origin'
  : 'allow-scripts allow-downloads';

useEffect(() => {
  const handler = (e: MessageEvent) => {
    if (e.source !== iframeRef.current?.contentWindow) return;
    if (e.data?.type === 'ksadk:linkClick' && e.data.channelId === sandboxed.channelId && e.data.href) {
      const url = new URL(e.data.href, window.location.origin);
      window.open(url.href, '_blank', 'noopener,noreferrer');
    }
  };
  window.addEventListener('message', handler);
  return () => window.removeEventListener('message', handler);
}, [sandboxed.channelId]);

return <iframe ref={iframeRef} sandbox={sandboxValue} srcDoc={sandboxed.html} />;
```

注意：sandbox srcdoc 的 `event.origin` 可能是 `"null"`，不能只靠 origin 判断。必须校验 `event.source === iframe.contentWindow`，再校验注入脚本带回来的 `channelId`。

#### 3.1.3 iframe sandbox 权限矩阵

| 权限 | 默认 | 管理员信任模式 | 风险 |
|------|------|---------------|------|
| allow-scripts | 是 | 是 | 执行 JS 是渲染 HTML 的核心需求 |
| allow-downloads | 是 | 是 | 允许下载生成的文件 |
| allow-forms | 否 | 是 | 表单提交可能被滥用外带数据 |
| allow-same-origin | 否 | 是（需管理员显式开启） | 高危，允许 iframe 访问父页面 Cookie/Storage |
| allow-popups | 否 | 否 | 防止弹窗 |

#### 3.1.4 安全 PoC 验收清单（Phase 0 前置）

在正式做 Artifacts 之前，先用一个最小测试页验证安全模型：

- [ ] sandbox 无 `allow-same-origin` 时，父页面 `iframe.contentWindow.document` 抛异常
- [ ] 注入脚本 `postMessage` 能传递链接点击，父页面能收到
- [ ] CSP 下 `fetch()`、`XMLHttpRequest`、`navigator.sendBeacon()` 均被拦截
- [ ] CSP 下 `<img src="https://evil.com/...">` 被拦截（不发出请求）
- [ ] CSP 下 `location.href = 'https://evil.com'` 不会让顶层页面跳转；如果 iframe 自身发生导航，UI 有重新预览/关闭预览的处理
- [ ] `data:` / `blob:` 图片正常显示
- [ ] 父页面只接受来自当前 iframe window 的 message（校验 `event.source`）

### 3.2 Workspace 文件编辑

#### 3.2.1 前置确认（Phase 3 开始前必须回答）

| 问题 | 需确认方 | 默认假设（如未确认） |
|------|---------|---------------------|
| AddWorkspaceFile 同路径是覆盖还是失败？ | 后端 | 覆盖（先按覆盖实现，如后端返回错误再改） |
| share 链接是否完全只读？ | 平台 | 是，share 不可写 |
| private/owner 模式写权限如何判断？ | 平台 | owner 可写，private 需校验 |
| 保存大文件有没有 size limit？ | 后端 | 沿用 MaxUploadBytes |
| 是否允许路径穿越（如 `../etc/passwd`）？ | 后端 | 不允许，后端已做路径归一化 |
| HTML preview 用编辑器当前内容还是保存后的服务端内容？ | 产品 | 编辑器当前内容（实时感更强，保存后也同步） |

#### 3.2.2 CodeMirror 正确引入

参考 Open WebUI 的 CodeMirror 模式，但适配 React：

```bash
npm install codemirror @codemirror/view @codemirror/state \
  @codemirror/language-data @codemirror/theme-one-dark \
  @codemirror/commands @codemirror/language @codemirror/search \
  @codemirror/autocomplete
```

```tsx
// src/components/workspace/FileEditor.tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import { EditorView, keymap } from '@codemirror/view';
import { Compartment, EditorState, type Extension } from '@codemirror/state';
import { basicSetup } from 'codemirror';
import { oneDark } from '@codemirror/theme-one-dark';
import { indentWithTab } from '@codemirror/commands';
import { LanguageDescription } from '@codemirror/language';
import { languages } from '@codemirror/language-data';

type FileEditorProps = {
  content: string;
  filePath: string;
  readOnly?: boolean;
  onSave?: (content: string) => Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
};

export function FileEditor({ content, filePath, readOnly = false, onSave, onDirtyChange }: FileEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const languageCompartmentRef = useRef(new Compartment());
  const themeCompartmentRef = useRef(new Compartment());
  const readOnlyCompartmentRef = useRef(new Compartment());
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  const applyLanguage = useCallback(async (path: string) => {
    const view = viewRef.current;
    if (!view) return;
    const extension = await loadLanguageExtension(path);
    if (viewRef.current !== view) return; // 文件切换时丢弃过期 async 结果
    view.dispatch({
      effects: languageCompartmentRef.current.reconfigure(extension),
    });
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;
    const isDark = document.documentElement.classList.contains('dark');
    setDirty(false);

    viewRef.current = new EditorView({
      state: EditorState.create({
        doc: content,
        extensions: [
          basicSetup,
          keymap.of([indentWithTab]),
          languageCompartmentRef.current.of([]),
          themeCompartmentRef.current.of(isDark ? oneDark : []),
          readOnlyCompartmentRef.current.of(EditorState.readOnly.of(readOnly)),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) setDirty(true);
          }),
          EditorView.theme({
            '&': { fontSize: '13.5px', height: '100%' },
            '.cm-scroller': { overflow: 'auto' },
          }),
        ],
      }),
      parent: containerRef.current,
    });
    void applyLanguage(filePath);
    return () => { viewRef.current?.destroy(); viewRef.current = null; };
  }, [content, filePath, applyLanguage]); // 切换文件或重新加载内容时重建

  // 语言切换
  useEffect(() => {
    void applyLanguage(filePath);
  }, [filePath, applyLanguage]);

  // readOnly 切换
  useEffect(() => {
    if (!viewRef.current) return;
    viewRef.current.dispatch({
      effects: readOnlyCompartmentRef.current.reconfigure(EditorState.readOnly.of(readOnly)),
    });
  }, [readOnly]);

  // 主题切换：监听 html.dark 变化，不要只在 mount 时读一次
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const isDark = document.documentElement.classList.contains('dark');
      viewRef.current?.dispatch({
        effects: themeCompartmentRef.current.reconfigure(isDark ? oneDark : []),
      });
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);

  const handleSave = async () => {
    if (!viewRef.current || !onSave) return;
    setSaving(true);
    try {
      await onSave(viewRef.current.state.doc.toString());
      setDirty(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="relative h-full">
      <div ref={containerRef} className="h-full" />
      {!readOnly && dirty && (
        <div className="absolute bottom-4 right-4 flex gap-2">
          <button type="button" onClick={() => void handleSave()} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      )}
    </div>
  );
}

async function loadLanguageExtension(filePathOrLang: string): Promise<Extension> {
  const description =
    LanguageDescription.matchFilename(languages, filePathOrLang)
    ?? LanguageDescription.matchLanguageName(languages, filePathOrLang, true);
  if (!description) return [];
  try {
    return await description.load();
  } catch (error) {
    console.warn('Failed to load CodeMirror language extension:', filePathOrLang, error);
    return [];
  }
}
```

执行注意：

- `loadLanguageExtension()` 是 async，不能直接塞进 `Compartment.of(...)`。初始化先放 `[]`，加载完成后 `reconfigure()`。
- `Compartment` 必须是每个 editor 实例自己的 ref，不要在模块顶层共享，否则多个编辑器会互相影响。
- 保存失败时不要清 dirty；只有 `onSave()` resolve 成功后才能 `setDirty(false)`。
- Phase 3 不做自动保存。用户切换文件、关闭面板、刷新目录时，如果 dirty 为 true，必须弹确认。

#### 3.2.3 保存流程

```
用户点击保存
  → 从 EditorView 获取当前文档内容
  → POST /agentengine/api/v1/AddWorkspaceFile
    body: FormData { file(Blob), AgentId, Path }
  → 成功：刷新目录列表，标记 dirty=false
  → 失败：toast 错误消息，保持 dirty=true
```

#### 3.2.4 HTML 实时预览

编辑 HTML 文件时，右侧自动出现 iframe 预览，300ms 防抖刷新：

```tsx
useEffect(() => {
  if (previewKind !== 'html') return;
  const timer = setTimeout(() => {
    setPreviewContent(currentContent);
  }, 300);
  return () => clearTimeout(timer);
}, [currentContent]);
```

#### 3.2.5 Workspace 交互分步交付

| 步骤 | 交付内容 | 不做的事 |
|------|---------|---------|
| 1 | 只读预览保持稳定，抽出 FilePreview 组件 | — |
| 2 | 文本文件支持编辑、dirty 标记、保存、未保存确认 | 不做自动保存 |
| 3 | HTML 文件加 split preview，300ms debounce | 不做复杂 diff、多人协作 |

---

## 4 视觉体系

### 4.1 设计原则

Hosted UI 是 **agent 开发/调试工作台**，不是消费级聊天产品。视觉方案要服务调试效率：

- user 消息：右侧气泡化，视觉突出"这是我的输入"
- assistant 回复：保持文档式阅读区，不套气泡，长 markdown/代码/tool trace 需要宽屏可读性
- tool call / reasoning / approval：做更清晰的状态组件（图标 + 颜色 + 折叠）
- 空状态：少量 prompt suggestion，不做过重的营销 hero

### 4.2 设计 Token 定制

```css
:root {
  --primary: 215 78% 52%;
  --primary-foreground: 0 0% 100%;
  --accent: 215 78% 95%;
  --accent-foreground: 215 78% 25%;
  --radius: 0.75rem;
  --background: 210 20% 99%;
  --foreground: 215 25% 15%;
  --muted: 210 15% 96%;
  --border: 210 12% 90%;
}

.dark {
  --primary: 215 70% 58%;
  --background: 220 20% 8%;
  --foreground: 210 15% 95%;
}
```

### 4.3 消息样式

```tsx
// user：右侧气泡
<div className="flex justify-end mb-4">
  <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-blue-600 text-white
    px-4 py-3 text-[15px] leading-relaxed">
    {content}
  </div>
</div>

// assistant：文档式阅读区，不套气泡
<div className="mb-4 max-w-none">
  <div className="flex items-center gap-2 mb-2 text-xs text-slate-400">
    <Bot className="w-3.5 h-3.5" />
    <span>{agentName}</span>
  </div>
  <MessageMarkdown content={content} />
</div>
```

### 4.4 中文排版

```css
/* 注意：Tailwind typography 的 --tw-prose-body 是复合属性，
   直接赋 15px / 1.8 不会按预期生效。
   正确做法是用 Tailwind 的 prose 工具类覆盖： */

.prose {
  font-size: 15px;
  line-height: 1.8;
  hanging-punctuation: first last;
}

.prose code {
  font-size: 13.5px;
  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
}

.prose p {
  margin-bottom: 0.75em;
}
```

### 4.5 空状态

```tsx
<div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
  <div className="mb-6 h-16 w-16 rounded-2xl bg-gradient-to-br from-blue-500 to-indigo-600
    flex items-center justify-center shadow-lg shadow-blue-500/20">
    <Bot className="h-8 w-8 text-white" />
  </div>
  <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
    有什么我可以帮您的吗？
  </h2>
  <p className="mt-2 text-sm text-slate-500">
    我是 {agentName}，由 Ksyun AgentEngine 驱动
  </p>
  <div className="mt-6 grid grid-cols-2 gap-2.5 sm:grid-cols-3">
    {suggestions.slice(0, 6).map(s => (
      <button key={s} onClick={() => setInput(s)}
        className="rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm text-slate-600
          hover:bg-slate-50 hover:border-slate-300 transition dark:border-slate-800
          dark:text-slate-300 dark:hover:bg-slate-900">
        {s}
      </button>
    ))}
  </div>
</div>
```

---

## 5 构建规范

### 5.1 统一双构建

```json
{
  "scripts": {
    "build:all": "npm run build && npm run build:hosted"
  }
}
```

### 5.2 构建产物处理（两个独立决策）

**决策 A：CI/build hook 自动构建并打包前端**（先做）

- Makefile 增加 `build-frontend` target
- CI pipeline：`make build-frontend && uv build`
- 验证：`make build-wheel` 产出的 wheel 包含完整前端产物

**决策 B：是否从 git 删除 static/ 和 dist-hosted/**（后评估）

- 前提：决策 A 已稳定运行至少一个发布周期
- 评估维度：离线构建需求、紧急 hotfix 流程、团队成员是否都有 Node 环境
- `.gitignore` 更新放在决策 B 确认后，不要和决策 A 混在一个 PR

### 5.3 双构建同步 CI 校验

```bash
# CI 中校验两份产物来自同一次源码构建
dist_time=$(stat -f %m dist/index.html 2>/dev/null || stat -c %Y dist/index.html)
hosted_time=$(stat -f %m dist-hosted/index.html 2>/dev/null || stat -c %Y dist-hosted/index.html)
diff=$((hosted_time - dist_time))
if [ "$diff" -lt 0 ]; then diff=$((-diff)); fi
if [ "$diff" -gt 60 ]; then
  echo "FAIL: dist and dist-hosted timestamps differ by ${diff}s (>60s)"
  exit 1
fi
```

### 5.4 bundle 分析开关

`rollup-plugin-visualizer` 只在分析模式启用，不要污染普通构建产物。

建议实现方式：

```ts
// vite.config.ts
import { visualizer } from 'rollup-plugin-visualizer';

const analyze = process.env.ANALYZE === '1';

export default defineConfig({
  plugins: [
    react(),
    analyze ? visualizer({
      filename: 'dist/stats.html',
      gzipSize: true,
      brotliSize: true,
      template: 'treemap',
    }) : null,
  ].filter(Boolean),
});
```

普通构建：`npm run build`。  
分析构建：`ANALYZE=1 npm run build`。  
CI 可以把 `dist/stats.html` 或 `dist-hosted/stats.html` 作为 artifact 上传，但不要提交到 git。

---

## 6 实施计划

### Phase 0：护栏和基线（1 人天）

目标：不改业务行为，只建立后续重构的验证地板。Phase 0 不允许做 store、API 迁移、UI 改版。

| 产出 | 说明 |
|------|------|
| `npm run build:all` 稳定 | 在 `ksadk/server/web-ui/package.json` 增加 `build:all`，本地两份产物均可构建 |
| 回归清单 | 在本文或单独 checklist 中列出 SSE 流式输出、session restore、upload、workspace CRUD、feedback、approval、terminal 打开/关闭 |
| bundle size 报告 | 安装 `rollup-plugin-visualizer`，用 `ANALYZE=1` 记录 initial JS gzip、async chunks total、CSS gzip 基线 |
| 安全 PoC 测试页 | 最小验证 sandbox/CSP/postMessage 模型，不接入正式 UI |

建议命令：

```bash
cd ksadk/server/web-ui
npm install
npm run build
npm run build:hosted
ANALYZE=1 npm run build
```

Phase 0 验收：

- [ ] `npm run build` 成功。
- [ ] `npm run build:hosted` 成功。
- [ ] `npm run build:all` 成功。
- [ ] `ANALYZE=1 npm run build` 生成 bundle report，但 report 文件未被 git 跟踪。
- [ ] 记录当前 initial JS gzip、main entry gzip、CSS gzip。
- [ ] 安全 PoC 能证明 `<img https:>`、`fetch`、XHR、`sendBeacon` 被 CSP 拦截。
- [ ] 安全 PoC 能证明父页面只接收当前 iframe 的 `postMessage`。

### Phase 1：API 层 + 错误模型 + Markdown 拆包（3 人天）

目标：减少 `App.tsx` 内联 fetch 和首屏重依赖。Phase 1 不做 Zustand 大迁移，不做 Workspace 编辑，不做正式 ArtifactsPanel。

| 天 | 任务 | 产出 |
|----|------|------|
| 1 | 创建 `errors.ts` + `client.ts`（4 类通信原语）+ `session.ts`，替换 App.tsx 中 session 域 fetch | PR: api-errors-session |
| 2 | 创建 `bootstrap.ts` + `model.ts` + `workspace.ts` + `feedback.ts` + `terminal.ts`，替换对应域 fetch | PR: api-all-domains |
| 3 | MessageMarkdown 拆包：lazy mermaid / syntax-highlighter / katex；WorkspacePanel / NativeTerminalPanel React.lazy | PR: markdown-code-split |

Phase 1 实现要点：

- `client.ts` 必须包含 `postJsonAction`、`postFormAction`、`getResource`、`streamAction` 四类原语。
- `streamAction()` 只返回 stream，不负责读完整 SSE；`useRunAgent()` 负责 SSE 中途断流错误。
- `CancelledError` 不能被 toast 成错误。
- `MessageMarkdown.tsx` 不允许直接 import `mermaid`、`react-syntax-highlighter`、`rehype-katex`、`katex/dist/katex.min.css`。
- `MathMessageMarkdown.tsx`、`MermaidBlock.tsx`、`CodeBlock.tsx` 分别承载重依赖。
- `ArtifactsPanel` 还不存在，不要在 Phase 1 引入不存在的组件。

验收标准（可测预算，不喊口号）：
- [ ] mermaid 不出现在 initial chunk
- [ ] react-syntax-highlighter 不出现在 initial chunk
- [ ] katex 不出现在无公式路径的 initial chunk
- [ ] terminal chunk 只在打开 terminal 时加载
- [ ] initial JS gzip 明确低于 Phase 0 基线
- [ ] UI 层不再直接判断 `data?.Code`，统一 catch `ApiError`
- [ ] `CancelledError` 对用户主动停止/切换不弹错误 toast
- [ ] `npm run build:all` 成功

### Phase 2：Store + App.tsx 拆分（3 人天）

目标：拆出清晰的数据流和 shell 结构，但保持行为不变。Phase 2 不做 Workspace 编辑、不做 HTML artifact 预览、不做视觉改版。

| 天 | 任务 | 产出 |
|----|------|------|
| 1 | 创建 `stores/ui.ts` + `stores/bootstrap.ts` + `stores/model.ts`，迁移对应状态 | PR: store-ui-bootstrap-model |
| 2 | 创建 `stores/session.ts` + `stores/message.ts`，迁移；创建 `useRunAgent()` hook/reducer | PR: store-session-message-run |
| 3 | 创建 `stores/streaming.ts`（只放 UI 共享读状态）；按需创建 `stores/workspace.ts`；拆分 App shell 子组件 | PR: store-streaming + app-split |

streaming store 边界再强调：
- **store 里放**：`isStreaming`、`currentRunId`、`stopRequested`
- **hook/reducer 里放**：`AbortController`、SSE reader、增量事件 buffer、resume cursor、queue 消费
- **message store**：只接收 normalize 后的 message patch

Phase 2 拆分建议：

```
src/hooks/
  useBootstrap.ts
  useSessionRestore.ts
  useRunAgent.ts

src/stores/
  bootstrap.ts
  model.ts
  session.ts
  message.ts
  streaming.ts
  ui.ts

src/components/shell/
  AppShell.tsx
  MainPane.tsx
```

`stores/artifact.ts` 留到 Phase 4 创建。`stores/workspace.ts` 只有在 workspace 状态确实被面板外共享时才创建。

验收标准：
- [ ] App.tsx < 400 行
- [ ] 无 prop drilling 超过 2 层
- [ ] `useRunAgent` 有单测
- [ ] SSE 流式中途取消不产生陈旧闭包错误
- [ ] 创建新会话、切换会话、恢复会话、删除会话行为与 Phase 0 回归清单一致
- [ ] `npm run build:all` 成功

### Phase 3：Workspace 编辑（3 人天）

前置：确认 3.2.1 中 6 个权限/语义问题。

目标：让 workspace 文本文件可编辑、可保存、可预览。Phase 3 不做 ArtifactsPanel，不做 message 代码块 HTML 预览。

| 天 | 任务 | 产出 |
|----|------|------|
| 1 | 抽出 FilePreview 组件，保持只读预览稳定 | PR: file-preview-extract |
| 2 | 新增 FileEditor（CodeMirror），支持编辑、dirty 标记、保存、未保存确认 | PR: workspace-file-editor |
| 3 | HTML 文件 split preview + 300ms debounce | PR: workspace-html-preview |

Phase 3 实现要点：

- `FilePreview` 抽出后，图片/PDF/Markdown/text 的现有预览行为不能变。
- CodeMirror 只在进入编辑能力时加载；不要放进 initial chunk。
- 保存使用 `workspaceApi.addFile()`，FormData 中 file 用 `new Blob([content], { type: mimeType || 'text/plain' })` 构造。
- 保存失败保持 dirty=true，并显示错误。
- share 链接默认不可编辑。权限不确定时只读，不要猜测放开写入。
- HTML split preview 使用编辑器当前内容，不要求先保存。

验收标准：
- [ ] .py/.js/.ts/.html/.json/.yaml/.md 文件可编辑保存
- [ ] 保存后刷新目录列表，dirty 状态清零
- [ ] 未保存时切换文件或关闭面板有确认提示
- [ ] HTML 文件编辑后右侧 iframe 实时刷新
- [ ] 非文本文件（图片、PDF）仍走原预览逻辑
- [ ] CodeMirror 相关 chunk 不进入 initial chunk
- [ ] `npm run build:all` 成功

### Phase 4：HTML/Artifact 安全预览（2 人天）

前置：Phase 0 或 Phase 1 完成安全 PoC 验收清单。

目标：给 message 代码块和 artifact 提供安全预览能力。Phase 4 不做 Workspace 编辑扩展，不做 Pyodide/代码执行。

| 天 | 任务 | 产出 |
|----|------|------|
| 1 | CodeBlock 添加 HTML/SVG 预览按钮 + Dialog 内 iframe 渲染 | PR: html-codeblock-preview |
| 2 | 创建 `stores/artifact.ts` + ArtifactsPanel 侧栏预览 + 管理员信任模式 | PR: artifacts-panel |

Phase 4 实现要点：

- `buildSandboxedHtml()` 必须复用 Phase 0 PoC 验证过的安全模型。
- 父页面 message handler 必须校验 `event.source` 和 `channelId`。
- 管理员信任模式默认关闭；普通用户路径不出现 `allow-same-origin`。
- SVG 预览优先直接安全渲染或 iframe sandbox 渲染，不要无条件 `dangerouslySetInnerHTML` 未清洗 SVG。
- ArtifactsPanel 支持 HTML/SVG/下载源码即可，不做多文件项目运行环境。

验收标准：
- [ ] Phase 0 安全 PoC 清单全部通过
- [ ] HTML 代码块点击预览可看到渲染结果
- [ ] JS 可正常执行，但 `fetch`/`XHR`/`sendBeacon`/`<img https:>` 被拦截
- [ ] 外部链接点击在新窗口打开，不跳转 iframe
- [ ] 管理员信任模式（allow-same-origin + allow-forms）可配置
- [ ] 父页面不接受其他 iframe/window 伪造的 `ksadk:linkClick`
- [ ] `npm run build:all` 成功

### Phase 5：视觉和构建规范（3 人天）

目标：改善开发工作台观感，并自动构建前端进 wheel。Phase 5 不删除 git 中已跟踪构建产物，除非另有明确批准。

| 天 | 任务 | 产出 |
|----|------|------|
| 1 | CSS 变量品牌化 + user 气泡 + assistant 文档式 + 中文排版 | PR: visual-brand |
| 2 | 空状态重设计 + 快捷提示词 + 暗色/移动端适配 | PR: visual-polish |
| 3 | 决策 A：Makefile/build hook 集成 + CI 全链路测试 | PR: build-automation |

决策 B（git 移除产物）：待决策 A 稳定一个发布周期后再评估，不在本 Phase 范围内。

Phase 5 验收：

- [ ] user 消息为右侧气泡，assistant 保持文档式阅读区。
- [ ] 长 markdown、代码块、tool trace 不被窄气泡压缩。
- [ ] 375px 移动宽度下 Header、Composer、Workspace sheet 不重叠。
- [ ] `make build-frontend` 或等价 target 能构建 local + hosted 产物。
- [ ] wheel 构建后包含前端产物。
- [ ] 未删除已跟踪的 `static/` / `dist-hosted/` 产物。
- [ ] `npm run build:all` 成功。

### Phase 6：Store 收敛评估（1 人天）

目标：回看前 5 个 Phase 的状态边界，删除空抽象，避免长期维护负担。

| 任务 | 说明 |
|------|------|
| 评估 hook/reducer 中的状态 | 是否有状态确实跨组件共享了（如 `isStreaming` 已在 store、`currentRunId` 也已共享） |
| 评估 reducer 复杂度 | `useRunAgent` 是否过于臃肿，是否值得拆成 sub-reducers |
| 收敛决策 | 只把确实跨组件共享的状态迁入 store，其余保持 hook 局部管理 |

Phase 6 验收：

- [ ] 没有空 store、空 hook、只转发 props 的无意义组件。
- [ ] `useRunAgent` 如果超过约 300 行，拆出 SSE parser、message patch reducer、approval continuation helper。
- [ ] 文档和代码实际边界一致。
- [ ] `npm run build:all` 成功。

---

## 7 风险与应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| Zustand 迁移引入状态不一致 | 中 | 高 | 每个 store 迁移后对照回归清单验证；streaming 瞬态保持 hook/reducer 隔离 |
| CodeMirror 语言动态加载失败 | 低 | 低 | 降级到 plain text + console.warn |
| iframe sandbox 过严导致部分 HTML 无法渲染 | 中 | 中 | 管理员信任模式可开启 allow-same-origin + allow-forms；默认安全 |
| 构建流程变更导致 pip 包缺前端产物 | 低 | 高 | CI `make build-wheel` 全链路测试；决策 A/B 分步执行 |
| CSP 阻止合法 CDN 资源 | 低 | 低 | style-src 可按需放开 cdn.jsdelivr.net；默认不放开 |

---

## 8 依赖新增

| 包 | 用途 | 体积（gzip） | 加载方式 |
|-----|------|-------------|---------|
| zustand | 状态管理 | ~2KB | 首屏 |
| codemirror + @codemirror/view + @codemirror/state + @codemirror/language-data + @codemirror/theme-one-dark + @codemirror/commands + @codemirror/language + @codemirror/search + @codemirror/autocomplete | Workspace 文件编辑 | ~60KB（按需加载语言） | lazy |
| rollup-plugin-visualizer | bundle 分析 | 0（devDep） | 仅 `ANALYZE=1` |

react-syntax-highlighter 将在 Phase 1 代码分割后改为 lazy load，不再是首屏依赖。

---

## 9 附录

### 9.1 当前文件体积基线

| 文件 | 行数 | 说明 |
|------|------|------|
| `App.tsx` | 1768 | 主组件，需拆分 |
| `ChatMessageList.tsx` | 555 | 消息列表 |
| `WorkspacePanel.tsx` | 758 | Workspace 面板 |
| `NativeTerminalPanel.tsx` | 545 | 终端面板 |
| `ChatHeader.tsx` | 354 | 头部控制栏 |
| `ChatComposer.tsx` | 228 | 输入框 |
| `MessageMarkdown.tsx` | 158 | Markdown 渲染 |
| `responses-stream.js` | 289 | SSE 事件归一化 |
| `session-events.js` | 416 | 事件→消息转换 |
| `markdown.js` | 236 | Markdown 修复管线 |
| **总计** | **8157** | 35 个源文件 |

### 9.2 bundle size 基线（Phase 0 记录）

> 以下数值需在 Phase 0 执行时填入实际测量值。

| 指标 | 当前值 | 目标 |
|------|--------|------|
| main entry gzip | 410 KB | < 200KB |
| initial JS gzip total | 410 KB | < 300KB |
| async chunks gzip total | 1,076 KB | 按需 |
| CSS gzip | 23 KB | < 50KB |

Phase 0 实测基线（2026-05-20）：main entry `index-CY8FjGwe.js` raw 1,241 KB / gzip 410 KB。CSS `index-Be25eA04.css` gzip 23 KB。异步 chunk 共 gzip 1,076 KB（含 mermaid/cytoscape/xterm/katex 等）。
