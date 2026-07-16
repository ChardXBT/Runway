export const API_URL =
  process.env.NEXT_PUBLIC_LEEWAY_API_URL ?? "http://127.0.0.1:8000";

export async function apiGet<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
    if (!response.ok) return fallback;
    return (await response.json()) as T;
  } catch {
    return fallback;
  }
}
