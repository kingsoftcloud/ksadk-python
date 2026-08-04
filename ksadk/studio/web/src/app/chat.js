function setStatusBadge(node, status) {
  if (!node) return;
  const value = String(status || "IDLE");
  node.className = `status-badge ${value}`;
  node.textContent = value;
}

async function refreshChatModels(agentId) {
  const payload = await api(`/agents/${encodeURIComponent(agentId)}/models`);
  state.chatModels = payload.Models || [];
  const allowedIds = state.chatModels.map(item => String(item.id));
  const selected = allowedIds.includes(state.activeChatModel)
    ? state.activeChatModel
    : String(payload.Current || allowedIds[0] || "");
  state.activeChatModel = selected;
  $("chatModel").innerHTML = state.chatModels.length
    ? state.chatModels.map(item => `
      <option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name || item.displayName || item.id)}</option>
    `).join("")
    : '<option value="">未绑定模型</option>';
  $("chatModel").value = selected;
  $("chatModel").disabled = state.chatModels.length <= 1;
}

async function openChat(
  agentId = state.current?.draft?.metadata?.id,
  { sessionId = null } = {}
) {
  if (!agentId) {
    const first = state.agents[0];
    if (!first) {
      openCreate();
      return;
    }
    agentId = first.metadata.id;
  }
  const viewRevision = ++state.chatViewRevision;
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  if (viewRevision !== state.chatViewRevision) return;
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  clearChatRunInspector();
  renderGlobalContext();
  state.chatSessionId = sessionId;
  $("sendMessage").disabled = false;
  renderChatAgent();
  switchView("chat");
  await Promise.all([refreshRuns(), refreshChatModels(agentId)]);
  renderChatAgent();
  const requestedSessionExists = state.chatSessionId
    && groupedSessions().some(item => item.sessionId === state.chatSessionId);
  if (!requestedSessionExists) {
    state.chatSessionId = groupedSessions()[0]?.sessionId || null;
    renderSessionList();
  }
  renderMessages();
  const latest = agentRuns()
    .filter(run => run.sessionId === state.chatSessionId)
    .at(-1);
  if (latest) {
    state.activeRun = latest;
    await renderTrace(latest);
  }
  renderInvocation();
  syncBrowserRoute();
}

function renderChatAgent() {
  const draft = state.current?.draft;
  const name = draft?.metadata.name || "选择一个 Agent";
  const template = draft?.metadata?.labels?.["agentkit.ksyun.com/template"] || "blank";
  const research = template === "research";
  $("chatAgentLabel").textContent = name;
  $("conversationAgentName").textContent = name;
  $("conversationAgentMeta").textContent = draft
    ? `${draft.metadata.id} · revision ${draft.metadata.revision}`
    : "等待选择 Agent";
  $("conversationAgentAvatar").classList.toggle("research", research);
  $("conversationAgentAvatar").innerHTML = `<svg data-icon="${research ? "search" : "bot"}"></svg>`;
  $("chatEmptyTitle").textContent = research ? "开始一次深度调研" : "开始与 Agent 对话";
  $("chatEmptyDescription").textContent = research
    ? "发送研究问题后，Agent 会先规划任务，再结合绑定的 Skill、MCP 和 Tool 逐步执行。"
    : "发送一条消息，Agent 会根据系统提示词和已绑定能力执行任务。";
  $("chatSuggestions").innerHTML = research
    ? `
      <button data-suggestion="先把我的调研目标拆成问题树，并给出执行计划。" type="button">拆解问题并规划</button>
      <button data-suggestion="先检查当前可用来源，并说明可能的信息缺口。" type="button">检查来源与缺口</button>
      <button data-suggestion="按证据质量设计最终报告结构。" type="button">设计报告结构</button>
    `
    : `
      <button data-suggestion="先介绍你的职责、能力和工作边界。" type="button">查看 Agent 能力</button>
      <button data-suggestion="根据当前上下文给出一个清晰的执行计划。" type="button">生成执行计划</button>
      <button data-suggestion="列出完成任务还需要我提供的信息。" type="button">检查输入缺口</button>
    `;
  $("chatInput").placeholder = research
    ? "输入调研问题，Enter 发送，Shift + Enter 换行"
    : "输入消息，Enter 发送，Shift + Enter 换行";
  const bindings = draft?.spec?.bindings || {};
  $("inspectorAgentSummary").innerHTML = draft
    ? `
      <div><dt>Revision</dt><dd>r${draft.metadata.revision}</dd></div>
      <div><dt>Model</dt><dd>${escapeHtml(state.chatModels.length > 1 ? `${state.chatModels.length} 个已绑定` : draft.metadata.labels?.["agentkit.ksyun.com/model"] || resourceById(bindings.modelProfileId)?.displayName || "未选择")}</dd></div>
      <div><dt>Skill</dt><dd>${bindings.skills?.length || 0}</dd></div>
      <div><dt>MCP</dt><dd>${bindings.mcpServers?.length || 0}</dd></div>
      <div><dt>Tool</dt><dd>${bindings.tools?.length || 0}</dd></div>
    `
    : "";
  injectIcons($("view-chat"));
}

