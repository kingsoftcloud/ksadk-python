async function refreshTraces({ selectFirst = true } = {}) {
  const query = new URLSearchParams({ limit: "500" });
  const agentId = $("traceAgentFilter").value;
  const status = $("traceStatusFilter").value;
  if (agentId) query.set("agentId", agentId);
  if (status) query.set("status", status);
  const payload = await api(`/traces?${query}`);
  state.traces = payload.items || [];
  renderTraceList();
  const currentId = state.activeTrace?.traceId;
  const currentStillVisible = currentId
    && state.traces.some(trace => trace.traceId === currentId);
  if (currentStillVisible) {
    await openTrace(currentId, { switchToView: false });
  } else if (selectFirst && state.traces.length) {
    await openTrace(state.traces[0].traceId, { switchToView: false });
  } else {
    clearTraceExplorer();
  }
}

function filteredTraces() {
  const query = $("traceSearch").value.trim().toLowerCase();
  if (!query) return state.traces;
  return state.traces.filter(trace => [
    trace.traceId,
    trace.runId,
    trace.sessionId,
    trace.agentId,
    trace.model,
    trace.runtimeType
  ].some(value => String(value || "").toLowerCase().includes(query)));
}

function renderTraceList() {
  const traces = filteredTraces();
  $("traceCount").textContent = `${traces.length} 条`;
  $("traceEmpty").hidden = traces.length > 0;
  $("traceList").hidden = traces.length === 0;
  $("traceList").innerHTML = traces.map(trace => `
    <button class="trace-list-item ${state.activeTrace?.traceId === trace.traceId ? "active" : ""}"
      data-open-trace="${escapeHtml(trace.traceId)}" type="button">
      <span class="trace-list-status ${escapeHtml(trace.status)}"></span>
      <span class="trace-list-copy">
        <strong>${escapeHtml(trace.agentId || "unknown-agent")}</strong>
        <span>${escapeHtml(shortId(trace.traceId, 24))}</span>
        <span class="trace-list-meta">
          <span>${escapeHtml(formatDate(trace.startedAt))}</span>
          <span>${escapeHtml(formatDuration(trace.durationMs))} · ${trace.usageReported ? `${escapeHtml(formatTokenCount(trace.totalTokens))} tokens` : "Token 未上报"}</span>
        </span>
      </span>
    </button>
  `).join("");
}

async function openTrace(traceId, { switchToView = true } = {}) {
  const trace = await api(`/traces/${encodeURIComponent(traceId)}`);
  // 从会话工作台直达 Trace 时，列表尚未预取。先同步目录，避免详情已经
  // 正确展示而左侧却错误提示“还没有 Trace”。由 refreshTraces 打开的条目
  // 已在 state.traces 中，不会产生重复请求。
  if (!state.traces.some(item => item.traceId === trace.traceId)) {
    const payload = await api("/traces?limit=500");
    state.traces = payload.items || [];
  }
  state.activeTrace = trace;
  state.activeSpanId = trace.rootSpanId || trace.spans?.[0]?.spanId || null;
  state.traceRawOtlp = null;
  $("copyRawOtlp").disabled = false;
  state.traceTab = "summary";
  renderTraceList();
  renderTraceExplorer();
  if (switchToView) switchView("observability");
  else syncBrowserRoute();
}

function clearTraceExplorer() {
  state.activeTrace = null;
  state.activeSpanId = null;
  state.traceRawOtlp = null;
  $("traceMetricStatus").textContent = "未选择";
  $("traceMetricSpanCount").textContent = "选择一条 Trace 查看";
  $("traceMetricDuration").textContent = "未上报";
  $("traceMetricDurationSource").textContent = "等待 Runtime 上报";
  $("traceMetricTokens").textContent = "未上报";
  $("traceMetricTokenSplit").textContent = "输入 / 输出";
  $("traceMetricModel").textContent = "-";
  $("traceMetricRuntime").textContent = "Runtime";
  $("traceTitle").textContent = "选择一条 Trace";
  $("traceIdLabel").textContent = "-";
  $("copyTraceparent").disabled = true;
  $("copyRawOtlp").disabled = true;
  $("traceSpanTree").innerHTML = '<div class="trace-stage-empty"><svg data-icon="network"></svg><p>选择左侧 Trace，查看 Agent、模型和 Tool 的父子关系与耗时。</p></div>';
  $("traceDetailTitle").textContent = "Span 详情";
  $("traceDetailSubtitle").textContent = "尚未选择 Span";
  $("traceDetailContent").innerHTML = '<div class="trace-stage-empty compact"><p>选择一个 Span 查看标准属性。</p></div>';
  $("traceRawOtlp").hidden = true;
  $("traceDetail").querySelector(".trace-detail-body").classList.remove("raw-active");
  injectIcons($("traceExplorer"));
  syncBrowserRoute();
}

