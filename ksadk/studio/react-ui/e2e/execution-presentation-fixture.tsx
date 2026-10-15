import { createRoot } from "react-dom/client";
import { useState } from "react";
import { HarnessActivity, type Activity } from "../src/components/HarnessActivity";
import { LiveThinking } from "../src/components/LiveThinking";
import { CompactHarnessTimeline } from "../src/components/CompactHarnessTimeline";
import "@kingsoftcloud/ksadk-web/styles";
import "../src/index.css";
import "../src/kingdesign.css";
import "../src/studio-refinement.css";
import "../src/layout-simplification.css";

const noop = () => {};
const activities: Activity[] = [
  { id: "plan", kind: "step", label: "更新计划", status: "completed", details: [], tools: [{ id: "p", callId: "p", status: "completed", display: {
    version: "tool-display/v1", category: "todo", summary: "更新计划", todos: [
      { id: "one", content: "读取失败用例", status: "completed", priority: "high" },
      { id: "two", content: "修复并验证", status: "in_progress", priority: "high" },
    ],
  } }] },
  { id: "tools", kind: "step", label: "运行检查", status: "failed", details: [], tools: [
    { id: "terminal", callId: "terminal", status: "failed", display: { version: "tool-display/v1", category: "terminal", summary: "运行命令：pytest", exit_code: 1, truncated: true, total_chars: 42000, output: Array.from({ length: 180 }, (_, i) => `line ${i}: fixture check output`).join("\n") } },
    { id: "edit", callId: "edit", status: "completed", display: { version: "tool-display/v1", category: "edit", path: "app.py", summary: "编辑文件：app.py", diff: "--- app.py before\n+++ app.py after\n@@ -1 +1 @@\n-decision = 'approved'\n+decision = observed_decision\n" } },
  ] },
];
function Fixture() {
  const [streaming, setStreaming] = useState(true);
  const [count, setCount] = useState(0);
  const messages = Array.from({ length: 400 }, (_, i) => ({ id: `m-${i}`, role: (i % 2 ? "model" : "user") as "model" | "user", timestamp: i, content: `历史消息 ${i}：独立执行记录。`, runId: `fixture-${i}` }));
  messages.push({ id: "live", role: "model", timestamp: 400, content: `当前输出 ${count}`, runId: "fixture-live" });
  return <main className="ksadk-web" style={{ height: "100vh", display: "flex", flexDirection: "column", maxWidth: 1000, margin: "auto" }}>
    <div style={{ padding: 12 }}><button onClick={() => setStreaming(false)}>结束流式</button><button onClick={() => setCount(n => n + 1)}>追加输出</button></div>
    <LiveThinking text={`核对审批决策和工具状态。\n正在验证事件投影 ${count}`} streaming={streaming} reasoningTokens={128} />
    <HarnessActivity streaming={false} fallback={activities} />
    <CompactHarnessTimeline sessionId="fixture" messages={messages} agentName="执行验证" isMobile={false} isStreaming={streaming} activity={null}
      onDeleteFeedback={noop} onSubmitFeedback={noop} onRespondToApproval={noop} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
