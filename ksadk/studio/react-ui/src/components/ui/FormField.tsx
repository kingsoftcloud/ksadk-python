import * as Label from "@radix-ui/react-label";
import * as Tooltip from "@radix-ui/react-tooltip";
import {
  cloneElement,
  isValidElement,
  useId,
  type ReactElement,
  type ReactNode,
} from "react";
import { CircleHelp } from "lucide-react";

export type FieldRequirement = "required" | "optional" | "generated";

const REQUIREMENT_COPY: Record<FieldRequirement, string> = {
  required: "*",
  optional: "",
  generated: "自动生成",
};

export function FormLabel({
  children,
  htmlFor,
  requirement,
  hint,
}: {
  children: ReactNode;
  htmlFor?: string;
  requirement?: FieldRequirement;
  hint?: string;
}) {
  return (
    <div className="studio-field-label-row">
      <Label.Root className="studio-field-label" htmlFor={htmlFor}>
        <span>{children}</span>
        {requirement && REQUIREMENT_COPY[requirement] ? (
          <span className={`studio-field-requirement ${requirement}`}>
            <span aria-hidden="true">{REQUIREMENT_COPY[requirement]}</span>
            {requirement === "required" && <span className="sr-only">必填</span>}
          </span>
        ) : null}
      </Label.Root>
      {hint ? (
        <Tooltip.Provider delayDuration={240}>
          <Tooltip.Root>
            <Tooltip.Trigger asChild>
              <button
                className="field-help-trigger"
                type="button"
                aria-label={`${String(children)}说明`}
                onClick={event => event.preventDefault()}
              >
                <CircleHelp size={14} />
              </button>
            </Tooltip.Trigger>
            <Tooltip.Portal>
              <Tooltip.Content className="studio-tooltip field-help-tooltip" side="top" sideOffset={7}>
                {hint}
                <Tooltip.Arrow className="studio-tooltip-arrow" />
              </Tooltip.Content>
            </Tooltip.Portal>
          </Tooltip.Root>
        </Tooltip.Provider>
      ) : null}
    </div>
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
  footer?: ReactNode;
}

export function FormField({
  label,
  requirement,
  hint,
  error,
  htmlFor,
  children,
  className,
  footer,
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
      <FormLabel htmlFor={htmlFor} requirement={requirement} hint={hint}>{label}</FormLabel>
      <div className="studio-field-control">{control}</div>
      {footer ? <div className="studio-field-footer">{footer}</div> : null}
      {hint ? <span className="sr-only" id={hintId}>{hint}</span> : null}
      {error ? <FieldError id={errorId}>{error}</FieldError> : null}
    </div>
  );
}
