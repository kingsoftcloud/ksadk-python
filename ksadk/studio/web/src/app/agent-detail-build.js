async function openAgentDetail(agentId) {
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  renderGlobalContext();
  renderAgentDetail();
  switchView("agent-detail", { parent: "Agent", title: detail.draft.metadata.name });
}

function renderAgentDetail() {
  if (!state.current) return;
  const draft = state.current.draft;
  const bindings = draft.spec.bindings || {};
  $("detailEdit").hidden = false;
  $("detailAgentName").textContent = draft.metadata.name;
  $("detailAgentMeta").textContent = `${draft.metadata.id} · revision ${draft.metadata.revision}`;
  $("detailSystemPrompt").textContent = draft.spec.instructions.system;
  $("detailTaskPrompt").textContent = draft.spec.instructions.task || "未配置任务契约";
  const manifestModels = String(
    draft.metadata.labels?.["agentkit.ksyun.com/models"] || ""
  ).split(",").map(item => item.trim()).filter(Boolean);
  const boundModelIds = bindings.modelProfileIds?.length
    ? bindings.modelProfileIds
    : bindings.modelProfileId ? [bindings.modelProfileId] : [];
  const groups = [
    ["Model", boundModelIds.length
      ? boundModelIds
      : manifestModels.length ? manifestModels : draft.metadata.labels?.["agentkit.ksyun.com/model"]
        ? [draft.metadata.labels["agentkit.ksyun.com/model"]]
        : []],
    ["Skill", (bindings.skills || []).map(item => item.resourceId)],
    ["MCP", (bindings.mcpServers || []).map(item => item.resourceId)],
    ["Tool", (bindings.tools || []).map(item => item.resourceId)]
  ];
  $("detailBindings").innerHTML = groups.map(([name, ids]) => `
    <div class="binding-group"><span>${name}</span><div class="binding-items">${
      ids.length
        ? ids.map(id => {
          const resource = resourceById(id);
          return `<span class="compact-resource"><svg data-icon="check"></svg>${escapeHtml(resource?.displayName || shortId(id, 28))}</span>`;
        }).join("")
        : '<span class="compact-resource">未绑定</span>'
    }</div></div>
  `).join("");
  $("detailSummary").innerHTML = `
    <div><dt>Revision</dt><dd>r${draft.metadata.revision}</dd></div>
    <div><dt>Runtime</dt><dd>${escapeHtml(draft.spec.runtime?.type || draft.metadata.labels?.["agentkit.ksyun.com/framework"] || "adk")}</dd></div>
    <div><dt>策略</dt><dd>${escapeHtml(draft.spec.execution.strategy)}</dd></div>
    <div><dt>最大步骤</dt><dd>${draft.spec.execution.maxSteps}</dd></div>
    <div><dt>超时</dt><dd>${draft.spec.execution.timeoutSeconds}s</dd></div>
  `;
  const latestBuild = currentSuccessfulBuild(state.current);
  $("detailBuildState").innerHTML = latestBuild
    ? `<span class="status-dot success"></span><div><strong>Bundle 已就绪</strong><span>${escapeHtml(shortId(latestBuild.bundleDigest, 28))}</span></div>`
    : '<span class="status-dot neutral"></span><div><strong>尚未构建</strong><span>创建 Bundle 后即可对话</span></div>';
  injectIcons($("view-agent-detail"));
}

async function buildCurrentAgent({ navigate = true } = {}) {
  if (!state.current) {
    showToast("没有选择 Agent", "请先从 Agent 列表选择一个 Agent。", "error");
    return null;
  }
  if (navigate) switchView("builds");
  renderBuildWorkspace();
  const draft = state.current.draft;
  $("buildLog").textContent = "提交本地构建...\n";
  setStatusBadge($("buildStatus"), "QUEUED");
  const button = $("buildCurrentAgent");
  setButtonLoading(button, true, "构建中");
  try {
    const operation = await api(`/agents/${encodeURIComponent(draft.metadata.id)}/builds`, {
      method: "POST",
      headers: { "Idempotency-Key": `build-${draft.metadata.id}-r${draft.metadata.revision}-${Date.now()}` },
      body: { revision: draft.metadata.revision, runEvaluation: false }
    });
    $("buildOperationId").textContent = operation.id;
    const completed = await waitOperation(operation.id, {
      onEvents: events => appendBuildEvents(events),
      onStatus: status => setStatusBadge($("buildStatus"), status)
    });
    if (completed.status !== "SUCCEEDED") {
      throw new Error(completed.error?.message || "构建未完成");
    }
    const completedBuild = await api(`/builds/${encodeURIComponent(completed.resourceId)}`);
    const refreshed = await api(`/agents/${encodeURIComponent(draft.metadata.id)}`);
    state.agentDetails.set(draft.metadata.id, refreshed);
    renderAgentRows();
    if (state.current?.draft?.metadata?.id === draft.metadata.id) {
      state.current = refreshed;
      state.build = completedBuild;
      renderBuildWorkspace();
    }
    showToast(
      `${draft.spec.runtime?.type || "Agent"} Bundle 构建完成`,
      shortId(completedBuild.bundleDigest, 36)
    );
    return completedBuild;
  } catch (error) {
    setStatusBadge($("buildStatus"), "FAILED");
    $("buildLog").textContent += `\n${error.message}\n`;
    showToast("构建失败", error.message, "error");
    return null;
  } finally {
    setButtonLoading(button, false);
  }
}

function renderBuildWorkspace() {
  const draft = state.current?.draft;
  const build = state.build || currentSuccessfulBuild(state.current);
  $("buildAgentName").textContent = draft?.metadata.name || "未选择";
  $("buildRevision").textContent = draft ? `r${draft.metadata.revision}` : "-";
  $("buildDigest").textContent = build?.bundleDigest || "-";
  setStatusBadge($("buildStatus"), build?.status || "IDLE");
  $("runtimeManifestDigest").textContent = build?.manifestSha256 || build?.sourceDigest || build?.resolvedDigest || "-";
  $("runtimeContract").textContent = build?.runtimeName
    ? `${build.runtimeName} ${build.runtimeVersion || ""}`.trim()
    : build?.runtimeType || draft?.spec?.runtime?.type || "未选择";
  if (build) {
    $("buildLog").textContent = [
      `Build       ${build.id}`,
      `Agent       ${build.agentId}`,
      `Revision    ${build.sourceRevision}`,
      `Resolved    ${build.resolvedDigest}`,
      `Bundle      ${build.bundleDigest}`,
      `Status      ${build.status}`
    ].join("\n");
  }
}

function appendBuildEvents(events) {
  if (!events.length) return;
  $("buildLog").textContent += events
    .map(event => `${String(event.id).padStart(2, "0")}  ${event.type}`)
    .join("\n") + "\n";
  $("buildLog").scrollTop = $("buildLog").scrollHeight;
}

async function waitOperation(operationId, { onEvents = () => {}, onStatus = () => {} } = {}) {
  let cursor = 0;
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const events = await fetchSse(`/operations/${encodeURIComponent(operationId)}/events?after=${cursor}`);
    if (events.length) {
      cursor = Math.max(cursor, ...events.map(event => event.id));
      onEvents(events);
    }
    const operation = await api(`/operations/${encodeURIComponent(operationId)}`);
    onStatus(operation.status, operation);
    if (terminalOperationStatuses.has(operation.status)) return operation;
    await sleep(200);
  }
  throw new Error("操作等待超时");
}
