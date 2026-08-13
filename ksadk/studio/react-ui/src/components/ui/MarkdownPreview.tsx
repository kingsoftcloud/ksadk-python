import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeViewer } from "./CodeViewer";

export interface MarkdownPreviewProps {
  content: string;
}

function splitFrontmatter(content: string): { frontmatter: string; markdown: string } {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  return match
    ? { frontmatter: match[1].trim(), markdown: content.slice(match[0].length) }
    : { frontmatter: "", markdown: content };
}

export function MarkdownPreview({ content }: MarkdownPreviewProps) {
  const { frontmatter, markdown } = splitFrontmatter(content);

  return (
    <div className="markdown-preview-shell">
      {frontmatter && (
        <details className="markdown-frontmatter">
          <summary>文档元数据</summary>
          <CodeViewer
            code={frontmatter}
            language="yaml"
            filename="frontmatter.yaml"
            showLineNumbers={false}
            wrap
          />
        </details>
      )}
      <article className="markdown-preview">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            pre: ({ children }) => <>{children}</>,
            code: ({ className, children, ...props }) => {
              const language = /language-([\w-]+)/.exec(className || "")?.[1];
              if (language) {
                return (
                  <CodeViewer
                    code={String(children).replace(/\n$/, "")}
                    language={language}
                    showLineNumbers={false}
                  />
                );
              }
              return <code className={className} {...props}>{children}</code>;
            },
            table: ({ children, ...props }) => (
              <div className="markdown-table-scroll">
                <table {...props}>{children}</table>
              </div>
            ),
            a: ({ children, ...props }) => (
              <a {...props} target="_blank" rel="noreferrer noopener">{children}</a>
            ),
          }}
        >
          {markdown}
        </ReactMarkdown>
      </article>
    </div>
  );
}
