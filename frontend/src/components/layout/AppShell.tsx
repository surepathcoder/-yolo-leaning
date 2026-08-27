/**
 * The application shell: navigation, connection status, and the outlet.
 *
 * Two deliberate choices for an operations screen:
 *
 * The **connection indicator is always visible**. A dashboard that has quietly
 * stopped receiving events looks identical to a quiet shop. An operator has to
 * be able to tell those apart at a glance, so the realtime state is shown in
 * the header at all times rather than only when it breaks.
 *
 * **Navigation is filtered by permission.** A viewer never sees a Settings
 * link they would only get a 403 from. This is usability, not security -- the
 * server checks every request regardless.
 */

import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";

import { classNames } from "@/components/ui/primitives";
import { useRealtimeConnection, useRealtimeStore } from "@/hooks/useRealtime";
import { useAuth } from "@/stores/auth";

interface NavItem {
  to: string;
  label: string;
  /** Permission required to see this item; undefined means always visible. */
  permission?: string;
}

const NAV_SECTIONS: { heading: string; items: NavItem[] }[] = [
  {
    heading: "Operations",
    items: [
      { to: "/", label: "Dashboard" },
      { to: "/alerts", label: "Alerts", permission: "alert:read" },
      { to: "/events", label: "Events", permission: "event:read" },
      { to: "/live", label: "Live view", permission: "camera:view_live" },
    ],
  },
  {
    heading: "Estate",
    items: [
      { to: "/stores", label: "Stores", permission: "store:read" },
      { to: "/cameras", label: "Cameras", permission: "camera:read" },
      { to: "/zones", label: "Zones & lines", permission: "zone:read" },
    ],
  },
  {
    heading: "Insight",
    items: [
      { to: "/analytics", label: "Analytics", permission: "analytics:read" },
      { to: "/system", label: "System", permission: "system:metrics_read" },
    ],
  },
];

function ConnectionIndicator() {
  const status = useRealtimeStore((state) => state.status);
  const messageCount = useRealtimeStore((state) => state.messageCount);
  const lastMessageAt = useRealtimeStore((state) => state.lastMessageAt);

  const presentation = {
    open: { dot: "bg-emerald-400", label: "Live", tone: "text-emerald-300" },
    connecting: { dot: "bg-amber-400 animate-pulse", label: "Connecting", tone: "text-amber-300" },
    reconnecting: { dot: "bg-amber-400 animate-pulse", label: "Reconnecting", tone: "text-amber-300" },
    closed: { dot: "bg-slate-600", label: "Offline", tone: "text-slate-400" },
    idle: { dot: "bg-slate-600", label: "Offline", tone: "text-slate-400" },
  }[status];

  const title =
    status === "open"
      ? `Receiving live updates. ${messageCount} message${messageCount === 1 ? "" : "s"} this session${
          lastMessageAt ? `, last ${new Date(lastMessageAt).toLocaleTimeString()}` : ""
        }.`
      : "Not receiving live updates. The page will still load data when you navigate.";

  return (
    <span
      className={classNames("inline-flex items-center gap-2 text-xs font-medium", presentation.tone)}
      title={title}
    >
      <span className={classNames("h-2 w-2 rounded-full", presentation.dot)} aria-hidden />
      {presentation.label}
    </span>
  );
}

function ThemeToggle() {
  const [isDark, setIsDark] = useState(() =>
    document.documentElement.classList.contains("dark"),
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", isDark);
    document.documentElement.classList.toggle("light", !isDark);
    try {
      localStorage.setItem("visionguard.theme", isDark ? "dark" : "light");
    } catch {
      /* private window */
    }
  }, [isDark]);

  return (
    <button
      type="button"
      className="btn-ghost px-2"
      onClick={() => setIsDark((value) => !value)}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {isDark ? "☾" : "☀"}
    </button>
  );
}

export function AppShell() {
  const user = useAuth((state) => state.user);
  const can = useAuth((state) => state.can);
  const logout = useAuth((state) => state.logout);
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);

  useRealtimeConnection(Boolean(user));

  const handleSignOut = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  const visibleSections = NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => !item.permission || can(item.permission)),
  })).filter((section) => section.items.length > 0);

  return (
    <div className="flex min-h-screen">
      {/* ------------------------------------------------------------ sidebar */}
      <aside
        className={classNames(
          "fixed inset-y-0 left-0 z-40 w-60 shrink-0 border-r border-canvas-border bg-canvas-raised",
          "transition-transform lg:static lg:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-14 items-center gap-2.5 border-b border-canvas-border px-5">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-md bg-brand-600"
            aria-hidden
          >
            <span className="block h-3 w-3 rounded-full border-2 border-white" />
          </span>
          <span className="text-sm font-semibold tracking-tight text-slate-100">
            VisionGuard
          </span>
        </div>

        <nav className="flex flex-col gap-6 px-3 py-5" aria-label="Main">
          {visibleSections.map((section) => (
            <div key={section.heading}>
              <p className="px-2 pb-2 text-2xs font-semibold uppercase tracking-wider text-slate-600">
                {section.heading}
              </p>
              <ul className="space-y-0.5">
                {section.items.map((item) => (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.to === "/"}
                      onClick={() => setMobileOpen(false)}
                      className={({ isActive }) =>
                        classNames(
                          "block rounded-lg px-3 py-2 text-sm transition-colors",
                          isActive
                            ? "bg-brand-600/15 font-medium text-brand-200"
                            : "text-slate-400 hover:bg-canvas-overlay hover:text-slate-200",
                        )
                      }
                    >
                      {item.label}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="absolute inset-x-0 bottom-0 border-t border-canvas-border p-3">
          <div className="rounded-lg px-3 py-2">
            <p className="truncate text-sm font-medium text-slate-200">
              {user?.full_name || user?.email}
            </p>
            <p className="truncate text-2xs uppercase tracking-wide text-slate-500">
              {user?.role.replace(/_/g, " ").toLowerCase()}
            </p>
          </div>
          <button type="button" className="btn-ghost mt-1 w-full justify-start" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </aside>

      {mobileOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* --------------------------------------------------------------- main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-canvas-border bg-canvas/90 px-5 backdrop-blur">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="btn-ghost px-2 lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
            >
              ☰
            </button>
            <p className="text-sm text-slate-400">{user?.organization_name}</p>
          </div>
          <div className="flex items-center gap-4">
            <ConnectionIndicator />
            <ThemeToggle />
          </div>
        </header>

        <main className="flex-1 px-5 py-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
