import MarkdownIt from "https://esm.sh/markdown-it@14.1.0";
import markdownItTaskLists from "https://esm.sh/markdown-it-task-lists@2.1.1";
import hljs from "https://esm.sh/highlight.js@11.10.0";

const BUILTIN_COMMANDS = {
  "/new": "创建一个新的本地会话",
  "/clear": "仅清空当前消息视图",
  "/stop": "停止当前生成",
  "/help": "查看所有快捷命令",
  "/attach": "打开附件选择器",
};

const LAST_SESSION_STORAGE_KEY = "agentengine-workbench:last-session";
const COMPOSER_DRAFT_STORAGE_KEY = "agentengine-workbench:composer-draft";
const REASONING_CACHE_STORAGE_KEY = "agentengine-workbench:reasoning-cache";
const REASONING_PANEL_STORAGE_KEY = "agentengine-workbench:reasoning-panel";
const THINKING_ENABLED_STORAGE_KEY = "agentengine-workbench:thinking-enabled";
const DEFAULT_THINKING_SUMMARY = "当前 Agent 的思考过程会在这里独立展示，并缓存最近一次结果到本地浏览器。";
const PLACEHOLDER_THINKING_HTML = '<p class="thinking-placeholder">等待模型输出思考内容...</p>';
const FILE_SIZE_LIMIT_BYTES = 50 * 1024 * 1024;
const INLINE_ATTACHMENT_LIMIT_BYTES = 4 * 1024 * 1024;
const ATTACHMENT_METADATA_ONLY_EXTENSIONS = new Set();
const ATTACHMENT_METADATA_ONLY_MIME_PREFIXES = [];

const state = {
  bootstrap: null,
  sessions: [],
  sessionId: null,
  events: [],
  pendingFiles: [],
  responseEl: null,
  abortController: null,
  commandIndex: 0,
  visibleCommands: [],
  contextSessionId: null,
  restoredSessionId: null,
  reasoningText: "",
  toolRuns: new Map(),
  toolSequence: 0,
  reasoningPanelExpanded: readBooleanStorage(REASONING_PANEL_STORAGE_KEY, true),
  thinkingEnabled: readBooleanStorage(THINKING_ENABLED_STORAGE_KEY, true),
};

const dom = {
  agentName: document.getElementById("agent-name"),
  agentMeta: document.getElementById("agent-meta"),
  agentCapabilities: document.getElementById("agent-capabilities"),
  activeSessionLabel: document.getElementById("active-session-label"),
  thinkingVisibilityToggle: document.getElementById("thinking-visibility-toggle"),
  sessionResumeBanner: document.getElementById("session-resume-banner"),
  sessionList: document.getElementById("session-list"),
  thinkingStage: document.getElementById("thinking-stage"),
  thinkingTitle: document.getElementById("thinking-title"),
  thinkingSummary: document.getElementById("thinking-summary"),
  thinkingContent: document.getElementById("thinking-content"),
  thinkingToggle: document.getElementById("thinking-toggle"),
  messageList: document.getElementById("message-list"),
  composerForm: document.getElementById("composer-form"),
  messageInput: document.getElementById("message-input"),
  stopButton: document.getElementById("stop-button"),
  newSessionButton: document.getElementById("new-session-button"),
  attachmentInput: document.getElementById("attachment-input"),
  attachmentList: document.getElementById("attachment-list"),
  composerStatus: document.getElementById("composer-status"),
  commandMenu: document.getElementById("command-menu"),
  sessionContextMenu: document.getElementById("session-context-menu"),
};

function readStorage(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Ignore storage failures in embedded or private contexts.
  }
}

function removeStorage(key) {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore storage failures in embedded or private contexts.
  }
}

function safeJsonParse(text, fallback) {
  if (!text) {
    return fallback;
  }
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function readBooleanStorage(key, fallback) {
  const value = readStorage(key);
  if (value === null) {
    return fallback;
  }
  return value !== "0";
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeAttribute(text) {
  return escapeHtml(text)
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderHighlightedCode(codeText, language) {
  try {
    if (language && hljs.getLanguage(language)) {
      return {
        html: hljs.highlight(codeText, { language }).value,
        language,
      };
    }
    const auto = hljs.highlightAuto(codeText);
    return {
      html: auto.value || escapeHtml(codeText),
      language: auto.language || language || "text",
    };
  } catch {
    return {
      html: escapeHtml(codeText),
      language: language || "text",
    };
  }
}

const markdown = MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(code, language) {
    const highlighted = renderHighlightedCode(code, language || "");
    return `
      <div class="code-block">
        <div class="code-block-header">
          <span class="code-block-language">${escapeHtml(highlighted.language)}</span>
          <span class="code-block-meta">code</span>
        </div>
        <pre class="hljs language-${escapeAttribute(highlighted.language)}"><code>${highlighted.html}</code></pre>
      </div>
    `;
  },
}).use(markdownItTaskLists, { enabled: true });

const defaultLinkOpenRenderer =
  markdown.renderer.rules.link_open ||
  ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options));

markdown.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  const hrefIndex = token.attrIndex("href");
  if (hrefIndex >= 0) {
    token.attrSet("target", "_blank");
    token.attrSet("rel", "noreferrer");
  }
  return defaultLinkOpenRenderer(tokens, idx, options, env, self);
};

function sanitizeUrl(url) {
  try {
    const resolved = new URL(String(url || "").trim(), window.location.origin);
    if (["http:", "https:", "mailto:", "tel:"].includes(resolved.protocol)) {
      return resolved.href;
    }
  } catch {
    // Ignore malformed URLs and fall back to inert anchor.
  }
  return "#";
}

