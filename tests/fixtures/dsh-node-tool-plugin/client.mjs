// DSH UI sandbox client bundle for the fixture plugin.
//
// Runs inside the opaque-origin iframe created by
// render_dsh_ui_sandbox_document().  Uses the injected window.AgentKitDshUI
// channel to list tools and invoke fixture_echo, then reports pass/fail via
// a data attribute that the Playwright host asserts on.
(async () => {
  "use strict";
  const report = {
    parentReadable: true,
    cookieReadable: true,
    methods: [],
  };
  try {
    // Opaque-origin sandbox assertions: parent and cookie must be unreachable.
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

    // Wait for the host to inject the capability channel.
    await window.AgentKitDshUI.ready;
    const tools = await window.AgentKitDshUI.listTools();
    report.methods.push("listTools");

    const callId = "fixture_echo_call";
    const call = window.AgentKitDshUI.callTool(
      "fixture_echo",
      { message: "sandbox-roundtrip" },
      { callId, deadlineMs: 10000 },
    );
    report.methods.push("callTool");
    const result = await call;
    Object.assign(report, { tools, result });

    const passed =
      report.parentReadable === false &&
      report.cookieReadable === false &&
      tools.tools.some((tool) => tool.id === "fixture_echo") &&
      result.ok === true &&
      result.result?.message === "sandbox-roundtrip";
    document.body.dataset.fixtureStatus = passed ? "passed" : "failed";
  } catch (error) {
    report.error = String(error && error.message ? error.message : error);
    document.body.dataset.fixtureStatus = "failed";
  }
  window.__dshUiSandboxFixture = report;
})();
