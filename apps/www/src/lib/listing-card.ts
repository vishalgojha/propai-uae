import { slugify } from "./supabase";
import { extractLocalityFromText } from "./locality-canon";

export type ListingCardFields = {
  id: number;
  bhk: string | number | null;
  price: number | null;
  price_unit: string | null;
  price_raw_text?: string | null;
  price_model?: string | null;
  price_per_sqft?: number | null;
  area_sqft: number | null;
  furnishing: string | null;
  intent: string | null;
  asset_type: string | null;
  property_type: string | null;
  micro_market: string | null;
  locality_raw?: string | null;
  locality_resolved?: string | null;
  building_name: string | null;
  landmark_name?: string | null;
  location_label?: string | null;
  floor_description?: string | null;
  view?: string | null;
  title?: string | null;
  representative_raw_message_id?: number | null;
  latest_raw_message_id?: number | null;
  broker_name: string | null;
  broker_phone: string | null;
  broker_id?: number | null;
  last_seen: string | null;
  deal_tags?: string[] | null;
  additional_charges?: AdditionalCharge[] | null;
  /** Internal explanation used by contextual recommendation surfaces. */
  recommendation_reason?: string | null;
};

type DedupableListing = Pick<
  ListingCardFields,
  | "id"
  | "price"
  | "price_unit"
  | "property_type"
  | "building_name"
  | "micro_market"
  | "locality_raw"
  | "locality_resolved"
  | "bhk"
  | "intent"
  | "area_sqft"
  | "floor_description"
  | "landmark_name"
  | "broker_name"
  | "broker_phone"
  | "last_seen"
>;

function dedupPart(value: unknown): string {
  return String(value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "").trim();
}

// A repeated WhatsApp observation is not new inventory. Keep the newest row
// for the same broker/building/unit/configuration/intent within the 24-hour
// ingestion window, while retaining different brokers and different units.
// This is deliberately query-time safe: source rows remain available for audit.
export function dedupeRecentListings<T extends DedupableListing>(rows: T[]): T[] {
  const ordered = [...rows].sort((a, b) => {
    const at = a.last_seen ? new Date(a.last_seen).getTime() : 0;
    const bt = b.last_seen ? new Date(b.last_seen).getTime() : 0;
    return bt - at || Number(b.id) - Number(a.id);
  });
  const kept: T[] = [];
  const seen = new Map<string, number>();

  for (const row of ordered) {
    const broker = dedupPart(row.broker_phone) || dedupPart(row.broker_name) || "unknown-broker";
    const place = dedupPart(row.building_name) || dedupPart(row.landmark_name) || dedupPart(row.micro_market) || "unknown-place";
    const key = [
      broker,
      place,
      dedupPart(row.micro_market || row.locality_resolved || row.locality_raw),
      dedupPart(row.bhk),
      dedupPart(row.intent),
      row.area_sqft == null ? "" : String(Math.round(Number(row.area_sqft))),
      dedupPart(row.floor_description),
    ].join("|");
    const time = row.last_seen ? new Date(row.last_seen).getTime() : 0;
    const previous = seen.get(key);
    if (previous != null && time > 0 && previous - time <= 24 * 60 * 60 * 1000) continue;
    seen.set(key, time);
    kept.push(row);
  }
  return kept;
}

export function formatBhkNumber(value: string | number | null | undefined): string {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  const match = raw.match(/^(\d+(?:\.\d+)?)(?:\s*BHK)?$/i);
  if (!match) return raw;
  const numeric = Number(match[1]);
  return Number.isInteger(numeric)
    ? String(numeric)
    : match[1].replace(/0+$/, "").replace(/\.$/, "");
}

/** Prefer an explicit BHK marker from the source message over a stale typed value. */
export function normalizeBhkFromEvidence(
  value: string | number | null | undefined,
  evidence: string | null | undefined,
): string | null {
  const match = String(evidence ?? "").match(/\b(\d+(?:\.\d+)?)\s*(?:bhk|bhd|rk|bed\s*rooms?|bedrooms?|br)\b/i);
  if (!match) return value == null ? null : String(value);
  const numeric = Number(match[1]);
  return Number.isFinite(numeric) ? String(numeric) : value == null ? null : String(value);
}

