"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { withBasePath } from "@/lib/base-path";
import Link from "next/link";
import { ArrowUpRight, CircleDot, RefreshCw, Sparkles, Users } from "lucide-react";
import * as api from "@/lib/api";
import { cleanGroupName } from "@/lib/whatsapp-display";
import { designTokens } from "@/lib/design-tokens";
import NetworkMap from "@/components/NetworkMap";

const DATAVIZ_SERIES = designTokens.datavizSeries; // cyan, single-series line/area

type Duplicate = { group_a?: { jid?: string; name?: string }; group_b?: { jid?: string; name?: string }; match_type?: string };
type State = {
  groups: api.AuditGroupCard[];
  uniqueParticipants: number;
  totalMembershipRows: number;
  duplicateMemberships: number;
  connectedGroups: number;
  postingGroups24h: number;
  health: api.AuditCaptureHealth | null;
  duplicates: Duplicate[];
  overlap: api.AuditGroupOverlapResponse;
  insights: api.AuditInsights;
  errors: string[];
};

const emptyInsights: api.AuditInsights = { daily_flow: [], markets: [], brokers: [], exclusive_members: {}, total_unique_brokers: 0, total_broker_appearances: 0 };
const emptyState: State = {
  groups: [], uniqueParticipants: 0, totalMembershipRows: 0, duplicateMemberships: 0, connectedGroups: 0, postingGroups24h: 0, health: null, duplicates: [],
  overlap: { pairs: [], groups: [] }, insights: emptyInsights, errors: [],
};

async function safe<T>(label: string, work: () => Promise<T>, fallback: T) {
  try { return { value: await work(), error: "" }; }
  catch (error) { return { value: fallback, error: `${label}: ${error instanceof Error ? error.message : String(error)}` }; }
}

