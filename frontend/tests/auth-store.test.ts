/**
 * `stores/auth.ts` drives `RequireAuth` and every permission-gated nav item,
 * so its state transitions matter more than most: a stuck `isLoading` traps
 * every user behind a spinner, and a `restore()` that doesn't clear a dead
 * token loops the app between the dashboard and the login page.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, tokenStore } from "@/services/client";

vi.mock("@/services/endpoints", () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
  },
}));

import { authApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";

import { currentUser, tokens } from "./fixtures";

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.setState({ user: null, isLoading: true, error: null });
});

describe("login", () => {
  it("stores the tokens and the user on success", async () => {
    const user = currentUser();
    vi.mocked(authApi.login).mockResolvedValue({ tokens: tokens(), user });

    await useAuth.getState().login("owner@example.com", "hunter2000", undefined);

    expect(tokenStore.access).toBe("access-token");
    expect(useAuth.getState().user).toEqual(user);
    expect(useAuth.getState().error).toBeNull();
  });

  it("surfaces the backend's message and rethrows on failure", async () => {
    vi.mocked(authApi.login).mockRejectedValue(
      new ApiError(401, "INVALID_CREDENTIALS", "Incorrect email or password."),
    );

    await expect(
      useAuth.getState().login("owner@example.com", "wrong", undefined),
    ).rejects.toBeInstanceOf(ApiError);
    expect(useAuth.getState().error).toBe("Incorrect email or password.");
    expect(useAuth.getState().user).toBeNull();
  });

  it("falls back to a generic message for a non-API failure", async () => {
    vi.mocked(authApi.login).mockRejectedValue(new Error("boom"));

    await expect(useAuth.getState().login("a", "b", undefined)).rejects.toThrow("boom");
    expect(useAuth.getState().error).toBe("Unable to sign in.");
  });
});

describe("logout", () => {
  it("clears the session locally even when the server call fails", async () => {
    tokenStore.set(tokens());
    useAuth.setState({ user: currentUser(), isLoading: false });
    vi.mocked(authApi.logout).mockRejectedValue(new Error("network down"));

    await useAuth.getState().logout();

    expect(tokenStore.access).toBeNull();
    expect(useAuth.getState().user).toBeNull();
  });
});

describe("restore", () => {
  it("skips the network call and settles immediately with no stored tokens", async () => {
    await useAuth.getState().restore();

    expect(authApi.me).not.toHaveBeenCalled();
    expect(useAuth.getState().isLoading).toBe(false);
    expect(useAuth.getState().user).toBeNull();
  });

  it("re-establishes the session from a valid stored token", async () => {
    tokenStore.set(tokens());
    const user = currentUser();
    vi.mocked(authApi.me).mockResolvedValue(user);

    await useAuth.getState().restore();

    expect(useAuth.getState().user).toEqual(user);
    expect(useAuth.getState().isLoading).toBe(false);
  });

  it("clears a dead token rather than looping", async () => {
    tokenStore.set(tokens());
    vi.mocked(authApi.me).mockRejectedValue(new ApiError(401, "TOKEN_INVALID", "bad"));

    await useAuth.getState().restore();

    expect(tokenStore.access).toBeNull();
    expect(useAuth.getState().user).toBeNull();
    expect(useAuth.getState().isLoading).toBe(false);
  });
});

describe("permissions", () => {
  it("can() and canAny() read the current user's permission list", () => {
    useAuth.setState({ user: currentUser({ permissions: ["event:read"] }) });

    expect(useAuth.getState().can("event:read")).toBe(true);
    expect(useAuth.getState().can("model:activate")).toBe(false);
    expect(useAuth.getState().canAny("model:activate", "event:read")).toBe(true);
  });

  it("has no permissions when signed out", () => {
    useAuth.setState({ user: null });

    expect(useAuth.getState().can("event:read")).toBe(false);
    expect(useAuth.getState().canAny("event:read")).toBe(false);
  });
});
