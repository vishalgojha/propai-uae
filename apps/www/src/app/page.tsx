// The homepage aggregates live WhatsApp inventory (locality/building/listing/
// broker counts + recent activity) that updates gradually. A few minutes of
// staleness is fine and avoids re-scanning the DB on every request (and any
// CDN/proxy caching). ISR re-renders every 5 minutes, so the public counters
// stay dynamic without re-querying on each visit.
export const revalidate = 300;
// The homepage reads live Supabase data. Keep it out of Docker image build
// time; ISR/SSR should populate it when the running service has its runtime
// database credentials and network access.
export const dynamic = "force-dynamic";

import { MapPin, MessageSquare, Phone, Shield } from "lucide-react";
import Link from "next/link";
import HomeSearch from "@/components/HomeSearch";
import LiveListingTicker from "@/components/LiveListingTicker";
import SiteHeader from "@/components/SiteHeader";
import { NoPhotosFaqJsonLd } from "@/components/NoPhotosFaq";
import SiteFooter from "@/components/SiteFooter";
import { ShortlistProvider } from "@/components/ShortlistProvider";
import ShortlistBar from "@/components/ShortlistBar";
import { buildListingSlug, formatBhkNumber } from "@/lib/listing-card";
import { formatPublicPrice, getPublicDataOverview, type PublicDataOverview } from "@/lib/public-data";
import CountUp from "@/components/CountUp";
import ScrollReveal from "@/components/ScrollReveal";

const howItWorksSteps = [
  {
    number: "01",
    title: "Browse listings",
    description: "Explore verified properties in your locality. Every listing comes from active WhatsApp broker conversations.",
  },
  {
    number: "02",
    title: "Send an enquiry",
    description: "Tap 'Enquire' on any listing. Your details go straight to the broker on WhatsApp — no forms, no spam.",
  },
  {
    number: "03",
    title: "Message the broker",
    description: "Continue the conversation directly with the broker on WhatsApp — no forms, no spam. Real person, not a chatbot.",
  },
];

function withHomepageTimeout<T>(promise: Promise<T>, timeoutMs = 10000): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Homepage data query timed out")), timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

