import { NextResponse } from "next/server";
import { getServerSupabase } from "@/lib/supabase";

// Lightweight endpoint powering the homepage "Just landed" ticker.
// Returns the single most-recently-seen listing (sanitized), server-side
// via the service-role client so the anon web key is never exposed.
//
// Keep this cached: the client is allowed to poll, but a public ticker must
// not turn every browser tab into a database query.
export const revalidate = 15;

const TABLES = [
  {
    table: "residential_sale_listings",
    assetType: "residential",
    transactionType: "sale",
    priceField: "total_asking_price",
    hasBhk: true,
  },
  {
    table: "residential_rent_listings",
    assetType: "residential",
    transactionType: "rent",
    priceField: "monthly_rent",
    hasBhk: true,
  },
  {
    table: "commercial_sale_listings",
    assetType: "commercial",
    transactionType: "sale",
    priceField: "total_asking_price",
    hasBhk: false,
  },
  {
    table: "commercial_rent_listings",
    assetType: "commercial",
    transactionType: "rent",
    priceField: "monthly_rent",
    hasBhk: false,
  },
] as const;

type LatestTable = (typeof TABLES)[number];

// Common ad-fragment words that indicate the column contains raw message
// text rather than a clean locality/building name.
const AD_FRAGMENTS = [
  "sqft", "sq ft", "carpet", "built", "super", "area",
  "asking", "asking for", "negotiable", "negotiable", "price", "rent", "sale",
  "parking", "park", "floor", "ground", "terrace", "balcony",
  "road", "linking", "main", "sv ", "sv road", "cpm", "emi",
  "deposit", "maintenance", "society", "lift", "amenities",
  "furnished", "semi", "unfurnished", "warm shell", "cold shell",
  "ready to move", "possession", "rera", "loan", "bank",
  "market pending",
];

// Known Dubai localities — used to extract clean locality from raw text.
const KNOWN_LOCALITIES = [
  "bandra west", "bandra east", "bandra kurla complex", "bkc",
  "santacruz west", "santacruz east", "santacruz",
  "khar west", "khar east", "khar",
  "juhu", "juhunagar", "vile parle west", "vile parle east", "vile parle",
  "andheri west", "andheri east", "andheri",
  "powai", "goregaon west", "goregaon east", "goregaon",
  "malad west", "malad east", "malad",
  "kandivali west", "kandivali east", "kandivali",
  "borivali west", "borivali east", "borivali",
  "dahisar west", "dahisar east", "dahisar",
  "chembur", "tilak nagar", "sion", "matunga", "dadar west", "dadar east", "dadar",
  "worli", "prabhadevi", "lower parel", "mahalaxmi", "mahalaxmi west",
  "marine lines", "churchgate", "colaba", "cuffe parade", "nariman point",
  "fort", "cst", "byculla", "mazgaon", "reay road", "cotton green",
  "sewri", "wadala west", "wadala east", "wadala", "kings circle", "mahim",
  "kharghar", "navi mumbai", "thane west", "thane east", "thane",
  "mira road", "bhayandar", "vasai", "virar", "nallasopara",
  "kalyan", "dombivali", "ambivili", "badlapur", "ulhasnagar",
  "panvel", "kharghar", "taloja", "kamothe", "koperkhairane",
];

function looksLikeAdFragment(text: string): boolean {
  const lower = text.toLowerCase();
  return AD_FRAGMENTS.some((f) => lower.includes(f));
}

function extractLocality(raw: string): string | null {
  const lower = raw.toLowerCase();
  // Try to find a known locality in the raw text
  for (const loc of KNOWN_LOCALITIES) {
    if (lower.includes(loc)) {
      // Return with proper casing
      return loc
        .split(" ")
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ");
    }
  }
  return null;
}

function sanitizeField(raw: string | null, fallback: string | null): string | null {
  if (!raw) return fallback;
  const trimmed = raw.trim();
  if (!trimmed) return fallback;
  if (looksLikeAdFragment(trimmed)) {
    // Try to salvage a known locality from the garbage
    const salvaged = extractLocality(trimmed);
    return salvaged ?? fallback;
  }
  return trimmed;
}

export async function GET() {
  const db = getServerSupabase();
  if (!db) {
    return NextResponse.json({ listing: null }, { status: 200 });
  }
  try {
    // Query each narrow typed table independently. This gives Postgres four
    // index-backed top-1 scans instead of sorting the wide UNION view.
    const rows = await Promise.all(TABLES.map(async (config) => {
      const fields = [
        "id",
        config.hasBhk ? "bhk" : null,
        config.priceField,
        "furnishing_status",
        "building_name",
        "micro_market",
        "locality_raw",
        "locality_resolved",
        "broker_name",
        "updated_at",
        "created_at",
      ].filter(Boolean).join(", ");
      const result = await db
        .from(config.table)
        .select(fields)
        .order("updated_at", { ascending: false })
        .order("created_at", { ascending: false })
        .limit(1)
        .maybeSingle();
      if (result.error || !result.data) return null;
      return { config, row: result.data as unknown as Record<string, unknown> };
    }));

    const latest = rows
      .filter((entry): entry is { config: LatestTable; row: Record<string, unknown> } => entry !== null)
      .sort((a, b) => {
        const aTime = Date.parse(String(a.row.updated_at ?? a.row.created_at ?? ""));
        const bTime = Date.parse(String(b.row.updated_at ?? b.row.created_at ?? ""));
        return bTime - aTime;
      })[0];

    if (!latest) {
      return NextResponse.json({ listing: null }, { status: 200 });
    }

    const { config, row: d } = latest;
    const rawBuilding = (d.building_name as string) ?? null;
    const rawMicroMarket = (d.micro_market as string) ?? null;
    const rawLocationLabel =
      (d.locality_raw as string) ??
      (d.locality_resolved as string) ??
      rawMicroMarket;

    const building = sanitizeField(rawBuilding, null);
    const microMarket = sanitizeField(rawMicroMarket, null);
    const locality = sanitizeField(rawLocationLabel, microMarket);

    const listing = {
      id: d.id as number,
      bhk: d.bhk == null ? null : String(d.bhk),
      price: typeof d[config.priceField] === "number" ? d[config.priceField] as number : null,
      priceUnit: "abs",
      furnishing: (d.furnishing_status as string) ?? null,
      assetType: config.assetType,
      transactionType: config.transactionType,
      building,
      microMarket,
      locality,
      broker: (d.broker_name as string) ?? null,
      lastSeen: ((d.updated_at as string) ?? (d.created_at as string)) ?? null,
    };

    return NextResponse.json({ listing }, { status: 200 });
  } catch {
    return NextResponse.json({ listing: null }, { status: 200 });
  }
}
