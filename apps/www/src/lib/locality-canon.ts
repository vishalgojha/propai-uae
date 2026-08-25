// Canonical locality mapping for www.propai.live (UAE / Dubai).
//
// Purpose: normalise the dirty `micro_market` strings that accumulate during
// ingestion. The www read path resolves every raw value through this module so
// duplicates merge, non-places hide, and implied short forms map to a
// confirmed canonical label — without needing a backfill first. The backfill
// script (scripts/backfill_canonical_localities.py) applies the same rules to
// the stored rows.
//
// Rules:
//  - Always trim + case-fold for comparison.
//  - Implied expansion applies ONLY to these unambiguous bare parents:
//      "Marina"   -> Dubai Marina
//      "Downtown" -> Downtown Dubai
//      "Barsha"   -> Al Barsha
//      "Furjan"   -> Al Furjan
//      "Ranches"  -> Arabian Ranches
//      "Springs"  -> The Springs
//      "Meadows"  -> The Meadows
//      "Greens"   -> The Greens
//  - Acronym handling: JBR/JVC/JVT/JLT/DIFC map to their expanded labels.
//  - "Dubai" (bare) stays its own bucket, public via general search but with
//    NO standalone page.
//  - Standalone public pages are opt-in. Raw micro_market values are ingestion
//    data, not an editorial locality taxonomy, so an unknown value must never
//    automatically create a public location page.

import { slugify } from "./supabase";

export type CanonicalLocality = {
  /** Display label, e.g. "Dubai Marina". */
  label: string;
  /** URL slug, e.g. "dubai-marina". */
  slug: string;
  /** True if this locality should appear anywhere on public pages. */
  public: boolean;
  /** True if this locality gets its own /localities/[slug] detail page.
   *  The bare parent "Dubai" is false — surfaced only via general search. */
  standalonePage: boolean;
};

// Non-place internal buckets → hidden from all public surfaces.
const HIDDEN_BUCKETS = new Set<string>([
  "unknown",
  "not specified",
  "not available",
  "n/a",
  "na",
  "none",
  "null",
  "nil",
  "listing",
  "requirement",
  "property",
  "text",
]);

// Generic parents that keep their own bucket but get NO standalone page.
const GENERIC_PARENTS = new Set<string>([
  "dubai",
  "uae",
]);

// Implied-expansion map (bare parent -> confirmed canonical label).
const IMPLIED_DIRECTION: Record<string, string> = {
  marina: "Dubai Marina",
  downtown: "Downtown Dubai",
  barsha: "Al Barsha",
  furjan: "Al Furjan",
  ranches: "Arabian Ranches",
  springs: "The Springs",
  meadows: "The Meadows",
  greens: "The Greens",
};

// Explicit redirects (case-folded raw -> canonical label).
const REDIRECTS: Record<string, string> = {
  jbr: "JBR",
  "jumeirah beach residence": "JBR",
  "jumeirah beach residences": "JBR",
  "burj khalifa": "Downtown Dubai",
  "old town": "Downtown Dubai",
  "opera district": "Downtown Dubai",
  difc: "DIFC",
  "dubai international financial centre": "DIFC",
  palm: "Palm Jumeirah",
  pj: "Palm Jumeirah",
  "palm jumeriah": "Palm Jumeirah",
  "signature villas": "Palm Jumeirah",
  "jumeirah village circle": "JVC",
  "jumeirah village triangle": "JVT",
  "jumeriah lakes towers": "JLT",
  "jumeirah lakes towers": "JLT",
  impz: "Production City",
  "production city": "Production City",
  "international media production zone": "Production City",
  "dubai hills": "Dubai Hills Estate",
  "hills estate": "Dubai Hills Estate",
  "damac hills": "Damac Hills",
  "akoya oxygen": "Damac Hills 2",
  "damac hills 2": "Damac Hills 2",
  "the lakes": "The Lakes",
  "the views": "The Views",
  "discovery gardens": "Discovery Gardens",
  "jebel ali": "Jebel Ali",
  "jebel ali village": "Jebel Ali",
  "al khail gate": "Al Quoz",
  "al quoz": "Al Quoz",
  "al qouz": "Al Quoz",
  "al quoz industrial": "Al Quoz",
  "meydan city": "Meydan",
  "mbr city": "MBR City",
  "mohammed bin rashid city": "MBR City",
  "district one": "MBR City",
  "district 7": "MBR City",
  "reem dubai": "Reem",
  "the villa": "The Villa",
  "al waha": "Silicon Oasis",
  "dubai silicon oasis": "Silicon Oasis",
  "silicon central": "Silicon Oasis",
};

