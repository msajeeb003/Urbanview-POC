/**
 * Fetch wrapper for the UrbanView API (`/v1/...`).
 *
 * - Base URL from `NEXT_PUBLIC_API_BASE_URL` (default http://localhost:8000); on the server an
 *   optional `API_INTERNAL_BASE_URL` wins (container-to-container address).
 * - Every request carries a client-generated `X-Request-ID`; the API honours it, so a failed call
 *   can be traced in the server log. The id the server used is read back from the response.
 * - Non-2xx answers are parsed from the API's single error envelope
 *   `{"error": {"code", "message", "request_id", "details"?}}` into an `ApiError`. Network
 *   failures and timeouts become `ApiError`s too (`network_error`, `timeout`, status 0).
 * - In the browser every request also carries the anonymous analytics session id
 *   (`X-Session-ID`, the id the events carry; see `setSessionIdProvider`). No accounts.
 * - A 429 is retried here, at most twice, after the server's `Retry-After` (else 0.5 s, 1 s …)
 *   when that wait is short (≤ 10 s); a longer one (a daily cap) is the caller's answer.
 * - Uncovered locations are NOT errors: the API answers 200 with `covered: false`.
 */

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue>;

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id?: string | null;
  details?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: unknown;
  readonly retryAfterSeconds: number | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
    details?: unknown;
    retryAfterSeconds?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId ?? null;
    this.details = init.details;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }

  /** 404 on an entity looked up by id (a stale link), not an uncovered location. */
  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** Worth retrying later: network trouble, rate limit, server-side outage. */
  get isTransient(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

export interface ApiResponse<T> {
  data: T;
  status: number;
  headers: Headers;
  requestId: string | null;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Milliseconds before the request is aborted with `ApiError(code: "timeout")`. */
  timeoutMs?: number;
  /** Keep the request alive while the page unloads (analytics flush). */
  keepalive?: boolean;
  /** Override for tests. */
  fetchImpl?: typeof fetch;
  baseUrl?: string;
  /** 429 retries (default `RETRY_429_MAX`; 0 = none). */
  retry429?: number;
  /** Override for tests: how the client waits before a retry. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_TIMEOUT_MS = 15_000;
export const RETRY_429_MAX = 2;
export const RETRY_429_MAX_WAIT_MS = 10_000;
export const SESSION_HEADER = "X-Session-ID";

let sessionIdOf: (() => string | null) | null = null;

/** Where the anonymous session id comes from (the analytics tracker registers it in the browser). */
export function setSessionIdProvider(provider: (() => string | null) | null): void {
  sessionIdOf = provider;
}

/** How long to wait before retrying a 429: the server's `Retry-After`, else 0.5 s · 2^attempt. */
export function retryDelayMs(attempt: number, retryAfterSeconds: number | null): number {
  return retryAfterSeconds != null ? retryAfterSeconds * 1000 : 500 * 2 ** attempt;
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function apiBaseUrl(): string {
  const serverSide = typeof window === "undefined" ? process.env.API_INTERNAL_BASE_URL : undefined;
  const raw = serverSide || process.env.NEXT_PUBLIC_API_BASE_URL || DEFAULT_BASE_URL;
  return raw.replace(/\/+$/, "");
}

/** A random id that satisfies the API's `^[A-Za-z0-9._:-]{1,128}$` request-id rule. */
export function newRequestId(): string {
  return `web-${randomToken(16)}`;
}

/** URL-safe random token (hex) from Web Crypto; works outside secure contexts too. */
export function randomToken(bytes = 16): string {
  const buf = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function buildUrl(path: string, query?: Query, baseUrl = apiBaseUrl()): string {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, `${baseUrl}/`);
  // new URL("/v1/x", "http://host/prefix/") drops the prefix; keep an API mounted under a path.
  const prefix = new URL(`${baseUrl}/`).pathname.replace(/\/$/, "");
  if (prefix && !url.pathname.startsWith(prefix)) url.pathname = `${prefix}${url.pathname}`;
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

function isEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (!value || typeof value !== "object") return false;
  const err = (value as { error?: unknown }).error;
  return (
    !!err &&
    typeof err === "object" &&
    typeof (err as ApiErrorBody).code === "string" &&
    typeof (err as ApiErrorBody).message === "string"
  );
}

export async function parseError(response: Response, sentRequestId: string): Promise<ApiError> {
  const headerId = response.headers.get("X-Request-ID");
  const retryAfter = Number(response.headers.get("Retry-After"));
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (isEnvelope(payload)) {
    return new ApiError({
      status: response.status,
      code: payload.error.code,
      message: payload.error.message,
      requestId: payload.error.request_id ?? headerId ?? sentRequestId,
      details: payload.error.details,
      retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    });
  }
  return new ApiError({
    status: response.status,
    code: `http_${response.status}`,
    message: response.statusText || `HTTP ${response.status}`,
    requestId: headerId ?? sentRequestId,
    details: payload ?? undefined,
    retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
  });
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const { retry429 = RETRY_429_MAX, sleep = wait, signal } = options;
  for (let attempt = 0; ; attempt++) {
    try {
      return await requestOnce<T>(path, options);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 429 || attempt >= retry429) throw error;
      const delay = retryDelayMs(attempt, error.retryAfterSeconds);
      if (delay > RETRY_429_MAX_WAIT_MS) throw error;
      await sleep(delay, signal);
    }
  }
}

async function requestOnce<T>(path: string, options: RequestOptions): Promise<ApiResponse<T>> {
  const {
    method = "GET",
    query,
    body,
    signal,
    headers = {},
    timeoutMs = DEFAULT_TIMEOUT_MS,
    keepalive,
    fetchImpl = fetch,
    baseUrl,
  } = options;
  const requestId = newRequestId();
  const url = buildUrl(path, query, baseUrl);
  const sessionId = typeof window !== "undefined" ? sessionIdOf?.() : null;

  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), timeoutMs);
  const onOuterAbort = () => timeout.abort();
  signal?.addEventListener("abort", onOuterAbort, { once: true });

  let response: Response;
  try {
    response = await fetchImpl(url, {
      method,
      headers: {
        Accept: "application/json",
        "X-Request-ID": requestId,
        ...(sessionId ? { [SESSION_HEADER]: sessionId } : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: timeout.signal,
      keepalive,
    });
  } catch (cause) {
    const aborted = timeout.signal.aborted;
    if (aborted && signal?.aborted) throw cause; // the caller cancelled (React Query, unmount)
    throw new ApiError({
      status: 0,
      code: aborted ? "timeout" : "network_error",
      message: aborted ? `No answer from the API within ${timeoutMs} ms` : "The API could not be reached",
      requestId,
      details: cause instanceof Error ? cause.message : undefined,
    });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onOuterAbort);
  }

  if (!response.ok) throw await parseError(response, requestId);

  const data = (response.status === 204 ? null : await response.json()) as T;
  return {
    data,
    status: response.status,
    headers: response.headers,
    requestId: response.headers.get("X-Request-ID") ?? requestId,
  };
}

/** Convenience: the payload only. */
export async function apiGet<T>(path: string, query?: Query, init?: Omit<RequestOptions, "query" | "method">): Promise<T> {
  return (await apiRequest<T>(path, { ...init, method: "GET", query })).data;
}

export async function apiPost<T>(path: string, body: unknown, init?: Omit<RequestOptions, "body" | "method">): Promise<T> {
  return (await apiRequest<T>(path, { ...init, method: "POST", body })).data;
}
