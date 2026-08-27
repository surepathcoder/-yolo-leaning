import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";
import "@/index.css";

// Restore the saved theme before first paint so the page does not flash the
// wrong background on load.
try {
  const stored = localStorage.getItem("visionguard.theme");
  const dark = stored ? stored === "dark" : true;
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.classList.toggle("light", !dark);
} catch {
  /* private window: keep the default dark theme */
}

const container = document.getElementById("root");
if (!container) throw new Error("root element is missing from index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
