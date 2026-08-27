/**
 * Typed wrappers for every endpoint the dashboard uses.
 *
 * One place where a URL is written, so a route change is a single edit and a
 * typo is a compile error rather than a runtime 404.
 */

import { api } from "@/services/client";
import type {
  Alert,
  AlertStatus,
  AlertSummary,
  Camera,
  CameraHealth,
  CameraSummary,
  ConnectionTestResult,
  Correlation,
  CorrelationAccuracy,
  CorrelationStatus,
  CountingLine,
  CurrentUser,
  EventDetail,
  EventSummary,
  EventTypeInfo,
  FalsePositiveReport,
  HealthResponse,
  InventoryEvent,
  InventoryEventType,
  InventorySnapshot,
  InventorySummary,
  LoginResponse,
  OperatingHours,
  Page,
  PipelineStatus,
  PlanSummary,
  POSConfig,
  POSImportResult,
  POSTransaction,
  Product,
  RegisterResponse,
  ReviewOutcome,
  SessionInfo,
  Severity,
  Shelf,
  ShelfStatus,
  Store,
  StoreSummary,
  Tokens,
  UUID,
  VisionDevice,
  Zone,
} from "@/types/api";

// ---------------------------------------------------------------------- auth
export const authApi = {
  login: (email: string, password: string, organizationSlug?: string) =>
    api.post<LoginResponse>(
      "/auth/login",
      { email, password, organization_slug: organizationSlug ?? null },
      { anonymous: true },
    ),

  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    organization_name: string;
    phone?: string;
    country?: string;
    timezone?: string;
  }) => api.post<RegisterResponse>("/auth/register", payload, { anonymous: true }),

  me: () => api.get<CurrentUser>("/auth/me"),

  logout: (refreshToken: string | null, allSessions = false) =>
    api.post<{ message: string }>("/auth/logout", {
      refresh_token: refreshToken,
      all_sessions: allSessions,
    }),

  refresh: (refreshToken: string) =>
    api.post<Tokens>("/auth/refresh", { refresh_token: refreshToken }, { anonymous: true }),

  changePassword: (currentPassword: string, newPassword: string) =>
    api.post<{ message: string }>("/auth/change-password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),

  sessions: () => api.get<SessionInfo[]>("/auth/sessions"),
};

// -------------------------------------------------------------------- stores
export const storeApi = {
  list: (params?: { limit?: number; offset?: number; include_inactive?: boolean }) =>
    api.get<Page<StoreSummary>>("/stores", params),

  get: (id: UUID) => api.get<Store>(`/stores/${id}`),

  create: (payload: Record<string, unknown>) => api.post<Store>("/stores", payload),

  update: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<Store>(`/stores/${id}`, payload),

  remove: (id: UUID) => api.delete<{ message: string }>(`/stores/${id}`),

  operatingHours: (id: UUID) => api.get<OperatingHours[]>(`/stores/${id}/operating-hours`),

  setOperatingHours: (id: UUID, days: OperatingHours[]) =>
    api.put<OperatingHours[]>(`/stores/${id}/operating-hours`, { days }),
};

// ------------------------------------------------------------------- cameras
export const cameraApi = {
  list: (params?: {
    store_id?: UUID;
    status?: string;
    enabled_only?: boolean;
    limit?: number;
    offset?: number;
  }) => api.get<Page<CameraSummary>>("/cameras", params),

  get: (id: UUID) => api.get<Camera>(`/cameras/${id}`),

  create: (payload: Record<string, unknown>) => api.post<Camera>("/cameras", payload),

  update: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<Camera>(`/cameras/${id}`, payload),

  remove: (id: UUID) => api.delete<{ message: string }>(`/cameras/${id}`),

  test: (id: UUID) => api.post<ConnectionTestResult>(`/cameras/${id}/test`),

  enable: (id: UUID) => api.post<Camera>(`/cameras/${id}/enable`),

  disable: (id: UUID) => api.post<Camera>(`/cameras/${id}/disable`),

  health: () => api.get<CameraHealth>("/cameras/health"),

  startPipeline: (id: UUID) => api.post<PipelineStatus>(`/cameras/${id}/pipeline/start`),

  stopPipeline: (id: UUID) => api.post<PipelineStatus>(`/cameras/${id}/pipeline/stop`),

  pipelineStatus: (id: UUID) => api.get<PipelineStatus>(`/cameras/${id}/pipeline`),
};

// --------------------------------------------------------------------- zones
export const zoneApi = {
  list: (params?: { camera_id?: UUID; store_id?: UUID }) =>
    api.get<Zone[]>("/zones", params),

  forCamera: (cameraId: UUID) => api.get<Zone[]>(`/cameras/${cameraId}/zones`),

  create: (cameraId: UUID, payload: Record<string, unknown>) =>
    api.post<Zone>(`/cameras/${cameraId}/zones`, payload),

  update: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<Zone>(`/zones/${id}`, payload),

  remove: (id: UUID) => api.delete<{ message: string }>(`/zones/${id}`),
};

export const lineApi = {
  list: (params?: { camera_id?: UUID; store_id?: UUID }) =>
    api.get<CountingLine[]>("/lines", params),

  forCamera: (cameraId: UUID) => api.get<CountingLine[]>(`/cameras/${cameraId}/lines`),

  create: (cameraId: UUID, payload: Record<string, unknown>) =>
    api.post<CountingLine>(`/cameras/${cameraId}/lines`, payload),

  update: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<CountingLine>(`/lines/${id}`, payload),

  remove: (id: UUID) => api.delete<{ message: string }>(`/lines/${id}`),
};

