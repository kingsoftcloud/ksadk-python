export type CollaborationMode = "default" | "plan";

export interface ComposerCommand {
  id: "plan" | "goal" | "default";
  slash: string;
  label: string;
  description: string;
}

export interface ComposerAttachment {
  id: string;
  kind: "image" | "text";
  name: string;
  mimeType: string;
  size: number;
  dataUrl?: string;
  text?: string;
}

export const COMPOSER_COMMANDS: ComposerCommand[] = [
  { id: "plan", slash: "/plan", label: "计划模式", description: "下一轮只分析并形成可执行计划" },
  { id: "goal", slash: "/goal", label: "设定长期目标", description: "启动可暂停、可持续的 Codex Goal" },
  { id: "default", slash: "/default", label: "默认模式", description: "返回直接执行模式" },
];

export function visibleComposerCommands(input: string): ComposerCommand[] {
  if (!input.startsWith("/") || /\s/.test(input)) return [];
  const query = input.toLocaleLowerCase();
  return COMPOSER_COMMANDS.filter(command => command.slash.startsWith(query));
}

export type ComposerSubmission =
  | { kind: "toggle-plan" }
  | { kind: "set-default" }
  | { kind: "goal"; objective: string }
  | { kind: "message"; text: string };

export function parseComposerSubmission(input: string): ComposerSubmission {
  const trimmed = input.trim();
  if (trimmed === "/plan") return { kind: "toggle-plan" };
  if (trimmed === "/default") return { kind: "set-default" };
  if (trimmed === "/goal" || trimmed.startsWith("/goal ")) {
    return { kind: "goal", objective: trimmed.slice(5).trim() };
  }
  return { kind: "message", text: trimmed };
}

export function buildResponsesInput(text: string, attachments: ComposerAttachment[]) {
  const content: Array<Record<string, string>> = [];
  if (text.trim()) content.push({ type: "input_text", text: text.trim() });
  for (const attachment of attachments) {
    if (attachment.kind === "image" && attachment.dataUrl) {
      content.push({
        type: "input_image",
        image_url: attachment.dataUrl,
        filename: attachment.name,
      });
    } else if (attachment.kind === "text") {
      content.push({
        type: "input_text",
        text: `\n\n<attachment name="${attachment.name.replaceAll('"', "&quot;")}">\n${attachment.text || ""}\n</attachment>`,
      });
    }
  }
  return [{ role: "user", content }];
}

const TEXT_EXTENSIONS = new Set([
  "txt", "md", "json", "yaml", "yml", "csv", "ts", "tsx", "js", "jsx",
  "py", "go", "rs", "java", "sh", "css", "html", "xml", "toml", "ini", "log",
]);

export const COMPOSER_ATTACHMENT_ACCEPT = [
  "image/*", ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".ts", ".tsx",
  ".js", ".jsx", ".py", ".go", ".rs", ".java", ".sh", ".css", ".html", ".xml",
  ".toml", ".ini", ".log",
].join(",");

export const MAX_COMPOSER_ATTACHMENTS = 4;
export const MAX_COMPOSER_ATTACHMENT_BYTES = 1_500_000;

export function encodedComposerAttachmentsBytes(attachments: ComposerAttachment[]): number {
  return new TextEncoder().encode(JSON.stringify(buildResponsesInput("", attachments))).byteLength;
}

function fileExtension(name: string): string {
  return name.split(".").at(-1)?.toLocaleLowerCase() || "";
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取附件 ${file.name}`));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
}

function readText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取附件 ${file.name}`));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsText(file);
  });
}

export async function fileToComposerAttachment(file: File): Promise<ComposerAttachment> {
  const base = {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    name: file.name,
    mimeType: file.type || "application/octet-stream",
    size: file.size,
  };
  if (file.type.startsWith("image/")) {
    return { ...base, kind: "image", dataUrl: await readDataUrl(file) };
  }
  if (file.type.startsWith("text/") || TEXT_EXTENSIONS.has(fileExtension(file.name))) {
    return { ...base, kind: "text", text: await readText(file) };
  }
  throw new Error(`暂不支持 ${file.name}；请添加图片或 UTF-8 文本/代码文件`);
}

export function formatAttachmentSize(size: number): string {
  return size < 1024 ? `${size} B` : `${Math.max(0.1, size / 1024).toFixed(1)} KiB`;
}
