import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ControlButton,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { CheckCircle2, Code2, Cpu, Maximize2, MessagesSquare, Network, Plus } from "lucide-react";
import { apiFetch } from "../api";
import { AgentAvatar, type AgentAppearance } from "../components/AgentAvatar";

interface AgentSummary { metadata: { id: string; name: string; revision?: number; appearance?: AgentAppearance } }
interface RunItem { id: string; agentId: string; status: string; startedAt?: string; input?: string }

type PipelineIcon = "input" | "runtime" | "model" | "capabilities" | "output";
interface PipelineStep {
  id: string;
  title: string;
  subtitle: string;
  icon: PipelineIcon;
  incomingLabel?: string;
}
interface PipelineNodeData extends Record<string, unknown> {
  title: string;
  subtitle: string;
  icon: PipelineIcon;
}
type PipelineGraphNode = Node<PipelineNodeData, "pipeline">;
type PipelineGraphEdge = Edge<Record<string, never>, "smoothstep">;

const NODE_WIDTH = 204;
const NODE_HEIGHT = 96;
const COLUMN_GAP = 84;
const ROW_GAP = 76;

const pipelineIcons = {
  input: MessagesSquare,
  runtime: Code2,
  model: Cpu,
  capabilities: Network,
  output: CheckCircle2,
};

