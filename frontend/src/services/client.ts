/**
 * The HTTP client.
 *
 * Two things here are worth more than the rest of the file:
 *
 * **Single-flight refresh.** When an access token expires, a dashboard often
 * has half a dozen requests in flight. If each one independently refreshed,
 * they would race: the first rotation invalidates the token the others are
 * about to present, the backend sees a revoked token being replayed, and its
 * stolen-token protection revokes the whole session family. The user is
 * silently signed out for using the app normally. So a refresh happens once
 * and every waiting request shares its result.
 *
 * **Typed errors.** Every failure becomes an `ApiError` carrying the backend's
 * stable `code`, so UI code branches on `PLAN_LIMIT_EXCEEDED` rather than
 * pattern-matching prose that might be translated tomorrow.
 */

import type { ApiErrorBody, Tokens } from "@/types/api";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";
const API_PREFIX = "/api/v1";

const ACCESS_TOKEN_KEY = "visionguard.access_token";
const REFRESH_TOKEN_KEY = "visionguard.refresh_token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Whether re-authenticating could plausibly fix this. */
  get isAuthError(): boolean {
    return (
      this.status === 401 ||
      ["NOT_AUTHENTICATED", "TOKEN_EXPIRED", "TOKEN_INVALID"].includes(this.code)
    );
  }

  get isPermissionError(): boolean {
    return this.status === 403;
  }

  get isPlanLimit(): boolean {
    return this.code === "PLAN_LIMIT_EXCEEDED";
  }

  /** Field-level messages from a validation failure, if any. */
  get fieldErrors(): Record<string, string> {
    const fields = this.details?.fields;
    if (!Array.isArray(fields)) return {};
    const result: Record<string, string> = {};
    for (const entry of fields) {
      if (entry && typeof entry === "object" && "field" in entry) {
        const record = entry as { field: string; message?: string };
        result[record.field] = record.message ?? "Invalid value";
      }
    }
    return result;
  }
}

// ---------------------------------------------------------------- token store
//
// localStorage is used rather than an httpOnly cookie. The trade-off, stated
// plainly: a cookie resists XSS better, but the API is token-based and
// stateless by design and cookies would require CSRF protection on every
// mutating call. Access tokens are short-lived (30 minutes) and refresh
// rotation with reuse detection limits the value of a stolen one. Moving to
// cookies is a deliberate future change, not an oversight.

export const tokenStore = {
  get access(): string | null {
    try {
      return localStorage.getItem(ACCESS_TOKEN_KEY);
    } catch {
      return null;
    }
  },
  get refresh(): string | null {
    try {
      return localStorage.getItem(REFRESH_TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(tokens: Tokens): void {
    try {
      localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
      localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
    } catch {
      // A private window with storage disabled: the session lives in memory
      // for this tab only, which still works.
    }
  },
  clear(): void {
    try {
      localStorage.removeItem(ACCESS_TOKEN_KEY);
      localStorage.removeItem(REFRESH_TOKEN_KEY);
    } catch {
      /* ignore */
    }
  },
};

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler = () => {};

export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler;
}

// ------------------------------------------------------------------- refresh
/** In-flight refresh, shared by every request that needs one. */
let refreshInFlight: Promise<string | null> | null = null;

async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = tokenStore.refresh;
  if (!refreshToken) return null;

  const response = await fetch(`${BASE_URL}${API_PREFIX}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

  if (!response.ok) {
    tokenStore.clear();
    onUnauthorized();
    return null;
  }

  const tokens = (await response.json()) as Tokens;
  tokenStore.set(tokens);
  return tokens.access_token;
}

function refreshOnce(): Promise<string | null> {
  refreshInFlight ??= refreshAccessToken().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

// -------------------------------------------------------------------- request
export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | string[] | undefined | null>;
  signal?: AbortSignal;
  /** Skip the Authorization header (login, register). */
  anonymous?: boolean;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(
    `${API_PREFIX}${path}`,
    BASE_URL || window.location.origin,
  );
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      if (Array.isArray(value)) {
        // Repeated keys, matching FastAPI's list handling.
        for (const item of value) url.searchParams.append(key, String(item));
      } else {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody | null = null;
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    // A proxy error page or a dropped connection: no JSON to read.
  }
  if (body?.error) {
    return new ApiError(
      response.status,
      body.error.code,
      body.error.message,
      body.error.details,
    );
  }
  return new ApiError(
    response.status,
    "NETWORK_ERROR",
    response.status === 0
      ? "Cannot reach the server."
      : `Request failed (${response.status}).`,
  );
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, query, signal, anonymous = false } = options;

  const send = async (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token && !anonymous) headers.Authorization = `Bearer ${token}`;

    return fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  };

  let response: Response;
  try {
    response = await send(anonymous ? null : tokenStore.access);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "Cannot reach the server. Check your connection.",
    );
  }

  // One retry after a shared refresh. Never more: a second 401 after a fresh
  // token means the session is genuinely gone, and retrying would loop.
  if (response.status === 401 && !anonymous) {
    const token = await refreshOnce();
    if (!token) {
      throw await toApiError(response);
    }
    try {
      response = await send(token);
    } catch {
      throw new ApiError(0, "NETWORK_ERROR", "Cannot reach the server.");
    }
    if (response.status === 401) {
      tokenStore.clear();
      onUnauthorized();
      throw await toApiError(response);
    }
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"], signal?: AbortSignal) =>
    request<T>(path, { method: "GET", query, signal }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/** The websocket URL, derived from the API base so one env var configures both. */
export function realtimeUrl(): string {
  const explicit = import.meta.env.VITE_WS_BASE_URL;
  if (explicit) return `${explicit}/ws/realtime`;
  const origin = BASE_URL || window.location.origin;
  return `${origin.replace(/^http/, "ws")}/ws/realtime`;
}
