type Validator<T> = (value: unknown) => value is T;

type ApiErrorOptions = {
  uncertainOutcome?: boolean;
};

export class ApiError extends Error {
  readonly uncertainOutcome: boolean;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = "ApiError";
    this.uncertainOutcome = options.uncertainOutcome ?? false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function responseDetail(payload: unknown, fallback: string) {
  if (!isRecord(payload) || typeof payload.detail !== "string") return fallback;
  const detail = payload.detail.trim();
  return detail ? detail.slice(0, 500) : fallback;
}

export async function readApiJson<T>(
  response: Response,
  {
    validate,
    failureMessage,
    malformedMessage = "Runway received an unexpected response. Nothing has been confirmed; refresh before retrying.",
  }: {
    validate: Validator<T>;
    failureMessage: string;
    malformedMessage?: string;
  },
): Promise<T> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) throw new ApiError(failureMessage);
    throw new ApiError(malformedMessage, { uncertainOutcome: true });
  }

  if (!response.ok) {
    throw new ApiError(responseDetail(payload, failureMessage));
  }
  if (!validate(payload)) {
    throw new ApiError(malformedMessage, { uncertainOutcome: true });
  }
  return payload;
}

export function actionError(
  error: unknown,
  fallback: string,
  uncertainMessage?: string,
) {
  if (error instanceof ApiError) {
    if (error.uncertainOutcome && uncertainMessage) return uncertainMessage;
    return error.message;
  }
  if (uncertainMessage) return uncertainMessage;
  if (error instanceof Error && error.message.trim()) {
    return error.message.slice(0, 500);
  }
  return fallback;
}

export function hasUncertainOutcome(error: unknown) {
  return !(error instanceof ApiError) || error.uncertainOutcome;
}
