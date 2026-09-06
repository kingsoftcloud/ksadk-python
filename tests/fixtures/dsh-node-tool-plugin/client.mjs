// DSH UI sandbox client bundle for the fixture plugin — ModuleLoader format.
//
// Registered via window.__ModuleLoader__.load(). The factory returns a Cordis
// plugin whose apply() renders a React UI into the sandbox slot surface and
// drives the fixture_echo tool through the AgentKitDshUI channel. The E2E
// host asserts on data-fixture-status.
window.__ModuleLoader__.load({
  id: "@ksadk-test/dsh-node-tool-plugin",
  factory: (require) => {
    const React = require("react");
    const { createRoot } = require("react-dom/client");
    const { useState, useCallback } = React;

    function EchoPanel() {
      const [message, setMessage] = useState("sandbox-roundtrip");
      const [result, setResult] = useState(null);
      const [running, setRunning] = useState(false);
      const [error, setError] = useState(null);

      const runEcho = useCallback(async () => {
        setRunning(true);
        setError(null);
        try {
          const res = await window.AgentKitDshUI.callTool(
            "fixture_echo",
            { message },
            { callId: "echo_" + Date.now().toString(36), deadlineMs: 10000 },
          );
          if (res && res.isError === false && res.structuredContent) {
            setResult(res.structuredContent.message);
          } else {
            setError("调用失败: " + JSON.stringify(res?.error || res));
          }
        } catch (e) {
          setError(String(e?.message || e));
        } finally {
          setRunning(false);
        }
      }, [message]);

      return React.createElement(
        "div",
        { style: { fontFamily: "system-ui, sans-serif", padding: 24, display: "flex", flexDirection: "column", gap: 16, height: "100%", boxSizing: "border-box", background: "#f8fafc" } },
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10 } },
          React.createElement("div", { style: { width: 36, height: 36, borderRadius: 8, background: "linear-gradient(135deg,#6366f1,#8b5cf6)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 15 } }, "K"),
          React.createElement("div", null,
            React.createElement("div", { style: { fontWeight: 600, fontSize: 15, color: "#0f172a" } }, "DSH Echo 插件"),
            React.createElement("div", { style: { fontSize: 12, color: "#64748b" } }, "sandbox iframe · ModuleLoader + React")
          )
        ),
        React.createElement("div", { style: { background: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", padding: 16, display: "flex", flexDirection: "column", gap: 10, boxShadow: "0 1px 3px rgba(0,0,0,0.06)" } },
          React.createElement("label", { style: { fontSize: 13, fontWeight: 500, color: "#334155" } }, "消息内容"),
          React.createElement("input", {
            value: message,
            onChange: (e) => setMessage(e.target.value),
            style: { border: "1px solid #cbd5e1", borderRadius: 6, padding: "8px 10px", fontSize: 14, outline: "none" },
          }),
          React.createElement("button", {
            onClick: runEcho,
            disabled: running || !message,
            style: { background: running ? "#94a3b8" : "#6366f1", color: "#fff", border: "none", borderRadius: 6, padding: "9px 14px", fontSize: 13, fontWeight: 600, cursor: running ? "default" : "pointer" },
          }, running ? "调用中…" : "调用 fixture_echo")
        ),
        result !== null && React.createElement("div", { style: { background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: 10, padding: 14 } },
          React.createElement("div", { style: { fontSize: 12, fontWeight: 600, color: "#166534", marginBottom: 6 } }, "调用结果"),
          React.createElement("pre", { style: { margin: 0, fontSize: 13, color: "#14532d", whiteSpace: "pre-wrap", wordBreak: "break-all" } }, result)
        ),
        error !== null && React.createElement("div", { style: { background: "#fef2f2", border: "1px solid #fecaca", borderRadius: 10, padding: 14 } },
          React.createElement("div", { style: { fontSize: 12, fontWeight: 600, color: "#991b1b", marginBottom: 6 } }, "错误"),
          React.createElement("pre", { style: { margin: 0, fontSize: 12, color: "#7f1d1d", whiteSpace: "pre-wrap" } }, error)
        )
      );
    }

    return {
      inject: [],
      apply(ctx) {
        // Mount the React panel into the sandbox slot surface.
        const container = document.createElement("div");
        container.style.cssText = "flex:1;overflow:auto;";
        const root = createRoot(container);
        root.render(React.createElement(EchoPanel));
        ctx.slots.register("main", { element: container, order: 0 });
        ctx.effect(() => () => root.unmount(), "dsh-echo: unmount");

        // E2E assertions: opaque origin, listTools + callTool round-trip.
        (async () => {
          const report = { parentReadable: true, cookieReadable: true, methods: [] };
          try {
            try { void window.parent.document.body; } catch (_) { report.parentReadable = false; }
            try { void document.cookie; } catch (_) { report.cookieReadable = false; }
            await window.AgentKitDshUI.ready;
            const tools = await window.AgentKitDshUI.listTools();
            report.methods.push("listTools");
            const res = await window.AgentKitDshUI.callTool(
              "fixture_echo",
              { message: "sandbox-roundtrip" },
              { callId: "fixture_echo_call", deadlineMs: 10000 },
            );
            report.methods.push("callTool");
            Object.assign(report, { tools, result: res });
            const toolIds = (tools.tools || []).map((t) => t.id);
            const ok =
              report.parentReadable === false &&
              report.cookieReadable === false &&
              toolIds.includes("fixture_echo") &&
              res && res.isError === false &&
              res.structuredContent && res.structuredContent.message === "sandbox-roundtrip";
            document.body.dataset.fixtureStatus = ok ? "passed" : "failed";
            if (!ok) {
              document.body.dataset.fixtureDetail = JSON.stringify({ toolIds, result: res });
            }
          } catch (error) {
            report.error = String(error?.message || error);
            document.body.dataset.fixtureStatus = "failed";
            document.body.dataset.fixtureDetail = JSON.stringify({ caught: true, error: report.error });
          }
          window.__dshUiSandboxFixture = report;
        })();
      },
    };
  },
});
