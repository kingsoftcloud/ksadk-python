import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ChevronDown, ChevronRight, FileCode2, FileText, FolderClosed } from "lucide-react";
import { apiFetch } from "../api";
import { Drawer, InlineAlert } from "./Drawer";
import { CodeViewer } from "./ui/CodeViewer";
import { MarkdownPreview } from "./ui/MarkdownPreview";

export interface SkillPreviewEntry {
  path: string;
  size: number;
  kind: "markdown" | "script" | "text" | "binary";
}

interface SkillPreview extends SkillPreviewEntry {
  content?: string;
  truncated?: boolean;
}

interface SkillTreeNode {
  name: string;
  path: string;
  directory: boolean;
  file?: SkillPreviewEntry;
  children: SkillTreeNode[];
}

export interface SkillFileBrowserProps {
  title: string;
  endpoint: string;
  onClose: () => void;
}

function formatByteCount(value: number): string {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

async function responseError(response: Response, fallback: string): Promise<string> {
  const text = await response.text().catch(() => "");
  try {
    return JSON.parse(text)?.error?.message || `${fallback}（${response.status}）`;
  } catch {
    return `${fallback}（${response.status}）`;
  }
}

function languageFor(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() || "";
  return ({
    bash: "bash",
    c: "c",
    cpp: "cpp",
    css: "css",
    go: "go",
    h: "c",
    html: "markup",
    java: "java",
    js: "javascript",
    json: "json",
    jsx: "jsx",
    md: "markdown",
    mjs: "javascript",
    py: "python",
    rs: "rust",
    sh: "bash",
    toml: "toml",
    ts: "typescript",
    tsx: "tsx",
    xml: "markup",
    yaml: "yaml",
    yml: "yaml",
  } as Record<string, string>)[extension] || "text";
}

function buildSkillTree(files: SkillPreviewEntry[]): SkillTreeNode[] {
  const root: SkillTreeNode = { name: "", path: "", directory: true, children: [] };
  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let parent = root;
    parts.forEach((part, index) => {
      const path = parts.slice(0, index + 1).join("/");
      const directory = index < parts.length - 1;
      let node = parent.children.find(child => child.name === part && child.directory === directory);
      if (!node) {
        node = {
          name: part,
          path,
          directory,
          file: directory ? undefined : file,
          children: [],
        };
        parent.children.push(node);
      }
      parent = node;
    });
  }
  const sortNodes = (nodes: SkillTreeNode[]) => {
    nodes.sort((left, right) => (
      Number(right.directory) - Number(left.directory) || left.name.localeCompare(right.name)
    ));
    nodes.forEach(node => sortNodes(node.children));
  };
  sortNodes(root.children);
  return root.children;
}

