export interface RunErrorCopy {
  title: string;
  message: string;
  recoverable: "credential" | "model" | "retry";
}
export function redactTechnicalError(value: string): string {
  return value
    .replace(/(api[_ -]?key|authorization|bearer)(\s*[:=]\s*)[^\s,;]+/gi, "$1$2***")
    .replace(/\bsk-[A-Za-z0-9_-]{6,}\b/g, "sk-***")
    .replace(/\b[A-Za-z0-9_-]{24,}\.{0,3}\b/g, match => (
      match.startsWith("run_") || match.startsWith("resp_") ? match : "***"
    ));
}

export function runErrorCopy(value: string): RunErrorCopy {
  const normalized = value.toLocaleLowerCase();
  if (/api.?key|credential|auth|unauthorized|401|403|凭证/.test(normalized)) {
    return {
      title: "模型凭证无效或已过期",
      message: "请更新模型 API Key，验证连接后重新运行本轮消息。",
      recoverable: "credential",
    };
  }
  if (/must configure model|model.*not configured|未配置模型|未绑定模型/.test(normalized)) {
    return {
      title: "当前 Agent 尚未绑定模型",
      message: "先在 Agent 配置中绑定一个可用模型，再重新运行本轮消息。",
      recoverable: "model",
    };
  }
  return {
    title: "Agent 运行失败",
    message: "本轮没有生成结果。可以重新运行；若问题持续，请展开技术详情定位原因。",
    recoverable: "retry",
  };
}
