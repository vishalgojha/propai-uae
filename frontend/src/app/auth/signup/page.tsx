"use client";

export const dynamic = 'force-dynamic';

import { absoluteUrl } from "@/lib/base-path";
import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Mail, Lock, User, Phone, Loader2, AlertCircle, ArrowRight, Eye, EyeOff, CheckCircle } from "lucide-react";
import { signUp } from "@/lib/auth";

const AUTH_NEXT_KEY = "propai_auth_next";

function SignupContent() {
  const router = useRouter();
  const [next] = useState(() => {
    if (typeof window === "undefined") return "/";
    return new URLSearchParams(window.location.search).get("next") || "/";
  });

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [phone, setPhone] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    const phoneDigits = phone.replace(/\D/g, "");
    if (phoneDigits.length < 10 || phoneDigits.length > 15) {
      setError("Please enter a valid WhatsApp/mobile number");
      return;
    }

    setLoading(true);

    try {
      localStorage.setItem(AUTH_NEXT_KEY, next);
      await signUp(
        email,
        password,
        absoluteUrl("/auth/callback"),
        fullName,
        workspaceName.trim() || undefined,
        phoneDigits,
      );
      setSuccess("Check your email to confirm your account");
      setTimeout(() => router.push(`/auth/login?next=${encodeURIComponent(next)}`), 2000);
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : "Sign up failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-black px-4 py-12">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <Link href="/" className="inline-block mb-6">
            <svg className="w-12 h-12 mx-auto" viewBox="0 0 32 32" fill="none">
              <rect width="32" height="32" rx="8" fill="#0B0F14" stroke="#3EE88A" strokeWidth="1.5" />
              <path d="M8 16L14 22L24 10" stroke="#3EE88A" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </Link>
          <h1 className="text-2xl font-bold text-white">Create your account</h1>
          <p className="mt-2 text-sm text-zinc-500">Join PropAI and organize your market intelligence</p>
        </div>

        <div className="rounded-2xl border border-white/10 p-6">
          {success && (
            <div className="mb-4 flex items-center gap-2 p-3 rounded-lg bg-green-900/30 border border-green-500/20 text-green-300 text-sm">
              <CheckCircle className="w-4 h-4 shrink-0" />
              {success}
            </div>
          )}

          {error && (
            <div className="mb-4 flex items-center gap-2 p-3 rounded-lg bg-red-900/30 border border-red-500/20 text-red-300 text-sm">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="fullName" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                Full Name
              </label>
              <div className="relative mt-1">
                <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  id="fullName"
                  type="text"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  required
                  autoComplete="name"
                  className="w-full pl-10 pr-4 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="John Doe"
                  disabled={loading}
                />
              </div>
            </div>

            <div>
              <label htmlFor="workspaceName" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                Agency / Workspace Name
              </label>
              <div className="relative mt-1">
                <input
                  id="workspaceName"
                  type="text"
                  value={workspaceName}
                  onChange={(e) => setWorkspaceName(e.target.value)}
                  autoComplete="organization"
                  className="w-full px-4 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="Ananta Realty"
                  disabled={loading}
                />
              </div>
            </div>

            <div>
              <label htmlFor="email" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                Email
              </label>
              <div className="relative mt-1">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                  className="w-full pl-10 pr-4 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="you@company.com"
                  disabled={loading}
                />
              </div>
            </div>

            <div>
              <label htmlFor="phone" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                WhatsApp / Mobile Number
              </label>
              <div className="relative mt-1">
                <Phone className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  id="phone"
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  required
                  autoComplete="tel"
                  className="w-full pl-10 pr-4 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="98765 43210"
                  disabled={loading}
                />
              </div>
              <p className="mt-1 text-[11px] text-zinc-600">Used to connect your PropAI workspace with WhatsApp.</p>
            </div>

            <div>
              <label htmlFor="password" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                Password
              </label>
              <div className="relative mt-1">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  autoComplete="new-password"
                  className="w-full pl-10 pr-12 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="•••••••• (min 8 chars)"
                  disabled={loading}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-white"
                >
                  {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">
                Confirm Password
              </label>
              <div className="relative mt-1">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  id="confirmPassword"
                  type={showPassword ? "text" : "password"}
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  required
                  autoComplete="new-password"
                  className="w-full pl-10 pr-4 py-2.5 bg-zinc-800/50 border border-white/10 rounded-lg text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/50 transition-colors"
                  placeholder="••••••••"
                  disabled={loading}
                />
              </div>
            </div>

            {error && (
              <div className="flex items-center gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                <AlertCircle className="w-4 h-4 shrink-0" />
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !email || !password || !confirmPassword || !fullName || !workspaceName || !phone}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-emerald-400 text-black rounded-lg text-sm font-bold min-h-[48px] disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>Creating account…</span>
                </>
              ) : (
                <>
                  Create account
                  <ArrowRight className="w-4 h-4" />
                </>
              )}
            </button>
          </form>

          <div className="mt-6 text-center text-sm text-zinc-500">
            Already have an account?{" "}
            <Link href={`/auth/login?next=${encodeURIComponent(next)}`} className="text-emerald-400 hover:text-emerald-300 font-medium">
              Sign in
            </Link>
          </div>
        </div>

        <p className="mt-6 text-center text-xs text-zinc-600">
          By continuing, you agree to our <Link href="/terms" className="underline hover:text-white">Terms</Link> and <Link href="/privacy" className="underline hover:text-white">Privacy Policy</Link>
        </p>
      </div>
    </div>
  );
}

export default function SignupPage() {
  return <SignupContent />;
}
