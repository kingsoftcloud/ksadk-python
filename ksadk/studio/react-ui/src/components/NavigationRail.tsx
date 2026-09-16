import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useEffect, useState, type ReactNode } from "react";
import {
  Bot,
  Boxes,
  ChevronDown,
  FolderOpen,
  LayoutGrid,
  Library,
  PanelTop,
  Settings,
  Users,
  X,
  ChartSpline,
  Clock3,
  ClipboardCheck,
  CloudUpload,
  PackageCheck,
  Plug,
  PanelLeftClose,
  Search,
  ServerCog,
  SquarePen,
  type LucideIcon,
} from "lucide-react";
import type { WorkspaceContribution } from "../plugins/workspaceSlots";
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
  | "evaluations"
  | "runtime-resources"
  | "plugins"
  | "automations"
  | `plugin:${string}`;

const GROUPS: Array<{
  id: string;
  label: string;
  icon: LucideIcon;
  items: Array<{
    id: NavigationView;
    label: string;
    icon: LucideIcon;
  }>;
}> = [
  {
    id: "resources",
    label: "资源库",
    icon: Library,
    items: [
      { id: "resources", label: "模型与工具", icon: Boxes },
      { id: "runtime-resources", label: "运行资源", icon: ServerCog },
      { id: "plugins", label: "插件", icon: Plug },
    ],
  },
  {
    id: "runs",
    label: "运行中心",
    icon: PanelTop,
    items: [
      { id: "builds", label: "构建", icon: PackageCheck },
      { id: "deployments", label: "部署", icon: CloudUpload },
      { id: "automations", label: "自动化", icon: Clock3 },
      { id: "observability", label: "可观测", icon: ChartSpline },
      { id: "evaluations", label: "评测", icon: ClipboardCheck },
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
    window.localStorage.setItem(
      NAVIGATION_RAIL_PREFERENCE_KEY,
      String(expanded),
    );
  } catch {
    /* The current choice remains available without persistence. */
  }
}
function RailTooltip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
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
  workspacePages?: WorkspaceContribution[];
  resourceKind: ResourceKind;
  expanded: boolean;
  workspaceName: string;
  workspacePath: string;
  runtimeReady: boolean;
  mobile?: boolean;
  mobileOpen?: boolean;
  onMobileOpenChange?: (open: boolean) => void;
  onExpand?: () => void;
  onToggle?: () => void;
  onStartChat?: () => void;
  chatStreaming?: boolean;
  onHistoryHostChange?: (host: HTMLDivElement | null) => void;
  onSearchHostChange?: (host: HTMLDivElement | null) => void;
  onNavigate: (view: NavigationView, kind?: ResourceKind) => void;
  onOpenSettings: () => void;
  onWorkspaceSwitch?: () => void;
  workspaceRunCount?: number;
}
export function NavigationRail({
  view,
  workspacePages = [],
  resourceKind,
  expanded,
  workspaceName,
  workspacePath,
  runtimeReady,
  mobile = false,
  mobileOpen = false,
  onMobileOpenChange,
  onExpand,
  onToggle,
  onStartChat,
  chatStreaming = false,
  onHistoryHostChange,
  onSearchHostChange,
  onNavigate,
  onOpenSettings,
  onWorkspaceSwitch,
  workspaceRunCount = 0,
}: NavigationRailProps) {
  const activeGroup =
    GROUPS.find((group) => group.items.some((item) => item.id === view))?.id ||
    "";
  const [openGroup, setOpenGroup] = useState(activeGroup);
  useEffect(() => {
    setOpenGroup(activeGroup);
  }, [activeGroup]);
  const showLabels = expanded || mobile;
  // Workspace tabs are contributed by the active DSH host. Keeping this list
  // live means unavailable plugins disappear instead of leaving a dead tab.
  const navigationPages = workspacePages;
  const rail = (
    <aside
      className="studio-navigation"
      data-state={showLabels ? "expanded" : "compact"}
      aria-label="工作区导航"
    >
      <div className="studio-nav-brand">
        <span className="studio-nav-mark" aria-hidden="true">
          K
        </span>
        {showLabels && (
          <strong>
            AgentKit <span>Studio</span>
          </strong>
        )}
        {showLabels && !mobile && onToggle && (
          <button
            type="button"
            className="icon-button tertiary studio-nav-collapse"
            aria-label="收起导航"
            aria-expanded={true}
            title="收起导航"
            onClick={onToggle}
          >
            <PanelLeftClose size={20} aria-hidden="true" />
          </button>
        )}
        {mobile && (
          <button
            type="button"
            className="icon-button tertiary"
            aria-label="关闭导航"
            onClick={() => onMobileOpenChange?.(false)}
          >
            <X size={20} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="studio-nav-scroll">
        <nav className="studio-nav-primary" aria-label="产品导航">
          <RailTooltip label="新对话">
            <button
              type="button"
              className="studio-nav-link"
              aria-label="新对话"
              disabled={chatStreaming}
              onClick={() => {
                if (onStartChat) onStartChat();
                else onNavigate("conversations");
              }}
            >
              <SquarePen size={20} aria-hidden="true" />
              {showLabels && <span>新对话</span>}
            </button>
          </RailTooltip>
          {onSearchHostChange && <>
            <div className="studio-nav-search" ref={onSearchHostChange} hidden={!showLabels} />
            {!showLabels && <RailTooltip label="搜索会话">
              <button type="button" className="studio-nav-link" aria-label="搜索会话" onClick={onExpand}>
                <Search size={20} aria-hidden="true" />
              </button>
            </RailTooltip>}
          </>}
          <RailTooltip label="Agent">
            <button
              type="button"
              className={`studio-nav-link${["agents", "create", "agent-detail"].includes(view) ? " active" : ""}`}
              aria-label="Agent"
              aria-current={
                ["agents", "create", "agent-detail"].includes(view)
                  ? "page"
                  : undefined
              }
              onClick={() => onNavigate("agents")}
            >
              <Bot size={20} aria-hidden="true" />
              {showLabels && <span>Agent</span>}
            </button>
          </RailTooltip>
          {navigationPages.map(page => <RailTooltip key={page.id} label={page.label}><button type="button" className={`studio-nav-link${view === `plugin:${page.id}` ? ' active' : ''}`} aria-label={page.label} aria-current={view === `plugin:${page.id}` ? 'page' : undefined} onClick={() => onNavigate(`plugin:${page.id}`)}>{page.id === "teams" ? <Users size={20} aria-hidden="true" /> : <LayoutGrid size={20} aria-hidden="true" />}{showLabels && <span>{page.label}</span>}</button></RailTooltip>)}
          {GROUPS.map((group) => {
            const open = showLabels && openGroup === group.id;
            return (
              <div key={group.id} className="studio-nav-group">
                <RailTooltip label={group.label}>
                  <button
                    type="button"
                    className={`studio-nav-link${activeGroup === group.id ? " active" : ""}`}
                    aria-label={group.label}
                    aria-expanded={open}
                    aria-controls={`studio-nav-${group.id}`}
                    onClick={() => {
                      if (!showLabels) onExpand?.();
                      setOpenGroup(open ? "" : group.id);
                    }}
                  >
                    <group.icon size={20} aria-hidden="true" />
                    {showLabels && (
                      <>
                        <span>{group.label}</span>
                        <ChevronDown
                          aria-hidden="true"
                          size={14}
                          className="studio-nav-chevron"
                          data-open={open}
                        />
                      </>
                    )}
                  </button>
                </RailTooltip>
                <div
                  id={`studio-nav-${group.id}`}
                  className="studio-nav-children"
                  hidden={!open}
                >
                  {group.items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`studio-nav-link${view === item.id ? " active" : ""}`}
                      aria-current={view === item.id ? "page" : undefined}
                      onClick={() =>
                        onNavigate(
                          item.id,
                          item.id === "resources" ? resourceKind : undefined,
                        )
                      }
                    >
                      <span>{item.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </nav>
        <div
          className="studio-nav-history"
          ref={onHistoryHostChange}
          hidden={!showLabels}
        />
      </div>
      <div className="studio-nav-footer">
        <RailTooltip label={`${workspacePath}（切换工作区）`}>
          <button
            type="button"
            className="studio-nav-workspace"
            aria-label={`${workspaceName} 工作区`}
            onClick={onWorkspaceSwitch}
          >
            <FolderOpen size={20} aria-hidden="true" />
            {showLabels && <span>{workspaceName}</span>}
            {showLabels && workspaceRunCount > 0 && <small aria-label={`${workspaceRunCount} 个后台任务`}>{workspaceRunCount}</small>}
            <i data-ready={runtimeReady} aria-label={runtimeReady ? "工作区已连接" : "工作区未连接"} />
          </button>
        </RailTooltip>
        <RailTooltip label="设置">
          <button
            type="button"
            className="studio-nav-link"
            aria-label="设置"
            onClick={onOpenSettings}
          >
            <Settings size={20} aria-hidden="true" />
            {showLabels && <span>设置</span>}
          </button>
        </RailTooltip>
      </div>
    </aside>
  );

  return (
    <Tooltip.Provider delayDuration={320} skipDelayDuration={120}>
      {mobile ? (
        <Dialog.Root open={mobileOpen} onOpenChange={onMobileOpenChange}>
          <Dialog.Portal>
            <Dialog.Overlay className="studio-nav-overlay" />
            <Dialog.Content
              className="studio-nav-dialog"
              aria-describedby={undefined}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                document
                  .querySelector<HTMLButtonElement>(".rail-toggle")
                  ?.focus();
              }}
            >
              <Dialog.Title className="sr-only">工作区导航</Dialog.Title>
              {rail}
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      ) : (
        rail
      )}
    </Tooltip.Provider>
  );
}
