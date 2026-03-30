const state = {
  bootstrap: null,
  sessions: [],
  sessionId: null,
  events: [],
  pendingFiles: [],
  thinkingEl: null,
  responseEl: null,
  abortController: null,
};

const dom = {
  agentName: document.getElementById("agent-name"),
  agentMeta: document.getElementById("agent-meta"),
  sessionList: document.getElementById("session-list"),
  messageList: document.getElementById("message-list"),
  composerForm: document.getElementById("composer-form"),
  messageInput: document.getElementById("message-input"),
  stopButton: document.getElementById("stop-button"),
  newSessionButton: document.getElementById("new-session-button"),
  attachmentInput: document.getElementById("attachment-input"),
  attachmentList: document.getElementById("attachment-list"),
  composerStatus: document.getElementById("composer-status"),
};

async function postAction(actionName, payload = {}) {
  const response = await fetch(`/agentengine/api/v1/${actionName}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.Code !== 0) {
    throw new Error(data.Message || `Action ${actionName} failed`);
  }
  return data.Data;
}

function setStatus(text = "", hidden = false) {
  dom.composerStatus.textContent = text;
  dom.composerStatus.classList.toggle("hidden", hidden || !text);
}

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderMarkdown(text) {
  const blocks = text.split("```");
  return blocks
    .map((block, index) => {
      if (index % 2 === 1) {
        return `<pre><code>${escapeHtml(block.trim())}</code></pre>`;
      }
      return escapeHtml(block).replaceAll("\n", "<br>");
    })
    .join("");
}

function eventText(event) {
  const content = event.Content || {};
  const parts = content.parts || content.Parts || [];
  return parts
    .map((part) => part.text || part.Text || "")
    .filter(Boolean)
    .join("");
}

function appendBubble({ role, text, meta = "", thinking = false }) {
  const wrapper = document.createElement("article");
  wrapper.className = `bubble ${role}${thinking ? " thinking" : ""}`;
  wrapper.innerHTML = `
    <div class="bubble-header">
      <div class="bubble-role">${role}</div>
      <div class="event-meta">${meta}</div>
    </div>
    <div class="bubble-content">${renderMarkdown(text)}</div>
  `;
  dom.messageList.appendChild(wrapper);
  dom.messageList.scrollTop = dom.messageList.scrollHeight;
  return wrapper;
}

function renderSessions() {
  dom.sessionList.innerHTML = "";
  if (!state.sessions.length) {
    dom.sessionList.innerHTML = '<div class="empty-state">No sessions yet.</div>';
    return;
  }
  for (const session of state.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item${session.SessionId === state.sessionId ? " active" : ""}`;
    button.innerHTML = `
      <div class="session-title">${session.SessionId.slice(0, 12)}</div>
      <div class="session-meta">v${session.Version} · ${new Date(session.UpdatedAt * 1000).toLocaleString()}</div>
    `;
    button.addEventListener("click", () => openSession(session.SessionId));
    dom.sessionList.appendChild(button);
  }
}

function renderEvents() {
  dom.messageList.innerHTML = "";
  if (!state.events.length) {
    dom.messageList.innerHTML = '<div class="empty-state">Start a conversation with this agent.</div>';
    return;
  }
  for (const event of state.events) {
    const text = eventText(event);
    if (!text) continue;
    appendBubble({
      role: event.Author === "user" ? "user" : "assistant",
      text,
      meta: event.EventType || "",
    });
  }
}

async function loadBootstrap() {
  state.bootstrap = await postAction("GetAgentUiBootstrap", {});
  dom.agentName.textContent = state.bootstrap.Agent.Name;
  const capabilities = Object.entries(state.bootstrap.Capabilities)
    .map(([key, value]) => `${key}:${Array.isArray(value) ? value.join(",") : value}`)
    .join(" · ");
  dom.agentMeta.textContent = capabilities;
}

async function refreshSessions() {
  state.sessions = (await postAction("ListSessions", { AgentId: state.bootstrap.Agent.AgentId, UserId: "user" })).Sessions || [];
  renderSessions();
}

async function createSession() {
  const data = await postAction("CreateSession", {
    AgentId: state.bootstrap.Agent.AgentId,
    UserId: "user",
  });
  state.sessionId = data.Session.SessionId;
  state.events = [];
  renderEvents();
  await refreshSessions();
}

async function openSession(sessionId) {
  state.sessionId = sessionId;
  const data = await postAction("ListSessionEvents", { SessionId: sessionId });
  state.events = data.Events || [];
  renderSessions();
  renderEvents();
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
    chip.textContent = `${file.name} (${file.size} bytes)`;
    dom.attachmentList.appendChild(chip);
  }
}

async function handleCommand(text) {
  if (text === "/new") {
    await createSession();
    dom.messageInput.value = "";
    return true;
  }
  if (text === "/clear") {
    dom.messageList.innerHTML = "";
    dom.messageInput.value = "";
    return true;
  }
  if (text === "/help") {
    appendBubble({ role: "assistant", text: "Available commands: /new, /clear, /stop, /help, /attach" });
    dom.messageInput.value = "";
    return true;
  }
  if (text === "/stop") {
    if (state.abortController) {
      state.abortController.abort();
    }
    dom.messageInput.value = "";
    return true;
  }
  return false;
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
    setStatus(`Unknown command: ${text}`, false);
    return;
  }

  const attachmentText = state.pendingFiles.length
    ? `\n\n[Attachments]\n${state.pendingFiles.map((file) => `- ${file.name}`).join("\n")}`
    : "";
  const finalText = `${text}${attachmentText}`.trim();
  appendBubble({ role: "user", text: finalText });
  setStatus("Streaming response...", false);

  state.thinkingEl = null;
  state.responseEl = appendBubble({ role: "assistant", text: "" });
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
      Messages: [{ role: "user", content: finalText }],
    }),
    signal: state.abortController.signal,
  });

  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  let currentEvent = "";
  let currentData = "";
  let responseText = "";

  const flushEvent = () => {
    if (!currentEvent || !currentData) return;
    const payload = JSON.parse(currentData);
    if (currentEvent === "response.reasoning.delta") {
      if (!state.thinkingEl) {
        state.thinkingEl = appendBubble({ role: "assistant", text: "", thinking: true, meta: "thinking" });
      }
      const thinkingContent = state.thinkingEl.querySelector(".bubble-content");
      thinkingContent.innerHTML = renderMarkdown((thinkingContent.textContent || "") + payload.delta);
    } else if (currentEvent === "response.output_text.delta") {
      responseText += payload.delta;
      responseContent.innerHTML = renderMarkdown(responseText);
    } else if (currentEvent === "response.completed") {
      state.events = [];
    }
    currentEvent = "";
    currentData = "";
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
        currentData = line.slice(6).trim();
      } else if (!line.trim()) {
        flushEvent();
      }
    }

    if (done) {
      flushEvent();
      break;
    }
  }

  dom.messageInput.value = "";
  clearPendingFiles();
  dom.stopButton.disabled = true;
  state.abortController = null;
  setStatus("", true);
  await refreshSessions();
}

async function bootstrap() {
  try {
    await loadBootstrap();
    await refreshSessions();
    if (!state.sessions.length) {
      await createSession();
    } else {
      await openSession(state.sessions[0].SessionId);
    }
  } catch (error) {
    setStatus(error.message || String(error), false);
  }
}

dom.composerForm.addEventListener("submit", (event) => {
  sendMessage(event).catch((error) => {
    if (error.name === "AbortError") {
      setStatus("Generation stopped.", false);
    } else {
      setStatus(error.message || String(error), false);
    }
    dom.stopButton.disabled = true;
    state.abortController = null;
  });
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
  state.pendingFiles = Array.from(event.target.files || []);
  showPendingFiles();
});

bootstrap();
