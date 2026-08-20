import type { UseFormSetError } from "react-hook-form";

type FieldErrorMap = Record<string, string>;
type ErrorDetail = { field?: string; message?: string; loc?: (string | number)[] };

function normalizeField(field: string): string {
  const leaf = field.split(".").filter(Boolean).at(-1) || field;
  return leaf.replace(/_([a-z])/g, (_match, value: string) => value.toUpperCase());
}

function readLocPath(loc: (string | number)[] | undefined): string | null {
  if (!loc?.length) return null;
  const tail = loc[loc.length - 1];
  return typeof tail === "string" ? tail : null;
}

function addField(map: FieldErrorMap, field: unknown, message: unknown): void {
  if (typeof field === "string" && field.trim() && typeof message === "string" && message) {
    map[normalizeField(field)] = message;
  }
}

function addFieldObject(map: FieldErrorMap, value: unknown): void {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  for (const [field, message] of Object.entries(value as Record<string, unknown>)) {
    if (typeof message === "string") addField(map, field, message);
    else if (Array.isArray(message) && typeof message[0] === "string") addField(map, field, message[0]);
  }
}

function collectFields(payload: unknown): FieldErrorMap | null {
  if (!payload || typeof payload !== "object") return null;
  const root = payload as Record<string, unknown>;
  const map: FieldErrorMap = {};
  addFieldObject(map, root.errors);

  const error = root.error;
  if (error && typeof error === "object" && !Array.isArray(error)) {
    const err = error as Record<string, unknown>;
    if (typeof err.field === "string" && typeof err.message === "string") {
      return { [err.field]: err.message };
    }
    const fields = err.fields;
    if (fields && typeof fields === "object" && !Array.isArray(fields)) {
      const map: FieldErrorMap = {};
      let found = false;
      for (const [field, value] of Object.entries(fields as Record<string, unknown>)) {
        if (typeof value === "string") {
          map[field] = value;
          found = true;
        }
      }
      if (found) return map;
    }
    const details = err.details;
    if (Array.isArray(details)) {
      for (const detail of details as ErrorDetail[]) {
        addField(map, detail.field || readLocPath(detail.loc), detail.message);
      }
    } else if (details && typeof details === "object") {
      addFieldObject(map, (details as Record<string, unknown>).fields);
    }
  }
  return Object.keys(map).length ? map : null;
}

export function applyApiFieldErrors(
  payload: unknown,
  setError: UseFormSetError<any>,
): boolean {
  const fields = collectFields(payload);
  if (!fields) return false;
  for (const [field, message] of Object.entries(fields)) {
    setError(field, { type: "server", message });
  }
  return true;
}
