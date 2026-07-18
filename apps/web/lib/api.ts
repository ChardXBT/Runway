export const API_URL =
  process.env.NEXT_PUBLIC_RUNWAY_API_URL ?? "http://127.0.0.1:8000";

function matchesFallbackShape(value: unknown, fallback: unknown) {
  if (Array.isArray(fallback)) return Array.isArray(value);
  if (fallback === null) return value === null || typeof value === "object";
  if (typeof fallback === "object") {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }
  return typeof value === typeof fallback;
}

export async function apiGet<T>(
  path: string,
  fallback: T,
  validate?: (value: unknown) => value is T,
): Promise<T> {
  try {
    const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
    if (!response.ok) return fallback;
    const value: unknown = await response.json();
    if (validate ? !validate(value) : !matchesFallbackShape(value, fallback)) {
      return fallback;
    }
    return value as T;
  } catch {
    return fallback;
  }
}
