import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Ellipsis } from "lucide-react";
import { Fragment } from "react";

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

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="icon-button more-actions-trigger" type="button" aria-label={label} title={label}>
          <Ellipsis size={16} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="more-actions-menu" align="end" sideOffset={6} collisionPadding={12}>
          {items.map((item, index) => (
            <Fragment key={item.label}>
              {index === firstDanger && index > 0 && <DropdownMenu.Separator className="more-actions-separator" />}
              <DropdownMenu.Item
                className={`more-actions-item${item.danger ? " danger" : ""}`}
                disabled={item.disabled}
                onSelect={item.onSelect}
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