// The public browse taxonomy. Add a location here only after it has been
// reviewed as a market-level area, rather than relying on whatever free text
// happened to be assigned to listings during ingestion.
const STANDALONE_LOCALITIES: Record<string, string> = {
  "business bay": "Business Bay",
  "downtown dubai": "Downtown Dubai",
  "dubai marina": "Dubai Marina",
  jbr: "JBR",
  difc: "DIFC",
  "palm jumeirah": "Palm Jumeirah",
  jvc: "JVC",
  jvt: "JVT",
  jlt: "JLT",
  "dubai hills estate": "Dubai Hills Estate",
  "damac hills": "Damac Hills",
  "damac hills 2": "Damac Hills 2",
  "arabian ranches": "Arabian Ranches",
  "arabian ranches 2": "Arabian Ranches",
  "arabian ranches 3": "Arabian Ranches",
  "the springs": "The Springs",
  "the meadows": "The Meadows",
  "the greens": "The Greens",
  "the lakes": "The Lakes",
  "the views": "The Views",
  "al barsha": "Al Barsha",
  "al barsha south": "Al Barsha South",
  "al furjan": "Al Furjan",
  deira: "Deira",
  karama: "Karama",
  mirdif: "Mirdif",
  "motor city": "Motor City",
  "sports city": "Sports City",
  "studio city": "Studio City",
  "production city": "Production City",
  "remraam": "Remraam",
  mudon: "Mudon",
  arjan: "Arjan",
  "town square": "Town Square",
  "dubailand": "Dubailand",
  liwan: "Liwan",
  majan: "Majan",
  "nad al sheba": "Nad Al Sheba",
  meydan: "Meydan",
  "mbr city": "MBR City",
  reem: "Reem",
  "city walk": "City Walk",
  zaabeel: "Zaabeel",
  "al jaddaf": "Al Jaddaf",
  "oud metha": "Oud Metha",
  "bur dubai": "Bur Dubai",
  satwa: "Satwa",
  jumeirah: "Jumeirah",
  "umm suqeim": "Umm Suqeim",
  "al sufouh": "Al Sufouh",
  "emirates hills": "Emirates Hills",
  "jumeirah golf estates": "Jumeirah Golf Estates",
  "jumeirah islands": "Jumeirah Islands",
  "green community": "Green Community",
  "dubai investment park": "Dubai Investment Park",
  "discovery gardens": "Discovery Gardens",
  "jebel ali": "Jebel Ali",
  "dubai silicon oasis": "Silicon Oasis",
  "academic city": "Academic City",
  "al warqa": "Al Warqa",
  muhaisnah: "Muhaisnah",
  "international city": "International City",
  "al nahda": "Al Nahda",
  "al qusais": "Al Qusais",
  rashidiya: "Rashidiya",
  hatta: "Hatta",
};

const KNOWN_LOCALITY_LABELS = Array.from(new Set([
  ...Object.values(STANDALONE_LOCALITIES),
  ...Object.values(REDIRECTS),
  ...Object.values(IMPLIED_DIRECTION),
]));

