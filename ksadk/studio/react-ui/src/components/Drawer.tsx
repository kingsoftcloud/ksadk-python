import type { ReactNode } from "react";
import { CircleAlert, CircleCheck } from "lucide-react";
import { StudioDrawer } from "./ui/StudioDialog";

/** Studio 通用 overlay + drawer，由 React-owned studio.css 提供基础样式。 */
export function Drawer({
  title,
  subtitle,
  wide = false,
  closeDisabled = false,
  onClose,
  footer,
  children,
}: {
  title: string;
  subtitle?: string;
  wide?: boolean;
  closeDisabled?: boolean;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <StudioDrawer
      open
      onOpenChange={open => { if (!open) onClose(); }}
      title={title}
      subtitle={subtitle}
      wide={wide}
      closeDisabled={closeDisabled}
      footer={footer}
    >
      {children}
    </StudioDrawer>
  );
}

export function InlineAlert({ kind, title, message }: { kind: "error" | "warning" | "success"; title: string; message?: string }) {
  return (
    <div className={`inline-alert ${kind}`}>
      {kind === "success" ? <CircleCheck size={15} /> : <CircleAlert size={15} />}
      <div>
        <strong>{title}</strong>
        {message ? <p>{message}</p> : null}
      </div>
    </div>
  );
}