function shortId(value: string, head = 34): string {
  if (!value) return "-";
  return value.length <= head ? value : `${value.slice(0, head)}…`;
}
function formatDate(value?: string): string {
  if (!value) return "刚刚";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

function PipelineGraphCard({ data }: NodeProps<PipelineGraphNode>) {
  const Icon = pipelineIcons[data.icon];
  return (
    <div className="pipeline-node-card">
      <Handle id="top" type="target" position={Position.Top} />
      <Handle id="top-out" type="source" position={Position.Top} />
      <Handle id="right" type="source" position={Position.Right} />
      <Handle id="right-in" type="target" position={Position.Right} />
      <Handle id="bottom" type="source" position={Position.Bottom} />
      <Handle id="bottom-in" type="target" position={Position.Bottom} />
      <Handle id="left" type="target" position={Position.Left} />
      <Handle id="left-out" type="source" position={Position.Left} />
      <span className="pipeline-node-icon"><Icon size={15} /></span>
      <span><strong>{data.title}</strong><small>{data.subtitle}</small></span>
    </div>
  );
}

const nodeTypes = { pipeline: PipelineGraphCard };

function layoutPipeline(steps: PipelineStep[], width: number): {
  nodes: PipelineGraphNode[];
  edges: PipelineGraphEdge[];
} {
  const usableWidth = Math.max(560, width - 96);
  const maxColumns = usableWidth >= 1120 ? 3 : usableWidth >= 720 ? 3 : 2;
  const columns = Math.max(1, Math.min(maxColumns, steps.length));
  const gridWidth = columns * NODE_WIDTH + (columns - 1) * COLUMN_GAP;
  const startX = Math.max(48, (Math.max(width, 560) - gridWidth) / 2);
  const positions = steps.map((_, index) => {
    const row = Math.floor(index / columns);
    const offset = index % columns;
    const column = row % 2 === 0 ? offset : columns - 1 - offset;
    return {
      row,
      column,
      x: startX + column * (NODE_WIDTH + COLUMN_GAP),
      y: 56 + row * (NODE_HEIGHT + ROW_GAP),
    };
  });

  const nodes = steps.map((step, index) => ({
    id: step.id,
    type: "pipeline" as const,
    position: { x: positions[index].x, y: positions[index].y },
    data: { title: step.title, subtitle: step.subtitle, icon: step.icon },
    style: { width: NODE_WIDTH, height: NODE_HEIGHT },
  }));

  const edges = steps.slice(1).map((step, index) => {
    const source = positions[index];
    const target = positions[index + 1];
    const rowChange = source.row !== target.row;
    const movingRight = target.column > source.column;
    return {
      id: `edge-${steps[index].id}-${step.id}`,
      source: steps[index].id,
      target: step.id,
      sourceHandle: rowChange ? "bottom" : movingRight ? "right" : "left-out",
      targetHandle: rowChange ? "top" : movingRight ? "left" : "right-in",
      type: "smoothstep" as const,
      label: step.incomingLabel,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
    };
  });

  return { nodes, edges };
}

function useCanvasWidth() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(960);

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return undefined;
    const update = (nextWidth: number) => {
      if (nextWidth > 0) setWidth(nextWidth);
    };
    update(element.getBoundingClientRect().width);
    const observer = new ResizeObserver(entries => {
      update(entries[0]?.contentRect.width || 0);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return { containerRef, width };
}

function PipelineCanvas({ steps }: { steps: PipelineStep[] }) {
  const { containerRef, width } = useCanvasWidth();
  const [instance, setInstance] = useState<ReactFlowInstance<PipelineGraphNode, PipelineGraphEdge> | null>(null);
  const graph = useMemo(() => layoutPipeline(steps, width), [steps, width]);

  useEffect(() => {
    if (!instance) return;
    requestAnimationFrame(() => instance.fitView({ padding: 0.16, duration: 260 }));
  }, [graph, instance]);

  return (
    <div
      ref={containerRef}
      className="orchestration-graph"
      role="application"
      aria-label="执行链路画布"
      data-layout="adaptive-serpentine"
      data-background="plain"
    >
      <ReactFlow<PipelineGraphNode, PipelineGraphEdge>
        nodes={graph.nodes}
        edges={graph.edges}
        nodeTypes={nodeTypes}
        onInit={setInstance}
        fitView
        fitViewOptions={{ padding: 0.16 }}
        minZoom={0.55}
        maxZoom={1.35}
        nodesConnectable={false}
        elementsSelectable
        panOnScroll
        selectionOnDrag={false}
        proOptions={{ hideAttribution: true }}
      >
        <Controls showFitView={false} position="bottom-right">
          <ControlButton
            aria-label="适应画布"
            title="适应画布"
            onClick={() => instance?.fitView({ padding: 0.16, duration: 260 })}
          >
            <Maximize2 size={14} />
          </ControlButton>
        </Controls>
      </ReactFlow>
    </div>
  );
}

export function OrchestrationPage({ currentAgentId, agents, onSelectAgent, onCreate }: {
  currentAgentId: string;
  agents: AgentSummary[];
  onSelectAgent: (id: string) => void;
  onCreate: () => void;
}) {
  void agents;
  void onSelectAgent; // 切换 Agent 统一走全局头部选择器。
  const [draft, setDraft] = useState<any>(null);
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [catalog, setCatalog] = useState<any[]>([]);

  useEffect(() => {
    apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json())
      .then(d => setCatalog(d.items || [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (!currentAgentId) { setDraft(null); setRuns([]); return; }
    let cancelled = false;
    Promise.all([
      apiFetch(`/api/v1/agents/${encodeURIComponent(currentAgentId)}`).then(r => r.json()),
      apiFetch("/api/v1/runs?limit=200").then(r => r.json()).catch(() => ({ items: [] })),
    ]).then(([detail, runList]) => {
      if (cancelled) return;
      setDraft(detail.draft || null);
      setRuns((runList.items || []).filter((r: RunItem) => r.agentId === currentAgentId));
    }).catch(() => { if (!cancelled) { setDraft(null); setRuns([]); } });
    return () => { cancelled = true; };
  }, [currentAgentId]);

  const bindings = draft?.spec?.bindings || {};
  const runtimeType = draft?.spec?.runtime?.type
    || draft?.metadata?.labels?.["agentkit.ksyun.com/framework"]
    || "runtime";
  const strategy = draft?.spec?.execution?.strategy || "direct";
  const model = catalog.find(r => r.resourceId === bindings.modelProfileId);
  const capCount = (bindings.tools?.length || 0) + (bindings.mcpServers?.length || 0) + (bindings.skills?.length || 0);
  const recentRuns = runs.slice(-5).reverse();
  const pipelineSteps = useMemo<PipelineStep[]>(() => {
    const steps: PipelineStep[] = [
      { id: "input", icon: "input", title: "任务输入", subtitle: "用户消息与会话上下文" },
      {
        id: "runtime",
        icon: "runtime",
        title: `${runtimeType} RuntimeAdapter`,
        subtitle: `${strategy} · 本地执行`,
        incomingLabel: "调度",
      },
      {
        id: "model",
        icon: "model",
        title: model?.displayName || bindings.modelProfileId || "未绑定模型",
        subtitle: "Model Profile",
        incomingLabel: "调用模型",
      },
    ];
    if (capCount > 0) {
      steps.push({
        id: "capabilities",
        icon: "capabilities",
        title: `${capCount} 个能力绑定`,
        subtitle: `${bindings.tools?.length || 0} Tool · ${bindings.mcpServers?.length || 0} MCP · ${bindings.skills?.length || 0} Skill`,
        incomingLabel: "加载能力",
      });
    }
    steps.push({
      id: "output",
      icon: "output",
      title: "结构化输出",
      subtitle: "返回会话并写入 Trace",
      incomingLabel: "写回结果",
    });
    return steps;
  }, [bindings, capCount, model?.displayName, runtimeType, strategy]);

  return (
    <div className="page-container orchestration-page" data-layout={draft ? "workbench" : "document"}>
      <header className="page-header">
        <div><h1>任务编排</h1><p>把当前 Agent 的声明式配置映射为可检查的执行链路与端云路由。</p></div>
      </header>
      {!draft ? (
        <div className="orchestration-empty">
          <span className="empty-icon"><Network size={24} /></span>
          <h2>先选择或创建一个 Agent</h2>
          <p>编排视图会读取 Agent Revision、RuntimeRef 和能力绑定生成真实执行链路。</p>
          <button className="button accent" type="button" onClick={onCreate}><Plus size={15} /><span>创建 Agent</span></button>
        </div>
      ) : (
        <div className="orchestration-workbench">
          <section className="orchestration-canvas">
            <div className="section-heading">
              <AgentAvatar name={draft.metadata?.name || "Agent"} appearance={draft.metadata?.appearance} size="md" />
              <div className="section-heading-copy">
                <h2>{draft.metadata?.name}</h2>
                <p>{draft.metadata?.id} · revision {draft.metadata?.revision} · {runtimeType} · 本地执行</p>
              </div>
            </div>
            <PipelineCanvas steps={pipelineSteps} />
          </section>
          <aside className="orchestration-aside">
            <section className="orchestration-aside-section">
              <div className="aside-title">路由与约束</div>
              <dl>
                <div><dt>Revision</dt><dd>r{draft.metadata?.revision}</dd></div>
                <div><dt>执行位置</dt><dd>Edge · Local</dd></div>
                <div><dt>Runtime</dt><dd>{runtimeType}</dd></div>
                <div><dt>策略</dt><dd>{strategy}</dd></div>
                <div><dt>最大步骤</dt><dd>{draft.spec?.execution?.maxSteps || "-"}</dd></div>
                <div><dt>云端</dt><dd>未连接</dd></div>
              </dl>
            </section>
            <div className="aside-divider" />
            <section className="orchestration-aside-section">
              <div className="aside-title">最近调度</div>
              <div className="dispatch-log">
                {recentRuns.length === 0 ? (
                  <div className="dispatch-log-empty">当前 Agent 还没有运行记录</div>
                ) : recentRuns.map(run => (
                  <div key={run.id} className="dispatch-log-row">
                    <span className={`dispatch-status ${String(run.status).toLowerCase()}`} />
                    <span><strong>{shortId(run.input || "Run")}</strong><small>{formatDate(run.startedAt)} · {run.status}</small></span>
                  </div>
                ))}
              </div>
            </section>
          </aside>
        </div>
      )}
    </div>
  );
}
