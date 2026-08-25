import Link from "next/link";

const FOOTER_LINKS = {
  browse: [
    { label: "Search listings", href: "/search" },
    { label: "Property map", href: "/map" },
    { label: "All localities", href: "/localities" },
  ],
  support: [
    { label: "How it works", href: "/about#how-it-works" },
    { label: "Why no photos", href: "/about#no-photos" },
    { label: "Search tips", href: "/search" },
  ],
  company: [
    { label: "About PropAI", href: "/about" },
    { label: "Contact", href: "/contact" },
    { label: "Localities", href: "/localities" },
  ],
};

export default function SiteFooter() {
  return (
    <footer className="site-footer border-t border-white/10 bg-black">
      <div className="max-w-[1600px] mx-auto px-4 lg:px-6 py-12 lg:py-16">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-8 lg:gap-12 mb-12">
          <div className="lg:col-span-1">
            <Link href="/" className="flex items-center gap-2 mb-6" aria-label="PropAI home">
              <span className="text-xl font-bold tracking-tight">
                Prop<span className="text-[#D8E3D0]">AI</span>
              </span>
            </Link>
            <p className="text-[15px] text-zinc-500 max-w-xs">
              PropAI reads WhatsApp broker groups so you get real, fresh
              Dubai listings — and a direct line to the broker.
            </p>
          </div>
          <nav aria-label="Browse">
            <h4 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-4">Browse</h4>
            <ul className="space-y-3">
              {FOOTER_LINKS.browse.map((link) => (
                <li key={link.href}>
                  <Link href={link.href} className="text-[15px] text-zinc-400 hover:text-white transition-all duration-base hover:scale-[1.02] active:scale-[0.98]">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <nav aria-label="Support">
            <h4 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-4">Support</h4>
            <ul className="space-y-3">
              {FOOTER_LINKS.support.map((link) => (
                <li key={link.href}>
                  <Link href={link.href} className="text-[15px] text-zinc-400 hover:text-white transition-all duration-base hover:scale-[1.02] active:scale-[0.98]">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <nav aria-label="Company">
            <h4 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-4">Company</h4>
            <ul className="space-y-3">
              {FOOTER_LINKS.company.map((link) => (
                <li key={link.href}>
                  <Link href={link.href} className="text-[15px] text-zinc-400 hover:text-white transition-all duration-base hover:scale-[1.02] active:scale-[0.98]">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
        <div className="border-t border-white/[0.06] pt-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-zinc-600">
          <p>© {new Date().getFullYear()} PropAI. Listings sourced from Dubai broker WhatsApp networks.</p>
          <p>Dubai&apos;s freshest property inventory, directly from brokers.</p>
        </div>
      </div>
    </footer>
  );
}
