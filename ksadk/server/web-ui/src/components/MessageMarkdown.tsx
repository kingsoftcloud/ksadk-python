import React, { Suspense } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { preprocessMarkdown } from '../utils/markdown.js';

const LazyCodeBlock = React.lazy(() =>
  import('./markdown/CodeBlock.js').then((m) => ({ default: m.CodeBlock }))
);

const LazyMermaidBlock = React.lazy(() =>
  import('./markdown/MermaidBlock.js').then((m) => ({ default: m.MermaidBlock }))
);

const LazyMathMessageMarkdown = React.lazy(() =>
  import('./markdown/MathMessageMarkdown.js').then((m) => ({
    default: m.MathMessageMarkdown,
  }))
);

function hasMath(content: string): boolean {
  return /\$[^$]+\$|\$\$[^$]+\$\$/.test(content);
}

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

    if (lang === 'mermaid') {
      return (
        <Suspense fallback={<div className="h-32 animate-pulse bg-slate-100 dark:bg-slate-800 rounded" />}>
          <LazyMermaidBlock chart={rawValue} />
        </Suspense>
      );
    }

    return (
      <Suspense fallback={<pre className="p-4 text-sm">{rawValue}</pre>}>
        <LazyCodeBlock language={lang} value={rawValue} />
      </Suspense>
    );
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

const PlainMarkdown: React.FC<{ content: string }> = React.memo(({ content }) => {
  const processedContent = preprocessMarkdown(content);

  return (
    <div className="prose prose-slate dark:prose-invert max-w-none break-words text-[15px] leading-7 prose-headings:mb-3 prose-headings:mt-6 prose-headings:font-semibold prose-headings:text-slate-900 dark:prose-headings:text-slate-50 prose-h1:text-[1.95rem] prose-h1:leading-tight prose-h1:tracking-[-0.02em] prose-h2:text-[1.55rem] prose-h2:leading-tight prose-h2:tracking-[-0.015em] prose-h3:text-[1.2rem] prose-h3:leading-snug prose-p:my-3 prose-p:leading-7 prose-strong:text-slate-900 dark:prose-strong:text-slate-100 prose-ol:my-3 prose-ul:my-3 prose-li:my-1.5 prose-li:leading-7 prose-hr:my-5 prose-hr:border-slate-200 dark:prose-hr:border-slate-700 prose-pre:m-0 prose-pre:bg-transparent prose-pre:p-0 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownComponents}
      >
        {processedContent}
      </ReactMarkdown>
    </div>
  );
});

export const MessageMarkdown: React.FC<{ content: string }> = React.memo(({ content }) => {
  if (hasMath(content)) {
    return (
      <Suspense fallback={<PlainMarkdown content={content} />}>
        <LazyMathMessageMarkdown content={content} />
      </Suspense>
    );
  }

  return <PlainMarkdown content={content} />;
});