export function formatBhkLabel(value: string | number | null | undefined): string {
  const number = formatBhkNumber(value);
  return number ? `${number} BHK` : "";
}

export type AdditionalCharge = {
  label: string;
  amount: number | null;
  amount_type: "fixed" | "percent_of_price";
};

// Structured spec entries so callers can render an icon per spec (bed count,
// area, furnishing, floor...) instead of one flat "3 BHK · 850 sqft" string.
export type ListingSpecItem = {
  kind: "bhk" | "area" | "furnishing" | "floor" | "view" | "type";
  label: string;
};

export type ListingCardViewModel = {
  title: string;
  locality: string | null;
  localitySlug: string | null;
  isBuilding: boolean;
  priceLabel: string;
  specRow: string;
  specItems: ListingSpecItem[];
  statusLabel: string;
  statusTone: "listed" | "unconfirmed";
  updatedLabel: string;
  freshnessLabel: string;
  freshnessBadge: string | null;
  assetTypeLabel: string | null;
  waLink: string | null;
  href: string | null;
  slug: string | null;
  waAvailable: boolean;
  brokerName: string | null;
  priceModel: string | null;
  pricePerSqft: number | null;
  dealTags: Array<{ tag: string; label: string; tone: string }>;
  additionalCharges: Array<{ label: string; amountLabel: string }>;
};

function normalizeUnit(value: string | null): string | null {
  if (!value) return null;
  const u = value.trim().toLowerCase();
  // Legacy Indian units still map so old rows/queries keep rendering.
  if (u === "cr" || u === "crore" || u === "crores") return "cr";
  if (u === "lac" || u === "lakh" || u === "lakhs") return "lac";
  if (u === "k" || u === "thousand") return "k";
  if (u === "m" || u === "mn" || u === "million" || u === "millions") return "m";
  if (u === "abs") return "abs";
  return null;
}

function intentValue(intent: string | null): "rent" | "sale" | "commercial" | null {
  const i = (intent || "").toLowerCase();
  if (i === "rent" || i === "rental" || i === "lease") return "rent";
  if (i === "sell" || i === "sale" || i === "resale" || i === "buy" || i === "purchase") return "sale";
  if (i === "commercial") return "commercial";
  return null;
}

// Human-readable residential/commercial label for a listing card. Prefers the
// parsed asset_type (residential | commercial); falls back to the intent when
// asset_type is missing so historical rows still get a sensible badge.
export function assetTypeLabel(
  assetType: string | null,
  intent: string | null,
): string | null {
  const a = (assetType || "").trim().toLowerCase();
  if (a === "commercial") return "Commercial";
  if (a === "residential") return "Residential";
  const i = intentValue(intent);
  if (i === "commercial") return "Commercial";
  if (i === "rent" || i === "sale") return "Residential";
  return null;
}