function renderTraceExplorer() {
  const trace = state.activeTrace;
  if (!trace) {
    clearTraceExplorer();
    return;
  }
  const metrics = trace.metrics || {};
  $("traceMetricStatus").textContent = trace.status || "UNSET";
  $("traceMetricSpanCount").textContent = `${trace.spans?.length || 0} Span · ${trace.target?.name || "本地工作区"}`;
  $("traceMetricDuration").textContent = formatDuration(metrics.durationMs);
  $("traceMetricDurationSource").textContent = metrics.durationMs === null || metrics.durationMs === undefined
    ? "Runtime 未上报"
    : metrics.durationSource === "runtime" ? "Runtime 精确上报" : "Studio 时钟回退";
  $("traceMetricTokens").textContent = metrics.usageReported
    ? formatTokenCount(metrics.totalTokens)
    : "未上报";
  $("traceMetricTokenSplit").textContent = metrics.usageReported
    ? `${formatTokenCount(metrics.inputTokens)} 输入 · ${formatTokenCount(metrics.outputTokens)} 输出`
    : "Provider / Runtime 未返回 Usage";
  $("traceMetricModel").textContent = trace.model || "-";
  $("traceMetricRuntime").textContent = `${trace.runtimeType || "unknown"} · ${metrics.usageSource || "Usage 未上报"}`;
  $("traceTitle").textContent = `${trace.agentId || "Agent"} · ${trace.runId || "Run"}`;
  $("traceIdLabel").textContent = trace.traceId;
  $("copyTraceparent").disabled = false;
  renderTraceSpans();
  renderTraceDetail();
  renderTraceList();
}

function orderedTraceSpans(trace) {
  const spans = trace?.spans || [];
  const byParent = new Map();
  spans.forEach(span => {
    const parent = span.parentSpanId || "";
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(span);
  });
  byParent.forEach(children => children.sort((left, right) => (
    Number(BigInt(left.startTimeUnixNano || "0") - BigInt(right.startTimeUnixNano || "0"))
  )));
  const ordered = [];
  const visited = new Set();
  const visit = (span, depth) => {
    if (!span || visited.has(span.spanId)) return;
    visited.add(span.spanId);
    ordered.push({ span, depth });
    (byParent.get(span.spanId) || []).forEach(child => visit(child, depth + 1));
  };
  const root = spans.find(span => span.spanId === trace.rootSpanId)
    || spans.find(span => !span.parentSpanId);
  visit(root, 0);
  spans.forEach(span => visit(span, span.parentSpanId ? 1 : 0));
  return ordered;
}

function renderTraceSpans() {
  const trace = state.activeTrace;
  const ordered = orderedTraceSpans(trace);
  if (!ordered.length) {
    $("traceSpanTree").innerHTML = '<div class="trace-stage-empty"><p>该 OTLP Trace 没有 Span。</p></div>';
    return;
  }
  const root = ordered.find(item => item.span.spanId === trace.rootSpanId)?.span || ordered[0].span;
  const rootStart = BigInt(root.startTimeUnixNano || "0");
  const rootEnd = BigInt(root.endTimeUnixNano || root.startTimeUnixNano || "0");
  const rootDuration = Number(rootEnd > rootStart ? rootEnd - rootStart : 1n);
  $("traceSpanTree").innerHTML = ordered.map(({ span, depth }) => {
    const start = BigInt(span.startTimeUnixNano || "0");
    const end = BigInt(span.endTimeUnixNano || span.startTimeUnixNano || "0");
    const left = Math.max(0, Math.min(100, Number(start - rootStart) / rootDuration * 100));
    const width = Math.max(0, Math.min(100 - left, Number(end - start) / rootDuration * 100));
    return `
      <button class="trace-span-row ${span.spanId === state.activeSpanId ? "active" : ""}"
        data-open-span="${escapeHtml(span.spanId)}" data-kind="${escapeHtml(span.kind)}" data-status="${escapeHtml(span.status)}" type="button">
        <span class="trace-span-name">
          <span class="trace-span-guides">${'<span class="trace-span-guide"></span>'.repeat(depth)}</span>
          <span class="trace-span-status ${escapeHtml(span.status)}"></span>
          <span class="trace-span-name-copy"><strong>${escapeHtml(span.name)}</strong><span>${escapeHtml(span.kind)} · ${escapeHtml(shortId(span.spanId, 16))}</span></span>
        </span>
        <span class="trace-waterfall-track"><span class="trace-waterfall-bar" style="--span-left:${left.toFixed(3)}%;--span-width:${width.toFixed(3)}%"></span></span>
        <span class="trace-span-duration">${escapeHtml(formatDuration(span.durationMs))}</span>
      </button>
    `;
  }).join("");
}

function traceKeyValues(values) {
  const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) return '<div class="trace-stage-empty compact"><p>没有可展示的字段。</p></div>';
  return `<dl class="trace-kv-list">${entries.map(([key, value]) => `
    <div class="trace-kv-row"><dt class="trace-kv-key">${escapeHtml(key)}</dt><dd class="trace-kv-value">${escapeHtml(typeof value === "object" ? JSON.stringify(value, null, 2) : value)}</dd></div>
  `).join("")}</dl>`;
}

