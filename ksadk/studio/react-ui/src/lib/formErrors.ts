type FormErrorSetter = (
  field: any,
  error: { type: "server"; message: string },
) => void;

export function applyApiFieldErrors(payload: unknown, setError: FormErrorSetter): boolean {
  if (!payload || typeof payload !== "object") return false;
  const error = (payload as { error?: unknown }).error;
  if (!error || typeof error !== "object") return false;
  const { field, message } = error as { field?: unknown; message?: unknown };
  if (typeof field !== "string" || !field || typeof message !== "string" || !message) {
    return false;
  }
  setError(field, { type: "server", message });
  return true;
}
