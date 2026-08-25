import { getAllBuildings, getAllLocalities, isJunkBuildingName, type BuildingSummary, type LocalitySummary } from "./localities";
import { canonicalLocality } from "./locality-canon";
import { getServerSupabase, slugify } from "./supabase";
import { dedupeRecentListings } from "./listing-card";
import { extractLocalityWithAI } from "./locality-ai";
import { getNearbyLocalityNames } from "./related-searches";

export type ParsedNaturalSearch = {
  query: string;
  locality: string | null;
  localityStated: boolean;
  statedLocalityText: string | null;
  bhk: number | null;
  intent: "rent" | "sale" | null;
  asset: "residential" | "commercial" | null;
  minPrice: number | null;
  maxPrice: number | null;
  furnishing: "furnished" | "semi-furnished" | "unfurnished" | null;
  tokens: string[];
  matchedLocalities: LocalitySummary[];
  buildingName?: string | null;
  _confidence?: number;
};

export type NaturalSearchRow = {
  id: number;
  intent: string | null;
  asset_type: string | null;
  property_type: string | null;
  bhk: string | null;
  price: number | null;
  price_unit: string | null;
  price_raw_text: string | null;
  price_model: string | null;
  price_per_sqft: number | null;
  area_sqft: number | null;
  furnishing: string | null;
  floor_description: string | null;
  view: string | null;
  location_label: string | null;
  building_name: string | null;
  landmark_name: string | null;
  micro_market: string | null;
  locality_raw: string | null;
  locality_resolved: string | null;
  broker_name: string | null;
  broker_phone: string | null;
  first_seen: string | null;
  last_seen: string | null;
  observation_count: number | null;
  latitude: number | null;
  longitude: number | null;
};

export type NaturalSearchResult = NaturalSearchRow & {
  score: number;
  priceLabel: string;
  matchedOn: string[];
  resultType: "locality" | "building";
};

export type NoResultsReason = "no_intent" | "locality_unmatched" | "no_matches" | null;

export type NaturalSearchState = {
  parsed: ParsedNaturalSearch;
  results: NaturalSearchResult[];
  totalScanned: number;
  suggestions: LocalitySummary[];
  hasData: boolean;
  localityUnmatched: boolean;
  localitySuggestions: LocalitySummary[];
  noResultsReason: NoResultsReason;
};

const MONEY_UNITS: Record<string, number> = {
  m: 1_000_000,
  mn: 1_000_000,
  million: 1_000_000,
  millions: 1_000_000,
  k: 1_000,
  thousand: 1_000,
  // Legacy Indian units kept so old shared queries still parse.
  cr: 1_00_00_000,
  crore: 1_00_00_000,
  crores: 1_00_00_000,
  l: 1_00_000,
  lac: 1_00_000,
  lakh: 1_00_000,
  lakhs: 1_00_000,
};

const LISTING_FIELDS = [
  "id",
  "intent",
  "asset_type",
  "property_type",
  "bhk",
  "price",
  "price_unit",
  "price_raw_text",
  "price_model",
  "price_per_sqft",
  "area_sqft",
  "furnishing",
  "floor_description",
  "view",
  "location_label",
  "building_name",
  "landmark_name",
  "micro_market",
  "locality_raw",
  "locality_resolved",
  "broker_name",
  "broker_phone",
  "first_seen",
  "last_seen",
  "observation_count",
] as const;

// Keep the second-stage natural-language scorer, but ask Postgres for a small
// relevant candidate set first.  This is deliberately well above the 24-card
// UI limit so scoring still has enough choice without shipping every listing
// to the Next.js server on each keystroke.
const SEARCH_CANDIDATE_LIMIT = 300;
const MAX_CANDIDATE_TOKENS = 4;

// Conversational-search slang / abbreviation expansion. Applied before
// normalization so "3 bhi bandar w" maps to "3 bhk bandra west" and fuzzy
// matching can do its job. Only whole-token replacements to avoid corrupting
// substrings (e.g. "w" only as a standalone token, never inside "powai").
const SLANG_MAP: Record<string, string> = {
  bhi: "bhk",
  bhk: "bhk",
  bh: "bhk",
  bandar: "bandra",
  vileparle: "vile parle",
  vileparla: "vile parle",
  w: "west",
  e: "east",
  rd: "road",
  rd_: "road",
  apt: "apartment",
  appt: "apartment",
  flat: "apartment",
  ph: "plot",
  bldg: "building",
  bldng: "building",
  juhu: "juhu",
  andheri: "andheri",
  goregaon: "goregaon",
  borivali: "borivali",
  khar: "khar",
  chembur: "chembur",
  parel: "parel",
  worli: "worli",
  dadar: "dadar",
  santacruz: "santacruz",
  vashi: "vashi",
  malad: "malad",
  kandivali: "kandivali",
  kandivli: "kandivli",
  powai: "powai",
  thane: "thane",
};

function expandSlang(value: string): string {
  return value
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .map((tok) => SLANG_MAP[tok] ?? tok)
    .join(" ")
    .trim();
}

