import Link from "next/link";
import { Sparkles, MessageSquare } from "lucide-react";
import { describeNaturalSearch, searchNaturalLanguageListings } from "@/lib/natural-search";
import { getAllLocalities } from "@/lib/localities";
import { slugify } from "@/lib/supabase";
import { canonicalLocality } from "@/lib/locality-canon";
import SearchBox from "@/components/SearchBox";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import { ShortlistProvider } from "@/components/ShortlistProvider";
import ShortlistBar from "@/components/ShortlistBar";
import RequirementCapture from "@/components/RequirementCapture";
import SearchResultsView from "@/components/SearchResultsView";
import RelatedSearches from "@/components/RelatedSearches";
import { generateSearchRelated } from "@/lib/related-searches";
import { NOINDEX } from "@/lib/seo";

const GOOGLE_MAPS_API_KEY =
  process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ||
  process.env.GOOGLE_MAPS_API_KEY ||
  null;

export const revalidate = 300;
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Search Listings — PropAI",
  description:
    "Search live WhatsApp broker listings in plain English. Try queries like '3 BHK in Dubai Marina budget 100k to 150k'.",
  robots: NOINDEX,
  alternates: {
    canonical: "/search",
  },
};

type SearchParams = Promise<{ q?: string; asset?: string }>;

async function getSearchLocalities() {
  try {
    return await Promise.race([
      getAllLocalities(),
      new Promise<Awaited<ReturnType<typeof getAllLocalities>>>((_, reject) =>
        setTimeout(() => reject(new Error("Locality lookup timed out")), 8000),
      ),
    ]);
  } catch (err) {
    console.error("getAllLocalities failed:", err);
    return [] as Awaited<ReturnType<typeof getAllLocalities>>;
  }
}

