import Link from "next/link";
import { ArrowRight, Mail, MessageSquare, Building2 } from "lucide-react";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";

export const metadata = {
  title: "Contact PropAI — Reach the Team & List Your Inventory",
  description:
    "Get in touch with PropAI. Brokers can list inventory directly; buyers and tenants reach verified brokers straight through WhatsApp.",
};

const CHANNELS = [
  {
    icon: MessageSquare,
    title: "For brokers — list your inventory",
    body: "Join the network and post listings directly into PropAI from your WhatsApp groups. Free to join.",
    cta: "Get started",
    href: "https://app.propai.live/auth/signup",
  },
  {
    icon: Building2,
    title: "For brokers — existing account",
    body: "Already part of the network? Sign in to manage your listings and track what's live.",
    cta: "Broker login",
    href: "https://app.propai.live/auth/login",
  },
  {
    icon: Mail,
    title: "General enquiries",
    body: "Questions about PropAI, partnerships, or press? Email us and we'll get back to you.",
    cta: "hello@propai.live",
    href: "mailto:hello@propai.live",
  },
];

export default function ContactPage() {
  return (
    <div className="min-h-screen bg-black text-white">
      <SiteHeader />
      <main className="max-w-3xl mx-auto px-4 lg:px-6 py-10 lg:py-16">
        <h1 className="text-[32px] lg:text-[44px] leading-[1.1] font-bold text-white mb-6">
          Contact <span className="text-green-400">PropAI</span>
        </h1>

        <p className="text-[15px] lg:text-[17px] text-zinc-400 leading-relaxed mb-10 max-w-2xl">
          PropAI connects you straight to Dubai&apos;s brokers — no middleman chatbot.
          Pick the channel that fits you below.
        </p>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {CHANNELS.map((c) => {
            const Icon = c.icon;
            return (
              <a
                key={c.title}
                href={c.href}
                className="group flex flex-col gap-3 bg-zinc-900/50 border border-white/10 rounded-2xl p-5 transition-colors hover:border-green-400/40 hover:bg-zinc-900/90"
              >
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-green-400/10 text-green-300">
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </span>
                <h2 className="text-[16px] font-semibold text-white">{c.title}</h2>
                <p className="text-sm text-zinc-400 leading-relaxed">{c.body}</p>
                <span className="mt-auto inline-flex items-center gap-1.5 text-sm font-medium text-green-300 group-hover:text-green-200 transition-colors">
                  {c.cta}
                  <ArrowRight className="h-4 w-4" aria-hidden="true" />
                </span>
              </a>
            );
          })}
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
