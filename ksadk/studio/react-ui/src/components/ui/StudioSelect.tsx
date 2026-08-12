import * as Select from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";

export interface StudioSelectOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
}

export function StudioSelect({
  id,
  ariaLabel,
  value,
  placeholder = "请选择",
  options,
  disabled = false,
  className = "",
  onValueChange,
}: {
  id?: string;
  ariaLabel: string;
  value: string;
  placeholder?: string;
  options: StudioSelectOption[];
  disabled?: boolean;
  className?: string;
  onValueChange: (value: string) => void;
}) {
  const selected = options.find(option => option.value === value);
  return (
    <Select.Root value={value || undefined} disabled={disabled} onValueChange={onValueChange}>
      <Select.Trigger
        id={id}
        className={`studio-select-trigger${className ? ` ${className}` : ""}`}
        aria-label={ariaLabel}
      >
        <Select.Value placeholder={placeholder}>{selected?.label}</Select.Value>
        <Select.Icon className="studio-select-chevron"><ChevronDown size={15} /></Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="studio-select-content" position="popper" sideOffset={5} collisionPadding={10}>
          <Select.ScrollUpButton className="studio-select-scroll"><ChevronUp size={14} /></Select.ScrollUpButton>
          <Select.Viewport className="studio-select-viewport">
            {options.map(option => (
              <Select.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className="studio-select-item"
              >
                <Select.ItemText>
                  <span className="studio-select-item-copy">
                    <span>{option.label}</span>
                    {option.description && <small>{option.description}</small>}
                  </span>
                </Select.ItemText>
                <Select.ItemIndicator className="studio-select-check"><Check size={14} /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
          <Select.ScrollDownButton className="studio-select-scroll"><ChevronDown size={14} /></Select.ScrollDownButton>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
