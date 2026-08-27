/**
 * Authentication state.
 *
 * `permissions` drives what the UI offers. It is a usability affordance, never
 * a security control: hiding a button stops a user stumbling into a 403, but
 * the server checks every request regardless of what the client believes.
 */

import { create } from "zustand";

import { authApi } from "@/services/endpoints";
import { ApiError, setUnauthorizedHandler, tokenStore } from "@/services/client";
import type { CurrentUser } from "@/types/api";

interface AuthState {
  user: CurrentUser | null;
  /** True until the initial token check finishes, so we do not flash the login page. */
  isLoading: boolean;
  error: string | null;

  login: (email: string, password: string, organizationSlug?: string) => Promise<void>;
  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    organization_name: string;
  }) => Promise<void>;
  logout: (allSessions?: boolean) => Promise<void>;
  restore: () => Promise<void>;
  clearError: () => void;

  can: (permission: string) => boolean;
  canAny: (...permissions: string[]) => boolean;
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  isLoading: true,
  error: null,

  async login(email, password, organizationSlug) {
    set({ error: null });
    try {
      const response = await authApi.login(email, password, organizationSlug);
      tokenStore.set(response.tokens);
      set({ user: response.user, error: null });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : "Unable to sign in.";
      set({ error: message });
      throw error;
    }
  },

  async register(payload) {
    set({ error: null });
    try {
      const response = await authApi.register(payload);
      tokenStore.set(response.tokens);
      set({ user: response.user, error: null });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : "Unable to create the account.";
      set({ error: message });
      throw error;
    }
  },

  async logout(allSessions = false) {
    try {
      await authApi.logout(tokenStore.refresh, allSessions);
    } catch {
      // A failed logout call must still sign the user out locally: leaving
      // them apparently signed in on a shared machine is the worse outcome.
    }
    tokenStore.clear();
    set({ user: null, error: null });
  },

  /** Re-establish the session on page load from a stored token. */
  async restore() {
    if (!tokenStore.access && !tokenStore.refresh) {
      set({ isLoading: false, user: null });
      return;
    }
    try {
      const user = await authApi.me();
      set({ user, isLoading: false });
    } catch {
      tokenStore.clear();
      set({ user: null, isLoading: false });
    }
  },

  clearError() {
    set({ error: null });
  },

  can(permission) {
    return get().user?.permissions.includes(permission) ?? false;
  },

  canAny(...permissions) {
    const held = get().user?.permissions;
    if (!held) return false;
    return permissions.some((permission) => held.includes(permission));
  },
}));

// A refresh failure anywhere in the app ends the session here, so every tab
// and every open page reacts the same way.
setUnauthorizedHandler(() => {
  tokenStore.clear();
  useAuth.setState({ user: null, isLoading: false });
});