function toDate(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  if (typeof value === "number") {
    const millis = value > 1e12 ? value : value * 1000;
    return new Date(millis);
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateTime(value, options = {}) {
  const date = toDate(value);
  if (!date) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: options.withYear ? "numeric" : undefined,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function shortSessionId(sessionId) {
  return String(sessionId || "").slice(0, 12);
}

function formatBytes(size) {
  if (!Number.isFinite(size) || size <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  let value = size;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const digits = value >= 10 || unitIndex === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unitIndex]}`;
}

function fileExtension(name) {
  const index = String(name || "").lastIndexOf(".");
  return index >= 0 ? String(name).slice(index).toLowerCase() : "";
}

function shouldSendAttachmentAsMetadataOnly(file) {
  const extension = fileExtension(file.name);
  return (
    ATTACHMENT_METADATA_ONLY_EXTENSIONS.has(extension) ||
    ATTACHMENT_METADATA_ONLY_MIME_PREFIXES.some((prefix) => (file.type || "").startsWith(prefix)) ||
    file.size > INLINE_ATTACHMENT_LIMIT_BYTES
  );
}

function attachmentModeLabel(file) {
  return shouldSendAttachmentAsMetadataOnly(file) ? "仅元信息" : "原始文件";
}

function metadataOnlyWarning(files) {
  const limitedFiles = files.filter((file) => shouldSendAttachmentAsMetadataOnly(file));
  if (!limitedFiles.length) {
    return "";
  }
  return `以下附件超过 ${formatBytes(INLINE_ATTACHMENT_LIMIT_BYTES)}，本次仅传递文件信息：${limitedFiles.map((file) => file.name).join("、")}`;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(`读取文件失败：${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function buildAttachmentPart(file) {
  if (shouldSendAttachmentAsMetadataOnly(file)) {
    return {
      type: "input_file",
      fileData: {
        displayName: file.name,
        mimeType: file.type || "application/octet-stream",
        fileUri: "",
      },
    };
  }

  const dataUrl = await readFileAsDataUrl(file);
  const [, base64Payload = ""] = dataUrl.split(",", 2);
  return {
    type: "input_file",
    inlineData: {
      displayName: file.name,
      mimeType: file.type || "application/octet-stream",
      data: base64Payload,
    },
  };
}

async function buildMessageContent(text, files) {
  const parts = [];
  if (text) {
    parts.push({ type: "input_text", text });
  }
  for (const file of files) {
    parts.push(await buildAttachmentPart(file));
  }
  return parts;
}

function scrollMessageListToEnd() {
  requestAnimationFrame(() => {
    dom.messageList.scrollTop = dom.messageList.scrollHeight;
  });
}

function buildConversationPreviewText(text, files) {
  const attachmentBlock = files.length ? `## 附件\n${files.map((file) => `- ${file.name}`).join("\n")}` : "";
  return [text.trim(), attachmentBlock].filter(Boolean).join("\n\n").trim();
}

function normalizeMarkdownSource(text) {
  let source = String(text || "").replace(/\r\n/g, "\n");
  const fenceCount = (source.match(/^```/gm) || []).length;
  if (fenceCount % 2 === 1) {
    source = `${source}\n\`\`\``;
  }
  return source;
}

function renderInlineMarkdown(text) {
  let source = String(text || "");
  const tokens = [];

  source = source.replace(/`([^`]+)`/g, (_, code) => {
    const index = tokens.push(`<code>${escapeHtml(code)}</code>`) - 1;
    return `%%TOKEN_${index}%%`;
  });

  source = escapeHtml(source);

  source = source.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const href = sanitizeUrl(url);
    return `<a href="${escapeAttribute(href)}" target="_blank" rel="noreferrer">${label}</a>`;
  });
  source = source.replace(/(\*\*|__)(.+?)\1/g, "<strong>$2</strong>");
  source = source.replace(/(\*|_)([^*_]+?)\1/g, "<em>$2</em>");
  source = source.replace(/~~(.+?)~~/g, "<del>$1</del>");

  return source.replace(/%%TOKEN_(\d+)%%/g, (_, index) => tokens[Number(index)] || "");
}

function isThematicBreak(line) {
  return /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line.trim());
}

function isTableSeparator(line) {
  return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(line);
}

function isTableHeader(line, nextLine) {
  return line.trim().includes("|") && isTableSeparator(nextLine || "");
}

function isListItem(line) {
  return /^\s*[-*+]\s+/.test(line);
}

function isOrderedListItem(line) {
  return /^\s*\d+\.\s+/.test(line);
}

function isSpecialBlock(line, nextLine = "") {
  return (
    /^#{1,6}\s+/.test(line) ||
    isThematicBreak(line) ||
    line.trim().startsWith(">") ||
    isListItem(line) ||
    isOrderedListItem(line) ||
    isTableHeader(line, nextLine)
  );
}

function splitTableCells(line) {
  let row = line.trim();
  if (row.startsWith("|")) {
    row = row.slice(1);
  }
  if (row.endsWith("|")) {
    row = row.slice(0, -1);
  }
  return row.split("|").map((cell) => cell.trim());
}

function renderTableBlock(lines, startIndex) {
  const headers = splitTableCells(lines[startIndex]).map(renderInlineMarkdown);
  const rows = [];
  let index = startIndex + 2;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim() || !line.includes("|")) {
      break;
    }
    rows.push(splitTableCells(line).map(renderInlineMarkdown));
    index += 1;
  }

  const head = `<thead><tr>${headers.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead>`;
  const body = rows.length
    ? `<tbody>${rows
        .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
        .join("")}</tbody>`
    : "";

  return {
    html: `<table>${head}${body}</table>`,
    nextIndex: index,
  };
}

function renderMarkdownTextBlocks(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    const nextLine = lines[index + 1] || "";

    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (/^#{1,6}\s*(.+)$/.test(line)) {
      const level = Math.min(6, line.match(/^#+/)[0].length);
      const content = line.replace(/^#{1,6}\s*/, "");
      blocks.push(`<h${level}>${renderInlineMarkdown(content)}</h${level}>`);
      index += 1;
      continue;
    }

    if (isThematicBreak(line)) {
      blocks.push("<hr>");
      index += 1;
      continue;
    }

    if (line.trim().startsWith(">")) {
      const quoteLines = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quoteBody = quoteLines.map((item) => renderInlineMarkdown(item)).join("<br>");
      blocks.push(`<blockquote><p>${quoteBody}</p></blockquote>`);
      continue;
    }

    if (isTableHeader(line, nextLine)) {
      const rendered = renderTableBlock(lines, index);
      blocks.push(rendered.html);
      index = rendered.nextIndex;
      continue;
    }

    if (isListItem(line)) {
      const items = [];
      while (index < lines.length && isListItem(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ""));
        index += 1;
      }
      blocks.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }

    if (isOrderedListItem(line)) {
      const items = [];
      while (index < lines.length && isOrderedListItem(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, ""));
        index += 1;
      }
      blocks.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !isSpecialBlock(lines[index], lines[index + 1] || "")) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push(`<p>${paragraph.map((item) => renderInlineMarkdown(item)).join("<br>")}</p>`);
  }

  return blocks.join("");
}

function renderMarkdownBlocks(text) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized.split("\n");
  const blocks = [];
  const textBuffer = [];
  const codeBuffer = [];
  let codeLanguage = "code";
  let inCodeBlock = false;

  const flushText = () => {
    const textContent = textBuffer.join("\n");
    if (textContent.trim()) {
      blocks.push(renderMarkdownTextBlocks(textContent));
    }
    textBuffer.length = 0;
  };

  const flushCode = () => {
    const code = escapeHtml(codeBuffer.join("\n").trimEnd());
    blocks.push(
      `<pre><div class="code-header"><span>${escapeHtml(codeLanguage || "code")}</span><span>code</span></div><code>${code}</code></pre>`,
    );
    codeBuffer.length = 0;
    codeLanguage = "code";
  };

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex];
    if (line.trimStart().startsWith("```")) {
      const hasClosingFenceAhead = lines
        .slice(lineIndex + 1)
        .some((candidate) => candidate.trimStart().startsWith("```"));

      if (!inCodeBlock && !hasClosingFenceAhead) {
        textBuffer.push(line);
        continue;
      }

      if (!inCodeBlock) {
        flushText();
        inCodeBlock = true;
        codeLanguage = line.trimStart().slice(3).trim() || "code";
      } else {
        inCodeBlock = false;
        flushCode();
      }
      continue;
    }

    if (inCodeBlock) {
      codeBuffer.push(line);
    } else {
      textBuffer.push(line);
    }
  }

  if (inCodeBlock) {
    flushCode();
  } else {
    flushText();
  }

  return blocks.join("");
}

function renderMarkdown(text) {
  const source = normalizeMarkdownSource(text);
  return markdown.render(source);
}

function setMarkdownContent(element, text) {
  element.innerHTML = renderMarkdown(text);
}

function eventText(event) {
  const content = event.Content || {};
  const parts = content.parts || content.Parts || [];
  return parts
    .map((part) => part.text || part.Text || "")
    .filter(Boolean)
    .join("");
}

function bubbleRoleLabel(role) {
  if (role === "user") {
    return "用户";
  }
  if (role === "assistant") {
    return "Assistant";
  }
  return role;
}

function formatEventMeta(event) {
  const pieces = [];
  if (event.Timestamp) {
    pieces.push(formatDateTime(event.Timestamp));
  }
  if (event.EventType && event.EventType !== "text") {
    pieces.push(event.EventType);
  }
  return pieces.join(" · ");
}

function capabilityLabel(key) {
  const labels = {
    Attachments: "附件上传",
    Approval: "人工审批",
    Thinking: "思考流",
    StopRun: "停止生成",
  };
  return labels[key] || key;
}

function updateActiveSessionLabel(sessionId) {
  if (!sessionId) {
    dom.activeSessionLabel.textContent = "等待选择会话";
    return;
  }
  dom.activeSessionLabel.textContent = `当前会话 · ${shortSessionId(sessionId)}`;
}

function showSessionResumeBanner(sessionId) {
  const session = state.sessions.find((item) => item.SessionId === sessionId);
  const timeText = session ? formatDateTime(session.UpdatedAt) : "刚刚";
  dom.sessionResumeBanner.textContent = `已恢复上次会话，可直接继续聊天 · ${shortSessionId(sessionId)} · ${timeText}`;
  dom.sessionResumeBanner.classList.remove("hidden");
}

function hideSessionResumeBanner() {
  dom.sessionResumeBanner.textContent = "";
  dom.sessionResumeBanner.classList.add("hidden");
}

function readReasoningCache() {
  return safeJsonParse(readStorage(REASONING_CACHE_STORAGE_KEY), {});
}

function writeReasoningCache(nextCache) {
  const entries = Object.entries(nextCache)
    .sort(([, left], [, right]) => (right?.updatedAt || 0) - (left?.updatedAt || 0))
    .slice(0, 12);
  writeStorage(REASONING_CACHE_STORAGE_KEY, JSON.stringify(Object.fromEntries(entries)));
}

function persistReasoningCache(sessionId, text) {
  if (!sessionId) {
    return;
  }
  const cache = readReasoningCache();
  if (!text) {
    delete cache[sessionId];
  } else {
    cache[sessionId] = {
      text,
      updatedAt: Date.now(),
    };
  }
  writeReasoningCache(cache);
}

function reasoningCacheForSession(sessionId) {
  const cache = readReasoningCache();
  return cache[sessionId] || null;
}

function applyThinkingPanelVisibility() {
  const shouldShowStage = state.thinkingEnabled && (!dom.thinkingStage.classList.contains("hidden") || state.reasoningText);
  dom.thinkingStage.classList.toggle("hidden", !shouldShowStage);
  dom.thinkingStage.classList.toggle("collapsed", !state.reasoningPanelExpanded);
  dom.thinkingToggle.textContent = state.reasoningPanelExpanded ? "收起" : "展开";
  dom.thinkingVisibilityToggle.textContent = state.thinkingEnabled ? "思考：开" : "思考：关";
  writeStorage(REASONING_PANEL_STORAGE_KEY, state.reasoningPanelExpanded ? "1" : "0");
  writeStorage(THINKING_ENABLED_STORAGE_KEY, state.thinkingEnabled ? "1" : "0");
}

function showThinkingStage({ title, summary, text }) {
  dom.thinkingTitle.textContent = title || "实时思考流";
  dom.thinkingSummary.textContent = summary || DEFAULT_THINKING_SUMMARY;
  if (text) {
    setMarkdownContent(dom.thinkingContent, text);
  } else {
    dom.thinkingContent.innerHTML = PLACEHOLDER_THINKING_HTML;
  }
  if (!state.thinkingEnabled) {
    dom.thinkingStage.classList.add("hidden");
    applyThinkingPanelVisibility();
    return;
  }
  dom.thinkingStage.classList.remove("hidden");
  applyThinkingPanelVisibility();
}

function hideThinkingStage() {
  dom.thinkingStage.classList.add("hidden");
  dom.thinkingContent.innerHTML = "";
  dom.thinkingSummary.textContent = DEFAULT_THINKING_SUMMARY;
  state.reasoningText = "";
}

function restoreThinkingStage(sessionId) {
  const cached = reasoningCacheForSession(sessionId);
  if (!cached?.text) {
    hideThinkingStage();
    return;
  }
  state.reasoningText = cached.text;
  showThinkingStage({
    title: "最近一次思考记录",
    summary: `来自浏览器本地缓存 · ${formatDateTime(cached.updatedAt)}`,
    text: cached.text,
  });
}

function resetStreamingStage() {
  state.reasoningText = "";
  showThinkingStage({
    title: "实时思考流",
    summary: "正在等待模型输出思考内容，结束后会缓存最近一次结果到本地浏览器。",
    text: "",
  });
}

async function postAction(actionName, payload = {}) {
  const response = await fetch(`/agentengine/api/v1/${actionName}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok || !data || data.Code !== 0) {
    throw new Error(data?.Message || data?.detail || `Action ${actionName} failed`);
  }
  return data.Data;
}

function setStatus(text = "", hidden = false) {
  dom.composerStatus.textContent = text;
  dom.composerStatus.classList.toggle("hidden", hidden || !text);
}

function normalizeCommandList() {
  const configured = state.bootstrap?.Capabilities?.SlashCommands || [];
  const commands = new Map(
    Object.entries(BUILTIN_COMMANDS).map(([command, description]) => [
      command,
      { command, description },
    ]),
  );

  for (const item of configured) {
    if (typeof item === "string") {
      commands.set(item, {
        command: item,
        description: BUILTIN_COMMANDS[item] || "执行命令",
      });
    } else if (item?.Command) {
      commands.set(item.Command, {
        command: item.Command,
        description: item.Description || BUILTIN_COMMANDS[item.Command] || "执行命令",
      });
    }
  }
  return Array.from(commands.values());
}

function appendBubble({ role, text, meta = "", emphasis = "" }) {
  if (dom.messageList.classList.contains("is-empty") || dom.messageList.querySelector(".empty-state")) {
    dom.messageList.innerHTML = "";
  }
  dom.messageList.classList.remove("is-empty");
  const wrapper = document.createElement("article");
  wrapper.className = `bubble ${role}${emphasis ? ` ${emphasis}` : ""}`;
  wrapper.innerHTML = `
    <div class="bubble-header">
      <div class="bubble-role">${bubbleRoleLabel(role)}</div>
      <div class="event-meta">${escapeHtml(meta)}</div>
    </div>
    <div class="bubble-content markdown-body"></div>
  `;
  setMarkdownContent(wrapper.querySelector(".bubble-content"), text);
  dom.messageList.appendChild(wrapper);
  scrollMessageListToEnd();
  return wrapper;
}

function stringifyToolPayload(value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function resetToolRuns() {
  state.toolRuns = new Map();
  state.toolSequence = 0;
}

function resolveToolRunId(payload, { createIfMissing = false } = {}) {
  if (payload?.run_id) {
    return String(payload.run_id);
  }

  const toolName = String(payload?.name || "");
  const unresolved = Array.from(state.toolRuns.values())
    .reverse()
    .find((entry) => entry.name === toolName && !entry.completed);
  if (unresolved) {
    return unresolved.id;
  }

  if (createIfMissing) {
    return `tool-${state.toolSequence += 1}`;
  }

  return "";
}

function appendToolRun(payload) {
  const id = resolveToolRunId(payload, { createIfMissing: true });
  if (state.toolRuns.has(id)) {
    return state.toolRuns.get(id);
  }

  if (dom.messageList.classList.contains("is-empty") || dom.messageList.querySelector(".empty-state")) {
    dom.messageList.innerHTML = "";
    dom.messageList.classList.remove("is-empty");
  }

  const toolName = String(payload?.name || "tool");
  const argsText = stringifyToolPayload(payload?.args);
  const wrapper = document.createElement("article");
  wrapper.className = "bubble tool";
  wrapper.innerHTML = `
    <div class="bubble-header">
      <div class="bubble-role">工具流程</div>
      <div class="event-meta">执行中</div>
    </div>
    <div class="tool-run">
      <div class="tool-summary">
        <div class="tool-name">${escapeHtml(toolName)}</div>
        <span class="tool-badge running">执行中</span>
      </div>
      ${
        argsText
          ? `<div class="tool-section"><div class="tool-label">输入参数</div><pre><code>${escapeHtml(argsText)}</code></pre></div>`
          : ""
      }
      <div class="tool-section tool-output hidden">
        <div class="tool-label">工具输出</div>
        <pre><code></code></pre>
      </div>
    </div>
  `;
  dom.messageList.appendChild(wrapper);
  scrollMessageListToEnd();

  const entry = {
    id,
    name: toolName,
    completed: false,
    wrapper,
    meta: wrapper.querySelector(".event-meta"),
    badge: wrapper.querySelector(".tool-badge"),
    outputSection: wrapper.querySelector(".tool-output"),
    outputCode: wrapper.querySelector(".tool-output code"),
  };
  state.toolRuns.set(id, entry);
  return entry;
}

function updateToolRunStatus(entry, statusText, statusClass) {
  if (!entry) {
    return;
  }
  entry.meta.textContent = statusText;
  entry.badge.textContent = statusText;
  entry.badge.className = `tool-badge ${statusClass}`;
  entry.completed = statusClass !== "running";
}

function resolveToolRun(payload) {
  const id = resolveToolRunId(payload) || resolveToolRunId(payload, { createIfMissing: true });
  const entry = state.toolRuns.get(id) || appendToolRun(payload);
  const outputText = stringifyToolPayload(payload?.output);
  if (outputText) {
    entry.outputSection.classList.remove("hidden");
    entry.outputCode.textContent = outputText;
  }
  updateToolRunStatus(entry, "已完成", "completed");
  scrollMessageListToEnd();
}

function settleToolRuns(statusText, statusClass) {
  for (const entry of state.toolRuns.values()) {
    if (!entry.completed) {
      updateToolRunStatus(entry, statusText, statusClass);
    }
  }
}

function buildLocalEvent({ author, role, text, timestamp = Date.now() }) {
  return {
    Author: author,
    EventType: "text",
    Timestamp: timestamp,
    Content: {
      role,
      parts: [{ text }],
    },
  };
}

function renderCapabilities() {
  const capabilities = state.bootstrap?.Capabilities || {};
  const chips = [
    capabilities.Attachments ? capabilityLabel("Attachments") : null,
    capabilities.Approval ? capabilityLabel("Approval") : null,
    capabilities.Thinking ? capabilityLabel("Thinking") : null,
    capabilities.StopRun ? capabilityLabel("StopRun") : null,
  ].filter(Boolean);

  dom.agentCapabilities.innerHTML = chips
    .map((chip) => `<span class="capability-chip">${escapeHtml(chip)}</span>`)
    .join("");
}

function renderSessions() {
  dom.sessionList.innerHTML = "";
  if (!state.sessions.length) {
    dom.sessionList.innerHTML = '<div class="empty-state">还没有本地会话。<br>点击上方按钮即可开始一段新的对话。</div>';
    return;
  }

  for (const session of state.sessions) {
    const isRestored = session.SessionId === state.restoredSessionId;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item${session.SessionId === state.sessionId ? " active" : ""}`;
    button.innerHTML = `
      <div class="session-row">
        <div class="session-title">${escapeHtml(shortSessionId(session.SessionId))}</div>
        ${isRestored ? '<span class="session-badge">已恢复</span>' : ""}
      </div>
      <div class="session-meta">更新于 ${formatDateTime(session.UpdatedAt)}</div>
    `;
    button.addEventListener("click", () => openSession(session.SessionId));
    button.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      showSessionContextMenu(session.SessionId, event.clientX, event.clientY);
    });
    dom.sessionList.appendChild(button);
  }
}

function renderEvents() {
  dom.messageList.innerHTML = "";
  resetToolRuns();
  if (!state.events.length) {
    dom.messageList.classList.add("is-empty");
    dom.messageList.innerHTML = '<div class="empty-state">从左侧恢复一段历史会话，或直接开始新的聊天。</div>';
    return;
  }
  dom.messageList.classList.remove("is-empty");
  for (const event of state.events) {
    const text = eventText(event);
    if (!text) {
      continue;
    }
    appendBubble({
      role: event.Author === "user" ? "user" : "assistant",
      text,
      meta: formatEventMeta(event),
    });
  }
}

async function loadBootstrap() {
  state.bootstrap = await postAction("GetAgentUiBootstrap", {});
  dom.agentName.textContent = state.bootstrap.Agent.Name;
  const agentType = state.bootstrap.Agent.Type || "Agent";
  const accessMode = state.bootstrap.AccessMode === "Owner" ? "所有者访问" : String(state.bootstrap.AccessMode || "共享访问");
  dom.agentMeta.textContent = `${agentType} · ${accessMode}`;
  renderCapabilities();
}

async function refreshSessions() {
  state.sessions =
    (
      await postAction("ListSessions", {
        AgentId: state.bootstrap.Agent.AgentId,
        UserId: "user",
      })
    ).Sessions || [];
  renderSessions();
}

function persistLastSession(sessionId) {
  if (!sessionId) {
    removeStorage(LAST_SESSION_STORAGE_KEY);
    return;
  }
  writeStorage(LAST_SESSION_STORAGE_KEY, sessionId);
}

function restoreComposerDraft() {
  const draft = readStorage(COMPOSER_DRAFT_STORAGE_KEY);
  if (draft) {
    dom.messageInput.value = draft;
  }
  autoResizeComposer();
}

function persistComposerDraft() {
  const value = dom.messageInput.value;
  if (!value.trim()) {
    removeStorage(COMPOSER_DRAFT_STORAGE_KEY);
    return;
  }
  writeStorage(COMPOSER_DRAFT_STORAGE_KEY, value);
}

function clearComposerDraft() {
  removeStorage(COMPOSER_DRAFT_STORAGE_KEY);
}

async function createSession() {
  const data = await postAction("CreateSession", {
    AgentId: state.bootstrap.Agent.AgentId,
    UserId: "user",
  });
  state.restoredSessionId = null;
  state.sessionId = data.Session.SessionId;
  state.events = [];
  state.responseEl = null;
  resetToolRuns();
  persistLastSession(state.sessionId);
  updateActiveSessionLabel(state.sessionId);
  hideSessionResumeBanner();
  hideThinkingStage();
  renderEvents();
  await refreshSessions();
}

async function openSession(sessionId, options = {}) {
  hideSessionContextMenu();
  state.sessionId = sessionId;
  state.responseEl = null;
  resetToolRuns();
  persistLastSession(sessionId);
  updateActiveSessionLabel(sessionId);

  const data = await postAction("ListSessionEvents", { SessionId: sessionId });
  state.events = data.Events || [];
  renderSessions();
  renderEvents();
  restoreThinkingStage(sessionId);

  if (options.restored) {
    state.restoredSessionId = sessionId;
    showSessionResumeBanner(sessionId);
  } else if (!options.keepBanner) {
    hideSessionResumeBanner();
  }

  scrollMessageListToEnd();
}

async function deleteSession(sessionId) {
  if (!sessionId) {
    return;
  }

  await postAction("DeleteSession", { SessionId: sessionId });
  const nextSessions = state.sessions.filter((item) => item.SessionId !== sessionId);

  if (state.restoredSessionId === sessionId) {
    state.restoredSessionId = null;
  }

  if (state.sessionId === sessionId) {
    if (nextSessions[0]) {
      await openSession(nextSessions[0].SessionId);
    } else {
      state.sessionId = null;
      state.events = [];
      state.responseEl = null;
      persistLastSession(null);
      updateActiveSessionLabel(null);
      hideSessionResumeBanner();
      hideThinkingStage();
      renderEvents();
    }
  }

  await refreshSessions();
}

function clearPendingFiles() {
  state.pendingFiles = [];
  dom.attachmentList.innerHTML = "";
  dom.attachmentInput.value = "";
}

function showPendingFiles() {
  dom.attachmentList.innerHTML = "";
  for (const file of state.pendingFiles) {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    chip.textContent = `${file.name} · ${formatBytes(file.size)} · ${attachmentModeLabel(file)}`;
    dom.attachmentList.appendChild(chip);
  }
}

function autoResizeComposer() {
  dom.messageInput.style.height = "auto";
  dom.messageInput.style.height = `${Math.min(dom.messageInput.scrollHeight, 240)}px`;
}

function hideCommandMenu() {
  state.visibleCommands = [];
  state.commandIndex = 0;
  dom.commandMenu.innerHTML = "";
  dom.commandMenu.classList.add("hidden");
}

function hideSessionContextMenu() {
  state.contextSessionId = null;
  dom.sessionContextMenu.innerHTML = "";
  dom.sessionContextMenu.classList.add("hidden");
}

function showSessionContextMenu(sessionId, clientX, clientY) {
  state.contextSessionId = sessionId;
  dom.sessionContextMenu.innerHTML = `
    <button type="button" class="session-menu-item danger" data-action="delete">
      删除本地会话
    </button>
  `;

  const left = Math.min(clientX, window.innerWidth - 204);
  const top = Math.min(clientY, window.innerHeight - 84);
  dom.sessionContextMenu.style.left = `${Math.max(12, left)}px`;
  dom.sessionContextMenu.style.top = `${Math.max(12, top)}px`;
  dom.sessionContextMenu.classList.remove("hidden");

  for (const item of dom.sessionContextMenu.querySelectorAll(".session-menu-item")) {
    item.addEventListener("click", async () => {
      if (item.dataset.action === "delete") {
        await deleteSession(state.contextSessionId);
      }
      hideSessionContextMenu();
    });
  }
}

function renderCommandMenu() {
  const rawValue = dom.messageInput.value.trimStart();
  if (!rawValue.startsWith("/")) {
    hideCommandMenu();
    return;
  }

  const query = rawValue.toLowerCase();
  const commands = normalizeCommandList().filter((item) => item.command.toLowerCase().startsWith(query));
  if (!commands.length) {
    hideCommandMenu();
    return;
  }

  state.visibleCommands = commands;
  state.commandIndex = Math.min(state.commandIndex, commands.length - 1);
  dom.commandMenu.innerHTML = commands
    .map(
      (item, index) => `
        <button
          type="button"
          class="command-option${index === state.commandIndex ? " active" : ""}"
          data-command="${escapeAttribute(item.command)}"
        >
          <span class="command-name">${escapeHtml(item.command)}</span>
          <span class="command-description">${escapeHtml(item.description)}</span>
        </button>
      `,
    )
    .join("");
  dom.commandMenu.classList.remove("hidden");

  for (const option of dom.commandMenu.querySelectorAll(".command-option")) {
    option.addEventListener("click", () => {
      applyCommand(option.dataset.command || "");
    });
  }
}

function applyCommand(command) {
  dom.messageInput.value = command;
  autoResizeComposer();
  renderCommandMenu();
  persistComposerDraft();
  dom.messageInput.focus();
}

async function handleCommand(text) {
  if (text === "/new") {
    await createSession();
    dom.messageInput.value = "";
    clearComposerDraft();
    hideCommandMenu();
    autoResizeComposer();
    return true;
  }

  if (text === "/clear") {
    dom.messageList.innerHTML = "";
    hideThinkingStage();
    dom.messageInput.value = "";
    clearComposerDraft();
    hideCommandMenu();
    autoResizeComposer();
    return true;
  }

  if (text === "/help") {
    appendBubble({
      role: "assistant",
      text: normalizeCommandList()
        .map((item) => `- \`${item.command}\`：${item.description}`)
        .join("\n"),
      meta: "快捷命令",
    });
    dom.messageInput.value = "";
    clearComposerDraft();
    hideCommandMenu();
    autoResizeComposer();
    return true;
  }

  if (text === "/stop") {
    if (state.abortController) {
      state.abortController.abort();
    }
    dom.messageInput.value = "";
    clearComposerDraft();
    hideCommandMenu();
    autoResizeComposer();
    return true;
  }

  if (text === "/attach") {
    dom.attachmentInput.click();
    dom.messageInput.value = "";
    clearComposerDraft();
    hideCommandMenu();
    autoResizeComposer();
    return true;
  }

  return false;
}

async function extractErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const payload = await response.json().catch(() => null);
    return payload?.detail || payload?.Message || "请求失败";
  }
  const text = await response.text().catch(() => "");
  return text || `请求失败，状态码 ${response.status}`;
}