export default async function WWWPage() {
  // The landing page must remain available even when Supabase is temporarily
  // unreachable. Live values are rendered when the query succeeds; an empty
  // overview gives the page an honest, crawlable empty state when it does not.
  let overview: PublicDataOverview;
  const overviewPromise = Promise.resolve().then(() =>
    getPublicDataOverview({ skipBuildingScan: true, skipCounts: true, skipLocalities: true, skipActivity: true }),
  );
  const overviewResult = await Promise.allSettled([
    withHomepageTimeout(overviewPromise),
  ]).then(([result]) => result);
  if (overviewResult.status === "fulfilled") overview = overviewResult.value;
  else {
    console.error("Homepage overview query failed:", overviewResult.reason);
    overview = {
      counts: {
        localities: 0,
        buildings: 0,
        listings: 0,
        activeListings: 0,
        brokers: 0,
        raw_messages: 0,
        messagesAnalysed: 0,
      },
      countsAvailable: false,
      activity: [],
      topLocalities: [],
      topBuildings: [],
      recentListings: [],
    };
  }
  const trustStats = [
    ["Active listings", overview.counts.activeListings],
    ["Active brokers", overview.counts.brokers],
    ["Localities covered", overview.counts.localities],
    ["Messages analysed", overview.counts.messagesAnalysed],
  ] as const;
  const glanceStats = [
    ["Localities", overview.counts.localities],
    ["Buildings", overview.counts.buildings],
    ["Active listings", overview.counts.activeListings],
    ["Total listings", overview.counts.listings],
    ["Brokers", overview.counts.brokers],
    ["Messages analysed", overview.counts.messagesAnalysed],
  ] as const;

  return (
    <div className="www-shell min-h-screen">
      <SiteHeader />
      <NoPhotosFaqJsonLd />

      <main id="main-content">
       <ShortlistProvider>
        <section className="www-hero relative overflow-hidden">
          <div className="www-hero-glow" aria-hidden="true" />
          <div className="max-w-[1240px] mx-auto px-4 lg:px-8 relative">
            <div className="www-hero-grid">
              <div className="www-hero-copy">
                <div className="www-eyebrow"><span aria-hidden="true" /> Real estate intelligence from live broker activity</div>
                <h1 className="text-[36px] lg:text-[68px] leading-[1.02] font-semibold tracking-[-0.045em] text-white">
                  Find the property <span className="www-gradient-text">before it hits a portal.</span>
                </h1>
                <p className="mt-6 text-[17px] lg:text-[19px] leading-8 text-zinc-400 max-w-xl">
                  Search active WhatsApp broker conversations, see what is fresh, and go straight to the person who shared it.
                </p>
                <div className="mt-9 max-w-2xl">
                  <HomeSearch localities={overview.topLocalities} />
                  <p className="mt-3 text-sm text-zinc-500">Try “2 BHK in Dubai Marina” or search a locality, building, or budget.</p>
                </div>
              </div>

              <aside className="www-market-board" aria-label="Live PropAI market pulse">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <span className="www-panel-label">MARKET PULSE</span>
                    <h2 className="mt-2 text-xl font-semibold">Fresh from the network</h2>
                  </div>
                  <span className="www-live-dot"><span aria-hidden="true" /> Live</span>
                </div>
                <div className="www-market-board-rule" />
                {overview.recentListings.length > 0 ? (
                  <div className="www-market-board-list">
                    {overview.recentListings.slice(0, 3).map((row) => {
                      const textValue = (value: unknown) => typeof value === "string" ? value.trim() : "";
                      const title = [row.building_name, row.summary_title, row.location_label, row.micro_market]
                        .map(textValue)
                        .find(Boolean) || "Fresh property";
                      const slug = buildListingSlug({
                        id: row.id,
                        bhk: row.bhk,
                        micro_market: row.micro_market,
                        building_name: row.building_name,
                        property_type: row.property_type,
                      }) ?? String(row.id);
                      return (
                        <Link key={`${row.card_type ?? "listing"}-${row.id}`} href={`/listings/${slug}/${row.id}`} className="www-market-board-row">
                          <span className="www-market-board-index">{textValue(row.micro_market) || "Dubai"}</span>
                          <span className="www-market-board-title">{title}</span>
                          <span className="www-market-board-arrow" aria-hidden="true">↗</span>
                        </Link>
                      );
                    })}
                  </div>
                ) : (
                  <div className="www-market-board-empty">Live inventory appears here as broker conversations are indexed.</div>
                )}
                <Link href="/search" className="www-market-board-link">Explore live inventory <span aria-hidden="true">→</span></Link>
              </aside>
            </div>

            <LiveListingTicker />

            <div className="www-feature-grid" aria-label="PropAI benefits">
              {[
                {
                  icon: MessageSquare,
                  title: "Direct to broker",
                  description: "Your enquiry lands on the broker's WhatsApp instantly — no middlemen, no delays.",
                },
                {
                  icon: Shield,
                  title: "Freshness guaranteed",
                  description: "Listings update daily from live conversations. Stale data is auto-hidden after 30 days.",
                },
                {
                  icon: Phone,
                  title: "Real brokers, real conversations",
                  description: "No chatbots. Every enquiry goes to a verified broker on WhatsApp.",
                },
              ].map((item, i) => (
                <div
                  key={i}
                  className="www-feature-card transition-all duration-base hover:border-green-400/30 hover:-translate-y-0.5"
                  data-scroll-reveal
                  style={{ transitionDelay: `${i * 100}ms` } as React.CSSProperties}
                >
                  <item.icon className="w-6 h-6 text-green-400 mb-4" aria-hidden="true" />
                  <h3 className="text-lg font-semibold text-white mb-2">{item.title}</h3>
                  <p className="text-[15px] text-zinc-400">{item.description}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="py-14 lg:py-20 border-b border-white/5">
          <div className="max-w-[1600px] mx-auto px-4 lg:px-6">
            <p className="text-center text-sm text-zinc-500 mb-8">
              Fresh properties from active broker conversations
            </p>
            {overview.countsAvailable && trustStats.some(([, value]) => value > 0) && (
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-5 lg:gap-8 max-w-4xl mx-auto">
                {trustStats.filter(([, value]) => value > 0).map(([label, value]) => (
                  <TrustStat key={label} label={label} value={value} />
                ))}
              </div>
            )}
            {!overview.countsAvailable && !trustStats.some(([, value]) => value > 0) && (
              <p className="text-center text-sm text-zinc-500">
                Browse current properties and contact the broker directly on WhatsApp.
              </p>
            )}
          </div>
        </section>

        <section id="live-data" className="py-16 lg:py-24 bg-zinc-950/60 border-y border-white/5" data-scroll-reveal>
          <div className="max-w-[1600px] mx-auto px-4 lg:px-6">
            <div className="text-center mb-10 lg:mb-12" data-scroll-reveal>
              <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-4">Explore fresh inventory</h2>
              <p className="text-[15px] text-zinc-400 max-w-2xl mx-auto">
                Browse current properties sourced from active broker conversations and reach the broker directly on WhatsApp.
              </p>
            </div>

            {overview.countsAvailable && glanceStats.some(([, value]) => value > 0) && (
              <div className="www-stats-strip grid grid-cols-2 lg:grid-cols-6 gap-3 lg:gap-4 mb-6">
                {glanceStats.filter(([, value]) => value > 0).map(([label, value]) => (
                  <div key={label as string} className="rounded-2xl border border-white/10 bg-black/70 p-4" data-scroll-reveal>
                    <div className="text-3xl font-bold text-white">
                      <CountUp end={value as number} duration={1800} locale="en-AE" />
                    </div>
                    <div className="mt-1 text-[10px] uppercase tracking-wider text-zinc-500">{label as string}</div>
                  </div>
                ))}
              </div>
            )}

            {overview.topLocalities.length > 0 && <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 lg:gap-6">
              <div className="rounded-3xl border border-white/10 bg-black/70 p-5 lg:p-6">
                <div className="flex items-center justify-between gap-3 mb-4">
                  <div>
                    <h3 className="text-lg font-semibold text-white">Top localities</h3>
                    <p className="text-sm text-zinc-500">By live listing count</p>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {overview.topLocalities.slice(0, 4).map((loc) => (
                    <Link
                      key={loc.slug}
                      href={`/localities/${loc.slug}`}
                      className="rounded-2xl border border-white/10 bg-zinc-950/80 p-4 hover:border-green-400/30 hover:bg-zinc-900 transition-colors"
                    >
                      <div className="text-white font-medium">{loc.locality}</div>
                      <div className="mt-1 text-sm text-zinc-500">{loc.listingCount} active listing{loc.listingCount === 1 ? "" : "s"}</div>
                    </Link>
                  ))}
                </div>
              </div>

            </div>}

            {overview.recentListings.length > 0 && (
              <div className="www-listing-section mt-6 rounded-3xl border border-white/10 bg-black/70 p-5 lg:p-6">
                <div className="mb-4 flex items-end justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-semibold text-white">Latest 6 listings</h3>
                    <p className="text-sm text-zinc-500">Fresh inventory from the live WhatsApp feed</p>
                  </div>
                </div>
                <div className="www-listing-list">
                  {overview.recentListings.slice(0, 6).map((row) => {
                    const textValue = (value: unknown) => typeof value === "string" ? value.trim() : "";
                    const sourceTitle = row.source_text?.split(/\r?\n/).map((line) => line.replace(/[*_]/g, "").trim()).find(Boolean);
                    const title = [row.building_name, row.landmark_name, row.summary_title, sourceTitle, row.location_label, row.micro_market]
                      .map(textValue)
                      .find(Boolean) || "Listing";
                    const slug = buildListingSlug({
                      id: row.id,
                      bhk: row.bhk,
                      micro_market: row.micro_market,
                      building_name: row.building_name,
                      property_type: row.property_type,
                    }) ?? String(row.id);
                    const price = formatPublicPrice(row.price, row.price_unit);
                    const furnishing = textValue(row.furnishing).replace(/[_-]+/g, " ");
                    const spec = [row.bhk ? formatBhkNumber(row.bhk) : "", furnishing].filter(Boolean).join(" · ");
                    const lastSeen = row.last_seen ? new Date(row.last_seen) : null;
                    const updatedLabel = lastSeen && !Number.isNaN(lastSeen.getTime())
                      ? `Updated ${lastSeen.toLocaleDateString("en-AE", { day: "numeric", month: "short" })}`
                      : "Updated recently";
                    return (
                      <Link
                        key={`${row.card_type ?? "listing"}-${row.id}`}
                        href={`/listings/${slug}/${row.id}`}
                        className="www-listing-row transition-colors hover:border-green-400/30"
                      >
                        <div className="www-listing-primary">
                          <div className="text-sm font-medium text-white line-clamp-2">{title}</div>
                          <div className="mt-1 text-xs text-zinc-500">
                            {textValue(row.micro_market) || "Dubai"}{textValue(row.broker_name) ? ` · ${textValue(row.broker_name)}` : ""}
                          </div>
                        </div>
                        <div className="www-listing-price text-sm font-semibold text-green-300">
                          <div>{price}</div>
                          {spec && <div className="mt-1 text-xs font-normal text-zinc-400">{spec}</div>}
                        </div>
                        <div className="www-listing-meta text-xs text-zinc-500">
                          {updatedLabel}
                        </div>
                      </Link>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </section>

        {overview.topLocalities.length > 0 && <section id="localities" className="py-16 lg:py-24 bg-zinc-950/50">
          <div className="max-w-[1600px] mx-auto px-4 lg:px-6">
            <div className="text-center mb-12 lg:mb-16">
              <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-4">Browse by locality</h2>
              <p className="text-[15px] text-zinc-400 max-w-2xl mx-auto">
                Every locality page shows live listings, price trends, and broker activity — all sourced from live WhatsApp conversations.
              </p>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 lg:gap-6">
              {overview.topLocalities.length > 0 && overview.topLocalities.slice(0, 8).map((loc) => {
                const slug = loc.slug;
                const name = loc.locality;
                const listingCount = loc.listingCount;
                return (
                  <Link
                    key={slug}
                    href={`/localities/${slug}`}
                    className="group bg-zinc-900/50 border border-white/10 rounded-xl p-5 lg:p-6 transition-colors hover:border-green-400/50 hover:bg-zinc-900"
                  >
                    <div className="flex flex-col h-full">
                      <div className="flex items-start justify-between gap-2 mb-3">
                        <h3 className="text-lg font-semibold text-white group-hover:text-green-400 transition-colors">{name}</h3>
                      </div>
                      <p className="text-xs text-zinc-500 mt-auto flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-400" aria-hidden="true" />
                        {listingCount} active listing{listingCount === 1 ? "" : "s"}
                      </p>
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        </section>}

        <section id="how-it-works" className="py-16 lg:py-24 bg-black" data-scroll-reveal>
          <div className="max-w-[1600px] mx-auto px-4 lg:px-6">
            <div className="text-center mb-12 lg:mb-16" data-scroll-reveal>
              <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-4">How it works</h2>
              <p className="text-[15px] text-zinc-400 max-w-2xl mx-auto">
                Three simple steps — no apps to download, no accounts to create.
              </p>
            </div>

            <div className="www-steps-grid grid grid-cols-1 md:grid-cols-3 gap-6 lg:gap-8">
              {howItWorksSteps.map((step, i) => (
                <div
                  key={i}
                  className="www-step relative bg-zinc-900/50 border border-white/10 rounded-xl p-6 lg:p-8"
                  data-scroll-reveal
                  style={{ transitionDelay: `${i * 100}ms` } as React.CSSProperties}
                >
                  <span className="text-4xl font-bold text-green-400/20 mb-4 block">{step.number}</span>
                  <h3 className="text-lg font-semibold text-white mb-3">{step.title}</h3>
                  <p className="text-[15px] text-zinc-400">{step.description}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="py-16 lg:py-24 bg-zinc-950/50" data-scroll-reveal>
          <div className="max-w-[1600px] mx-auto px-4 lg:px-6">
            <div className="text-center mb-12 lg:mb-16" data-scroll-reveal>
              <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-4">Why PropAI?</h2>
              <p className="text-[15px] text-zinc-400 max-w-2xl mx-auto">
                We don't scrape portals. We read the source — live WhatsApp conversations between brokers and buyers.
              </p>
            </div>

            <div className="www-why-grid grid grid-cols-1 md:grid-cols-3 gap-6 lg:gap-8">
              {[
                {
                  icon: MessageSquare,
                  title: "Direct to broker",
                  description: "Your enquiry lands on the broker's WhatsApp instantly — no middlemen, no delays.",
                },
                {
                  icon: Shield,
                  title: "Freshness guaranteed",
                  description: "Listings update daily from live conversations. Stale data is auto-hidden after 30 days.",
                },
                {
                  icon: Phone,
                  title: "Real brokers, real conversations",
                  description: "No chatbots. Every enquiry goes to a verified broker on WhatsApp.",
                },
              ].map((item, i) => (
                <div
                  key={i}
                  className="www-why-item transition-all duration-base hover:border-green-400/30"
                  data-scroll-reveal
                  style={{ transitionDelay: `${i * 100}ms` } as React.CSSProperties}
                >
                  <item.icon className="w-6 h-6 text-green-400 mb-4" aria-hidden="true" />
                  <h3 className="text-lg font-semibold text-white mb-2">{item.title}</h3>
                  <p className="text-[15px] text-zinc-400">{item.description}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="no-photos" className="py-16 lg:py-24 bg-black" data-scroll-reveal>
          <div className="max-w-3xl mx-auto px-4 lg:px-6">
            <div className="www-note-panel bg-zinc-900/50 border border-white/10 rounded-xl p-6 lg:p-8">
              <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-3">
                Why we skip photos on purpose
              </h2>
              <p className="text-[15px] text-zinc-400 leading-relaxed">
                This inventory moves fast. Message the broker directly and they&apos;ll
                send you real, current photos and videos over WhatsApp — not stock
                images from whenever the listing was first posted. Pre-loading static
                photos would misrepresent what&apos;s actually available today, so we
                keep the page fast and the media fresh, straight from the source.
              </p>
            </div>
          </div>
        </section>

       <ShortlistBar />
       </ShortlistProvider>
      </main>

      <SiteFooter />
      <ScrollReveal />
    </div>
  );
}

function TrustStat({
  label,
  value,
  suffix,
}: {
  label: string;
  value: number;
  suffix?: string;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-zinc-900/50 p-6 lg:p-8 text-center" data-scroll-reveal>
      <div className="text-3xl lg:text-4xl font-bold text-white leading-none">
        <CountUp end={value} duration={1800} locale="en-AE" suffix={suffix} />
      </div>
      <div className="mt-3 text-xs lg:text-sm text-zinc-400">{label}</div>
    </div>
  );
}
