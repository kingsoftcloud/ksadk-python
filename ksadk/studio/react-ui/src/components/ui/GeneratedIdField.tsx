import { RefreshCw } from "lucide-react";
import { generateAgentSlug } from "../../lib/generatedId";
import { FormField } from "./FormField";

export function GeneratedIdField({
  value,
  onChange,
  error,
  generate = generateAgentSlug,
  id = "agent-slug",
  label = "本地标识",
}: {
  value: string;
  onChange: (value: string) => void;
  error?: string;
  generate?: () => string;
  id?: string;
  label?: string;
}) {
  return (
    <FormField
      htmlFor={id}
      label={label}
      requirement="generated"
      hint="默认生成唯一的本地 ID；可手动修改，云端 AgentId 由部署服务另行映射。"
      error={error}
    >
      <div className="generated-id-control">
        <input
          id={id}
          value={value}
          onChange={event => onChange(event.target.value)}
          pattern="[a-z][a-z0-9-]{2,62}"
          maxLength={63}
          spellCheck={false}
          autoComplete="off"
        />
        <button
          className="icon-button tertiary"
          type="button"
          aria-label="重新生成本地标识"
          title="重新生成"
          onClick={() => onChange(generate())}
        >
          <RefreshCw size={15} />
        </button>
      </div>
    </FormField>
  );
}
