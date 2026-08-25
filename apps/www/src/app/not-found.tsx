import Link from "next/link";
import { Search } from "lucide-react";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";

export default function NotFound() {
  return (
    <div className="min-h-screen bg-black text-white flex flex-col">
      <SiteHeader />
      <main className="flex-1 flex items-center justify-center px-4 lg:px-6 py-20">
        <div className="text-center max-w-lg">
          <p className="text-[80px] lg:text-[110px] leading-none font-bold text-green-400/90 mb-4">404</p>
          <h1 className="text-[24px] lg:text-[30px] font-semibold text-white mb-3">
            This page wandered off
          </h1>
          <p className="text-[15px] lg:text-[17px] text-zinc-400 mb-8">
            The listing or page you&apos;re looking for isn&apos;t here — it may have
            been rented, removed, or never existed. Try searching live Dubai inventory instead.
          </p>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <Link
              href="/search"
              className="inline-flex items-center gap-2 px-6 py-3 bg-green-400 text-black text-sm font-semibold rounded-lg hover:bg-green-300 transition-colors"
            >
              <Search className="w-4 h-4" aria-hidden="true" />
              Search listings
            </Link>
          </div>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
