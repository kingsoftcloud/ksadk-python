import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useEffect, useState, type ReactNode } from "react";
import {
  Boxes,
  ChartSpline,
  Clock3,
  ClipboardCheck,
  CloudUpload,
  PackageCheck,
  Plug,
  ServerCog,
  type LucideIcon,
} from "lucide-react";
import { KingIcon, type KingIconName } from "./KingIcon";
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
  icon: KingIconName;
  items: Array<{
    id: NavigationView;
    label: string;
    icon: LucideIcon;
  }>;
}> = [
  {
    id: "resources",
    label: "资源库",
    icon: "folder",
    items: [
      { id: "resources", label: "模型与工具", icon: Boxes },
      { id: "runtime-resources", label: "运行资源", icon: ServerCog },
      { id: "plugins", label: "插件", icon: Plug },
    ],
  },
  {
    id: "runs",
    label: "运行中心",
    icon: "panel",
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
  onStartChat?: () => void;
  chatStreaming?: boolean;
  onHistoryHostChange?: (host: HTMLDivElement | null) => void;
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
  onStartChat,
  chatStreaming = false,
  onHistoryHostChange,
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
        {mobile && (
          <button
            type="button"
            className="icon-button tertiary"
            aria-label="关闭导航"
            onClick={() => onMobileOpenChange?.(false)}
          >
            <KingIcon name="close" size={18} />
          </button>
        )}
      </div>
      <RailTooltip label={`${workspacePath}（切换工作区）`}>
        <button
          type="button"
          className="studio-nav-workspace workspace-switcher"
          aria-label={`${workspaceName} 工作区`}
          onClick={onWorkspaceSwitch}
        >
          <KingIcon name="folder" size={15} />
          {showLabels && <span>{workspaceName}</span>}
          {showLabels && workspaceRunCount > 0 && <small aria-label={`${workspaceRunCount} 个后台任务`}>{workspaceRunCount}</small>}
          <i
            data-ready={runtimeReady}
            aria-label={runtimeReady ? "工作区已连接" : "工作区未连接"}
          />
        </button>
      </RailTooltip>
      <div className="studio-nav-scroll">
        <nav className="studio-nav-primary" aria-label="产品导航">
          <RailTooltip label="新对话">
            <button
              type="button"
              className={`studio-nav-link${view === "conversations" ? " active" : ""}`}
              aria-label="新对话"
              disabled={chatStreaming}
              aria-current={view === "conversations" ? "page" : undefined}
              onClick={() => {
                if (onStartChat) onStartChat();
                else onNavigate("conversations");
              }}
            >
              <KingIcon name="message" size={18} />
              {showLabels && <span>新对话</span>}
            </button>
          </RailTooltip>
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
              <KingIcon name="cpu" size={18} />
              {showLabels && <span>Agent</span>}
            </button>
          </RailTooltip>
          {navigationPages.map(page => <RailTooltip key={page.id} label={page.label}><button type="button" className={`studio-nav-link${view === `plugin:${page.id}` ? ' active' : ''}`} aria-label={page.label} aria-current={view === `plugin:${page.id}` ? 'page' : undefined} onClick={() => onNavigate(`plugin:${page.id}`)}><KingIcon name={page.id === "teams" ? "users" : "all"} size={18} />{showLabels && <span>{page.label}</span>}</button></RailTooltip>)}
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
                    <KingIcon name={group.icon} size={18} />
                    {showLabels && (
                      <>
                        <span>{group.label}</span>
                        <KingIcon
                          name="down"
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
        <RailTooltip label="设置">
          <button
            type="button"
            className="studio-nav-link"
            aria-label="设置"
            onClick={onOpenSettings}
          >
            <KingIcon name="settings" size={18} />
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
