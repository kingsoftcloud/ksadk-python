import { lazy, Suspense, useEffect, useState } from "react";
import { Check, Copy, WrapText } from "lucide-react";

const PrismRenderer = lazy(() => import("./PrismRenderer"));

export interface CodeViewerProps {
  code: string;
  language?: string;
  filename?: string;
  wrap?: boolean;
  showLineNumbers?: boolean;
}

export function CodeViewer({
  code,
  language = "text",
  filename,
  wrap = false,
  showLineNumbers = true,
}: CodeViewerProps) {
  const [wrapEnabled, setWrapEnabled] = useState(wrap);
  const [copied, setCopied] = useState(false);

  useEffect(() => setWrapEnabled(wrap), [wrap]);

  async function copyCode() {
    if (!navigator.clipboard?.writeText) return;
    await navigator.clipboard.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  const label = filename ? `${filename} 源码` : `${language || "text"} 代码`;

  return (
    <section
      className="code-viewer"
      role="region"
      aria-label={label}
      data-code-theme="studio"
      data-wrap={String(wrapEnabled)}
    >
      <header className="code-viewer-toolbar">
        <div>
          {filename && <strong title={filename}>{filename}</strong>}
          <span>{language || "text"}</span>
        </div>
        <div className="code-viewer-actions">
          <button
            className="icon-button tertiary"
            type="button"
            aria-label={wrapEnabled ? "取消自动换行" : "自动换行"}
            title={wrapEnabled ? "取消自动换行" : "自动换行"}
            aria-pressed={wrapEnabled}
            onClick={() => setWrapEnabled(value => !value)}
          >
            <WrapText size={15} />
          </button>
          <button
            className="icon-button tertiary"
            type="button"
            aria-label={copied ? "已复制" : "复制代码"}
            title={copied ? "已复制" : "复制代码"}
            onClick={() => void copyCode()}
          >
            {copied ? <Check size={15} /> : <Copy size={15} />}
          </button>
        </div>
      </header>
      <div className="code-viewer-scroll">
        <Suspense fallback={<pre className="code-viewer-fallback"><code>{code}</code></pre>}>
          <PrismRenderer
            code={code}
            language={language || "text"}
            showLineNumbers={showLineNumbers}
          />
        </Suspense>
      </div>
    </section>
  );
}
