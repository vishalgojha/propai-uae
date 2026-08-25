import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import { NoPhotosFaqJsonLd } from "@/components/NoPhotosFaq";
import { getPublicDataOverview } from "@/lib/public-data";
import { getAllLocalities } from "@/lib/localities";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "About PropAI — Real Listings from Dubai's Broker WhatsApp Groups",
  description:
    "Most listings online are old. PropAI reads the WhatsApp groups where Dubai's brokers actually work, shows how many groups corroborate each listing, and auto-hides anything untouched for 30 days. No stale photos — message the broker for current ones.",
};

export default async function AboutPage() {
  const known = await getAllLocalities();
  const overview = await getPublicDataOverview({ localities: known });
  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <NoPhotosFaqJsonLd />
      <main className="max-w-3xl mx-auto px-4 lg:px-6 py-10 lg:py-16">
        <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-6">
          About <span className="text-green-400">PropAI</span>
        </h1>

        <div className="space-y-8 text-[15px] lg:text-[17px] text-zinc-400 leading-relaxed">
          <p>
            Most property listings you find online are old. A broker posts a flat
            in a WhatsApp group, it gets forwarded twenty times, and three weeks
            later it&apos;s still floating around the internet — except it&apos;s
            already been rented out.
          </p>
          <p>
            PropAI reads the WhatsApp groups where Dubai&apos;s brokers actually
            work. Every listing you see here came from a real broker, in a real
            conversation, usually within the last few days. We show you how many
            separate broker groups have mentioned it and when it was last seen —
            so instead of trusting one listing, you&apos;re seeing what&apos;s
            actually corroborated across the market right now. Anything untouched
            for 30 days gets auto-hidden. What you see is what&apos;s live today,
            not what was live sometime this month.
          </p>
          <div>
            <h2 className="text-lg lg:text-xl font-semibold text-white mb-3">
              Why there are no photos.
            </h2>
            <p>
              This inventory turns over daily. A photo shot when a flat was first
              listed is often wrong by the time you&apos;d see it — wrong
              furnishing, wrong price, sometimes a different flat entirely. Instead
              of stale photos, every listing routes straight to the broker who&apos;s
              actually holding it — message them on WhatsApp and they&apos;ll send
              you real, current photos, video walkthroughs, and tell you what&apos;s
              actually still available.
            </p>
          </div>
          <p>
            <span className="text-white font-medium">Direct to broker, no middleman chatbot.</span>{" "}
            Your enquiry goes straight to a real person who calls you back. No forms
            that go nowhere, no chatbot standing between you and the person who has
            the keys.
          </p>
        </div>

        <div className="mt-10 grid grid-cols-2 lg:grid-cols-4 gap-4">
          <AboutStat label="Active listings" value={overview.counts.activeListings} />
          <AboutStat label="Active brokers" value={overview.counts.brokers} />
          <AboutStat label="Localities covered" value={overview.counts.localities} />
          <AboutStat
            label="Messages analysed"
            value={overview.counts.messagesAnalysed}
          />
        </div>
        <p className="mt-4 text-sm text-zinc-500">
          These numbers are pulled live from the same broker conversations powering
          every listing on this site. They update as brokers post.
        </p>
      </main>
      <SiteFooter />
    </div>
  );
}

function AboutStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-zinc-900/50 p-4 text-center">
      <div className="text-2xl font-bold text-white leading-none">
        {value.toLocaleString("en-AE")}
      </div>
      <div className="mt-2 text-xs text-zinc-400">{label}</div>
    </div>
  );
}