// Renders an explicit, buyer-readable price with a unit. Never a bare number.
//
// Ingestion stores prices inconsistently by unit:
//   - "abs": absolute AED (the normal case for fresh UAE rows).
//   - "k" / "m": the number is already in thousands/millions.
//   - "cr" / "lac": legacy Indian-unit rows kept as a safety net — small
//     numbers are native-scale ("2.5 cr"), large ones are absolute values
//     mis-tagged with the unit.
//   - "psf": price is per-sqft; total = price_per_sqft * area_sqft (unit = abs).
// We normalise each to a readable, grouped amount in AED.
export function formatCardPrice(
  price: number | null,
  priceUnit: string | null,
  intent: string | null,
  priceModel: string | null = null,
  pricePerSqft: number | null = null,
  areaSqft: number | null = null,
  priceRawText: string | null = null,
): string {
  const unit = normalizeUnit(priceUnit);
  const intentKind = intentValue(intent);
  const perMonth = intentKind === "rent";
  const grouped = (n: number) => Math.round(n).toLocaleString("en-AE");
  const formatScaled = (amount: number, suffix: string) => {
    if (amount >= 1_000_000) {
      const m = amount / 1_000_000;
      return `AED ${m % 1 === 0 ? m : m.toFixed(2)}M${suffix}`;
    }
    if (amount >= 10_000) {
      return `AED ${Math.round(amount / 1_000)}k${suffix}`;
    }
    return `AED ${grouped(amount)}${suffix}`;
  };

  // If price model is per-sqft and we have area, compute total price
  if (priceModel === "psf" && pricePerSqft != null && areaSqft != null && areaSqft > 0) {
    return formatScaled(pricePerSqft * areaSqft, "");
  }

  if (price == null) return "Price on request";

  if (perMonth) {
    // Rentals follow the same storage convention as before: the absolute
    // number is the monthly figure in AED. "k"/"thousand" = thousands of
    // AED/month, "m"/"million" = millions of AED/month.
    const rawRent = priceRawText?.match(
      /(?:rent|lease|monthly|yearly|price)\s*[:=-]?[^\d]{0,20}(?:aed|dhs)?\s*([\d,]+(?:\.\d+)?)\s*(million|m|k|thousand)?/i,
    ) ?? priceRawText?.match(
      /(?:aed|dhs)\s*([\d,]+(?:\.\d+)?)\s*(million|m|k|thousand)?/i,
    );
    if (rawRent) {
      const rawAmount = Number(rawRent[1].replace(/,/g, ""));
      const rawUnit = (rawRent[2] || "").toLowerCase();
      const rawMultiplier = rawUnit.startsWith("m") ? 1_000_000 : 1_000;
      if (Number.isFinite(rawAmount) && rawAmount > 0) return formatScaled(rawAmount * rawMultiplier, "/month");
    }

    let abs = price;
    if (unit === "k") abs = price * 1_000;
    else if (unit === "m") abs = price * 1_000_000;
    else if (unit === "abs" && abs > 0 && abs < 1_000) return "Price on request";
    // Guard against implausible monthly rents (e.g. mis-stored "abs" values
    // like 12 or 185 dirhams). Anything under AED 1,000/month is not a real
    // Dubai rent — fall back rather than show a clearly-wrong number.
    if (abs < 1000) return "Price on request";
    return formatScaled(abs, "/month");
  }

  // Sale / commercial
  // Legacy Indian-unit rows: native-scale small numbers ("2.5 cr") vs
  // absolute values mis-tagged with the unit ("85000000 cr"). Fresh UAE
  // ingestion emits abs/k/m, so this is just a safety net.
  if (unit === "cr") {
    return formatScaled(price > 1000 ? price : price * 10_000_000, "");
  }
  if (unit === "lac") {
    return formatScaled(price > 1000 ? price : price * 100_000, "");
  }
  if (unit === "k") {
    const abs = price > 1000 ? price : price * 1_000;
    return `AED ${grouped(abs)}`;
  }
  if (unit === "m") {
    return `AED ${price % 1 === 0 ? price : price.toFixed(1).replace(/\.0$/, "")}M`;
  }
  // "abs" or unknown — render the grouped whole amount, but scale obvious
  // outliers into M/k so we do not surface raw comma-dumped parser junk.
  return formatScaled(price, "");
}

