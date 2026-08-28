/**
 * The shell's nav is filtered by permission so a viewer never sees a link
 * that would only 403 -- covered here since it is a usability contract, not
 * something the server enforces on the client's behalf.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useRealtime", () => ({
  useRealtimeConnection: vi.fn(),
  useRealtimeStore: vi.fn(),
}));

vi.mock("@/services/endpoints", () => ({
  authApi: { logout: vi.fn().mockResolvedValue({ message: "ok" }) },
}));

import { AppShell } from "@/components/layout/AppShell";
import { useRealtimeStore } from "@/hooks/useRealtime";
import { tokenStore } from "@/services/client";
import { useAuth } from "@/stores/auth";

import { currentUser, tokens } from "../fixtures";

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<div>Page content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useRealtimeStore).mockImplementation(
    ((selector: (state: unknown) => unknown) =>
      selector({
        status: "open",
        messageCount: 3,
        lastMessageAt: Date.now(),
        reconnectAttempts: 0,
        connect: vi.fn(),
        disconnect: vi.fn(),
        subscribe: vi.fn(),
        setCameraFilter: vi.fn(),
      })) as unknown as typeof useRealtimeStore,
  );
});

describe("navigation", () => {
  it("shows only the sections and links the user's permissions allow", async () => {
    useAuth.setState({
      user: currentUser({ permissions: ["store:read"] }),
    });

    renderShell();

    expect(await screen.findByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Stores" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Cameras" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Detection models" })).not.toBeInTheDocument();
  });

  it("shows the detection-models link when model:read is granted", async () => {
    useAuth.setState({ user: currentUser({ permissions: ["model:read"] }) });

    renderShell();

    expect(await screen.findByRole("link", { name: "Detection models" })).toBeInTheDocument();
  });
});

describe("sign out", () => {
  it("clears the session and returns to the login route", async () => {
    tokenStore.set(tokens());
    useAuth.setState({ user: currentUser({ permissions: [] }) });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div>Page content</div>} />
          </Route>
          <Route path="/login" element={<div>Sign-in screen</div>} />
        </Routes>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /sign out/i }));

    expect(await screen.findByText("Sign-in screen")).toBeInTheDocument();
    expect(tokenStore.access).toBeNull();
    expect(useAuth.getState().user).toBeNull();
  });
});
