import { Highlight, type PrismTheme } from "prism-react-renderer";

const studioCodeTheme: PrismTheme = {
  plain: {
    color: "var(--code-text)",
    backgroundColor: "transparent",
  },
  styles: [
    {
      types: ["comment", "prolog", "doctype", "cdata"],
      style: { color: "var(--code-token-comment)", fontStyle: "italic" },
    },
    {
      types: ["punctuation"],
      style: { color: "var(--code-token-punctuation)" },
    },
    {
      types: ["property", "tag", "constant", "symbol", "deleted", "attr-name"],
      style: { color: "var(--code-token-property)" },
    },
    {
      types: ["boolean", "number"],
      style: { color: "var(--code-token-number)" },
    },
    {
      types: ["selector", "string", "char", "builtin", "inserted", "attr-value"],
      style: { color: "var(--code-token-string)" },
    },
    {
      types: ["operator", "entity", "url"],
      style: { color: "var(--code-token-operator)" },
    },
    {
      types: ["atrule", "keyword"],
      style: { color: "var(--code-token-keyword)" },
    },
    {
      types: ["function"],
      style: { color: "var(--code-token-function)" },
    },
    {
      types: ["class-name"],
      style: { color: "var(--code-token-class)" },
    },
    {
      types: ["regex", "important", "variable"],
      style: { color: "var(--code-token-variable)" },
    },
  ],
};

export interface PrismRendererProps {
  code: string;
  language: string;
  showLineNumbers: boolean;
}

export default function PrismRenderer({
  code,
  language,
  showLineNumbers,
}: PrismRendererProps) {
  return (
    <Highlight code={code} language={language || "text"} theme={studioCodeTheme}>
      {({ className, tokens, getLineProps, getTokenProps }) => (
        <pre className={`${className} code-viewer-pre`}>
          <code>
            {tokens.map((line, lineIndex) => {
              const lineProps = getLineProps({ line });
              return (
                <span
                  {...lineProps}
                  className={`${lineProps.className} code-viewer-line`}
                  key={lineIndex}
                >
                  {showLineNumbers && (
                    <span className="code-viewer-line-number" aria-hidden="true">
                      {lineIndex + 1}
                    </span>
                  )}
                  <span className="code-viewer-line-content">
                    {line.map((token, tokenIndex) => (
                      <span key={tokenIndex} {...getTokenProps({ token })} />
                    ))}
                  </span>
                </span>
              );
            })}
          </code>
        </pre>
      )}
    </Highlight>
  );
}
