import React, { useEffect, useId, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { Copy, Check } from 'lucide-react';
import mermaid from 'mermaid';
import 'katex/dist/katex.min.css';
import { copyTextToClipboard } from '../utils/clipboard.js';
import { preprocessMarkdown } from '../utils/markdown.js';

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
});

interface CodeBlockProps {
  language: string;
  value: string;
}

const CodeBlock: React.FC<CodeBlockProps> = ({ language, value }) => {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  useEffect(() => {
    if (copyState === 'idle') {
      return undefined;
    }
    const timer = window.setTimeout(() => setCopyState('idle'), 2000);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const handleCopy = async () => {
    const ok = await copyTextToClipboard(value);
    setCopyState(ok ? 'copied' : 'failed');
  };

  if (language === 'mermaid') {
    return <MermaidBlock chart={value} />;
  }

  return (
    <div className="my-4 rounded-lg overflow-hidden bg-[#1e1e1e] border border-slate-700/50 shadow-sm">
      <div className="flex items-center justify-between px-4 py-1.5 bg-[#2d2d2d] text-slate-300 text-xs font-mono">
        <span className="uppercase">{language || 'text'}</span>
        <button
          type="button"
          onClick={() => { void handleCopy(); }}
          className="flex items-center gap-1.5 hover:text-white transition-colors py-1"
        >
          {copyState === 'copied' ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
          <span>{copyState === 'copied' ? 'Copied!' : copyState === 'failed' ? 'Copy failed' : 'Copy'}</span>
        </button>
      </div>
      <div className="overflow-x-auto text-[13.5px]">
        <SyntaxHighlighter
          language={language}
          style={vscDarkPlus}
          customStyle={{ margin: 0, padding: '1rem', background: 'transparent' }}
          PreTag="div"
        >
          {String(value).replace(/\n$/, '')}
        </SyntaxHighlighter>
      </div>
    </div>
  );
};

const MermaidBlock: React.FC<{ chart: string }> = ({ chart }) => {
  const [svg, setSvg] = useState<string>('');
  const id = useId().replace(/:/g, '-');

  useEffect(() => {
    let isCancelled = false;
    const renderChart = async () => {
      try {
        const { svg: renderedSvg } = await mermaid.render(id, chart);
        if (!isCancelled) setSvg(renderedSvg);
      } catch {
        if (!isCancelled) setSvg(`<div class="text-red-500 p-4 border border-red-200 rounded">Failed to render flowchart</div>`);
      }
    };
    renderChart();
    return () => { isCancelled = true; };
  }, [chart, id]);

  return (
    <div 
      className="my-4 flex justify-center bg-white dark:bg-slate-800 p-4 rounded-lg border border-slate-200 dark:border-slate-700 overflow-x-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
};

type MarkdownCodeProps = React.HTMLAttributes<HTMLElement> & {
  className?: string;
  children?: React.ReactNode;
};

type MarkdownTableProps = React.TableHTMLAttributes<HTMLTableElement>;
type MarkdownCellProps = React.ThHTMLAttributes<HTMLTableCellElement>;
type MarkdownDataCellProps = React.TdHTMLAttributes<HTMLTableCellElement>;
type MarkdownLinkProps = React.AnchorHTMLAttributes<HTMLAnchorElement>;

const markdownComponents = {
  code({ className, children, ...props }: MarkdownCodeProps) {
    const match = /language-(\w+)/.exec(className || '');
    const rawValue = String(children ?? '');
    const isInline = !match && !rawValue.includes('\n');
    
    if (isInline) {
      return (
        <code className="bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded-md text-[13.5px] font-mono text-slate-800 dark:text-slate-200 before:content-none after:content-none border border-slate-200 dark:border-slate-700" {...props}>
          {children}
        </code>
      );
    }
    const lang = match ? match[1] : '';
    return <CodeBlock language={lang} value={rawValue} />;
  },
  table({ children, ...props }: MarkdownTableProps) {
    return (
      <div className="overflow-x-auto my-4 border border-slate-200 dark:border-slate-700 rounded-lg">
        <table className="w-full text-sm text-left my-0" {...props}>
          {children}
        </table>
      </div>
    );
  },
  th({ children, ...props }: MarkdownCellProps) {
    return <th className="bg-slate-50 dark:bg-slate-800/50 px-4 py-2 font-semibold border-b border-slate-200 dark:border-slate-700" {...props}>{children}</th>;
  },
  td({ children, ...props }: MarkdownDataCellProps) {
    return <td className="px-4 py-2 border-b border-slate-100 dark:border-slate-800 last:border-0" {...props}>{children}</td>;
  },
  a({ children, href, ...props }: MarkdownLinkProps) {
     return <a href={href} className="text-blue-600 dark:text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer" {...props}>{children}</a>
  }
};

export const MessageMarkdown: React.FC<{ content: string }> = React.memo(({ content }) => {
  const processedContent = preprocessMarkdown(content);
  
  return (
    <div className="prose prose-slate dark:prose-invert max-w-none break-words text-[15px] leading-7 prose-headings:mb-3 prose-headings:mt-6 prose-headings:font-semibold prose-headings:text-slate-900 dark:prose-headings:text-slate-50 prose-h1:text-[1.95rem] prose-h1:leading-tight prose-h1:tracking-[-0.02em] prose-h2:text-[1.55rem] prose-h2:leading-tight prose-h2:tracking-[-0.015em] prose-h3:text-[1.2rem] prose-h3:leading-snug prose-p:my-3 prose-p:leading-7 prose-strong:text-slate-900 dark:prose-strong:text-slate-100 prose-ol:my-3 prose-ul:my-3 prose-li:my-1.5 prose-li:leading-7 prose-hr:my-5 prose-hr:border-slate-200 dark:prose-hr:border-slate-700 prose-pre:m-0 prose-pre:bg-transparent prose-pre:p-0 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={markdownComponents}
      >
        {processedContent}
      </ReactMarkdown>
    </div>
  );
});
