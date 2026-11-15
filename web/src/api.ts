// The one client for the API. Paths are given without the `/api` prefix. Every error the API
// raises itself has the shape {"detail": {"code", "message", ...}}; it becomes an ApiError whose
// message can be shown as it is.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: Record<string, unknown>;

  constructor(status: number, code: string, message: string, detail: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

const FALLBACK: Record<number, string> = {
  401: "Open your invitation link to sign in.",
  403: "Your role cannot do this.",
  404: "That was not found.",
  413: "That is too large to send.",
  429: "Too many requests. Wait a moment and try again.",
  503: "HackProof is busy or a service it needs is not answering. Try again shortly.",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function failure(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const detail = isRecord(body) ? body.detail : undefined;
  if (isRecord(detail) && typeof detail.code === "string") {
    const message = typeof detail.message === "string" ? detail.message : response.statusText;
    return new ApiError(response.status, detail.code, message, detail);
  }
  if (Array.isArray(detail)) {
    // FastAPI's own validation errors: a list of {loc, msg}.
    const first: unknown = detail[0];
    let message = "Some fields are not valid.";
    if (isRecord(first) && typeof first.msg === "string") {
      const loc = Array.isArray(first.loc) ? first.loc.filter((part) => part !== "body") : [];
      message = loc.length ? `${loc.join(" › ")}: ${first.msg}` : first.msg;
    }
    return new ApiError(response.status, "invalid", message, { errors: detail });
  }
  const message = FALLBACK[response.status] ?? `HackProof answered ${response.status}.`;
  return new ApiError(response.status, `http_${response.status}`, message);
}

async function request(method: string, path: string, body?: unknown, accept = "application/json") {
  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers: { Accept: accept, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError(0, "network", "HackProof could not be reached. Check your connection.");
  }
  if (!response.ok) throw await failure(response);
  return response;
}

export async function get<T>(path: string): Promise<T> {
  const response = await request("GET", path);
  return (await response.json()) as T;
}

export async function send<T>(method: "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  const response = await request(method, path, body);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function getText(path: string): Promise<string> {
  const response = await request("GET", path, undefined, "text/plain");
  return response.text();
}

export function isApiError(error: unknown, code?: string): error is ApiError {
  return error instanceof ApiError && (code === undefined || error.code === code);
}

/** A fresh key for one action (an idempotency key or a request ID): 8 to 100 of [A-Za-z0-9_-]. */
export function newKey(): string {
  // randomUUID exists only in secure contexts (https or localhost); a phone on the LAN over
  // plain http has getRandomValues alone.
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function remove(path: string): Promise<void> {
  await request("DELETE", path);
}

/** A multipart form (a file upload). The browser sets the boundary itself. */
export async function upload<T>(path: string, form: FormData): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { method: "POST", credentials: "same-origin", body: form });
  } catch {
    throw new ApiError(0, "network", "HackProof could not be reached. Check your connection.");
  }
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}
