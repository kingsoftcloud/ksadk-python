import { useEffect, useRef, useState, type MouseEvent } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { ChevronRight, FileText, FolderOpen } from "lucide-react";
import { apiFetch } from "../api";
import "./runDocumentActions.css";

type DocumentLink = { href: string; base: string; path: string; anchor: HTMLAnchorElement };
type Metadata = {
  name: string; path: string | null; relativePath: string;
  capabilities: { open: boolean; reveal: boolean; openWith: boolean };
  applications: { id: string; name: string }[]; revealLabel: string;
};

/** Only links issued by Studio's run document service can launch local files. */
export function documentLink(target: EventTarget | null): DocumentLink | null {
  if (!(target instanceof Element)) return null;
  const anchor = target.closest("a");
  if (!anchor || anchor.hasAttribute("data-run-document-download")) return null;
  let url: URL;
  try { url = new URL(anchor.href, window.location.href); } catch { return null; }
  if (url.origin !== window.location.origin || !/^\/api\/v1\/runs\/[^/]+\/documents\/content$/.test(url.pathname)) return null;
  const path = url.searchParams.get("path");
  return path ? { href: `${url.pathname}?${new URLSearchParams({ path })}`, base: url.pathname.replace(/\/content$/, ""), path, anchor } : null;
}

async function checked(response: Response) {
  if (response.ok) return response.json();
  const body = await response.json().catch(() => null);
  throw new Error(body?.error?.message || (response.status === 404 ? "文件可能已移动或删除。" : "文件操作失败，请稍后重试。"));
}

export function useRunDocumentActions() {
  const [menu, setMenu] = useState<{ link: DocumentLink; x: number; y: number } | null>(null);
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [menuError, setMenuError] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const menuContent = useRef<HTMLDivElement>(null);
  const activeLink = useRef<DocumentLink | null>(null);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), notice.error ? 12000 : 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!menu) return;
    const abort = new AbortController();
    setMetadata(null); setMenuError("");
    void apiFetch(`${menu.link.base}/metadata?${new URLSearchParams({ path: menu.link.path })}`, { signal: abort.signal })
      .then(checked).then(data => { if (!abort.signal.aborted) setMetadata(data); })
      .catch(error => { if (!abort.signal.aborted) setMenuError(error.message); });
    return () => abort.abort();
  }, [menu]);

  const run = (action: () => Promise<void>) => { void action().catch(error => setNotice({ text: error.message, error: true })); };
  const open = async (link: DocumentLink, action = "open", applicationId?: string) => {
    const result = await checked(await apiFetch(`${link.base}/actions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: link.path, action, ...(applicationId ? { applicationId } : {}) }),
    }));
    setNotice({ text: action === "reveal" ? `已请求显示文件位置：${result.name}` : `已请求用${applicationId ? "所选应用" : "系统默认应用"}打开：${result.name}`, error: false });
  };
  const onClickCapture = (event: MouseEvent) => {
    const link = documentLink(event.target);
    if (!link) return;
    event.preventDefault(); event.stopPropagation();
    run(() => open(link));
  };
  const onContextMenuCapture = (event: MouseEvent) => {
    const link = documentLink(event.target);
    if (!link) return;
    event.preventDefault(); event.stopPropagation();
    activeLink.current = link;
    setMenu({ link, x: event.clientX, y: event.clientY });
  };
  const copy = async (contents: boolean) => {
    if (!menu || !metadata) return;
    const text = contents ? (await checked(await apiFetch(`${menu.link.href}&preview=true`))).content : metadata.path;
    if (typeof text !== "string") throw new Error("当前连接不能提供本机文件路径。");
    if (!navigator.clipboard) throw new Error("浏览器不支持剪贴板，请下载文件后复制。");
    await navigator.clipboard.writeText(text);
    setNotice({ text: contents ? "已复制文件内容" : "已复制文件路径", error: false });
  };
  const item = (label: string, action: () => Promise<void>, disabled = false) => <Menu.Item className="run-file-menu-item" disabled={disabled} onSelect={() => run(action)}>{label}</Menu.Item>;
  const ui = <>
    {notice && <div className={`run-file-notice${notice.error ? " error" : ""}`} role={notice.error ? "alert" : "status"}>
      {notice.text}<button type="button" aria-label="关闭文件提示" onClick={() => setNotice(null)}>×</button>
    </div>}
    <Menu.Root open={Boolean(menu)} onOpenChange={value => { if (!value) setMenu(null); }}>
      <Menu.Trigger asChild><button ref={trigger} className="run-file-menu-anchor" tabIndex={-1} aria-label="文件操作菜单"
        style={{ left: menu?.x || 0, top: menu?.y || 0 }} /></Menu.Trigger>
      <Menu.Portal><Menu.Content ref={menuContent} className="run-file-menu" align="start" sideOffset={0} collisionPadding={8}
        onCloseAutoFocus={event => { event.preventDefault(); activeLink.current?.anchor.focus(); }}>
        <Menu.Label className="run-file-menu-title"><FileText size={14} />{metadata?.name || "文件操作"}</Menu.Label>
        {menuError ? <div className="run-file-menu-note" role="alert">{menuError}</div> : !metadata ? <div className="run-file-menu-note" role="status">正在读取文件信息…</div> : menu && <>
          {item("打开文件", () => open(menu.link), !metadata.capabilities.open)}
          {metadata.applications.some(app => app.id === "vscode") && item("在 VS Code 中打开", () => open(menu.link, "open", "vscode"))}
          <Menu.Sub><Menu.SubTrigger className="run-file-menu-item" disabled={!metadata.capabilities.openWith || !metadata.applications.length}>打开方式<ChevronRight size={14} /></Menu.SubTrigger>
            <Menu.Portal><Menu.SubContent className="run-file-menu" sideOffset={4} collisionPadding={8}
              onFocusOutside={event => {
                // Radix briefly focuses the parent content when the pointer
                // leaves the trigger for the portalled submenu. Keep that
                // transit open; another item, Escape or outside click still
                // follows the normal menu dismissal behavior.
                if (event.target === menuContent.current) event.preventDefault();
              }}>
              {metadata.applications.map(app => <Menu.Item key={app.id} className="run-file-menu-item" onSelect={() => run(() => open(menu.link, "open", app.id))}>{app.name}</Menu.Item>)}
            </Menu.SubContent></Menu.Portal>
          </Menu.Sub>
          <Menu.Separator className="run-file-menu-separator" />
          <Menu.Item asChild className="run-file-menu-item"><a href={menu.link.href} download={metadata.name} data-run-document-download>下载副本…</a></Menu.Item>
          {item("复制路径", () => copy(false), !metadata.path)}
          {item("复制文件内容", () => copy(true))}
          <Menu.Item className="run-file-menu-item" disabled={!metadata.capabilities.reveal} onSelect={() => run(() => open(menu.link, "reveal"))}><FolderOpen size={14} />{metadata.revealLabel || "显示所在文件夹"}</Menu.Item>
          {!metadata.capabilities.open && <div className="run-file-menu-note">当前连接不支持打开本机应用，可下载后打开。</div>}
        </>}
      </Menu.Content></Menu.Portal>
    </Menu.Root>
  </>;
  return { onClickCapture, onContextMenuCapture, ui };
}
