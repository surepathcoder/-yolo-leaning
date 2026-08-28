/**
 * `EventKindBadge` renders the platform's one governing rule -- an
 * observation is not a conclusion -- so its two branches get a test each, not
 * just a snapshot. The rest of this file covers the small formatting helpers
 * every page leans on.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  CameraStatusPill,
  EmptyState,
  ErrorState,
  EventKindBadge,
  RelativeTime,
  SeverityBadge,
  classNames,
  formatDuration,
  humaniseEventType,
} from "@/components/ui/primitives";

describe("classNames", () => {
  it("joins truthy values and drops false/null/undefined", () => {
    expect(classNames("a", false, null, undefined, "b")).toBe("a b");
  });
});

describe("formatDuration", () => {
  it("formats seconds, minutes and hours", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(125)).toBe("2m 5s");
    expect(formatDuration(120)).toBe("2m");
    expect(formatDuration(3725)).toBe("1h 2m");
  });
});

describe("humaniseEventType", () => {
  it("turns an upper-snake event type into a sentence", () => {
    expect(humaniseEventType("RESTRICTED_AREA_ENTRY")).toBe("Restricted area entry");
    expect(humaniseEventType("POSSIBLE_INVENTORY_DISCREPANCY")).toBe(
      "Possible inventory discrepancy",
    );
  });
});

describe("EventKindBadge", () => {
  it("marks a measurement as Observed with no inference language", () => {
    render(<EventKindBadge isInference={false} />);
    expect(screen.getByText("Observed")).toBeInTheDocument();
    expect(screen.queryByText(/inferred/i)).not.toBeInTheDocument();
  });

  it("marks an inference distinctly and states its confidence", () => {
    render(<EventKindBadge isInference confidence="MEDIUM" />);
    expect(screen.getByText("Inferred · medium confidence")).toBeInTheDocument();
  });

  it("still reads as Inferred with no confidence supplied", () => {
    render(<EventKindBadge isInference confidence={null} />);
    expect(screen.getByText("Inferred")).toBeInTheDocument();
  });
});

describe("SeverityBadge", () => {
  it("renders the severity label", () => {
    render(<SeverityBadge severity="CRITICAL" />);
    expect(screen.getByText("CRITICAL")).toBeInTheDocument();
  });
});

describe("CameraStatusPill", () => {
  it("title-cases the status", () => {
    render(<CameraStatusPill status="OFFLINE" />);
    expect(screen.getByText("Offline")).toBeInTheDocument();
  });
});

describe("RelativeTime", () => {
  it("reads 'just now' for a timestamp seconds ago", () => {
    render(<RelativeTime value={new Date(Date.now() - 5_000).toISOString()} />);
    expect(screen.getByText("just now")).toBeInTheDocument();
  });

  it("reports whole minutes for an older timestamp", () => {
    render(<RelativeTime value={new Date(Date.now() - 5 * 60_000).toISOString()} />);
    expect(screen.getByText("5 min ago")).toBeInTheDocument();
  });

  it("always exposes the absolute time on hover via the title attribute", () => {
    const value = new Date(Date.now() - 5_000).toISOString();
    render(<RelativeTime value={value} />);
    const time = screen.getByText("just now");
    expect(time).toHaveAttribute("title");
    expect(time).toHaveAttribute("datetime", value);
  });
});

describe("EmptyState / ErrorState", () => {
  it("renders the empty-state title and description", () => {
    render(<EmptyState title="No cameras" description="Add one to get started." />);
    expect(screen.getByText("No cameras")).toBeInTheDocument();
    expect(screen.getByText("Add one to get started.")).toBeInTheDocument();
  });

  it("calls onRetry when the error state's retry button is clicked", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Could not load cameras." onRetry={onRetry} />);
    screen.getByRole("button", { name: /try again/i }).click();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("omits the retry button when no handler is given", () => {
    render(<ErrorState message="Could not load cameras." />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
