/**
 * The API contract, as TypeScript.
 *
 * These mirror the backend's Pydantic schemas. They are written by hand rather
 * than generated so that the *shape the UI depends on* is explicit and small:
 * a generated client would import every field of every model and make the
 * coupling invisible.
 *
 * The most important type here is `VisionEvent.is_inference`. The UI must
 * render a conclusion differently from a measurement, and `kind` is the
 * discriminator the backend provides so a component cannot forget to check.
 */

// ---------------------------------------------------------------- primitives
export type UUID = string;
export type ISODateTime = string;

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

// --------------------------------------------------------------------- enums
export const USER_ROLES = [
  "SUPER_ADMIN",
  "ORGANIZATION_ADMIN",
  "STORE_MANAGER",
  "SECURITY_OPERATOR",
  "ANALYST",
  "VIEWER",
] as const;
export type UserRole = (typeof USER_ROLES)[number];

export const SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;
export type Severity = (typeof SEVERITIES)[number];

/** Rank for sorting and threshold comparisons. Mirrors the backend's order. */
export const SEVERITY_RANK: Record<Severity, number> = {
  INFO: 0,
  LOW: 1,
  MEDIUM: 2,
  HIGH: 3,
  CRITICAL: 4,
};

export const CAMERA_STATUSES = [
  "ONLINE",
  "OFFLINE",
  "CONNECTING",
  "ERROR",
  "DISABLED",
] as const;
export type CameraStatus = (typeof CAMERA_STATUSES)[number];

export const ALERT_STATUSES = [
  "NEW",
  "ACKNOWLEDGED",
  "RESOLVED",
  "DISMISSED",
] as const;
export type AlertStatus = (typeof ALERT_STATUSES)[number];

export const ZONE_TYPES = [
  "RESTRICTED",
  "ENTRANCE",
  "EXIT",
  "SHELF",
  "CHECKOUT",
  "WAREHOUSE",
  "CUSTOM",
] as const;
export type ZoneType = (typeof ZONE_TYPES)[number];

export type SourceKind = "RTSP" | "FILE" | "WEBCAM" | "HTTP" | "SYNTHETIC";
export type ConfidenceLevel = "LOW" | "MEDIUM" | "HIGH";
export type PlanTier = "TRIAL" | "STARTER" | "BUSINESS" | "ENTERPRISE";

// ---------------------------------------------------------------------- auth
export interface Tokens {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  expires_at: ISODateTime;
}

export interface CurrentUser {
  id: UUID;
  email: string;
  full_name: string;
  role: UserRole;
  organization_id: UUID | null;
  organization_name: string | null;
  is_active: boolean;
  phone: string | null;
  timezone: string | null;
  locale: string | null;
  last_login_at: ISODateTime | null;
  created_at: ISODateTime;
  /**
   * The caller's effective permissions. Used to hide actions they cannot take.
   * This is a usability affordance only -- every endpoint enforces server side
   * regardless of what the client believes.
   */
  permissions: string[];
  accessible_store_ids: UUID[];
  is_store_restricted: boolean;
}

export interface LoginResponse {
  tokens: Tokens;
  user: CurrentUser;
}

export interface RegisterResponse extends LoginResponse {
  organization_id: UUID;
  organization_slug: string;
}

export interface SessionInfo {
  id: UUID;
  created_at: ISODateTime;
  expires_at: ISODateTime;
  ip_address: string | null;
  user_agent: string | null;
  is_current: boolean;
}

// -------------------------------------------------------------------- stores
export interface OperatingHours {
  day_of_week: number;
  opens_at: string | null;
  closes_at: string | null;
  is_closed: boolean;
}

export interface StoreSummary {
  id: UUID;
  name: string;
  code: string;
  city: string | null;
  is_active: boolean;
  timezone: string | null;
}

export interface Store extends StoreSummary {
  organization_id: UUID;
  description: string | null;
  address: string | null;
  region: string | null;
  country: string | null;
  latitude: number | null;
  longitude: number | null;
  settings: Record<string, unknown>;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  operating_hours: OperatingHours[];
}

// ------------------------------------------------------------------- cameras
export interface CameraSummary {
  id: UUID;
  store_id: UUID;
  name: string;
  location: string | null;
  status: CameraStatus;
  is_enabled: boolean;
  last_seen_at: ISODateTime | null;
  measured_fps: number | null;
}

