/**
 * Camera management.
 *
 * The credential field is write-only in both directions: the form sends a
 * password, and nothing in any response can return one. `has_credentials` is
 * what the UI shows instead, so an installer can tell a camera is configured
 * without the page ever holding the secret.
 *
 * "Test connection" reads an actual frame rather than just opening a socket. A
 * misconfigured stream path opens fine and then delivers nothing, which is the
 * single most common setup mistake with a new NVR.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import {
  CameraStatusPill,
  Card,
  EmptyState,
  ErrorState,
  RelativeTime,
  classNames,
} from "@/components/ui/primitives";
import { useRealtimeEvents } from "@/hooks/useRealtime";
import { ApiError } from "@/services/client";
import { cameraApi, storeApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { CameraSummary, ConnectionTestResult, SourceKind, UUID } from "@/types/api";

export function CamerasPage() {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [showForm, setShowForm] = useState(false);
  const [storeFilter, setStoreFilter] = useState<UUID | "">("");

  const stores = useQuery({ queryKey: ["stores"], queryFn: () => storeApi.list({ limit: 100 }) });
  const cameras = useQuery({
    queryKey: ["cameras", { store_id: storeFilter }],
    queryFn: () => cameraApi.list({ store_id: storeFilter || undefined, limit: 200 }),
  });

  const onRealtime = useCallback(
    (message: { type: string }) => {
      if (message.type === "camera_status") {
        void queryClient.invalidateQueries({ queryKey: ["cameras"] });
      }
    },
    [queryClient],
  );
  useRealtimeEvents(onRealtime, [onRealtime]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-100">Cameras</h1>
          <p className="mt-1 text-sm text-slate-500">
            Connect existing CCTV cameras and NVRs over RTSP.
          </p>
        </div>
        {can("camera:write") && (
          <button
            type="button"
            className="btn-primary"
            onClick={() => setShowForm((value) => !value)}
          >
            {showForm ? "Cancel" : "Add camera"}
          </button>
        )}
      </div>

      {showForm && (
        <AddCameraForm
          stores={stores.data?.items ?? []}
          onDone={() => {
            setShowForm(false);
            void queryClient.invalidateQueries({ queryKey: ["cameras"] });
          }}
        />
      )}

      {stores.data && stores.data.items.length > 1 && (
        <Card>
          <div className="flex flex-wrap items-center gap-2 px-5 py-3">
            <span className="text-xs text-slate-500">Store:</span>
            <button
              type="button"
              onClick={() => setStoreFilter("")}
              className={classNames(
                "rounded-full px-3 py-1 text-xs font-medium",
                storeFilter === ""
                  ? "bg-brand-600/20 text-brand-200 ring-1 ring-inset ring-brand-500/40"
                  : "text-slate-400 hover:bg-canvas-overlay",
              )}
            >
              All
            </button>
            {stores.data.items.map((store) => (
              <button
                key={store.id}
                type="button"
                onClick={() => setStoreFilter(store.id)}
                className={classNames(
                  "rounded-full px-3 py-1 text-xs font-medium",
                  storeFilter === store.id
                    ? "bg-brand-600/20 text-brand-200 ring-1 ring-inset ring-brand-500/40"
                    : "text-slate-400 hover:bg-canvas-overlay",
                )}
              >
                {store.name}
              </button>
            ))}
          </div>
        </Card>
      )}

      {cameras.isError ? (
        <ErrorState
          message={(cameras.error as Error).message}
          onRetry={() => void cameras.refetch()}
        />
      ) : cameras.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((index) => (
            <div key={index} className="h-44 animate-pulse rounded-xl bg-canvas-raised" />
          ))}
        </div>
      ) : cameras.data && cameras.data.items.length > 0 ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {cameras.data.items.map((camera) => (
            <CameraCard key={camera.id} camera={camera} />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No cameras yet"
          description={
            stores.data?.items.length
              ? "Add a camera to start detecting."
              : "Create a store first, then add its cameras."
          }
          action={
            !stores.data?.items.length ? (
              <Link to="/stores" className="btn-primary">
                Create a store
              </Link>
            ) : undefined
          }
        />
      )}
    </div>
  );
}

function CameraCard({ camera }: { camera: CameraSummary }) {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["cameras"] });
  };

  const test = useMutation({
    mutationFn: () => cameraApi.test(camera.id),
    onSuccess: (result) => {
      setTestResult(result);
      invalidate();
    },
  });

  const pipeline = useQuery({
    queryKey: ["cameras", camera.id, "pipeline"],
    queryFn: () => cameraApi.pipelineStatus(camera.id),
    refetchInterval: 10_000,
  });

  const start = useMutation({
    mutationFn: () => cameraApi.startPipeline(camera.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cameras"] });
    },
  });

  const stop = useMutation({
    mutationFn: () => cameraApi.stopPipeline(camera.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cameras"] });
    },
  });

  const toggleEnabled = useMutation({
    mutationFn: () =>
      camera.is_enabled ? cameraApi.disable(camera.id) : cameraApi.enable(camera.id),
    onSuccess: invalidate,
  });

  const running = pipeline.data?.running ?? false;
  const stats = pipeline.data?.pipeline;

  return (
    <Card className="flex flex-col">
      <div className="flex items-start justify-between px-5 py-4">
        <div className="min-w-0">
          <p className="truncate font-medium text-slate-100">{camera.name}</p>
          {camera.location && (
            <p className="mt-0.5 truncate text-xs text-slate-500">{camera.location}</p>
          )}
        </div>
        <CameraStatusPill status={camera.status} />
      </div>

      <dl className="grid grid-cols-2 gap-y-2 border-t border-canvas-border px-5 py-3 text-xs">
        <dt className="text-slate-500">Last seen</dt>
        <dd className="text-right text-slate-300">
          {camera.last_seen_at ? <RelativeTime value={camera.last_seen_at} /> : "never"}
        </dd>
        <dt className="text-slate-500">Processing</dt>
        <dd className="text-right text-slate-300">
          {running && stats
            ? `${stats.processed_fps.toFixed(1)} fps`
            : running
              ? "starting…"
              : "stopped"}
        </dd>
        {running && stats && (
          <>
            <dt className="text-slate-500">Tracks seen</dt>
            <dd className="text-right tabular-nums text-slate-300">{stats.tracks_seen}</dd>
          </>
        )}
      </dl>

      {testResult && (
        <div
          className={classNames(
            "mx-5 mb-3 rounded-lg px-3 py-2 text-xs",
            testResult.success
              ? "border border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
              : "border border-rose-500/30 bg-rose-500/10 text-rose-200",
          )}
        >
          {testResult.message}
          {testResult.resolution && (
            <span className="text-slate-400">
              {" "}
              ({testResult.resolution[0]}×{testResult.resolution[1]}
              {testResult.latency_ms ? `, ${Math.round(testResult.latency_ms)} ms` : ""})
            </span>
          )}
        </div>
      )}

      <div className="mt-auto flex flex-wrap gap-2 border-t border-canvas-border px-5 py-3">
        {can("camera:control") && (
          <>
            <button
              type="button"
              className="btn-secondary text-xs"
              onClick={() => test.mutate()}
              disabled={test.isPending}
            >
              {test.isPending ? "Testing…" : "Test"}
            </button>
            <button
              type="button"
              className={running ? "btn-secondary text-xs" : "btn-primary text-xs"}
              onClick={() => (running ? stop.mutate() : start.mutate())}
              disabled={start.isPending || stop.isPending || !camera.is_enabled}
            >
              {start.isPending || stop.isPending
                ? "Working…"
                : running
                  ? "Stop detection"
                  : "Start detection"}
            </button>
          </>
        )}
        {can("camera:write") && (
          <button
            type="button"
            className="btn-ghost text-xs"
            onClick={() => toggleEnabled.mutate()}
            disabled={toggleEnabled.isPending}
          >
            {camera.is_enabled ? "Disable" : "Enable"}
          </button>
        )}
        {can("zone:read") && (
          <Link to={`/zones?camera=${camera.id}`} className="btn-ghost text-xs">
            Zones
          </Link>
        )}
      </div>

      {(start.isError || stop.isError) && (
        <p className="px-5 pb-3 text-xs text-rose-300">
          {((start.error ?? stop.error) as ApiError).message}
        </p>
      )}
    </Card>
  );
}

function AddCameraForm({
  stores,
  onDone,
}: {
  stores: { id: UUID; name: string }[];
  onDone: () => void;
}) {
  const [storeId, setStoreId] = useState<UUID | "">(stores[0]?.id ?? "");
  const [name, setName] = useState("");
  const [sourceKind, setSourceKind] = useState<SourceKind>("RTSP");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(554);
  const [streamPath, setStreamPath] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const create = useMutation({
    mutationFn: () =>
      cameraApi.create({
        store_id: storeId,
        name,
        source_kind: sourceKind,
        ...(sourceKind === "RTSP"
          ? {
              host,
              port,
              stream_path: streamPath || undefined,
              username: username || undefined,
              password: password || undefined,
            }
          : {}),
      }),
    onSuccess: () => {
      // Clear the password from component state the moment it is no longer
      // needed, so it does not linger in a React devtools inspection.
      setPassword("");
      onDone();
    },
    onError: (caught) => {
      if (caught instanceof ApiError) {
        setError(caught.message);
        setFieldErrors(caught.fieldErrors);
      } else {
        setError("Could not add the camera.");
      }
    },
  });

  return (
    <Card title="Add a camera">
      <form
        className="grid gap-4 p-5 md:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setFieldErrors({});
          create.mutate();
        }}
      >
        <div>
          <label className="label" htmlFor="camera-store">
            Store
          </label>
          <select
            id="camera-store"
            className="input"
            value={storeId}
            onChange={(event) => setStoreId(event.target.value)}
            required
          >
            {stores.map((store) => (
              <option key={store.id} value={store.id}>
                {store.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="label" htmlFor="camera-name">
            Name
          </label>
          <input
            id="camera-name"
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Warehouse door"
            required
          />
        </div>

        <div>
          <label className="label" htmlFor="camera-kind">
            Source
          </label>
          <select
            id="camera-kind"
            className="input"
            value={sourceKind}
            onChange={(event) => setSourceKind(event.target.value as SourceKind)}
          >
            <option value="RTSP">RTSP camera or NVR</option>
            <option value="SYNTHETIC">Self-test (no hardware)</option>
          </select>
          {sourceKind === "SYNTHETIC" && (
            <p className="mt-1 text-xs text-slate-500">
              Generates a known moving object so you can verify zones, rules and
              alerts end to end before mounting a real camera. Its events are
              marked as synthetic.
            </p>
          )}
        </div>

        {sourceKind === "RTSP" && (
          <>
            <div>
              <label className="label" htmlFor="camera-host">
                Host or IP
              </label>
              <input
                id="camera-host"
                className="input"
                value={host}
                onChange={(event) => setHost(event.target.value)}
                placeholder="192.168.1.64"
                required
              />
              {fieldErrors.host && (
                <p className="mt-1 text-xs text-rose-300">{fieldErrors.host}</p>
              )}
              <p className="mt-1 text-xs text-slate-500">
                Address only — not a full rtsp:// URL.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="camera-port">
                Port
              </label>
              <input
                id="camera-port"
                type="number"
                className="input"
                value={port}
                onChange={(event) => setPort(Number(event.target.value))}
              />
            </div>

            <div>
              <label className="label" htmlFor="camera-path">
                Stream path
              </label>
              <input
                id="camera-path"
                className="input"
                value={streamPath}
                onChange={(event) => setStreamPath(event.target.value)}
                placeholder="/Streaming/Channels/102"
              />
              <p className="mt-1 text-xs text-slate-500">
                Prefer the sub stream — it is far cheaper to decode.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="camera-username">
                Username
              </label>
              <input
                id="camera-username"
                className="input"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="off"
              />
            </div>

            <div>
              <label className="label" htmlFor="camera-password">
                Password
              </label>
              <input
                id="camera-password"
                type="password"
                className="input"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
              />
              <p className="mt-1 text-xs text-slate-500">
                Encrypted at rest. It is never shown again, here or anywhere else.
              </p>
            </div>
          </>
        )}

        {error && (
          <div
            role="alert"
            className="md:col-span-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200"
          >
            {error}
          </div>
        )}

        <div className="md:col-span-2">
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {create.isPending ? "Adding…" : "Add camera"}
          </button>
        </div>
      </form>
    </Card>
  );
}
