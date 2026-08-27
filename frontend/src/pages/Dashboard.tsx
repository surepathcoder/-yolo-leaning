/**
 * The operations dashboard.
 *
 * Ordered by what an operator needs to decide, not by what is easy to query:
 * open high-priority alerts first (something needs a person now), then camera
 * health (is the system even watching), then activity.
 *
 * The camera-health tile is deliberately prominent. The most common real-world
 * failure of a CCTV analytics deployment is not a wrong detection -- it is a
 * camera that has been offline for a week and nobody noticed, so the system was
 * quietly not watching the thing it was bought to watch.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useCallback } from "react";

import {
  Card,
  EmptyState,
  EventKindBadge,
  RelativeTime,
  SeverityBadge,
  StatTile,
  humaniseEventType,
} from "@/components/ui/primitives";
import { useRealtimeEvents } from "@/hooks/useRealtime";
import { alertApi, cameraApi, eventApi, storeApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";

export function DashboardPage() {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);

  const stores = useQuery({
    queryKey: ["stores", { limit: 100 }],
    queryFn: () => storeApi.list({ limit: 100 }),
    enabled: can("store:read"),
  });

  const cameraHealth = useQuery({
    queryKey: ["cameras", "health"],
    queryFn: () => cameraApi.health(),
    enabled: can("camera:read"),
    refetchInterval: 30_000,
  });

  const alertSummary = useQuery({
    queryKey: ["alerts", "summary"],
    queryFn: () => alertApi.summary(),
    enabled: can("alert:read"),
  });

  const eventSummary = useQuery({
    queryKey: ["events", "summary"],
    queryFn: () => eventApi.summary(),
    enabled: can("event:read"),
  });

  const recentEvents = useQuery({
    queryKey: ["events", { limit: 8 }],
    queryFn: () => eventApi.list({ limit: 8 }),
    enabled: can("event:read"),
  });

  const openAlerts = useQuery({
    queryKey: ["alerts", { status: "NEW", limit: 5 }],
    queryFn: () => alertApi.list({ status: "NEW", limit: 5 }),
    enabled: can("alert:read"),
  });

  // A new event or alert invalidates the summaries so the tiles move without
  // a refresh. Refetching rather than patching the cache keeps one source of
  // truth and avoids the counts drifting from the server's.
  const onRealtime = useCallback(
    (message: { type: string }) => {
      if (message.type === "event") {
        void queryClient.invalidateQueries({ queryKey: ["events"] });
      } else if (message.type === "alert") {
        void queryClient.invalidateQueries({ queryKey: ["alerts"] });
      } else if (message.type === "camera_status") {
        void queryClient.invalidateQueries({ queryKey: ["cameras"] });
      }
    },
    [queryClient],
  );
  useRealtimeEvents(onRealtime, [onRealtime]);

  const health = cameraHealth.data;
  const camerasNotWatching = health ? health.OFFLINE + health.ERROR : 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Dashboard</h1>
        <p className="mt-1 text-sm text-slate-500">
          Live operational status across your stores.
        </p>
      </div>

      {/* ---------------------------------------------------------- tiles */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Open alerts"
          value={alertSummary.data?.open_total ?? 0}
          hint={
            alertSummary.data?.high_priority_open
              ? `${alertSummary.data.high_priority_open} high priority`
              : "Nothing needs attention"
          }
          tone={alertSummary.data?.high_priority_open ? "bad" : "neutral"}
          loading={alertSummary.isLoading}
        />
        <StatTile
          label="Cameras online"
          value={`${health?.ONLINE ?? 0}/${health?.total ?? 0}`}
          hint={
            camerasNotWatching > 0
              ? `${camerasNotWatching} not watching`
              : "All cameras reporting"
          }
          tone={camerasNotWatching > 0 ? "warn" : "good"}
          loading={cameraHealth.isLoading}
        />
        <StatTile
          label="Events (24 h)"
          value={eventSummary.data?.events ?? 0}
          hint={
            eventSummary.data
              ? `${eventSummary.data.high_severity} high severity`
              : undefined
          }
          loading={eventSummary.isLoading}
        />
        <StatTile
          label="Stores"
          value={stores.data?.total ?? 0}
          hint={
            eventSummary.data
              ? `${eventSummary.data.line_crossings} door crossings today`
              : undefined
          }
          loading={stores.isLoading}
        />
      </div>

      {/* A camera that is offline is a camera that is not watching. Say so
          plainly rather than leaving it to be inferred from a tile. */}
      {camerasNotWatching > 0 && (
        <div className="flex items-center justify-between rounded-xl border border-amber-500/30 bg-amber-500/10 px-5 py-3.5">
          <p className="text-sm text-amber-200">
            <span className="font-medium">
              {camerasNotWatching} camera{camerasNotWatching === 1 ? " is" : "s are"} not
              currently watching.
            </span>{" "}
            Nothing is being detected on {camerasNotWatching === 1 ? "it" : "them"}.
          </p>
          <Link to="/cameras" className="btn-secondary shrink-0">
            Review cameras
          </Link>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* ------------------------------------------------------ alerts */}
        <Card
          title="Alerts needing attention"
          action={
            <Link to="/alerts" className="text-xs text-brand-300 hover:underline">
              View all
            </Link>
          }
        >
          {openAlerts.isLoading ? (
            <div className="space-y-2 p-5">
              {[0, 1, 2].map((index) => (
                <div key={index} className="h-12 animate-pulse rounded bg-canvas-overlay" />
              ))}
            </div>
          ) : openAlerts.data && openAlerts.data.items.length > 0 ? (
            <ul className="divide-y divide-canvas-border/60">
              {openAlerts.data.items.map((alert) => (
                <li key={alert.id}>
                  <Link
                    to={`/alerts?focus=${alert.id}`}
                    className="flex items-start gap-3 px-5 py-3.5 transition-colors hover:bg-canvas-overlay/60"
                  >
                    <SeverityBadge severity={alert.severity} />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-slate-200">
                        {alert.title}
                      </p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        <RelativeTime value={alert.created_at} />
                        {alert.occurrences > 1 && ` · ${alert.occurrences} occurrences`}
                      </p>
                    </div>
                    {alert.event?.is_inference && (
                      <EventKindBadge isInference confidence={alert.event.confidence_level} />
                    )}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No open alerts"
              description="Nothing currently needs a person's attention."
            />
          )}
        </Card>

        {/* ------------------------------------------------------ events */}
        <Card
          title="Recent events"
          action={
            <Link to="/events" className="text-xs text-brand-300 hover:underline">
              Full timeline
            </Link>
          }
        >
          {recentEvents.isLoading ? (
            <div className="space-y-2 p-5">
              {[0, 1, 2, 3].map((index) => (
                <div key={index} className="h-10 animate-pulse rounded bg-canvas-overlay" />
              ))}
            </div>
          ) : recentEvents.data && recentEvents.data.items.length > 0 ? (
            <ul className="divide-y divide-canvas-border/60">
              {recentEvents.data.items.map((event) => (
                <li
                  key={event.id}
                  className={event.is_inference ? "inference-mark" : undefined}
                >
                  <Link
                    to={`/events?focus=${event.id}`}
                    className="flex items-center gap-3 px-5 py-3 transition-colors hover:bg-canvas-overlay/60"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-slate-300">{event.summary}</p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        {humaniseEventType(event.event_type)} ·{" "}
                        <RelativeTime value={event.occurred_at} />
                      </p>
                    </div>
                    <SeverityBadge severity={event.severity} />
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No events yet"
              description="Events appear here once a camera pipeline is running."
              action={
                can("camera:write") ? (
                  <Link to="/cameras" className="btn-primary">
                    Set up a camera
                  </Link>
                ) : undefined
              }
            />
          )}
        </Card>
      </div>
    </div>
  );
}
