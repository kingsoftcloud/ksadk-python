import * as Popover from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Check, ChevronDown, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

export interface StudioMultiSelectProps<T> {
  ariaLabel: string;
  items: T[];
  selectedIds: string[];
  getId: (item: T) => string;
  getLabel: (item: T) => string;
  getDescription?: (item: T) => string;
  onChange: (ids: string[]) => void;
  searchPlaceholder?: string;
  emptyMessage?: string;
  disabledIds?: Iterable<string>;
}

export function StudioMultiSelect<T>({
  ariaLabel,
  items,
  selectedIds,
  getId,
  getLabel,
  getDescription = () => "",
  onChange,
  searchPlaceholder = "搜索",
  emptyMessage = "没有匹配项",
  disabledIds = [],
}: StudioMultiSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [selectedOnly, setSelectedOnly] = useState(false);
  const disabled = useMemo(() => new Set(disabledIds), [disabledIds]);
  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const selectedItems = items.filter(item => selected.has(getId(item)));
  const visibleItems = selectedOnly ? selectedItems : items;

  function toggle(id: string) {
    if (disabled.has(id)) return;
    onChange(selected.has(id)
      ? selectedIds.filter(value => value !== id)
      : [...selectedIds, id]);
  }

  return (
    <div className="studio-multi-select">
      <div className="studio-multi-select-summary">
        <span>{selectedIds.length ? `已选 ${selectedIds.length} 个` : "尚未选择"}</span>
        {selectedIds.length ? (
          <button className="text-button" type="button" onClick={() => onChange([])}>清空</button>
        ) : null}
      </div>
      <div className="studio-multi-select-selection" data-testid="studio-multi-select-selection">
        {selectedItems.map(item => {
          const id = getId(item);
          const label = getLabel(item);
          return (
            <span className="studio-selection-chip" key={id}>
              <span title={label}>{label}</span>
              {!disabled.has(id) ? (
                <button type="button" aria-label={`移除 ${label}`} onClick={() => toggle(id)}>
                  <X size={12} />
                </button>
              ) : null}
            </span>
          );
        })}
      </div>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button
            className="studio-multi-select-trigger"
            type="button"
            aria-label={ariaLabel}
            aria-expanded={open}
          >
            <span>{selectedIds.length ? "继续选择" : ariaLabel}</span>
            <ChevronDown size={15} />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            className="studio-multi-select-popover"
            align="start"
            sideOffset={6}
            collisionPadding={12}
          >
            <Command loop label={searchPlaceholder}>
              <div className="studio-command-search">
                <Search size={14} aria-hidden="true" />
                <Command.Input aria-label={searchPlaceholder} placeholder={searchPlaceholder} autoFocus />
              </div>
              <div className="studio-multi-select-tools">
                <button
                  className={selectedOnly ? "selected" : ""}
                  type="button"
                  aria-pressed={selectedOnly}
                  onClick={() => setSelectedOnly(value => !value)}
                >
                  仅看已选
                </button>
                <span>{visibleItems.length} 项</span>
              </div>
              <Command.List className="studio-command-list">
                <Command.Empty>{emptyMessage}</Command.Empty>
                {visibleItems.map(item => {
                  const id = getId(item);
                  const label = getLabel(item);
                  const description = getDescription(item);
                  const checked = selected.has(id);
                  const itemDisabled = disabled.has(id);
                  return (
                    <Command.Item
                      key={id}
                      value={`${label} ${description} ${id}`}
                      disabled={itemDisabled}
                      onSelect={() => toggle(id)}
                    >
                      <span className="studio-option-check" role="checkbox" aria-checked={checked}>
                        {checked ? <Check size={13} /> : null}
                      </span>
                      <span className="studio-option-copy">
                        <strong>{label}</strong>
                        {description ? <small>{description}</small> : null}
                      </span>
                    </Command.Item>
                  );
                })}
              </Command.List>
            </Command>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