export default async function SearchPage({ searchParams }: { searchParams: SearchParams }) {
  const { q = "", asset: assetParam = "" } = await searchParams;
  const query = q.trim();
  const asset =
    assetParam === "residential" || assetParam === "commercial" ? assetParam : null;

  const knownLocalities = await getSearchLocalities();

  let state: Awaited<ReturnType<typeof searchNaturalLanguageListings>> | null = null;
  let searchError = false;
  if (query) {
    try {
      state = await searchNaturalLanguageListings(query, 24, asset, knownLocalities);
    } catch (err) {
      console.error("searchNaturalLanguageListings failed:", err);
      searchError = true;
    }
  }

  const summary = state?.parsed ? describeNaturalSearch(state.parsed) : "";

  // Candidate search can return a nearby market when the exact locality has
  // limited live inventory. Make that fallback explicit instead of claiming
  // every card belongs to the requested locality.
  const nearbyResultLocalities = state?.parsed.locality
    ? Array.from(new Set(
        state.results
          .map((row) => row.micro_market || row.locality_resolved || row.locality_raw)
          .filter((locality): locality is string => Boolean(locality))
          .filter((locality) => canonicalLocality(locality).slug !== canonicalLocality(state!.parsed.locality!).slug),
      )).slice(0, 3)
    : [];

  let relatedSections: Awaited<ReturnType<typeof generateSearchRelated>> = [];
  if (state?.parsed) {
    try {
      relatedSections = await generateSearchRelated(state.parsed);
    } catch (err) {
      console.error("generateSearchRelated failed:", err);
    }
  }

  const hasResults = Boolean(state);

  return (
    <div className="www-shell min-h-screen">
      <SiteHeader />
      <ShortlistProvider>
      <main className="mx-auto max-w-[1600px] px-4 py-6 sm:px-8 lg:py-12 xl:px-12">
        <header className="max-w-5xl">
          <div className="inline-flex items-center gap-2 rounded-full border border-green-400/20 bg-green-400/10 px-3 py-1 text-xs font-medium text-green-300 mb-4">
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            Natural-language search
          </div>
          <h1 className="max-w-3xl text-[28px] font-bold leading-[1.08] text-white sm:text-[34px] lg:text-[48px]">
            Search live listings the way you actually ask for them.
          </h1>
          <p className="mt-4 text-[15px] lg:text-[18px] text-zinc-400 max-w-3xl">
            Describe the home you want, and PropAI will look across live broker listings, localities, and buildings.
          </p>

          {!query && (
            <div className="mt-8 max-w-2xl">
              <SearchBox query="" asset={assetParam} localities={knownLocalities} />
            </div>
          )}

          {!query && (
            <>
              <div className="mt-8 flex flex-wrap gap-2">
                {[
                  "3 BHK in Dubai Marina budget 100k to 150k",
                  "2 BHK in JVC under AED 80k",
                  "Fully furnished rental in Business Bay",
                  "Offices in Business Bay",
                ].map((example) => (
                  <Link
                    key={example}
                    href={`/search?q=${encodeURIComponent(example)}`}
                    className="rounded-full border border-white/10 bg-zinc-900/70 px-4 py-2 text-sm text-zinc-300 hover:border-green-400/40 hover:text-white transition-colors"
                  >
                    {example}
                  </Link>
                ))}
              </div>

              <div className="mt-8 rounded-2xl border border-white/10 bg-zinc-900/40 p-5 lg:p-6">
                <h2 className="text-sm font-semibold text-white mb-3">Search tips</h2>
                <ul className="grid gap-2 text-sm text-zinc-400 sm:grid-cols-2">
                  <li>• Type the way you&apos;d ask a broker — plain English works.</li>
                  <li>• Add a locality: &ldquo;in Dubai Marina&rdquo;, &ldquo;near JLT&rdquo;.</li>
                  <li>• Set a budget: &ldquo;budget 100k to 150k&rdquo; or &ldquo;under AED 2M&rdquo;.</li>
                  <li>• Specify config: &ldquo;2 BHK&rdquo;, &ldquo;3 BHK furnished&rdquo;.</li>
                  <li>• Pick residential or commercial from the toggle above.</li>
                  <li>• Stuck? Try a building or society name directly.</li>
                </ul>
              </div>
            </>
          )}
        </header>

        {searchError ? (
          <section className="mt-10 rounded-2xl border border-red-400/20 bg-red-400/5 p-6 lg:p-8">
            <h2 className="text-lg font-semibold text-white mb-2">Search is temporarily unavailable</h2>
            <p className="text-sm text-zinc-400">
              We couldn&apos;t complete your search right now. Please try again in a moment, or{" "}
              <Link href="/search" className="text-green-300 hover:text-green-200">
                refresh the page
              </Link>.
            </p>
          </section>
        ) : hasResults ? (
          <section className="mt-10 space-y-6">
            {/* ── No-intent clarification ─────────────────────────── */}
            {state?.noResultsReason === "no_intent" && (
              <div className="rounded-2xl border border-amber-400/20 bg-amber-400/5 p-6 lg:p-8">
                <div className="flex items-start gap-3">
                  <MessageSquare className="h-5 w-5 mt-0.5 shrink-0 text-amber-400" aria-hidden="true" />
                  <div>
                    <h2 className="text-lg font-semibold text-white mb-2">
                      I couldn&apos;t find property criteria in that
                    </h2>
                    <p className="text-sm text-zinc-400 mb-4">
                      Try something like:
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {[
                        "3 BHK in Dubai Marina under AED 2M",
                        "2 BHK for rent in JVC",
                        "Furnished apartment in Business Bay budget 80k to 120k",
                        "Office space in Business Bay",
                      ].map((example) => (
                        <Link
                          key={example}
                          href={`/search?q=${encodeURIComponent(example)}`}
                          className="rounded-full border border-white/10 bg-zinc-900/70 px-4 py-2 text-sm text-zinc-300 hover:border-green-400/40 hover:text-white transition-colors"
                        >
                          {example}
                        </Link>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ── No matches (has intent but no results) ──────────── */}
            {state?.noResultsReason === "no_matches" && (
              <div className="rounded-2xl border border-white/10 bg-zinc-950/80 p-6 lg:p-8">
                <h2 className="text-lg font-semibold text-white mb-2">No matches found</h2>
                <p className="text-sm text-zinc-400">
                  We couldn&apos;t find listings matching your criteria. Try broadening your search —
                  remove the budget filter, try a nearby locality, or check back later when new inventory arrives.
                </p>
              </div>
            )}

            {/* ── Standard results header ─────────────────────────── */}
            {state?.noResultsReason !== "no_intent" && (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                {query && (
                  <>
                    <span className="text-zinc-500">Searching for:</span>
                    <span className="rounded-full border border-white/10 bg-zinc-900 px-3 py-1 text-zinc-200">{query}</span>
                  </>
                )}
                {summary && <span className="text-[var(--text-secondary)]">PropAI understood:</span>}
                {state?.parsed.bhk != null && <span className="rounded-full border border-[var(--accent-primary)] bg-[var(--accent-soft)] px-3 py-1 text-[var(--accent-forest)]">{state.parsed.bhk === 0 ? "Studio" : `${state.parsed.bhk} BHK`}</span>}
                {state?.parsed.asset && <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1 capitalize text-[var(--text-primary)]">{state.parsed.asset}</span>}
                {state?.parsed.intent && <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1 text-[var(--text-primary)]">{state.parsed.intent === "rent" ? "For rent" : "For sale"}</span>}
                {state?.parsed.locality && <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1 text-[var(--text-primary)]">{state.parsed.locality}</span>}
                {state?.parsed.minPrice != null || state?.parsed.maxPrice != null ? <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1 text-[var(--text-primary)]">Budget understood</span> : null}
                {!query && asset && (
                  <span className="text-[var(--text-secondary)]">Showing {asset} listings</span>
                )}
              </div>
            )}

            {state && state.parsed.locality && state.noResultsReason !== "no_intent" && (
              <div className="rounded-2xl border border-white/10 bg-zinc-950/80 p-4 text-sm text-zinc-400">
                {nearbyResultLocalities.length > 0 ? (
                  <>
                    We found matching results for{" "}
                    <Link
                      href={`/localities/${slugify(state.parsed.locality)}`}
                      className="font-medium text-green-300 hover:text-green-200"
                    >
                      {state.parsed.locality}
                    </Link>{" "}
                    and nearby areas, including {nearbyResultLocalities.join(", ")}.
                  </>
                ) : (
                  <>
                    We found matching results in{" "}
                    <Link
                      href={`/localities/${slugify(state.parsed.locality)}`}
                      className="font-medium text-green-300 hover:text-green-200"
                    >
                      {state.parsed.locality}
                    </Link>
                    .
                  </>
                )}
              </div>
            )}

            {state && state.localityUnmatched && (
              <div className="rounded-2xl border border-amber-400/20 bg-amber-400/5 p-4 text-sm text-amber-200/90">
                We couldn&apos;t confirm live coverage for{" "}
                <span className="font-medium text-amber-100">{state.parsed.statedLocalityText || "that locality"}</span>{" "}
                yet, so no listings were returned for that locality.
                {state.localitySuggestions.length > 0 && (
                  <>
                    <span className="block mt-2">Try one of these covered localities:</span>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {state.localitySuggestions.map((loc) => (
                        <Link
                          key={loc.slug}
                          href={`/localities/${loc.slug}`}
                          className="rounded-full border border-white/10 bg-zinc-900 px-3 py-1 text-xs text-zinc-200 hover:border-green-400/40 hover:text-white transition-colors"
                        >
                          {loc.locality}
                        </Link>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}

            {state && state.results.length > 0 ? (
              <>
                <SearchResultsView
                  results={state.results}
                  googleMapsApiKey={GOOGLE_MAPS_API_KEY}
                />
                {relatedSections.length > 0 && (
                  <RelatedSearches sections={relatedSections} />
                )}
              </>
            ) : state?.noResultsReason !== "no_intent" && (
              <div className="grid grid-cols-1 lg:grid-cols-[3fr_1fr] gap-6">
                <RequirementCapture query={query} />

                <aside className="rounded-3xl border border-white/10 bg-zinc-950/80 p-6 lg:p-8">
                  <h2 className="text-lg font-semibold text-white mb-3">What happens next</h2>
                  <p className="text-sm text-zinc-400">
                    We keep the request attached to your timeline and follow up when matching inventory appears.
                  </p>
                  <div className="mt-6 rounded-2xl border border-white/10 bg-black/70 p-4 text-sm text-zinc-400">
                    If a match lands inside your timeline, the requirement can be routed to a broker and/or sent back to you for follow-up.
                  </div>
                </aside>
              </div>
            )}
            {relatedSections.length > 0 && state && state.results.length === 0 && state.noResultsReason !== "no_intent" && (
              <RelatedSearches sections={relatedSections} />
            )}
          </section>
        ) : (
          <section className="mt-10 grid grid-cols-1 lg:grid-cols-3 gap-4 lg:gap-6">
            <div className="rounded-3xl border border-white/10 bg-zinc-950/80 p-6 lg:p-8 lg:col-span-2">
              <h2 className="text-xl font-semibold text-white mb-3">What you can ask for</h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm text-zinc-300">
                {[
                  "3 BHK in Dubai Marina budget 100k to 150k",
                  "2 BHK rental in Business Bay fully furnished",
                  "Office in Business Bay under 5M",
                  "Listings near JLT West with 2 bathrooms",
                ].map((example) => (
                  <div key={example} className="rounded-2xl border border-white/10 bg-black/70 px-4 py-3">
                    {example}
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-white/10 bg-zinc-950/80 p-6 lg:p-8">
              <h2 className="text-lg font-semibold text-white mb-3">Live coverage</h2>
              <p className="text-sm text-zinc-400">
                Search across the live WhatsApp inventory backing the public locality pages.
              </p>
              <div className="mt-5 text-sm text-zinc-500">
                {knownLocalities.length > 0 ? (
                  <>
                    <div>{knownLocalities.length.toLocaleString()} localities tracked</div>
                    <div className="mt-1">{knownLocalities.slice(0, 3).map((l) => l.locality).join(" • ")}</div>
                  </>
                ) : (
                  <div>Loading live localities...</div>
                )}
              </div>
            </div>
          </section>
        )}
      </main>
      <ShortlistBar />
      </ShortlistProvider>
      <SiteFooter />
    </div>
  );
}
