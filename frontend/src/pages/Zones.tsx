/**
 * Zone and counting-line editor.
 *
 * Shapes are drawn on a normalised canvas, not on pixels. A zone drawn against
 * a 1920×1080 preview has to stay correct when the pipeline reads a 640×360
 * sub stream, and when an NVR is later reconfigured to a different resolution.
 * Storing 0..1 coordinates makes that automatic; storing pixels would mean a
 * silent, invisible drift of every zone in the estate.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Card, EmptyState, classNames } from "@/components/ui/primitives";
import { ApiError } from "@/services/client";
import { cameraApi, lineApi, zoneApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { NormalisedPoint, UUID, ZoneType } from "@/types/api";
import { ZONE_TYPES } from "@/types/api";

type DrawMode = "zone" | "line" | null;

export function ZonesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const can = useAuth((state) => state.can);
  const cameraId = (searchParams.get("camera") ?? "") as UUID | "";

  const cameras = useQuery({
    queryKey: ["cameras"],
    queryFn: () => cameraApi.list({ limit: 200 }),
  });

  const zones = useQuery({
    queryKey: ["zones", { camera_id: cameraId }],
    queryFn: () => zoneApi.forCamera(cameraId as UUID),
    enabled: Boolean(cameraId),
  });

  const lines = useQuery({
    queryKey: ["lines", { camera_id: cameraId }],
    queryFn: () => lineApi.forCamera(cameraId as UUID),
    enabled: Boolean(cameraId),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">
          Zones &amp; counting lines
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Draw the areas and doorways this camera should watch. Coordinates are
          stored relative to the frame, so they survive a resolution change.
        </p>
      </div>

      <Card>
        <div className="px-5 py-4">
          <label className="label" htmlFor="zone-camera">
            Camera
          </label>
          <select
            id="zone-camera"
            className="input max-w-md"
            value={cameraId}
            onChange={(event) =>
              setSearchParams(event.target.value ? { camera: event.target.value } : {})
            }
          >
            <option value="">Choose a camera…</option>
            {cameras.data?.items.map((camera) => (
              <option key={camera.id} value={camera.id}>
                {camera.name}
              </option>
            ))}
          </select>
        </div>
      </Card>

      {!cameraId ? (
        <EmptyState
          title="Select a camera"
          description="Zones and lines are drawn on a specific camera's view."
        />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <ZoneCanvas
            cameraId={cameraId as UUID}
            zones={zones.data ?? []}
            lines={lines.data ?? []}
            editable={can("zone:write")}
          />

          <div className="space-y-4">
            <Card title={`Zones (${zones.data?.length ?? 0})`}>
              {zones.data && zones.data.length > 0 ? (
                <ul className="divide-y divide-canvas-border/60">
                  {zones.data.map((zone) => (
                    <li key={zone.id} className="flex items-center gap-3 px-5 py-3">
                      <span
                        className="h-3 w-3 shrink-0 rounded-sm"
                        style={{ backgroundColor: zone.color }}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-slate-200">{zone.name}</p>
                        <p className="text-xs text-slate-500">
                          {zone.zone_type.toLowerCase()} · {zone.polygon.length} points
                          {zone.after_hours_monitoring && " · after-hours"}
                        </p>
                      </div>
                      {can("zone:delete") && <DeleteZoneButton id={zone.id} />}
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No zones" description="Draw one on the canvas." />
              )}
            </Card>

            <Card title={`Counting lines (${lines.data?.length ?? 0})`}>
              {lines.data && lines.data.length > 0 ? (
                <ul className="divide-y divide-canvas-border/60">
                  {lines.data.map((line) => (
                    <li key={line.id} className="flex items-center gap-3 px-5 py-3">
                      <span
                        className="h-3 w-3 shrink-0 rounded-sm"
                        style={{ backgroundColor: line.color }}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-slate-200">{line.name}</p>
                        <p className="text-xs text-slate-500">
                          {line.invert_direction ? "inverted" : "standard"} direction ·{" "}
                          {line.cooldown_seconds}s cooldown
                        </p>
                      </div>
                      {can("zone:delete") && <DeleteLineButton id={line.id} />}
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No lines" description="Draw one across a doorway." />
              )}
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}

function DeleteZoneButton({ id }: { id: UUID }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => zoneApi.remove(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["zones"] }),
  });
  return (
    <button
      type="button"
      className="btn-ghost text-xs"
      onClick={() => remove.mutate()}
      disabled={remove.isPending}
    >
      Delete
    </button>
  );
}

function DeleteLineButton({ id }: { id: UUID }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => lineApi.remove(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["lines"] }),
  });
  return (
    <button
      type="button"
      className="btn-ghost text-xs"
      onClick={() => remove.mutate()}
      disabled={remove.isPending}
    >
      Delete
    </button>
  );
}

function ZoneCanvas({
  cameraId,
  zones,
  lines,
  editable,
}: {
  cameraId: UUID;
  zones: { id: UUID; name: string; polygon: NormalisedPoint[]; color: string }[];
  lines: {
    id: UUID;
    name: string;
    point_a: NormalisedPoint;
    point_b: NormalisedPoint;
    color: string;
  }[];
  editable: boolean;
}) {
  const queryClient = useQueryClient();
  const svgRef = useRef<SVGSVGElement>(null);
  const [mode, setMode] = useState<DrawMode>(null);
  const [points, setPoints] = useState<NormalisedPoint[]>([]);
  const [name, setName] = useState("");
  const [zoneType, setZoneType] = useState<ZoneType>("RESTRICTED");
  const [afterHours, setAfterHours] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setMode(null);
    setPoints([]);
    setName("");
    setError(null);
  };

  const createZone = useMutation({
    mutationFn: () =>
      zoneApi.create(cameraId, {
        name,
        zone_type: zoneType,
        polygon: points,
        after_hours_monitoring: afterHours,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["zones"] });
      reset();
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : "Could not save the zone."),
  });

  const createLine = useMutation({
    mutationFn: () =>
      lineApi.create(cameraId, { name, point_a: points[0], point_b: points[1] }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lines"] });
      reset();
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : "Could not save the line."),
  });

  const handleClick = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!mode || !svgRef.current) return;
    const bounds = svgRef.current.getBoundingClientRect();
    // Normalise immediately: the rest of the system never sees a pixel.
    const x = Math.min(Math.max((event.clientX - bounds.left) / bounds.width, 0), 1);
    const y = Math.min(Math.max((event.clientY - bounds.top) / bounds.height, 0), 1);
    const point: NormalisedPoint = [Number(x.toFixed(4)), Number(y.toFixed(4))];

    if (mode === "line") {
      setPoints(points.length >= 2 ? [point] : [...points, point]);
    } else {
      setPoints([...points, point]);
    }
  };

  const canSave =
    name.trim().length > 0 &&
    ((mode === "zone" && points.length >= 3) || (mode === "line" && points.length === 2));

  return (
    <Card
      title="Camera view"
      action={
        editable && (
          <div className="flex gap-2">
            <button
              type="button"
              className={mode === "zone" ? "btn-primary text-xs" : "btn-secondary text-xs"}
              onClick={() => {
                setMode(mode === "zone" ? null : "zone");
                setPoints([]);
              }}
            >
              Draw zone
            </button>
            <button
              type="button"
              className={mode === "line" ? "btn-primary text-xs" : "btn-secondary text-xs"}
              onClick={() => {
                setMode(mode === "line" ? null : "line");
                setPoints([]);
              }}
            >
              Draw line
            </button>
          </div>
        )
      }
    >
      <div className="p-5">
        <svg
          ref={svgRef}
          viewBox="0 0 100 56.25"
          className={classNames(
            "aspect-video w-full rounded-lg border border-canvas-border bg-canvas",
            mode && "cursor-crosshair",
          )}
          onClick={handleClick}
          role="img"
          aria-label="Camera view with drawn zones and counting lines"
        >
          {/* A reference grid. Without a live frame behind it, an installer
              still needs something to judge proportion against. */}
          <defs>
            <pattern id="grid" width="10" height="10" patternUnits="userSpaceOnUse">
              <path
                d="M 10 0 L 0 0 0 10"
                fill="none"
                stroke="rgb(35,48,67)"
                strokeWidth="0.2"
              />
            </pattern>
          </defs>
          <rect width="100" height="56.25" fill="url(#grid)" />

          {zones.map((zone) => (
            <g key={zone.id}>
              <polygon
                points={zone.polygon
                  .map(([x, y]) => `${x * 100},${y * 56.25}`)
                  .join(" ")}
                fill={zone.color}
                fillOpacity="0.15"
                stroke={zone.color}
                strokeWidth="0.4"
              />
              <text
                x={(zone.polygon[0]?.[0] ?? 0) * 100 + 1}
                y={(zone.polygon[0]?.[1] ?? 0) * 56.25 - 1}
                fill={zone.color}
                fontSize="2"
              >
                {zone.name}
              </text>
            </g>
          ))}

          {lines.map((line) => (
            <g key={line.id}>
              <line
                x1={line.point_a[0] * 100}
                y1={line.point_a[1] * 56.25}
                x2={line.point_b[0] * 100}
                y2={line.point_b[1] * 56.25}
                stroke={line.color}
                strokeWidth="0.6"
                strokeLinecap="round"
              />
              <text
                x={line.point_a[0] * 100 + 1}
                y={line.point_a[1] * 56.25 + 3}
                fill={line.color}
                fontSize="2"
              >
                {line.name}
              </text>
            </g>
          ))}

          {/* The shape being drawn right now. */}
          {points.length > 0 && (
            <>
              {mode === "zone" && points.length >= 2 && (
                <polygon
                  points={points.map(([x, y]) => `${x * 100},${y * 56.25}`).join(" ")}
                  fill="#38bdf8"
                  fillOpacity="0.2"
                  stroke="#38bdf8"
                  strokeWidth="0.4"
                  strokeDasharray="1 1"
                />
              )}
              {mode === "line" && points.length === 2 && (
                <line
                  x1={points[0]![0] * 100}
                  y1={points[0]![1] * 56.25}
                  x2={points[1]![0] * 100}
                  y2={points[1]![1] * 56.25}
                  stroke="#a78bfa"
                  strokeWidth="0.6"
                  strokeDasharray="1 1"
                />
              )}
              {points.map(([x, y], index) => (
                <circle
                  key={index}
                  cx={x * 100}
                  cy={y * 56.25}
                  r="0.8"
                  fill="#38bdf8"
                />
              ))}
            </>
          )}
        </svg>

        {mode && (
          <div className="mt-4 space-y-3 rounded-lg border border-canvas-border bg-canvas-overlay p-4">
            <p className="text-xs text-slate-400">
              {mode === "zone"
                ? `Click to add points. ${points.length} placed — at least 3 needed.`
                : `Click two points across the doorway. ${points.length} of 2 placed.`}
            </p>

            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="label" htmlFor="shape-name">
                  Name
                </label>
                <input
                  id="shape-name"
                  className="input"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder={mode === "zone" ? "Warehouse" : "Front door"}
                />
              </div>

              {mode === "zone" && (
                <div>
                  <label className="label" htmlFor="shape-type">
                    Zone type
                  </label>
                  <select
                    id="shape-type"
                    className="input"
                    value={zoneType}
                    onChange={(event) => setZoneType(event.target.value as ZoneType)}
                  >
                    {ZONE_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type.toLowerCase()}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            {mode === "zone" && (
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={afterHours}
                  onChange={(event) => setAfterHours(event.target.checked)}
                />
                Alert on activity outside opening hours
              </label>
            )}

            {error && <p className="text-sm text-rose-300">{error}</p>}

            <div className="flex gap-2">
              <button
                type="button"
                className="btn-primary"
                disabled={!canSave || createZone.isPending || createLine.isPending}
                onClick={() => (mode === "zone" ? createZone.mutate() : createLine.mutate())}
              >
                Save {mode}
              </button>
              <button type="button" className="btn-ghost" onClick={reset}>
                Cancel
              </button>
              {points.length > 0 && (
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setPoints(points.slice(0, -1))}
                >
                  Undo point
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
