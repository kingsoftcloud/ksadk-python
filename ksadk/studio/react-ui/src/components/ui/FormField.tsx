import * as Label from "@radix-ui/react-label";
import {
  cloneElement,
  isValidElement,
  useId,
  type ReactElement,
  type ReactNode,
} from "react";

export type FieldRequirement = "required" | "optional" | "generated";

const REQUIREMENT_COPY: Record<FieldRequirement, string> = {
  required: "必填",
  optional: "选填",
  generated: "自动生成",
};

export function FormLabel({
  children,
  htmlFor,
  requirement,
}: {
  children: ReactNode;
  htmlFor?: string;
  requirement?: FieldRequirement;
}) {
  return (
    <Label.Root className="studio-field-label" htmlFor={htmlFor}>
      <span>{children}</span>
      {requirement ? (
        <span className={`studio-field-requirement ${requirement}`}>
          {REQUIREMENT_COPY[requirement]}
        </span>
      ) : null}
    </Label.Root>
  );
}

export function FieldHint({ id, children }: { id?: string; children: ReactNode }) {
  return <p className="studio-field-hint" id={id}>{children}</p>;
}

export function FieldError({ id, children }: { id?: string; children: ReactNode }) {
  return <p className="studio-field-error" id={id} role="alert">{children}</p>;
}

export interface FormFieldProps {
  label: string;
  requirement?: FieldRequirement;
  hint?: string;
  error?: string;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}

export function FormField({
  label,
  requirement,
  hint,
  error,
  htmlFor,
  children,
  className,
}: FormFieldProps) {
  const generatedId = useId();
  const hintId = hint ? `${generatedId}-hint` : undefined;
  const errorId = error ? `${generatedId}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  let control = children;
  if (isValidElement(children)) {
    const child = children as ReactElement<Record<string, unknown>>;
    const currentDescription = typeof child.props["aria-describedby"] === "string"
      ? child.props["aria-describedby"]
      : undefined;
    control = cloneElement(child, {
      "aria-describedby": [currentDescription, describedBy].filter(Boolean).join(" ") || undefined,
      "aria-invalid": error ? true : child.props["aria-invalid"],
    });
  }

  return (
    <div className={`studio-form-field${error ? " has-error" : ""}${className ? ` ${className}` : ""}`}>
      <FormLabel htmlFor={htmlFor} requirement={requirement}>{label}</FormLabel>
      <div className="studio-field-control">{control}</div>
      {hint ? <FieldHint id={hintId}>{hint}</FieldHint> : null}
      {error ? <FieldError id={errorId}>{error}</FieldError> : null}
    </div>
  );
}
