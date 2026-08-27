/**
 * Shelf changes matched against till sales.
 *
 * This page shows the output of the most easily misread part of VisionGuard,
 * so its whole job is to stop one word being read as a different word.
 *
 * `UNMATCHED` means **no sale was found in the window**. It does not mean
 * anything was taken. In a real shop the ordinary causes -- restocking, staff
 * moving stock, an item put back in another aisle, a mis-scanned item, a
 * detection error -- are far more common than the rest. So an unmatched row
 * never gets a red "alert" treatment, never appears without its explanation,
 * and is labelled "Needs checking" rather than anything stronger.
 *
 * `UNCERTAIN` is shown as prominently as the rest rather than filtered away,
 * because "we could not see the till data for this period" is exactly what a
 * manager needs to know before drawing any conclusion at all.
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
} from "@/components/ui/primitives";
import { ApiError } from "@/services/client";
import { posApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";
import type { Correlation, CorrelationStatus, ReviewOutcome, UUID } from "@/types/api";

const STATUS_STYLES: Record<
  CorrelationStatus,
  { label: string; chip: string; hint: string }
> = {
  MATCHED: {
    label: "Explained by a sale",
    chip: "bg-emerald-500/10 text-emerald-300",
    hint: "A till transaction accounts for this change.",
  },
  // Deliberately amber, not red, and worded as a task rather than a verdict.
  UNMATCHED: {
    label: "Needs checking",
    chip: "bg-amber-500/10 text-amber-300",
    hint: "No sale was found nearby. There are many ordinary reasons for that.",
  },
  UNCERTAIN: {
    label: "Cannot say",
    chip: "bg-slate-600/30 text-slate-300",
    hint: "The till data for this period was incomplete, so no conclusion is possible.",
  },
};

const REVIEW_OPTIONS: { value: ReviewOutcome; label: string; help: string }[] = [
  { value: "CORRECT", label: "Got it right", help: "The match, or the lack of one, was correct." },
  { value: "INCORRECT", label: "Got it wrong", help: "The system matched the wrong thing." },
  {
    value: "EXPLAINED",
    label: "I know the reason",
    help: "Unmatched, but there is an ordinary explanation.",
  },
  { value: "UNCLEAR", label: "Cannot tell", help: "Not enough to judge either way." },
];

export function ReconciliationPage() {
  const queryClient = useQueryClient();
  const can = useAuth((state) => state.can);
  const [filter, setFilter] = useState<CorrelationStatus | "ALL">("ALL");

  const accuracy = useQuery({
    queryKey: ["pos", "accuracy"],
    queryFn: () => posApi.accuracy(),
  });

  const correlations = useQuery({
    queryKey: ["pos", "correlations", filter],
    queryFn: () =>
      posApi.correlations(filter === "ALL" ? { limit: 100 } : { status: filter, limit: 100 }),
  });

  const run = useMutation({
    mutationFn: () => posApi.correlate({ limit: 200 }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["pos"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h1 className="text-xl font-semibold tracking-tight text-slate-100">
            Stock reconciliation
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Shelf changes matched against till sales. A change with no matching
            sale is something to look into, not a conclusion &mdash; restocking,
            staff moving stock and detection errors all look the same to a
            camera.
          </p>
        </div>
        {can("pos:read") && (
          <button
            type="button"
            className="btn-primary"
            disabled={run.isPending}
            onClick={() => run.mutate()}
          >
            {run.isPending ? "Checking..." : "Check for new changes"}
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Explained by a sale"
          value={accuracy.data ? accuracy.data.matched : "--"}
          tone="good"
          loading={accuracy.isLoading}
        />
        <StatTile
          label="Needs checking"
          value={accuracy.data ? accuracy.data.unmatched : "--"}
          tone={accuracy.data && accuracy.data.unmatched > 0 ? "warn" : "neutral"}
          loading={accuracy.isLoading}
        />
        <StatTile
          label="Cannot say"
          value={accuracy.data ? accuracy.data.uncertain : "--"}
          hint="Till data incomplete"
          loading={accuracy.isLoading}
        />
        <StatTile
          label="Correct when reviewed"
          // Null until a human has judged something. An unmeasured system must
          // not display "0%" -- that would read as "this never works".
          value={
            accuracy.data?.accuracy === null || accuracy.data === undefined
              ? "Not yet rated"
              : `${Math.round(accuracy.data.accuracy * 100)}%`
          }
          hint={
            accuracy.data ? `${accuracy.data.reviewed} reviewed by a person` : undefined
          }
          loading={accuracy.isLoading}
        />
      </div>

      <div className="flex flex-wrap gap-2">
        {(["ALL", "UNMATCHED", "UNCERTAIN", "MATCHED"] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setFilter(value)}
            className={classNames(
              "rounded-full px-3 py-1 text-xs font-medium transition",
              filter === value
                ? "bg-slate-700 text-slate-100"
                : "bg-canvas-raised text-slate-400 hover:text-slate-200",
            )}
          >
            {value === "ALL" ? "All" : STATUS_STYLES[value].label}
          </button>
        ))}
      </div>

      {correlations.isError ? (
        <ErrorState
          message={(correlations.error as Error).message}
          onRetry={() => void correlations.refetch()}
        />
      ) : correlations.isLoading ? (
        <LoadingRows rows={5} columns={3} />
      ) : (correlations.data?.items ?? []).length === 0 ? (
        <EmptyState
          title="Nothing to reconcile"
          description="Shelf changes are matched against sales once both a camera and a till are connected."
        />
      ) : (
        <div className="space-y-3">
          {(correlations.data?.items ?? []).map((row) => (
            <CorrelationCard
              key={row.id}
              correlation={row}
              canReview={can("pos:write")}
              onReviewed={() => void queryClient.invalidateQueries({ queryKey: ["pos"] })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function CorrelationCard({
  correlation,
  canReview,
  onReviewed,
}: {
  correlation: Correlation;
  canReview: boolean;
  onReviewed: () => void;
}) {
  const style = STATUS_STYLES[correlation.status];
  const [open, setOpen] = useState(false);

  return (
    <Card>
      <div className="space-y-3 px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <span
              className={classNames(
                "inline-block rounded px-2 py-0.5 text-2xs font-semibold uppercase tracking-wider",
                style.chip,
              )}
            >
              {style.label}
            </span>
            {/* The explanation is not optional detail. The status word on its
                own is what gets misread. */}
            <p className="mt-2 text-sm text-slate-300">{correlation.explanation}</p>
            <p className="mt-1 text-xs text-slate-500">
              <RelativeTime value={correlation.correlated_at} />
              {" · "}
              searched {correlation.window_seconds}s
              {" · "}
              {correlation.candidates_considered} sale(s) considered
            </p>
          </div>
          {correlation.reviewed_at ? (
            <span className="text-2xs uppercase tracking-wider text-slate-500">
              Reviewed: {correlation.review_outcome}
            </span>
          ) : (
            canReview && (
              <button
                type="button"
                className="text-xs text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
                onClick={() => setOpen((value) => !value)}
              >
                {open ? "Cancel" : "Record what happened"}
              </button>
            )
          )}
        </div>

        {Object.keys(correlation.score_breakdown).length > 0 && (
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer hover:text-slate-300">
              How this was scored
            </summary>
            <ul className="mt-2 space-y-1 pl-4">
              {Object.entries(correlation.score_breakdown).map(([signal, value]) => (
                <li key={signal} className="flex justify-between gap-4 tabular-nums">
                  <span className="capitalize">{signal}</span>
                  <span>{Math.round(value * 100)}%</span>
                </li>
              ))}
              <li className="flex justify-between gap-4 border-t border-slate-800 pt-1 tabular-nums text-slate-400">
                <span>Overall</span>
                <span>{Math.round(correlation.score * 100)}%</span>
              </li>
            </ul>
          </details>
        )}

        {open && (
          <ReviewForm
            correlationId={correlation.id}
            onDone={() => {
              setOpen(false);
              onReviewed();
            }}
          />
        )}
      </div>
    </Card>
  );
}

