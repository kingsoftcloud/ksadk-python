import { z } from "zod";

const displayName = z.string().trim().min(1, "请填写显示名称").max(128, "显示名称不能超过 128 个字符");
const resourceName = z.string().trim()
  .min(2, "资源标识至少填写 2 个字符")
  .max(128, "资源标识不能超过 128 个字符")
  .regex(/^[a-z][a-z0-9._-]*$/, "资源标识只能包含小写字母、数字、点、下划线和连字符");
const pythonIdentifier = z.string().trim()
  .min(1, "请填写 Python 标识")
  .max(128, "Python 标识不能超过 128 个字符")
  .regex(/^[A-Za-z_][A-Za-z0-9_]*$/, "请输入有效的 Python 标识");
const endpointUrl = z.string().trim().url("请输入有效的接口地址").max(1024, "接口地址不能超过 1024 个字符");
const credentialRef = z.string().trim()
  .max(512, "凭证引用不能超过 512 个字符")
  .regex(/^env:\/\/[A-Za-z_][A-Za-z0-9_]*$/, "凭证引用需使用 env://环境变量名");
const description = z.string().trim().max(4096, "描述不能超过 4096 个字符").default("");

export const modelProfileSchema = z.object({
  name: resourceName,
  displayName,
  model: z.string().trim().min(1, "请填写模型 ID").max(256, "模型 ID 不能超过 256 个字符"),
  endpointUrl,
  credentialRef,
  description: description.optional(),
  apiKey: z.string().max(16384, "API Key 不能超过 16384 个字符").default(""),
  temperature: z.coerce.number().min(0, "temperature 需在 0-2 之间").max(2, "temperature 需在 0-2 之间").default(0.2),
  maxTokens: z.coerce.number().int("max_tokens 需为 1-131072 的整数").min(1, "max_tokens 需为 1-131072 的整数").max(131072, "max_tokens 需为 1-131072 的整数").default(2048),
  addressMode: z.enum(["endpoint", "base"]).default("endpoint"),
  wireApi: z.enum(["", "chat", "responses"]).default(""),
});

const mcpBaseSchema = z.object({
  displayName,
  name: resourceName,
  transport: z.enum(["stdio", "sse", "http", "streamable-http"]),
  endpointUrl: z.string().trim().max(1024, "Server URL 不能超过 1024 个字符").default(""),
  command: z.string().trim().max(4096, "Command 不能超过 4096 个字符").default(""),
  args: z.string().trim().max(8192, "Arguments 不能超过 8192 个字符").default(""),
  apiKeyName: z.union([
    z.literal(""),
    z.string().trim().regex(/^[A-Za-z_][A-Za-z0-9_]*$/, "环境变量名需为合法标识符"),
  ]).default(""),
  apiKeyValue: z.string().max(16384, "API Key 不能超过 16384 个字符").default(""),
  description,
});

export const mcpSchema = mcpBaseSchema.superRefine((value, context) => {
  if (value.transport === "stdio") {
    if (!value.command) {
      context.addIssue({ code: "custom", path: ["command"], message: "请填写 Command" });
    }
    return;
  }
  if (!value.endpointUrl) {
    context.addIssue({ code: "custom", path: ["endpointUrl"], message: "请填写 Server URL" });
    return;
  }
  if (!z.url().safeParse(value.endpointUrl).success) {
    context.addIssue({ code: "custom", path: ["endpointUrl"], message: "请输入有效的 Server URL" });
  }
});

export const pythonToolSchema = z.object({
  displayName,
  name: pythonIdentifier,
  callableName: pythonIdentifier,
  description: z.string().trim().max(1024, "描述不能超过 1024 个字符").default(""),
  sourceMode: z.enum(["upload", "workspace"]).default("upload"),
  sourcePath: z.string().trim().max(4096, "工作区路径不能超过 4096 个字符").default(""),
}).superRefine((value, context) => {
  if (value.sourceMode === "workspace" && !value.sourcePath) {
    context.addIssue({ code: "custom", path: ["sourcePath"], message: "请填写工作区 Python 文件" });
  }
});

export const credentialValueSchema = z.object({
  value: z.string().max(16384, "API Key 不能超过 16384 个字符"),
});

export const settingsSchema = z.object({
  sandbox: z.enum(["read-only", "read_only", "workspace-write", "workspace-write-auto", "full-access"]),
  buildAfterCreate: z.boolean(),
  codexProxy: z.enum(["auto", "forced", "direct"]),
  cloudAccessKey: z.string().max(1024, "Access Key 不能超过 1024 个字符").default(""),
  cloudSecretKey: z.string().max(4096, "Secret Key 不能超过 4096 个字符").default(""),
  cloudRegion: z.string().trim().max(128, "Region 不能超过 128 个字符").default(""),
});

export type ModelProfileFormValues = z.infer<typeof modelProfileSchema>;
export type McpFormValues = z.infer<typeof mcpSchema>;
export type PythonToolFormValues = z.infer<typeof pythonToolSchema>;
export type CredentialValueFormValues = z.infer<typeof credentialValueSchema>;
export type SettingsFormValues = z.infer<typeof settingsSchema>;
