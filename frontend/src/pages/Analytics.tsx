/**
 * Analytics.
 *
 * Every aggregate here is computed in PostgreSQL and fetched already summed.
 * Pulling raw events into the browser to group them works for a demo shop and
 * collapses in month two, when one store has produced two million rows.
 */

import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, EmptyState, StatTile, humaniseEventType } from "@/components/ui/primitives";
import { alertApi, eventApi } from "@/services/endpoints";

export function AnalyticsPage() {
  const counts = useQuery({
    queryKey: ["events", "counts"],
    queryFn: () => eventApi.countsByType(),
  });

  const summary = useQuery({
    queryKey: ["events", "summary"],
    queryFn: () => eventApi.summary(),
  });

  const falsePositives = useQuery({
    queryKey: ["alerts", "false-positive-rate"],
    queryFn: () => alertApi.falsePositiveRate(),
  });

  const data = Object.entries(counts.data ?? {})
    .filter(([, value]) => value > 0)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 12)
    .map(([type, value]) => ({
      type,
      label: humaniseEventType(type),
      value,
    }));

  const rate = falsePositives.data?.false_positive_rate;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-100">Analytics</h1>
        <p className="mt-1 text-sm text-slate-500">
          Aggregated in the database, not the browser.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Events (24 h)"
          value={summary.data?.events ?? 0}
          loading={summary.isLoading}
        />
        <StatTile
          label="Door crossings"
          value={summary.data?.line_crossings ?? 0}
          hint="entries and exits combined"
          loading={summary.isLoading}
        />
        <StatTile
          label="Inferences"
          value={summary.data?.inferences ?? 0}
          hint="conclusions, not measurements"
          loading={summary.isLoading}
        />
        <StatTile
          label="Alerts judged"
          value={falsePositives.data?.judged ?? 0}
          hint={
            rate !== null && rate !== undefined
              ? `${Math.round(rate * 100)}% not actionable`
              : "resolve alerts to measure accuracy"
          }
          loading={falsePositives.isLoading}
        />
      </div>

      <Card title="Events by type">
        {counts.isLoading ? (
          <div className="m-5 h-72 animate-pulse rounded bg-canvas-overlay" />
        ) : data.length === 0 ? (
          <EmptyState
            title="No events yet"
            description="Charts appear once cameras have been running."
          />
        ) : (
          <div className="h-80 p-5">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} layout="vertical" margin={{ left: 24, right: 16 }}>
                <CartesianGrid stroke="#233043" horizontal={false} />
                <XAxis type="number" stroke="#64748b" fontSize={11} allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="label"
                  stroke="#64748b"
                  fontSize={11}
                  width={180}
                />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  contentStyle={{
                    background: "#111823",
                    border: "1px solid #233043",
                    borderRadius: "0.5rem",
                    fontSize: "0.8125rem",
                  }}
                />
                <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                  {data.map((entry) => (
                    <Cell
                      key={entry.type}
                      fill={
                        entry.type.startsWith("POSSIBLE_") ||
                        entry.type.startsWith("POTENTIAL_")
                          ? "#a78bfa"
                          : "#06aef0"
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <p className="mt-2 text-xs text-slate-500">
              Violet bars are inferred conclusions; blue bars are direct observations.
            </p>
          </div>
        )}
      </Card>

      <Card title="How to read the false-positive rate">
        <div className="space-y-3 px-5 py-4 text-sm text-slate-400">
          <p>
            The rate counts only alerts an operator explicitly judged when resolving
            them. Alerts closed without a verdict are excluded, because reporting a
            rate over unjudged alerts would be inventing a number.
          </p>
          <p>
            A high rate is not a failure of the cameras. It usually means a zone is
            drawn too broadly, a threshold is too low, or a genuinely normal activity
            such as restocking or cleaning has not been accounted for in the opening
            hours.
          </p>
        </div>
      </Card>
    </div>
  );
}