function SkillFileTree({
  files,
  selected,
  onSelect,
}: {
  files: SkillPreviewEntry[];
  selected: string;
  onSelect: (path: string) => void;
}) {
  const tree = useMemo(() => buildSkillTree(files), [files]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const renderNodes = (nodes: SkillTreeNode[], depth = 0): ReactNode => nodes.map(node => {
    const style = { "--skill-depth": depth } as CSSProperties;
    if (node.directory) {
      const closed = collapsed.has(node.path);
      return (
        <div key={`dir:${node.path}`} role="none">
          <button
            className="skill-tree-row directory"
            type="button"
            role="treeitem"
            style={style}
            aria-expanded={!closed}
            onClick={() => setCollapsed(previous => {
              const next = new Set(previous);
              if (next.has(node.path)) next.delete(node.path);
              else next.add(node.path);
              return next;
            })}
          >
            {closed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
            <FolderClosed size={14} />
            <span title={node.name}>{node.name}</span>
          </button>
          {!closed && <div role="group">{renderNodes(node.children, depth + 1)}</div>}
        </div>
      );
    }
    const Icon = node.file?.kind === "script" ? FileCode2 : FileText;
    return (
      <button
        key={`file:${node.path}`}
        className={`skill-tree-row file${selected === node.path ? " active" : ""}`}
        type="button"
        role="treeitem"
        aria-selected={selected === node.path}
        style={style}
        onClick={() => onSelect(node.path)}
      >
        <span className="skill-tree-spacer" />
        <Icon size={14} />
        <span title={node.path}>{node.name}</span>
      </button>
    );
  });

  return (
    <div className="skill-file-tree" role="tree" aria-label="Skill 文件树">
      {renderNodes(tree)}
    </div>
  );
}

export function SkillFileBrowser({ title, endpoint, onClose }: SkillFileBrowserProps) {
  const [files, setFiles] = useState<SkillPreviewEntry[]>([]);
  const [selected, setSelected] = useState("");
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [filesLoading, setFilesLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [filesError, setFilesError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const previewSequence = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    setFilesLoading(true);
    setFilesError("");
    setFiles([]);
    setSelected("");
    setPreview(null);
    apiFetch(endpoint, { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error(await responseError(response, "Skill 文件读取失败"));
        return response.json();
      })
      .then(payload => {
        const nextFiles: SkillPreviewEntry[] = payload.files || [];
        setFiles(nextFiles);
        setSelected(nextFiles.find(file => file.path === "SKILL.md")?.path || nextFiles[0]?.path || "");
      })
      .catch(reason => {
        if (reason?.name !== "AbortError") setFilesError(reason.message || "Skill 文件读取失败");
      })
      .finally(() => {
        if (!controller.signal.aborted) setFilesLoading(false);
      });
    return () => controller.abort();
  }, [endpoint]);

  useEffect(() => {
    if (!selected) {
      setPreview(null);
      setPreviewLoading(false);
      return;
    }
    const controller = new AbortController();
    const sequence = ++previewSequence.current;
    setPreview(null);
    setPreviewLoading(true);
    setPreviewError("");
    const query = new URLSearchParams({ path: selected });
    apiFetch(`${endpoint}?${query}`, { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error(await responseError(response, "Skill 文件读取失败"));
        return response.json();
      })
      .then(payload => {
        if (previewSequence.current === sequence) setPreview(payload);
      })
      .catch(reason => {
        if (reason?.name !== "AbortError" && previewSequence.current === sequence) {
          setPreviewError(reason.message || "Skill 文件读取失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && previewSequence.current === sequence) {
          setPreviewLoading(false);
        }
      });
    return () => controller.abort();
  }, [endpoint, selected]);

  return (
    <Drawer
      title={`${title} · 文件`}
      subtitle={`${files.length} 个文件；只读预览，不会执行脚本。`}
      wide
      onClose={onClose}
    >
      <div className="skill-preview-layout">
        <aside className="skill-preview-sidebar">
          {filesLoading && <div className="skill-file-state">正在读取目录…</div>}
          {!filesLoading && filesError && (
            <InlineAlert kind="error" title="无法读取 Skill 文件" message={filesError} />
          )}
          {!filesLoading && !filesError && files.length === 0 && (
            <div className="skill-file-state">Skill 中没有可预览的文件。</div>
          )}
          {!filesLoading && !filesError && files.length > 0 && (
            <SkillFileTree files={files} selected={selected} onSelect={setSelected} />
          )}
        </aside>
        <section className="skill-preview-pane" aria-live="polite">
          {(preview || selected) && (
            <header className="skill-preview-header">
              <strong title={preview?.path || selected}>{preview?.path || selected}</strong>
              {preview && <span>{preview.kind} · {formatByteCount(preview.size || 0)}</span>}
            </header>
          )}
          <div className="skill-preview-content">
            {previewLoading && <div className="skill-file-state">正在读取文件…</div>}
            {!previewLoading && previewError && (
              <InlineAlert kind="error" title="无法预览文件" message={previewError} />
            )}
            {!previewLoading && !previewError && preview?.kind === "markdown" && (
              <MarkdownPreview content={preview.content || ""} />
            )}
            {!previewLoading && !previewError && ["script", "text"].includes(preview?.kind || "") && (
              <CodeViewer
                code={preview?.content || ""}
                language={languageFor(preview?.path || selected)}
                filename={preview?.path || selected}
                showLineNumbers
              />
            )}
            {!previewLoading && !previewError && preview?.kind === "binary" && (
              <div className="skill-file-state">二进制文件不提供内容预览。</div>
            )}
            {!previewLoading && !previewError && !preview && !selected && (
              <div className="skill-file-state">选择一个文件查看内容。</div>
            )}
          </div>
          {preview?.truncated && (
            <div className="skill-preview-truncated">文件较大，仅显示前 512 KiB。</div>
          )}
        </section>
      </div>
    </Drawer>
  );
}
