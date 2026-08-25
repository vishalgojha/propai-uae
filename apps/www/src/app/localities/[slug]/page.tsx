import { notFound } from "next/navigation";
import { MapPin, Building2 } from "lucide-react";
import Link from "next/link";
import { getLocalityData, getAllLocalities } from "@/lib/localities";
import LocalityMapLoader from "@/components/LocalityMapLoader";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import ListingCard, { LocalityBackLink } from "@/components/ListingCard";
import { NoPhotosFaqJsonLd, NoPhotosFaq } from "@/components/NoPhotosFaq";
import LocalityFaq, { LocalityFaqJsonLd } from "@/components/LocalityFaq";
import { JsonLd, buildLocalBusiness, buildBreadcrumb, getSiteUrl } from "@/lib/seo";
import { localityTitle, localityDescription } from "@/lib/seo-copy";

// Read at server runtime (Coolify injects env into the running container).
// Passed to the client map so we don't depend on NEXT_PUBLIC_* build-time
// inlining, which the Docker build stage doesn't receive.
const GOOGLE_MAPS_API_KEY =
  process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ||
  process.env.GOOGLE_MAPS_API_KEY ||
  null;

type Params = { params: Promise<{ slug: string }> };

// These pages aggregate live WhatsApp inventory that updates gradually; a few
// minutes of staleness is acceptable and avoids re-scanning the full listings/
// buildings tables on every request. ISR caches the rendered page for 5 min.
export const revalidate = 300;

export async function generateMetadata({ params }: Params) {
  const { slug } = await params;
  const data = await getLocalityData(slug);
  if (!data) return { title: "Locality not found — PropAI" };
  return {
    title: localityTitle(data.locality),
    description: data.hasListings
      ? localityDescription({
          locality: data.locality,
          totalListings: data.totalListings,
          buildingCount: data.buildings.length,
          saleCount: data.saleCount,
          rentCount: data.rentCount,
          topBhk: data.topBhk,
        })
      : `Live ${data.locality} listings, price ranges, and broker activity sourced from WhatsApp broker conversations.`,
  };
}