function num(value?: number | string | null) { return Number(value || 0).toLocaleString("en-IN"); }
function ago(value?: string) {
  if (!value) return "never";
  const stamp = new Date(value).getTime();
  if (Number.isNaN(stamp)) return "unknown";
  const minutes = Math.max(0, Math.floor((Date.now() - stamp) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`border border-white/10 bg-[#090909] ${className}`}>{children}</div>;
}

function Kicker({ children }: { children: ReactNode }) {
  return <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">{children}</div>;
}

function QualityRing({ score }: { score: number }) {
  const safeScore = Math.max(0, Math.min(100, score));
  return (
    <div className="relative grid h-12 w-12 shrink-0 place-items-center rounded-full" style={{ background: `conic-gradient(${DATAVIZ_SERIES} ${safeScore * 3.6}deg, #27272a 0deg)` }}>
      <div className="grid h-10 w-10 place-items-center rounded-full bg-[#090909] text-[11px] font-semibold tabular-nums text-zinc-200">{safeScore}</div>
    </div>
  );
}

function SignalTrace({ points }: { points: api.AuditInsights["daily_flow"] }) {
  const values = points.map((item) => item.posts);
  const max = Math.max(1, ...values);
  const coords = values.map((value, index) => `${8 + (index * 84) / Math.max(1, values.length - 1)},${72 - (value / max) * 56}`).join(" ");
  return (
    <div className="mt-5">
      <svg viewBox="0 0 100 82" preserveAspectRatio="none" className="h-28 w-full overflow-visible" aria-label="Seven day opportunity flow">
        <defs><linearGradient id="flow" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={DATAVIZ_SERIES} stopOpacity=".22"/><stop offset="1" stopColor={DATAVIZ_SERIES} stopOpacity="0"/></linearGradient></defs>
        {[16, 44, 72].map((y) => <line key={y} x1="0" x2="100" y1={y} y2={y} stroke="#27272a" strokeWidth=".35" />)}
        {coords ? <><polygon points={`8,76 ${coords} 92,76`} fill="url(#flow)"/><polyline points={coords} fill="none" stroke={DATAVIZ_SERIES} strokeWidth="1.2" vectorEffect="non-scaling-stroke"/></> : null}
      </svg>
      <div className="grid grid-cols-7 text-center text-[9px] uppercase tracking-wide text-zinc-600">
        {points.map((item) => <span key={item.date}>{new Date(item.date).toLocaleDateString("en-IN", { weekday: "short" })}</span>)}
      </div>
    </div>
  );
}

export default function AuditPage() {
  return (
    <div className="mx-auto max-w-3xl py-16 text-center">
      <h1 className="text-xl font-semibold text-white">WhatsApp audit is paused</h1>
      <p className="mt-3 text-sm text-zinc-400">
        Extraction paused — data will resume once the new pipeline is live.
      </p>
      <a href={withBasePath("/connections")} className="mt-6 inline-flex rounded-lg bg-[#3EE88A] px-4 py-2 text-sm font-semibold text-black hover:bg-[#74f0a5]">
        Manage WhatsApp connections
      </a>
    </div>
  );
}

function AuditWorkspacePage() {
  const [state, setState] = useState<State>(emptyState);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    const [groups, health, duplicates, overlap, insights] = await Promise.all([
      safe("groups", () => api.getAuditGroups(), {
        groups: [],
        total_unique_senders: 0,
        total_unique_participants: 0,
        total_membership_rows: 0,
        duplicate_memberships: 0,
        connected_groups: 0,
        posting_groups_24h: 0,
      } as api.AuditGroupsResponse),
      safe("capture", () => api.getAuditCaptureHealth(), null),
      safe("duplicates", () => api.getAuditDuplicates(), []),
      safe("overlap", () => api.getAuditGroupOverlap(), { pairs: [], groups: [] } as api.AuditGroupOverlapResponse),
      safe("insights", () => api.getAuditInsights(), emptyInsights),
    ]);
    setState({
      groups: groups.value.groups,
      uniqueParticipants: groups.value.total_unique_participants || groups.value.total_unique_senders,
      totalMembershipRows: groups.value.total_membership_rows ?? 0,
      duplicateMemberships: groups.value.duplicate_memberships ?? 0,
      connectedGroups: groups.value.connected_groups ?? 0,
      postingGroups24h: groups.value.posting_groups_24h ?? 0,
      health: health.value, duplicates: duplicates.value, overlap: overlap.value, insights: insights.value,
      errors: [groups.error, health.error, duplicates.error, overlap.error, insights.error].filter(Boolean),
    });
    setLoading(false);
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  useEffect(() => {
    const interval = window.setInterval(() => { void load(); }, 120000);
    return () => window.clearInterval(interval);
  }, [load]);

  const listings = state.groups.reduce((sum, group) => sum + group.listings, 0);
  const requirements = state.groups.reduce((sum, group) => sum + group.requirements, 0);
  const brokersAcrossGroups = state.groups.reduce((sum, group) => sum + (group.active_brokers || 0), 0);
  const brokersOverall = state.insights.total_unique_brokers || (state.health?.stage?.brokers ?? 0);
  const brokerAppearances = state.insights.total_broker_appearances || brokersAcrossGroups;
  const bestGroups = [...state.groups].map((group) => ({
    ...group,
    exclusive: state.insights.exclusive_members[group.jid] || state.insights.exclusive_members[group.name] || 0,
    score: Math.round(Math.min(100, (group.coverage * .45) + (Math.min(1, group.observations / Math.max(1, group.messages)) * 35) + (group.status === "live" ? 20 : 0))),
  })).sort((a, b) => b.score - a.score).slice(0, 6);
  const today = state.insights.daily_flow.at(-1);
  const topMarket = state.insights.markets[0];
  const redundant = state.overlap.pairs.filter((pair) => pair.overlap_pct >= 60);
  const initialLoading = loading && state.groups.length === 0 && state.health === null;
  const coreUnavailable = state.groups.length === 0
    && state.errors.some((error) => error.startsWith("groups:"));

  return (
    <main className="mx-auto max-w-[1500px] space-y-5 px-4 py-5 text-white sm:px-6">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 pb-5">
        <div>
          <Kicker>WhatsApp intelligence</Kicker>
          <h1 className="mt-2 text-3xl font-semibold tracking-[-0.04em] sm:text-4xl">Your market, decoded.</h1>
          <p className="mt-2 max-w-2xl text-sm text-zinc-500">Reach, signal quality and market movement across every group PropAI monitors.</p>
        </div>
        <button type="button" onClick={load} disabled={loading} className="inline-flex items-center gap-2 rounded-md border border-white/15 bg-white px-3 py-2 text-xs font-semibold text-black transition hover:bg-zinc-200 disabled:opacity-50">
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh intelligence
        </button>
      </header>

      {state.errors.length ? <div className="border border-red-500/25 bg-red-500/[0.04] px-4 py-3 text-xs text-red-300">Some intelligence is temporarily unavailable: {state.errors.join(" · ")}</div> : null}

      {initialLoading ? (
        <Card className="grid min-h-72 place-items-center p-8 text-center">
          <div><RefreshCw className="mx-auto h-5 w-5 animate-spin text-zinc-500" /><div className="mt-3 text-sm font-medium text-zinc-300">Loading your WhatsApp intelligence…</div><div className="mt-1 text-xs text-zinc-600">Reading groups, participants and market signals.</div></div>
        </Card>
      ) : coreUnavailable ? (
        <Card className="grid min-h-72 place-items-center p-8 text-center">
          <div className="max-w-lg"><div className="text-lg font-semibold text-zinc-100">Market intelligence could not be loaded</div><p className="mt-2 text-sm leading-6 text-zinc-500">The dashboard has not received group data, so PropAI will not show misleading zero counts.</p><button type="button" onClick={() => void load()} disabled={loading} className="mt-5 inline-flex items-center gap-2 rounded-md bg-white px-3 py-2 text-xs font-semibold text-black disabled:opacity-50"><RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> Try again</button></div>
        </Card>
      ) : (
      <>
      <section className="grid gap-4 xl:grid-cols-[1.5fr_.9fr]">
        <Card className="relative overflow-hidden p-6 sm:p-8">
          <div className="absolute right-0 top-0 h-48 w-48 bg-[radial-gradient(circle,rgba(255,255,255,.07),transparent_65%)]" />
          <Kicker>Today&apos;s brief</Kicker>
          <p className="mt-5 max-w-4xl text-2xl font-medium leading-tight tracking-[-0.035em] text-zinc-100 sm:text-4xl">
            PropAI found <span className="text-white">{num(today?.posts || state.health?.total_parsed_today)}</span> market posts from a network of <span className="text-white">{num(state.uniqueParticipants)}</span> participants across <span className="text-white">{num(state.connectedGroups)}</span> connected groups.
          </p>
          <div className="mt-4 flex flex-wrap gap-2 text-[11px] font-medium">
            <span className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-zinc-300">
              {num(state.postingGroups24h)} groups posted in the last 24h
            </span>
            <span className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-zinc-300">
              {num(state.totalMembershipRows)} raw memberships · {num(state.duplicateMemberships)} duplicates
            </span>
          </div>
          <div className="mt-8 grid grid-cols-2 gap-px border border-white/10 bg-white/10 lg:grid-cols-3">
            {[["Listings", listings], ["Requirements", requirements], ["Markets", state.insights.markets.length], ["Unique brokers", brokersOverall], ["Broker appearances", brokerAppearances], ["Parser ready", `${Math.min(100, Math.round(state.health?.parser_success_rate || 0))}%`]].map(([label, value]) => (
              <div key={label} className="bg-[#090909] p-4"><Kicker>{label}</Kicker><div className="mt-2 text-xl font-semibold tabular-nums">{typeof value === "number" ? num(value) : value}</div></div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-5 text-zinc-500">
            "Unique brokers" is the deduplicated total across all groups. "Broker appearances" counts how many times broker records appear across groups, so you can see overlap — the gap between these two numbers shows how many brokers are shared across multiple groups.
          </p>
        </Card>
        <Card className="p-5">
          <div className="flex items-center justify-between"><div><Kicker>Opportunity flow</Kicker><div className="mt-2 text-sm text-zinc-300">Last seven days</div></div><CircleDot className="h-4 w-4 text-zinc-600" /></div>
          <SignalTrace points={state.insights.daily_flow} />
          <div className="mt-3 flex justify-between border-t border-white/10 pt-4 text-xs"><span className="text-zinc-500">Peak market</span><span className="font-medium text-zinc-200">{topMarket?.name || "Building signal"}</span></div>
        </Card>
      </section>

      <section>
        <Card className="p-5 sm:p-6">
          <Kicker>Network map</Kicker>
          <NetworkMap
            groups={state.groups}
            pairs={state.overlap.pairs}
            uniqueMembers={state.uniqueParticipants}
            redundantCount={redundant.length}
            duplicateMemberships={state.duplicateMemberships}
          />
        </Card>
      </section>

      <section className="grid gap-4 xl:grid-cols-[1fr_.85fr]">
        <Card className="p-5">
          <Kicker>Best groups</Kicker>
          <h2 className="mt-2 text-lg font-semibold">Signal worth watching</h2>
          <div className="mt-3 divide-y divide-white/[0.07]">
            {bestGroups.map((group, index) => <Link href={`/audit/groups/${encodeURIComponent(group.jid)}`} key={group.jid} className="flex items-center gap-4 p-4 transition hover:bg-white/[0.03]">
              <div className="w-5 text-xs tabular-nums text-zinc-600">{String(index + 1).padStart(2, "0")}</div>
              <QualityRing score={group.score} />
              <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium text-zinc-100">{cleanGroupName(group.name)}</div><div className="mt-1 text-[11px] text-zinc-500">{num(group.observations)} posts · {num(group.senders_count)} participants · {num(group.exclusive)} exclusive</div></div>
              <ArrowUpRight className="h-4 w-4 text-zinc-700" />
            </Link>)}
            {!bestGroups.length ? <div className="p-8 text-center text-xs text-zinc-600">Group scores appear after capture starts.</div> : null}
          </div>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        <Card className="overflow-hidden">
          <div className="border-b border-white/10 p-5"><Kicker>Market pulse</Kicker><h2 className="mt-2 text-lg font-semibold">Where activity is concentrating</h2></div>
          <div className="divide-y divide-white/[0.07]">{state.insights.markets.slice(0, 6).map((market, index) => <div key={market.name} className="grid grid-cols-[24px_1fr_auto] items-center gap-3 px-5 py-3"><span className="text-[10px] text-zinc-600">{index + 1}</span><div><div className="text-sm font-medium">{market.name}</div><div className="mt-1 text-[10px] text-zinc-500">{num(market.brokers)} brokers · {num(market.requirements)} requirements</div></div><div className="text-right"><div className="text-sm font-semibold tabular-nums">{num(market.posts)}</div><div className="text-[9px] text-zinc-600">posts</div></div></div>)}</div>
        </Card>

        <Card className="p-5 lg:col-span-2 xl:col-span-1">
          <div className="flex items-start justify-between"><div><Kicker>PropAI recommendations</Kicker><h2 className="mt-2 text-lg font-semibold">Make the network sharper</h2></div><Sparkles className="h-4 w-4 text-zinc-600" /></div>
          <div className="mt-5 space-y-3">
            <div className="border-l border-white/20 pl-4"><div className="text-sm font-medium">Prioritise {bestGroups[0]?.name || "your strongest group"}</div><p className="mt-1 text-xs leading-5 text-zinc-500">It currently has the best balance of freshness, extraction coverage and useful market posts.</p></div>
            <div className="border-l border-white/20 pl-4"><div className="text-sm font-medium">Review {num(redundant.length)} redundant group pairs</div><p className="mt-1 text-xs leading-5 text-zinc-500">These groups share over 60% of their active participants. Lower-priority duplicates can be muted.</p></div>
            <div className="border-l border-white/20 pl-4"><div className="text-sm font-medium">Follow demand in {topMarket?.name || "your top market"}</div><p className="mt-1 text-xs leading-5 text-zinc-500">{num(topMarket?.requirements)} active requirements are visible against {num(topMarket?.listings)} listings.</p></div>
          </div>
          <Link href="/inbox" className="mt-6 inline-flex items-center gap-2 rounded-md border border-white/15 px-3 py-2 text-xs font-semibold text-zinc-200 hover:bg-white/[0.05]">Open market inbox <ArrowUpRight className="h-3.5 w-3.5" /></Link>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card className="p-5"><div className="flex gap-3"><Users className="mt-0.5 h-4 w-4 text-zinc-500"/><div><Kicker>What member overlap means</Kicker><p className="mt-2 text-sm leading-6 text-zinc-400">It counts the same WhatsApp participants appearing across multiple groups. It is not your total broker count. Exclusive members only appear in one monitored group and represent reach you would lose by leaving it.</p></div></div></Card>
        <Card className="p-5"><Kicker>Capture health</Kicker><div className="mt-3 flex items-center justify-between"><div><div className="text-sm font-medium">{state.health?.degraded ? "Needs attention" : "Pipeline is healthy"}</div><div className="mt-1 text-xs text-zinc-500">Last WhatsApp event {ago(state.health?.last_webhook)} · {num(state.health?.queue_backlog)} queued</div></div><div className={`h-2 w-2 rounded-full ${state.health?.degraded ? "bg-red-500" : "bg-[#3EE88A]"}`} /></div></Card>
      </section>
      </>
      )}
    </main>
  );
}
