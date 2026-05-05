import { ExternalLink, FolderOpen, MessageSquareText, ShieldCheck } from 'lucide-react';

type NativeManagementLink = {
  href: string;
  label: string;
  title: string;
};

type OpenClawNativeLauncherProps = {
  nativeManagementLink?: NativeManagementLink | null;
  workspaceEnabled: boolean;
  onOpenWorkspace: () => void;
};

export function OpenClawNativeLauncher({
  nativeManagementLink,
  workspaceEnabled,
  onOpenWorkspace,
}: OpenClawNativeLauncherProps) {
  return (
    <section className="flex min-h-0 flex-1 overflow-y-auto bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.12),transparent_34%),linear-gradient(135deg,#f8fafc_0%,#ffffff_48%,#f1f5f9_100%)] px-4 py-8 dark:bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.16),transparent_34%),linear-gradient(135deg,#020617_0%,#0f172a_52%,#111827_100%)] sm:px-8">
      <div className="mx-auto flex w-full max-w-4xl flex-col justify-center">
        <div className="overflow-hidden rounded-[2rem] border border-slate-200/80 bg-white/90 shadow-xl shadow-slate-200/60 backdrop-blur dark:border-slate-800 dark:bg-slate-900/85 dark:shadow-black/30">
          <div className="border-b border-slate-200/80 bg-slate-50/80 px-6 py-5 dark:border-slate-800 dark:bg-slate-950/40 sm:px-8">
            <div className="inline-flex items-center gap-2 rounded-full border border-sky-200 bg-sky-50 px-3 py-1 text-xs font-medium text-sky-700 dark:border-sky-900/60 dark:bg-sky-950/50 dark:text-sky-300">
              <ShieldCheck className="h-3.5 w-3.5" />
              OpenClaw native runtime
            </div>
            <h1 className="mt-5 text-2xl font-semibold tracking-tight text-slate-950 dark:text-slate-50 sm:text-3xl">
              OpenClaw 对话请使用原生 Dashboard
            </h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300 sm:text-base">
              OpenClaw 的工具调用、思考过程、审批、中断和会话历史由 Gateway 原生状态机承载。
              AgentEngine Hosted UI 在这里保留统一入口和 Workspace 文件管理，不再重复实现不稳定的通用对话流。
            </p>
          </div>

          <div className="grid gap-4 p-6 sm:grid-cols-2 sm:p-8">
            <div className="rounded-3xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white dark:bg-slate-100 dark:text-slate-950">
                <MessageSquareText className="h-5 w-5" />
              </div>
              <h2 className="mt-4 text-base font-semibold text-slate-950 dark:text-slate-50">
                原生对话入口
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
                使用 OpenClaw Dashboard 处理对话、工具卡片、思考流、审批和停止输出，避免 Hosted UI
                与 OpenClaw 内部会话状态不一致。
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
                  打开 OpenClaw Dashboard
                </a>
              ) : (
                <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
                  当前访问模式不开放原生管理入口。需要完整对话体验时，请使用 private 链接或
                  AgentEngine CLI 打开 Dashboard。
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
                文件浏览、上传、预览仍由 AgentEngine Hosted UI 提供，和原生 Dashboard
                组合使用即可覆盖对话与工作区两个入口。
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
          </div>
        </div>
      </div>
    </section>
  );
}
