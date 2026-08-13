import type { UseFormSetError } from "react-hook-form";

type FieldErrorMap = Record<string, string>;
type ErrorDetail = { field?: string; message?: string; loc?: (string | number)[] };

function readLocPath(loc: (string | number)[] | undefined): string | null {
  if (!loc || loc.length === 0) return null;
  const tail = loc[loc.length - 1];
  return typeof tail === "string" ? tail : null;
}

function collectFields(payload: unknown): FieldErrorMap | null {
  if (!payload || typeof payload !== "object") return null;
  const root = payload as Record<string, unknown>;
  const errors = root.errors;
  if (errors && typeof errors === "object" && !Array.isArray(errors)) {
    const map: FieldErrorMap = {};
    let found = false;
    for (const [field, value] of Object.entries(errors as Record<string, unknown>)) {
      if (typeof value === "string") {
        map[field] = value;
        found = true;
      } else if (Array.isArray(value) && value.length && typeof value[0] === "string") {
        map[field] = String(value[0]);
        found = true;
      }
    }
    return found ? map : null;
  }
  const error = root.error;
  if (error && typeof error === "object") {
    const err = error as Record<string, unknown>;
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
      const map: FieldErrorMap = {};
      let found = false;
      for (const detail of details as ErrorDetail[]) {
        const field = detail.field || readLocPath(detail.loc);
        if (field && detail.message) {
          map[field] = detail.message;
          found = true;
        }
      }
      return found ? map : null;
    }
  }
  return null;
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
