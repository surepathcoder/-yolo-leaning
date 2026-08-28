/**
 * `RequireAuth` is what stops a signed-out visitor ever reaching a data page.
 * Exercised through the real `App` (BrowserRouter and all) rather than an
 * extracted unit, since the redirect and the loading state are the actual
 * product behaviour a user sees.
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "@/App";
import { tokenStore } from "@/services/client";

beforeEach(() => {
  window.history.pushState({}, "", "/");
  tokenStore.clear();
});

describe("App / RequireAuth", () => {
  it("redirects an unauthenticated visitor from the dashboard to sign in", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });
});
