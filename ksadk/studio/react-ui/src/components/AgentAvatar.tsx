import type { CSSProperties } from "react";
import { Bot, Code2, Network, Search, Sparkles } from "lucide-react";

export interface AgentAppearance {
  icon?: "bot" | "sparkles" | "search" | "code" | "workflow";
  color?: string;
  imageUrl?: string | null;
}

const iconComponents = {
  bot: Bot,
  sparkles: Sparkles,
  search: Search,
  code: Code2,
  workflow: Network,
};

export function AgentAvatar({
  name,
  appearance,
  template,
  size = "md",
  className = "",
}: {
  name: string;
  appearance?: AgentAppearance;
  template?: string;
  size?: "xs" | "sm" | "md" | "lg";
  className?: string;
}) {
  const icon = appearance?.icon || (template === "research" ? "search" : "bot");
  const Icon = iconComponents[icon];
  const color = appearance?.color || (template === "research" ? "#2d7c68" : "#426ea8");
  const style = { "--agent-avatar-color": color } as CSSProperties;

  return (
    <span
      className={`agent-avatar agent-avatar-${size}${className ? ` ${className}` : ""}`}
      style={style}
      role="img"
      aria-label={`${name}头像`}
    >
      {appearance?.imageUrl
        ? <img src={appearance.imageUrl} alt="" draggable={false} />
        : <Icon aria-hidden size={size === "lg" ? 22 : size === "xs" ? 12 : 16} />}
    </span>
  );
}