async function refreshRuns() {
  const payload = await api("/runs");
  state.runs = payload.items;
  renderSessionList();
  recoverActiveRuns();
}

function recoverActiveRuns() {
  state.runs
    .filter(run => run.status === "RUNNING" && !state.recoveringRunIds.has(run.id))
    .forEach(run => pollActiveRun(run.id));
}

async function pollActiveRun(runId) {
  state.recoveringRunIds.add(runId);
  try {
    while (true) {
      const run = await api(`/runs/${encodeURIComponent(runId)}`);
      const index = state.runs.findIndex(item => item.id === run.id);
      if (index >= 0) state.runs[index] = run;
      else state.runs.push(run);
      const visible = state.current?.draft?.metadata?.id === run.agentId
        && state.chatSessionId === run.sessionId;
      if (visible) {
        state.activeRun = run;
        renderSessionList();
        renderMessages();
        setStatusBadge($("inspectorStatus"), run.status);
      }
      if (run.status !== "RUNNING") {
        if (visible) await renderTrace(run);
        if (state.view === "observability") await refreshTraces();
        break;
      }
      await sleep(400);
    }
  } catch (error) {
    console.warn("恢复本地 Run 状态失败", runId, error);
  } finally {
    state.recoveringRunIds.delete(runId);
  }
}

function agentRuns() {
  const agentId = state.current?.draft?.metadata?.id;
  return state.runs.filter(run => !agentId || run.agentId === agentId);
}

function groupedSessions() {
  const groups = new Map();
  agentRuns().forEach(run => {
    if (!groups.has(run.sessionId)) groups.set(run.sessionId, []);
    groups.get(run.sessionId).push(run);
  });
  return [...groups.entries()]
    .map(([sessionId, runs]) => ({ sessionId, runs }))
    .sort((a, b) => {
      const aDate = new Date(a.runs.at(-1)?.startedAt || 0).getTime();
      const bDate = new Date(b.runs.at(-1)?.startedAt || 0).getTime();
      return bDate - aDate;
    });
}

function renderSessionList() {
  const sessions = groupedSessions();
  $("sessionList").innerHTML = sessions.length
    ? sessions.map(({ sessionId, runs }) => {
      const firstInput = runs[0]?.input || "新会话";
      const latest = runs.at(-1);
      return `
        <div class="session-item ${state.chatSessionId === sessionId ? "active" : ""}">
          <button class="session-main" data-session-id="${escapeHtml(sessionId)}" title="${escapeHtml(firstInput)}" type="button">
            <span class="session-status ${latest?.status === "RUNNING" ? "running" : ""}"></span>
            <strong>${escapeHtml(shortId(firstInput, 28))}</strong>
          </button>
          <time>${formatSessionTime(latest?.startedAt || latest?.completedAt)}</time>
          <button class="session-more" data-session-menu="${escapeHtml(sessionId)}" type="button" aria-label="会话操作" title="会话操作">•••</button>
          <div class="session-menu" data-session-menu-popover="${escapeHtml(sessionId)}" hidden>
            <button data-delete-session="${escapeHtml(sessionId)}" type="button">删除会话</button>
          </div>
        </div>
      `;
    }).join("")
    : '<div class="session-empty">当前 Agent 还没有会话</div>';
}

function formatSessionTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function openDeleteSession(sessionId) {
  state.pendingDeleteSessionId = sessionId;
  document.querySelectorAll("[data-session-menu-popover]").forEach(node => { node.hidden = true; });
  openOverlay("deleteSessionOverlay");
}

async function deleteSession() {
  const sessionId = state.pendingDeleteSessionId;
  if (!sessionId) return;
  const button = $("confirmDeleteSession");
  setButtonLoading(button, true, "正在删除");
  try {
    await api(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (state.chatSessionId === sessionId) {
      state.chatSessionId = null;
      state.activeRun = null;
    }
    state.pendingDeleteSessionId = null;
    closeOverlay("deleteSessionOverlay");
    await refreshRuns();
    if (!state.chatSessionId) {
      state.chatSessionId = groupedSessions()[0]?.sessionId || null;
    }
    renderMessages();
    renderInvocation();
    syncBrowserRoute();
    showToast("会话已删除", "相关 Run 与 Trace 已从本地工作区移除。");
  } catch (error) {
    showToast("删除失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function openDeleteAgent(agentId) {
  state.pendingDeleteAgentId = agentId;
  $("deleteAgentTitle").textContent = `删除 ${agentId}？`;
  $("deleteAgentDescription").textContent = "该 Agent 的 YAML、Build、Artifact、Run 与 Trace 将移入 .agentkit/trash，可从工作区手工恢复。";
  openOverlay("deleteAgentOverlay");
}

async function deleteAgent() {
  const agentId = state.pendingDeleteAgentId;
  if (!agentId) return;
  const button = $("confirmDeleteAgent");
  setButtonLoading(button, true, "正在删除");
  try {
    await api(`/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
    const deletingCurrent = state.current?.draft?.metadata?.id === agentId;
    if (deletingCurrent) {
      state.current = null;
      state.build = null;
      state.chatSessionId = null;
      state.activeRun = null;
      state.chatModels = [];
      state.activeChatModel = "";
      state.editingAgentId = null;
    }
    state.pendingDeleteAgentId = null;
    closeOverlay("deleteAgentOverlay");
    await Promise.all([refreshAgents(), refreshRuns()]);
    switchView("agents");
    showToast(
      "Agent 已删除",
      "YAML 与关联的本地构建、运行和 Trace 已移入回收站。"
    );
  } catch (error) {
    showToast("删除失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function renderMessages() {
  const runs = state.chatSessionId
    ? agentRuns().filter(run => run.sessionId === state.chatSessionId)
    : [];
  $("chatEmpty").hidden = runs.length > 0;
  document.querySelectorAll("#messageList .message").forEach(node => node.remove());
  runs.forEach(run => {
    appendMessage("user", run.input, { time: run.startedAt });
    if (run.status === "RUNNING") {
      appendMessage("assistant", "", {
        loading: true,
        runId: run.id,
        model: run.model,
        time: run.startedAt
      });
      return;
    }
    const cancelled = run.status === "CANCELLED";
    appendMessage(
      run.status === "COMPLETED" ? "assistant" : cancelled ? "status" : "error",
      run.output || run.error?.message || (cancelled ? "运行已取消" : `运行状态：${run.status}`),
      {
        runId: run.id,
        model: run.model,
        time: run.completedAt,
        errorCode: run.error?.code || "",
        retryRunId: run.id
      }
    );
  });
  scrollMessages();
}

function appendMessage(
  role,
  content,
  {
    loading = false,
    runId = "",
    time = null,
    errorCode = "",
    retryRunId = "",
    model = ""
  } = {}
) {
  $("chatEmpty").hidden = true;
  const node = document.createElement("article");
  node.className = `message ${role}`;
  if (loading) node.dataset.loadingMessage = "true";
  const author = role === "user" ? "你" : role === "error" ? "运行错误" : state.current?.draft?.metadata?.name || "Agent";
  const modelResourceId = state.current?.draft?.spec?.bindings?.modelProfileId || "";
  const recovery = role === "error" && errorCode === "SECRET_NOT_FOUND" && modelResourceId
    ? `
      <div class="message-actions">
        <button class="button secondary small" data-configure-model="${escapeHtml(modelResourceId)}" type="button">配置模型凭证</button>
        <button class="button tertiary small" data-retry-run="${escapeHtml(retryRunId)}" type="button">重新发送</button>
      </div>
    `
    : "";
  const renderedContent = loading
    ? '<span class="message-loading"><i></i><i></i><i></i></span>'
    : role === "assistant"
      ? '<ksadk-message></ksadk-message>'
      : `<span class="plain-message">${escapeHtml(content)}</span>`;
  node.innerHTML = `
    <div class="message-meta"><strong>${escapeHtml(author)}</strong><span>${time ? formatDate(time) : "刚刚"}</span>${model ? `<span class="message-model">${escapeHtml(model)}</span>` : ""}${runId ? `<span>${escapeHtml(shortId(runId, 18))}</span>` : ""}</div>
    <div class="message-content">${renderedContent}${recovery}</div>
  `;
  const markdown = node.querySelector("ksadk-message");
  if (markdown) {
    if (customElements.get("ksadk-message")) markdown.content = content;
    else customElements.whenDefined("ksadk-message").then(() => { markdown.content = content; });
  }
  $("messageList").append(node);
  return node;
}

function updateStreamingMessage(node, content) {
  node.removeAttribute("data-loading-message");
  const container = node.querySelector(".message-content");
  let markdown = container.querySelector("ksadk-message");
  if (!markdown) {
    container.replaceChildren(document.createElement("ksadk-message"));
    markdown = container.querySelector("ksadk-message");
  }
  if (customElements.get("ksadk-message")) markdown.content = content;
  else customElements.whenDefined("ksadk-message").then(() => { markdown.content = content; });
  scrollMessages();
}

function parseSseBlock(block) {
  let id = "";
  let type = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try {
    return { id, type, data: JSON.parse(raw) };
  } catch {
    return { id, type, data: { text: raw } };
  }
}

async function streamRun(buildId, body, onEvent, allowSessionRecovery = true) {
  const headers = new Headers({
    "Content-Type": "application/json",
    "Idempotency-Key": operationKey("chat-stream")
  });
  if (state.csrf) headers.set("X-CSRF-Token", state.csrf);
  if (state.sessionToken) headers.set("X-AgentKit-Session", state.sessionToken);
  const response = await fetch(`/api/v1/builds/${encodeURIComponent(buildId)}/run:stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    credentials: "same-origin"
  });
  if (response.status === 401 && allowSessionRecovery) {
    await recoverBrowserSession();
    return streamRun(buildId, body, onEvent, false);
  }
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => ({}));
    const error = payload?.error || {};
    const failure = new Error(error.message || `运行请求失败（HTTP ${response.status}）`);
    failure.code = error.code || "";
    throw failure;
  }
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const event = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) onEvent(event);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  const tail = parseSseBlock(buffer);
  if (tail) onEvent(tail);
}