// -------------------------------------------------------------------- events
export interface EventQuery extends Record<string, unknown> {
  store_id?: UUID;
  camera_id?: UUID;
  zone_id?: UUID;
  event_type?: string[];
  severity?: Severity[];
  track_key?: string;
  is_inference?: boolean;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export const eventApi = {
  list: (query: EventQuery = {}) =>
    api.get<Page<EventSummary>>("/events", query as Record<string, never>),

  get: (id: UUID) => api.get<EventDetail>(`/events/${id}`),

  /** An evidence artefact's bytes for inline view. Authorised, audited. */
  evidenceBlob: (evidenceId: UUID) => api.getBlob(`/evidence/${evidenceId}`),

  /** The same bytes, but the request audits as an export. Needs evidence:export. */
  evidenceDownloadBlob: (evidenceId: UUID) =>
    api.getBlob(`/evidence/${evidenceId}/download`),

  types: () => api.get<EventTypeInfo[]>("/events/types"),

  summary: () =>
    api.get<{
      window_hours: number;
      events: number;
      inferences: number;
      high_severity: number;
      line_crossings: number;
    }>("/events/summary"),

  countsByType: (params?: { since?: string; store_id?: UUID }) =>
    api.get<Record<string, number>>("/events/counts", params),
};

// -------------------------------------------------------------------- alerts
export const alertApi = {
  list: (params?: {
    status?: AlertStatus;
    severity?: Severity;
    store_id?: UUID;
    since?: string;
    limit?: number;
    offset?: number;
  }) => api.get<Page<Alert>>("/alerts", params),

  get: (id: UUID) => api.get<Alert>(`/alerts/${id}`),

  summary: () => api.get<AlertSummary>("/alerts/summary"),

  acknowledge: (id: UUID) => api.post<Alert>(`/alerts/${id}/acknowledge`),

  resolve: (
    id: UUID,
    payload: { notes?: string; was_actionable?: boolean; dismiss?: boolean },
  ) => api.post<Alert>(`/alerts/${id}/resolve`, payload),

  falsePositiveRate: (since?: string) =>
    api.get<FalsePositiveReport>("/alerts/false-positive-rate", { since }),
};

// -------------------------------------------------------------------- system
export const systemApi = {
  visionMetrics: () => api.get<Record<string, unknown>>("/system/vision/metrics"),
  visionDevice: () => api.get<VisionDevice>("/system/vision/device"),
  plan: () => api.get<PlanSummary>("/system/plan"),
  realtime: () => api.get<Record<string, unknown>>("/system/realtime"),
};

/** Health lives outside the versioned API so probes never depend on a version. */
export async function fetchHealth(): Promise<HealthResponse> {
  const response = await fetch("/health");
  if (!response.ok) throw new Error(`health check failed: ${response.status}`);
  return (await response.json()) as HealthResponse;
}

// ----------------------------------------------------------------- inventory
export const inventoryApi = {
  products: (params?: { category?: string; search?: string; include_inactive?: boolean }) =>
    api.get<Product[]>("/products", params),

  createProduct: (payload: Record<string, unknown>) => api.post<Product>("/products", payload),

  updateProduct: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<Product>(`/products/${id}`, payload),

  removeProduct: (id: UUID) => api.delete<{ message: string }>(`/products/${id}`),

  shelves: (params?: { store_id?: UUID; camera_id?: UUID }) =>
    api.get<Shelf[]>("/shelves", params),

  createShelf: (payload: Record<string, unknown>) => api.post<Shelf>("/shelves", payload),

  updateShelf: (id: UUID, payload: Record<string, unknown>) =>
    api.patch<Shelf>(`/shelves/${id}`, payload),

  removeShelf: (id: UUID) => api.delete<{ message: string }>(`/shelves/${id}`),

  status: (params?: { store_id?: UUID }) => api.get<ShelfStatus[]>("/inventory/status", params),

  summary: () => api.get<InventorySummary>("/inventory/summary"),

  snapshots: (params?: { shelf_id?: UUID; since?: string; limit?: number; offset?: number }) =>
    api.get<Page<InventorySnapshot>>("/inventory/snapshots", params),

  events: (params?: {
    shelf_id?: UUID;
    event_type?: InventoryEventType;
    since?: string;
    limit?: number;
    offset?: number;
  }) => api.get<Page<InventoryEvent>>("/inventory/events", params),
};

// ----------------------------------------------------------------------- pos
export const posApi = {
  transactions: (params?: {
    store_id?: UUID;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
  }) => api.get<Page<POSTransaction>>("/pos/transactions", params),

  importTransactions: (payload: Record<string, unknown>) =>
    api.post<POSImportResult>("/pos/transactions", payload),

  correlate: (payload: { store_id?: UUID; since?: string; limit?: number }) =>
    api.post<Correlation[]>("/pos/correlate", payload),

  correlations: (params?: {
    status?: CorrelationStatus;
    store_id?: UUID;
    reviewed?: boolean;
    limit?: number;
    offset?: number;
  }) => api.get<Page<Correlation>>("/pos/correlations", params),

  correlation: (id: UUID) => api.get<Correlation>(`/pos/correlations/${id}`),

  review: (id: UUID, payload: { outcome: ReviewOutcome; notes?: string }) =>
    api.post<Correlation>(`/pos/correlations/${id}/review`, payload),

  accuracy: () => api.get<CorrelationAccuracy>("/pos/accuracy"),

  config: (storeId: UUID) => api.get<POSConfig>(`/pos/config/${storeId}`),

  setConfig: (storeId: UUID, payload: { provider?: string | null; options?: object }) =>
    api.put<POSConfig>(`/pos/config/${storeId}`, payload),
};
