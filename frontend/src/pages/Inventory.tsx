/**
 * Shelf stock levels and the product catalogue.
 *
 * The one rule this page exists to honour: **UNKNOWN is not zero.** A shelf
 * the camera could not see, or has never read, must never be drawn as an
 * empty bar next to shelves that genuinely are empty. An operator who walks
 * to an aisle because the dashboard said "empty" and finds it full stops
 * trusting the dashboard, and after that the real stockouts go unread too.
 *
 * So unknown shelves get their own visual treatment, their own count in the
 * summary, and an explicit reason where one is known ("view blocked", "never
 * read"). Showing a smaller, honest number beats showing a complete-looking
 * one that is partly invented.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  Card,
  EmptyState,
  ErrorState,
  LoadingRows,
  RelativeTime,
  StatTile,
  classNames,
  humaniseEventType,
} from "@/components/ui/primitives";
import { ApiError } from "@/services/client";
import { inventoryApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { OccupancyLevel, ShelfStatus, UUID } from "@/types/api";

const LEVEL_STYLES: Record<
  OccupancyLevel,
  { label: string; bar: string; text: string; track: string }
> = {
  FULL: {
    label: "Full",
    bar: "bg-emerald-400",
    text: "text-emerald-300",
    track: "bg-emerald-500/15",
  },
  NORMAL: {
    label: "Stocked",
    bar: "bg-sky-400",
    text: "text-sky-300",
    track: "bg-sky-500/15",
  },
  LOW: {
    label: "Low",
    bar: "bg-amber-400",
    text: "text-amber-300",
    track: "bg-amber-500/15",
  },
  EMPTY: {
    label: "Empty",
    bar: "bg-rose-400",
    text: "text-rose-300",
    track: "bg-rose-500/15",
  },
  // Deliberately colourless and striped: an unknown shelf must not read as a
  // measurement at a glance.
  UNKNOWN: {
    label: "Not visible",
    bar: "bg-slate-600",
    text: "text-slate-400",
    track: "bg-slate-700/40",
  },
};

function percent(ratio: number | null): string {
  return ratio === null ? "--" : `${Math.round(ratio * 100)}%`;
}

/** Why we cannot report a level, when we know why. */
function unknownReason(shelf: ShelfStatus): string {
  if (!shelf.monitoring_enabled) return "Monitoring turned off";
  if (shelf.is_occluded) return "View blocked";
  if (!shelf.camera_id) return "No camera assigned";
  if (!shelf.last_reading_at) return "Not read yet";
  return "No usable reading";
}

