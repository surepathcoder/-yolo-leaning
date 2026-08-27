/**
 * The event timeline.
 *
 * The visual grammar here carries the product's central rule. An inference has
 * a dashed violet edge, an "Inferred" badge with its confidence, and -- when
 * opened -- the observations it was drawn from, listed in full. A measurement
 * has none of that decoration.
 *
 * Filtering is server side. The events table grows by millions of rows a month
 * in a busy chain; sending it to the browser to filter is not an option, and
 * pretending otherwise would work fine in a demo and fail in month two.
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  Card,
  EmptyState,
  ErrorState,
  EventKindBadge,
  InferenceNotice,
  LoadingRows,
  RelativeTime,
  SeverityBadge,
  classNames,
  formatDuration,
  humaniseEventType,
} from "@/components/ui/primitives";
import { cameraApi, eventApi, storeApi } from "@/services/endpoints";
import type { EventSummary, Severity, UUID } from "@/types/api";
import { SEVERITIES } from "@/types/api";

const PAGE_SIZE = 50;

type InferenceFilter = "all" | "observations" | "inferences";

export function EventsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [offset, setOffset] = useState(0);
  const [storeId, setStoreId] = useState<UUID | "">("");
  const [cameraId, setCameraId] = useState<UUID | "">("");
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [severities, setSeverities] = useState<Severity[]>([]);
  const [kind, setKind] = useState<InferenceFilter>("all");
  const [trackKey, setTrackKey] = useState("");

  const focusedId = searchParams.get("focus");

  const stores = useQuery({ queryKey: ["stores"], queryFn: () => storeApi.list({ limit: 100 }) });
  const cameras = useQuery({
    queryKey: ["cameras", { store_id: storeId }],
    queryFn: () => cameraApi.list({ store_id: storeId || undefined, limit: 200 }),
  });
  const types = useQuery({ queryKey: ["events", "types"], queryFn: () => eventApi.types() });

  const query = useMemo(
    () => ({
      store_id: storeId || undefined,
      camera_id: cameraId || undefined,
      event_type: eventTypes.length ? eventTypes : undefined,
      severity: severities.length ? severities : undefined,
      track_key: trackKey || undefined,
      is_inference:
        kind === "all" ? undefined : kind === "inferences" ? true : false,
      limit: PAGE_SIZE,
      offset,
    }),
    [storeId, cameraId, eventTypes, severities, trackKey, kind, offset],
  );

  const events = useQuery({
    queryKey: ["events", query],
    queryFn: () => eventApi.list(query),
  });

  const resetPaging = () => setOffset(0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Events</h1>
        <p className="mt-1 text-sm text-slate-500">
          Everything the system observed, and every conclusion it drew from those
          observations.
        </p>
      </div>

      {/* -------------------------------------------------------- filters */}
      <Card>
        <div className="grid gap-4 p-5 md:grid-cols-2 lg:grid-cols-5">
          <div>
            <label className="label" htmlFor="filter-store">
              Store
            </label>
            <select
              id="filter-store"
              className="input"
              value={storeId}
              onChange={(event) => {
                setStoreId(event.target.value);
                setCameraId("");
                resetPaging();
              }}
            >
              <option value="">All stores</option>
              {stores.data?.items.map((store) => (
                <option key={store.id} value={store.id}>
                  {store.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="filter-camera">
              Camera
            </label>
            <select
              id="filter-camera"
              className="input"
              value={cameraId}
              onChange={(event) => {
                setCameraId(event.target.value);
                resetPaging();
              }}
            >
              <option value="">All cameras</option>
              {cameras.data?.items.map((camera) => (
                <option key={camera.id} value={camera.id}>
                  {camera.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="filter-type">
              Event type
            </label>
            <select
              id="filter-type"
              className="input"
              value={eventTypes[0] ?? ""}
              onChange={(event) => {
                setEventTypes(event.target.value ? [event.target.value] : []);
                resetPaging();
              }}
            >
              <option value="">All types</option>
              {types.data?.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                  {type.is_inference ? " (inference)" : ""}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="filter-severity">
              Severity
            </label>
            <select
              id="filter-severity"
              className="input"
              value={severities[0] ?? ""}
              onChange={(event) => {
                setSeverities(event.target.value ? [event.target.value as Severity] : []);
                resetPaging();
              }}
            >
              <option value="">Any severity</option>
              {SEVERITIES.map((severity) => (
                <option key={severity} value={severity}>
                  {severity}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="filter-track">
              Track ID
            </label>
            <input
              id="filter-track"
              className="input"
              placeholder="camera:session:42"
              value={trackKey}
              onChange={(event) => {
                setTrackKey(event.target.value);
                resetPaging();
              }}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-canvas-border px-5 py-3">
          <span className="text-xs text-slate-500">Show:</span>
          {(
            [
              ["all", "Everything"],
              ["observations", "Observations only"],
              ["inferences", "Inferences only"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => {
                setKind(value);
                resetPaging();
              }}
              className={classNames(
                "rounded-full px-3 py-1 text-xs font-medium transition-colors",
                kind === value
                  ? "bg-brand-600/20 text-brand-200 ring-1 ring-inset ring-brand-500/40"
                  : "text-slate-400 hover:bg-canvas-overlay hover:text-slate-200",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </Card>

      {/* ---------------------------------------------------------- table */}
      <Card
        title={
          events.data
            ? `${events.data.total.toLocaleString()} event${events.data.total === 1 ? "" : "s"}`
            : "Events"
        }
      >
        {events.isError ? (
          <ErrorState
            message={(events.error as Error).message}
            onRetry={() => void events.refetch()}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="w-[42%]">Event</th>
                  <th>Kind</th>
                  <th>Severity</th>
                  <th>Track</th>
                  <th>Duration</th>
                  <th>When</th>
                </tr>
              </thead>
              {events.isLoading ? (
                <LoadingRows rows={8} columns={6} />
              ) : (
                <tbody>
                  {events.data?.items.map((event) => (
                    <EventRow
                      key={event.id}
                      event={event}
                      expanded={focusedId === event.id}
                      onToggle={() =>
                        setSearchParams(
                          focusedId === event.id ? {} : { focus: event.id },
                          { replace: true },
                        )
                      }
                    />
                  ))}
                </tbody>
              )}
            </table>

            {events.data && events.data.items.length === 0 && (
              <EmptyState
                title="No events match these filters"
                description="Try widening the time range or clearing a filter."
              />
            )}
          </div>
        )}

        {events.data && events.data.total > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-canvas-border px-5 py-3">
            <p className="text-xs text-slate-500">
              Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, events.data.total)} of{" "}
              {events.data.total.toLocaleString()}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                className="btn-secondary"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={offset + PAGE_SIZE >= events.data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function EventRow({
  event,
  expanded,
  onToggle,
}: {
  event: EventSummary;
  expanded: boolean;
  onToggle: () => void;
}) {
  const detail = useQuery({
    queryKey: ["events", event.id],
    queryFn: () => eventApi.get(event.id),
    enabled: expanded,
  });

  return (
    <>
      <tr
        className={classNames("cursor-pointer", event.is_inference && "inference-mark")}
        onClick={onToggle}
      >
        <td>
          <p className="text-slate-200">{event.summary}</p>
          <p className="mt-0.5 text-xs text-slate-500">
            {humaniseEventType(event.event_type)}
          </p>
        </td>
        <td>
          <EventKindBadge
            isInference={event.is_inference}
            confidence={event.confidence_level}
          />
        </td>
        <td>
          <SeverityBadge severity={event.severity} />
        </td>
        <td className="font-mono text-xs text-slate-400">
          {event.track_id ?? "—"}
        </td>
        <td className="text-xs text-slate-400">
          {formatDuration(event.duration_seconds)}
        </td>
        <td className="text-xs text-slate-400">
          <RelativeTime value={event.occurred_at} />
        </td>
      </tr>

      {expanded && (
        <tr>
          <td colSpan={6} className="bg-canvas-overlay/40 px-4 py-4">
            {detail.isLoading ? (
              <div className="h-20 animate-pulse rounded bg-canvas-overlay" />
            ) : detail.data ? (
              <div className="space-y-4">
                {detail.data.is_inference && (
                  <InferenceNotice
                    ruleName={detail.data.rule_name}
                    sourceCount={detail.data.source_events.length}
                    confidence={detail.data.confidence_level}
                  />
                )}

                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs md:grid-cols-4">
                  <Detail label="Occurred" value={new Date(detail.data.occurred_at).toLocaleString()} />
                  <Detail label="Rule" value={detail.data.rule_name ?? "—"} />
                  <Detail
                    label="Model confidence"
                    value={
                      detail.data.confidence !== null
                        ? `${Math.round(detail.data.confidence * 100)}%`
                        : "—"
                    }
                  />
                  <Detail label="Track key" value={detail.data.track_key ?? "—"} mono />
                </dl>

                {detail.data.source_events.length > 0 && (
                  <div>
                    <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-slate-500">
                      Observations this conclusion rests on
                    </p>
                    <ul className="space-y-1.5">
                      {detail.data.source_events.map((source) => (
                        <li
                          key={source.id}
                          className="rounded-lg border border-canvas-border bg-canvas px-3 py-2 text-xs text-slate-300"
                        >
                          <span className="text-slate-500">
                            {new Date(source.occurred_at).toLocaleTimeString()} ·{" "}
                            {humaniseEventType(source.event_type)}
                          </span>
                          <br />
                          {source.summary}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {Object.keys(detail.data.event_metadata).length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-slate-500 hover:text-slate-300">
                      Technical detail
                    </summary>
                    <pre className="mt-2 overflow-x-auto rounded-lg bg-canvas p-3 font-mono text-2xs text-slate-400">
                      {JSON.stringify(detail.data.event_metadata, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            ) : (
              <p className="text-sm text-slate-500">Could not load this event.</p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function Detail({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className={classNames("mt-0.5 text-slate-300", mono && "font-mono")}>{value}</dd>
    </div>
  );
}
