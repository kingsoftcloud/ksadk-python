import { z } from "zod";

const agentName = z.string().trim().min(1, "请填写 Agent 名称").max(128, "Agent 名称不能超过 128 个字符");
const agentSlug = z.string().trim()
  .min(3, "本地标识至少填写 3 个字符")
  .max(63, "本地标识不能超过 63 个字符")
  .regex(/^[a-z][a-z0-9-]*$/, "本地标识只能包含小写字母、数字和连字符");
const optionalAgentSlug = z.union([z.literal(""), agentSlug]);
const runtimeType = z.enum(["codex", "adk", "langgraph"]);
const agentPrompt = z.string().trim()
  .min(4, "系统提示词至少填写 4 个字符")
  .max(32768, "系统提示词不能超过 32768 个字符");
const description = z.string().trim().max(1024, "描述不能超过 1024 个字符").default("");

export const quickAgentSchema = z.object({
  name: agentName,
  slug: agentSlug,
  runtimeType,
  template: z.enum(["blank", "research"]).default("blank"),
  prompt: agentPrompt,
  description,
  audience: z.string().trim().max(256, "目标读者不能超过 256 个字符").default(""),
  language: z.enum(["zh-CN", "en-US"]).default("zh-CN"),
  depth: z.enum(["focused", "standard", "deep"]).default("deep"),
  format: z.enum(["report", "brief", "evidence-table"]).default("report"),
  systemPrompt: z.string().max(32768, "系统提示词不能超过 32768 个字符").default(""),
  taskPrompt: z.string().max(32768, "任务契约不能超过 32768 个字符").default(""),
  buildAfterCreate: z.boolean().default(true),
}).superRefine((value, context) => {
  if (value.template === "research" && !value.audience) {
    context.addIssue({ code: "custom", path: ["audience"], message: "请填写目标读者" });
  }
});

export const conversationCommitSchema = z.object({
  name: agentName,
  slug: agentSlug,
  runtimeType,
  prompt: agentPrompt,
  description: description.optional(),
  modelProfileId: z.string().trim().min(3, "请选择用于构建的模型").max(256).optional(),
});

export const agentImportSchema = z.object({
  name: agentName,
  slug: optionalAgentSlug.default(""),
});

export const projectImportSchema = z.object({
  name: agentName,
  slug: optionalAgentSlug.default(""),
  path: z.string().trim().min(1, "请选择项目目录").max(4096, "项目路径不能超过 4096 个字符"),
});

export type QuickAgentFormValues = z.infer<typeof quickAgentSchema>;
export type ConversationCommitFormValues = z.infer<typeof conversationCommitSchema>;
export type AgentImportFormValues = z.infer<typeof agentImportSchema>;
export type ProjectImportFormValues = z.infer<typeof projectImportSchema>;