export interface CameraStream {
  id: UUID;
  profile: "MAIN" | "SUB" | "MOBILE";
  stream_path: string;
  width: number | null;
  height: number | null;
  fps: number | null;
  codec: string | null;
  is_inference_source: boolean;
}

/**
 * Note what is absent: there is no `password` field, at any nesting level.
 * `has_credentials` says whether one is stored; nothing reveals it.
 */
export interface Camera extends CameraSummary {
  organization_id: UUID;
  description: string | null;
  source_kind: SourceKind;
  host: string | null;
  port: number | null;
  stream_path: string | null;
  source_locator: string | null;
  username: string | null;
  has_credentials: boolean;
  connection_hint: string | null;
  width: number | null;
  height: number | null;
  target_fps: number;
  transport: string;
  detection_enabled: boolean;
  confidence_threshold: number | null;
  detect_classes: string[];
  settings: Record<string, unknown>;
  last_connected_at: ISODateTime | null;
  last_error: string | null;
  consecutive_failures: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  streams: CameraStream[];
}

export interface ConnectionTestResult {
  camera_id: UUID;
  success: boolean;
  message: string;
  resolution: [number, number] | null;
  source_fps: number | null;
  latency_ms: number | null;
}

export interface CameraHealth {
  ONLINE: number;
  OFFLINE: number;
  CONNECTING: number;
  ERROR: number;
  DISABLED: number;
  total: number;
}

export interface PipelineStatus {
  camera_id: UUID;
  running: boolean;
  events_ingested?: number;
  events_deduplicated?: number;
  ingest_failures?: number;
  started_at?: ISODateTime;
  state?: string;
  pipeline?: {
    frames_processed: number;
    detections: number;
    tracks_seen: number;
    events_emitted: number;
    errors: number;
    processed_fps: number;
    mean_frame_ms: number;
  };
  detector?: {
    mean_inference_ms: number;
    p95_inference_ms: number;
    theoretical_max_fps: number;
  };
  line_counts?: Record<string, { entered: number; exited: number; net: number }>;
  zone_occupancy?: Record<string, number>;
}

// --------------------------------------------------------------------- zones
export type NormalisedPoint = [number, number];

