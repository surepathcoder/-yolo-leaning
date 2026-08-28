/**
 * The sign-in / registration form, including the 409 multi-organization
 * disambiguation flow described in the page's own header comment: the same
 * email can legitimately belong to two organizations, and the form has to
 * turn that raw error into a picker rather than a dead end.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/services/endpoints", () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
  },
}));

import { ApiError } from "@/services/client";
import { authApi } from "@/services/endpoints";
import { LoginPage } from "@/pages/Login";
import { useAuth } from "@/stores/auth";

import { currentUser, tokens } from "../fixtures";

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<div>Dashboard home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.setState({ user: null, isLoading: false, error: null });
});

describe("sign in", () => {
  it("signs in and navigates to the dashboard", async () => {
    vi.mocked(authApi.login).mockResolvedValue({ tokens: tokens(), user: currentUser() });
    renderLogin();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/email/i), "owner@example.com");
    await user.type(screen.getByLabelText(/password/i), "supersecretpassword");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Dashboard home")).toBeInTheDocument();
    expect(authApi.login).toHaveBeenCalledWith("owner@example.com", "supersecretpassword", undefined);
  });

  it("shows the server's field errors on a validation failure", async () => {
    vi.mocked(authApi.login).mockRejectedValue(
      new ApiError(422, "VALIDATION_ERROR", "Check the highlighted fields.", {
        fields: [{ field: "password", message: "Incorrect password" }],
      }),
    );
    renderLogin();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/email/i), "owner@example.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    expect(screen.getByText("Incorrect password")).toBeInTheDocument();
  });

  it("offers an organization picker on a multi-org 409 and retries with the choice", async () => {
    vi.mocked(authApi.login)
      .mockRejectedValueOnce(
        new ApiError(409, "MULTIPLE_ORGANIZATIONS", "This email is registered with more than one organization.", {
          organizations: [
            { slug: "acme", name: "Acme Retail" },
            { slug: "acme-2", name: "Acme Wholesale" },
          ],
        }),
      )
      .mockResolvedValueOnce({ tokens: tokens(), user: currentUser() });

    renderLogin();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/email/i), "owner@example.com");
    await user.type(screen.getByLabelText(/password/i), "supersecretpassword");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    const picker = await screen.findByLabelText(/organization/i);
    await user.selectOptions(picker, "acme-2");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Dashboard home")).toBeInTheDocument();
    expect(authApi.login).toHaveBeenLastCalledWith("owner@example.com", "supersecretpassword", "acme-2");
  });
});

describe("registration", () => {
  it("switches modes and creates an organization", async () => {
    vi.mocked(authApi.register).mockResolvedValue({
      tokens: tokens(),
      user: currentUser(),
      organization_id: "org-1",
      organization_slug: "acme",
    });
    renderLogin();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /create an organization/i }));

    await user.type(screen.getByLabelText(/your name/i), "Jamie Rivera");
    await user.type(screen.getByLabelText(/business name/i), "Acme Retail");
    await user.type(screen.getByLabelText(/email/i), "jamie@example.com");
    await user.type(screen.getByLabelText(/password/i), "supersecretpassword");
    await user.click(screen.getByRole("button", { name: /create organization/i }));

    expect(await screen.findByText("Dashboard home")).toBeInTheDocument();
    expect(authApi.register).toHaveBeenCalledWith({
      email: "jamie@example.com",
      password: "supersecretpassword",
      full_name: "Jamie Rivera",
      organization_name: "Acme Retail",
    });
  });
});