function scrollMessages() {
  $("messageList").scrollTop = $("messageList").scrollHeight;
}

function autoSizeComposer() {
  const input = $("chatInput");
  input.style.height = "42px";
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 42), 160)}px`;
}

async function ensureChatBuild() {
  if (state.build?.status === "SUCCEEDED") return state.build;
  const latest = currentSuccessfulBuild(state.current);
  if (latest) {
    state.build = latest;
    return latest;
  }
  showToast(
    "正在准备不可变 Runtime Bundle",
    "第一次对话前需要完成本地构建。"
  );
  return buildCurrentAgent({ navigate: false });
}

async function sendChatMessage() {
  const input = $("chatInput");
  const content = input.value.trim();
  if (!content || $("sendMessage").disabled) return;
  if (!state.current) {
    showToast("没有选择 Agent", "请先创建或选择一个 Agent。", "error");
    return;
  }
  const runAgentId = state.current.draft.metadata.id;
  const selectedModel = $("chatModel").value || state.activeChatModel;
  const chatRevision = state.chatViewRevision;
  const requestedSessionId = state.chatSessionId;
  const isVisible = () => state.chatViewRevision === chatRevision
    && state.current?.draft?.metadata?.id === runAgentId;
  $("sendMessage").disabled = true;
  input.value = "";
  autoSizeComposer();
  appendMessage("user", content);
  const loading = appendMessage("assistant", "", {
    loading: true,
    model: selectedModel
  });
  scrollMessages();
  try {
    const build = await ensureChatBuild();
    if (!build) {
      throw new Error("Runtime Bundle 尚未就绪");
    }
    const body = {
      model: selectedModel,
      input: { role: "user", content },
      environment: "local",
      stream: true
    };
    if (requestedSessionId) body.sessionId = requestedSessionId;
    beginChatRunInspector();
    const liveEvents = [];
    let streamedOutput = "";
    let streamedSessionId = requestedSessionId;
    let streamFailure = null;
    await streamRun(build.id, body, event => {
      liveEvents.push(event);
      if (event.type === "run.created") streamedSessionId = event.data.sessionId || streamedSessionId;
      if (event.type === "message.delta") {
        streamedOutput += String(event.data.text || "");
        if (isVisible()) updateStreamingMessage(loading, streamedOutput);
      } else if (event.type === "message.completed") {
        streamedOutput = String(event.data.text || streamedOutput);
        if (isVisible()) updateStreamingMessage(loading, streamedOutput);
      } else if (event.type === "run.failed") {
        streamFailure = event.data;
      }
      if (isVisible()) renderTimeline(liveEvents);
    });
    if (streamFailure) {
      const failure = new Error(streamFailure.message || streamFailure.error || "Agent 运行失败");
      failure.code = streamFailure.code || "";
      throw failure;
    }
    await refreshRuns();
    const run = state.runs
      .filter(item => item.agentId === runAgentId && item.sessionId === streamedSessionId)
      .at(-1);
    if (!run) throw new Error("运行已结束，但未找到对应的 Run 记录");
    loading.remove();
    const completedSuccessfully = run.status === "COMPLETED";
    if (run.error?.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    if (isVisible()) {
      state.chatSessionId = streamedSessionId;
      syncBrowserRoute();
      state.activeRun = run;
      renderSessionList();
      renderMessages();
      await renderTrace(run);
      renderInvocation();
      if (!completedSuccessfully && run.status !== "CANCELLED") {
        showToast(
          "Agent 运行未完成",
          run.error?.message || `运行状态：${run.status}`,
          "error"
        );
      }
    }
  } catch (error) {
    loading.remove();
    if (error.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    if (isVisible()) {
      appendMessage("error", error.message, { errorCode: error.code || "" });
      setStatusBadge($("inspectorStatus"), "FAILED");
      showToast("运行失败", error.message, "error");
    }
  } finally {
    if (isVisible()) {
      $("sendMessage").disabled = false;
      input.focus();
      scrollMessages();
    }
  }
}

async function renderTrace(run) {
  const trace = await api(`/traces/${encodeURIComponent(run.traceId)}`);
  renderSpanTimeline(trace.spans || []);
  $("inspectorRunId").textContent = run.id;
  setStatusBadge($("inspectorStatus"), run.status);
  $("usageInput").textContent = trace.metrics?.usageReported
    ? formatTokenCount(trace.metrics.inputTokens)
    : "未上报";
  $("usageOutput").textContent = trace.metrics?.usageReported
    ? formatTokenCount(trace.metrics.outputTokens)
    : "未上报";
  $("usageDuration").textContent = formatDuration(trace.metrics?.durationMs);
  $("openFullTrace").disabled = false;
  $("openFullTrace").dataset.traceId = trace.traceId;
}

function renderSpanTimeline(spans) {
  const ordered = [...spans].sort((left, right) => (
    Number(BigInt(left.startTimeUnixNano || "0") - BigInt(right.startTimeUnixNano || "0"))
  ));
  $("eventTimeline").innerHTML = ordered.length
    ? ordered.map(span => `
      <div class="timeline-event">
        <span class="timeline-marker"></span>
        <div class="timeline-copy">
          <strong>${escapeHtml(span.name)}</strong>
          <span>${escapeHtml(span.kind)} · ${escapeHtml(formatDuration(span.durationMs))} · ${escapeHtml(span.status)}</span>
        </div>
      </div>
    `).join("")
    : '<div class="timeline-empty">当前 Trace 没有 Span</div>';
}

function renderTimeline(events) {
  const visibleEvents = compactTimelineEvents(events);
  $("eventTimeline").innerHTML = visibleEvents.length
    ? visibleEvents.map(event => `
      <div class="timeline-event">
        <span class="timeline-marker"></span>
        <div class="timeline-copy"><strong>${escapeHtml(event.type)}</strong><span>${escapeHtml(eventSummary(event))}</span></div>
      </div>
    `).join("")
    : '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
}

function clearChatRunInspector() {
  state.activeRun = null;
  $("eventTimeline").innerHTML = '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
  setStatusBadge($("inspectorStatus"), "IDLE");
  $("inspectorRunId").textContent = "尚未运行";
  $("usageInput").textContent = "未上报";
  $("usageOutput").textContent = "未上报";
  $("usageDuration").textContent = "未上报";
  $("openFullTrace").disabled = true;
  delete $("openFullTrace").dataset.traceId;
}

function beginChatRunInspector() {
  clearChatRunInspector();
  setStatusBadge($("inspectorStatus"), "RUNNING");
  $("inspectorRunId").textContent = "正在创建 Run";
  $("eventTimeline").innerHTML = '<div class="timeline-empty">正在建立 Runtime 连接…</div>';
}

function compactTimelineEvents(events) {
  const compacted = [];
  for (const event of events || []) {
    if (!["thinking.delta", "message.delta"].includes(event?.type)) {
      compacted.push(event);
      continue;
    }
    const previous = compacted.at(-1);
    if (previous?.type === event.type) {
      previous.data.text = String(previous.data.text || "") + String(event.data?.text || "");
      previous.createdAt = event.createdAt || previous.createdAt;
    } else {
      compacted.push({ ...event, data: { ...(event.data || {}), text: String(event.data?.text || "") } });
    }
  }
  return compacted;
}

function eventSummary(event) {
  const data = event.data || {};
  if (event.type === "run.started") return `${data.runtime || data.runtimeType || "RuntimeAdapter"}`;
  if (event.type === "command.started") return data.command || "command";
  if (event.type === "command.completed") return `exit ${data.exitCode ?? "-"} · ${data.durationMs || 0} ms`;
  if (["thinking.delta", "message.delta"].includes(event.type)) return String(data.text || "").trim();
  if (data.model) return `${data.model} · step ${data.step || 1}`;
  if (data.tool) return data.tool;
  if (data.usage) return `${data.usage.totalTokens || 0} tokens`;
  if (data.output) return shortId(data.output, 60);
  return formatDate(event.createdAt);
}

function newChatSession() {
  state.chatViewRevision += 1;
  state.chatSessionId = null;
  $("sendMessage").disabled = false;
  renderSessionList();
  renderMessages();
  clearChatRunInspector();
  $("chatInput").focus();
  renderInvocation();
  syncBrowserRoute();
}
