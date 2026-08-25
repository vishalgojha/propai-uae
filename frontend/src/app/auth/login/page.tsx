"use client";

export const dynamic = 'force-dynamic';

import { absoluteUrl, withBasePath } from "@/lib/base-path";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Mail, Lock, Eye, EyeOff, ArrowRight, AlertCircle, Loader2 } from "lucide-react";
import { signInWithEmail, signInWithMagicLink, getSession } from "@/lib/auth";

const AUTH_NEXT_KEY = "propai_auth_next";

function LoginContent() {
  const router = useRouter();
  const [next] = useState(() => {
    if (typeof window === "undefined") return "/";
    return new URLSearchParams(window.location.search).get("next") || "/";
  });

  const [mode, setMode] = useState<"email" | "magic">("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      if (mode === "email") {
        await signInWithEmail(email, password);
      } else {
        localStorage.setItem(AUTH_NEXT_KEY, next);
        await signInWithMagicLink(email, absoluteUrl("/auth/callback"));
        alert("Magic link sent! Check your email.");
        return;
      }

      router.push(next);
      router.refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : "Sign in failed");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    getSession().then((session) => {
      if (session) router.push(next);
    });
  }, [next, router]);

  return (
    <div className="min-h-dvh bg-[var(--background)] px-4 py-8 text-[var(--text-primary)] sm:px-8 lg:px-12 lg:py-12">
      <div className="mx-auto grid min-h-[calc(100dvh-6rem)] w-full max-w-6xl items-center gap-12 lg:grid-cols-[minmax(0,0.9fr)_minmax(26rem,0.75fr)] lg:gap-20">
        <section className="hidden lg:block">
          <Link href="/" className="inline-flex items-center gap-3">
            <img src={withBasePath("//propai-logo.svg")} alt="" aria-hidden="true" className="h-12 w-12" />
            <span className="text-xl font-bold tracking-tight">Prop<span className="text-[var(--accent-primary)]">AI</span></span>
          </Link>
          <div className="mt-24 max-w-xl">
            <h1 className="max-w-lg text-5xl font-semibold leading-[1.03] tracking-[-0.04em] text-[var(--text-primary)]">
              Your market desk starts before the portal.
            </h1>
            <p className="mt-6 max-w-lg text-lg leading-8 text-[var(--text-secondary)]">
              PropAI turns live broker conversations into searchable property context—locality, building, price, and the person behind the signal.
            </p>
            <div className="mt-12 border-y border-[var(--border-subtle)] text-sm">
              <div className="grid grid-cols-[5.5rem_1fr] gap-5 border-b border-[var(--border-subtle)] py-4">
                <span className="text-[var(--accent-primary)]">Captured</span>
                <span className="text-[var(--text-secondary)]">A real broker conversation, kept intact.</span>
              </div>
              <div className="grid grid-cols-[5.5rem_1fr] gap-5 border-b border-[var(--border-subtle)] py-4">
                <span className="text-[var(--accent-primary)]">Grounded</span>
                <span className="text-[var(--text-secondary)]">Locality and building context around the listing.</span>
              </div>
              <div className="grid grid-cols-[5.5rem_1fr] gap-5 py-4">
                <span className="text-[var(--accent-primary)]">Actionable</span>
                <span className="text-[var(--text-secondary)]">A direct path back to the broker who shared it.</span>
              </div>
            </div>
          </div>
          <p className="mt-16 text-xs text-[var(--text-secondary)]">A private workspace for property professionals.</p>
        </section>

        <section className="mx-auto w-full max-w-md">
          <div className="mb-8 text-center lg:text-left">
            <Link href="/" className="mb-6 inline-flex lg:hidden">
              <img src={withBasePath("//propai-logo.svg")} alt="PropAI" className="h-12 w-12" />
            </Link>
            <h2 className="text-2xl font-bold text-[var(--text-primary)]">Welcome back</h2>
            <p className="mt-2 text-sm text-[var(--text-secondary)]">Sign in to your PropAI workspace</p>
          </div>

        <div className="rounded-[14px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-6 sm:p-7">
          <div className="mb-6 flex gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-base)] p-1">
            <button
              onClick={() => setMode("email")}
              className={`flex-1 px-3 py-2 text-sm font-medium rounded-md transition-colors ${
                mode === "email" ? "bg-[var(--accent-primary)] text-[#FAF7F0]" : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              Email + Password
            </button>
            <button
              onClick={() => setMode("magic")}
              className={`flex-1 px-3 py-2 text-sm font-medium rounded-md transition-colors ${
                mode === "magic" ? "bg-[var(--accent-primary)] text-[#FAF7F0]" : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              Magic Link
            </button>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="email" className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--text-secondary)]">
                Email
              </label>
              <div className="relative mt-1">
                <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-secondary)]" />
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-base)] py-2.5 pl-10 pr-4 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-secondary)] transition-colors focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-[var(--accent-primary)]"
                  placeholder="you@company.com"
                  disabled={loading}
                />
              </div>
            </div>

            {mode === "email" && (
              <div>
                <label htmlFor="password" className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--text-secondary)]">
                  Password
                </label>
                <div className="relative mt-1">
                  <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-secondary)]" />
                  <input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    autoComplete="current-password"
                    className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-base)] py-2.5 pl-10 pr-12 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-secondary)] transition-colors focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-[var(--accent-primary)]"
                    placeholder="••••••••"
                    disabled={loading}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                  >
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>
            )}

            {error && (
              <div className="flex items-center gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                <AlertCircle className="w-4 h-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !email || (mode === "email" && !password)}
              className="flex min-h-[48px] w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent-primary)] px-4 py-2.5 text-sm font-bold text-[#FAF7F0] transition-all hover:-translate-y-0.5 hover:bg-[var(--accent-primary-hover)] hover:shadow-[0_8px_18px_rgba(63,90,58,0.18)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>Signing in…</span>
                </>
              ) : mode === "email" ? (
                <>
                  Sign in
                  <ArrowRight className="w-4 h-4" />
                </>
              ) : (
                <>
                  Send Magic Link
                  <ArrowRight className="w-4 h-4" />
                </>
              )}
            </button>
          </form>

          <div className="mt-6 text-center text-sm text-[var(--text-secondary)]">
            Don&apos;t have an account?{" "}
            <Link href={`/auth/signup?next=${encodeURIComponent(next)}`} className="font-medium text-[var(--accent-primary)] hover:text-[var(--accent-primary-hover)]">
              Sign up
            </Link>
          </div>
        </div>
        </section>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return <LoginContent />;
}
