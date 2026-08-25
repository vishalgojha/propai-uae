import { JsonLd, buildFaqPage, getSiteUrl } from "@/lib/seo";

type FaqProps = {
  locality: string;
  saleCount: number;
  rentCount: number;
  buildingCount: number;
};

// Real, intent-matching FAQ for a locality. Q&A is templated per locality so
// every programmatic page emits unique FAQPage structured data (not duplicated
// boilerplate) and answers the long-tail questions Google/LLMs ask.
export function LocalityFaqJsonLd({ locality, saleCount, rentCount }: FaqProps) {
  const siteUrl = getSiteUrl();
  const items = [
    {
      question: `Are there properties for sale in ${locality}?`,
      answer: `Yes. PropAI currently tracks ${saleCount.toLocaleString("en-AE")} live ${locality} listings for sale, sourced directly from WhatsApp broker conversations and updated in real time.`,
    },
    {
      question: `Can I find ${locality} properties on rent?`,
      answer: `Yes. There are ${rentCount.toLocaleString("en-AE")} ${locality} rental listings live on PropAI right now, ranging from 1 BHK to large apartments and commercial spaces.`,
    },
    {
      question: `How fresh are ${locality} listings on PropAI?`,
      answer: `Listings update continuously from verified broker WhatsApp groups. Each card shows when it last landed, so you always see the newest inventory first.`,
    },
    {
      question: `Do I contact a portal or the actual broker?`,
      answer: `You connect directly with the posting broker on WhatsApp — no lead forms, no middlemen. PropAI routes your enquiry straight to the person who listed the property.`,
    },
  ];
  return <JsonLd data={buildFaqPage(items)} />;
}

export default function LocalityFaq({ locality }: { locality: string }) {
  const items = [
    {
      q: `Are there properties for sale in ${locality}?`,
      a: `Yes. PropAI tracks live ${locality} sale listings sourced directly from WhatsApp broker conversations and refreshed in real time.`,
    },
    {
      q: `Can I find ${locality} properties on rent?`,
      a: `Yes. ${locality} rental listings — from 1 BHK to large apartments and commercial spaces — are live on PropAI and updated continuously.`,
    },
    {
      q: `How fresh are ${locality} listings on PropAI?`,
      a: `Inventory updates continuously from verified broker WhatsApp groups; each card shows when it last landed so you see the newest first.`,
    },
    {
      q: `Do I contact a portal or the actual broker?`,
      a: `You connect directly with the posting broker on WhatsApp — no lead forms, no middlemen.`,
    },
  ];
  return (
    <section className="mt-14" aria-label={`${locality} frequently asked questions`}>
      <h2 className="mb-5 text-[20px] lg:text-[24px] font-semibold text-white">
        {locality} — frequently asked questions
      </h2>
      <div className="divide-y divide-white/10 rounded-2xl border border-white/10 bg-zinc-950/90">
        {items.map((it) => (
          <details key={it.q} className="group p-5">
            <summary className="cursor-pointer list-none text-sm font-medium text-white marker:hidden">
              <span className="inline-flex items-center gap-2">
                <span className="text-green-400 transition-transform group-open:rotate-45">+</span>
                {it.q}
              </span>
            </summary>
            <p className="mt-3 text-sm leading-relaxed text-zinc-300">{it.a}</p>
          </details>
        ))}
      </div>
    </section>
  );
}
