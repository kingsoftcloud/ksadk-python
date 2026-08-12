import * as Tooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";
import {
  Activity,
  Bot,
  CloudUpload,
  Cpu,
  Folder,
  MessagesSquare,
  Network,
  Package,
  Settings,
  Sparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { ResourceKind } from "../pages/ResourcesPage";

export const NAVIGATION_RAIL_PREFERENCE_KEY = "agentkit.studio.rail-expanded";

export type NavigationView =
  | "agents"
  | "create"
  | "agent-detail"
  | "conversations"
  | "resources"
  | "builds"
  | "deployments"
  | "observability"
  | "runtime-resources"
  | "orchestration";

interface NavigationItem {
  id: NavigationView;
  label: string;
  icon: LucideIcon;
  kind?: ResourceKind;
}

const NAVIGATION_GROUPS: Array<{ group: string; items: NavigationItem[] }> = [
  {
    group: "构建与运行",
    items: [
      { id: "agents", label: "Agent", icon: Bot },
      { id: "conversations", label: "会话", icon: MessagesSquare },
      { id: "builds", label: "构建", icon: Package },
      { id: "deployments", label: "部署", icon: CloudUpload },
    ],
  },
  {
    group: "工程资源",
    items: [
      { id: "resources", label: "模型", icon: Cpu, kind: "model" },
      { id: "resources", label: "Tool", icon: Wrench, kind: "tool" },
      { id: "resources", label: "MCP", icon: Network, kind: "mcp" },
      { id: "resources", label: "Skill", icon: Sparkles, kind: "skill" },
    ],
  },
  {
    group: "治理",
    items: [
      { id: "observability", label: "可观测", icon: Activity },
      { id: "runtime-resources", label: "运行资源", icon: Cpu },
      { id: "orchestration", label: "任务编排", icon: Network },
    ],
  },
];

export function readNavigationRailPreference(): boolean | null {
  try {
    const value = window.localStorage.getItem(NAVIGATION_RAIL_PREFERENCE_KEY);
    return value === null ? null : value === "true";
  } catch {
    return null;
  }
}

export function writeNavigationRailPreference(expanded: boolean): void {
  try {
    window.localStorage.setItem(NAVIGATION_RAIL_PREFERENCE_KEY, String(expanded));
  } catch {
    // 无持久化能力时，调用方仍保留当前 React 状态。
  }
}

function isItemActive(
  item: NavigationItem,
  view: NavigationView,
  resourceKind: ResourceKind,
): boolean {
  return (
    (view === item.id && (item.id !== "resources" || item.kind === resourceKind))
    || ((view === "agent-detail" || view === "create") && item.id === "agents")
  );
}

function RailTooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="studio-tooltip" side="right" sideOffset={8}>
          {label}
          <Tooltip.Arrow className="studio-tooltip-arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export interface NavigationRailProps {
  view: NavigationView;
  resourceKind: ResourceKind;
  expanded: boolean;
  workspaceName: string;
  workspacePath: string;
  runtimeReady: boolean;
  onNavigate: (view: NavigationView, kind?: ResourceKind) => void;
  onOpenSettings: () => void;
}

export function NavigationRail({
  view,
  resourceKind,
  expanded,
  workspaceName,
  workspacePath,
  runtimeReady,
  onNavigate,
  onOpenSettings,
}: NavigationRailProps) {
  return (
    <Tooltip.Provider delayDuration={320} skipDelayDuration={120}>
      <aside className="sidebar navigation-rail" data-state={expanded ? "expanded" : "compact"}>
        <div className="product">
          <span className="product-mark" aria-hidden="true">K</span>
          <span className="product-copy"><strong>AgentKit</strong><span>Studio</span></span>
        </div>

        <RailTooltip label={workspacePath}>
          <button
            className="workspace-switcher"
            type="button"
            aria-label={`${workspaceName} 工作区`}
            aria-disabled={!runtimeReady}
          >
            <span className="workspace-mark"><Folder size={16} /></span>
            <span className="workspace-copy"><strong>{workspaceName}</strong></span>
          </button>
        </RailTooltip>

        <nav className="primary-nav" aria-label="产品导航">
          {NAVIGATION_GROUPS.map(group => (
            <div key={group.group} className="nav-group">
              <div className="nav-label">{group.group}</div>
              {group.items.map(item => {
                const Icon = item.icon;
                const active = isItemActive(item, view, resourceKind);
                const button = (
                  <button
                    key={`${item.id}-${item.label}`}
                    className={`nav-item${active ? " active" : ""}`}
                    type="button"
                    aria-label={item.label}
                    aria-current={active ? "page" : undefined}
                    onClick={() => onNavigate(item.id, item.kind)}
                  >
                    <Icon size={18} />
                    <span>{item.label}</span>
                  </button>
                );
                return expanded ? button : (
                  <RailTooltip key={`${item.id}-${item.label}`} label={item.label}>
                    {button}
                  </RailTooltip>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <RailTooltip label="本地用户">
            <span className="user-avatar" aria-label="本地用户">A</span>
          </RailTooltip>
          <RailTooltip label="设置">
            <button
              className="icon-button tertiary"
              type="button"
              aria-label="设置"
              onClick={onOpenSettings}
            >
              <Settings size={16} />
            </button>
          </RailTooltip>
        </div>
      </aside>
    </Tooltip.Provider>
  );
}
