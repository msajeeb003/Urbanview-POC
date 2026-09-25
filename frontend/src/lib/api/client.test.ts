import { describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest, buildUrl, newRequestId, retryDelayMs, setSessionIdProvider } from "./client";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

describe("buildUrl", () => {
  it("joins base and path and drops empty query values", () => {
    expect(buildUrl("/v1/locate", { lat: 42.44, lng: 19.26, sub: null, x: undefined, y: "" }, "http://api:8000/")).toBe(
      "http://api:8000/v1/locate?lat=42.44&lng=19.26",
    );
  });

  it("keeps a path prefix of the base URL", () => {
    expect(buildUrl("/v1/municipality", undefined, "https://example.org/api")).toBe("https://example.org/api/v1/municipality");
  });
});

describe("newRequestId", () => {
  it("matches the API's safe request-id rule", () => {
    expect(newRequestId()).toMatch(/^[A-Za-z0-9._:-]{1,128}$/);
  });
});

describe("apiRequest", () => {
  it("sends a request id and returns data with the id the server used", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const sent = new Headers(init?.headers).get("X-Request-ID");
      return json(200, { ok: true }, { "X-Request-ID": sent ?? "" });
    });
    const res = await apiRequest<{ ok: boolean }>("/v1/x", { fetchImpl, baseUrl: "http://api" });
    expect(res.data).toEqual({ ok: true });
    expect(res.requestId).toMatch(/^web-[0-9a-f]{32}$/);
    const init = fetchImpl.mock.calls[0][1]!;
    expect(new Headers(init.headers).get("Accept")).toBe("application/json");
    expect(new Headers(init.headers).get("Content-Type")).toBeNull();
  });

  it("serialises a JSON body", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => json(202, { received: 1, accepted: 1, duplicates: 0 }));
    await apiRequest("/v1/events", { method: "POST", body: { events: [] }, fetchImpl, baseUrl: "http://api", keepalive: true });
    const init = fetchImpl.mock.calls[0][1]!;
    expect(init.body).toBe('{"events":[]}');
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.keepalive).toBe(true);
  });

  it("parses the error envelope", async () => {
    const fetchImpl = vi.fn(async () =>
      json(404, { error: { code: "not_found", message: "No urban panel 9", request_id: "abc", details: { type: "urban", id: 9 } } }),
    );
    const err = await apiRequest("/v1/panel", { fetchImpl, baseUrl: "http://api" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", message: "No urban panel 9", requestId: "abc" });
    expect(err.details).toEqual({ type: "urban", id: 9 });
    expect(err.isNotFound).toBe(true);
    expect(err.isTransient).toBe(false);
  });

  it("reads Retry-After on a rate limit", async () => {
    const fetchImpl = vi.fn(async () =>
      json(429, { error: { code: "rate_limited", message: "Too many requests", request_id: "r" } }, { "Retry-After": "12" }),
    );
    const err: ApiError = await apiRequest("/v1/geocode", { fetchImpl, baseUrl: "http://api" }).catch((e) => e);
    expect(err.retryAfterSeconds).toBe(12);
    expect(err.isTransient).toBe(true);
  });

  it("falls back to an http_<status> code without an envelope", async () => {
    const fetchImpl = vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    const err: ApiError = await apiRequest("/v1/x", { fetchImpl, baseUrl: "http://api" }).catch((e) => e);
    expect(err.code).toBe("http_502");
    expect(err.isTransient).toBe(true);
    expect(err.requestId).toMatch(/^web-/);
  });

  it("turns a network failure into network_error", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const err: ApiError = await apiRequest("/v1/x", { fetchImpl, baseUrl: "http://api" }).catch((e) => e);
    expect(err).toMatchObject({ status: 0, code: "network_error" });
  });

  it("times out", async () => {
    const fetchImpl = vi.fn(
      (_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const err: ApiError = await apiRequest("/v1/x", { fetchImpl, baseUrl: "http://api", timeoutMs: 10 }).catch((e) => e);
    expect(err).toMatchObject({ status: 0, code: "timeout" });
  });

  it("rethrows a cancellation by the caller untouched", async () => {
    const outer = new AbortController();
    const fetchImpl = vi.fn(
      (_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const pending = apiRequest("/v1/x", { fetchImpl, baseUrl: "http://api", signal: outer.signal });
    outer.abort();
    const err = await pending.catch((e) => e);
    expect(err).toBeInstanceOf(DOMException);
  });
});

describe("session id and rate limits", () => {
  const limited = (retryAfter?: string) =>
    json(429, { error: { code: "rate_limited", message: "Too many requests" } }, retryAfter ? { "Retry-After": retryAfter } : {});

  it("sends the anonymous session id on every call from the browser", async () => {
    vi.stubGlobal("window", {});
    setSessionIdProvider(() => "sess_0123456789abcdef");
    try {
      const fetchImpl = vi.fn(async () => json(200, {}));
      await apiRequest("/v1/zones", { fetchImpl, baseUrl: "http://api" });
      await apiRequest("/v1/orders", { method: "POST", body: {}, fetchImpl, baseUrl: "http://api" });
      for (const call of fetchImpl.mock.calls as unknown as [string, RequestInit][]) {
        expect((call[1].headers as Record<string, string>)["X-Session-ID"]).toBe("sess_0123456789abcdef");
      }
    } finally {
      setSessionIdProvider(null);
      vi.unstubAllGlobals();
    }
  });

  it("never sends one from the server", async () => {
    setSessionIdProvider(() => "sess_0123456789abcdef");
    try {
      const fetchImpl = vi.fn(async () => json(200, {}));
      await apiRequest("/v1/municipality", { fetchImpl, baseUrl: "http://api" });
      const init = (fetchImpl.mock.calls as unknown as [string, RequestInit][])[0][1];
      expect(init.headers as Record<string, string>).not.toHaveProperty("X-Session-ID");
    } finally {
      setSessionIdProvider(null);
    }
  });

  it("retries a 429 after Retry-After, then answers", async () => {
    const waits: number[] = [];
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValueOnce(limited("2")).mockResolvedValueOnce(limited()).mockResolvedValueOnce(json(200, { ok: true }));
    const res = await apiRequest<{ ok: boolean }>("/v1/x", {
      fetchImpl,
      baseUrl: "http://api",
      sleep: async (ms) => void waits.push(ms),
    });
    expect(res.data.ok).toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(waits).toEqual([2000, 1000]);
  });

  it("gives up after two retries, and at once when the server asks for a long wait", async () => {
    const sleep = async () => undefined;
    const always = vi.fn<typeof fetch>(async () => limited());
    const err: ApiError = await apiRequest("/v1/x", { fetchImpl: always, baseUrl: "http://api", sleep }).catch((e) => e);
    expect(err.status).toBe(429);
    expect(always).toHaveBeenCalledTimes(3);

    const daily = vi.fn<typeof fetch>(async () => limited("3600"));
    await apiRequest("/v1/orders", { method: "POST", body: {}, fetchImpl: daily, baseUrl: "http://api", sleep }).catch(() => null);
    expect(daily).toHaveBeenCalledTimes(1);

    const none = vi.fn<typeof fetch>(async () => limited());
    await apiRequest("/v1/events", { fetchImpl: none, baseUrl: "http://api", sleep, retry429: 0 }).catch(() => null);
    expect(none).toHaveBeenCalledTimes(1);
  });

  it("backs off 0.5 s, 1 s … without Retry-After", () => {
    expect([0, 1, 2].map((a) => retryDelayMs(a, null))).toEqual([500, 1000, 2000]);
    expect(retryDelayMs(0, 3)).toBe(3000);
  });
});
