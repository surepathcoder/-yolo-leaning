/**
 * Routing and the authentication gate.
 *
 * `RequireAuth` waits for the initial session restore before deciding. Without
 * that wait, a signed-in user reloading the page sees the login screen flash
 * before being redirected back, which reads as "I have been signed out" every
 * single refresh.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { Spinner } from "@/components/ui/primitives";
import { AlertsPage } from "@/pages/Alerts";
import { AnalyticsPage } from "@/pages/Analytics";
import { CamerasPage } from "@/pages/Cameras";
import { DashboardPage } from "@/pages/Dashboard";
import { EventsPage } from "@/pages/Events";
import { InventoryPage } from "@/pages/Inventory";
import { LiveViewPage } from "@/pages/LiveView";
import { LoginPage } from "@/pages/Login";
import { ReconciliationPage } from "@/pages/Reconciliation";
import { StoresPage } from "@/pages/Stores";
import { SystemPage } from "@/pages/System";
import { ZonesPage } from "@/pages/Zones";
import { ApiError } from "@/services/client";
import { useAuth } from "@/stores/auth";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The dashboard is driven by WebSocket invalidation, so aggressive
      // polling is unnecessary. A short stale time still avoids refetching
      // the same list twice when two widgets mount together.
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (failureCount, error) => {
        // Never retry an authorisation failure: the answer will not change,
        // and retrying turns one 403 into four.
        if (error instanceof ApiError && (error.isAuthError || error.isPermissionError)) {
          return false;
        }
        return failureCount < 2;
      },
    },
  },
});

function RequireAuth({ children }: { children: React.ReactNode }) {
  const user = useAuth((state) => state.user);
  const isLoading = useAuth((state) => state.isLoading);
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}

function AppRoutes() {
  const restore = useAuth((state) => state.restore);

  useEffect(() => {
    void restore();
  }, [restore]);

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<DashboardPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/events" element={<EventsPage />} />
        <Route path="/live" element={<LiveViewPage />} />
        <Route path="/stores" element={<StoresPage />} />
        <Route path="/cameras" element={<CamerasPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
            <Route path="/reconciliation" element={<ReconciliationPage />} />
            <Route path="/zones" element={<ZonesPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/system" element={<SystemPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
