/**
 * Shared UI primitives.
 *
 * `EventKindBadge` is the important one. The product's central rule -- an
 * observation is not a conclusion -- has to be visible, not just true in the
 * database. An inference gets a distinct mark, its confidence stated, and
 * hedged wording; a measurement does not. Rendering both identically would
 * turn a guess into evidence on screen, which is exactly what this system
 * exists to avoid.
 */

import type { ReactNode } from "react";

import type { CameraStatus, ConfidenceLevel, Severity } from "@/types/api";

export function classNames(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(" ");
}

// ------------------------------------------------------------------ severity
const SEVERITY_STYLES: Record<Severity, string> = {
  INFO: "bg-slate-500/15 text-slate-300 ring-1 ring-inset ring-slate-500/30",
  LOW: "bg-sky-500/15 text-sky-300 ring-1 ring-inset ring-sky-500/30",
  MEDIUM: "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-500/30",
  HIGH: "bg-orange-500/15 text-orange-300 ring-1 ring-inset ring-orange-500/30",
  CRITICAL: "bg-rose-500/20 text-rose-300 ring-1 ring-inset ring-rose-500/40",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={classNames("badge", SEVERITY_STYLES[severity])}>{severity}</span>
  );
}

/** A slim colour bar for table rows, where a badge would be too heavy. */
export function SeverityBar({ severity }: { severity: Severity }) {
  const colour: Record<Severity, string> = {
    INFO: "bg-slate-500",
    LOW: "bg-sky-400",
    MEDIUM: "bg-amber-400",
    HIGH: "bg-orange-400",
    CRITICAL: "bg-rose-500",
  };
  return (
    <span
      aria-hidden
      className={classNames("inline-block h-full w-1 rounded-full", colour[severity])}
    />
  );
}

// -------------------------------------------------- observation vs inference
export function EventKindBadge({
  isInference,
  confidence,
}: {
  isInference: boolean;
  confidence?: ConfidenceLevel | null;
}) {
  if (!isInference) {
    return (
      <span
        className="badge bg-slate-500/10 text-slate-400 ring-1 ring-inset ring-slate-600/40"
        title="Measured by the camera"
      >
        Observed
      </span>
    );
  }
  return (
    <span
      className="badge border border-dashed border-violet-400/60 bg-violet-500/10 text-violet-300"
      title="Inferred from other events by a configured rule. Not a finding."
    >
      Inferred{confidence ? ` · ${confidence.toLowerCase()} confidence` : ""}
    </span>
  );
}

/**
 * The explanatory note shown with any inference.
 *
 * Spelled out in full rather than abbreviated: the reader may be about to
 * discipline someone, and the limits of what the system actually knows have to
 * be impossible to miss.
 */
export function InferenceNotice({
  ruleName,
  sourceCount,
  confidence,
}: {
  ruleName?: string | null;
  sourceCount: number;
  confidence?: ConfidenceLevel | null;
}) {
  return (
    <div className="inference-mark rounded-r-lg px-4 py-3 text-sm text-slate-300">
      <p className="font-medium text-violet-300">This is an inference, not an observation.</p>
      <p className="mt-1 text-slate-400">
        A configured rule
        {ruleName ? ` (${ruleName})` : ""} drew this conclusion from{" "}
        {sourceCount} recorded observation{sourceCount === 1 ? "" : "s"}
        {confidence ? ` at ${confidence.toLowerCase()} confidence` : ""}. It is not a
        finding of wrongdoing. Review the supporting evidence before acting.
      </p>
    </div>
  );
}

// -------------------------------------------------------------- camera status
const CAMERA_STATUS_STYLES: Record<CameraStatus, { dot: string; text: string }> = {
  ONLINE: { dot: "bg-emerald-400", text: "text-emerald-300" },
  OFFLINE: { dot: "bg-slate-500", text: "text-slate-400" },
  CONNECTING: { dot: "bg-amber-400 animate-pulse", text: "text-amber-300" },
  ERROR: { dot: "bg-rose-500", text: "text-rose-300" },
  DISABLED: { dot: "bg-slate-700", text: "text-slate-500" },
};

export function CameraStatusPill({ status }: { status: CameraStatus }) {
  const style = CAMERA_STATUS_STYLES[status];
  return (
    <span className={classNames("inline-flex items-center gap-2 text-xs font-medium", style.text)}>
      <span className={classNames("h-2 w-2 rounded-full", style.dot)} aria-hidden />
      {status.charAt(0) + status.slice(1).toLowerCase()}
    </span>
  );
}

// ------------------------------------------------------------------- layout
export function Card({
  title,
  action,
  children,
  className,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={classNames("card", className)}>
      {(title || action) && (
        <header className="card-header">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function StatTile({
  label,
  value,
  hint,
  tone = "neutral",
  loading,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
  loading?: boolean;
}) {
  const toneClass = {
    neutral: "text-slate-100",
    good: "text-emerald-300",
    warn: "text-amber-300",
    bad: "text-rose-300",
  }[tone];

  return (
    <div className="card px-5 py-4">
      <p className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </p>
      {loading ? (
        <div className="mt-2 h-8 w-20 animate-pulse rounded bg-canvas-overlay" />
      ) : (
        <p className={classNames("mt-1 text-3xl font-semibold tabular-nums", toneClass)}>
          {value}
        </p>
      )}
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

// -------------------------------------------------------------------- states
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {description && (
        <p className="mt-1.5 max-w-md text-sm text-slate-500">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <p className="text-sm font-medium text-rose-300">Something went wrong</p>
      <p className="mt-1.5 max-w-md text-sm text-slate-400">{message}</p>
      {onRetry && (
        <button type="button" className="btn-secondary mt-4" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function LoadingRows({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <tbody>
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <tr key={rowIndex}>
          {Array.from({ length: columns }).map((__, columnIndex) => (
            <td key={columnIndex} className="px-4 py-3">
              <div className="h-4 animate-pulse rounded bg-canvas-overlay" />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-400" role="status">
      <span
        className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-brand-400"
        aria-hidden
      />
      {label ?? "Loading"}
    </div>
  );
}

// --------------------------------------------------------------------- time
/**
 * Relative time, with the absolute time always available on hover.
 *
 * "3 minutes ago" is what an operator scans for; the exact timestamp is what
 * they need when writing an incident report, so it is never more than a hover
 * away.
 */
export function RelativeTime({ value }: { value: string }) {
  const date = new Date(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);

  let text: string;
  if (seconds < 45) text = "just now";
  else if (seconds < 90) text = "a minute ago";
  else if (seconds < 3600) text = `${Math.round(seconds / 60)} min ago`;
  else if (seconds < 86400) text = `${Math.round(seconds / 3600)} h ago`;
  else if (seconds < 604800) text = `${Math.round(seconds / 86400)} d ago`;
  else text = date.toLocaleDateString();

  return (
    <time dateTime={value} title={date.toLocaleString()} className="tabular-nums">
      {text}
    </time>
  );
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/** Human label for an event type, e.g. RESTRICTED_AREA_ENTRY -> "Restricted area entry". */
export function humaniseEventType(value: string): string {
  const lower = value.replace(/_/g, " ").toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}