export function InventoryPage() {
  const can = useAuth((state) => state.can);
  const [tab, setTab] = useState<"shelves" | "activity" | "products">("shelves");

  const summary = useQuery({
    queryKey: ["inventory", "summary"],
    queryFn: () => inventoryApi.summary(),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Inventory</h1>
        <p className="mt-1 text-sm text-slate-500">
          Shelf occupancy measured from the camera. Levels are estimates of how
          much of a shelf is covered, not unit counts.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Shelves monitored"
          value={summary.data ? summary.data.shelves_monitored : "--"}
          hint={summary.data ? `${summary.data.shelves_total} configured` : undefined}
          loading={summary.isLoading}
        />
        <StatTile
          label="Low stock"
          value={summary.data ? summary.data.shelves_low : "--"}
          tone={summary.data && summary.data.shelves_low > 0 ? "warn" : "neutral"}
          loading={summary.isLoading}
        />
        <StatTile
          label="Empty"
          value={summary.data ? summary.data.shelves_empty : "--"}
          tone={summary.data && summary.data.shelves_empty > 0 ? "bad" : "neutral"}
          loading={summary.isLoading}
        />
        <StatTile
          label="Not visible"
          value={summary.data ? summary.data.shelves_unknown : "--"}
          hint="Blocked, offline or never read"
          loading={summary.isLoading}
        />
      </div>

      <div className="flex gap-1 border-b border-slate-800">
        {(
          [
            ["shelves", "Shelves"],
            ["activity", "Activity"],
            ["products", "Products"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={classNames(
              "px-4 py-2 text-sm font-medium transition",
              tab === key
                ? "border-b-2 border-sky-400 text-slate-100"
                : "text-slate-500 hover:text-slate-300",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "shelves" && <ShelfLevels canWrite={can("shelf:write")} />}
      {tab === "activity" && <ShelfActivity />}
      {tab === "products" && <ProductCatalogue canWrite={can("product:write")} />}
    </div>
  );
}

// ------------------------------------------------------------------- shelves
function ShelfLevels({ canWrite }: { canWrite: boolean }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["inventory", "status"],
    queryFn: () => inventoryApi.status(),
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: UUID; enabled: boolean }) =>
      inventoryApi.updateShelf(id, { monitoring_enabled: enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["inventory"] });
    },
  });

  if (status.isError) {
    return (
      <ErrorState
        message={(status.error as Error).message}
        onRetry={() => void status.refetch()}
      />
    );
  }
  if (status.isLoading) return <LoadingRows rows={4} columns={3} />;

  const shelves = status.data ?? [];
  if (shelves.length === 0) {
    return (
      <EmptyState
        title="No shelves configured"
        description="Draw a shelf on a camera to start measuring how full it is."
      />
    );
  }

  return (
    <div className="space-y-3">
      {shelves.map((shelf) => {
        const style = LEVEL_STYLES[shelf.occupancy_level];
        const isUnknown = shelf.occupancy_level === "UNKNOWN";
        return (
          <Card key={shelf.shelf_id}>
            <div className="space-y-3 px-5 py-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <p className="font-medium text-slate-100">
                    {shelf.name}{" "}
                    <span className="ml-1 font-mono text-xs text-slate-500">
                      {shelf.code}
                    </span>
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {isUnknown ? (
                      unknownReason(shelf)
                    ) : shelf.last_reading_at ? (
                      <>
                        Last seen <RelativeTime value={shelf.last_reading_at} />
                      </>
                    ) : (
                      "Not read yet"
                    )}
                  </p>
                </div>
                <div className="text-right">
                  <p className={classNames("text-2xl font-semibold tabular-nums", style.text)}>
                    {percent(shelf.occupancy_ratio)}
                  </p>
                  <p className={classNames("text-2xs font-semibold uppercase tracking-wider", style.text)}>
                    {style.label}
                  </p>
                </div>
              </div>

              <div className={classNames("h-2 w-full overflow-hidden rounded-full", style.track)}>
                {/* An unknown shelf gets no bar at all. A zero-width bar and a
                    genuinely empty shelf would look identical. */}
                {!isUnknown && (
                  <div
                    className={classNames("h-full rounded-full transition-all", style.bar)}
                    style={{ width: `${Math.max((shelf.occupancy_ratio ?? 0) * 100, 2)}%` }}
                  />
                )}
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
                <span>
                  {shelf.is_occluded && (
                    <span className="mr-2 rounded bg-slate-700/60 px-1.5 py-0.5 text-slate-300">
                      View blocked
                    </span>
                  )}
                  {shelf.confidence !== null && !isUnknown && (
                    <>Confidence {Math.round(shelf.confidence * 100)}%</>
                  )}
                </span>
                {canWrite && (
                  <button
                    type="button"
                    className="text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
                    disabled={toggle.isPending}
                    onClick={() =>
                      toggle.mutate({
                        id: shelf.shelf_id,
                        enabled: !shelf.monitoring_enabled,
                      })
                    }
                  >
                    {shelf.monitoring_enabled ? "Pause monitoring" : "Resume monitoring"}
                  </button>
                )}
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ activity
function ShelfActivity() {
  const events = useQuery({
    queryKey: ["inventory", "events"],
    queryFn: () => inventoryApi.events({ limit: 50 }),
  });
  const shelves = useQuery({
    queryKey: ["inventory", "shelves"],
    queryFn: () => inventoryApi.shelves(),
  });

  if (events.isError) {
    return (
      <ErrorState
        message={(events.error as Error).message}
        onRetry={() => void events.refetch()}
      />
    );
  }
  if (events.isLoading) return <LoadingRows rows={6} columns={4} />;

  const rows = events.data?.items ?? [];
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No shelf activity yet"
        description="Changes in shelf stock will appear here once a camera has been watching for a while."
      />
    );
  }

  const shelfName = (id: UUID) =>
    shelves.data?.find((shelf) => shelf.id === id)?.name ?? "Unknown shelf";

  return (
    <Card>
      <p className="border-b border-slate-800 px-5 py-3 text-xs text-slate-500">
        What changed on each shelf. These are observations, not conclusions:
        restocking, a customer and a staff member tidying a display all look
        the same to a camera.
      </p>
      <ul className="divide-y divide-slate-800">
        {rows.map((event) => {
          const delta =
            event.occupancy_before !== null && event.occupancy_after !== null
              ? event.occupancy_after - event.occupancy_before
              : null;
          return (
            <li key={event.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm text-slate-200">
                  {humaniseEventType(event.event_type)}
                  <span className="ml-2 text-slate-500">{shelfName(event.shelf_id)}</span>
                </p>
                <p className="mt-0.5 text-xs text-slate-500">
                  <RelativeTime value={event.occurred_at} />
                  {event.occupancy_before !== null && event.occupancy_after !== null && (
                    <>
                      {" · "}
                      {percent(event.occupancy_before)} → {percent(event.occupancy_after)}
                    </>
                  )}
                </p>
              </div>
              {delta !== null && (
                <span
                  className={classNames(
                    "font-mono text-sm tabular-nums",
                    delta < 0 ? "text-rose-300" : "text-emerald-300",
                  )}
                >
                  {delta > 0 ? "+" : ""}
                  {Math.round(delta * 100)}%
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

// ------------------------------------------------------------------ products
function ProductCatalogue({ canWrite }: { canWrite: boolean }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);

  const products = useQuery({
    queryKey: ["inventory", "products", search],
    queryFn: () => inventoryApi.products(search ? { search } : undefined),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by name or SKU"
          className="input max-w-xs"
        />
        {canWrite && (
          <button
            type="button"
            className="btn-primary"
            onClick={() => setShowForm((value) => !value)}
          >
            {showForm ? "Cancel" : "Add product"}
          </button>
        )}
      </div>

      {showForm && (
        <AddProductForm
          onDone={() => {
            setShowForm(false);
            void queryClient.invalidateQueries({ queryKey: ["inventory", "products"] });
          }}
        />
      )}

      {products.isError ? (
        <ErrorState
          message={(products.error as Error).message}
          onRetry={() => void products.refetch()}
        />
      ) : products.isLoading ? (
        <LoadingRows rows={5} columns={3} />
      ) : (products.data ?? []).length === 0 ? (
        <EmptyState
          title={search ? "No matching products" : "No products yet"}
          description={
            search
              ? undefined
              : "The catalogue is optional. Shelves report occupancy without it, they just cannot say what is on them."
          }
        />
      ) : (
        <Card>
          <ul className="divide-y divide-slate-800">
            {(products.data ?? []).map((product) => (
              <li
                key={product.id}
                className="flex flex-wrap items-center gap-3 px-5 py-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-slate-200">{product.name}</p>
                  <p className="mt-0.5 font-mono text-xs text-slate-500">
                    {product.sku}
                    {product.category && (
                      <span className="ml-2 font-sans">{product.category}</span>
                    )}
                  </p>
                </div>
                {product.detection_class_name ? (
                  <span className="rounded bg-sky-500/10 px-2 py-0.5 text-2xs font-semibold uppercase tracking-wider text-sky-300">
                    Detectable
                  </span>
                ) : (
                  <span
                    className="text-2xs text-slate-600"
                    title="No trained model can recognise this product yet"
                  >
                    Not detectable
                  </span>
                )}
                {product.unit_price && (
                  <span className="font-mono text-sm tabular-nums text-slate-400">
                    {product.currency} {product.unit_price}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function AddProductForm({ onDone }: { onDone: () => void }) {
  const [sku, setSku] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");

  const create = useMutation({
    mutationFn: () =>
      inventoryApi.createProduct({
        sku,
        name,
        category: category || null,
      }),
    onSuccess: onDone,
  });

  return (
    <Card title="Add a product">
      <form
        className="grid gap-3 px-5 py-4 sm:grid-cols-3"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <label className="space-y-1">
          <span className="text-xs text-slate-500">SKU</span>
          <input
            className="input"
            value={sku}
            onChange={(event) => setSku(event.target.value)}
            required
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-slate-500">Name</span>
          <input
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-slate-500">Category</span>
          <input
            className="input"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          />
        </label>

        {create.isError && (
          <p className="text-sm text-rose-300 sm:col-span-3">
            {create.error instanceof ApiError
              ? create.error.message
              : "Could not add the product."}
          </p>
        )}

        <div className="sm:col-span-3">
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {create.isPending ? "Adding..." : "Add product"}
          </button>
        </div>
      </form>
    </Card>
  );
}