function setTraceDetailExpanded(expanded) {
  state.traceDetailExpanded = Boolean(expanded);
  $("traceWorkbench").classList.toggle("detail-expanded", state.traceDetailExpanded);
  $("toggleTraceDetail").setAttribute("aria-pressed", String(state.traceDetailExpanded));
  $("toggleTraceDetail").setAttribute(
    "aria-label",
    state.traceDetailExpanded ? "收起 Span 详情" : "展开 Span 详情"
  );
  $("toggleTraceDetail").title = state.traceDetailExpanded ? "收起详情" : "展开详情";
  $("toggleTraceDetail").querySelector("span").textContent = state.traceDetailExpanded ? "收起" : "展开";
}

function renderTraceDetail() {
  const trace = state.activeTrace;
  const span = trace?.spans?.find(item => item.spanId === state.activeSpanId);
  document.querySelectorAll("[data-trace-tab]").forEach(button => {
    const active = button.dataset.traceTab === state.traceTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("traceRawOtlp").hidden = state.traceTab !== "raw";
  $("traceDetailContent").hidden = state.traceTab === "raw";
  $("traceDetail").querySelector(".trace-detail-body").classList.toggle("raw-active", state.traceTab === "raw");
  if (!span) {
    $("traceDetailTitle").textContent = "Span 详情";
    $("traceDetailSubtitle").textContent = "尚未选择 Span";
    $("traceDetailContent").innerHTML = '<div class="trace-stage-empty compact"><p>选择一个 Span 查看标准属性。</p></div>';
    return;
  }
  $("traceDetailTitle").textContent = span.name;
  $("traceDetailSubtitle").textContent = `${span.kind} · ${span.status}`;
  if (state.traceTab === "summary") {
    $("traceDetailContent").innerHTML = `<dl class="trace-detail-grid">
      <div><dt>Trace ID</dt><dd>${escapeHtml(trace.traceId)}</dd></div>
      <div><dt>Span ID</dt><dd>${escapeHtml(span.spanId)}</dd></div>
      <div><dt>Parent</dt><dd>${escapeHtml(span.parentSpanId || "Root")}</dd></div>
      <div><dt>Kind</dt><dd>${escapeHtml(span.kind)}</dd></div>
      <div><dt>Status</dt><dd>${escapeHtml(span.status)}</dd></div>
      <div><dt>开始</dt><dd>${escapeHtml(formatNanoseconds(span.startTimeUnixNano))}</dd></div>
      <div><dt>耗时</dt><dd>${escapeHtml(formatDuration(span.durationMs))}</dd></div>
    </dl>`;
  } else if (state.traceTab === "attributes") {
    $("traceDetailContent").innerHTML = traceKeyValues(span.attributes);
  } else if (state.traceTab === "events") {
    $("traceDetailContent").innerHTML = span.events?.length
      ? `<div class="trace-event-list">${span.events.map(event => `<article class="trace-event-card"><strong>${escapeHtml(event.name)}</strong><span>${escapeHtml(formatNanoseconds(event.timeUnixNano))}</span>${traceKeyValues(event.attributes)}</article>`).join("")}</div>`
      : '<div class="trace-stage-empty compact"><p>该 Span 没有 Events。</p></div>';
  } else if (state.traceTab === "resource") {
    $("traceDetailContent").innerHTML = traceKeyValues({ ...trace.resource, "otel.scope.name": trace.scope?.name, "otel.scope.version": trace.scope?.version });
  } else if (state.traceTab === "raw") {
    loadRawTrace(trace.traceId).catch(handleGlobalError);
  }
}

async function loadRawTrace(traceId) {
  if (state.traceRawOtlp) {
    $("traceRawOtlp").textContent = JSON.stringify(state.traceRawOtlp, null, 2);
    return;
  }
  $("traceRawOtlp").textContent = "正在读取 OTLP JSON…";
  const raw = await api(`/traces/${encodeURIComponent(traceId)}/otlp`);
  if (state.activeTrace?.traceId !== traceId) return;
  state.traceRawOtlp = raw;
  $("traceRawOtlp").textContent = JSON.stringify(raw, null, 2);
}

async function copyRawTrace() {
  const traceId = state.activeTrace?.traceId;
  if (!traceId) return;
  if (!state.traceRawOtlp) await loadRawTrace(traceId);
  if (!state.traceRawOtlp || state.activeTrace?.traceId !== traceId) return;
  await navigator.clipboard.writeText(JSON.stringify(state.traceRawOtlp, null, 2));
  const label = $("copyRawOtlp").querySelector("span");
  label.textContent = "已复制";
  window.setTimeout(() => { label.textContent = "复制 Raw OTLP"; }, 1400);
  showToast("Raw OTLP 已复制", shortId(traceId, 24));
}
