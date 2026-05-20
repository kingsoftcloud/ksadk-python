import { lazy, Suspense, useState } from 'react';
import { ExternalLink, FolderOpen, MessageSquareText, ShieldCheck, TerminalSquare } from 'lucide-react';

type NativeManagementLink = {
  href: string;
  label: string;
  title: string;
};

type NativeTerminalCapability = {
  Enabled: boolean;
  Mode?: string | null;
  Path?: string | null;
};

type NativeRuntimeLauncherProps = {
  productLabel?: string;
  nativeManagementLink?: NativeManagementLink | null;
  nativeTerminal?: NativeTerminalCapability | null;
  workspaceEnabled: boolean;
  onOpenWorkspace: () => void;
};

const LazyNativeTerminalPanel = lazy(() =>
  import('./NativeTerminalPanel').then((module) => ({
    default: module.NativeTerminalPanel,
  })),
);

export function NativeRuntimeLauncher({
  productLabel = '原生运行时',
  nativeManagementLink,
  nativeTerminal,
  workspaceEnabled,
  onOpenWorkspace,
}: NativeRuntimeLauncherProps) {
  const terminalEnabled = Boolean(nativeTerminal?.Enabled);
  const [terminalOpen, setTerminalOpen] = useState(false);

  return (
    <section className="flex min-h-0 flex-1 overflow-y-auto bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.12),transparent_34%),linear-gradient(135deg,#f8fafc_0%,#ffffff_48%,#f1f5f9_100%)] px-4 py-8 dark:bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.16),transparent_34%),linear-gradient(135deg,#020617_0%,#0f172a_52%,#111827_100%)] sm:px-8">
      <div className="mx-auto flex w-full max-w-5xl flex-col justify-center">
        <div className="overflow-hidden rounded-[2rem] border border-slate-200/80 bg-white/90 shadow-xl shadow-slate-200/60 backdrop-blur dark:border-slate-800 dark:bg-slate-900/85 dark:shadow-black/30">
          <div className="border-b border-slate-200/80 bg-slate-50/80 px-6 py-5 dark:border-slate-800 dark:bg-slate-950/40 sm:px-8">
            <div className="inline-flex items-center gap-2 rounded-full border border-sky-200 bg-sky-50 px-3 py-1 text-xs font-medium text-sky-700 dark:border-sky-900/60 dark:bg-sky-950/50 dark:text-sky-300">
              <ShieldCheck className="h-3.5 w-3.5" />
              {productLabel} native runtime
            </div>
            <h1 className="mt-5 text-2xl font-semibold tracking-tight text-slate-950 dark:text-slate-50 sm:text-3xl">
              对话请使用原生管理台
            </h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300 sm:text-base">
              当前运行时的对话状态由原生管理台承载。AgentEngine Hosted UI 在这里保留统一入口和
              Workspace 文件管理，不重复实现运行时内部已有的复杂对话状态机。
            </p>
          </div>

          <div className="grid gap-4 p-6 md:grid-cols-3 sm:p-8">
            <div className="rounded-3xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white dark:bg-slate-100 dark:text-slate-950">
                <MessageSquareText className="h-5 w-5" />
              </div>
              <h2 className="mt-4 text-base font-semibold text-slate-950 dark:text-slate-50">
                原生管理台
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
                使用原生管理台处理对话、工具卡片、思考流、审批、中断和运行时历史，避免跨状态机不一致。
              </p>
              {nativeManagementLink ? (
                <a
                  href={nativeManagementLink.href}
                  target="_blank"
                  rel="noreferrer"
                  title={nativeManagementLink.title}
                  className="mt-5 inline-flex items-center gap-2 rounded-2xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-950 dark:hover:bg-white"
                >
                  <ExternalLink className="h-4 w-4" />
                  打开{nativeManagementLink.label}
                </a>
              ) : (
                <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
                  当前访问模式不开放原生管理入口。需要完整对话体验时，请使用 owner/private 链接或 CLI。
                </div>
              )}
            </div>

            <div className="rounded-3xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300">
                <FolderOpen className="h-5 w-5" />
              </div>
              <h2 className="mt-4 text-base font-semibold text-slate-950 dark:text-slate-50">
                Workspace 文件管理
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
                文件浏览、上传、预览仍由 Hosted UI 提供，和原生管理台组合使用覆盖对话与工作区。
              </p>
              {workspaceEnabled ? (
                <button
                  type="button"
                  onClick={onOpenWorkspace}
                  className="mt-5 inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm font-medium text-slate-800 transition hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700"
                >
                  <FolderOpen className="h-4 w-4" />
                  打开 Workspace
                </button>
              ) : (
                <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-6 text-slate-500 dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-400">
                  当前链接未开放 Workspace 文件管理。Owner 或 private 链接可使用该能力。
                </div>
              )}
            </div>

            <div className="rounded-3xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                <TerminalSquare className="h-5 w-5" />
              </div>
              <h2 className="mt-4 text-base font-semibold text-slate-950 dark:text-slate-50">
                原生 TUI
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
                如果运行时暴露安全的 ks-terminal.v1 通道，可以在这里进入原生终端 UI；share 链接不会开放该入口。
              </p>
              {terminalEnabled ? (
                <button
                  type="button"
                  onClick={() => setTerminalOpen(true)}
                  className="mt-5 inline-flex items-center gap-2 rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm font-medium text-emerald-800 transition hover:bg-emerald-100 dark:border-emerald-900/70 dark:bg-emerald-950/40 dark:text-emerald-200"
                >
                  <TerminalSquare className="h-4 w-4" />
                  打开 TUI
                </button>
              ) : (
                <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-6 text-slate-500 dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-400">
                  当前运行时未声明可用的浏览器 TUI 能力。
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
      {nativeTerminal && terminalOpen ? (
        <Suspense fallback={null}>
          <LazyNativeTerminalPanel
            capability={nativeTerminal}
            open={terminalOpen}
            onClose={() => setTerminalOpen(false)}
          />
        </Suspense>
      ) : null}
    </section>
  );
}
