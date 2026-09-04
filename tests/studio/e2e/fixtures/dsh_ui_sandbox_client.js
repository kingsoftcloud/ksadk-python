(async () => {
  "use strict";
  const report = {
    parentReadable: true,
    cookieReadable: true,
    networkBlocked: false,
    methods: [],
  };
  try {
    void window.parent.document.body;
  } catch (_error) {
    report.parentReadable = false;
  }
  try {
    void document.cookie;
  } catch (_error) {
    report.cookieReadable = false;
  }
  try {
    await fetch("/probe", { credentials: "include" });
  } catch (_error) {
    report.networkBlocked = true;
  }

  try {
    await window.AgentKitDshUI.ready;
    const tools = await window.AgentKitDshUI.listTools();
    report.methods.push("listTools");
    const callId = "fixture_call_1";
    const call = window.AgentKitDshUI.callTool(
      "@example/read",
      { query: "sandbox" },
      { callId, deadlineMs: 5000 },
    );
    report.methods.push("callTool");
    const cancelled = await window.AgentKitDshUI.cancelTool(callId);
    report.methods.push("cancelTool");
    const result = await call;
    Object.assign(report, { tools, cancelled, result });
    const passed =
      report.parentReadable === false &&
      report.cookieReadable === false &&
      report.networkBlocked === true &&
      tools.tools[0].id === "@example/read" &&
      cancelled.cancelled === true &&
      result.cancelled === true;
    document.body.dataset.fixtureStatus = passed ? "passed" : "failed";
  } catch (error) {
    report.error = String(error && error.message ? error.message : error);
    document.body.dataset.fixtureStatus = "failed";
  }
  window.__dshUiSandboxFixture = report;
})();
