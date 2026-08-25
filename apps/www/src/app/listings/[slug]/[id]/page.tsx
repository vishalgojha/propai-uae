import type { Metadata } from "next";
import Link from "next/link";
import { cache } from "react";
import { notFound, redirect } from "next/navigation";
import { JsonLd, buildRealEstateListing, buildBreadcrumb, getSiteUrl } from "@/lib/seo";
import { listingTitle, listingDescription } from "@/lib/seo-copy";
import {
  MapPin,
  MessageSquare,
  ShieldCheck,
  Clock,
  BedDouble,
  Ruler,
  Sofa,
  Building2,
  Eye,
  Flag,
  Target,
  ChevronRight,
  ChevronDown,
  Tag,
} from "lucide-react";
import { getListingById, getBrokerAreas, getBuildingBrokers, getSimilarListingsForDetail, getSimilarListingsForExpired } from "@/lib/localities";
import { slugify } from "@/lib/supabase";
import {
  toListingCardViewModel,
  buildListingSlug,
  type ListingCardFields,
  type ListingSpecItem,
} from "@/lib/listing-card";
import { cleanBuildingName } from "@/lib/localities";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import ListingSpecs from "@/components/ListingSpecs";
import BackButton from "@/components/BackButton";
import RelatedSearches from "@/components/RelatedSearches";
import { generateListingRelated } from "@/lib/related-searches";

// Metadata and the page body both need the same listing. React request
// memoization prevents two identical Supabase round trips on one request.
const getListingByIdCached = cache(getListingById);

