// Centralized, intent-driven SEO copy generation for PropAI.
// All titles/descriptions are produced here so every route stays consistent
// with the editorial rules:
//   - primary keyword first, human readable
//   - brand ("PropAI") always at the very end
//   - never start with "PropAI"
//   - under ~60 chars for titles where possible
//   - descriptions 140-160 chars, natural language, no broken numbers

export type Txn = "sale" | "rent";

function titleCase(s: string): string {
  return s
    .split(/\s+/)
    .map((w) => (w.length > 2 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w))
    .join(" ");
}

// ---- Locality titles -------------------------------------------------------

export function localityTitle(locality: string): string {
  return `${locality} Properties for Sale and Rent — PropAI`;
}

export function localityTxnTitle(locality: string, txn: Txn): string {
  const verb = txn === "rent" ? "for Rent" : "for Sale";
  return `${locality} Properties ${verb} — PropAI`;
}

export function localityBhkTitle(locality: string, bhk: string): string {
  return `${bhk} Flats for Sale and Rent in ${locality} — PropAI`;
}

export function localityBudgetTitle(
  locality: string,
  bhk: string | null,
  txn: Txn,
  budgetLabel: string,
): string {
  const subject = bhk ? `${bhk} in ${locality}` : `${titleCase(locality)} property`;
  const verb = txn === "rent" ? "for Rent" : "for Sale";
  return `${subject} ${budgetLabel} ${verb} — PropAI`;
}

export function localityCommercialTitle(locality: string, kind: string): string {
  return `${titleCase(kind)} for Sale and Rent in ${locality} — PropAI`;
}

// ---- Building titles -------------------------------------------------------

export function buildingTitle(name: string): string {
  return `${name} — Live Property Listings — PropAI`;
}

export function buildingTxnTitle(name: string, txn: Txn): string {
  const verb = txn === "rent" ? "for Rent" : "for Sale";
  return `${name} ${verb} — Live Property Listings — PropAI`;
}

// ---- Listing / 3BHK / budget generic --------------------------------------

export function listingTitle(card: {
  title: string;
  locality: string | null;
  priceLabel: string;
}): string {
  const where = card.locality ? ` in ${card.locality}` : "";
  const base = card.title || "Property";
  if (card.priceLabel && card.priceLabel !== "Price on request") {
    return `${base}${where} — ${card.priceLabel} — PropAI`;
  }
  return `${base}${where} — PropAI`;
}

export function searchTitle(query: string): string {
  const q = query.trim();
  if (!q) return "Search Property Listings — PropAI";
  return `${q} — Live Property Search — PropAI`;
}

// ---- Programmatic sub-page titles (locality x txn / bhk / budget / commercial) ----

export function localitySegmentTitle(
  locality: string,
  segment: "sale" | "rent" | "commercial",
): string {
  if (segment === "commercial") return `${titleCase(locality)} Commercial Properties for Sale and Rent — PropAI`;
  const verb = segment === "rent" ? "for Rent" : "for Sale";
  return `${locality} Properties ${verb} — PropAI`;
}

export function localityBhkSegmentTitle(locality: string, bhk: number): string {
  const label = bhk >= 5 ? "5+ BHK" : `${bhk} BHK`;
  return `${label} Flats for Sale and Rent in ${locality} — PropAI`;
}

export function localityBudgetSegmentTitle(
  locality: string,
  budgetLabel: string,
  txn: Txn,
): string {
  const subject = `${titleCase(locality)} property`;
  const verb = txn === "rent" ? "for Rent" : "for Sale";
  return `${subject} ${budgetLabel} ${verb} — PropAI`;
}

export function localitySegmentDescription(opts: {
  locality: string;
  segmentLabel: string;
  listingCount: number;
  txn: Txn;
}): string {
  const { locality, segmentLabel, listingCount, txn } = opts;
  const verb = txn === "rent" ? "for rent" : "for sale";
  const parts: string[] = [];
  parts.push(
    `Explore ${listingCount.toLocaleString("en-AE")} live ${segmentLabel} listings in ${locality} ${verb}.`,
  );
  parts.push("Filter by budget, furnishing and building, then contact the listing broker instantly on WhatsApp.");
  return clip(parts.join(" "), 155);
}

// ---- Descriptions (natural language, 140-160 chars) -----------------------

function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1).replace(/\s+\S*$/, "");
  return `${cut}.`;
}

export function localityDescription(opts: {
  locality: string;
  totalListings: number;
  buildingCount: number;
  saleCount: number;
  rentCount: number;
  topBhk: string | null;
}): string {
  const { locality, totalListings, buildingCount, saleCount, rentCount, topBhk } = opts;
  const parts: string[] = [];
  parts.push(
    `Browse ${totalListings.toLocaleString("en-AE")} live ${locality} property listings across ${buildingCount} buildings.`,
  );
  if (saleCount > 0 && rentCount > 0) {
    parts.push(`Includes ${saleCount} for sale and ${rentCount} for rent.`);
  } else if (saleCount > 0) {
    parts.push(`Includes ${saleCount} for sale.`);
  } else if (rentCount > 0) {
    parts.push(`Includes ${rentCount} for rent.`);
  }
  if (topBhk) parts.push(`${topBhk} homes are most common.`);
  parts.push("Connect directly with verified brokers. Updated in real time.");
  return clip(parts.join(" "), 155);
}

