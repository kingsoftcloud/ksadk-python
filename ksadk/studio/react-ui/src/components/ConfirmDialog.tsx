import { CircleAlert, Info } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { StudioDialog } from "./ui/StudioDialog";

export function ConfirmDialog({
  title,
  description,
  confirmText = "确认",
  danger = true,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  description?: string;
  confirmText?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <StudioDialog
      open
      onOpenChange={open => { if (!open && !busy) onCancel(); }}
      title={title}
      description={description}
      closeDisabled={busy}
      showClose={false}
      role="alertdialog"
      className="confirm-dialog"
      icon={(
        <span className={`confirm-icon${danger ? " danger" : ""}`} style={danger ? undefined : { background: "var(--accent-soft)", color: "var(--accent-strong)" }}>
          {danger ? <CircleAlert size={18} /> : <Info size={18} />}
        </span>
      )}
      footer={(
        <>
          <Dialog.Close asChild>
            <button className="button secondary" type="button" disabled={busy}>取消</button>
          </Dialog.Close>
          <button className={`button ${danger ? "danger" : "accent"}`} type="button" onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmText}
          </button>
        </>
      )}
    />
  );
}
