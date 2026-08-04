function openOverlay(id) {
  $(id).hidden = false;
  document.body.style.overflow = "hidden";
  const focusable = $(id).querySelector("input, textarea, select, button");
  window.setTimeout(() => focusable?.focus(), 0);
}

function closeOverlay(id) {
  $(id).hidden = true;
  document.body.style.overflow = "";
}

function updateMcpTransport() {
  const stdio = $("mcpTransport").value === "stdio";
  $("mcpCommandField").hidden = !stdio;
  $("mcpArgsField").hidden = !stdio;
  $("mcpEndpointField").hidden = stdio;
  $("mcpCommand").required = stdio;
  $("mcpEndpoint").required = !stdio;
}

async function saveAndProbeMcp() {
  if (!$("mcpForm").reportValidity()) return;
  $("mcpError").hidden = true;
  const button = $("saveAndProbeMcp");
  setButtonLoading(button, true, "正在探测");
  try {
    const transport = $("mcpTransport").value;
    const envName = $("mcpEnvName").value.trim();
    const secretRef = $("mcpSecretRef").value.trim();
    const server = {
      name: $("mcpName").value.trim(),
      version: $("mcpVersion").value.trim(),
      transport,
      args: transport === "stdio"
        ? $("mcpArgs").value.trim().split(/\s+/).filter(Boolean)
        : [],
      envRefs: envName && secretRef ? { [envName]: secretRef } : {}
    };
    if (transport === "stdio") server.command = $("mcpCommand").value.trim();
    else server.endpointUrl = $("mcpEndpoint").value.trim();
    const created = await api("/catalog/mcp-servers", {
      method: "POST",
      body: {
        displayName: $("mcpDisplayName").value.trim(),
        description: $("mcpDescription").value.trim(),
        server
      }
    });
    const probed = await api(`/catalog/mcp-servers/${encodeURIComponent(created.resourceId)}:probe?timeoutSeconds=15`, {
      method: "POST"
    });
    await refreshCatalog();
    state.wizard.autoBindMcp = false;
    state.wizard.selectedMcpIds = [probed.resourceId];
    await composeAgent();
    closeOverlay("mcpOverlay");
    $("mcpForm").reset();
    updateMcpTransport();
    showToast("MCP 已连接", `已发现 ${probed.health?.toolCount || 0} 个 Tool。`);
  } catch (error) {
    $("mcpError").hidden = false;
    $("mcpErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

function openInvocation() {
  renderInvocation();
  openOverlay("invokeOverlay");
}

function renderInvocation() {
  const buildId = state.build?.id || currentSuccessfulBuild(state.current)?.id;
  const sessionId = state.chatSessionId;
  const endpoint = "/v1/responses";
  $("invokeEndpoint").textContent = `POST ${endpoint}`;
  $("invokeBuildId").textContent = buildId || "尚未构建";
  $("invokeSessionId").textContent = sessionId || "首次调用可省略";
  const template = state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/template"] || "blank";
  const modelResource = resourceById(
    state.current?.draft?.spec?.bindings?.modelProfileId || ""
  );
  const body = {
    model: $("chatModel").value
      || state.activeChatModel
      || modelResource?.contract?.model
      || state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/model"]
      || "glm-5.1",
    input: [{
      role: "user",
      content: [{
        type: "input_text",
        text: template === "research"
          ? "调研 Agent 工程平台的核心能力"
          : "请根据你的职责处理这个请求"
      }]
    }],
    metadata: {
      agent_id: state.current?.draft?.metadata?.id || "review-helper"
    },
    ...(sessionId ? { conversation: sessionId } : {}),
    stream: true
  };
  const code = state.invocationTab === "curl"
    ? [
      `curl -X POST "${window.location.origin}${endpoint}" \\`,
      '  -H "Content-Type: application/json" \\',
      '  -H "Authorization: Bearer <RUNTIME_API_KEY>" \\',
      `  -d '${JSON.stringify(body, null, 2)}'`
    ].join("\n")
    : [
      `const response = await fetch("${window.location.origin}${endpoint}", {`,
      '  method: "POST",',
      "  headers: {",
      '    "Content-Type": "application/json",',
      '    "Authorization": `Bearer ${runtimeApiKey}`',
      "  },",
      `  body: JSON.stringify(${JSON.stringify(body, null, 2)})`,
      "});",
      "for await (const event of response.body) {",
      "  // OpenAI Responses SSE: created / output_text.delta / completed",
      "  console.log(event);",
      "}"
    ].join("\n");
  $("invocationCode").textContent = code;
}

async function copyInvocation() {
  await navigator.clipboard.writeText($("invocationCode").textContent);
  const label = $("copyInvocation").querySelector("span");
  label.textContent = "已复制";
  window.setTimeout(() => { label.textContent = "复制"; }, 1400);
}

function renderResources() {
  const names = {
    model: ["模型", "管理 Model Profile、Endpoint 和凭据引用。"],
    tool: ["Tool", "管理结构化 Tool Contract、权限和审批策略。"],
    mcp: ["MCP", "连接、探测并复用 MCP Server。"],
    skill: ["Skill", "安装版本化 Skill，并在构建时锁定内容摘要。"]
  };
  $("resourcePageTitle").textContent = names[state.resourceKind][0];
  $("resourcePageDescription").textContent = names[state.resourceKind][1];
  $("addResourceButton").querySelector("span").textContent = state.resourceKind === "model"
    ? "配置模型"
    : state.resourceKind === "skill"
    ? "发现 Skill"
    : state.resourceKind === "tool"
    ? "添加 Python Tool"
    : "添加资源";
  const headings = state.resourceKind === "model"
    ? ["发现来源", "上下文窗口", "输入模态"]
    : state.resourceKind === "tool"
    ? ["来源", "Tool 分组", "权限 / 边界"]
    : ["来源", "版本", "说明"];
  $("resourceSourceHeading").textContent = headings[0];
  $("resourceDetailHeading").textContent = headings[1];
  $("resourceCapabilityHeading").textContent = headings[2];
  const query = $("resourceSearch").value.trim().toLowerCase();
  const status = $("resourceStatusFilter").value;
  const items = state.catalog[state.resourceKind].filter(item => {
    const matchesQuery = !query
      || item.displayName.toLowerCase().includes(query)
      || item.name.toLowerCase().includes(query)
      || item.description.toLowerCase().includes(query);
    const reference = item.kind === "model" ? modelCredentialReference(item) : "";
    const effectiveStatus = item.kind === "model"
      ? state.credentialStatuses[reference]?.configured
        ? "ready"
        : "missing-secret"
      : item.status;
    return matchesQuery && (!status || effectiveStatus === status);
  });
  $("resourceEmpty").hidden = items.length > 0;
  $("resourceRows").innerHTML = items.map(item => `
    <tr>
      <td><div class="agent-cell"><span class="capability-icon"><svg data-icon="${resourceIcon(item.kind)}"></svg></span><div class="agent-cell-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.name)}</span></div></div></td>
      <td>${resourceSourceMarkup(item)}</td>
      <td>${resourceDetailMarkup(item)}</td>
      <td>${resourceCapabilityMarkup(item)}</td>
      <td>${resourceStatusMarkup(item)}</td>
      <td class="actions-column">${resourceActionMarkup(item)}</td>
    </tr>
  `).join("");
  injectIcons($("resourceRows"));
}

function resourceSourceMarkup(item) {
  const labels = {
    provider: "模型服务 /v1/models",
    builtin: "ksadk 内置",
    local: "工作区自定义",
    market: "市场"
  };
  return escapeHtml(labels[item.source] || item.source);
}

function resourceDetailMarkup(item) {
  if (item.kind === "model") {
    const metadata = item.contract?.metadata || {};
    const tokens = Number(metadata.context_window_tokens || 0);
    const origin = item.contract?.discovery?.contextWindow === "provider"
      ? "服务返回"
      : "ksadk 默认";
    const value = tokens >= 1000000
      ? `${(tokens / 1000000).toFixed(tokens % 1000000 ? 1 : 0)}M`
      : tokens >= 1000
      ? `${Math.round(tokens / 1000)}K`
      : `${tokens || "-"}`;
    return `<strong>${escapeHtml(value)}</strong><span class="resource-origin">${escapeHtml(origin)}</span>`;
  }
  if (item.kind === "tool") {
    return `<span class="status-badge neutral">${escapeHtml(item.contract?.group || item.category || "general")}</span>`;
  }
  return `<span class="mono">${escapeHtml(item.version)}</span>`;
}

function resourceCapabilityMarkup(item) {
  if (item.kind === "model") {
    const metadata = item.contract?.metadata || {};
    const capabilities = metadata.capabilities || {};
    const modalities = ["文字"];
    if (capabilities.multimodal_input_image) modalities.push("图片");
    if (capabilities.multimodal_input_video) modalities.push("视频");
    if (capabilities.multimodal_input_file) modalities.push("文件");
    const origin = item.contract?.discovery?.inputModalities === "provider"
      ? "服务返回"
      : "ksadk 默认";
    return `${escapeHtml(modalities.join(" + "))}<span class="resource-origin">${escapeHtml(origin)}</span>`;
  }
  if (item.kind === "tool") {
    const approval = item.contract?.approval === "always" ? "需审批" : "无需审批";
    const boundary = item.contract?.boundary || "ksadk-runtime";
    return `${escapeHtml(approval)}<span class="resource-origin">${escapeHtml(boundary)}</span>`;
  }
  return escapeHtml(item.description || "未提供说明");
}

function resourceStatusMarkup(item) {
  if (item.kind === "model") {
    const reference = modelCredentialReference(item);
    const status = state.credentialStatuses[reference];
    const configured = Boolean(status?.configured);
    return `<span class="status-badge ${configured ? "success" : "warning"}">${configured ? "凭证已配置" : "凭证未配置"}</span>`;
  }
  return `<span class="status-badge ${item.status === "ready" ? "success" : "warning"}">${escapeHtml(item.status)}</span>`;
}

function resourceActionMarkup(item) {
  if (item.kind === "model") {
    return `<button class="button secondary small" data-configure-model="${escapeHtml(item.resourceId)}" type="button">配置凭证</button>`;
  }
  if (item.kind === "mcp" && item.source === "local") {
    return `<button class="button secondary small" data-probe-mcp="${escapeHtml(item.resourceId)}" type="button">重新探测</button>`;
  }
  return '<button class="button tertiary small" type="button">查看</button>';
}

function resourceIcon(kind) {
  return { model: "cpu", tool: "wrench", mcp: "network", skill: "sparkles" }[kind] || "database";
}

function renderModelCredentialStatus(status) {
  const configured = Boolean(status?.configured);
  const source = status?.source || "missing";
  $("modelCredentialStatus").className = `credential-status ${configured ? "configured" : "missing"}`;
  $("modelCredentialStatus").querySelector(".status-dot").className = `status-dot ${configured ? "success" : "warning"}`;
  $("modelCredentialStatusTitle").textContent = configured ? "模型凭证已配置" : "模型凭证未配置";
  $("modelCredentialStatusDescription").textContent = source === "session"
    ? "凭证保存在当前 Studio 会话内存中，Runtime 已可直接使用。"
    : source === "environment"
    ? "凭证由 Studio 启动环境变量提供，可以用新的会话凭证临时覆盖。"
    : "输入 API Key 后即可在本地运行当前模型。";
  $("removeModelCredential").hidden = source !== "session";
  $("modelCredentialValue").placeholder = configured
    ? "输入新的 API Key 以覆盖当前凭证"
    : "输入新的 API Key";
}

function showModelCredentialError(title, message) {
  $("modelCredentialError").hidden = false;
  $("modelCredentialErrorTitle").textContent = title;
  $("modelCredentialErrorMessage").textContent = message;
}

async function openModelCredential(resourceId) {
  const resource = resourceById(resourceId);
  if (!resource || resource.kind !== "model") {
    showToast("模型配置不存在", "请刷新资源列表后重试。", "error");
    return;
  }
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  if (!name) {
    showToast("凭证类型暂不支持", "当前 WebUI 仅支持 env:// 模型凭证引用。", "error");
    return;
  }
  state.activeModelResourceId = resource.resourceId;
  $("modelCredentialDisplayName").textContent = resource.displayName;
  $("modelCredentialProvider").textContent = resource.contract?.provider || "openai-compatible";
  $("modelCredentialEndpoint").textContent = resource.contract?.endpointUrl || resource.contract?.baseUrl || "-";
  $("modelCredentialReference").textContent = reference;
  $("modelCredentialValue").value = "";
  $("modelCredentialError").hidden = true;
  const status = await api(`/credentials/${encodeURIComponent(name)}`);
  state.credentialStatuses[reference] = status;
  renderModelCredentialStatus(status);
  openOverlay("modelCredentialOverlay");
}

async function saveModelCredential({ testConnection = false } = {}) {
  const resource = resourceById(state.activeModelResourceId);
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  const value = $("modelCredentialValue").value;
  const existing = state.credentialStatuses[reference];
  if (!resource || !name) return;
  if (!value && !existing?.configured) {
    showModelCredentialError("请输入 API Key", "当前模型还没有可用凭证。");
    $("modelCredentialValue").focus();
    return;
  }
  const button = testConnection
    ? $("saveAndTestModelCredential")
    : $("saveModelCredential");
  $("modelCredentialError").hidden = true;
  setButtonLoading(button, true, testConnection ? "正在测试" : "正在保存");
  let saved = false;
  try {
    let status = existing;
    if (value) {
      status = await api(`/credentials/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: { value, persistence: "session" }
      });
      saved = true;
      $("modelCredentialValue").value = "";
      state.credentialStatuses[reference] = status;
      renderModelCredentialStatus(status);
      renderResources();
      populateModelSelect();
    }
    let result = null;
    if (testConnection) {
      result = await api(`/model-profiles/${encodeURIComponent(resource.resourceId)}:test`, {
        method: "POST"
      });
    }
    closeOverlay("modelCredentialOverlay");
    if (state.lastFailedMessage && state.view === "chat") {
      $("chatInput").value = state.lastFailedMessage;
      autoSizeComposer();
      $("chatInput").focus();
    }
    showToast(
      testConnection ? "模型连接测试通过" : "模型凭证已保存",
      testConnection
        ? `${resource.displayName} · ${result?.latencyMs || 0} ms`
        : "凭证已在当前 Studio 会话中生效。"
    );
  } catch (error) {
    showModelCredentialError(
      saved ? "凭证已保存，但连接测试失败" : "模型凭证配置失败",
      error.message
    );
  } finally {
    setButtonLoading(button, false);
  }
}

async function removeModelCredential() {
  const resource = resourceById(state.activeModelResourceId);
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  if (!resource || !name) return;
  const button = $("removeModelCredential");
  setButtonLoading(button, true, "正在清除");
  try {
    const status = await api(`/credentials/${encodeURIComponent(name)}`, {
      method: "DELETE"
    });
    state.credentialStatuses[reference] = status;
    renderModelCredentialStatus(status);
    renderResources();
    populateModelSelect();
    showToast("会话凭证已清除", resource.displayName);
  } catch (error) {
    showModelCredentialError("凭证清除失败", error.message);
  } finally {
    setButtonLoading(button, false);
  }
}

async function probeMcp(resourceId) {
  try {
    const resource = await api(`/catalog/mcp-servers/${encodeURIComponent(resourceId)}:probe?timeoutSeconds=15`, {
      method: "POST"
    });
    await refreshCatalog();
    showToast("MCP 探测完成", `已发现 ${resource.health?.toolCount || 0} 个 Tool。`);
  } catch (error) {
    await refreshCatalog();
    showToast("MCP 探测失败", error.message, "error");
  }
}

function renderSkillDiscovery() {
  const candidates = state.skillDiscovery?.candidates || [];
  $("skillDiscoveryList").innerHTML = candidates.length
    ? candidates.map(candidate => {
      const risk = candidate.risk || {};
      const valid = ["ready", "conflict"].includes(candidate.status);
      const details = candidate.diagnostics?.map(item => item.message).join("；")
        || `${candidate.fileCount || 0} 个文件 · ${formatByteCount(candidate.totalBytes || 0)}`;
      return `<label class="skill-candidate ${valid ? "" : "invalid"}">
        <input type="radio" name="skillCandidate" data-skill-candidate="${escapeHtml(candidate.candidateId)}" ${valid ? "" : "disabled"}>
        <span class="capability-icon"><svg data-icon="sparkles"></svg></span>
        <span class="selection-item-copy"><strong>${escapeHtml(candidate.displayName || candidate.name)}</strong><span>${escapeHtml(candidate.path)} · ${escapeHtml(candidate.version || "版本无效")}</span><small>${escapeHtml(details)}</small></span>
        <span class="status-badge ${candidate.status === "ready" ? "success" : "warning"}">${escapeHtml(candidate.status)}${risk.requiresReview ? " · 需复核" : ""}</span>
      </label>`;
    }).join("")
    : '<div class="trace-stage-empty compact"><p>安全默认目录中没有发现 Skill。</p></div>';
  $("commitDiscoveredSkill").disabled = true;
  injectIcons($("skillDiscoveryOverlay"));
}

function formatByteCount(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

async function discoverWorkspaceSkills() {
  const button = $("discoverSkills");
  $("skillDiscoveryError").hidden = true;
  const scanPaths = $("skillScanPaths").value
    .split(",")
    .map(item => item.trim())
    .filter(Boolean);
  setButtonLoading(button, true, "正在扫描");
  try {
    state.skillDiscovery = await api("/catalog/skills:discover", {
      method: "POST",
      body: { scanPaths }
    });
    renderSkillDiscovery();
  } catch (error) {
    $("skillDiscoveryError").hidden = false;
    $("skillDiscoveryErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

async function commitSelectedSkill(overwrite = false) {
  const selected = document.querySelector("[data-skill-candidate]:checked");
  const token = state.skillDiscovery?.inspectionToken;
  if (!selected || !token) return;
  const candidate = state.skillDiscovery.candidates.find(
    item => item.candidateId === selected.dataset.skillCandidate
  );
  const button = $("commitDiscoveredSkill");
  setButtonLoading(button, true, "正在导入");
  try {
    const created = await api(`/catalog/skills/discoveries/${encodeURIComponent(token)}:commit`, {
      method: "POST",
      body: { candidateId: selected.dataset.skillCandidate, overwrite }
    });
    await refreshCatalog();
    closeOverlay("skillDiscoveryOverlay");
    state.skillDiscovery = null;
    showToast("Skill 已安装", `${created.displayName || created.name} · ${created.version}`);
  } catch (error) {
    if (error.code === "SKILL_IMPORT_CONFLICT" && !overwrite) {
      if (window.confirm(`Skill ${candidate?.displayName || candidate?.name || ""} 已存在，是否覆盖并把旧版本移入回收站？`)) {
        await commitSelectedSkill(true);
      }
      return;
    }
    $("skillDiscoveryError").hidden = false;
    $("skillDiscoveryErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

async function savePythonTool(event) {
  event.preventDefault();
  const form = $("pythonToolForm");
  if (!form.reportValidity()) return;
  const button = $("savePythonTool");
  $("pythonToolError").hidden = true;
  setButtonLoading(button, true, "正在保存");
  try {
    const name = $("pythonToolName").value.trim();
    const created = await api("/catalog/tools", {
      method: "POST",
      body: {
        displayName: name,
        category: "custom",
        contract: {
          name,
          version: "1.0.0",
          description: $("pythonToolDescription").value.trim(),
          inputSchema: { type: "object", properties: {} },
          outputSchema: { type: "object", properties: {} },
          executor: "python",
          sourcePath: $("pythonToolSource").value.trim(),
          callableName: $("pythonToolCallable").value.trim(),
          sideEffect: "none",
          approval: "never"
        }
      }
    });
    await refreshCatalog();
    form.reset();
    closeOverlay("pythonToolOverlay");
    showToast("Python Tool 已保存", `${created.displayName} · SHA-256 已锁定`);
  } catch (error) {
    $("pythonToolError").hidden = false;
    $("pythonToolErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

function addResource() {
  if (state.resourceKind === "model") {
    const model = state.catalog.model[0];
    if (model) openModelCredential(model.resourceId).catch(handleGlobalError);
    return;
  }
  if (state.resourceKind === "mcp") {
    openOverlay("mcpOverlay");
    return;
  }
  if (state.resourceKind === "skill") {
    state.skillDiscovery = null;
    $("skillDiscoveryList").innerHTML = '<div class="trace-stage-empty compact"><p>点击扫描候选。</p></div>';
    $("skillDiscoveryError").hidden = true;
    $("commitDiscoveredSkill").disabled = true;
    openOverlay("skillDiscoveryOverlay");
    return;
  }
  if (state.resourceKind === "tool") {
    $("pythonToolError").hidden = true;
    openOverlay("pythonToolOverlay");
    return;
  }
  showToast("资源创建入口正在收敛", "当前可在 Agent 创建流程中选择已有资源。");
}
