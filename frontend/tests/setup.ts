import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

// Every test starts from a signed-out browser: no leftover tokens from a
// previous test deciding whether a request carries a bearer token.
beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});