function extractReasoningDelta(payload) {
  if (!payload) {
    return "";
  }
  if (typeof payload.delta === "string") {
    return payload.delta;
  }
  if (Array.isArray(payload.delta)) {
    return payload.delta.map((item) => extractReasoningDelta(item)).join("");
  }
  if (payload.delta && typeof payload.delta === "object") {
    if (typeof payload.delta.reasoning_content === "string") {
      return payload.delta.reasoning_content;
    }
    if (typeof payload.delta.text === "string") {
      return payload.delta.text;
    }
  }
  if (typeof payload.reasoning_content === "string") {
    return payload.reasoning_content;
  }
  if (typeof payload.reasoning === "string") {
    return payload.reasoning;
  }
  if (Array.isArray(payload.content)) {
    return payload.content
      .map((item) => item.reasoning_content || item.text || "")
      .filter(Boolean)
      .join("");
  }
  return "";
}

function extractOutputDelta(payload) {
  if (!payload) {
    return "";
  }
  if (typeof payload.delta === "string") {
    return payload.delta;
  }
  if (Array.isArray(payload.delta)) {
    return payload.delta.map((item) => extractOutputDelta(item)).join("");
  }
  if (payload.delta && typeof payload.delta === "object") {
    if (typeof payload.delta.output_text === "string") {
      return payload.delta.output_text;
    }
    if (typeof payload.delta.text === "string") {
      return payload.delta.text;
    }
  }
  if (typeof payload.output_text === "string") {
    return payload.output_text;
  }
  if (Array.isArray(payload.content)) {
    return payload.content
      .map((item) => item.text || "")
      .filter(Boolean)
      .join("");
  }
  return "";
}