function ReviewForm({
  correlationId,
  onDone,
}: {
  correlationId: UUID;
  onDone: () => void;
}) {
  const [outcome, setOutcome] = useState<ReviewOutcome>("CORRECT");
  const [notes, setNotes] = useState("");

  const review = useMutation({
    mutationFn: () => posApi.review(correlationId, { outcome, notes: notes || undefined }),
    onSuccess: onDone,
  });

  return (
    <form
      className="space-y-3 rounded-lg border border-slate-800 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        review.mutate();
      }}
    >
      <p className="text-xs text-slate-500">
        This records whether <em>the system</em> got the match right. It is not a
        judgement about a person.
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {REVIEW_OPTIONS.map((option) => (
          <label
            key={option.value}
            className={classNames(
              "flex cursor-pointer gap-2 rounded border p-2 text-sm transition",
              outcome === option.value
                ? "border-sky-500/50 bg-sky-500/5 text-slate-100"
                : "border-slate-800 text-slate-400 hover:border-slate-700",
            )}
          >
            <input
              type="radio"
              name={`review-${correlationId}`}
              className="mt-1"
              checked={outcome === option.value}
              onChange={() => setOutcome(option.value)}
            />
            <span>
              {option.label}
              <span className="block text-2xs text-slate-500">{option.help}</span>
            </span>
          </label>
        ))}
      </div>
      <textarea
        className="input min-h-16 w-full"
        placeholder="What actually happened? (optional)"
        value={notes}
        onChange={(event) => setNotes(event.target.value)}
      />
      {review.isError && (
        <p className="text-sm text-rose-300">
          {review.error instanceof ApiError
            ? review.error.message
            : "Could not save the review."}
        </p>
      )}
      <button type="submit" className="btn-primary" disabled={review.isPending}>
        {review.isPending ? "Saving..." : "Save"}
      </button>
    </form>
  );
}
