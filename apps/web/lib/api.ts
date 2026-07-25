export const API_URL =
  process.env.NEXT_PUBLIC_RUNWAY_API_URL ?? "http://127.0.0.1:8000";

type Validator<T> = (value: unknown) => value is T;

export class ApiReadError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "ApiReadError";
    this.status = status;
  }
}

function matchesFallbackShape(value: unknown, fallback: unknown) {
  if (Array.isArray(fallback)) return Array.isArray(value);
  if (fallback === null) return value === null || typeof value === "object";
  if (typeof fallback === "object") {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }
  return typeof value === typeof fallback;
}

async function readRequiredJson<T>(
  path: string,
  validate: Validator<T>,
  { allowNotFound = false }: { allowNotFound?: boolean } = {},
): Promise<T | null> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new ApiReadError(
      "Runway could not reach the local service. Start or restart the API, then reload this view.",
    );
  }

  if (allowNotFound && response.status === 404) return null;
  if (!response.ok) {
    throw new ApiReadError(
      `The local service returned ${response.status} while loading this view. Reload after checking the API.`,
      response.status,
    );
  }

  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new ApiReadError(
      "Runway received unreadable data from the local service. Reload after checking the API.",
      response.status,
    );
  }
  if (!validate(value)) {
    throw new ApiReadError(
      "Runway received an unexpected response from the local service. Reload before taking another action.",
      response.status,
    );
  }
  return value;
}

export async function apiGetRequired<T>(
  path: string,
  validate: Validator<T>,
): Promise<T> {
  return (await readRequiredJson(path, validate)) as T;
}

export async function apiGetOptional<T>(
  path: string,
  validate: Validator<T>,
): Promise<T | null> {
  return readRequiredJson(path, validate, { allowNotFound: true });
}

/**
 * Best-effort reads are reserved for non-essential chrome such as the global nav.
 * Product pages should use apiGetRequired/apiGetOptional so failures are not
 * presented as legitimate empty data.
 */
export async function apiGet<T>(
  path: string,
  fallback: T,
  validate?: Validator<T>,
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