function clearPendingBubbles() {
  if (state.responseEl && !state.responseEl.querySelector(".bubble-content")?.textContent?.trim()) {
    state.responseEl.remove();
  }
  state.responseEl = null;
  if (!dom.messageList.children.length) {
    dom.messageList.classList.add("is-empty");
  }
  if (!state.reasoningText) {
    hideThinkingStage();
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (!state.sessionId) {
    await createSession();
  }

  const text = dom.messageInput.value.trim();
  if (!text && !state.pendingFiles.length) {
    return;
  }

  if (text.startsWith("/") && !(await handleCommand(text))) {
    setStatus(`未知命令：${text}`, false);
    return;
  }

  const finalText = buildConversationPreviewText(text, state.pendingFiles);
  const requestContent = await buildMessageContent(text, state.pendingFiles);
  const eventTimestamp = Date.now();

  appendBubble({
    role: "user",
    text: finalText,
    meta: formatDateTime(eventTimestamp),
  });
  state.events.push(
    buildLocalEvent({
      author: "user",
      role: "user",
      text: finalText,
      timestamp: eventTimestamp,
    }),
  );
  setStatus("正在流式生成回复...", false);

  dom.messageInput.value = "";
  hideCommandMenu();
  clearComposerDraft();
  clearPendingFiles();
  autoResizeComposer();
  resetStreamingStage();

  state.responseEl = appendBubble({
    role: "assistant",
    text: "",
    meta: "生成中",
  });

  const responseContent = state.responseEl.querySelector(".bubble-content");
  state.abortController = new AbortController();
  dom.stopButton.disabled = false;

  const response = await fetch("/agentengine/api/v1/RunAgent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      AgentId: state.bootstrap.Agent.AgentId,
      SessionId: state.sessionId,
      ApiFormat: "responses",
      Stream: true,
      Messages: [{ role: "user", content: requestContent }],
    }),
    signal: state.abortController.signal,
  });

  if (!response.ok) {
    throw new Error(await extractErrorMessage(response));
  }
  if (!response.body) {
    throw new Error("流式响应为空");
  }

  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  let currentEvent = "";
  let currentDataLines = [];
  let responseText = "";

  const flushEvent = () => {
    if (!currentEvent) {
      return;
    }

    const rawData = currentDataLines.join("\n");
    const payload = rawData ? safeJsonParse(rawData, { delta: rawData }) : {};

    if (currentEvent === "response.reasoning.delta") {
      const delta = extractReasoningDelta(payload);
      if (delta) {
        state.reasoningText += delta;
        showThinkingStage({
          title: "实时思考流",
          summary: "正在接收模型思考内容，结束后会缓存最近一次结果到本地浏览器。",
          text: state.reasoningText,
        });
      }
    } else if (currentEvent === "response.output_text.delta") {
      const delta = extractOutputDelta(payload);
      if (delta) {
        responseText += delta;
        setMarkdownContent(responseContent, responseText);
        scrollMessageListToEnd();
      }
    } else if (currentEvent === "response.tool_call") {
      appendToolRun(payload);
    } else if (currentEvent === "response.tool_result") {
      resolveToolRun(payload);
    } else if (currentEvent === "response.approval_request") {
      settleToolRuns("等待审批", "paused");
      appendBubble({
        role: "assistant",
        text: "本次运行需要人工审批后才能继续。",
        meta: "审批中断",
      });
    } else if (currentEvent === "response.error") {
      const message = payload.message || "Agent 运行失败";
      setMarkdownContent(responseContent, `生成失败：${message}`);
      state.responseEl.classList.add("error");
      dom.thinkingSummary.textContent = "本次运行异常结束，已保留已收到的思考内容。";
      setStatus(message, false);
      settleToolRuns("执行失败", "error");
      scrollMessageListToEnd();
    } else if (currentEvent === "response.completed") {
      settleToolRuns("已完成", "completed");
      if (!state.reasoningText) {
        dom.thinkingSummary.textContent = "这次回复没有输出可见的思考内容。";
      } else {
        dom.thinkingSummary.textContent = "本次思考过程已完成，并缓存到本地浏览器。";
      }
    }

    currentEvent = "";
    currentDataLines = [];
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        currentDataLines.push(line.slice(6));
      } else if (!line.trim()) {
        flushEvent();
      }
    }

    if (done) {
      flushEvent();
      break;
    }
  }

  if (state.reasoningText) {
    persistReasoningCache(state.sessionId, state.reasoningText);
  }

  if (responseText.trim()) {
    state.events.push(
      buildLocalEvent({
        author: state.bootstrap.Agent.AgentId || "assistant",
        role: "assistant",
        text: responseText,
      }),
    );
  }

  dom.messageInput.value = "";
  autoResizeComposer();
  dom.stopButton.disabled = true;
  state.abortController = null;
  setStatus("", true);
  await refreshSessions();
}

