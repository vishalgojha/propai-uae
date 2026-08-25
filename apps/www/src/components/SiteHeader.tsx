"use client";

import Link from "next/link";
import { useState } from "react";

const NAV_LINKS = [
  { href: "/map", label: "Map" },
  { href: "/localities", label: "Localities" },
  { href: "/about", label: "About" },
  { href: "/contact", label: "Contact" },
];

function Wordmark() {
  return (
    <span className="flex items-center gap-2.5 transition-all duration-base hover:scale-[1.02] active:scale-[0.98]">
      <img src="/propai-logo.svg" alt="" aria-hidden="true" className="h-10 w-10" />
      <span className="text-2xl font-bold tracking-tight text-white">
        Prop<span className="text-[#6B8E63]">AI</span>
      </span>
    </span>
  );
}

export type SiteHeaderProps = {
  backHref?: string;
  backLabel?: string;
};

export default function SiteHeader({ backHref, backLabel }: SiteHeaderProps) {
  const [open, setOpen] = useState(false);

  return (
    <header className="site-header border-b border-white/[0.06] sticky top-0 bg-black/80 backdrop-blur z-50">
      <div className="max-w-[1600px] mx-auto px-4 lg:px-6 h-20 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/" aria-label="PropAI home" className="flex items-center" onClick={() => setOpen(false)}>
            <Wordmark />
          </Link>
          {backHref && (
            <Link
              href={backHref}
              className="hidden sm:inline-flex items-center gap-1.5 text-sm text-zinc-400 hover:text-white transition-colors"
            >
              <span aria-hidden="true">←</span> {backLabel ?? "Back"}
            </Link>
          )}
        </div>

        <nav className="hidden lg:flex items-center gap-8" aria-label="Primary">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-[15px] text-zinc-400 hover:text-white transition-all duration-base hover:scale-[1.02] active:scale-[0.98]"
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <div className="hidden lg:flex items-center gap-4">
          <Link
            href="/broker/auth/login"
            className="text-[15px] text-zinc-400 hover:text-white transition-all duration-base hover:scale-[1.02] active:scale-[0.98]"
          >
            Broker login
          </Link>
          <Link
            href="/contact"
            className="site-primary-cta inline-flex items-center rounded-full bg-[var(--accent-primary)] px-4 py-2 text-sm font-semibold text-[#FAF7F0] transition-all duration-base hover:bg-[var(--accent-primary-hover)] hover:scale-[1.02] active:scale-[0.98]"
          >
            Get started
          </Link>
        </div>

        {/* Mobile menu toggle */}
        <div className="flex items-center gap-2 lg:hidden">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-label={open ? "Close menu" : "Open menu"}
            aria-expanded={open}
            className="inline-flex items-center justify-center h-10 w-10 rounded-xl border border-white/10 text-zinc-300 hover:text-white hover:border-white/20 transition-colors"
          >
            {open ? (
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
            </svg>
            ) : (
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M4 7h16M4 12h16M4 17h16" strokeLinecap="round" />
            </svg>
            )}
          </button>
        </div>
      </div>

      {/* Mobile dropdown */}
      {open && (
        <div className="site-mobile-menu lg:hidden border-t border-white/[0.06] bg-black/95 backdrop-blur">
          <nav className="max-w-[1600px] mx-auto px-4 py-3 flex flex-col" aria-label="Mobile">
            {NAV_LINKS.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                onClick={() => setOpen(false)}
                className="py-3 text-[16px] text-zinc-300 hover:text-white border-b border-white/[0.04] transition-colors"
              >
                {link.label}
              </Link>
            ))}
            <div className="flex items-center gap-4 pt-4 pb-2">
              <Link
                href="/broker/auth/login"
                onClick={() => setOpen(false)}
                className="text-[15px] text-zinc-400 hover:text-white transition-colors"
              >
                Broker login
              </Link>
              <Link
                href="/contact"
                onClick={() => setOpen(false)}
              className="site-primary-cta inline-flex items-center rounded-full bg-[var(--accent-primary)] px-4 py-2 text-sm font-semibold text-[#FAF7F0] transition-colors"
              >
                Get started
              </Link>
            </div>
          </nav>
        </div>
      )}
    </header>
  );
}
