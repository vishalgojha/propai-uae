import type { Metadata } from "next";
import { cache } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import {
  MapPin,
  Building2,
  TrendingUp,
  Clock,
  Search,
  MessageSquare,
  ArrowRight,
} from "lucide-react";
import {
  getBuildingBySlug,
  getBuildingListings,
  type BuildingListing,
} from "@/lib/localities";
import { toListingCardViewModel, type ListingCardFields } from "@/lib/listing-card";
import { slugify, getServerSupabase } from "@/lib/supabase";
import { buildingTitle, buildingDescription } from "@/lib/seo-copy";
import { JsonLd, getSiteUrl } from "@/lib/seo";
import {
  computeHeroStats,
  generateBuildingSummary,
  getSimilarBuildings,
  getLocalityListingCount,
  getNearbyLocalities,
  getNearbyLandmarks,
  getPopularSearches,
  computeMarketInsights,
  getNearbyBuildings,
  buildBuildingBreadcrumb,
} from "@/lib/building-intelligence";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import ListingTile from "@/components/ListingTile";
import { ShortlistProvider } from "@/components/ShortlistProvider";

type Params = { params: Promise<{ slug: string }> };

const getBuildingBySlugCached = cache(getBuildingBySlug);
const getBuildingListingsCached = cache(getBuildingListings);

export const revalidate = 300;

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { slug } = await params;
  const building = await getBuildingBySlugCached(slug);
  if (!building) return { title: "Building not found — PropAI" };
  const listings = await getBuildingListingsCached(building.name, building.microMarket);
  let saleCount = 0;
  let rentCount = 0;
  for (const l of listings) {
    const i = (l.intent || "").toLowerCase();
    if (i === "rent" || i === "rental" || i === "lease") rentCount += 1;
    else if (i === "sale" || i === "sell" || i === "buy") saleCount += 1;
  }
  return {
    title: buildingTitle(building.name),
    description: buildingDescription({
      name: building.name,
      locality: building.microMarket,
      listingCount: listings.length,
      saleCount,
      rentCount,
    }),
  };
}

function toCardFields(row: BuildingListing): ListingCardFields {
  return {
    id: row.id,
    bhk: row.bhk,
    price: row.price,
    price_unit: row.price_unit,
    price_model: row.price_model,
    price_per_sqft: row.price_per_sqft,
    area_sqft: null,
    furnishing: row.furnishing,
    intent: row.intent,
    asset_type: row.asset_type,
    property_type: row.property_type,
    micro_market: null,
    building_name: null,
    landmark_name: null,
    location_label: null,
    broker_name: row.broker_name,
    broker_phone: row.broker_phone,
    last_seen: row.last_seen,
  };
}

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-AE", { day: "numeric", month: "short", year: "numeric" });
}

function RelatedLinks({
  heading,
  links,
  icon,
}: {
  heading: string;
  links: Array<{ label: string; href: string }>;
  icon?: React.ReactNode;
}) {
  if (!links || links.length === 0) return null;
  return (
    <section>
      <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
        {icon}
        {heading}
      </h2>
      <div className="flex flex-wrap gap-2">
        {links.map((link) => (
          <Link
            key={link.href + link.label}
            href={link.href}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-[13px] text-zinc-300 hover:border-green-400/30 hover:text-green-200 hover:bg-green-400/5 transition-all"
          >
            {link.label}
            <ArrowRight className="h-3 w-3 opacity-50" aria-hidden="true" />
          </Link>
        ))}
      </div>
    </section>
  );
}

function StatBlock({
  label,
  value,
  icon,
}: {
  label: string;
  value: string | null;
  icon?: React.ReactNode;
}) {
  if (!value) return null;
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 px-4 py-3">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-zinc-500 mb-1">
        {icon}
        {label}
      </div>
      <div className="text-lg font-semibold text-white">{value}</div>
    </div>
  );
}

