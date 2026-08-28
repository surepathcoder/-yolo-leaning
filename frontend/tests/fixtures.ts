/** Minimal, valid fixtures for the API types tests build requests and responses from. */

import type { CameraSummary, CurrentUser, ModelVersion, Page, Tokens } from "@/types/api";

export function tokens(overrides: Partial<Tokens> = {}): Tokens {
  return {
    access_token: "access-token",
    refresh_token: "refresh-token",
    token_type: "bearer",
    expires_in: 1800,
    expires_at: "2030-01-01T00:00:00Z",
    ...overrides,
  };
}

export function currentUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: "user-1",
    email: "owner@example.com",
    full_name: "Jamie Rivera",
    role: "ORGANIZATION_ADMIN",
    organization_id: "org-1",
    organization_name: "Acme Retail",
    is_active: true,
    phone: null,
    timezone: null,
    locale: null,
    last_login_at: null,
    created_at: "2026-01-01T00:00:00Z",
    permissions: ["model:read", "model:activate", "camera:write"],
    accessible_store_ids: [],
    is_store_restricted: false,
    ...overrides,
  };
}

export function modelVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
  return {
    id: "model-1",
    organization_id: "org-1",
    name: "shelf-products",
    version: "v1",
    task: "detect",
    status: "VALIDATED",
    class_names: ["blue_carton", "red_carton"],
    map50: 0.62,
    map50_95: 0.5,
    trained_at: "2026-08-20T00:00:00Z",
    activated_at: null,
    created_at: "2026-08-20T00:00:00Z",
    activatable: true,
    ...overrides,
  };
}

export function cameraSummary(overrides: Partial<CameraSummary> = {}): CameraSummary {
  return {
    id: "camera-1",
    store_id: "store-1",
    name: "Aisle 3",
    location: null,
    status: "ONLINE",
    is_enabled: true,
    last_seen_at: null,
    measured_fps: 12,
    model_version_id: null,
    ...overrides,
  };
}

export function page<T>(items: T[], overrides: Partial<Page<T>> = {}): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0, ...overrides };
}