async function bootstrap() {
  try {
    applyThinkingPanelVisibility();
    await loadBootstrap();
    await refreshSessions();
    restoreComposerDraft();

    if (!state.sessions.length) {
      await createSession();
    } else {
      const preferredSessionId = readStorage(LAST_SESSION_STORAGE_KEY);
      const existingSessionId = state.sessions.find((item) => item.SessionId === preferredSessionId)?.SessionId;
      const targetSessionId = existingSessionId || state.sessions[0].SessionId;
      const restored = Boolean(existingSessionId);
      if (restored) {
        state.restoredSessionId = targetSessionId;
      }
      await openSession(targetSessionId, { restored });
    }

    autoResizeComposer();
  } catch (error) {
    setStatus(error.message || String(error), false);
  }
}

dom.composerForm.addEventListener("submit", (event) => {
  sendMessage(event).catch((error) => {
    clearPendingBubbles();

    if (state.reasoningText) {
      persistReasoningCache(state.sessionId, state.reasoningText);
    }

    if (error.name === "AbortError") {
      setStatus("已停止生成。", false);
      if (!dom.thinkingStage.classList.contains("hidden")) {
        dom.thinkingSummary.textContent = "本次生成已被手动停止，已保留已收到的思考内容。";
      }
    } else {
      setStatus(error.message || String(error), false);
      if (state.responseEl) {
        state.responseEl.classList.add("error");
      }
    }

    dom.stopButton.disabled = true;
    state.abortController = null;
  });
});