function normalizeText(value: string): string {
  const expanded = expandSlang(value);
  return expanded.replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

function candidateTokens(query: string): string[] {
  const ignored = new Set([
    "a", "an", "and", "any", "apartment", "at", "bhk", "buy", "commercial",
    "flat", "for", "from", "furnished", "in", "lease", "on", "property", "rent",
    "rental", "residential", "sale", "sell", "semi", "semifurnished", "to",
    "unfurnished", "with",
  ]);
  return Array.from(new Set(
    normalizeText(query)
      .split(" ")
      .filter((token) => token.length >= 3 && !ignored.has(token) && !/^\d+$/.test(token)),
  )).slice(0, MAX_CANDIDATE_TOKENS);
}

function postgrestLikeToken(token: string): string {
  // We only use normalized alphanumeric tokens in the PostgREST filter, so
  // they cannot alter its comma/parenthesis filter grammar.
  return `%${token.replace(/[^a-z0-9]/gi, "")}%`;
}

// Base locality names recognised when extracting a stated locality from a query
// (used by detectLocalityStated / extractStatedLocalityPhrase). Mirrors the
// slang map's expanded forms so "jumeirah" -> "jumeirah" is caught.
const BASE_NAMES = new Set([
  // UAE / Dubai
  "dubai",
  "marina",
  "jbr",
  "jlt",
  "jvc",
  "jvt",
  "difc",
  "zaabeel",
  "deira",
  "karama",
  "mirdif",
  "satwa",
  "jumeirah",
  "barsha",
  "furjan",
  "remraam",
  "mudon",
  "arjan",
  "liwan",
  "majan",
  "meydan",
  "reem",
  "warqa",
  "muhaisnah",
  "nahda",
  "business",
  "downtown",
  "palm",
  "sports",
  "production",
  "international",
  "academic",
  "discovery",
  "springs",
  "meadows",
  "lakes",
  "views",
  "greens",
  // Legacy Indian localities kept so old shared queries still parse
  "bandra",
  "andheri",
  "goregaon",
  "juhu",
  "powai",
  "khar",
  "chembur",
  "thane",
  "navi",
  "mumbai",
  "delhi",
  "bangalore",
  "bengaluru",
  "hyderabad",
  "pune",
  "chennai",
  "kolkata",
  "gurgaon",
  "gurugram",
  "noida",
  "vile",
  "borivali",
  "kandivali",
  "parel",
  "worli",
  "dadar",
  "santacruz",
  "vashi",
  "malad",
]);

function baseNameRegex(): RegExp {
  return new RegExp(`\\b(${Array.from(BASE_NAMES).join("|")})\\b`);
}

// Trigram overlap (Jaccard) for fuzzy locality matching — catches typos and
// phonetic variants ("Bandra BKC" vs "Bandra Bkc") after slang expansion.
function trigrams(s: string): Set<string> {
  const t = normalizeText(s).replace(/\s+/g, "");
  const out = new Set<string>();
  if (t.length < 3) {
    if (t) out.add(t);
    return out;
  }
  for (let i = 0; i < t.length - 2; i += 1) out.add(t.slice(i, i + 3));
  return out;
}

function trigramSimilarity(a: string, b: string): number {
  const ta = trigrams(a);
  const tb = trigrams(b);
  if (ta.size === 0 || tb.size === 0) return 0;
  let inter = 0;
  for (const g of ta) if (tb.has(g)) inter += 1;
  return inter / (ta.size + tb.size - inter);
}

function formatPrice(value: number | null): string {
  if (value == null) return "Price on request";
  if (value >= 1_000_000) {
    const m = value / 1_000_000;
    return `AED ${m % 1 === 0 ? m : m.toFixed(1)}M`;
  }
  if (value >= 10_000) {
    return `AED ${Math.round(value / 1_000)}k`;
  }
  return `AED ${Math.round(value).toLocaleString("en-AE")}`;
}

function moneyValue(amount: string, unit?: string | null): number | null {
  const numeric = Number.parseFloat(amount);
  if (!Number.isFinite(numeric)) return null;
  const multiplier = unit ? MONEY_UNITS[unit.toLowerCase()] : null;
  if (multiplier) return Math.round(numeric * multiplier);
  return Math.round(numeric);
}

function parseBhk(query: string): number | null {
  const lower = query.toLowerCase();
  if (/\bstudio\b/.test(lower)) return 0;
  const match = lower.match(/\b(\d+(?:\.\d+)?)\s*bhk\b/);
  if (!match) return null;
  return Number.parseFloat(match[1]);
}

function parseIntent(query: string): "rent" | "sale" | null {
  const lower = query.toLowerCase();
  if (/\b(rent|rental|lease|leasing|tenant)\b/.test(lower)) return "rent";
  if (/\b(sale|sell|selling|purchase|buy|buying|resale)\b/.test(lower)) return "sale";
  return null;
}

function parseFurnishing(query: string): ParsedNaturalSearch["furnishing"] {
  const lower = query.toLowerCase();
  if (/\bfully\s+furnished\b|\bfull\s*furn\b|\bff\b/.test(lower)) return "furnished";
  if (/\bsemi\s+furnished\b|\bsemi\s*fur\b|\bsf\b/.test(lower)) return "semi-furnished";
  if (/\bunfurnished\b|\bnon[-\s]?furnished\b/.test(lower)) return "unfurnished";
  return null;
}

// Detect an explicit residential/commercial intent from the query. Commercial
// cues mirror the ingestion keyword set (office/shop/showroom/warehouse/godown/
// retail). Absent those, we don't force a bucket — most queries are residential.
function parseAsset(query: string): "residential" | "commercial" | null {
  const lower = query.toLowerCase();
  if (
    /\b(commercial|office|shop|showroom|warehouse|godown|retail|co[- ]?working|coworking|industrial|factory|plot|land)\b/.test(lower)
  ) {
    return "commercial";
  }
  if (/\b(residential|apartment|flat|house|villa|society|residence)\b/.test(lower)) {
    return "residential";
  }
  return null;
}

function parseBudget(query: string): { minPrice: number | null; maxPrice: number | null } {
  const lower = query.toLowerCase();
  const unitHint = lower.match(/\b(m|mn|millions?|cr|crores?|lacs?|lakhs?|k|thousand)\b/);

  const range = lower.match(
    /\b(?:budget|between|from|within|under|below|max|upto|up to)?\s*(?:aed|dhs)?\s*(\d+(?:\.\d+)?)\s*(?:-|to|and|–|—)\s*(\d+(?:\.\d+)?)\s*(m|mn|million|millions|cr|crore|crores|l|lac|lakh|lakhs|k|thousand)?\b/,
  );
  if (range) {
    const unit = range[3] || unitHint?.[1] || null;
    const min = moneyValue(range[1], unit);
    const max = moneyValue(range[2], unit);
    return { minPrice: min, maxPrice: max };
  }

  const under = lower.match(
    /\b(?:budget|under|below|max|upto|up to|within|less than)\s*(?:aed|dhs)?\s*(\d+(?:\.\d+)?)\s*(m|mn|million|millions|cr|crore|crores|l|lac|lakh|lakhs|k|thousand)?\b/,
  );
  if (under) {
    const unit = under[2] || unitHint?.[1] || null;
    const value = moneyValue(under[1], unit);
    return { minPrice: null, maxPrice: value };
  }

  return { minPrice: null, maxPrice: null };
}

export function findLocalityMatches(query: string, localities: LocalitySummary[]): LocalitySummary[] {
  const qText = normalizeText(query);
  const betweenMatch = qText.match(/\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+|$)/);
  if (betweenMatch) {
    const first = betweenMatch[1].split(/[^a-z0-9]+/)[0];
    const second = betweenMatch[2].split(/[^a-z0-9]+/)[0];
    const matches: LocalitySummary[] = [];
    if (first) {
      const firstMatches = localities.filter((loc) => {
        const locText = normalizeText(loc.locality);
        return locText.includes(first) || locText.split(/\s+/).some((w) => w === first);
      });
      matches.push(...firstMatches);
    }
    if (second) {
      const secondMatches = localities.filter((loc) => {
        const locText = normalizeText(loc.locality);
        return locText.includes(second) || locText.split(/\s+/).some((w) => w === second);
      });
      for (const m of secondMatches) {
        if (!matches.includes(m)) matches.push(m);
      }
    }
    if (matches.length > 0) {
      return matches
        .sort((a, b) => b.listingCount - a.listingCount)
        .slice(0, 6);
    }
  }
  const qSlug = canonicalLocality(query).slug;
  const scored = localities.map((loc) => {
    const locSlug = canonicalLocality(loc.locality).slug;
    const locText = normalizeText(loc.locality);
    let score = 0;
    if (!locSlug) return { loc, score };
    if (qSlug === locSlug || qText === locText) score = 100;
    else if (qSlug.includes(locSlug) || qText.includes(locText)) score = 80;
    else if (locSlug.includes(qSlug) && qSlug.length >= 3) score = 55;
    else {
      const locWords = locText.split(/\s+/).filter((w) => w.length >= 3);
      const matchingWords = locWords.filter((w) => {
        const wordRe = new RegExp(`\\b${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`);
        return wordRe.test(qText);
      });
      if (matchingWords.length > 0 && matchingWords.length >= Math.ceil(locWords.length / 2)) {
        score = 70;
      } else if (matchingWords.length > 0 && matchingWords[0].length >= 4) {
        score = 50;
      } else if (
        qText
          .split(" ")
          .filter((part) => part.length >= 3)
          .every((part) => locText.includes(part))
      ) {
        score = 40;
      } else {
        const sim = trigramSimilarity(query, loc.locality);
        if (sim >= 0.5) score = Math.round(30 + sim * 20);
      }
    }
    return { loc, score };
  });
  return scored
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || b.loc.listingCount - a.loc.listingCount)
    .slice(0, 3)
    .map((entry) => entry.loc);
}

