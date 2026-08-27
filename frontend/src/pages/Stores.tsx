/**
 * Stores and their opening hours.
 *
 * Opening hours are edited as a whole week rather than a day at a time. A
 * partial update is how a store ends up with a Tuesday nobody remembers
 * configuring, and after-hours detection then fires all Tuesday evening.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  Card,
  EmptyState,
  ErrorState,
  classNames,
} from "@/components/ui/primitives";
import { ApiError } from "@/services/client";
import { storeApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { OperatingHours, UUID } from "@/types/api";

const DAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

export function StoresPage() {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [showForm, setShowForm] = useState(false);
  const [editingHours, setEditingHours] = useState<UUID | null>(null);

  const stores = useQuery({
    queryKey: ["stores"],
    queryFn: () => storeApi.list({ limit: 100 }),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-100">Stores</h1>
          <p className="mt-1 text-sm text-slate-500">
            Each store has its own timezone and opening hours.
          </p>
        </div>
        {can("store:write") && (
          <button
            type="button"
            className="btn-primary"
            onClick={() => setShowForm((value) => !value)}
          >
            {showForm ? "Cancel" : "Add store"}
          </button>
        )}
      </div>

      {showForm && (
        <AddStoreForm
          onDone={() => {
            setShowForm(false);
            void queryClient.invalidateQueries({ queryKey: ["stores"] });
          }}
        />
      )}

      {stores.isError ? (
        <ErrorState
          message={(stores.error as Error).message}
          onRetry={() => void stores.refetch()}
        />
      ) : stores.isLoading ? (
        <div className="h-32 animate-pulse rounded-xl bg-canvas-raised" />
      ) : stores.data && stores.data.items.length > 0 ? (
        <div className="space-y-4">
          {stores.data.items.map((store) => (
            <Card key={store.id}>
              <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div>
                  <p className="font-medium text-slate-100">
                    {store.name}{" "}
                    <span className="ml-1 font-mono text-xs text-slate-500">
                      {store.code}
                    </span>
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {[store.city, store.timezone].filter(Boolean).join(" · ")}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {!store.is_active && (
                    <span className="badge bg-slate-500/15 text-slate-400">Inactive</span>
                  )}
                  {can("store:write") && (
                    <button
                      type="button"
                      className="btn-secondary text-xs"
                      onClick={() =>
                        setEditingHours(editingHours === store.id ? null : store.id)
                      }
                    >
                      {editingHours === store.id ? "Close" : "Opening hours"}
                    </button>
                  )}
                </div>
              </div>

              {editingHours === store.id && <OperatingHoursEditor storeId={store.id} />}
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No stores yet"
          description="A store is the location your cameras belong to."
        />
      )}
    </div>
  );
}

function OperatingHoursEditor({ storeId }: { storeId: UUID }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<OperatingHours[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const hours = useQuery({
    queryKey: ["stores", storeId, "hours"],
    queryFn: () => storeApi.operatingHours(storeId),
  });

  const save = useMutation({
    mutationFn: (days: OperatingHours[]) => storeApi.setOperatingHours(storeId, days),
    onSuccess: () => {
      setDraft(null);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["stores", storeId, "hours"] });
    },
    onError: (caught) => {
      setError(caught instanceof ApiError ? caught.message : "Could not save.");
    },
  });

  const current =
    draft ??
    hours.data ??
    DAY_NAMES.map((_, index) => ({
      day_of_week: index,
      opens_at: "08:00:00",
      closes_at: "22:00:00",
      is_closed: false,
    }));

  const update = (index: number, patch: Partial<OperatingHours>) => {
    setDraft(
      current.map((day, position) =>
        position === index ? { ...day, ...patch } : day,
      ),
    );
  };

  return (
    <div className="border-t border-canvas-border px-5 py-4">
      <p className="mb-3 text-xs text-slate-500">
        Times are local to this store. A closing time earlier than the opening
        time means an overnight shift (for example 20:00–04:00).
      </p>

      <div className="space-y-2">
        {current.map((day, index) => (
          <div key={day.day_of_week} className="flex flex-wrap items-center gap-3">
            <span className="w-24 text-sm text-slate-300">
              {DAY_NAMES[day.day_of_week]}
            </span>
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={!day.is_closed}
                onChange={(event) => update(index, { is_closed: !event.target.checked })}
              />
              Open
            </label>
            <input
              type="time"
              className={classNames("input w-32", day.is_closed && "opacity-40")}
              value={(day.opens_at ?? "08:00:00").slice(0, 5)}
              disabled={day.is_closed}
              onChange={(event) => update(index, { opens_at: `${event.target.value}:00` })}
            />
            <span className="text-slate-600">to</span>
            <input
              type="time"
              className={classNames("input w-32", day.is_closed && "opacity-40")}
              value={(day.closes_at ?? "22:00:00").slice(0, 5)}
              disabled={day.is_closed}
              onChange={(event) => update(index, { closes_at: `${event.target.value}:00` })}
            />
          </div>
        ))}
      </div>

      {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}

      <div className="mt-4 flex gap-2">
        <button
          type="button"
          className="btn-primary"
          onClick={() => save.mutate(current)}
          disabled={save.isPending}
        >
          {save.isPending ? "Saving…" : "Save week"}
        </button>
        {draft && (
          <button type="button" className="btn-ghost" onClick={() => setDraft(null)}>
            Discard changes
          </button>
        )}
      </div>
    </div>
  );
}

function AddStoreForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [city, setCity] = useState("");
  const [timezone, setTimezone] = useState("Africa/Dar_es_Salaam");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => storeApi.create({ name, code, city: city || undefined, timezone }),
    onSuccess: onDone,
    onError: (caught) => {
      setError(caught instanceof ApiError ? caught.message : "Could not create the store.");
    },
  });

  return (
    <Card title="Add a store">
      <form
        className="grid gap-4 p-5 md:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <div>
          <label className="label" htmlFor="store-name">
            Name
          </label>
          <input
            id="store-name"
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
          />
        </div>
        <div>
          <label className="label" htmlFor="store-code">
            Code
          </label>
          <input
            id="store-code"
            className="input"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            placeholder="DSM-01"
            required
          />
          <p className="mt-1 text-xs text-slate-500">
            A short reference used in alerts and reports.
          </p>
        </div>
        <div>
          <label className="label" htmlFor="store-city">
            City
          </label>
          <input
            id="store-city"
            className="input"
            value={city}
            onChange={(event) => setCity(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="store-timezone">
            Timezone
          </label>
          <input
            id="store-timezone"
            className="input"
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
            placeholder="Africa/Dar_es_Salaam"
          />
          <p className="mt-1 text-xs text-slate-500">
            Every opening-hours comparison and report uses this, not the server's.
          </p>
        </div>

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
            {create.isPending ? "Creating…" : "Create store"}
          </button>
        </div>
      </form>
    </Card>
  );
}