export interface Zone {
  id: UUID;
  organization_id: UUID;
  store_id: UUID;
  camera_id: UUID;
  name: string;
  description: string | null;
  zone_type: ZoneType;
  /** Normalised 0..1, so it survives a resolution change. */
  polygon: NormalisedPoint[];
  reference_point: string;
  trigger_classes: string[];
  loitering_threshold_seconds: number | null;
  dwell_threshold_seconds: number | null;
  after_hours_monitoring: boolean;
  max_occupancy: number | null;
  is_active: boolean;
  color: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface CountingLine {
  id: UUID;
  organization_id: UUID;
  store_id: UUID;
  camera_id: UUID;
  name: string;
  point_a: NormalisedPoint;
  point_b: NormalisedPoint;
  invert_direction: boolean;
  reference_point: string;
  trigger_classes: string[];
  cooldown_seconds: number;
  is_active: boolean;
  color: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// -------------------------------------------------------------------- events
export interface Evidence {
  id: UUID;
  kind: "SNAPSHOT" | "CLIP" | "THUMBNAIL" | "METADATA";
  mime_type: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  bbox: number[] | null;
  captured_at: ISODateTime;
  expires_at: ISODateTime | null;
  url: string | null;
}

export interface EventSummary {
  id: UUID;
  event_type: string;
  severity: Severity;
  /** True when this is a conclusion drawn from other events. */
  is_inference: boolean;
  confidence_level: ConfidenceLevel | null;
  camera_id: UUID | null;
  store_id: UUID | null;
  zone_id: UUID | null;
  track_id: number | null;
  occurred_at: ISODateTime;
  duration_seconds: number | null;
  summary: string;
  has_evidence: boolean;
}

export interface VisionEvent extends EventSummary {
  organization_id: UUID;
  shelf_id: UUID | null;
  product_id: UUID | null;
  confidence: number | null;
  rule_name: string | null;
  source_event_ids: string[];
  track_key: string | null;
  object_class: string | null;
  ended_at: ISODateTime | null;
  event_metadata: Record<string, unknown>;
  ai_explanation: string | null;
  edge_device_id: string | null;
  created_at: ISODateTime;
  evidence: Evidence[];
  /** Explicit discriminator so a component cannot forget to check. */
  kind: "observation" | "inference";
}

export interface EventDetail extends VisionEvent {
  /** The observations an inference rests on. Empty for a measurement. */
  source_events: EventSummary[];
  alert_id: UUID | null;
}

export interface EventTypeInfo {
  value: string;
  label: string;
  is_inference: boolean;
}

export interface TodaySummary {
  window_hours: number;
  events: number;
  inferences: number;
  high_severity: number;
  line_crossings: number;
}

// -------------------------------------------------------------------- alerts
export interface Alert {
  id: UUID;
  organization_id: UUID;
  store_id: UUID | null;
  event_id: UUID;
  title: string;
  message: string;
  severity: Severity;
  status: AlertStatus;
  acknowledged_by_id: UUID | null;
  acknowledged_at: ISODateTime | null;
  resolved_by_id: UUID | null;
  resolved_at: ISODateTime | null;
  resolution_notes: string | null;
  was_actionable: boolean | null;
  delivery_status: Record<string, unknown>;
  created_at: ISODateTime;
  event: EventSummary | null;
  /** How many times this incident recurred while the alert stayed open. */
  occurrences: number;
}

export interface AlertSummary {
  by_status: Record<string, number>;
  open_by_severity: Record<string, number>;
  open_total: number;
  high_priority_open: number;
}

export interface FalsePositiveReport {
  judged: number;
  actionable: number;
  not_actionable: number;
  false_positive_rate: number | null;
}

// -------------------------------------------------------------------- system
export interface PlanSummary {
  tier: PlanTier | null;
  status: string;
  trial_ends_on: string | null;
  expires_on: string | null;
  limits: Record<string, { limit: number | boolean | null; current?: number }>;
}

export interface VisionDevice {
  configured_device: string;
  resolved_device: string;
  model_path: string;
  model_present: boolean;
  image_size: number;
  tracker: string;
  loaded: boolean;
  classes?: number;
  inference?: {
    frames_processed: number;
    mean_inference_ms: number;
    p95_inference_ms: number;
    theoretical_max_fps: number;
  };
}

export interface HealthResponse {
  status: "ok" | "degraded";
  database: "connected" | "unavailable" | "disabled";
  redis: "connected" | "unavailable" | "disabled";
  version: string;
  environment: string;
  uptime_seconds: number;
  timestamp: ISODateTime;
}

// ------------------------------------------------------------------ realtime
export type RealtimeMessage =
  | { type: "connected"; connection_id: string; server_time: string }
  | { type: "ping" }
  | { type: "pong" }
  | {
      type: "event";
      organization_id: UUID;
      store_id: UUID | null;
      camera_id: UUID | null;
      data: {
        id: UUID;
        event_type: string;
        severity: Severity;
        is_inference: boolean;
        confidence_level: ConfidenceLevel | null;
        zone_id: UUID | null;
        track_id: number | null;
        occurred_at: ISODateTime;
        summary: string;
      };
    }
  | {
      type: "alert";
      organization_id: UUID;
      store_id: UUID | null;
      camera_id: UUID | null;
      data: {
        id: UUID;
        event_id: UUID;
        title: string;
        message: string;
        severity: Severity;
        status: AlertStatus;
        is_inference: boolean;
        created_at: ISODateTime;
      };
    }
  | {
      type: "camera_status";
      organization_id: UUID;
      store_id: UUID | null;
      camera_id: UUID;
      data: {
        camera_id: UUID;
        status: CameraStatus;
        error: string | null;
        measured_fps: number | null;
        at: ISODateTime;
      };
    };

// ----------------------------------------------------------------- inventory
export type OccupancyLevel = "FULL" | "NORMAL" | "LOW" | "EMPTY" | "UNKNOWN";

export type InventoryEventType =
  | "PRODUCT_REMOVED"
  | "PRODUCT_RETURNED"
  | "LOW_STOCK"
  | "SHELF_EMPTY"
  | "SHELF_CHANGED"
  | "SHELF_RESTOCKED";

export interface Product {
  id: UUID;
  organization_id: UUID;
  sku: string;
  name: string;
  brand: string | null;
  category: string | null;
  barcode: string | null;
  unit: string;
  description: string | null;
  unit_price: string | null;
  currency: string;
  detection_class_name: string | null;
  image_path: string | null;
  low_stock_threshold: number | null;
  is_active: boolean;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface Shelf {
  id: UUID;
  organization_id: UUID;
  store_id: UUID;
  camera_id: UUID | null;
  name: string;
  code: string;
  location: string | null;
  polygon: number[][] | null;
  baseline_occupancy: number | null;
  low_threshold: number;
  empty_threshold: number;
  sample_interval_seconds: number;
  monitoring_enabled: boolean;
  is_active: boolean;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  products: Product[];
}

/**
 * A shelf's latest known state.
 *
 * `occupancy_ratio` is null when the shelf has never been read in a usable
 * frame. That is deliberately distinct from "read as empty", and the UI must
 * not collapse the two -- an unmonitored shelf shown as 0% is a lie.
 */
export interface ShelfStatus {
  shelf_id: UUID;
  name: string;
  code: string;
  camera_id: UUID | null;
  monitoring_enabled: boolean;
  occupancy_ratio: number | null;
  occupancy_level: OccupancyLevel;
  confidence: number | null;
  is_occluded: boolean;
  last_reading_at: ISODateTime | null;
}

export interface InventorySummary {
  shelves_total: number;
  shelves_monitored: number;
  shelves_low: number;
  shelves_empty: number;
  shelves_unknown: number;
  products_total: number;
  events_24h: number;
}

export interface InventorySnapshot {
  id: UUID;
  shelf_id: UUID;
  camera_id: UUID | null;
  occupancy_ratio: number;
  occupancy_level: OccupancyLevel;
  confidence: number;
  is_occluded: boolean;
  product_counts: Record<string, number>;
  detected_object_count: number | null;
  snapshot_path: string | null;
  captured_at: ISODateTime;
  method: string;
}

export interface InventoryEvent {
  id: UUID;
  shelf_id: UUID;
  camera_id: UUID | null;
  product_id: UUID | null;
  event_type: InventoryEventType;
  quantity_delta: number | null;
  confidence: number;
  occupancy_before: number | null;
  occupancy_after: number | null;
  track_key: string | null;
  occurred_at: ISODateTime;
  event_metadata: Record<string, unknown>;
}

// ----------------------------------------------------------------------- pos
export type POSTransactionStatus = "COMPLETED" | "VOIDED" | "REFUNDED" | "PENDING";

/**
 * The outcome of matching a shelf observation against sales.
 *
 * `UNMATCHED` means **no sale was found in the window**. It is a statement
 * about the data, not about a person: restocking, staff moving stock, an item
 * put back elsewhere, a mis-scanned item and a detection error all land here.
 * Never render this status without its `explanation`.
 */
export type CorrelationStatus = "MATCHED" | "UNMATCHED" | "UNCERTAIN";

export type ReviewOutcome = "CORRECT" | "INCORRECT" | "EXPLAINED" | "UNCLEAR";

export interface POSItem {
  id: UUID;
  product_id: UUID | null;
  external_product_ref: string | null;
  name: string;
  quantity: string;
  unit_price: string | null;
  total_price: string | null;
  discount_amount: string | null;
}

export interface POSTransaction {
  id: UUID;
  store_id: UUID;
  provider: string;
  external_id: string;
  receipt_number: string | null;
  transaction_at: ISODateTime;
  status: POSTransactionStatus;
  total_amount: string;
  currency: string;
  payment_method: string | null;
  terminal_ref: string | null;
  cashier_ref: string | null;
  imported_at: ISODateTime | null;
  items: POSItem[];
}

export interface Correlation {
  id: UUID;
  store_id: UUID;
  inventory_event_id: UUID;
  pos_transaction_id: UUID | null;
  event_id: UUID | null;
  status: CorrelationStatus;
  score: number;
  window_seconds: number;
  candidates_considered: number;
  score_breakdown: Record<string, number>;
  explanation: string;
  correlated_at: ISODateTime;
  reviewed_by_id: UUID | null;
  reviewed_at: ISODateTime | null;
  review_outcome: ReviewOutcome | null;
  review_notes: string | null;
}

export interface CorrelationAccuracy {
  total: number;
  matched: number;
  unmatched: number;
  uncertain: number;
  reviewed: number;
  review_correct: number;
  review_incorrect: number;
  review_explained: number;
  /** Null until a human has judged at least one result. */
  accuracy: number | null;
}

export interface POSConfig {
  store_id: UUID;
  provider: string | null;
  options: Record<string, unknown>;
  available_providers: string[];
}

export interface POSImportResult {
  created: number;
  skipped: number;
  store_id: UUID;
  provider: string;
}
