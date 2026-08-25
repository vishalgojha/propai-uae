"use client";

import { useEffect, useMemo, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Search, MapPin, Loader2 } from "lucide-react";
import type { LocalitySummary } from "@/lib/localities";
import { useAnalytics } from "@/lib/useAnalytics";

type LocalitySuggestion = { locality: string; slug: string; listingCount: number };

export default function SearchBox({
  query,
  asset,
  localities,
  onSubmit,
}: {
  query: string;
  asset: string;
  localities: LocalitySummary[];
  onSubmit?: (next: { q: string; asset: string }) => void;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [value, setValue] = useState(query);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [justTyped, setJustTyped] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { track } = useAnalytics();

  const suggestions = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q) return [];
    return localities
      .filter((l) => l.locality.toLowerCase().includes(q))
      .sort((a, b) => b.listingCount - a.listingCount)
      .slice(0, 8);
  }, [value, localities]);

  // Keep the dropdown open while the user is actively typing a partial match,
  // but let an explicit submit close it.
  useEffect(() => {
    if (justTyped) {
      setOpen(suggestions.length > 0);
      setJustTyped(false);
    }
  }, [justTyped, suggestions.length]);

  // Close on outside click.
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function submitSearch(override?: string) {
    const q = (override ?? value).trim();
    setOpen(false);
    // When used inline (e.g. homepage), let the parent handle the search so
    // results render in place instead of navigating to /search.
    if (onSubmit) {
      onSubmit({ q, asset });
      return;
    }
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (asset) params.set("asset", asset);
    const qs = params.toString();
    track("search", { query: q, asset });
    startTransition(() => {
      router.push(qs ? `/search?${qs}` : "/search");
    });
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    // Arrow/escape navigation only matters while the dropdown is open.
    if (open) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => Math.min(i + 1, suggestions.length - 1));
        return;
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => Math.max(i - 1, 0));
        return;
      } else if (e.key === "Escape") {
        setOpen(false);
        return;
      }
    }

    // Enter always submits the search (highlighted suggestion jumps to its
    // locality page; otherwise run the natural-language search). This keeps
    // the button + Enter keyboard-accessible regardless of dropdown state.
    if (e.key === "Enter") {
      if (open && active >= 0 && suggestions[active]) {
        e.preventDefault();
        router.push(`/localities/${suggestions[active].slug}`);
        setOpen(false);
      } else {
        e.preventDefault();
        submitSearch();
      }
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3 shadow-[0_10px_28px_rgba(46,42,34,0.08)] sm:p-4 lg:p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <label htmlFor="natural-search" className="block text-sm font-medium text-zinc-400">
            Search in plain English
          </label>
          <div className="flex items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-base)] p-1">
            {[
              { value: "", label: "All" },
              { value: "residential", label: "Residential" },
              { value: "commercial", label: "Commercial" },
            ].map((opt) => {
              const activeOpt = asset === opt.value;
              return (
                <label
                  key={opt.value}
                  className={`cursor-pointer select-none rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                    activeOpt ? "bg-[var(--accent-primary)] text-[#FAF7F0]" : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                  }`}
                >
                  <input
                    type="radio"
                    name="asset"
                    value={opt.value}
                    defaultChecked={activeOpt}
                    className="sr-only"
                    onChange={() => {
                      if (onSubmit) {
                        onSubmit({ q: value.trim(), asset: opt.value });
                        return;
                      }
                      const params = new URLSearchParams();
                      if (value.trim()) params.set("q", value.trim());
                      if (opt.value) params.set("asset", opt.value);
                      const qs = params.toString();
                      startTransition(() => {
                        router.push(qs ? `/search?${qs}` : "/search");
                      });
                    }}
                  />
                  {opt.label}
                </label>
              );
            })}
          </div>
        </div>
        <div className="relative">
          <Search className="absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-[var(--text-secondary)]" aria-hidden="true" />
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submitSearch();
            }}
          >
            <input
              id="natural-search"
              name="q"
              ref={inputRef}
              type="search"
              value={value}
              autoComplete="off"
              placeholder="e.g. 2 BHK in Dubai Marina budget 100k to 150k"
              className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-base)] py-4 pl-12 pr-24 text-[15px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-secondary)] focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-[var(--accent-primary)] sm:py-5 sm:text-[16px] lg:text-[18px]"
              onChange={(e) => {
                setValue(e.target.value);
                setActive(-1);
                setJustTyped(true);
              }}
              onFocus={() => {
                if (suggestions.length) setOpen(true);
              }}
              onBlur={() => setOpen(false)}
              onKeyDown={onKeyDown}
            />
            <button
              type="submit"
              disabled={isPending}
              className="absolute right-2 top-1/2 inline-flex -translate-y-1/2 items-center gap-1.5 rounded-lg bg-[var(--accent-primary)] px-3 py-2.5 text-xs font-semibold text-[#FAF7F0] transition-all hover:bg-[var(--accent-primary-hover)] disabled:cursor-not-allowed disabled:opacity-80 sm:right-3 sm:gap-2 sm:px-4 sm:text-sm"
            >
              {isPending ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Searching...
                </>
              ) : (
                <>
                  Search
                  <ArrowRight className="h-4 w-4" aria-hidden="true" />
                </>
              )}
            </button>
          </form>
        </div>
        <p className="mt-3 text-xs leading-5 text-[var(--text-secondary)] sm:text-sm">
          Try a locality, building, broker, BHK, or a full request like “2 BHK in Dubai Marina budget 100k to 150k”.
        </p>
      </div>

      {open && suggestions.length > 0 && (
        <ul className="absolute z-20 mt-2 w-full overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] shadow-[0_16px_36px_rgba(46,42,34,0.14)]">
          {suggestions.map((s, i) => (
            <li key={s.slug}>
              <button
                type="button"
                className={`flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm transition-colors ${
                    i === active ? "bg-[var(--accent-soft)] text-[var(--text-primary)]" : "text-[var(--text-secondary)] hover:bg-[var(--bg-surface-hover)]"
                }`}
                onMouseEnter={() => setActive(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  setOpen(false);
                  router.push(`/localities/${s.slug}`);
                }}
              >
                <span className="flex items-center gap-2">
                  <MapPin className="h-4 w-4 text-[var(--accent-forest)]" aria-hidden="true" />
                  {s.locality}
                </span>
                <span className="text-xs text-zinc-500">
                  {s.listingCount.toLocaleString()} listings
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