export default async function BuildingPage({ params }: Params) {
  const { slug } = await params;
  const building = await getBuildingBySlugCached(slug);
  if (!building) notFound();

  const listings = await getBuildingListingsCached(building.name, building.microMarket);

  const siteUrl = getSiteUrl();
  const stats = computeHeroStats(listings);
  const summary = generateBuildingSummary(building, listings, stats);
  const marketInsights = computeMarketInsights(listings);

  // Parallel section data fetching
  const [
    similarBuildings,
    localityCount,
    nearbyLocalities,
    nearbyLandmarks,
  ] = await Promise.all([
    getSimilarBuildings(building.name, building.microMarket),
    getLocalityListingCount(building.microMarket),
    getNearbyLocalities(building.microMarket),
    getNearbyLandmarks(building.microMarket),
  ]);
  const nearbyBuildings = similarBuildings;

  const popularSearches = getPopularSearches(building.microMarket, stats.bhkRange);

  const bhkRange = stats.bhkRange || null;
  const verifiedAddress = building.enrichmentConfidence != null && building.enrichmentConfidence >= 0.99;

  const breadcrumbSchema = buildBuildingBreadcrumb(siteUrl, building.name, building.microMarket);

  const buildingJsonLd = {
    "@context": "https://schema.org",
    "@type": "Residence",
    name: building.name,
    url: `${siteUrl}/buildings/${slug}`,
    address: verifiedAddress && building.address
      ? {
          "@type": "PostalAddress",
          streetAddress: building.address,
          addressLocality: building.microMarket || "Dubai",
          addressRegion: "MH",
          addressCountry: "IN",
        }
      : undefined,
    ...(building.developer
      ? { developer: { "@type": "Organization", name: building.developer } }
      : {}),
    ...(stats.listingCount > 0
      ? {
          numberOfAvailableUnits: stats.listingCount,
        }
      : {}),
  };

  return (
    <ShortlistProvider>
      <div className="min-h-screen bg-black text-white">
        <SiteHeader />
        <JsonLd data={breadcrumbSchema} />
        <JsonLd data={buildingJsonLd} />

        <main className="max-w-[1600px] mx-auto px-4 lg:px-6 py-10 lg:py-14">

          {/* Breadcrumb */}
          <nav className="flex items-center gap-1.5 text-[13px] text-zinc-500 mb-8" aria-label="Breadcrumb">
            <Link href="/" className="hover:text-white transition-colors">Home</Link>
            <span>/</span>
            <Link href="/localities" className="hover:text-white transition-colors">Dubai</Link>
            {building.microMarket && (
              <>
                <span>/</span>
                <Link
                  href={`/localities/${slugify(building.microMarket)}`}
                  className="hover:text-white transition-colors"
                >
                  {building.microMarket}
                </Link>
              </>
            )}
            <span>/</span>
            <span className="text-zinc-300">{building.name}</span>
          </nav>

          {/* Hero Section */}
          <header className="mb-12">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-3">
                  {building.name}
                </h1>
                <div className="flex flex-wrap items-center gap-2">
                  {building.microMarket && (
                    <Link
                      href={`/localities/${slugify(building.microMarket)}`}
                      className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400 hover:border-green-400/30 hover:text-green-200 transition-colors"
                    >
                      <MapPin className="h-3.5 w-3.5" aria-hidden="true" />
                      {building.microMarket}
                    </Link>
                  )}
                  {stats.listingCount > 0 && (
                    <span className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400">
                      <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
                      {stats.listingCount} listing{stats.listingCount === 1 ? "" : "s"}
                    </span>
                  )}
                  {stats.avgPricePerSqft && (
                    <span className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400">
                      <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
                      {stats.avgPricePerSqft}
                    </span>
                  )}
                </div>
              </div>
            </div>

            {verifiedAddress && building.address && (
              <p className="mt-5 text-[15px] lg:text-[17px] text-zinc-400 max-w-2xl">
                {building.address}
              </p>
            )}
            {building.developer && (
              <p className="mt-1 text-sm text-zinc-500">Developer: {building.developer}</p>
            )}
          </header>

          {/* Stats Row */}
          {stats.listingCount > 0 && (
            <section className="mb-12 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
              <StatBlock label="Total Listings" value={stats.listingCount.toLocaleString("en-AE")} icon={<Building2 className="h-3 w-3" />} />
              <StatBlock label="Avg Rent" value={stats.avgRent} icon={<TrendingUp className="h-3 w-3" />} />
              <StatBlock label="Avg Sale Price" value={stats.avgSalePrice} icon={<TrendingUp className="h-3 w-3" />} />
              <StatBlock label="BHK Range" value={stats.bhkRange} icon={<Building2 className="h-3 w-3" />} />
              <StatBlock label="Last Updated" value={formatDate(stats.lastUpdated)} icon={<Clock className="h-3 w-3" />} />
            </section>
          )}

          {/* About the Building */}
          <section className="mb-12 max-w-3xl">
            <h2 className="text-lg font-semibold text-white mb-3">About {building.name}</h2>
            <p className="text-[15px] leading-relaxed text-zinc-400">{summary}</p>
          </section>

          {(verifiedAddress && building.address) || building.developer ? (
            <section className="mb-12 max-w-3xl rounded-xl border border-white/10 bg-white/[0.03] p-5">
              <h2 className="text-lg font-semibold text-white mb-4">Verified building details</h2>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                {verifiedAddress && building.address && (
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-zinc-500">Address</dt>
                    <dd className="mt-1 text-zinc-300">{building.address}</dd>
                  </div>
                )}
                {building.developer && (
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-zinc-500">Developer</dt>
                    <dd className="mt-1 text-zinc-300">{building.developer}</dd>
                  </div>
                )}
              </dl>
            </section>
          ) : null}

          {/* Listings */}
          <section className="mb-12">
            <h2 className="text-xl font-semibold text-white mb-6">
              {listings.length > 0
                ? `${listings.length} Active Listing${listings.length === 1 ? "" : "s"}`
                : "No Active Listings Yet"}
            </h2>

            {listings.length === 0 ? (
              <div className="rounded-xl border border-white/10 bg-white/5 p-8 text-center">
                <p className="text-zinc-400">
                  No broker activity has been tracked for {building.name} yet. Listings appear
                  automatically as soon as brokers post in our WhatsApp network.
                </p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-4 lg:gap-6">
                {listings.map((row) => {
                  const card = toListingCardViewModel(toCardFields(row), false, building.microMarket);
                  return <ListingTile key={row.id} card={card} buildingName={building.name} />;
                })}
              </div>
            )}
          </section>

          {/* Similar Buildings Nearby */}
          {similarBuildings.length > 0 && (
            <section className="mb-12">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <Building2 className="h-5 w-5 text-zinc-500" />
                Similar Buildings in {building.microMarket}
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {similarBuildings.map((b) => (
                  <Link
                    key={b.slug}
                    href={`/buildings/${b.slug}`}
                    className="flex items-center justify-between rounded-xl border border-white/10 bg-white/5 px-4 py-3 hover:border-green-400/30 hover:bg-green-400/5 transition-all group"
                  >
                    <div>
                      <div className="text-sm font-medium text-white group-hover:text-green-200 transition-colors">{b.name}</div>
                      <div className="text-[12px] text-zinc-500">
                        {b.listingCount} listing{b.listingCount === 1 ? "" : "s"}
                        {b.avgPrice && (
                          <span className="ml-1.5">
                            · {formatPrice(b.avgPrice)} {b.priceUnit || ""}
                          </span>
                        )}
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 text-zinc-600 group-hover:text-green-400 transition-colors" />
                  </Link>
                ))}
              </div>
            </section>
          )}

          {/* More Properties in Locality */}
          {building.microMarket && localityCount > 0 && (
            <section className="mb-12">
              <h2 className="text-lg font-semibold text-white mb-3">
                More Properties in {building.microMarket}
              </h2>
              <Link
                href={`/localities/${slugify(building.microMarket)}`}
                className="inline-flex items-center gap-2 rounded-xl border border-white/10 bg-white/5 px-5 py-3 text-sm text-zinc-300 hover:border-green-400/30 hover:text-green-200 hover:bg-green-400/5 transition-all group"
              >
                View all {localityCount.toLocaleString("en-AE")} listings in {building.microMarket}
                <ArrowRight className="h-4 w-4 group-hover:translate-x-0.5 transition-transform" />
              </Link>
            </section>
          )}

          {/* Related Links: Nearby Localities, Landmarks, Popular Searches */}
          <div className="space-y-8 mb-12">
            <RelatedLinks
              heading="Nearby Localities"
              links={nearbyLocalities}
              icon={<MapPin className="h-5 w-5 text-zinc-500" />}
            />
            <RelatedLinks
              heading="Nearby Landmarks"
              links={nearbyLandmarks}
              icon={<MapPin className="h-5 w-5 text-zinc-500" />}
            />
            <RelatedLinks
              heading="Popular Searches"
              links={popularSearches}
              icon={<Search className="h-5 w-5 text-zinc-500" />}
            />
          </div>

          {/* Market Insights */}
          {marketInsights.length > 0 && (
            <section className="mb-12">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <TrendingUp className="h-5 w-5 text-zinc-500" />
                Market Insights
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                {marketInsights.map((insight) => (
                  <div
                    key={insight.label}
                    className="rounded-xl border border-white/10 bg-white/5 px-4 py-3"
                  >
                    <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">{insight.label}</div>
                    <div className="text-base font-semibold text-white">{insight.value}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* People Also Viewed */}
          {nearbyBuildings.length > 0 && (
            <section className="mb-12">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <Building2 className="h-5 w-5 text-zinc-500" />
                People Also Viewed
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {nearbyBuildings.map((b) => (
                  <Link
                    key={b.slug}
                    href={`/buildings/${b.slug}`}
                    className="flex items-center justify-between rounded-xl border border-white/10 bg-white/5 px-4 py-3 hover:border-green-400/30 hover:bg-green-400/5 transition-all group"
                  >
                    <div>
                      <div className="text-sm font-medium text-white group-hover:text-green-200 transition-colors">{b.name}</div>
                      <div className="text-[12px] text-zinc-500">
                        {b.listingCount} listing{b.listingCount === 1 ? "" : "s"}
                        {b.avgPrice && (
                          <span className="ml-1.5">
                            · {formatPrice(b.avgPrice)} {b.priceUnit || ""}
                          </span>
                        )}
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 text-zinc-600 group-hover:text-green-400 transition-colors" />
                  </Link>
                ))}
              </div>
            </section>
          )}

        </main>
        <SiteFooter />
      </div>
    </ShortlistProvider>
  );
}

function formatPrice(price: number): string {
  if (price >= 1_000_000) return `AED ${(price / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (price >= 10_000) return `AED ${Math.round(price / 1000)}k`;
  return `AED ${Math.round(price).toLocaleString("en-AE")}`;
}
