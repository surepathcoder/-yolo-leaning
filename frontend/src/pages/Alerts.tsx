/**
 * The alert queue.
 *
 * Resolving an alert *always* asks whether it was worth raising. That question
 * is the only ground truth available for measuring the false-positive rate,
 * and without it every threshold decision is guesswork. Making it a required
 * choice rather than an optional note is a deliberate friction: two clicks now
 * buys the ability to tune the system honestly later.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  Card,
  EmptyState,
  ErrorState,
  EventKindBadge,
  InferenceNotice,
  RelativeTime,
  SeverityBadge,
  StatTile,
  classNames,
} from "@/components/ui/primitives";
import { useRealtimeEvents } from "@/hooks/useRealtime";
import { alertApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { Alert, AlertStatus } from "@/types/api";
import { ALERT_STATUSES } from "@/types/api";

export function AlertsPage() {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState<AlertStatus | "">("NEW");

  const focusedId = searchParams.get("focus");

  const alerts = useQuery({
    queryKey: ["alerts", { status, limit: 50 }],
    queryFn: () => alertApi.list({ status: status || undefined, limit: 50 }),
  });

  const summary = useQuery({
    queryKey: ["alerts", "summary"],
    queryFn: () => alertApi.summary(),
  });

  const falsePositives = useQuery({
    queryKey: ["alerts", "false-positive-rate"],
    queryFn: () => alertApi.falsePositiveRate(),
    enabled: can("analytics:read"),
  });

  const onRealtime = useCallback(
    (message: { type: string }) => {
      if (message.type === "alert") {
        void queryClient.invalidateQueries({ queryKey: ["alerts"] });
      }
    },
    [queryClient],
  );
  useRealtimeEvents(onRealtime, [onRealtime]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Alerts</h1>
        <p className="mt-1 text-sm text-slate-500">
          Events escalated for a person to look at.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Open"
          value={summary.data?.open_total ?? 0}
          loading={summary.isLoading}
        />
        <StatTile
          label="High priority"
          value={summary.data?.high_priority_open ?? 0}
          tone={summary.data?.high_priority_open ? "bad" : "neutral"}
          loading={summary.isLoading}
        />
        <StatTile
          label="Resolved"
          value={summary.data?.by_status.RESOLVED ?? 0}
          loading={summary.isLoading}
        />
        <StatTile
          label="Not actionable"
          value={
            falsePositives.data?.false_positive_rate !== null &&
            falsePositives.data?.false_positive_rate !== undefined
              ? `${Math.round(falsePositives.data.false_positive_rate * 100)}%`
              : "—"
          }
          hint={
            falsePositives.data?.judged
              ? `of ${falsePositives.data.judged} judged`
              : "no alerts judged yet"
          }
          tone={
            (falsePositives.data?.false_positive_rate ?? 0) > 0.4 ? "warn" : "neutral"
          }
          loading={falsePositives.isLoading}
        />
      </div>

      <Card>
        <div className="flex flex-wrap items-center gap-2 px-5 py-3">
          <span className="text-xs text-slate-500">Status:</span>
          {(["", ...ALERT_STATUSES] as const).map((value) => (
            <button
              key={value || "all"}
              type="button"
              onClick={() => setStatus(value)}
              className={classNames(
                "rounded-full px-3 py-1 text-xs font-medium transition-colors",
                status === value
                  ? "bg-brand-600/20 text-brand-200 ring-1 ring-inset ring-brand-500/40"
                  : "text-slate-400 hover:bg-canvas-overlay hover:text-slate-200",
              )}
            >
              {value || "All"}
            </button>
          ))}
        </div>
      </Card>

      {alerts.isError ? (
        <ErrorState
          message={(alerts.error as Error).message}
          onRetry={() => void alerts.refetch()}
        />
      ) : alerts.isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((index) => (
            <div key={index} className="h-24 animate-pulse rounded-xl bg-canvas-raised" />
          ))}
        </div>
      ) : alerts.data && alerts.data.items.length > 0 ? (
        <div className="space-y-3">
          {alerts.data.items.map((alert) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              expanded={focusedId === alert.id}
              onToggle={() =>
                setSearchParams(
                  focusedId === alert.id ? {} : { focus: alert.id },
                  { replace: true },
                )
              }
            />
          ))}
        </div>
      ) : (
        <EmptyState
          title="Nothing here"
          description={
            status === "NEW"
              ? "No alerts are waiting for attention."
              : "No alerts match this filter."
          }
        />
      )}
    </div>
  );
}

function AlertCard({
  alert,
  expanded,
  onToggle,
}: {
  alert: Alert;
  expanded: boolean;
  onToggle: () => void;
}) {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [notes, setNotes] = useState("");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["alerts"] });
  };

  const acknowledge = useMutation({
    mutationFn: () => alertApi.acknowledge(alert.id),
    onSuccess: invalidate,
  });

  const resolve = useMutation({
    mutationFn: (wasActionable: boolean) =>
      alertApi.resolve(alert.id, {
        notes: notes || undefined,
        was_actionable: wasActionable,
      }),
    onSuccess: invalidate,
  });

  const isOpen = alert.status === "NEW" || alert.status === "ACKNOWLEDGED";
  const isInference = alert.event?.is_inference ?? false;

  return (
    <article
      className={classNames(
        "card overflow-hidden transition-colors",
        isInference && "inference-mark",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-start gap-4 px-5 py-4 text-left hover:bg-canvas-overlay/50"
      >
        <SeverityBadge severity={alert.severity} />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-slate-100">{alert.title}</p>
          <p className="mt-1 line-clamp-2 text-sm text-slate-400">
            {alert.message.split("\n")[0]}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
            <RelativeTime value={alert.created_at} />
            <span>· {alert.status.toLowerCase()}</span>
            {alert.occurrences > 1 && <span>· {alert.occurrences} occurrences</span>}
            {alert.was_actionable === false && (
              <span className="text-amber-400">· judged not actionable</span>
            )}
          </div>
        </div>
        {isInference && (
          <EventKindBadge isInference confidence={alert.event?.confidence_level} />
        )}
      </button>

      {expanded && (
        <div className="space-y-4 border-t border-canvas-border px-5 py-4">
          {isInference && alert.event && (
            <InferenceNotice
              sourceCount={0}
              confidence={alert.event.confidence_level}
            />
          )}

          <p className="whitespace-pre-line text-sm text-slate-300">{alert.message}</p>

          {alert.resolution_notes && (
            <div className="rounded-lg border border-canvas-border bg-canvas px-3 py-2">
              <p className="text-2xs uppercase tracking-wider text-slate-500">
                Resolution notes
              </p>
              <p className="mt-1 text-sm text-slate-300">{alert.resolution_notes}</p>
            </div>
          )}

          {isOpen && (
            <div className="space-y-3">
              {can("alert:resolve") && (
                <div>
                  <label className="label" htmlFor={`notes-${alert.id}`}>
                    What actually happened? (optional)
                  </label>
                  <textarea
                    id={`notes-${alert.id}`}
                    className="input min-h-[72px]"
                    placeholder="e.g. Cleaner on site after hours; expected."
                    value={notes}
                    onChange={(event) => setNotes(event.target.value)}
                  />
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                {alert.status === "NEW" && can("alert:acknowledge") && (
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => acknowledge.mutate()}
                    disabled={acknowledge.isPending}
                  >
                    {acknowledge.isPending ? "Working…" : "Acknowledge"}
                  </button>
                )}

                {can("alert:resolve") && (
                  <>
                    {/* Two buttons, not one with a checkbox. The verdict is the
                        point of resolving, so it cannot be skipped by accident. */}
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={() => resolve.mutate(true)}
                      disabled={resolve.isPending}
                    >
                      Resolve — this was real
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => resolve.mutate(false)}
                      disabled={resolve.isPending}
                    >
                      Resolve — not worth alerting
                    </button>
                  </>
                )}
              </div>

              {can("alert:resolve") && (
                <p className="text-xs text-slate-500">
                  Your answer is the only measure of how often this system cries
                  wolf. It is used to tune thresholds, not to judge you.
                </p>
              )}
            </div>
          )}

          {(acknowledge.isError || resolve.isError) && (
            <p className="text-sm text-rose-300">
              {((acknowledge.error ?? resolve.error) as Error).message}
            </p>
          )}
        </div>
      )}
    </article>
  );
}
