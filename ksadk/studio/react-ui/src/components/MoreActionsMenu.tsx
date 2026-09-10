import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Ellipsis } from "lucide-react";
import { Fragment, useRef } from "react";

export interface MoreActionItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

export function MoreActionsMenu({
  items,
  label = "更多操作",
}: {
  items: MoreActionItem[];
  label?: string;
}) {
  const firstDanger = items.findIndex(item => item.danger);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const pendingActionRef = useRef<(() => void) | null>(null);

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button ref={triggerRef} className="icon-button more-actions-trigger" type="button" aria-label={label} title={label}>
          <Ellipsis size={16} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="more-actions-menu" align="end" sideOffset={6} collisionPadding={12}
          onCloseAutoFocus={event => {
            if (!pendingActionRef.current) return;
            event.preventDefault();
            const action = pendingActionRef.current;
            pendingActionRef.current = null;
            triggerRef.current?.focus();
            action();
          }}>
          {items.map((item, index) => (
            <Fragment key={item.label}>
              {index === firstDanger && index > 0 && <DropdownMenu.Separator className="more-actions-separator" />}
              <DropdownMenu.Item
                className={`more-actions-item${item.danger ? " danger" : ""}`}
                disabled={item.disabled}
                onSelect={() => {
                  // Close the menu's focus scope before opening another surface.
                  pendingActionRef.current = item.onSelect;
                }}
              >
                {item.label}
              </DropdownMenu.Item>
            </Fragment>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