function normalise(raw: string): string {
  // This resolver is used for both stored locality labels ("Dubai Marina")
  // and dynamic route params ("dubai-marina"). Treat slug separators as word
  // separators so every canonical locality survives a label -> slug -> route
  // round trip.
  return (raw ?? "")
    .trim()
    .replace(/-+/g, " ")
    .replace(/\s+/g, " ")
    .toLowerCase();
}

export function canonicalLocality(raw: string | null | undefined): CanonicalLocality {
  const input = normalise(raw ?? "");
  if (!input) {
    return { label: "", slug: "", public: false, standalonePage: false };
  }

  // Hidden internal buckets.
  if (HIDDEN_BUCKETS.has(input)) {
    return { label: "", slug: "", public: false, standalonePage: false };
  }

  // Explicit redirects (most specific first).
  if (REDIRECTS[input]) {
    const label = REDIRECTS[input];
    return { label, slug: slugify(label), public: true, standalonePage: true };
  }

  // Implied expansion for the confirmed bare parents.
  if (IMPLIED_DIRECTION[input]) {
    const label = IMPLIED_DIRECTION[input];
    return { label, slug: slugify(label), public: true, standalonePage: true };
  }

  // Generic parent: keep own bucket, no standalone page, but still public
  // (surfaced via general search).
  if (GENERIC_PARENTS.has(input)) {
    const label = raw!.trim().replace(/\s+/g, " ");
    return { label, slug: slugify(label), public: true, standalonePage: false };
  }

  const label = STANDALONE_LOCALITIES[input];
  if (label) {
    return { label, slug: slugify(label), public: true, standalonePage: true };
  }

  // Unreviewed raw values remain available to ingestion and broad listing
  // search, but cannot appear in the public locality index or create a route.
  return { label: "", slug: "", public: false, standalonePage: false };
}

/**
 * Return the stored slugs that can represent one public canonical locality.
 *
 * The database column is derived from raw ingestion text, so historical rows
 * may contain `marina`, `impz`, or `burj-khalifa` even though the public
 * page is grouped under `dubai-marina` / `production-city` /
 * `downtown-dubai`. Keep this expansion in the read path until the stored
 * column is rebuilt from the canonical taxonomy.
 */
export function localityQuerySlugs(raw: string): string[] {
  const canonical = canonicalLocality(raw);
  if (!canonical.public || !canonical.slug) return [];

  const slugs = new Set<string>([canonical.slug]);
  for (const value of [
    ...Object.keys(REDIRECTS),
    ...Object.keys(IMPLIED_DIRECTION),
    ...Object.keys(STANDALONE_LOCALITIES),
  ]) {
    const mapped = canonicalLocality(value);
    if (mapped.slug === canonical.slug) slugs.add(slugify(value));
  }
  return Array.from(slugs);
}

/** Historical text labels that may represent one canonical public locality. */
export function localityQueryLabels(raw: string): string[] {
  const canonical = canonicalLocality(raw);
  if (!canonical.public || !canonical.slug) return [];

  const labels = new Set<string>([canonical.label]);
  for (const value of [
    ...Object.keys(REDIRECTS),
    ...Object.keys(IMPLIED_DIRECTION),
    ...Object.keys(STANDALONE_LOCALITIES),
  ]) {
    const mapped = canonicalLocality(value);
    if (mapped.slug === canonical.slug) labels.add(value);
  }
  return Array.from(labels);
}

/** Convenience: is this raw value hidden from public pages? */
export function isHiddenLocality(raw: string | null | undefined): boolean {
  return !canonicalLocality(raw).public;
}

/** Extract the longest reviewed locality phrase embedded in free text. */
export function extractLocalityFromText(raw: string | null | undefined): string | null {
  const text = (raw ?? "").trim().replace(/\s+/g, " ");
  if (!text) return null;
  return KNOWN_LOCALITY_LABELS
    .filter((label) => {
      const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp(`(^|[^a-z])${escaped}(?=$|[^a-z])`, "i").test(text);
    })
    .sort((a, b) => b.length - a.length)[0] ?? null;
}
