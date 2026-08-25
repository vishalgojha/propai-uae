import { getServerSupabase } from "./supabase";
import { getAllBuildings, getAllLocalities, type BuildingSummary, type LocalitySummary } from "./localities";
import { dedupeRecentListings, normalizeBhkFromEvidence } from "./listing-card";

export type PublicCountKey =
  | "localities"
  | "buildings"
  | "listings"
  | "activeListings"
  | "brokers"
  | "raw_messages"
  | "messagesAnalysed";

export type PublicListingSummary = {
  id: number;
  card_type?: string | null;
  bhk: string | null;
  price: number | null;
  price_unit: string | null;
  furnishing: string | null;
  location_label: string | null;
  building_name: string | null;
  summary_title?: string | null;
  landmark_name: string | null;
  micro_market: string | null;
  broker_name: string | null;
  broker_phone?: string | null;
  intent?: string | null;
  area_sqft?: number | null;
  floor_description?: string | null;
  property_type?: string | null;
  observation_count: number | null;
  last_seen: string | null;
  price_raw_text?: string | null;
  source_text?: string | null;
};

function priceFromRawText(value: unknown): number | null {
  const match = String(value ?? "").match(/(?:aed|dhs|dh\.?)?\s*(\d[\d,]*(?:\.\d+)?)\s*(m(?:illion)?|k|thousand)?\b/i);
  if (!match) return null;
  const amount = Number(match[1].replace(/,/g, ""));
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const unit = (match[2] || "").toLowerCase();
  const multiplier = unit.startsWith("m") ? 1_000_000
    : unit === "k" || unit.startsWith("thousand") ? 1_000 : 1;
  return amount * multiplier;
}

export type PublicBrokerSummary = {
  display_name: string;
  listing_count: number | null;
  market_count: number | null;
};

export type PublicDataOverview = {
  counts: Record<PublicCountKey, number>;
  /** False when the live count query could not be read. */
  countsAvailable: boolean;
  activity: PublicActivityPoint[];
  topLocalities: LocalitySummary[];
  topBuildings: BuildingSummary[];
  recentListings: PublicListingSummary[];
};

export type PublicActivityPoint = {
  date: string;
  messages: number;
  parsedRecords: number;
  listings: number;
};

function priceLabel(value: number | null, unit: string | null): string {
  if (value == null || value <= 0) return "Price on request";
  // The public listings view normalizes prices to absolute AED and uses
  // `price_unit = abs`. Older rows may retain `m`/`k`, but the numeric value
  // is still absolute. Format the amount by scale so the homepage never leaks
  // grouped values such as AED 1,430,000 instead of AED 1.43M.
  if (value >= 1_000_000) {
    const m = value / 1_000_000;
    return `AED ${m % 1 === 0 ? m : m.toFixed(2)}M`;
  }
  if (value >= 10_000) {
    return `AED ${Math.round(value / 1_000)}k`;
  }
  return `AED ${Math.round(value).toLocaleString("en-AE")}`;
}

export function formatPublicPrice(value: number | null, unit: string | null): string {
  return priceLabel(value, unit);
}

function buildActivityTimeline(rows: Array<{ created_at: string | null }>, days = 14): PublicActivityPoint[] {
  const points = new Map<string, PublicActivityPoint>();
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  for (let i = days - 1; i >= 0; i -= 1) {
    const date = new Date(today);
    date.setDate(today.getDate() - i);
    const key = date.toISOString().slice(0, 10);
    points.set(key, { date: key, messages: 0, parsedRecords: 0, listings: 0 });
  }

  for (const row of rows) {
    if (!row.created_at) continue;
    const key = row.created_at.slice(0, 10);
    const entry = points.get(key);
    if (entry) entry.messages += 1;
  }

  return Array.from(points.values());
}