export default async function LocalityPage({ params }: Params) {
  const { slug } = await params;
  const data = await getLocalityData(slug);
  if (!data) notFound();

  // Nearby / other localities for internal linking (top by inventory, excluding self).
  const allLocalities = (await getAllLocalities()).filter((l) => l.slug !== data.slug);
  const nearby = allLocalities.slice(0, 8);

  const siteUrl = getSiteUrl();
  const localityUrl = `${siteUrl}/localities/${data.slug}`;
  const localitySchema = buildLocalBusiness({
    url: localityUrl,
    name: data.locality,
    description: `Live ${data.locality} property listings, price ranges, and broker activity sourced from WhatsApp broker conversations on PropAI.`,
    listingCount: data.totalListings,
  });
  const breadcrumbSchema = buildBreadcrumb(siteUrl, [
    { name: "Home", url: "/" },
    { name: "Localities", url: "/localities" },
    { name: data.locality, url: `/localities/${data.slug}` },
  ]);

  const mapped = data.buildings.filter(
    (b) => b.latitude != null && b.longitude != null,
  );

  // A known place with zero active listings — distinct from a typo/404 slug.
  if (!data.hasListings) {
    const suggestions = (await getAllLocalities()).slice(0, 5);
    return (
      <div className="min-h-screen bg-black text-white">
        <SiteHeader />
        <JsonLd data={localitySchema} />
        <JsonLd data={breadcrumbSchema} />
        <main className="max-w-3xl mx-auto px-4 lg:px-6 py-16 lg:py-24">
          <NoPhotosFaqJsonLd />
          <div className="mb-8">
            <LocalityBackLink />
          </div>
          <div className="bg-zinc-900/50 border border-white/10 rounded-2xl p-8 lg:p-10 text-center">
            <div className="flex items-center justify-center w-12 h-12 rounded-full bg-zinc-800 mb-5 mx-auto">
              <Building2 className="w-6 h-6 text-green-400" aria-hidden="true" />
            </div>
            <h1 className="text-[28px] lg:text-[36px] leading-[1.1] font-bold text-white mb-3">
              No listings in {data.locality} yet
            </h1>
            <p className="text-lg text-zinc-400 max-w-xl mx-auto">
              {data.locality} is on our radar, but no broker activity has been
              tracked there yet. Listings appear automatically as soon as brokers
              start posting in our WhatsApp network.
            </p>
            {suggestions.length > 0 && (
              <div className="mt-8">
                <p className="text-sm text-zinc-500 mb-4">
                  Browse a locality with live listings:
                </p>
                <div className="flex flex-wrap justify-center gap-3">
                  {suggestions.map((loc) => (
                    <Link
                      key={loc.slug}
                      href={`/localities/${loc.slug}`}
                      className="px-4 py-2 rounded-lg bg-zinc-800 border border-white/10 text-white text-sm font-medium hover:border-green-400/50 transition-colors"
                    >
                      {loc.locality}
                    </Link>
                  ))}
                </div>
              </div>
            )}
          </div>
          <NoPhotosFaq />
        </main>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <JsonLd data={localitySchema} />
      <JsonLd data={breadcrumbSchema} />
      <LocalityFaqJsonLd
        locality={data.locality}
        saleCount={data.saleCount}
        rentCount={data.rentCount}
        buildingCount={data.buildings.length}
      />
      <main className="max-w-[1600px] mx-auto px-4 lg:px-6 py-10 lg:py-14">
        <NoPhotosFaqJsonLd />
        <div className="mb-8">
          <LocalityBackLink />
        </div>

        <header className="mb-10">
          <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-3">
            {data.locality}
          </h1>
          <p className="text-lg text-zinc-400 max-w-2xl">
            {data.totalListings.toLocaleString("en-AE")} live {data.locality} listings
            {data.buildings.length ? ` across ${data.buildings.length} buildings` : ""},
            sourced from WhatsApp broker conversations and updated in real time.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <TrustStat label="Active listings" value={data.totalListings} />
            <TrustStat label="Buildings" value={data.buildings.length} />
            {data.saleCount > 0 && <TrustStat label="For sale" value={data.saleCount} />}
            {data.rentCount > 0 && <TrustStat label="For rent" value={data.rentCount} />}
          </div>
        {mapped.length === 0 && (
          <p className="mt-3 inline-flex items-center gap-2 text-sm text-zinc-500">
            <MapPin className="w-4 h-4" aria-hidden="true" />
            Map view unavailable — location data still being enriched for this
            locality. Showing listing cards below.
          </p>
        )}
      </header>

      <section aria-label="Buildings in this locality">
        <h2 className="text-[20px] lg:text-[24px] font-semibold text-white mb-6">
          Buildings
        </h2>
        {data.buildings.length === 0 ? (
          <p className="text-zinc-400">No buildings with listings yet for this locality.</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 lg:gap-6">
            {data.buildings.map((b) => (
              <ListingCard key={b.name} building={b} />
            ))}
          </div>
        )}
      </section>

      {mapped.length > 0 && (
        <section className="mt-12" aria-label={`Map of ${data.locality}`}>
          <h2 className="text-[18px] lg:text-[20px] font-semibold text-white mb-4">
            Map
          </h2>
          <LocalityMapLoader locality={data.locality} buildings={data.buildings} apiKey={GOOGLE_MAPS_API_KEY} />
          {data.unmappedCount > 0 && (
            <p className="mt-3 text-xs text-zinc-500 text-center">
              Showing {mapped.length} of {data.buildings.length} buildings on the map.
              {data.unmappedCount} more are listed above.
            </p>
          )}
        </section>
      )}

      <NoPhotosFaq />

      {/* Internal linking: drill into filtered views + nearby localities. */}
      <section className="mt-14" aria-label={`More ${data.locality} searches`}>
        <h2 className="mb-4 text-[18px] lg:text-[20px] font-semibold text-white">
          Refine {data.locality}
        </h2>
        <div className="flex flex-wrap gap-2.5">
          <Link href={`/localities/${data.slug}/sale`} className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white">
            {data.locality} for Sale
          </Link>
          <Link href={`/localities/${data.slug}/rent`} className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white">
            {data.locality} for Rent
          </Link>
          {data.topBhk && (
            <Link
              href={`/localities/${data.slug}/${data.topBhk.toLowerCase().replace(/\s+/g, "-").replace("bhk", "bhk-")}`}
              className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white"
            >
              {data.topBhk} in {data.locality}
            </Link>
          )}
          <Link href={`/localities/${data.slug}/commercial`} className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white">
            {data.locality} Commercial
          </Link>
        </div>
      </section>

      {nearby.length > 0 && (
        <section className="mt-10" aria-label="Nearby localities">
          <h2 className="mb-4 text-[18px] lg:text-[20px] font-semibold text-white">
            Explore nearby localities
          </h2>
          <div className="flex flex-wrap gap-2.5">
            {nearby.map((l) => (
              <Link
                key={l.slug}
                href={`/localities/${l.slug}`}
                className="rounded-lg border border-white/10 bg-zinc-900/60 px-3.5 py-2 text-sm text-zinc-200 transition-colors hover:border-green-400/40 hover:text-white"
              >
                {l.locality}
              </Link>
            ))}
          </div>
        </section>
      )}

      <LocalityFaq locality={data.locality} />
      </main>
      <SiteFooter />
    </div>
  );
}

// Small E-E-A-T stat chip shown on locality pages — concrete, sourced numbers
// that build trust with both users and LLM citation.
function TrustStat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-zinc-900/50 px-4 py-3">
      <div className="text-[20px] lg:text-[22px] font-semibold text-white leading-none">
        {typeof value === "number" ? value.toLocaleString("en-AE") : value}
      </div>
      <div className="mt-1 text-xs text-zinc-400">{label}</div>
    </div>
  );
}
