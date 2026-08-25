import { MapPinned, RefreshCw } from "lucide-react";
import { getPublicMapListings } from "@/lib/natural-search";
import { toListingCardViewModel } from "@/lib/listing-card";
import { ShortlistProvider } from "@/components/ShortlistProvider";
import ListingTile from "@/components/ListingTile";
import SearchMapLoader from "@/components/SearchMapLoader";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";

const GOOGLE_MAPS_API_KEY =
  process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ||
  process.env.GOOGLE_MAPS_API_KEY ||
  null;

export const revalidate = 300;
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Property Map — Live Dubai Listings | PropAI",
  description:
    "Explore fresh Dubai property listings on a map, with live broker inventory alongside every mapped result.",
};

export default async function MapPage() {
  const results = await getPublicMapListings(60);
  const mappedResults = results.filter(
    (result) => result.latitude != null && result.longitude != null,
  );

  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <ShortlistProvider>
        <main className="mx-auto max-w-[1800px] px-4 sm:px-8 xl:px-12 py-8 lg:py-10">
          <header className="mb-8 max-w-3xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-green-400/20 bg-green-400/10 px-3 py-1 text-xs font-medium text-green-300">
              <MapPinned className="h-3.5 w-3.5" aria-hidden="true" />
              Live map view
            </div>
            <h1 className="mt-4 text-[32px] lg:text-[48px] leading-[1.05] font-bold text-white">
              Find properties by location
            </h1>
            <p className="mt-4 text-[15px] lg:text-[18px] text-zinc-400">
              Browse {results.length.toLocaleString("en-AE")} fresh listings from
              the WhatsApp broker network, with {mappedResults.length.toLocaleString("en-AE")} plotted on the map.
            </p>
          </header>

          {results.length === 0 ? (
            <div className="rounded-2xl border border-white/10 bg-zinc-950/80 p-10 text-center">
              <RefreshCw className="mx-auto h-6 w-6 text-green-400" aria-hidden="true" />
              <h2 className="mt-4 text-lg font-semibold text-white">No live listings to map right now</h2>
              <p className="mt-2 text-sm text-zinc-400">
                New broker posts appear here automatically as they arrive.
              </p>
            </div>
          ) : (
            <div className="grid gap-6 lg:grid-cols-[minmax(320px,0.85fr)_minmax(0,1.5fr)] lg:items-start">
              <section
                aria-label="Mapped live listings"
                className="order-2 grid max-h-[calc(100vh-170px)] grid-cols-1 gap-4 overflow-y-auto pr-1 sm:grid-cols-2 lg:order-1 lg:grid-cols-1"
              >
                {results.map((row) => (
                  <ListingTile
                    key={row.id}
                    card={toListingCardViewModel(row, false)}
                    buildingName={row.building_name}
                    footerNote="Live inventory"
                  />
                ))}
              </section>

              <section
                aria-label="Dubai property map"
                className="order-1 lg:order-2 lg:sticky lg:top-24"
              >
                <SearchMapLoader results={results} apiKey={GOOGLE_MAPS_API_KEY} />
              </section>
            </div>
          )}
        </main>
      </ShortlistProvider>
      <SiteFooter />
    </div>
  );
}
