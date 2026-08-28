/**
 * Custom product-detection models (milestone 15).
 *
 * Models are trained with `python -m training.train` and registered by the
 * training script or an upload. This page is where an operator reviews the
 * validation metrics, activates one version per model name (subject to the
 * mAP gate), and points cameras at it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  Card,
  EmptyState,
  ErrorState,
  LoadingRows,
  RelativeTime,
  classNames,
} from "@/components/ui/primitives";
import { ApiError } from "@/services/client";
import { cameraApi, modelApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { ModelStatus, ModelVersion } from "@/types/api";

const STATUS_STYLES: Record<ModelStatus, string> = {
  DRAFT: "bg-slate-700/40 text-slate-300",
  TRAINING: "bg-amber-500/20 text-amber-200",
  VALIDATED: "bg-sky-500/20 text-sky-200",
  ACTIVE: "bg-emerald-500/20 text-emerald-200 ring-1 ring-inset ring-emerald-500/40",
  ARCHIVED: "bg-slate-700/40 text-slate-400",
  FAILED: "bg-rose-500/20 text-rose-200",
};

export function ModelsPage() {
  const can = useAuth((state) => state.can);
  const queryClient = useQueryClient();

  const models = useQuery({ queryKey: ["models"], queryFn: () => modelApi.list() });
  const cameras = useQuery({
    queryKey: ["cameras", "all"],
    queryFn: () => cameraApi.list({ limit: 200 }),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["models"] });
    void queryClient.invalidateQueries({ queryKey: ["cameras"] });
  };

  const activate = useMutation({
    mutationFn: (id: string) => modelApi.activate(id),
    onSuccess: refresh,
  });
  const archive = useMutation({
    mutationFn: (id: string) => modelApi.archive(id),
    onSuccess: refresh,
  });
  const assign = useMutation({
    mutationFn: async ({
      cameraId,
      modelId,
      currentModelId,
    }: {
      cameraId: string;
      modelId: string | null;
      currentModelId: string | null;
    }) => {
      if (modelId) {
        await modelApi.assignCamera(modelId, cameraId);
      } else if (currentModelId) {
        await modelApi.unassignCamera(currentModelId, cameraId);
      }
    },
    onSuccess: refresh,
  });

  const activeModels = (models.data ?? []).filter((m) => m.status === "ACTIVE");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">
          Detection models
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Custom models that recognise your own products by name. Trained offline,
          reviewed here, then assigned to a camera.
        </p>
      </div>

      <Card title="Model versions">
        {models.isError ? (
          <ErrorState
            message={(models.error as Error).message}
            onRetry={() => void models.refetch()}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Status</th>
                  <th>mAP50</th>
                  <th>Classes</th>
                  <th>Trained</th>
                  <th />
                </tr>
              </thead>
              {models.isLoading ? (
                <LoadingRows rows={4} columns={6} />
              ) : (
                <tbody>
                  {(models.data ?? []).map((model) => (
                    <ModelRow
                      key={model.id}
                      model={model}
                      canActivate={can("model:activate")}
                      onActivate={() => activate.mutate(model.id)}
                      onArchive={() => archive.mutate(model.id)}
                      busy={activate.isPending || archive.isPending}
                    />
                  ))}
                </tbody>
              )}
            </table>
            {models.data && models.data.length === 0 && (
              <EmptyState
                title="No models yet"
                description="Train one with `python -m training.train`, then register it."
              />
            )}
            {(activate.error || archive.error) && (
              <p className="border-t border-canvas-border px-5 py-2 text-xs text-rose-300">
                {((activate.error || archive.error) as ApiError).message}
              </p>
            )}
          </div>
        )}
      </Card>

      {can("camera:write") && (
        <Card title="Camera assignments">
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Camera</th>
                  <th>Model</th>
                </tr>
              </thead>
              <tbody>
                {(cameras.data?.items ?? []).map((camera) => (
                  <tr key={camera.id}>
                    <td className="text-slate-200">{camera.name}</td>
                    <td>
                      <select
                        className="input max-w-xs"
                        value={camera.model_version_id ?? ""}
                        disabled={assign.isPending}
                        onChange={(event) =>
                          assign.mutate({
                            cameraId: camera.id,
                            modelId: event.target.value || null,
                            currentModelId: camera.model_version_id,
                          })
                        }
                      >
                        <option value="">None (default model)</option>
                        {activeModels.map((model) => (
                          <option key={model.id} value={model.id}>
                            {model.name}:{model.version}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {cameras.data && cameras.data.items.length === 0 && (
              <EmptyState title="No cameras" description="Add a camera first." />
            )}
          </div>
        </Card>
      )}
    </div>
  );
}

function ModelRow({
  model,
  canActivate,
  onActivate,
  onArchive,
  busy,
}: {
  model: ModelVersion;
  canActivate: boolean;
  onActivate: () => void;
  onArchive: () => void;
  busy: boolean;
}) {
  return (
    <tr>
      <td>
        <p className="text-slate-200">
          {model.name} <span className="text-slate-500">:{model.version}</span>
        </p>
        {model.dataset_name && (
          <p className="mt-0.5 text-xs text-slate-500">
            from {model.dataset_name}
            {model.dataset_size ? ` (${model.dataset_size} images)` : ""}
          </p>
        )}
      </td>
      <td>
        <span
          className={classNames(
            "rounded-full px-2 py-0.5 text-2xs font-medium",
            STATUS_STYLES[model.status],
          )}
        >
          {model.status}
        </span>
      </td>
      <td className="text-xs text-slate-300">
        {model.map50 === null ? "—" : model.map50.toFixed(3)}
      </td>
      <td className="text-xs text-slate-400">{model.class_names.length}</td>
      <td className="text-xs text-slate-400">
        {model.trained_at ? <RelativeTime value={model.trained_at} /> : "—"}
      </td>
      <td className="text-right">
        {canActivate && model.status !== "ACTIVE" && (
          <button
            type="button"
            className="btn-secondary text-xs"
            disabled={busy || !model.activatable}
            title={
              model.activatable
                ? undefined
                : "mAP50 is below the activation threshold"
            }
            onClick={onActivate}
          >
            Activate
          </button>
        )}
        {canActivate && model.status === "ACTIVE" && (
          <button
            type="button"
            className="btn-secondary text-xs"
            disabled={busy}
            onClick={onArchive}
          >
            Archive
          </button>
        )}
      </td>
    </tr>
  );
}
