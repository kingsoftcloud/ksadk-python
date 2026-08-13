import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Check, CornerDownLeft, Pencil, Shield, X } from "lucide-react";
import type { A2UIComponent, A2UISurface } from "../chatProtocol";
import { StudioSelect } from "./ui/StudioSelect";

interface A2UIRendererProps {
  surface: A2UISurface;
  busy?: boolean;
  onSubmit: (interactionId: string, name: string, data: Record<string, unknown>) => void | Promise<void>;
}

function optionsOf(value: unknown): Array<{ label: string; value: string; description: string }> {
  if (!Array.isArray(value)) return [];
  return value.map(item => {
    if (typeof item === "string") return { label: item, value: item, description: "" };
    const record = item && typeof item === "object" ? item as Record<string, unknown> : {};
    const optionValue = String(record.value ?? record.id ?? record.label ?? "");
    return {
      label: String(record.label ?? record.title ?? optionValue),
      value: optionValue,
      description: String(record.description ?? record.help ?? ""),
    };
  }).filter(item => item.value);
}

function childIds(component: A2UIComponent): string[] {
  const raw = Array.isArray(component.children)
    ? component.children
    : typeof component.child === "string" ? [component.child] : [];
  return raw.filter((value): value is string => typeof value === "string");
}

export function A2UIRenderer({ surface, busy = false, onSubmit }: A2UIRendererProps) {
  const [values, setValues] = useState<Record<string, unknown>>(() => ({ ...surface.dataModel }));
  const [customValues, setCustomValues] = useState<Record<string, string>>({});
  const dirtyFields = useRef(new Set<string>());
  const pending = surface.interaction?.status === "pending";
  const disabled = busy || !pending;
  const roots = surface.roots.length ? surface.roots : Object.keys(surface.components).slice(0, 1);

  useEffect(() => {
    setValues(current => {
      const next = { ...current };
      for (const [field, value] of Object.entries(surface.dataModel)) {
        if (!dirtyFields.current.has(field)) next[field] = value;
      }
      return next;
    });
  }, [surface.dataModel]);

  const updateField = (name: string, value: unknown) => {
    dirtyFields.current.add(name);
    setValues(current => ({ ...current, [name]: value }));
  };

  const submit = (name: string, extra: Record<string, unknown> = {}) => {
    if (!surface.interaction || disabled) return;
    const data = { ...values };
    for (const [field, customValue] of Object.entries(customValues)) {
      const trimmed = customValue.trim();
      if (!trimmed) continue;
      const current = data[field];
      data[field] = Array.isArray(current)
        ? [...current.filter(value => String(value) !== trimmed), trimmed]
        : trimmed;
    }
    void onSubmit(surface.interaction.id, name, { ...data, ...extra });
  };

  const renderChoiceGroup = (
    component: A2UIComponent,
    multiple: boolean,
  ) => {
    const name = String(component.name || component.id);
    const options = optionsOf(component.options);
    const selectedMany = Array.isArray(values[name]) ? values[name] as string[] : [];
    const selectedOne = String(values[name] ?? component.value ?? "");
    const allowOther = Boolean(component.allow_other ?? component.allowOther ?? component.is_other ?? component.isOther);
    const customValue = customValues[name] ?? "";
    return (
      <fieldset className="a2ui-field a2ui-options">
        <legend>{String(component.label || component.title || "请选择")}</legend>
        {Boolean(component.description) && <p className="a2ui-field-description">{String(component.description)}</p>}
        <div className="a2ui-choice-list" role={multiple ? "group" : "radiogroup"}>
          {options.map((option, index) => {
            const isSelected = multiple ? selectedMany.includes(option.value) : selectedOne === option.value;
            return (
              <label key={option.value} className={`a2ui-choice${isSelected ? " selected" : ""}`}>
                <input
                  type={multiple ? "checkbox" : "radio"}
                  aria-label={option.label}
                  name={multiple ? undefined : `${surface.id}-${name}`}
                  checked={isSelected}
                  disabled={disabled}
                  onChange={event => {
                    if (multiple) {
                      updateField(
                        name,
                        event.target.checked
                          ? [...selectedMany, option.value]
                          : selectedMany.filter(value => value !== option.value),
                      );
                    } else {
                      setCustomValues(current => ({ ...current, [name]: "" }));
                      updateField(name, option.value);
                    }
                  }}
                />
                <span className="a2ui-choice-index">{index + 1}</span>
                <span className="a2ui-choice-copy">
                  <strong>{option.label}</strong>
                  {option.description && <small>{option.description}</small>}
                </span>
              </label>
            );
          })}
          {allowOther && (
            <label className={`a2ui-other${customValue ? " active" : ""}`}>
              <span className="a2ui-other-icon"><Pencil size={14} /></span>
              <input
                type={Boolean(component.secret) ? "password" : "text"}
                aria-label={`${String(component.label || component.title || name)}自定义输入`}
                value={customValue}
                placeholder={String(component.other_placeholder || component.otherPlaceholder || "其他，请输入…")}
                disabled={disabled}
                onChange={event => {
                  const next = event.target.value;
                  dirtyFields.current.add(name);
                  setCustomValues(current => ({ ...current, [name]: next }));
                  if (!multiple) setValues(current => ({ ...current, [name]: "" }));
                }}
              />
            </label>
          )}
        </div>
      </fieldset>
    );
  };

  const renderNode = (componentId: string): ReactNode => {
    const component = surface.components[componentId];
    if (!component) return null;
    const type = component.component;
    const children = childIds(component).map(child => <div key={child}>{renderNode(child)}</div>);
    if (type === "Card") {
      return (
        <section className="a2ui-card">
          {Boolean(component.title) && <h3>{String(component.title)}</h3>}
          {Boolean(component.body) && <p>{String(component.body)}</p>}
          {children.length > 0 && <div className="a2ui-card-content">{children}</div>}
        </section>
      );
    }
    if (["Column", "Row"].includes(type)) {
      return <div className={`a2ui-layout ${type.toLowerCase()}`}>{children}</div>;
    }
    if (type === "Text") return <p className={`a2ui-text ${String(component.variant || "body")}`}>{String(component.text || "")}</p>;
    if (["TextField", "Input"].includes(type)) {
      const name = String(component.name || component.id);
      return (
        <label className="a2ui-field">
          <span>{String(component.label || component.title || name)}</span>
          <input
            value={String(values[name] ?? component.value ?? "")}
            placeholder={String(component.placeholder || "")}
            disabled={disabled}
            onChange={event => updateField(name, event.target.value)}
          />
        </label>
      );
    }
    if (["Select", "RadioGroup", "MultipleChoice"].includes(type)) {
      const name = String(component.name || component.id);
      const options = optionsOf(component.options);
      const multiple = type === "MultipleChoice" && Boolean(component.multiple);
      if (type === "RadioGroup" || type === "MultipleChoice") return renderChoiceGroup(component, multiple);
      return (
        <div className="a2ui-field">
          <span>{String(component.label || component.title || "请选择")}</span>
          <StudioSelect
            ariaLabel={String(component.label || component.title || "请选择")}
            value={String(values[name] ?? component.value ?? "")}
            options={options}
            disabled={disabled}
            onValueChange={value => updateField(name, value)}
          />
        </div>
      );
    }
    if (type === "CheckboxGroup") {
      return renderChoiceGroup(component, true);
    }
    if (type === "ApprovalBar") {
      return (
        <div className="a2ui-approval" role="group" aria-label="批准操作">
          <span className="a2ui-approval-summary"><Shield size={15} />{String(component.summary || component.tool_name || "请确认此操作")}</span>
          <span className="a2ui-actions">
            <button type="button" className="secondary" disabled={disabled} onClick={() => submit("deny")}><X size={14} />{String(component.deny_label || "拒绝")}</button>
            <button type="button" disabled={disabled} onClick={() => submit("approve")}><Check size={14} />{String(component.approve_label || "批准")}</button>
          </span>
        </div>
      );
    }
    if (type === "Form") {
      return (
        <form className="a2ui-form" onSubmit={event => { event.preventDefault(); submit("submit"); }}>
          {Boolean(component.title) && <strong>{String(component.title)}</strong>}
          {children}
          <div className="a2ui-form-actions">
            <button type="submit" disabled={disabled}>{String(component.submit_label || "提交")}<CornerDownLeft size={14} /></button>
          </div>
        </form>
      );
    }
    if (type === "Button") {
      const action = String(component.action || component.name || component.id || "submit");
      return <button type="button" disabled={disabled} onClick={() => submit(action)}>{String(component.label || component.text || "提交")}</button>;
    }
    return <div className="a2ui-unsupported">此卡片包含暂不支持的组件：{type || "unknown"}</div>;
  };

  const content = roots.map(root => <div key={root}>{renderNode(root)}</div>);
  return (
    <div className={`a2ui-surface${pending ? " pending" : " resolved"}`} data-surface-id={surface.id}>
      {content}
      {surface.interaction && !pending && <div className="a2ui-resolved"><Check size={14} />已提交</div>}
    </div>
  );
}