function RawSourceMessage({
  message,
  sender,
  groupName,
  timestamp,
}: {
  message: string | null;
  sender: string | null;
  groupName: string | null;
  timestamp: string | null;
}) {
  if (!message) return null;

  // Strip external links (YouTube, Instagram, Facebook, Twitter, etc.)
  // but preserve the text around them so the message is still readable.
  const stripped = message
    .replace(/https?:\/\/(?:www\.)?(?:youtube\.com|youtu\.be|instagram\.com|facebook\.com|fb\.com|twitter\.com|x\.com|t\.co|tiktok\.com|linkedin\.com)\/\S*/gi, "")
    // Render the source as readable evidence, not as unprocessed WhatsApp
    // markdown. The private recall/CTA still uses the original slice.
    .replace(/[*_`~]/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();

  // If the entire message was just links with no property text, don't show it
  if (!stripped) return null;

  const formattedTime = timestamp
    ? new Date(timestamp).toLocaleDateString("en-AE", {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="mt-7">
      <details className="group rounded-xl border border-white/10 bg-zinc-950/60">
        <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-semibold text-zinc-300 select-none hover:text-white transition-colors">
          <ChevronDown className="h-4 w-4 text-zinc-500 transition-transform group-open:rotate-180" aria-hidden="true" />
          View original message
        </summary>
        <div className="border-t border-white/5 px-4 py-4">
          <p className="text-sm leading-relaxed text-zinc-400 whitespace-pre-wrap">{stripped}</p>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-zinc-600">
            {groupName && <span>{groupName}</span>}
            {sender && <span>Sender: {sender}</span>}
            {formattedTime && <span>{formattedTime}</span>}
          </div>
        </div>
      </details>
    </div>
  );
}

const DETAIL_LABELS: Array<[string, string]> = [
  ["bathroom_count", "Bathrooms"],
  ["carpet_area_sqft", "Carpet area"],
  ["built_up_area_sqft", "Built-up area"],
  ["super_built_up_area_sqft", "Super built-up area"],
  ["chargeable_area_sqft", "Chargeable area"],
  ["saleable_area_sqft", "Saleable area"],
  ["deposit_amount", "Deposit"],
  ["deposit_months", "Deposit months"],
  ["car_parking_count", "Parking"],
  ["parking_type", "Parking type"],
  ["floor_level", "Floor"],
  ["floor_range", "Floor"],
  ["fitout_status", "Fit-out"],
  ["ceiling_height", "Ceiling height"],
  ["commercial_use_type", "Commercial use"],
  ["pet_policy", "Pets"],
  ["tenant_type_preference", "Tenant preference"],
  ["sharing_allowed", "Sharing"],
  ["tenant_nationality_preference", "Nationality preference"],
  ["lease_term_type", "Lease term"],
  ["lock_in_period_months", "Lock-in"],
  ["notice_period_months", "Notice period"],
  ["property_view", "View"],
  ["orientation", "Orientation"],
  ["possession_status", "Possession"],
  ["occupancy_status", "Occupancy"],
  ["age_of_property", "Property age"],
  ["brokerage_type", "Brokerage"],
  ["developer_name", "Developer"],
  ["building_amenities", "Building amenities"],
  ["unit_amenities", "Home amenities"],
];

function formatDetailValue(key: string, value: unknown): string | null {
  if (value == null || value === "" || (Array.isArray(value) && value.length === 0)) return null;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.filter(Boolean).join(", ") || null;
  if (typeof value === "number") {
    const suffix = key.includes("area") ? " sqft" : key.includes("months") ? " months" : "";
    return `${value.toLocaleString("en-AE")}${suffix}`;
  }
  const text = String(value).replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  if (/^(?:not specified|not available|unknown|none|null|n\/a|na|-)$/.test(text.toLowerCase())) return null;
  return text;
}

function ListingDetailFacts({ fields }: { fields: Record<string, unknown> }) {
  const facts = DETAIL_LABELS.map(([key, label]) => {
    const value = formatDetailValue(key, fields[key]);
    return value ? { key, label, value } : null;
  }).filter((fact): fact is { key: string; label: string; value: string } => Boolean(fact));
  if (facts.length === 0) return null;

  return (
    <section className="mt-8" aria-labelledby="property-details-heading">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 id="property-details-heading" className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Property details
        </h2>
        <span className="text-[11px] text-zinc-600">Parsed from the broker post</span>
      </div>
      <dl className="grid grid-cols-1 overflow-hidden rounded-2xl border border-white/10 bg-zinc-950/70 sm:grid-cols-2">
        {facts.map((fact) => (
          <div key={fact.key} className="border-b border-white/5 px-4 py-3 last:border-b-0 sm:even:border-l">
            <dt className="text-[11px] uppercase tracking-wide text-zinc-600">{fact.label}</dt>
            <dd className="mt-1 text-sm font-medium text-zinc-200">{fact.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

type Params = { params: Promise<{ slug: string; id: string }> };

const SPEC_ICONS: Record<ListingSpecItem["kind"], typeof BedDouble> = {
  bhk: BedDouble,
  area: Ruler,
  furnishing: Sofa,
  floor: Building2,
  view: Eye,
  type: Tag,
};

function toCardFields(row: NonNullable<Awaited<ReturnType<typeof getListingById>>>): ListingCardFields {
  return {
    id: row.id,
    bhk: row.bhk,
    price: row.price,
    price_unit: row.price_unit,
    price_model: row.price_model,
    price_per_sqft: row.price_per_sqft,
    area_sqft: row.area_sqft,
    furnishing: row.furnishing,
    intent: row.intent,
    asset_type: row.asset_type,
    property_type: row.property_type,
    micro_market: row.micro_market,
    locality_raw: row.locality_raw,
    locality_resolved: row.locality_resolved,
    building_name: row.building_name,
    landmark_name: row.landmark_name,
    location_label: row.location_label,
    floor_description: row.floor_description,
    view: row.view,
    title: row.title,
    broker_name: row.broker_name,
    broker_id: row.broker_id ?? null,
    broker_phone: row.broker_phone,
    last_seen: row.last_seen,
    deal_tags: row.deal_tags,
    additional_charges: row.additional_charges,
  };
}

// Computes the canonical slug for a listing row (id + bhk + locality).
// Kept in this file (as well as in listing-card.ts) so the metadata + page
// functions agree on the URL Google should index.
function canonicalSlugFor(row: NonNullable<Awaited<ReturnType<typeof getListingById>>>): string | null {
  return buildListingSlug({
    id: row.id,
    bhk: row.bhk,
    micro_market: row.micro_market,
    building_name: row.building_name,
    property_type: row.property_type,
  });
}

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { id, slug } = await params;
  let listing;
  try {
    listing = await getListingByIdCached(Number(id), slug);
  } catch {
    return { title: "Listing not found — PropAI" };
  }
  if (!listing) return { title: "Listing not found — PropAI" };
  let card;
  try {
    card = toListingCardViewModel(toCardFields(listing), false);
  } catch {
    return { title: "Listing not found — PropAI" };
  }
  // Transaction type is authoritative. Do not infer Rent/Sale from the
  // formatted price: a legitimate rent-per-sqft quote may be converted to a
  // monthly total for display, and that label must not turn it into a sale.
  const isRent = /^(rent|rental|lease)$/i.test(String(listing.intent || ""));
  const dealType = isRent ? "For rent" : "For sale";
  return {
    title: listingTitle(card),
    description: listingDescription({
      dealType,
      title: card.title,
      locality: card.locality,
      specRow: card.specRow,
      building: listing.building_name,
      landmark: listing.landmark_name,
      sourceMessage: listing.rawMessage?.message,
    }, 155),
  };
}

export default async function ListingPage({ params }: Params) {
  const { slug, id } = await params;
  const numericId = Number(id);
  if (!Number.isFinite(numericId)) notFound();

  let listing;
  try {
    listing = await getListingByIdCached(numericId, slug);
  } catch (err) {
    console.error("getListingById failed:", err);
    notFound();
  }
  if (!listing) notFound();

  const freshnessCutoff = new Date();
  freshnessCutoff.setDate(freshnessCutoff.getDate() - 90);
  const isExpired = listing.last_seen ? new Date(listing.last_seen) < freshnessCutoff : true;

  if (isExpired) {
    const similarListings = await getSimilarListingsForExpired({
      micro_market: listing.micro_market,
      bhk: listing.bhk,
      intent: listing.intent,
      limit: 5,
    });

    return (
      <div className="www-shell min-h-screen">
        <SiteHeader />
        <main className="mx-auto max-w-7xl px-4 py-8 lg:px-6 lg:py-12">
          <div className="mb-6 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
            <Link href="/search" className="hover:text-white transition-colors">
              Home
            </Link>
            <ChevronRight className="h-3 w-3" aria-hidden="true" />
            <Link href={`/localities/${slugify(listing.micro_market || "")}`} className="hover:text-white transition-colors">
              {listing.micro_market}
            </Link>
            <ChevronRight className="h-3 w-3" aria-hidden="true" />
            <span className="text-zinc-400">{cleanBuildingName(listing.building_name) || `Listing ${numericId}`}</span>
          </div>

          <div className="mx-auto max-w-2xl text-center">
            <h1 className="text-3xl font-bold text-white mb-4">
              This listing has expired
            </h1>
            <p className="text-lg text-zinc-300 mb-6">
              This property was last mentioned in WhatsApp conversations more than 90 days ago and is no longer active.
            </p>

            {similarListings.length > 0 && (
              <>
                <h2 className="text-xl font-semibold text-white mb-4">
                  Similar current listings in {listing.micro_market}
                </h2>
                <div className="grid gap-4">
                  {similarListings.map((l) => (
                    <Link
                      key={l.id}
                      href={`/listings/${buildListingSlug({
                        id: l.id,
                        bhk: l.bhk,
                        micro_market: l.micro_market,
                        building_name: l.building_name,
                        property_type: l.property_type,
                      })}/${l.id}`}
                      className="block rounded-xl border border-white/10 bg-zinc-950/50 p-4 hover:border-green-400/40 transition-colors"
                    >
                      <div className="flex justify-between items-start mb-2">
                        <h3 className="text-lg font-semibold text-white">
                          {l.building_name || "Property"}
                        </h3>
                        <span className="text-green-400 font-semibold">
                          {l.price} {l.price_unit}
                        </span>
                      </div>
                      <p className="text-zinc-400 text-sm">{l.bhk} • {l.property_type} • {l.micro_market}</p>
                      <p className="text-xs text-zinc-500 mt-1">
                        Last seen: {l.last_seen ? new Date(l.last_seen).toLocaleDateString("en-AE") : "recently"}
                      </p>
                    </Link>
                  ))}
                </div>
              </>
            )}

            <div className="mt-8">
              <Link
                href="/search"
                className="inline-flex items-center gap-2 rounded-lg bg-green-400 px-5 py-3 text-sm font-semibold text-black hover:bg-green-300 transition-colors"
              >
                Search current listings
              </Link>
            </div>
          </div>
          <SiteFooter />
        </main>
      </div>
    );
  }

  // Fetch broker's operating areas from their listing history
  // These are independent secondary panels. Fetch them together so the
  // sidebar does not wait for the related-search section (or vice versa).
  const [brokerAreas, buildingBrokers, similarListings, relatedSections] = await Promise.all([
    getBrokerAreas(listing.broker_phone),
    getBuildingBrokers(listing.building_name, listing.micro_market),
    getSimilarListingsForDetail({
      ...listing,
      broker_id: listing.broker_id ?? null,
      broker_name: listing.broker_name,
      broker_phone: listing.broker_phone,
      property_type: listing.property_type,
      asset_type: listing.asset_type,
      floor_description: listing.floor_description,
    }),
    generateListingRelated(listing).catch((err) => {
      console.error("generateListingRelated failed:", err);
      return [];
    }),
  ]);

  // If the request slug doesn't match the canonical slug (e.g. external site
  // linked to an older slug after the listing was edited), 301 to the canonical
  // URL so Google consolidates ranking signals.
  const canonicalSlug = canonicalSlugFor(listing);
  if (canonicalSlug && slug !== canonicalSlug) {
    redirect(`/listings/${canonicalSlug}/${numericId}`, "replace");
  }

  let card;
  try {
    card = toListingCardViewModel(toCardFields(listing), false);
  } catch (err) {
    console.error("toListingCardViewModel failed:", err);
    notFound();
  }
  // Defensive: ensure all required fields exist on card
  if (!card || !card.title || !card.href) {
    console.error("Invalid card view model:", card);
    notFound();
  }
  const similarCards = similarListings
    .map((row) => {
      return {
        card: toListingCardViewModel(row, false),
        reason: row.recommendation_reason || "Same locality",
      };
    })
    .filter(({ card: similar }) => similar.href && similar.title);

  const brokerInitials = (card.brokerName || "PR")
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
  // The stored transaction type is authoritative even when a rent-per-sqft
  // quote has been converted into a monthly total for display.
  const isRent = /^(rent|rental|lease)$/i.test(String(listing.intent || ""));
  const dealType = isRent ? "For rent" : "For sale";

  const siteUrl = getSiteUrl();
  // Canonical URL mirrors the dynamic route: /listings/[slug]/[id].
  const listingUrl = `${siteUrl}/listings/${canonicalSlug ?? "listing"}/${numericId}`;
  const priceUnit = (listing.price_unit || "").toLowerCase();
  let priceAED: number | null = null;
  if (typeof listing.price === "number" && !Number.isNaN(listing.price)) {
    if (priceUnit === "m" || priceUnit === "mn" || priceUnit.includes("million")) priceAED = listing.price * 1_000_000;
    else if (priceUnit === "k" || priceUnit.includes("thousand")) priceAED = listing.price * 1_000;
    // Legacy Indian-unit rows kept as a safety net; fresh ingestion emits abs/k/m.
    else if (priceUnit.includes("cr")) priceAED = listing.price * 10_000_000;
    else if (priceUnit.includes("lac") || priceUnit.includes("lakh")) priceAED = listing.price * 100_000;
    else priceAED = listing.price;
  }
  const safeTitle = card.title || `${listing.bhk || ""} ${listing.property_type || "property"} in ${card.locality || "Dubai"}`.trim();
  const safeLocality = card.locality || "Dubai";
  const listingSchema = buildRealEstateListing({
    url: listingUrl,
    id: numericId,
    title: safeTitle,
    description: `${dealType} — ${card.title || "property"} in ${card.locality || "Dubai"}. Listed via live WhatsApp broker network, routed directly to the posting broker.`,
    price: priceAED,
    priceCurrency: "AED",
    dealType,
    bedrooms: listing.bhk,
    areaSqft: typeof listing.area_sqft === "number" ? listing.area_sqft : null,
    locality: card.locality,
    brokerName: card.brokerName,
    datePosted: listing.last_seen,
  });
  const breadcrumbSchema = buildBreadcrumb(siteUrl, [
    { name: "Home", url: "/" },
    ...(card.locality && card.localitySlug
      ? [{ name: card.locality, url: `/localities/${card.localitySlug}` }]
      : []),
    { name: card.title || `Listing ${numericId}`, url: `/listings/${canonicalSlug ?? "listing"}/${numericId}` },
  ]);

  return (
    <div className="www-shell min-h-screen">
      <SiteHeader />
      <JsonLd data={listingSchema} />
      <JsonLd data={breadcrumbSchema} />
      <main className="mx-auto max-w-[1600px] px-4 py-8 lg:px-8 lg:py-12">
        <BackButton />

        <div className="mb-6 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
          <Link href="/search" className="hover:text-white transition-colors">
            Home
          </Link>
          <ChevronRight className="h-3 w-3" aria-hidden="true" />
          {card.locality && (
            <>
              <Link
                href={`/localities/${card.localitySlug}`}
                className="hover:text-white transition-colors"
              >
                {card.locality}
              </Link>
              <ChevronRight className="h-3 w-3" aria-hidden="true" />
            </>
          )}
          <span className="text-zinc-400">{cleanBuildingName(listing.building_name) || card.title}</span>
        </div>

        <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(620px,720px)]">
          {/* Main column */}
          <div>
            {/* Header — no image hero. The page is text-first; photos are
                not part of the public inventory yet. */}
            <div className="grid grid-cols-1 items-start gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:gap-6">
              <div>
                <div className="flex items-center gap-1.5 text-sm text-zinc-400">
                  <MapPin className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{card.locality}</span>
                </div>
                <h1 className="mt-1 max-w-[22ch] text-[26px] font-bold leading-[1.12] text-white lg:text-[30px]">
                  {cleanBuildingName(listing.building_name) || card.title}
                </h1>
              </div>
              <div className="text-left sm:pt-1 sm:text-right">
                <div className="text-2xl font-semibold leading-tight text-white lg:text-3xl">{card.priceLabel}</div>
                {/* Transaction and availability are already communicated by
                    the price/specs and freshness; avoid redundant badges. */}
                {card.additionalCharges.length > 0 && (
                  <ul className="mt-2 space-y-0.5 text-xs text-zinc-400">
                    {card.additionalCharges.map((c, i) => (
                      <li key={`${c.label}-${i}`} className="flex items-center justify-end gap-1.5">
                        <span className="text-zinc-500">{c.label}</span>
                        <span className="font-medium text-zinc-200">{c.amountLabel}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            {/* Specs grid */}
            {card.specItems.length > 0 && (
              <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3">
                {card.specItems.map((s, i) => {
                  const Icon = SPEC_ICONS[s.kind] ?? BedDouble;
                  return (
                    <div
                      key={`${s.kind}-${i}`}
                      className="flex items-center gap-3 rounded-xl border border-white/10 bg-zinc-950/90 p-3.5"
                    >
                      <Icon className="h-4 w-4 shrink-0 text-green-400" aria-hidden="true" />
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-zinc-500">{s.kind}</div>
                        <div className="text-sm font-semibold text-white">{s.label}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            <ListingDetailFacts fields={listing.detailFields} />

            {/* Description — only show if location_label adds info beyond micro_market */}
            {listing.location_label && listing.location_label !== listing.micro_market && (
              <div className="mt-7">
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
                  About this listing
                </h2>
                <p className="text-sm leading-relaxed text-zinc-300 break-words">{listing.location_label}</p>
              </div>
            )}

            {/* Landmarks / nearby — only show if we have actual landmark info */}
            {listing.landmark_name && listing.landmark_name !== card.locality && (
              <div className="mt-7">
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
                  Nearby
                </h2>
                <ul className="space-y-1.5">
                  <li className="flex items-center gap-2 text-sm text-zinc-300">
                    <Building2 className="h-3.5 w-3.5 text-zinc-500" aria-hidden="true" />
                    {listing.landmark_name}
                  </li>
                </ul>
              </div>
            )}

            {/* Public copy is structured and indexable. Raw WhatsApp evidence
                stays in internal review surfaces and must never reach public HTML. */}
            <div className="mt-7">
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
                About this listing
              </h2>
              <p className="text-sm leading-relaxed text-zinc-300">
                {listingDescription({
                  dealType,
                  title: card.title,
                  locality: card.locality,
                  specRow: card.specRow,
                  building: listing.building_name,
                  landmark: listing.landmark_name,
                  sourceMessage: listing.rawMessage?.message,
                })}
              </p>
            </div>

            <div className="mt-6 text-xs text-zinc-600">
              <p>
                This listing is sourced from live broker activity in PropAI&apos;s WhatsApp network. Details
                are parsed automatically and may change — confirm specifics with the broker before proceeding.
              </p>
              <p className="mt-1">
                Last updated: {listing.last_seen ? (() => {
                  const d = new Date(listing.last_seen);
                  const ms = d.getTime();
                  if (!Number.isFinite(ms)) return "recently";
                  const diffMs = Date.now() - ms;
                  const dayMs = 24 * 60 * 60 * 1000;
                  if (diffMs < 0) return "just now";
                  if (diffMs < dayMs) return "today";
                  if (diffMs < 2 * dayMs) return "yesterday";
                  if (diffMs < 7 * dayMs) return `${Math.floor(diffMs / dayMs)}d ago`;
                  return d.toLocaleDateString("en-AE", { day: "numeric", month: "short" });
                })() : "recently"}
              </p>
            </div>
          </div>

          {/* Sidebar */}
          <aside className="relative grid items-start gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
            <div className="sticky top-6 overflow-hidden rounded-2xl border border-white/10 bg-zinc-950/90 p-5">
              <button
                className="absolute right-3 top-3 inline-flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:border-white/20 hover:bg-white/5 hover:text-amber-400"
                aria-label="Report incorrect information for this listing"
                title="Report incorrect info"
              >
                <Flag className="h-3 w-3" strokeWidth={2} aria-hidden="true" />
              </button>

              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-green-400/15 text-lg font-bold text-green-300">
                {brokerInitials}
              </div>
              <div className="mt-3 flex items-center justify-center gap-1.5 text-center text-base font-semibold text-white">
                <span className="truncate">{card.brokerName || "PropAI network"}</span>
                {card.brokerName && (
                  <ShieldCheck className="h-4 w-4 shrink-0 text-green-400" aria-hidden="true" />
                )}
              </div>
              <div className="mt-1 text-center text-xs text-zinc-500">
                Active listings on PropAI
              </div>

              <div className="mt-5 flex flex-col gap-2.5">
                {/* Contact CTA: only render the WhatsApp button when we know
                    broker_phone can resolve to a wa.me link server-side. If
                    the phone is missing/bad, show a clear "unavailable" message
                    instead of a button that would just silently 302 back to
                    this page. Phone number is NEVER embedded in public HTML
                    (DPDP Act 2023). */}
                {card.waAvailable ? (
                  <a
                    href={card.waLink ?? "#"}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center justify-center gap-2 rounded-xl bg-green-400 px-5 py-3 text-sm font-semibold text-black transition-colors hover:bg-green-300"
                  >
                    <MessageSquare className="h-4 w-4" aria-hidden="true" />
                    Contact on WhatsApp
                  </a>
                ) : (
                  <span
                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-white/10 px-5 py-3 text-sm text-zinc-500"
                    data-testid="broker-unavailable"
                  >
                    <MessageSquare className="h-4 w-4" aria-hidden="true" />
                    Contact info unavailable
                  </span>
                )}
              </div>

              {brokerAreas.length > 0 && (
                <div className="mt-5">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500 mb-2">
                    Active in
                  </h3>
                  <div className="flex flex-wrap gap-1.5">
                    {brokerAreas.map((area) => (
                      <span
                        key={area}
                        className="rounded-full bg-white/5 border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400"
                      >
                        {area}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {buildingBrokers.filter((broker) => broker.name.toLowerCase() !== (card.brokerName || "").toLowerCase()).length > 0 && (
                <div className="mt-5">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500 mb-2">
                    Other brokers for this building
                  </h3>
                  <div className="space-y-1.5">
                    {buildingBrokers
                      .filter((broker) => broker.name.toLowerCase() !== (card.brokerName || "").toLowerCase())
                      .slice(0, 5)
                      .map((broker) => (
                      <div key={broker.name} className="flex items-center justify-between gap-3 text-xs text-zinc-400">
                        <span className="truncate">{broker.name}</span>
                        <span className="shrink-0 text-zinc-600">{broker.listingCount} listings</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <p className="mt-4 text-[11px] leading-relaxed text-zinc-500">
                Listing sourced from active broker WhatsApp networks and refreshed continuously by
                PropAI.
              </p>
            </div>

            {similarCards.length > 0 && (
              <section className="mt-5 rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 lg:mt-0" aria-label="More like this">
                <h2 className="text-sm font-semibold text-[var(--text-primary)]">More like this</h2>
                <p className="mb-3 mt-1 text-xs text-[var(--text-secondary)]">
                  Fresh matches ranked by building, configuration, price and recency.
                </p>
                <div className="max-h-[620px] space-y-3 overflow-y-auto pr-1">
                  {similarCards.map(({ card: similar, reason }) => (
                    <Link
                      key={similar.href}
                      href={similar.href as string}
                      className="block rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-base)] p-3 transition-colors hover:border-[var(--accent-primary)]"
                    >
                      <div className="mb-2 inline-flex rounded-full bg-[var(--accent-soft)] px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--accent-forest)]">
                        {reason}
                      </div>
                      <div className="text-sm font-semibold leading-snug text-[var(--text-primary)]">{similar.title}</div>
                      <div className="mt-1 text-base font-semibold text-[var(--price-highlight)]">{similar.priceLabel}</div>
                      <div className="mt-1 text-xs text-[var(--text-secondary)]">
                        {[similar.locality, similar.specRow, similar.freshnessLabel].filter(Boolean).join(" · ")}
                      </div>
                      {similar.brokerName && (
                        <div className="mt-2 text-[11px] text-[var(--text-secondary)]">Listed by {similar.brokerName}</div>
                      )}
                    </Link>
                  ))}
                </div>
              </section>
            )}
          </aside>
        </div>

        {/* Internal links: same locality views, same BHK, same building. */}
        {card.localitySlug && (
          <nav className="mt-8" aria-label="Related searches">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-400">
              More like this
            </h2>
            <div className="flex flex-wrap gap-2.5">
              {(() => {
                const txn = (listing.intent || "").toLowerCase().includes("rent") ? "rent" : "sale";
                const links: Array<{ href: string; label: string }> = [
                  { href: `/localities/${card.localitySlug}/${txn}`, label: `${card.locality} ${txn === "rent" ? "for Rent" : "for Sale"}` },
                ];
                const bhkNum = (listing.bhk || "").match(/(\d+)/)?.[1];
                if (bhkNum) {
                  links.push({ href: `/localities/${card.localitySlug}/bhk-${bhkNum}`, label: `${bhkNum} BHK in ${card.locality}` });
                }
                if (listing.building_name) {
                  const cleanName = cleanBuildingName(listing.building_name);
                  if (cleanName) {
                    links.push({ href: `/buildings/${slugify(cleanName)}`, label: cleanName });
                  }
                }
                return links.map((l) => (
                  <Link
                    key={l.href}
                    href={l.href}
                    className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white"
                  >
                    {l.label}
                  </Link>
                ));
              })()}
            </div>
          </nav>
        )}

        {relatedSections.length > 0 && (
          <RelatedSearches sections={relatedSections} />
        )}
      </main>
      <SiteFooter />
    </div>
  );
}