export function buildingDescription(opts: {
  name: string;
  locality: string | null;
  listingCount: number;
  saleCount: number;
  rentCount: number;
}): string {
  const { name, locality, listingCount, saleCount, rentCount } = opts;
  const where = locality ? ` in ${locality}` : "";
  const parts: string[] = [];
  parts.push(
    `Explore ${listingCount.toLocaleString("en-AE")} live listings at ${name}${where}.`,
  );
  if (saleCount > 0 && rentCount > 0) {
    parts.push(`${saleCount} for sale, ${rentCount} for rent.`);
  } else if (saleCount > 0) {
    parts.push(`${saleCount} available for sale.`);
  } else if (rentCount > 0) {
    parts.push(`${rentCount} available for rent.`);
  }
  parts.push("Contact the posting broker instantly on WhatsApp.");
  return clip(parts.join(" "), 155);
}

export type ListingSourceFacts = {
  bhk: string | null;
  landmark: string | null;
  view: string | null;
  parking: string | null;
  pets: boolean;
  possession: string | null;
};

/** Extract only high-signal, allow-listed facts from the private source slice.
 * The source text itself is never rendered; these facts are safe display copy.
 */
export function extractListingSourceFacts(
  message: string | null | undefined,
  building: string | null | undefined,
  locality: string | null | undefined,
): ListingSourceFacts {
  const text = message || "";
  const lower = text.toLowerCase();
  const bhk = text.match(/\b(\d+(?:\.\d+)?)\s*bhk\b/i)?.[1] ?? null;
  const view = text.match(/\b(partial\s+sea\s+view|sea\s+view|garden\s+view|city\s+view|pool\s+view)\b/i)?.[1] ?? null;
  const parking = text.match(/\b(\d+)\s+car\s+parking\s+(?:available|provided|included)\b/i)?.[1]
    ? `${text.match(/\b(\d+)\s+car\s+parking\s+(?:available|provided|included)\b/i)?.[1]} car parking`
    : (/\bcar\s+parking\s+(?:available|provided|included)\b/i.test(text) ? "car parking" : null);
  const possession = text.match(/\bpossession\s+([\w\s]+?)(?=[.!\n]|$)/i)?.[0]?.trim() ?? null;

  let landmark: string | null = text.match(/\b(?:near|opposite|opp\.?|next\s+to|behind)\s+([A-Za-z][A-Za-z .&'-]{2,45})/i)?.[1]?.trim() ?? null;
  if (!landmark && building && locality) {
    const lines = text.split(/\r?\n|,/).map((line) => line.replace(/[\*_]/g, "").trim()).filter(Boolean);
    const buildingIndex = lines.findIndex((line) => line.toLowerCase().includes(building.toLowerCase()));
    const localityIndex = lines.findIndex((line, index) => index > buildingIndex && line.toLowerCase().includes(locality.toLowerCase()));
    if (buildingIndex >= 0 && localityIndex > buildingIndex) {
      const candidate = lines.slice(buildingIndex + 1, localityIndex).find((line) =>
        line.length >= 3 && line.length <= 50 && /[A-Za-z]/.test(line) &&
        !/^(building|flat|floor|rent|sale|price|available|furnished|residential|commercial|open|pets|possession|video|brokerage|kindly|call|contact)/i.test(line) &&
        !/\d{5,}|\b(?:bhk|parking|lakhs?|lakh|cr|sq\.?\s*ft)\b/i.test(line),
      );
      landmark = candidate || null;
    }
  }

  return {
    bhk,
    landmark,
    view,
    parking,
    pets: /\bpets?\s+(?:allowed|permitted|okay|ok)\b/i.test(lower),
    possession,
  };
}

export function listingDescription(opts: {
  dealType: "For rent" | "For sale";
  title: string;
  locality: string | null;
  specRow: string;
  sourceMessage?: string | null;
  building?: string | null;
  landmark?: string | null;
}, maxLength = 320): string {
  const { dealType, title, locality, specRow, sourceMessage, building } = opts;
  const facts = extractListingSourceFacts(sourceMessage, building, locality);
  const where = locality ? ` in ${locality}` : " in Dubai";
  const parts: string[] = [];
  const factBhk = facts.bhk ? `${facts.bhk} BHK ` : "";
  const landmark = opts.landmark || facts.landmark;
  const place = landmark ? `${where}, near ${landmark}` : where;
  const furnishing = specRow.match(/\b(fully furnished|semi[- ]furnished|unfurnished)\b/i)?.[1]?.toLowerCase() ?? "";
  const buildingLabel = (building || title).replace(/\s+for\s+(?:rent|sale)\s+at\s+.*$/i, "").trim();
  const subject = `${factBhk}${furnishing ? `${furnishing} ` : ""}home`.trim();
  parts.push(`${dealType} — ${subject} at ${buildingLabel}${place}.`);
  const extras = [facts.view, facts.parking, facts.pets ? "pets allowed" : null, facts.possession]
    .filter(Boolean)
    .join("; ");
  if (extras) parts.push(`${extras.charAt(0).toUpperCase()}${extras.slice(1)}.`);
  parts.push("Listed via Dubai's live WhatsApp broker network. Contact the broker directly, no lead forms.");
  return clip(parts.join(" "), maxLength);
}

export function searchDescription(query: string): string {
  const q = query.trim() || "Dubai";
  return clip(
    `Explore live ${q} property listings from Dubai's WhatsApp broker network. Filter by budget, furnishing and building, then contact the listing broker instantly.`,
    158,
  );
}
