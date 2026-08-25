import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { getAllLocalities } from "@/lib/localities";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";

export const metadata = {
  title: "Dubai Localities with Live Listings | PropAI",
  description:
    "Browse every Dubai locality PropAI tracks — live sale and rental listings sourced directly from WhatsApp broker networks, updated in real time.",
};

// Locality list changes gradually; ISR caches the page for 5 min so navigation
// is instant instead of re-scanning the localities table on every click.
export const revalidate = 300;
export const dynamic = "force-dynamic";


export default async function LocalitiesIndexPage() {
  const localities = await getAllLocalities();

  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <main className="max-w-[1600px] mx-auto px-4 lg:px-6 py-10 lg:py-14">
          <header className="mb-10">
            <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-3">
              Dubai localities with live listings
            </h1>
            <p className="text-lg text-zinc-400 max-w-2xl">
              {localities.length} localities tracked, with live sale and rental
              listing counts sourced directly from WhatsApp broker conversations.
            </p>
          </header>

          {localities.length === 0 ? (
            <p className="text-zinc-400">
              No localities indexed yet. Data is populated from live broker activity.
            </p>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 lg:gap-6">
              {localities.map((loc) => (
                <div
                  key={loc.slug}
                  className="group bg-zinc-900/50 border border-white/10 rounded-xl p-5 lg:p-6 transition-colors hover:border-green-400/50 hover:bg-zinc-900"
                >
                  <div className="flex flex-col h-full">
                    <h3 className="text-lg font-semibold text-white mb-1">
                      <Link
                        href={`/localities/${loc.slug}`}
                        className="group-hover:text-green-400 transition-colors"
                      >
                        {loc.locality}
                      </Link>
                    </h3>
                    <p className="text-xs text-zinc-500 flex items-center gap-1 mb-3">
                      <span className="w-1.5 h-1.5 rounded-full bg-green-400" aria-hidden="true" />
                      {loc.listingCount} active listing{loc.listingCount === 1 ? "" : "s"}
                    </p>
                    <div className="mt-auto flex flex-wrap gap-2 text-xs">
                      <Link
                        href={`/localities/${loc.slug}/sale`}
                        className="rounded-md border border-white/10 px-2.5 py-1 text-zinc-300 transition-colors hover:border-green-400/40 hover:text-white"
                      >
                        For Sale
                      </Link>
                      <Link
                        href={`/localities/${loc.slug}/rent`}
                        className="rounded-md border border-white/10 px-2.5 py-1 text-zinc-300 transition-colors hover:border-green-400/40 hover:text-white"
                      >
                        For Rent
                      </Link>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

        <div className="text-center mt-12">
          <Link
            href="/search"
            className="inline-flex items-center gap-2 px-6 py-3 bg-green-400 text-black text-sm font-semibold rounded-lg hover:bg-green-300 transition-colors"
          >
            Search listings
            <ArrowRight className="w-4 h-4" aria-hidden="true" />
          </Link>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