export async function getPublicDataOverview(options?: {
  localities?: LocalitySummary[];
  buildings?: BuildingSummary[];
  skipBuildingScan?: boolean;
  skipCounts?: boolean;
  skipLocalities?: boolean;
  skipActivity?: boolean;
}): Promise<PublicDataOverview> {
  const db = getServerSupabase();

  // Single RPC for the counters. If the RPC is unavailable to the public
  // runtime role, recover from the same read paths used for live listings
  // instead of making the whole homepage look empty.
  const countsPromise = db && !options?.skipCounts ? db.rpc("get_public_counts").then(async (res) => {
    if (res.error) {
      console.error("get_public_counts error:", res.error.message);
      const cutoff = new Date(Date.now() - 30 * 86_400_000).toISOString();
      const [listings, activeListings, brokers, rawMessages] = await Promise.all([
        db.from("listings_unified").select("id", { count: "exact", head: true }),
        db.from("listings_unified").select("id", { count: "exact", head: true }).gte("last_seen", cutoff),
        db.from("brokers").select("id", { count: "exact", head: true }),
        db.from("raw_messages").select("id", { count: "exact", head: true }),
      ]);
      const values = [listings, activeListings, brokers, rawMessages];
      if (values.some((value) => value.error)) return null;
      return {
        listings_total: listings.count ?? 0,
        listings_active_30d: activeListings.count ?? 0,
        brokers: brokers.count ?? 0,
        raw_messages: rawMessages.count ?? 0,
      };
    }
    return res.data?.[0] ?? null;
  }) : Promise.resolve(null);

  const [localities, buildings, countsRow] = await Promise.all([
    options?.localities ?? (options?.skipLocalities ? Promise.resolve([]) : getAllLocalities()),
    options?.buildings ?? (options?.skipBuildingScan ? Promise.resolve([]) : getAllBuildings(200)),
    countsPromise,
  ]);

  const listings = Number(countsRow?.listings_total ?? 0);
  const activeListings = Number(countsRow?.listings_active_30d ?? 0);
  const brokers = Number(countsRow?.brokers ?? 0);
  const rawMessages = Number(countsRow?.raw_messages ?? 0);
  const buildingCount = Number(countsRow?.buildings ?? countsRow?.buildings_total ?? buildings.length);

  const topBuildings = [...buildings]
    .sort((a, b) => b.listingCount - a.listingCount || a.name.localeCompare(b.name))
    .slice(0, 8);

  const recentListings: PublicListingSummary[] = [];
  const activity: PublicActivityPoint[] = [];
  const days = 14;

  if (db) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - (days - 1));
    const cutoffIso = cutoff.toISOString();
    // `listings_unified` is a wide UNION view. Query the four typed tables
    // directly so a slow compatibility view cannot blank the homepage.
    const recentSpecs = [
      { table: "residential_sale_listings", cardType: "residential_sale", asset: "residential", intent: "sale", price: "total_asking_price", furnishing: "furnishing_status", hasBhk: true },
      { table: "residential_rent_listings", cardType: "residential_rent", asset: "residential", intent: "rent", price: "monthly_rent", furnishing: "furnishing_status", hasBhk: true },
      { table: "commercial_sale_listings", cardType: "commercial_sale", asset: "commercial", intent: "sale", price: "total_asking_price", furnishing: "fitout_status", hasBhk: false },
      { table: "commercial_rent_listings", cardType: "commercial_rent", asset: "commercial", intent: "rent", price: "monthly_rent", furnishing: "fitout_status", hasBhk: false },
    ] as const;
    const recentRows = (await Promise.all(recentSpecs.map(async (spec) => {
      const selection = `id, ${spec.hasBhk ? "bhk, " : ""}${spec.price}, price_raw_text, raw_payload, carpet_area_sqft, ${spec.furnishing}, summary_title, building_name, landmark_name, micro_market, locality_resolved, locality_raw, broker_name, broker_phone, created_at, updated_at`;
      const { data, error } = await db
        .from(spec.table)
        .select(selection)
        .order("updated_at", { ascending: false, nullsFirst: false })
        .limit(20);
      if (error) {
        console.error(`homepage ${spec.table} error:`, error.message);
        return [];
      }
      return (data ?? []).map((row: any) => ({
        ...row,
        bhk: spec.hasBhk
          ? normalizeBhkFromEvidence(row.bhk ?? null, row.raw_payload?.full_text)
          : null,
        card_type: spec.cardType,
        asset_type: spec.asset,
        intent: spec.intent,
        property_type: spec.asset,
        price: row[spec.price] ?? priceFromRawText(row.price_raw_text ?? row.raw_payload?.full_text),
        price_unit: "abs",
        furnishing: row[spec.furnishing] ?? null,
        area_sqft: row.carpet_area_sqft ?? null,
        location_label: row.micro_market || row.locality_resolved || row.locality_raw || null,
        last_seen: row.updated_at ?? row.created_at ?? null,
        observation_count: null,
        price_raw_text: row.price_raw_text ?? null,
        source_text: row.raw_payload?.full_text ?? null,
      }));
    }))).flat().sort((a, b) => String(b.last_seen || "").localeCompare(String(a.last_seen || ""))).slice(0, 50);

    const [rawRowsRes, parsedRowsRes, listingRowsRes] = options?.skipActivity
      ? [{ data: [], error: null }, { data: [], error: null }, { data: [], error: null }]
      : await Promise.all([
          db.from("raw_messages").select("created_at").gte("created_at", cutoffIso),
          db.from("parsed_output_unified").select("created_at").gte("created_at", cutoffIso),
          db.from("listings_unified").select("created_at").gte("created_at", cutoffIso),
        ]);

    {
      const rows = dedupeRecentListings(recentRows.map((row) => ({
        ...row,
        price_raw_text: row.price_raw_text ?? null,
        price_model: null,
        area_sqft: row.area_sqft ?? null,
        asset_type: null,
        property_type: row.property_type ?? null,
        locality_raw: null,
        locality_resolved: null,
        floor_description: row.floor_description ?? null,
        broker_phone: row.broker_phone ?? null,
        last_seen: row.last_seen ?? null,
        landmark_name: row.landmark_name ?? null,
        intent: row.intent ?? null,
      })));
      for (const row of rows) {
        recentListings.push(row as PublicListingSummary);
      }
    }
    const rawRows = rawRowsRes.error ? [] : (rawRowsRes.data ?? []);
    const parsedRows = parsedRowsRes.error ? [] : (parsedRowsRes.data ?? []);
    const listingRows = listingRowsRes.error ? [] : (listingRowsRes.data ?? []);

    const base = buildActivityTimeline(rawRows, days);
    const byDate = new Map(base.map((point) => [point.date, point]));
    for (const row of parsedRows) {
      if (!row.created_at) continue;
      const key = row.created_at.slice(0, 10);
      const point = byDate.get(key);
      if (point) point.parsedRecords += 1;
    }
    for (const row of listingRows) {
      if (!row.created_at) continue;
      const key = row.created_at.slice(0, 10);
      const point = byDate.get(key);
      if (point) point.listings += 1;
    }
    activity.push(...base);
  }

  return {
    counts: {
      localities: localities.length,
      buildings: buildingCount,
      listings,
      activeListings,
      brokers,
      raw_messages: rawMessages,
      messagesAnalysed: rawMessages,
    },
    countsAvailable: countsRow !== null,
    activity,
    topLocalities: localities.slice(0, 8),
    topBuildings,
    recentListings,
  };
}
