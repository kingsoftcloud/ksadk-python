import { marked } from "marked";
import { normalizeCapabilities } from "@kingsoftcloud/ksadk-web/capabilities";

function normalizeStreamingMarkdown(content: string): string {
  const normalized = String(content || "").replace(/\r\n?/g, "\n").trim();
  const fences = normalized.match(/```/g)?.length || 0;
  return fences % 2 === 1 ? `${normalized}\n\`\`\`` : normalized;
}

const styles = `
  :host { display: block; color: inherit; font: inherit; }
  * { box-sizing: border-box; }
  .markdown { max-width: 100%; color: inherit; font: inherit; line-height: 1.72; overflow-wrap: anywhere; }
  .markdown > :first-child { margin-top: 0; }
  .markdown > :last-child { margin-bottom: 0; }
  h1, h2, h3 { margin: 1.2em 0 .5em; color: inherit; line-height: 1.35; }
  h1 { font-size: 1.35em; }
  h2 { font-size: 1.2em; }
  h3 { font-size: 1.08em; }
  p { margin: .62em 0; }
  ul, ol { margin: .62em 0; padding-left: 1.45em; }
  li { margin: .28em 0; }
  strong { font-weight: 650; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  blockquote { margin: .75em 0; padding-left: 1em; border-left: 2px solid #cbd5e1; color: #64748b; }
  code { padding: .12em .35em; border: 1px solid #e2e8f0; border-radius: 5px; background: #f8fafc; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre { max-width: 100%; overflow: auto; padding: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }
  pre code { padding: 0; border: 0; background: transparent; }
  table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
  th, td { padding: 7px 9px; border-bottom: 1px solid #e2e8f0; text-align: left; }
`;

const allowedTags = new Set([
  "A", "BLOCKQUOTE", "BR", "CODE", "DEL", "DIV", "EM", "H1", "H2", "H3",
  "H4", "HR", "LI", "OL", "P", "PRE", "SPAN", "STRONG", "TABLE", "TBODY",
  "TD", "TH", "THEAD", "TR", "UL"
]);

function sanitize(html: string): DocumentFragment {
  const template = document.createElement("template");
  template.innerHTML = html;
  for (const element of Array.from(template.content.querySelectorAll("*"))) {
    if (!allowedTags.has(element.tagName)) {
      element.replaceWith(...Array.from(element.childNodes));
      continue;
    }
    for (const attribute of Array.from(element.attributes)) {
      const isSafeLink = element.tagName === "A" && attribute.name === "href";
      if (!isSafeLink) element.removeAttribute(attribute.name);
    }
    if (element.tagName === "A") {
      const link = element as HTMLAnchorElement;
      if (!/^(https?:|mailto:)/i.test(link.getAttribute("href") || "")) {
        link.removeAttribute("href");
      }
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  }
  return template.content;
}

class KsadkMessageElement extends HTMLElement {
  private value = "";
  private mount: HTMLDivElement;

  constructor() {
    super();
    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = styles;
    this.mount = document.createElement("div");
    this.mount.className = "markdown";
    shadow.append(style, this.mount);
  }

  connectedCallback(): void {
    this.renderContent();
  }

  set content(value: string) {
    this.value = String(value ?? "");
    this.renderContent();
  }

  get content(): string {
    return this.value;
  }

  private renderContent(): void {
    const normalized = normalizeStreamingMarkdown(this.value);
    const html = marked.parse(normalized, { async: false, gfm: true, breaks: true });
    this.mount.replaceChildren(sanitize(String(html)));
  }
}

if (!customElements.get("ksadk-message")) {
  customElements.define("ksadk-message", KsadkMessageElement);
}

Object.assign(window, {
  AgentKitSharedWeb: Object.freeze({ normalizeCapabilities })
});

export { KsadkMessageElement };