dom.messageInput.addEventListener("keydown", (event) => {
  if (!dom.commandMenu.classList.contains("hidden")) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      state.commandIndex = (state.commandIndex + 1) % state.visibleCommands.length;
      renderCommandMenu();
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      state.commandIndex = (state.commandIndex - 1 + state.visibleCommands.length) % state.visibleCommands.length;
      renderCommandMenu();
      return;
    }

    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const selectedCommand = state.visibleCommands[state.commandIndex]?.command || dom.messageInput.value.trim();
      dom.messageInput.value = selectedCommand;
      hideCommandMenu();
      autoResizeComposer();
      persistComposerDraft();
      dom.composerForm.requestSubmit();
      return;
    }

    if (event.key === "Escape") {
      hideCommandMenu();
      return;
    }
  }

  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    dom.composerForm.requestSubmit();
  }
});

dom.messageInput.addEventListener("input", () => {
  autoResizeComposer();
  renderCommandMenu();
  persistComposerDraft();
});

dom.newSessionButton.addEventListener("click", () => {
  createSession().catch((error) => setStatus(error.message || String(error), false));
});

dom.stopButton.addEventListener("click", () => {
  if (state.abortController) {
    state.abortController.abort();
  }
});

dom.attachmentInput.addEventListener("change", (event) => {
  const selectedFiles = Array.from(event.target.files || []);
  const oversizedFiles = selectedFiles.filter((file) => file.size > FILE_SIZE_LIMIT_BYTES);
  state.pendingFiles = selectedFiles.filter((file) => file.size <= FILE_SIZE_LIMIT_BYTES);
  showPendingFiles();
  const warnings = [];
  if (oversizedFiles.length) {
    warnings.push(`已跳过超出 50MB 的附件：${oversizedFiles.map((file) => file.name).join("、")}`);
  }
  const metadataWarning = metadataOnlyWarning(state.pendingFiles);
  if (metadataWarning) {
    warnings.push(metadataWarning);
  }
  setStatus(warnings.join("；"), warnings.length === 0);
});

dom.thinkingToggle.addEventListener("click", () => {
  state.reasoningPanelExpanded = !state.reasoningPanelExpanded;
  applyThinkingPanelVisibility();
});

dom.thinkingVisibilityToggle.addEventListener("click", () => {
  state.thinkingEnabled = !state.thinkingEnabled;
  if (state.thinkingEnabled) {
    if (state.reasoningText) {
      showThinkingStage({
        title: "实时思考流",
        summary: "已重新开启思考展示。",
        text: state.reasoningText,
      });
    } else if (state.sessionId) {
      restoreThinkingStage(state.sessionId);
    } else {
      applyThinkingPanelVisibility();
    }
  } else {
    dom.thinkingStage.classList.add("hidden");
    applyThinkingPanelVisibility();
  }
});

document.addEventListener("click", (event) => {
  if (!dom.commandMenu.contains(event.target) && event.target !== dom.messageInput) {
    hideCommandMenu();
  }
  if (!dom.sessionContextMenu.contains(event.target)) {
    hideSessionContextMenu();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideCommandMenu();
    hideSessionContextMenu();
  }
});

bootstrap();
