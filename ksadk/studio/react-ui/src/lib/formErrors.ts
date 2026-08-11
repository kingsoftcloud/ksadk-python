type FormErrorSetter = (
  name: never,
  error: { type: string; message: string },
  options: { shouldFocus: boolean },
) => void;

function normalizeField(field: string): string {
  const leaf = field.split(".").filter(Boolean).at(-1) || field;
  return leaf.replace(/_([a-z])/g, (_match, value: string) => value.toUpperCase());
}

export function applyApiFieldErrors(payload: unknown, setError: FormErrorSetter): boolean {
  const error = payload && typeof payload === "object"
    ? (payload as { error?: { field?: unknown; message?: unknown } }).error
    : undefined;
  if (!error || typeof error.field !== "string" || !error.field.trim()) return false;
  setError(
    normalizeField(error.field) as never,
    { type: "server", message: String(error.message || "字段内容无效") },
    { shouldFocus: true },
  );
  return true;
}
