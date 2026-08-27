/**
 * Live view.
 *
 * Video streaming is not implemented, and this page says so plainly rather
 * than showing a placeholder that looks like a broken feed. What it does show
 * is real: the detections arriving from that camera right now, over the same
 * WebSocket the rest of the dashboard uses, with the configured zones drawn.
 *
 * A browser cannot play RTSP directly. Reaching a live picture needs a
 * transcoding path (WebRTC through a media server, or HLS at several seconds
 * of latency), which is substantial infrastructure in its own right.
 */

import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import {
  CameraStatusPill,
  Card,
  EmptyState,
  EventKindBadge,
  RelativeTime,
  SeverityBadge,
  humaniseEventType,
} from "@/components/ui/primitives";
import { useRealtimeEvents, useRealtimeStore } from "@/hooks/useRealtime";
import { cameraApi, zoneApi } from "@/services/endpoints";
import type { RealtimeMessage, Severity, UUID } from "@/types/api";

interface LiveEvent {
  id: string;
  eventType: string;
  severity: Severity;
  summary: string;
  occurredAt: string;
  isInference: boolean;
}

export function LiveViewPage() {
  const [cameraId, setCameraId] = useState<UUID | "">("");
  const [liveEvents, setLiveEvents] = useState<LiveEvent[]>([]);
  const setCameraFilter = useRealtimeStore((state) => state.setCameraFilter);

  const cameras = useQuery({
    queryKey: ["cameras"],
    queryFn: () => cameraApi.list({ limit: 200 }),
  });

  const zones = useQuery({
    queryKey: ["zones", { camera_id: cameraId }],
    queryFn: () => zoneApi.forCamera(cameraId as UUID),
    enabled: Boolean(cameraId),
  });

  const onMessage = useCallback(
    (message: RealtimeMessage) => {
      if (message.type !== "event") return;
      if (cameraId && message.camera_id !== cameraId) return;
      setLiveEvents((current) =>
        [
          {
            id: message.data.id,
            eventType: message.data.event_type,
            severity: message.data.severity,
            summary: message.data.summary,
            occurredAt: message.data.occurred_at,
            isInference: message.data.is_inference,
          },
          ...current,
        ].slice(0, 40),
      );
    },
    [cameraId],
  );
  useRealtimeEvents(onMessage, [onMessage]);

  const selected = cameras.data?.items.find((camera) => camera.id === cameraId);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Live view</h1>
        <p className="mt-1 text-sm text-slate-500">
          Detections as they happen, on the camera you choose.
        </p>
      </div>

      <Card>
        <div className="px-5 py-4">
          <label className="label" htmlFor="live-camera">
            Camera
          </label>
          <select
            id="live-camera"
            className="input max-w-md"
            value={cameraId}
            onChange={(event) => {
              const value = event.target.value as UUID | "";
              setCameraId(value);
              setLiveEvents([]);
              // Narrow the socket server side rather than filtering in the
              // browser: an unwatched camera's events never leave the server.
              setCameraFilter(value ? [value] : []);
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
      </Card>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <Card
          title={selected ? selected.name : "Camera view"}
          action={selected ? <CameraStatusPill status={selected.status} /> : undefined}
        >
          <div className="p-5">
            <div className="relative aspect-video overflow-hidden rounded-lg border border-canvas-border bg-canvas">
              <svg viewBox="0 0 100 56.25" className="h-full w-full">
                <defs>
                  <pattern
                    id="live-grid"
                    width="10"
                    height="10"
                    patternUnits="userSpaceOnUse"
                  >
                    <path
                      d="M 10 0 L 0 0 0 10"
                      fill="none"
                      stroke="rgb(35,48,67)"
                      strokeWidth="0.2"
                    />
                  </pattern>
                </defs>
                <rect width="100" height="56.25" fill="url(#live-grid)" />
                {zones.data?.map((zone) => (
                  <polygon
                    key={zone.id}
                    points={zone.polygon
                      .map(([x, y]) => `${x * 100},${y * 56.25}`)
                      .join(" ")}
                    fill={zone.color}
                    fillOpacity="0.12"
                    stroke={zone.color}
                    strokeWidth="0.4"
                  />
                ))}
              </svg>

              <div className="absolute inset-0 flex items-center justify-center bg-canvas/70">
                <div className="max-w-sm px-6 text-center">
                  <p className="text-sm font-medium text-slate-300">
                    Video streaming is not implemented yet
                  </p>
                  <p className="mt-2 text-xs text-slate-500">
                    A browser cannot play RTSP directly; showing live picture needs a
                    transcoding path, which is planned but not built. The configured
                    zones are drawn above, and the detections beside this panel are
                    live and real.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </Card>

        <Card title="Live detections">
          {liveEvents.length === 0 ? (
            <EmptyState
              title="Waiting for activity"
              description={
                selected
                  ? "Events from this camera appear here the moment they happen."
                  : "Start a camera pipeline to see detections."
              }
            />
          ) : (
            <ul className="max-h-[560px] divide-y divide-canvas-border/60 overflow-y-auto">
              {liveEvents.map((event) => (
                <li
                  key={event.id}
                  className={`animate-slide-in px-5 py-3 ${
                    event.isInference ? "inference-mark" : ""
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm text-slate-300">{event.summary}</p>
                    <SeverityBadge severity={event.severity} />
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-slate-500">
                    <span>{humaniseEventType(event.eventType)}</span>
                    <span>·</span>
                    <RelativeTime value={event.occurredAt} />
                    {event.isInference && <EventKindBadge isInference />}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
