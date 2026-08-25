// Hermetic unit test for the NL-search locality filter.
// Run: npx tsx apps/www/test/natural-search.test.ts
// Verifies the reported bug fix: a stated locality is extracted and ENFORCED
// (never silently dropped into a broad BHK-only search), and a stated-but-
// unknown locality yields a "no matches" state instead of mixed results.

import assert from "node:assert/strict";
import {
  parseSearchQuery,
  matchesHardFilters,
  type ParsedNaturalSearch,
  type NaturalSearchRow,
} from "../src/lib/natural-search";
import type { LocalitySummary } from "../src/lib/localities";

const gazetteer: LocalitySummary[] = [
  { locality: "JVC", slug: "jvc", listingCount: 120 },
  { locality: "JVT", slug: "jvt", listingCount: 156 },
  { locality: "Dubai Marina", slug: "dubai-marina", listingCount: 189 },
  { locality: "JBR", slug: "jbr", listingCount: 1000 },
  { locality: "Business Bay", slug: "business-bay", listingCount: 90 },
  { locality: "Al Barsha", slug: "al-barsha", listingCount: 98 },
];

function makeRow(over: Partial<NaturalSearchRow>): NaturalSearchRow {
  return {
    id: 1,
    intent: "rent",
    bhk: "3 BHK",
    price: 250000,
    price_unit: "abs",
    price_raw_text: null,
    price_model: null,
    price_per_sqft: null,
    area_sqft: 1200,
    furnishing: "furnished",
    floor_description: null,
    view: null,
    asset_type: null,
    property_type: null,
    location_label: null,
    building_name: null,
    landmark_name: null,
    micro_market: "JVC",
    locality_raw: null,
    locality_resolved: null,
    broker_name: "Test",
    broker_phone: "+971501234567",
    first_seen: null,
    last_seen: new Date().toISOString(),
    observation_count: 2,
    latitude: null,
    longitude: null,
    ...over,
  };
}

let passed = 0;
function check(name: string, fn: () => void) {
  fn();
  passed += 1;
  console.log(`  ok - ${name}`);
}

console.log("natural-search locality filter tests");

// 1. Compound locality "Dubai Marina" is extracted distinctly from "Marina"-like noise.
check('parses "3 bhk in dubai marina" -> locality === "Dubai Marina"', () => {
  const parsed = parseSearchQuery("3 bhk in dubai marina", gazetteer);
  assert.equal(parsed.locality, "Dubai Marina");
  assert.equal(parsed.bhk, 3);
  assert.equal(parsed.localityStated, true);
});

// 1b. Display-layer extraction: statedLocalityText captures the locality
// phrase, NOT a mangled substring from the BHK/budget portion.
check('statedLocalityText for "3 bhk in dubai marina" === "Dubai Marina"', () => {
  const parsed = parseSearchQuery("3 bhk in dubai marina", gazetteer);
  assert.equal(parsed.statedLocalityText, "Dubai Marina");
  assert.notEqual(parsed.statedLocalityText, "3");
});

check('statedLocalityText for "2 bhk in business bay budget 100k" === "Business Bay"', () => {
  const parsed = parseSearchQuery("2 bhk in business bay budget 100k", gazetteer);
  assert.equal(parsed.statedLocalityText, "Business Bay");
});

check('statedLocalityText for "jvc 1bhk" === "JVC" (no preposition)', () => {
  const parsed = parseSearchQuery("jvc 1bhk", gazetteer);
  assert.equal(parsed.statedLocalityText, "JVC");
});

// 2. The hard filter actually enforces locality: a JVT row is rejected.
check("matchesHardFilters rejects non-matching locality even when BHK matches", () => {
  const parsed = parseSearchQuery("3 bhk in jvc", gazetteer);
  const jvtRow = makeRow({ micro_market: "JVT" });
  const jvcRow = makeRow({ micro_market: "JVC" });
  assert.equal(matchesHardFilters(jvtRow, parsed), false);
  assert.equal(matchesHardFilters(jvcRow, parsed), true);
});

// 3. Bug repro: ALL returned cards must share the stated locality (no mixing).
check('"3 bhk in jvc" never returns JVT / other localities', () => {
  const parsed = parseSearchQuery("3 bhk in jvc", gazetteer);
  const rows = [
    makeRow({ id: 1, micro_market: "JVC" }),
    makeRow({ id: 2, micro_market: "JVT" }),
    makeRow({ id: 3, micro_market: "JBR" }),
    makeRow({ id: 4, micro_market: "Business Bay" }),
  ];
  const kept = rows.filter((r) => matchesHardFilters(r, parsed));
  assert.ok(kept.length >= 1, "expected at least one in-locality match");
  for (const r of kept) {
    assert.equal(r.micro_market, "JVC");
  }
});

// 4. Stated-but-unknown locality -> localityStated true, locality null (no silent drop).
check('"3 bhk in jvc" against gazetteer WITHOUT JVC -> unmatched, not broad', () => {
  const noJvc = gazetteer.filter((l) => l.locality !== "JVC");
  const parsed = parseSearchQuery("3 bhk in jvc", noJvc);
  assert.equal(parsed.localityStated, true);
  assert.equal(parsed.locality, null);
});

// 5. Other compounds parse distinctly.
check('parses "2 bhk in al barsha" -> "Al Barsha"', () => {
  const parsed = parseSearchQuery("2 bhk in al barsha", gazetteer);
  assert.equal(parsed.locality, "Al Barsha");
  assert.equal(parsed.bhk, 2);
});

console.log(`\n${passed} checks passed`);