function titleCase(value: string): string {
  return value
    .replace(/\bfullyfurnished\b/gi, "fully furnished")
    .replace(/\bsemifurnished\b/gi, "semi furnished")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

// WhatsApp markdown is useful in the private source message, but its control
// characters must never leak into public titles, labels, or broker names.
export function cleanPublicText(value: string | null | undefined): string | null {
  if (value == null) return null;
  const cleaned = String(value)
    .replace(/[*_`~]/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  return cleaned || null;
}

function normalizePropertyType(value: string | null): string | null {
  const raw = (value || "").trim();
  if (!raw) return null;
  const lower = raw.toLowerCase().replace(/[_-]+/g, " ");
  if (
    lower === "other" ||
    lower === "unknown" ||
    lower === "misc" ||
    lower === "miscellaneous" ||
    lower === "na" ||
    lower === "n a" ||
    lower === "n/a" ||
    lower === "none" ||
    lower === "unspecified" ||
    lower === "not specified"
  ) {
    return null;
  }
  return titleCase(raw);
}

function buildTitle(row: ListingCardFields): string {
  // A raw WhatsApp title is evidence, not display copy.  It is often merely
  // a building name (or a noisy poster headline), which made equivalent cards
  // read as "Ten BKC", "3 BHK — BKC", and "Available Ten bkc 3bhk".  Build
  // one deterministic title from the structured fields for every card.
  const furnishingValue = cleanPublicText(row.furnishing);
  const furnishing = furnishingValue && !/^(none|null|unknown)$/i.test(furnishingValue) ? furnishingValue : "";
  const bhk = formatBhkNumber(row.bhk);
  const propertyType = normalizePropertyType(row.property_type);
  // Extract first segment before comma — real building names are short and
  // appear at the start (e.g. "Wallfort Tower" from "Wallfort Tower, 2bhk...").
  const rawBuilding = cleanPublicText(row.building_name) ?? "";
  const building = rawBuilding
    ? (rawBuilding.includes(",") ? rawBuilding.split(",")[0].trim() : rawBuilding)
    : null;
  const locality = listingLocality(row);
  const intent = intentValue(row.intent);
  const transaction = intent === "rent" ? "for Rent" : intent === "sale" ? "for Sale" : "";

  const descriptor = [
    furnishing ? titleCase(furnishing) : "",
    bhk ? `${bhk} BHK` : propertyType || (assetTypeLabel(row.asset_type, row.intent) === "Commercial" ? "Commercial Space" : "Property"),
  ].filter(Boolean).join(" ");
  const place = building || locality || cleanPublicText(row.landmark_name);

  if (place && transaction) return `${descriptor} ${transaction} at ${place}`;
  if (place) return `${descriptor} at ${place}`;
  if (transaction) return `${descriptor} ${transaction}`;
  return descriptor;
}

function listingLocality(row: ListingCardFields): string | null {
  return cleanPublicText(row.micro_market)
    || cleanPublicText(row.location_label)
    || cleanPublicText(row.locality_raw)
    || cleanPublicText(row.locality_resolved)
    || cleanPublicText(extractLocalityFromText(row.building_name))
    || null;
}

function buildSpecItems(row: ListingCardFields): ListingSpecItem[] {
  const items: ListingSpecItem[] = [];
  const ptype = normalizePropertyType(row.property_type);
  if (ptype) {
    items.push({ kind: "type", label: ptype });
  }
  if (row.bhk != null && String(row.bhk).trim()) {
    items.push({ kind: "bhk", label: formatBhkLabel(row.bhk) });
  }
  if (typeof row.area_sqft === "number" && row.area_sqft > 0) {
    items.push({ kind: "area", label: `${row.area_sqft.toLocaleString("en-IN")} sqft` });
  }
  const furnishing = cleanPublicText(row.furnishing);
  if (furnishing && !/^(none|null|unknown)$/i.test(furnishing)) {
    items.push({ kind: "furnishing", label: titleCase(furnishing) });
  }
  const floor = cleanPublicText(row.floor_description);
  if (floor) {
    items.push({ kind: "floor", label: floor });
  }
  const view = cleanPublicText(row.view);
  if (view) {
    items.push({ kind: "view", label: view });
  }
  return items;
}

function buildSpecRow(items: ListingSpecItem[]): string {
  return items.map((i) => i.label).join(" · ");
}

function formatUpdated(iso: string | null): string {
  if (!iso) return "Recently";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "Recently";
  return date.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

// Relative freshness for the card ("Today 2:30 PM", "Yesterday", "3d ago",
// or an absolute date for older listings). Backs the "freshness" claim with a
// real timestamp instead of a vague label.
function formatFreshness(iso: string | null): string {
  if (!iso) return "Recently";
  const date = new Date(iso);
  const ms = date.getTime();
  if (!Number.isFinite(ms)) return "Recently";
  const now = Date.now();
  const diffMs = now - ms;
  const dayMs = 24 * 60 * 60 * 1000;
  if (diffMs < 0) return "Just now";
  if (diffMs < dayMs) {
    const time = date.toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", hour12: true });
    return diffMs < 60 * 60 * 1000 ? "Just now" : `Today ${time}`;
  }
  if (diffMs < 2 * dayMs) return "Yesterday";
  if (diffMs < 7 * dayMs) return `${Math.floor(diffMs / dayMs)}d ago`;
  return date.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

// Short, SEO-friendly freshness badge for cards: emphasizes that PropAI's
// inventory changes continuously ("Just Landed", "Active today").
function formatFreshnessBadge(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  const ms = date.getTime();
  if (!Number.isFinite(ms)) return null;
  const now = Date.now();
  const diffMs = now - ms;
  if (diffMs < 0) return "Just Landed";
  if (diffMs < 60 * 60 * 1000) return "Just Landed";
  if (diffMs < 24 * 60 * 60 * 1000) return "Active today";
  return null;
}

// Broker contact must NEVER embed the phone number in public HTML (DPDP Act
// 2023 — phone is sensitive personal data). Instead we link to a server route
// that resolves the phone server-side and 302-redirects to wa.me, so the raw
// digits are never crawlable / exposed in the public DOM.
export function waLinkFor(listingId: number | null): string | null {
  if (listingId == null) return null;
  return `/api/contact-broker/${listingId}`;
}

// Strips decorative emoji / pictographs from display strings (broker names
// pulled from WhatsApp display names often contain ✨ ⚔️ 🕉️ etc.). Display-only
// cleanup — stored data is untouched.
const EMOJI_RE =
  /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{1F1E6}-\u{1F1FF}\u{1F3FB}-\u{1F3FF}\u200d]/gu;
export function stripEmoji(value: string | null): string | null {
  if (!value) return value;
  return cleanPublicText(value.replace(EMOJI_RE, ""));
}

// Broker names are sometimes stored as raw phone numbers (e.g. "+91 9920993025"
// or "9930079206"). Never surface those in the public card DOM — mask them so
// the number is not crawlable / exposed (DPDP Act 2023). The real contact path
// is the /api/contact-broker/{id} redirect, which the server controls.
const PHONEISH = /[0-9]/;
export function safeBrokerName(raw: string | null): string | null {
  if (!raw || !raw.trim()) return null;
  const cleaned = stripEmoji(raw);
  if (!cleaned) return null;
  const v = cleaned.trim();
  // If it's mostly digits / a phone-shaped string, don't show it.
  const digitRatio = (v.match(/[0-9]/g) || []).length / Math.max(v.replace(/\s/g, "").length, 1);
  if (digitRatio > 0.5 || /^\+?\d[\d\s().-]{6,}$/.test(v)) return null;
  if (/wa\.me|whatsapp/i.test(v)) return null;
  // Reject garbage text that the LLM sometimes extracts as broker names.
  const low = v.toLowerCase();
  const GARBAGE = (
    "stamp duty|furnished|carpet|bhk|sqft|sq ft|ready to move|negotiable|"
    + "balcon|sea view|amenities|parking|deposit|possession|available|"
    + "options|benefit|family|bachelor|veg|non-veg|near|opp|opposite|"
    + "behind|floor|tower|residency|heights|apartment|regards|thank|"
    + "hello|dear|rent|sale|commercial|office|shop|lift|backup|"
    + "security|power|gym|swimming|landmark|station|price|asking|"
    + "location|coverage|capacity|reception|entrance|ground|first|"
    + "second|third|fourth|fifth|upper|lower|basement|dedicated|"
    + "visitor|ample|separate|exclusive|ready|restaurant|central|"
    + "suburb|mumbai"
  );
  if (new RegExp(GARBAGE).test(low)) return null;
  // Too short or too long to be a real name.
  if (v.length < 2 || v.length > 50) return null;
  // All-caps single word is usually not a name (e.g. "FURNISHED").
  if (v === v.toUpperCase() && !v.includes(" ")) return null;
  return v;
}

// True when the stored broker_phone can be coerced to a 10-digit Indian mobile
// (with or without the +91 prefix). Mirrors the server-side check in the
// /api/contact-broker/[id] route so the public card never shows a "Contact on
// WhatsApp" button that would just 302 back to the listing page.
export function isBrokerContactable(raw: string | null | undefined): boolean {
  if (!raw) return false;
  const digits = String(raw).replace(/\D/g, "");
  // UAE numbers arrive as 971 + 9-digit subscriber (12 digits). Legacy India
  // rows are 10-digit local or 91 + local (12 digits).
  if (digits.length >= 11 && digits.length <= 15) return true;
  return digits.length === 10;
}

// SEO-friendly slug for the public /listings/[slug]/[id] route. Format:
//   `{bhk}-{building}-{locality}-{id}`
// The id is always appended so the URL is unique even when the prefix is empty
// or repeats. Examples:
//   bhk="3 BHK", building="Lodha Bellissimo", micro_market="Andheri West"
//     → "3-bhk-lodha-bellissimo-andheri-west-319236"
//   bhk=null, micro_market=null, id=319236              → "319236"
//   bhk="2.5 BHK", micro_market="Powai", id=999         → "2-5-bhk-powai-999"
// Use this from listing-card.ts, sitemap.ts, and contact-broker route so all
// three surfaces point at the same canonical URL.
export type SlugInput = {
  id: number;
  bhk?: string | number | null;
  micro_market?: string | null;
  building_name?: string | null;
  property_type?: string | null;
};

export function buildListingSlug(input: SlugInput): string | null {
  if (!Number.isFinite(input.id)) return null;
  const id = String(input.id);
  const parts: string[] = [];
  const bhk = String(input.bhk ?? "").trim();
  if (bhk) {
    const bhkNumber = bhk.match(/^(\d+(?:\.\d+)?)/)?.[1];
    const normalizedBhk = bhkNumber ? formatBhkNumber(bhkNumber) : "";
    parts.push(normalizedBhk ? `${slugify(normalizedBhk)}-bhk` : slugify(bhk));
  }
  // Include the building before locality so a listing URL identifies the
  // actual property, not just its neighbourhood.
  const raw = (input.building_name ?? "").trim();
  const bldg = raw.includes(",") ? raw.split(",")[0].trim() : raw;
  if (bldg && bldg.length <= 50 && !/^(sq\.?\s*ft|multiple options|carpet|na\b|\d+\s*bhk|for sale|for rent|available|new listing|video|pics|car park|residential listing)/i.test(bldg)) {
    parts.push(slugify(bldg));
  }
  const micro = (input.micro_market ?? "").trim();
  if (micro) parts.push(slugify(micro));
  // If both bhk and locality/building are missing, the slug is just the id.
  // Otherwise join with hyphens, then suffix the id for uniqueness.
  if (parts.length === 0) return id;
  // Filter out empty parts (e.g. if bhk was just whitespace), then join.
  const filtered = parts.filter((p) => p.length > 0);
  if (filtered.length === 0) return id;
  return `${filtered.join("-")}-${id}`;
}

// Deal-tag taxonomy — mirrors the whitelist enforced server-side in
// ai_extraction._VALID_DEAL_TAGS. Tone buckets are public-site Tailwind class
// fragments (border + bg + text). Kept in this file so the card + detail
// page stay in sync without prop drilling.
const DEAL_TAG_LABELS: Record<string, string> = {
  distress_sale: "Distress sale",
  urgent_sale: "Urgent sale",
  negotiable: "Negotiable",
  bank_auction: "Bank auction",
  resale: "Resale",
  exclusive_mandate: "Exclusive mandate",
  price_drop: "Price drop",
};

const DEAL_TAG_TONES: Record<string, string> = {
  distress_sale: "border-red-400/30 bg-red-400/10 text-red-300",
  urgent_sale: "border-orange-400/30 bg-orange-400/10 text-orange-300",
  negotiable: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  bank_auction: "border-blue-400/30 bg-blue-400/10 text-blue-300",
  resale: "border-zinc-400/30 bg-zinc-400/10 text-zinc-300",
  exclusive_mandate: "border-purple-400/30 bg-purple-400/10 text-purple-300",
  price_drop: "border-cyan-400/30 bg-cyan-400/10 text-cyan-300",
};

export function buildDealTags(raw: string[] | null | undefined): ListingCardViewModel["dealTags"] {
  if (!raw || raw.length === 0) return [];
  const out: ListingCardViewModel["dealTags"] = [];
  for (const tag of raw) {
    if (typeof tag !== "string") continue;
    const key = tag.trim().toLowerCase();
    if (!key) continue;
    const label = DEAL_TAG_LABELS[key];
    const tone = DEAL_TAG_TONES[key];
    if (!label || !tone) continue; // whitelist — drop anything we don't recognise
    out.push({ tag: key, label, tone });
  }
  return out;
}

// Compact AED formatter for additional charge lines: 10000 → "AED 10k",
// 2500000 → "AED 2.5M". Mirrors formatCardPrice scaling but is purely
// display-side; server stores `amount` as raw AED.
function formatChargeAmount(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return "AED —";
  if (amount >= 10_000) {
    const m = amount / 1_000_000;
    if (m >= 1) return `AED ${m % 1 === 0 ? m : m.toFixed(1).replace(/\.0$/, "")}M`;
    const k = amount / 1_000;
    return `AED ${k % 1 === 0 ? k : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  return `AED ${amount.toLocaleString("en-US")}`;
}

export function buildAdditionalCharges(
  raw: AdditionalCharge[] | null | undefined,
): ListingCardViewModel["additionalCharges"] {
  if (!raw || raw.length === 0) return [];
  const out: ListingCardViewModel["additionalCharges"] = [];
  for (const c of raw) {
    if (!c || typeof c !== "object") continue;
    const label = typeof c.label === "string" ? c.label.trim() : "";
    if (!label) continue;
    if (c.amount_type === "percent_of_price" && typeof c.amount === "number" && Number.isFinite(c.amount)) {
      const pct = c.amount;
      out.push({ label, amountLabel: `${pct % 1 === 0 ? pct.toFixed(0) : pct.toFixed(2)}% of price` });
      continue;
    }
    if (c.amount_type === "fixed" && typeof c.amount === "number" && Number.isFinite(c.amount) && c.amount > 0) {
      out.push({ label, amountLabel: `+ ${formatChargeAmount(c.amount)}` });
      continue;
    }
    // Malformed entry — drop silently rather than render "undefined%".
  }
  return out;
}

export function toListingCardViewModel(
  row: ListingCardFields,
  isBuilding: boolean,
  fallbackLocality?: string | null,
): ListingCardViewModel {
  // On a building page, inherit the building's confirmed locality when the
  // individual listing failed to resolve its own micro_market.
  const ownLocality = listingLocality(row);
  const locality = ownLocality ?? (fallbackLocality && fallbackLocality.trim() ? fallbackLocality.trim() : null);
  const hasLocality = Boolean(locality);
  const specItems = buildSpecItems(row);
  // Compute the SEO slug once so card href, JSON-LD, sitemap, and the
  // back-compat redirect all agree on the canonical URL.
  const slug = row.id != null
    ? buildListingSlug({
        id: row.id,
        bhk: row.bhk,
        micro_market: row.micro_market,
        building_name: row.building_name,
        property_type: row.property_type,
      })
    : null;
  return {
    title: buildTitle(row),
    locality,
    localitySlug: locality ? slugify(locality) : null,
    isBuilding,
    priceLabel: formatCardPrice(row.price, row.price_unit, row.intent, row.price_model, row.price_per_sqft, row.area_sqft, row.price_raw_text),
    specRow: buildSpecRow(specItems),
    specItems,
    statusLabel: hasLocality ? "Listed" : "Locality unconfirmed",
    statusTone: hasLocality ? "listed" : "unconfirmed",
    updatedLabel: formatUpdated(row.last_seen),
    freshnessLabel: formatFreshness(row.last_seen),
    freshnessBadge: formatFreshnessBadge(row.last_seen),
    assetTypeLabel: assetTypeLabel(row.asset_type, row.intent),
    waLink: waLinkFor(row.id),
    // The public route is /listings/[slug]/[id].  Keeping both segments here
    // prevents every card click/prefetch from requesting a one-segment 404.
    slug,
    href: row.id != null && Number.isFinite(row.id)
      ? `/listings/${slug ?? "listing"}/${row.id}`
      : null,
    waAvailable: isBrokerContactable(row.broker_phone),
    brokerName: safeBrokerName(row.broker_name),
    priceModel: row.price_model ?? null,
    pricePerSqft: row.price_per_sqft ?? null,
    dealTags: buildDealTags(row.deal_tags),
    additionalCharges: buildAdditionalCharges(row.additional_charges),
  };
}