// Detects whether the user actually named a locality in the query, even if it
// didn't resolve to a known gazetteer entry. Compound forms ("Bandra East",
// "Andheri West") are recognised as a base name + directional suffix so that a
// stated locality is never silently discarded into a broad, locality-less search.
function detectLocalityStated(query: string): boolean {
  const qText = normalizeText(query);
  const parts = qText.split(" ").filter(Boolean);
  const directional = /\b(east|west|north|south|central|e|w|n|s)\b/;
  const baseName = baseNameRegex();

  if (/\bbetween\s+.+\s+and\s+.+\b/.test(qText)) return true;

  if (/\b(in|at|near|around|locality|area)\b/.test(qText)) return true;

  for (let i = 0; i < parts.length; i += 1) {
    if (baseName.test(parts[i])) {
      const next = parts[i + 1];
      if (!next || directional.test(next)) return true;
    }
  }
  return false;
}

// Extracts the human-readable locality phrase the user stated, for display in
// the "we don't track X yet" banner. Unlike detectLocalityStated (boolean),
// this returns the actual phrase — e.g. "3 bhk in Bandra East" -> "Bandra East".
// Extraction stops at the next stop token (bhk, budget keywords, end) so the
// BHK/budget portion of the query is never captured as the locality.
function extractStatedLocalityPhrase(query: string): string | null {
  const qText = normalizeText(query);
  const parts = qText.split(" ").filter(Boolean);
  if (parts.length === 0) return null;

  const directional = /\b(east|west|north|south|central)\b/;
  const baseName = baseNameRegex();
  const stopAfter = /\b(bhk|rk|studio|budget|under|below|max|upto|up to|within|less than|rent|rental|sale|sell|buy|buying|purchase|furnished|semi|unfurnished|sqft|sq\.?\s*ft|area)\b/;

  // Case 0: "between X and Y" range pattern
  const betweenMatch = qText.match(/\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+|$)/);
  if (betweenMatch) {
    const first = betweenMatch[1].split(stopAfter)[0].trim();
    const second = betweenMatch[2].split(stopAfter)[0].trim();
    if (first && second) {
      return `${titleCase(first)} and ${titleCase(second)}`;
    }
  }

  // Case 1: "in/at/near <locality> [stop token | end]"
  const prepMatch = qText.match(/\b(in|at|near|around|locality|area)\b\s+(.+)$/);
  if (prepMatch) {
    const after = prepMatch[2];
    const cut = after.split(stopAfter)[0].trim();
    const tokens = cut.split(" ").filter((t) => t.length > 0);
    if (tokens.length > 0) {
      return titleCase(tokens.join(" "));
    }
  }

  // Case 2: base name (+ optional directional) without a preposition.
  for (let i = 0; i < parts.length; i += 1) {
    if (baseName.test(parts[i])) {
      const captured = [parts[i]];
      if (parts[i + 1] && directional.test(parts[i + 1])) captured.push(parts[i + 1]);
      return titleCase(captured.join(" "));
    }
  }

  return null;
}

// Community abbreviations that must stay fully uppercase ("jvc" -> "JVC").
const ACRONYMS = new Set(["jbr", "jlt", "jvc", "jvt", "difc", "dip"]);

