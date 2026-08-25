import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { getLocalityData, getLocalityListings, type LocalityListingFilter } from "@/lib/localities";
import { toListingCardViewModel } from "@/lib/listing-card";
import {
  JsonLd,
  buildBreadcrumb,
  buildLocalBusiness,
  getSiteUrl,
} from "@/lib/seo";
import {
  localitySegmentTitle,
  localityBhkSegmentTitle,
  localityBudgetSegmentTitle,
  localitySegmentDescription,
} from "@/lib/seo-copy";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import ListingTile from "@/components/ListingTile";
import { ShortlistProvider } from "@/components/ShortlistProvider";

type Params = { params: Promise<{ slug: string; segment: string }> };
type SearchParams = { searchParams?: Promise<{ intent?: string | string[] }> };

export const revalidate = 300;

// Decode a URL segment into a filter + human label.
//   sale | rent | commercial
//   bhk-3 | bhk-2 | bhk-5 (5 => "5+")
//   budget-under-2-cr | budget-under-5-cr | budget-under-50-lac
function decodeSegment(
  segment: string,
  intentParam?: string,
): { filter: LocalityListingFilter; label: string; txn: "sale" | "rent" } | null {
  const s = segment.toLowerCase();
  if (s === "sale") return { filter: { txn: "sale" }, label: "for sale", txn: "sale" };
  if (s === "rent") return { filter: { txn: "rent" }, label: "for rent", txn: "rent" };
  if (s === "commercial")
    return { filter: { commercial: true }, label: "commercial", txn: "sale" };
  const bhk = s.match(/^(?:bhk-(\d+)|(\d+)-bhk)$/);
  if (bhk) {
    const n = Number(bhk[1] || bhk[2]);
    const txn = intentParam === "rent" ? "rent" : "sale";
    return { filter: { bhk: n, txn }, label: n >= 5 ? "5+ BHK" : `${n} BHK`, txn };
  }
  const budget = s.match(/^budget-under-(\d+)-(cr|lac)$/);
  if (budget) {
    const n = Number(budget[1]);
    const unit = budget[2];
    const txn: "sale" | "rent" = "sale";
    return {
      filter: { budgetMaxCr: unit === "cr" ? n : n / 100 },
      // Legacy India-unit slugs (`cr`/`lac`) relabelled as AED millions for
      // the UAE site; the underlying budget filter keeps its old semantics.
      label: unit === "cr" ? `under AED ${n}M` : `under AED ${(n / 10) % 1 === 0 ? n / 10 : (n / 10).toFixed(1)}M`,
      txn,
    };
  }
  return null;
}

export async function generateMetadata({ params, searchParams }: Params & SearchParams): Promise<Metadata> {
  const { slug, segment } = await params;
  const query = searchParams ? await searchParams : {};
  const intent = Array.isArray(query.intent) ? query.intent[0] : query.intent;
  const decoded = decodeSegment(segment, intent?.toLowerCase());
  if (!decoded) return { title: "Not found — PropAI" };
  const data = await getLocalityData(slug);
  if (!data) return { title: "Locality not found — PropAI" };

  let title: string;
  if (decoded.filter.bhk) title = localityBhkSegmentTitle(data.locality, decoded.filter.bhk);
  else if (decoded.filter.budgetMaxCr != null)
    title = localityBudgetSegmentTitle(data.locality, decoded.label, decoded.txn);
  else title = localitySegmentTitle(data.locality, segment.toLowerCase() as "sale" | "rent" | "commercial");

  return {
    title,
    description: localitySegmentDescription({
      locality: data.locality,
      segmentLabel: decoded.label,
      listingCount: data.totalListings,
      txn: decoded.txn,
    }),
    alternates: { canonical: `/localities/${data.slug}/${segment}` },
  };
}

export default async function LocalitySegmentPage({ params, searchParams }: Params & SearchParams) {
  const { slug, segment } = await params;
  const query = searchParams ? await searchParams : {};
  const intent = Array.isArray(query.intent) ? query.intent[0] : query.intent;
  const decoded = decodeSegment(segment, intent?.toLowerCase());
  if (!decoded) notFound();

  const base = await getLocalityData(slug);
  if (!base) notFound();

  const listings = await getLocalityListings(slug, decoded.filter);
  if (!listings) notFound();

  const siteUrl = getSiteUrl();
  const cards = listings.rows
    .map((r) => toListingCardViewModel(r, false))
    .filter((c) => c.href);

  const schema = buildLocalBusiness({
    url: `${siteUrl}/localities/${base.slug}/${segment}`,
    name: `${base.locality} ${decoded.label}`,
    description: `${decoded.label} listings in ${base.locality}, sourced live from WhatsApp broker conversations.`,
    listingCount: cards.length,
  });
  const breadcrumb = buildBreadcrumb(siteUrl, [
    { name: "Home", url: "/" },
    { name: "Localities", url: "/localities" },
    { name: base.locality, url: `/localities/${base.slug}` },
    { name: decoded.label, url: `/localities/${base.slug}/${segment}` },
  ]);

  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <JsonLd data={schema} />
      <JsonLd data={breadcrumb} />
      <main className="mx-auto max-w-[1600px] px-4 lg:px-6 py-10 lg:py-14">
        <div className="mb-8">
          <Link
            href={`/localities/${base.slug}`}
            className="inline-flex items-center gap-1.5 text-sm font-medium text-zinc-400 transition-colors hover:text-white"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            {base.locality}
          </Link>
        </div>

        <header className="mb-10">
          <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-3">
            {decoded.label === "commercial"
              ? `Commercial Properties in ${base.locality}`
              : `${decoded.label.charAt(0).toUpperCase()}${decoded.label.slice(1)} in ${base.locality}`}
          </h1>
          <p className="text-lg text-zinc-400 max-w-2xl">
            {cards.length.toLocaleString("en-AE")} live {decoded.label} listings in {base.locality},
            sourced from WhatsApp broker conversations and updated in real time.
          </p>
        </header>

        {cards.length > 0 ? (
          <ShortlistProvider>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 lg:gap-6">
              {cards.map((c) => (
                <ListingTile key={c.href} card={c} />
              ))}
            </div>
          </ShortlistProvider>
        ) : (
          <div className="rounded-2xl border border-white/10 bg-zinc-900/50 p-10 text-center">
            <p className="text-lg text-zinc-300">
              No active {decoded.label} listings in {base.locality} right now.
            </p>
            <p className="mt-2 text-sm text-zinc-500">
              New broker posts arrive continuously — check back shortly, or browse all{" "}
              <Link href={`/localities/${base.slug}`} className="text-green-400 hover:underline">
                {base.locality} listings
              </Link>
              .
            </p>
          </div>
        )}
      </main>
      <SiteFooter />
    </div>
  );
}
