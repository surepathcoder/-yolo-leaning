/**
 * Sign in and first-time registration.
 *
 * The disambiguation flow is worth noting: the same email can legitimately
 * belong to two organizations (one person running two businesses), and the
 * backend answers a 409 listing them. Rather than showing a raw error, the
 * form turns that into an organization picker and retries.
 */

import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "@/services/client";
import { useAuth } from "@/stores/auth";

type Mode = "signin" | "register";

interface OrganizationChoice {
  slug: string;
  name: string;
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useAuth((state) => state.login);
  const register = useAuth((state) => state.register);

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [organizationSlug, setOrganizationSlug] = useState<string>("");
  const [choices, setChoices] = useState<OrganizationChoice[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const redirectTo =
    (location.state as { from?: string } | null)?.from ?? "/";

  const submit = async (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    setError(null);
    setFieldErrors({});
    setBusy(true);

    try {
      if (mode === "signin") {
        await login(email, password, organizationSlug || undefined);
      } else {
        await register({
          email,
          password,
          full_name: fullName,
          organization_name: organizationName,
        });
      }
      navigate(redirectTo, { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError) {
        // The same email in two organizations: ask which, then retry.
        const organizations = caught.details?.organizations;
        if (caught.status === 409 && Array.isArray(organizations)) {
          setChoices(organizations as OrganizationChoice[]);
          setError("This email is registered with more than one organization.");
        } else {
          setError(caught.message);
          setFieldErrors(caught.fieldErrors);
        }
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-3">
          <span
            className="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-600"
            aria-hidden
          >
            <span className="block h-4 w-4 rounded-full border-2 border-white" />
          </span>
          <div>
            <h1 className="text-lg font-semibold tracking-tight text-slate-100">
              VisionGuard
            </h1>
            <p className="text-xs text-slate-500">
              CCTV, security and inventory intelligence
            </p>
          </div>
        </div>

        <div className="card p-6">
          <h2 className="text-base font-semibold text-slate-100">
            {mode === "signin" ? "Sign in" : "Create an organization"}
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            {mode === "signin"
              ? "Use your VisionGuard account."
              : "Registers a new business and its first administrator."}
          </p>

          <form className="mt-6 space-y-4" onSubmit={submit} noValidate>
            {mode === "register" && (
              <>
                <div>
                  <label className="label" htmlFor="full_name">
                    Your name
                  </label>
                  <input
                    id="full_name"
                    className="input"
                    value={fullName}
                    onChange={(event) => setFullName(event.target.value)}
                    autoComplete="name"
                    required
                  />
                </div>
                <div>
                  <label className="label" htmlFor="organization_name">
                    Business name
                  </label>
                  <input
                    id="organization_name"
                    className="input"
                    value={organizationName}
                    onChange={(event) => setOrganizationName(event.target.value)}
                    autoComplete="organization"
                    required
                  />
                </div>
              </>
            )}

            <div>
              <label className="label" htmlFor="email">
                Email
              </label>
              <input
                id="email"
                type="email"
                className="input"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="email"
                required
              />
              {fieldErrors.email && (
                <p className="mt-1 text-xs text-rose-300">{fieldErrors.email}</p>
              )}
            </div>

            <div>
              <label className="label" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                type="password"
                className="input"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                required
              />
              {mode === "register" && (
                <p className="mt-1 text-xs text-slate-500">
                  At least 12 characters. Length matters more than symbols.
                </p>
              )}
              {fieldErrors.password && (
                <p className="mt-1 text-xs text-rose-300">{fieldErrors.password}</p>
              )}
            </div>

            {choices.length > 0 && (
              <div>
                <label className="label" htmlFor="organization">
                  Organization
                </label>
                <select
                  id="organization"
                  className="input"
                  value={organizationSlug}
                  onChange={(event) => setOrganizationSlug(event.target.value)}
                  required
                >
                  <option value="">Choose an organization…</option>
                  {choices.map((choice) => (
                    <option key={choice.slug} value={choice.slug}>
                      {choice.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {error && (
              <div
                role="alert"
                className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200"
              >
                {error}
              </div>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy
                ? "Please wait…"
                : mode === "signin"
                  ? "Sign in"
                  : "Create organization"}
            </button>
          </form>

          <p className="mt-5 text-center text-sm text-slate-500">
            {mode === "signin" ? (
              <>
                New business?{" "}
                <button
                  type="button"
                  className="text-brand-300 hover:underline"
                  onClick={() => {
                    setMode("register");
                    setError(null);
                    setChoices([]);
                  }}
                >
                  Create an organization
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button
                  type="button"
                  className="text-brand-300 hover:underline"
                  onClick={() => {
                    setMode("signin");
                    setError(null);
                  }}
                >
                  Sign in
                </button>
              </>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}