function titleCase(value: string): string {
  return value
    .split(" ")
    .filter(Boolean)
    .map((w) => (ACRONYMS.has(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

function parsedQueryTokens(query: string): string[] {
  const stopwords = new Set([
    "a",
    "an",
    "and",
    "any",
    "at",
    "around",
    "budget",
    "for",
    "in",
    "looking",
    "near",
    "of",
    "on",
    "show",
    "the",
    "to",
    "under",
    "with",
    "want",
    "wanting",
    "within",
  ]);
  return normalizeText(query)
    .split(" ")
    .filter((token) => token.length >= 3 && !stopwords.has(token));
}

// Detect whether a query has ANY real-estate signal. Returns false for greetings,
// gibberish, single words with no property meaning, etc.
function hasRealEstateIntent(query: string, parsed: ParsedNaturalSearch): boolean {
  // Explicit signals from regex
  if (parsed.bhk != null) return true;
  if (parsed.intent != null) return true;
  if (parsed.furnishing != null) return true;
  if (parsed.minPrice != null || parsed.maxPrice != null) return true;
  if (parsed.locality != null) return true;
  if (parsed.matchedLocalities.length > 0) return true;
  if (parsed.asset != null) return true;

  // Check if any tokens matched known buildings or landmarks
  const tokens = parsedQueryTokens(query);
  if (tokens.some((t) => t.length >= 4)) {
    // Has substantial tokens — could be a building name or landmark
    // but only if they look like property-related words
    const propertyWords = new Set([
      "flat", "apartment", "villa", "house", "office", "shop", "plot",
      "buy", "rent", "sale", "lease", "purchase", "tenant", "landlord",
      "furnished", "unfurnished", "parking", "terrace", "balcony",
      "society", "complex", "tower", "wing", "block",
    ]);
    if (tokens.some((t) => propertyWords.has(t))) return true;
  }

  return false;
}

export function parseSearchQuery(query: string, localities: LocalitySummary[]): ParsedNaturalSearch {
  const parsedBhk = parseBhk(query);
  const parsedIntent = parseIntent(query);
  const parsedFurnishing = parseFurnishing(query);
  const parsedAsset = parseAsset(query);
  const { minPrice, maxPrice } = parseBudget(query);
  const matchedLocalities = findLocalityMatches(query, localities);

  return {
    query,
    locality: matchedLocalities[0]?.locality ?? null,
    localityStated: detectLocalityStated(query),
    statedLocalityText: extractStatedLocalityPhrase(query),
    bhk: parsedBhk,
    intent: parsedIntent,
    asset: parsedAsset,
    minPrice,
    maxPrice,
    furnishing: parsedFurnishing,
    tokens: parsedQueryTokens(query),
    matchedLocalities,
  };
}

export async function parseNaturalSearchQuery(query: string): Promise<ParsedNaturalSearch> {
  const localities = await getAllLocalities();
  return parseSearchQuery(query, localities);
}

function rowBhkValue(bhk: string | null): number | null {
  if (!bhk) return null;
  const lower = bhk.toLowerCase();
  if (lower.includes("studio")) return 0;
  const match = lower.match(/\d+(?:\.\d+)?/);
  return match ? Number.parseFloat(match[0]) : null;
}

function rowIntentValue(intent: string | null): "rent" | "sale" | null {
  const lower = (intent || "").toLowerCase();
  if (/\b(rent|rental|lease)\b/.test(lower)) return "rent";
  if (/\b(sale|sell|buy|purchase)\b/.test(lower)) return "sale";
  return null;
}

/** Convert a row's price (stored in its unit) to absolute rupees for budget comparison. */
function rowPriceInRupees(row: NaturalSearchRow): number | null {
  if (typeof row.price !== "number") return null;
  const unit = (row.price_unit || "").toLowerCase();
  if (unit === "cr" || unit === "crore" || unit === "crores") return row.price * 1_00_00_000;
  if (unit === "lac" || unit === "lakh" || unit === "lakhs" || unit === "l") return row.price * 1_00_000;
  if (unit === "k" || unit === "thousand") return row.price * 1_000;
  // "abs" or empty → already absolute rupees
  return row.price;
}

function rowSearchText(row: NaturalSearchRow): string {
  return normalizeText(
    [
      row.building_name,
      row.location_label,
      row.landmark_name,
      row.micro_market,
      row.locality_raw,
      row.locality_resolved,
      row.broker_name,
      row.broker_phone,
      row.intent,
      row.bhk,
      row.furnishing,
    ]
      .filter(Boolean)
      .join(" "),
  );
}

function scoreRow(row: NaturalSearchRow, parsed: ParsedNaturalSearch): { score: number; matchedOn: string[] } {
  const matchedOn: string[] = [];
  let score = 0;
  const text = rowSearchText(row);

  const rowLocality = row.micro_market || row.locality_resolved || row.locality_raw;
  if (parsed.locality && rowLocality && canonicalLocality(rowLocality).slug === canonicalLocality(parsed.locality).slug) {
    score += 80;
    matchedOn.push(parsed.locality);
  }

  if (parsed.bhk != null) {
    const rowBhk = rowBhkValue(row.bhk);
    if (rowBhk === parsed.bhk) {
      score += 40;
      matchedOn.push(`${parsed.bhk} BHK`);
    } else if (rowBhk != null && Math.abs(rowBhk - parsed.bhk) < 0.5) {
      score += 20;
      matchedOn.push(`${parsed.bhk} BHK-ish`);
    }
  }

  if (parsed.intent) {
    const rowIntent = rowIntentValue(row.intent);
    if (rowIntent === parsed.intent) {
      score += 25;
      matchedOn.push(parsed.intent);
    }
  }

  if (parsed.asset) {
    const rowAsset = (row.asset_type || "").toLowerCase();
    if (rowAsset === parsed.asset) {
      score += 25;
      matchedOn.push(parsed.asset);
    }
  }

  if (parsed.furnishing) {
    const furnishingText = normalizeText(row.furnishing || "");
    const matches =
      (parsed.furnishing === "furnished" && furnishingText.includes("furnished") && !furnishingText.includes("semi")) ||
      (parsed.furnishing === "semi-furnished" && furnishingText.includes("semi")) ||
      (parsed.furnishing === "unfurnished" && furnishingText.includes("unfurnished"));
    if (matches) {
      score += 12;
      matchedOn.push(parsed.furnishing);
    }
  }

  if (parsed.minPrice != null || parsed.maxPrice != null) {
    const price = rowPriceInRupees(row);
    if (price != null) {
      const inRange =
        (parsed.minPrice == null || price >= parsed.minPrice) &&
        (parsed.maxPrice == null || price <= parsed.maxPrice);
      if (inRange) {
        score += 35;
        matchedOn.push("budget");
      }
    }
  }

  for (const token of parsed.tokens) {
    if (text.includes(token)) score += 4;
  }

  if (row.observation_count && row.observation_count > 1) {
    score += Math.min(12, row.observation_count);
  }

  if (row.last_seen) {
    const ageMs = Date.now() - new Date(row.last_seen).getTime();
    if (Number.isFinite(ageMs) && ageMs >= 0) {
      score += Math.max(0, 10 - Math.min(10, Math.floor(ageMs / 86_400_000)));
    }
  }

  return { score, matchedOn };
}

export function matchesHardFilters(row: NaturalSearchRow, parsed: ParsedNaturalSearch, allowNearbyLocality = false): boolean {
  if (parsed.locality && !allowNearbyLocality) {
    const rowLocality = row.micro_market || row.locality_resolved || row.locality_raw;
    const rowSlug = rowLocality ? canonicalLocality(rowLocality).slug : "";
    if (rowSlug) {
      const anyMatch = parsed.matchedLocalities.some((loc) => {
        const locSlug = canonicalLocality(loc.locality).slug;
        return locSlug && rowSlug === locSlug;
      });
      if (!anyMatch) {
        return false;
      }
    }
  }

  if (parsed.bhk != null) {
    const rowBhk = rowBhkValue(row.bhk);
    if (rowBhk == null || rowBhk !== parsed.bhk) return false;
  }

  if (parsed.intent) {
    const rowIntent = rowIntentValue(row.intent);
    if (rowIntent != null && rowIntent !== parsed.intent) return false;
  }

  if (parsed.asset) {
    const rowAsset = (row.asset_type || "").toLowerCase();
    if (rowAsset && rowAsset !== parsed.asset) return false;
  }

  if (parsed.furnishing) {
    const furnishingText = normalizeText(row.furnishing || "");
    const matches =
      (parsed.furnishing === "furnished" && furnishingText.includes("furnished") && !furnishingText.includes("semi")) ||
      (parsed.furnishing === "semi-furnished" && furnishingText.includes("semi")) ||
      (parsed.furnishing === "unfurnished" && furnishingText.includes("unfurnished"));
    if (!matches) return false;
  }

  if (parsed.minPrice != null || parsed.maxPrice != null) {
    const price = rowPriceInRupees(row);
    if (price == null) return false;
    if (parsed.minPrice != null && price < parsed.minPrice) return false;
    if (parsed.maxPrice != null && price > parsed.maxPrice) return false;
  }

  return true;
}

export function describeNaturalSearch(parsed: ParsedNaturalSearch): string {
  const parts: string[] = [];
  if (parsed.bhk != null) parts.push(parsed.bhk === 0 ? "Studio" : `${parsed.bhk} BHK`);
  if (parsed.locality) parts.push(parsed.locality);
  if (parsed.intent) parts.push(parsed.intent === "rent" ? "rentals" : "sale");
  if (parsed.asset) parts.push(parsed.asset);
  if (parsed.minPrice != null || parsed.maxPrice != null) {
    const min = formatPrice(parsed.minPrice);
    const max = formatPrice(parsed.maxPrice);
    parts.push(
      parsed.minPrice != null && parsed.maxPrice != null
        ? `${min} to ${max}`
        : parsed.maxPrice != null
          ? `under ${max}`
          : `from ${min}`,
    );
  }
  if (parsed.furnishing) parts.push(parsed.furnishing);
  return parts.join(" • ");
}

// Recency-ranked browse scoped to a single asset type. Used when a user lands on
// /search?asset=commercial (or selects Commercial on the homepage) with no
// free-text query, so they see relevant listings immediately instead of being
// forced into a second search step.
async function browseByAsset(
  db: NonNullable<ReturnType<typeof getServerSupabase>>,
  asset: "residential" | "commercial",
  limit: number,
  localities: LocalitySummary[],
  matchedSuggestions: LocalitySummary[],
): Promise<NaturalSearchState> {
  const fields = LISTING_FIELDS.join(", ");
  // This is a recency browse, not a relevance search. Fetch only the cards
  // we can render rather than paginating the entire asset inventory.
  const { data, error } = await db
    .from("listings_unified")
    .select(fields)
    .eq("asset_type", asset)
    .order("last_seen", { ascending: false })
    .limit(limit);
  if (error) console.error("browseByAsset error:", error.message);
  const rows = dedupeRecentListings((data ?? []) as unknown as NaturalSearchRow[]);

  const candidateBuildingNames = Array.from(new Set(
    rows
      .map((r) => r.building_name?.trim())
      .filter((n): n is string => Boolean(n) && !isJunkBuildingName(n || ""))
  ));
  const buildingNameSet = new Set<string>();
  if (candidateBuildingNames.length > 0) {
    try {
      const { data: matchedBuildings } = await db
        .from("buildings")
        .select("canonical_name")
        .in("canonical_name", candidateBuildingNames);
      for (const b of matchedBuildings ?? []) {
        if (b.canonical_name) buildingNameSet.add(slugify(b.canonical_name));
      }
    } catch (err) {
      console.error("Building classification query failed in browseByAsset:", err);
    }
  }
  const localitySlugSet = new Set(
    localities.map((l: LocalitySummary) => l.slug).filter(Boolean),
  );

  const classify = (row: NaturalSearchRow): "locality" | "building" => {
    const marketSlug = row.micro_market ? slugify(row.micro_market) : null;
    if (marketSlug && localitySlugSet.has(marketSlug)) return "locality";
    const buildingSlug = row.building_name ? slugify(row.building_name) : null;
    if (buildingSlug && buildingNameSet.has(buildingSlug)) return "building";
    return marketSlug ? "locality" : "building";
  };

  const ranked = rows
    .slice(0, limit)
    .map((row) => {
      const priceLabel = formatPrice(row.price);
      return {
        ...row,
        score: 0,
        matchedOn: ["asset"],
        priceLabel,
        resultType: classify(row),
      };
    });

  return {
    parsed: { query: "", locality: null, localityStated: false, statedLocalityText: null, bhk: null, intent: null, asset, minPrice: null, maxPrice: null, furnishing: null, tokens: [], matchedLocalities: [] },
    results: await enrichWithBuildingCoords(ranked),
    totalScanned: rows.length,
    suggestions: matchedSuggestions,
    hasData: true,
    localityUnmatched: false,
    localitySuggestions: [],
    noResultsReason: null,
  };
}

async function fetchParsedQuery(query: string, localities: LocalitySummary[]): Promise<ParsedNaturalSearch | null> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    const res = await fetch(`/api/search/parse?q=${encodeURIComponent(query)}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return null;
    const data = await res.json();

    // If confidence is 0, the LLM thinks this isn't a property query at all.
    // Return null so the caller can detect no-intent.
    if (data.confidence === 0) {
      return { query, locality: null, localityStated: false, statedLocalityText: null, bhk: null, intent: null, asset: null, minPrice: null, maxPrice: null, furnishing: null,       tokens: parsedQueryTokens(query),
      matchedLocalities: [],
      buildingName: null,
      _confidence: 0 };
    }

    const backendLocalities = data.localities || [];
    let matchedLocalities: LocalitySummary[] = [];
    if (backendLocalities.length > 0) {
      matchedLocalities = backendLocalities
        .map((loc: string) => localities.find((l: LocalitySummary) => l.slug === slugify(loc) || l.locality.toLowerCase() === loc.toLowerCase()))
        .filter((l?: LocalitySummary): l is LocalitySummary => Boolean(l));
    }
    return {
      query,
      locality: data.locality || null,
      localityStated: detectLocalityStated(query),
      statedLocalityText: extractStatedLocalityPhrase(query),
      bhk: data.bhk ?? null,
      intent: data.intent ?? null,
      asset: data.asset ?? null,
      minPrice: data.minPrice ?? null,
      maxPrice: data.maxPrice ?? null,
      furnishing: data.furnishing ?? null,
      tokens: parsedQueryTokens(query),
      matchedLocalities,
      buildingName: data.buildingName || null,
      _confidence: data.confidence ?? 1,
    };
  } catch {
    return null;
  }
}

export async function searchNaturalLanguageListings(
  query: string,
  limit = 24,
  asset: "residential" | "commercial" | null = null,
  localitiesOverride?: LocalitySummary[],
): Promise<NaturalSearchState> {
  const db = getServerSupabase();
  const localities = localitiesOverride ?? (await getAllLocalities());
  let parsed = parseSearchQuery(query, localities);
  const llmParsed = await fetchParsedQuery(query, localities);
  if (llmParsed) {
    const frontendMatches = findLocalityMatches(query, localities);
    if (frontendMatches.length > 0) {
      parsed = { ...llmParsed, matchedLocalities: frontendMatches, localityStated: detectLocalityStated(query) };
      if (!parsed.locality && frontendMatches[0]) {
        parsed.locality = frontendMatches[0].locality;
      }
    } else if (llmParsed.locality) {
      parsed = { ...llmParsed, localityStated: detectLocalityStated(query) };
    }
  }
  if (asset) parsed.asset = asset;

  const matchedSuggestions =
    parsed.matchedLocalities.length > 0 ? parsed.matchedLocalities : localities.slice(0, 6);

  // ── No-intent detection ──────────────────────────────────────────
  // If the LLM returned confidence 0 (not a property query) AND the
  // regex parser also found zero real-estate signals, AND no building
  // name was matched, surface a clarification prompt.
  const llmConfident = llmParsed?._confidence != null && llmParsed._confidence > 0;
  const regexHasIntent = hasRealEstateIntent(query, parsed);

  // Building name fallback: if neither LLM nor regex detected intent,
  // check if any query token matches a known building name in the DB.
  // Also use the LLM's building name if it provided one.
  let buildingNameMatch: string | null = parsed.buildingName || null;
  let brokerNameMatch: string | null = null;
  if (!buildingNameMatch && !llmConfident && !regexHasIntent && db && query.trim().length >= 2) {
    const tokens = parsedQueryTokens(query);
    for (const token of tokens) {
      const like = `%${token}%`;
      const { data: matches } = await db
        .from("buildings")
        .select("canonical_name")
        .ilike("canonical_name", like)
        .limit(1);
      if (matches && matches.length > 0) {
        buildingNameMatch = matches[0].canonical_name;
        break;
      }
    }
    // Also check building_name_aliases
    if (!buildingNameMatch) {
      for (const token of tokens) {
        const like = `%${token}%`;
        const { data: aliasMatches } = await db
          .from("building_name_aliases")
          .select("canonical_name")
          .ilike("alias", like)
          .limit(1);
        if (aliasMatches && aliasMatches.length > 0) {
          buildingNameMatch = aliasMatches[0].canonical_name;
          break;
        }
      }
    }
    // Broker name fallback: check if query matches a known broker
    if (!buildingNameMatch) {
      const fullQuery = query.trim();
      const { data: brokerMatches } = await db
        .from("listings_unified")
        .select("broker_name")
        .ilike("broker_name", `%${fullQuery}%`)
        .not("broker_name", "is", null)
        .neq("broker_name", "")
        .limit(1);
      if (brokerMatches && brokerMatches.length > 0 && brokerMatches[0].broker_name) {
        brokerNameMatch = brokerMatches[0].broker_name;
      }
    }
  }

  if (!llmConfident && !regexHasIntent && !buildingNameMatch && !brokerNameMatch && query.trim().length > 0) {
    return {
      parsed,
      results: [],
      totalScanned: 0,
      suggestions: matchedSuggestions,
      hasData: Boolean(db),
      localityUnmatched: false,
      localitySuggestions: [],
      noResultsReason: "no_intent",
    };
  }

  // AI locality extraction: when regex didn't find a locality but the user
  // mentioned one, try LLM extraction as a smarter fallback. This handles
  // typos ("bhi", "bandar"), abbreviations, and compound queries.
  if (parsed.localityStated && !parsed.locality && query.trim().length >= 3) {
    try {
      const aiLocality = await extractLocalityWithAI(query, localities);
      if (aiLocality) {
        parsed.locality = aiLocality;
        // Update matchedLocalities so the UI shows the correct locality link.
        const match = localities.find((l) => l.locality === aiLocality);
        if (match) parsed.matchedLocalities = [match];
      }
    } catch {
      // AI extraction is best-effort; fall through to regex results.
    }
  }

  // A transient locality-cache/RPC miss must not turn a valid, explicitly
  // stated locality into an immediate zero-result response. Keep the exact
  // phrase the user supplied as the search locality so the candidate query
  // can still use the indexed listing text; only the result rows determine
  // whether inventory actually exists there.
  if (parsed.localityStated && !parsed.locality && parsed.statedLocalityText) {
    parsed.locality = parsed.statedLocalityText;
  }

  // The user named a locality that we could not resolve to any tracked
  // gazetteer entry. Do NOT silently fall back to a broad, locality-less
  // search — that erodes trust by mixing unrelated localities. Surface a
  // "no matches for that locality" state with honest suggestions instead.
  if (parsed.localityStated && !parsed.locality) {
    return {
      parsed,
      results: [],
      totalScanned: 0,
      suggestions: matchedSuggestions,
      hasData: Boolean(db),
      localityUnmatched: true,
      localitySuggestions: localities.slice(0, 6),
      noResultsReason: "locality_unmatched",
    };
  }

  if (!db) {
    return {
      parsed,
      results: [],
      totalScanned: 0,
      suggestions: matchedSuggestions,
      hasData: false,
      localityUnmatched: false,
      localitySuggestions: [],
      noResultsReason: null,
    };
  }

  // Browsing by asset type with no free-text query (e.g. /search?asset=commercial)
  // should show that asset's live listings directly — not an empty state that
  // forces a second search. Treat it as a recency-ranked browse, scoped to the
  // selected asset type.
  if (!query.trim() && parsed.asset) {
    return await browseByAsset(db, parsed.asset, limit, localities, matchedSuggestions);
  }

  if (!query.trim()) {
    return {
      parsed,
      results: [],
      totalScanned: 0,
      suggestions: matchedSuggestions,
      hasData: Boolean(db),
      localityUnmatched: false,
      localitySuggestions: [],
      noResultsReason: null,
    };
  }

  const fields = LISTING_FIELDS.join(", ");

  const thirtyDaysAgo = new Date(Date.now() - 30 * 86_400_000).toISOString();

  const fetchCandidateRows = async (): Promise<NaturalSearchRow[]> => {
    // Priority 1: Building name match from DB lookup
    if (buildingNameMatch) {
      let qb = db.from("listings_unified").select(fields).gte("last_seen", thirtyDaysAgo).order("last_seen", { ascending: false });
      qb = qb.ilike("building_name", buildingNameMatch);
      if (parsed.asset) qb = qb.eq("asset_type", parsed.asset);
      const { data, error } = await qb.limit(SEARCH_CANDIDATE_LIMIT);
      if (error) {
        console.error("searchNaturalLanguageListings building name candidate error:", error.message);
        return [];
      }
      // Also get the building's locality for the parsed object
      if (!parsed.locality) {
        const { data: bData } = await db
          .from("buildings")
          .select("micro_market")
          .ilike("canonical_name", buildingNameMatch)
          .limit(1);
        if (bData && bData[0]?.micro_market) {
          parsed.locality = bData[0].micro_market;
          parsed.matchedLocalities = localities.filter(
            (l) => l.locality.toLowerCase() === bData[0].micro_market.toLowerCase()
          ).slice(0, 1);
        }
      }
      return (data ?? []) as unknown as NaturalSearchRow[];
    }

    // Priority 2: Broker name match
    if (brokerNameMatch) {
      let qb = db.from("listings_unified").select(fields).gte("last_seen", thirtyDaysAgo).order("last_seen", { ascending: false });
      qb = qb.ilike("broker_name", brokerNameMatch);
      if (parsed.asset) qb = qb.eq("asset_type", parsed.asset);
      const { data, error } = await qb.limit(SEARCH_CANDIDATE_LIMIT);
      if (error) {
        console.error("searchNaturalLanguageListings broker name candidate error:", error.message);
        return [];
      }
      return (data ?? []) as unknown as NaturalSearchRow[];
    }

    const localitySlugs = parsed.matchedLocalities.map((l) => canonicalLocality(l.locality).slug).filter(Boolean);
    if (localitySlugs.length > 0) {
      let qb = db.from("listings_unified").select(fields).gte("last_seen", thirtyDaysAgo).order("last_seen", { ascending: false });
      qb = qb.in("canonical_micro_market_slug", localitySlugs);
      if (parsed.asset) qb = qb.eq("asset_type", parsed.asset);
      const [canonicalResult, textResults] = await Promise.all([
        qb.limit(SEARCH_CANDIDATE_LIMIT * localitySlugs.length),
        Promise.all(parsed.matchedLocalities.map(async (locality) => {
          const like = `%${locality.locality.replace(/[%,()]/g, "")}%`;
          let textQuery = db.from("listings_unified").select(fields)
            .or(`micro_market.ilike.${like},locality_raw.ilike.${like},locality_resolved.ilike.${like},building_name.ilike.${like},landmark_name.ilike.${like}`)
            .gte("last_seen", thirtyDaysAgo).order("last_seen", { ascending: false });
          if (parsed.asset) textQuery = textQuery.eq("asset_type", parsed.asset);
          return textQuery.limit(SEARCH_CANDIDATE_LIMIT);
        })),
      ]);
      const { data, error } = canonicalResult;
      if (error) {
        console.error("searchNaturalLanguageListings locality candidate error:", error.message);
      }
      const deduped = new Map<number, NaturalSearchRow>();
      for (const row of (data ?? []) as unknown as NaturalSearchRow[]) deduped.set(row.id, row);
      for (const result of textResults) {
        for (const row of (result.data ?? []) as unknown as NaturalSearchRow[]) deduped.set(row.id, row);
      }
      return Array.from(deduped.values());
    }

    const tokens = candidateTokens(query);
    if (tokens.length === 0) return [];
    const batches = await Promise.all(tokens.map(async (token) => {
      const like = postgrestLikeToken(token);
      let qb = db.from("listings_unified")
        .select(fields)
        .or(`building_name.ilike.${like},micro_market.ilike.${like},locality_raw.ilike.${like},locality_resolved.ilike.${like},location_label.ilike.${like},landmark_name.ilike.${like}`)
        .gte("last_seen", thirtyDaysAgo)
        .order("last_seen", { ascending: false });
      if (parsed.asset) qb = qb.eq("asset_type", parsed.asset);
      const { data, error } = await qb.limit(SEARCH_CANDIDATE_LIMIT);
      if (error) {
        console.error("searchNaturalLanguageListings text candidate error:", error.message);
        return [] as NaturalSearchRow[];
      }
      return (data ?? []) as unknown as NaturalSearchRow[];
    }));
    const deduped = new Map<number, NaturalSearchRow>();
    for (const batch of batches) for (const row of batch) deduped.set(row.id, row);
    return Array.from(deduped.values());
  };

  let rows = dedupeRecentListings(await fetchCandidateRows());

  // Match building names for candidate rows against known buildings
  const candidateBuildingNames = Array.from(new Set(
    rows
      .map((r) => r.building_name?.trim())
      .filter((n): n is string => Boolean(n) && !isJunkBuildingName(n || ""))
  ));
  const buildingNameSet = new Set<string>();
  if (candidateBuildingNames.length > 0 && db) {
    try {
      const { data: matchedBuildings } = await db
        .from("buildings")
        .select("canonical_name")
        .in("canonical_name", candidateBuildingNames);
      for (const b of matchedBuildings ?? []) {
        if (b.canonical_name) buildingNameSet.add(slugify(b.canonical_name));
      }
    } catch (err) {
      console.error("Building classification query failed in search:", err);
    }
  }
  const localitySlugSet = new Set(
    localities.map((l: LocalitySummary) => l.slug).filter(Boolean),
  );

  const classify = (row: NaturalSearchRow): "locality" | "building" => {
    const marketSlug = row.micro_market ? slugify(row.micro_market) : null;
    if (marketSlug && localitySlugSet.has(marketSlug)) return "locality";
    const buildingSlug = row.building_name ? slugify(row.building_name) : null;
    if (buildingSlug && buildingNameSet.has(buildingSlug)) return "building";
    // Default unknown market values to building only when they look like a
    // building; otherwise keep "locality" so the chip is at least sensible.
    return marketSlug ? "locality" : "building";
  };

  const rankRows = (candidateRows: NaturalSearchRow[], allowNearbyLocality = false) => candidateRows
    .filter((row) => matchesHardFilters(row, parsed, allowNearbyLocality))
    .map((row) => {
      const { score, matchedOn } = scoreRow(row, parsed);
      const priceLabel = formatPrice(row.price);
      return {
        ...row,
        score,
        matchedOn,
        priceLabel,
        resultType: classify(row),
      };
    })
    .sort((a, b) => b.score - a.score || (b.last_seen ? new Date(b.last_seen).getTime() : 0) - (a.last_seen ? new Date(a.last_seen).getTime() : 0))
    .slice(0, limit);

  let ranked = rankRows(rows);
  if (ranked.length === 0 && parsed.locality) {
    const nearbySlugs = getNearbyLocalityNames(parsed.locality)
      .map((locality) => canonicalLocality(locality).slug)
      .filter((slug): slug is string => Boolean(slug));
    if (nearbySlugs.length > 0) {
      let nearbyQuery = db
        .from("listings_unified")
        .select(fields)
        .in("canonical_micro_market_slug", nearbySlugs)
        .gte("last_seen", thirtyDaysAgo)
        .order("last_seen", { ascending: false });
      if (parsed.asset) nearbyQuery = nearbyQuery.eq("asset_type", parsed.asset);
      const { data: nearbyData, error: nearbyError } = await nearbyQuery.limit(SEARCH_CANDIDATE_LIMIT);
      if (nearbyError) {
        console.error("searchNaturalLanguageListings nearby locality fallback error:", nearbyError.message);
      } else if (nearbyData?.length) {
        const nearbyRows = dedupeRecentListings(nearbyData as unknown as NaturalSearchRow[]);
        ranked = rankRows(nearbyRows, true);
        if (ranked.length > 0) rows = nearbyRows;
      }
    }
  }

  return {
    parsed,
    results: await enrichWithBuildingCoords(ranked),
    totalScanned: rows.length,
    suggestions: matchedSuggestions,
    hasData: true,
    localityUnmatched: false,
    localitySuggestions: [],
    noResultsReason: ranked.length === 0 ? "no_matches" : null,
  };
}

// Dedicated map browse: recent live inventory with building coordinates. This
// intentionally does not invoke the AI parser; the /map page is a browse
// surface, while /search remains the natural-language discovery surface.
export async function getPublicMapListings(limit = 60): Promise<NaturalSearchResult[]> {
  const db = getServerSupabase();
  if (!db) return [];

  const thirtyDaysAgo = new Date(Date.now() - 30 * 86_400_000).toISOString();
  const { data, error } = await db
    .from("listings_unified")
    .select(LISTING_FIELDS.join(", "))
    .not("building_name", "is", null)
    .gte("last_seen", thirtyDaysAgo)
    // Do not ask Postgres to sort the entire 278k-row union view before
    // returning a small page. The server-side client has a 45s timeout and
    // the sort can exhaust it, which used to render as a false empty map.
    .limit(Math.max(limit * 4, 240));

  if (error) {
    console.error("getPublicMapListings error:", error.message);
    return [];
  }

  const rows = (data ?? []) as unknown as NaturalSearchRow[];
  const results: NaturalSearchResult[] = rows
    .sort(
      (a, b) =>
        (b.last_seen ? new Date(b.last_seen).getTime() : 0) -
        (a.last_seen ? new Date(a.last_seen).getTime() : 0),
    )
    .slice(0, limit)
    .map((row) => ({
      ...row,
      score: 0,
      matchedOn: ["live inventory"],
      priceLabel: formatPrice(row.price),
      resultType: "locality",
    }));

  return enrichWithBuildingCoords(results);
}

const _buildingCoordsCache = new Map<string, { latitude: number; longitude: number }>();

async function enrichWithBuildingCoords(
  results: NaturalSearchResult[],
): Promise<NaturalSearchResult[]> {
  const names = [...new Set(results.map((r) => r.building_name).filter(Boolean) as string[])];
  if (names.length === 0) return results;

  const missing = names.filter((n) => !_buildingCoordsCache.has(n.toLowerCase()));
  if (missing.length > 0) {
    const db = getServerSupabase();
    if (db) {
      const PAGE = 500;
      try {
        for (let i = 0; i < missing.length; i += PAGE) {
          const batch = missing.slice(i, i + PAGE);
          const { data } = await db
            .from("buildings")
            .select("canonical_name, latitude, longitude")
            .in("canonical_name", batch)
            .not("latitude", "is", null);
          for (const row of data ?? []) {
            const name = (row.canonical_name ?? "").trim().toLowerCase();
            if (name && row.latitude != null && row.longitude != null) {
              _buildingCoordsCache.set(name, {
                latitude: row.latitude,
                longitude: row.longitude,
              });
            }
          }
        }
      } catch (err) {
        console.error("enrichWithBuildingCoords query failed:", err);
      }
    }
  }

  return results.map((r) => {
    if (!r.building_name) return r;
    const coords = _buildingCoordsCache.get(r.building_name.toLowerCase());
    if (coords) {
      return { ...r, latitude: coords.latitude, longitude: coords.longitude };
    }
    return r;
  });
}
