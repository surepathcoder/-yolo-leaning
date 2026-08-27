/**
 * System diagnostics.
 *
 * The question this page exists to answer at a glance is "is the system
 * actually working". There are two ways it silently is not: inference has
 * fallen back to CPU when a GPU was expected, and the realtime relay is
 * disconnected so dashboards look quiet rather than broken.
 */

import { useQuery } from "@tanstack/react-query";

import { Card, StatTile, classNames } from "@/components/ui/primitives";
import { systemApi } from "@/services/endpoints";

interface CameraMetrics {
  camera_id: string;
  camera_name?: string;
  state: string;
  events_ingested?: number;
  pipeline?: { processed_fps?: number };
  detector?: { mean_inference_ms?: number; p95_inference_ms?: number };
  reader?: { frames_dropped?: number; frames_skipped?: number };
}

export function SystemPage() {
  const device = useQuery({
    queryKey: ["system", "device"],
    queryFn: () => systemApi.visionDevice(),
    refetchInterval: 30_000,
  });

  const metrics = useQuery({
    queryKey: ["system", "metrics"],
    queryFn: () => systemApi.visionMetrics(),
    refetchInterval: 5_000,
  });

  const plan = useQuery({ queryKey: ["system", "plan"], queryFn: () => systemApi.plan() });

  const cameras = (metrics.data?.cameras ?? []) as CameraMetrics[];
  const realtime = metrics.data?.realtime as Record<string, unknown> | undefined;
  const subscriber = realtime?.subscriber as
    | { connected?: boolean; messages_relayed?: number }
    | undefined;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">System</h1>
        <p className="mt-1 text-sm text-slate-500">
          Vision performance, realtime delivery and plan usage.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Inference device"
          value={device.data?.resolved_device?.toUpperCase() ?? "—"}
          hint={
            device.data && device.data.configured_device !== device.data.resolved_device
              ? `configured as "${device.data.configured_device}"`
              : "as configured"
          }
          tone={device.data?.resolved_device === "cpu" ? "warn" : "good"}
          loading={device.isLoading}
        />
        <StatTile
          label="Pipelines running"
          value={(metrics.data?.pipelines_running as number) ?? 0}
          loading={metrics.isLoading}
        />
        <StatTile
          label="Live connections"
          value={(realtime?.connections as number) ?? 0}
          hint="dashboards receiving updates"
          loading={metrics.isLoading}
        />
        <StatTile
          label="Realtime relay"
          value={subscriber?.connected ? "Connected" : "Down"}
          hint={
            subscriber?.connected
              ? `${subscriber.messages_relayed ?? 0} relayed`
              : "dashboards will look quiet"
          }
          tone={subscriber?.connected ? "good" : "bad"}
          loading={metrics.isLoading}
        />
      </div>

      {device.data && !device.data.model_present && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-5 py-3.5 text-sm text-rose-200">
          <span className="font-medium">The detection model file is missing.</span> No
          camera can start until the weights are available at{" "}
          <code className="font-mono text-xs">{device.data.model_path}</code>.
        </div>
      )}

      {device.data?.resolved_device === "cpu" && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-5 py-3.5 text-sm text-amber-200">
          Inference is running on the CPU. That is fine for a handful of cameras at a
          low frame rate; on a busy site it is roughly twenty times slower than a GPU
          and shows up as dropped frames.
        </div>
      )}

      <Card title="Per-camera performance">
        {cameras.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-slate-500">
            No pipelines are running in this process.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Camera</th>
                  <th>State</th>
                  <th>Processed FPS</th>
                  <th>Inference (mean)</th>
                  <th>Inference (p95)</th>
                  <th>Frames dropped</th>
                  <th>Events</th>
                </tr>
              </thead>
              <tbody>
                {cameras.map((camera) => {
                  const dropped = camera.reader?.frames_dropped ?? 0;
                  return (
                    <tr key={camera.camera_id}>
                      <td className="text-slate-200">
                        {camera.camera_name || camera.camera_id}
                      </td>
                      <td className="text-xs">{camera.state}</td>
                      <td className="tabular-nums">
                        {(camera.pipeline?.processed_fps ?? 0).toFixed(1)}
                      </td>
                      <td className="tabular-nums">
                        {(camera.detector?.mean_inference_ms ?? 0).toFixed(1)} ms
                      </td>
                      <td className="tabular-nums">
                        {(camera.detector?.p95_inference_ms ?? 0).toFixed(1)} ms
                      </td>
                      <td className={classNames("tabular-nums", dropped > 0 && "text-amber-300")}>
                        {dropped}
                      </td>
                      <td className="tabular-nums">{camera.events_ingested ?? 0}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="px-5 py-3 text-xs text-slate-500">
              Dropped frames mean the box cannot keep up with the configured frame
              rate. Skipped frames are normal: the reader deliberately discards
              surplus frames on a live camera to stay current.
            </p>
          </div>
        )}
      </Card>

      <Card title="Plan usage">
        {plan.data ? (
          <dl className="grid gap-x-8 gap-y-3 px-5 py-4 text-sm md:grid-cols-2">
            <div className="flex justify-between">
              <dt className="text-slate-500">Tier</dt>
              <dd className="text-slate-200">{plan.data.tier}</dd>
            </div>
            {Object.entries(plan.data.limits).map(([key, value]) => (
              <div key={key} className="flex justify-between">
                <dt className="text-slate-500">{key.replace(/_/g, " ")}</dt>
                <dd className="text-slate-200">
                  {value.current !== undefined ? `${value.current} / ` : ""}
                  {value.limit === null
                    ? "unlimited"
                    : typeof value.limit === "boolean"
                      ? value.limit
                        ? "included"
                        : "not included"
                      : value.limit}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="h-24 animate-pulse" />
        )}
      </Card>
    </div>
  );
}
