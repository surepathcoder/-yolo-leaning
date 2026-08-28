/**
 * `services/client.ts` is where the two things called out in its own header
 * comment live: single-flight token refresh, and typed errors. Both are
 * exercised directly here rather than through a page, so a regression shows
 * up as one failing assertion instead of a confusing page-level flake.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, setUnauthorizedHandler, tokenStore } from "@/services/client";

import { tokens } from "./fixtures";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function headerOf(init: RequestInit | undefined, name: string): string | undefined {
  const headers = init?.headers as Record<string, string> | undefined;
  return headers?.[name];
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(() => {});
});

describe("tokenStore", () => {
  it("round-trips and clears tokens", () => {
    tokenStore.set(tokens({ access_token: "a", refresh_token: "r" }));
    expect(tokenStore.access).toBe("a");
    expect(tokenStore.refresh).toBe("r");

    tokenStore.clear();
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.refresh).toBeNull();
  });
});

describe("request()", () => {
  it("attaches the bearer token and builds the query string", async () => {
    tokenStore.set(tokens({ access_token: "a" }));
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/cameras", { limit: 5, status: undefined, tags: ["x", "y"] });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/v1/cameras");
    expect(url).toContain("limit=5");
    expect(url).not.toContain("status");
    expect(url).toContain("tags=x");
    expect(url).toContain("tags=y");
    expect(headerOf(init, "Authorization")).toBe("Bearer a");
  });

  it("omits the Authorization header for anonymous calls even with a token stored", async () => {
    tokenStore.set(tokens({ access_token: "a" }));
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await api.post("/auth/login", { email: "a" }, { anonymous: true });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(headerOf(init, "Authorization")).toBeUndefined();
  });

  it("throws a typed ApiError carrying the backend's code and field errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(422, {
        error: {
          code: "VALIDATION_ERROR",
          message: "Check the highlighted fields.",
          details: { fields: [{ field: "email", message: "Required" }] },
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    let caught: ApiError | undefined;
    try {
      await api.get("/whatever");
    } catch (error) {
      caught = error as ApiError;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught?.status).toBe(422);
    expect(caught?.code).toBe("VALIDATION_ERROR");
    expect(caught?.fieldErrors).toEqual({ email: "Required" });
  });

  it("returns undefined for a 204 response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api.delete("/things/1")).resolves.toBeUndefined();
  });

  it("wraps a network failure as a NETWORK_ERROR ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api.get("/x")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  });

  it("shares a single in-flight refresh across concurrent 401s and retries both", async () => {
    tokenStore.set(tokens({ access_token: "expired", refresh_token: "refresh-token" }));
    let refreshCalls = 0;

    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return Promise.resolve(
          jsonResponse(200, tokens({ access_token: "new-access", refresh_token: "new-refresh" })),
        );
      }
      const auth = headerOf(init, "Authorization");
      if (auth === "Bearer expired") {
        return Promise.resolve(
          jsonResponse(401, { error: { code: "TOKEN_EXPIRED", message: "expired" } }),
        );
      }
      if (auth === "Bearer new-access") {
        return Promise.resolve(jsonResponse(200, { ok: true }));
      }
      return Promise.resolve(jsonResponse(500, {}));
    });
    vi.stubGlobal("fetch", fetchMock);

    const [a, b] = await Promise.all([api.get("/one"), api.get("/two")]);

    expect(a).toEqual({ ok: true });
    expect(b).toEqual({ ok: true });
    expect(refreshCalls).toBe(1);
    expect(tokenStore.access).toBe("new-access");
  });

  it("clears the session and notifies the app when refresh itself fails", async () => {
    tokenStore.set(tokens({ access_token: "expired", refresh_token: "bad-refresh" }));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/auth/refresh")) {
        return Promise.resolve(
          jsonResponse(401, { error: { code: "TOKEN_INVALID", message: "bad token" } }),
        );
      }
      return Promise.resolve(
        jsonResponse(401, { error: { code: "TOKEN_EXPIRED", message: "expired" } }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.get("/one")).rejects.toBeInstanceOf(ApiError);
    expect(tokenStore.access).toBeNull();
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("does not loop when the retried request is also a 401", async () => {
    tokenStore.set(tokens({ access_token: "expired", refresh_token: "refresh-token" }));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/auth/refresh")) {
        return Promise.resolve(
          jsonResponse(200, tokens({ access_token: "still-bad", refresh_token: "r2" })),
        );
      }
      return Promise.resolve(
        jsonResponse(401, { error: { code: "TOKEN_EXPIRED", message: "expired" } }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.get("/one")).rejects.toBeInstanceOf(ApiError);
    // One request, one refresh, one retry -- never a second refresh attempt.
    const refreshAttempts = fetchMock.mock.calls.filter(
      (call: unknown[]) => typeof call[0] === "string" && call[0].includes("/auth/refresh"),
    ).length;
    expect(refreshAttempts).toBe(1);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});

describe("api.getBlob", () => {
  it("authorises the request and resolves the body as a blob", async () => {
    tokenStore.set(tokens({ access_token: "a" }));
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("binary-evidence-bytes", {
        status: 200,
        headers: { "Content-Type": "image/jpeg" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getBlob("/evidence/1");

    expect(result.type).toBe("image/jpeg");
    expect(await result.text()).toBe("binary-evidence-bytes");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(headerOf(init, "Authorization")).toBe("Bearer a");
  });
});

describe("ApiError", () => {
  it("classifies auth, permission and plan-limit failures", () => {
    expect(new ApiError(401, "TOKEN_EXPIRED", "x").isAuthError).toBe(true);
    expect(new ApiError(403, "PERMISSION_DENIED", "x").isPermissionError).toBe(true);
    expect(new ApiError(402, "PLAN_LIMIT_EXCEEDED", "x").isPlanLimit).toBe(true);
    expect(new ApiError(500, "INTERNAL", "x").isAuthError).toBe(false);
  });

  it("returns an empty field-error map when details carry no field list", () => {
    expect(new ApiError(400, "BAD_REQUEST", "x").fieldErrors).toEqual({});
    expect(new ApiError(400, "BAD_REQUEST", "x", { fields: "not-an-array" }).fieldErrors).toEqual(
      {},
    );
  });
});
