// Card view-model tests for the public /search result cards.
// Run: npx tsx test/listing-card.test.ts
import assert from "node:assert/strict";
import {
  toListingCardViewModel,
  formatCardPrice,
  waLinkFor,
  buildListingSlug,
  isBrokerContactable,
  type ListingCardFields,
  type AdditionalCharge,
} from "../src/lib/listing-card";

function base(over: Partial<ListingCardFields>): ListingCardFields {
  return {
    id: 1,
    bhk: "3 BHK",
    price: 1_500_000,
    price_unit: "abs",
    price_model: null,
    price_per_sqft: null,
    area_sqft: 1450,
    furnishing: "Semi-furnished",
    intent: "sell",
    asset_type: null,
    property_type: null,
    micro_market: "Dubai Marina",
    building_name: null,
    landmark_name: null,
    location_label: null,
    floor_description: null,
    view: null,
    title: null,
    broker_name: "Acme Broker",
    broker_phone: "+971501234567",
    last_seen: new Date().toISOString(),
    ...over,
  };
}

let passed = 0;
function check(name: string, fn: () => void) {
  fn();
  passed += 1;
  console.log(`  ok - ${name}`);
}

console.log("listing-card view model tests");

// Titles are deterministic summaries of structured data, never raw poster copy.
check("building card title is a normalized structured summary", () => {
  const vm = toListingCardViewModel(base({ building_name: "Marina Gate" }), true);
  assert.equal(vm.title, "Semi Furnished 3 BHK for Sale at Marina Gate");
  assert.equal(vm.locality, "Dubai Marina");
});

check("no building name -> structured title uses locality", () => {
  const vm = toListingCardViewModel(base({ building_name: null }), false);
  assert.equal(vm.title, "Semi Furnished 3 BHK for Sale at Dubai Marina");
  assert.equal(vm.locality, "Dubai Marina");
});
check("underscore furnishing values render as readable words", () => {
  const vm = toListingCardViewModel(base({ furnishing: "fully_furnished" }), false);
  assert.match(vm.title, /^Fully Furnished /);
  assert.match(vm.specRow, /Fully Furnished/);
});

check("generic property type does not leak as 'Other'", () => {
  const vm = toListingCardViewModel(base({ bhk: null, property_type: "Other", asset_type: "commercial" }), false);
  assert.equal(vm.title, "Semi Furnished Commercial Space for Sale at Dubai Marina");
  assert.equal(vm.specRow.includes("Other"), false);
});

check("price_model psf uses area to compute the public price label", () => {
  const vm = toListingCardViewModel(
    base({ price: 750, price_unit: "abs", price_model: "psf", price_per_sqft: 750, area_sqft: 1000, intent: "sell" }),
    false,
  );
  assert.equal(vm.priceLabel, "AED 750k");
});

// Price always carries an explicit unit.
check("sale price in millions renders M", () => {
  const vm = toListingCardViewModel(base({ price: 1_500_000, price_unit: "abs", intent: "sell" }), false);
  assert.match(vm.priceLabel, /M$/);
});
check("m unit renders M directly", () => {
  const vm = toListingCardViewModel(base({ price: 2.5, price_unit: "m", intent: "sell" }), false);
  assert.equal(vm.priceLabel, "AED 2.5M");
});
check("rental price renders /month", () => {
  const vm = toListingCardViewModel(base({ price: 85000, price_unit: "abs", intent: "rent" }), false);
  assert.match(vm.priceLabel, /\/month$/);
});
check("null price -> Price on request (never bare number)", () => {
  const vm = toListingCardViewModel(base({ price: null, price_unit: null }), false);
  assert.equal(vm.priceLabel, "Price on request");
});
check("abs unit with no scale -> explicit AED, scaled to thousands", () => {
  const vm = toListingCardViewModel(base({ price: 37000, price_unit: "abs", intent: "commercial" }), false);
  assert.match(vm.priceLabel, /^AED \d+k$/);
  assert.equal(vm.priceLabel.match(/(M|\/month)$/), null);
});
check("large absolute sale values are normalized to M", () => {
  const vm = toListingCardViewModel(base({ price: 4_400_000_000, price_unit: "abs", intent: "sell" }), false);
  assert.match(vm.priceLabel, /M$/);
  assert.equal(vm.priceLabel, "AED 4400M");
});
check("large absolute rent values are normalized per month", () => {
  const vm = toListingCardViewModel(base({ price: 4_400_000_000, price_unit: "abs", intent: "rent" }), false);
  assert.match(vm.priceLabel, /\/month$/);
  assert.match(vm.priceLabel, /M\/month$/);
});

// Status badge is buyer-readable, not internal "Market pending".
check("micro_market present -> Available", () => {
  const vm = toListingCardViewModel(base({ micro_market: "JVC" }), false);
  assert.equal(vm.statusLabel, "Listed");
  assert.equal(vm.statusTone, "listed");
});
check("micro_market null -> Locality unconfirmed (not 'Market pending')", () => {
  const vm = toListingCardViewModel(base({ micro_market: null }), false);
  assert.equal(vm.statusLabel, "Locality unconfirmed");
  assert.equal(vm.statusTone, "unconfirmed");
});

// Recency label renamed to Updated (not Seen).
check("updatedLabel is a date string, not 'Seen'", () => {
  const vm = toListingCardViewModel(base({}), false);
  assert.ok(vm.updatedLabel.length > 0);
  assert.notEqual(vm.updatedLabel, "Unknown");
});

// Broker contact must not embed the phone in public HTML.
// waLinkFor returns a server route that resolves the phone server-side.
check("waLinkFor returns the server redirect route (no phone in URL)", () => {
  assert.equal(waLinkFor(123), "/api/contact-broker/123");
});
check("waLinkFor(null) -> no link", () => {
  assert.equal(waLinkFor(null), null);
});
check("missing listing id -> no wa link (no dead CTA)", () => {
  const vm = toListingCardViewModel(base({ id: null as unknown as number }), false);
  assert.equal(vm.waLink, null);
});

// The canonical repro: a 3BHK in a stated locality must yield cards that
// satisfy the public-format requirements.
check('"3 bhk in jvc" card has a structured title + unit price + status + CTA', () => {
  const vm = toListingCardViewModel(
    base({ bhk: "3 BHK", micro_market: "JVC", building_name: "Belgravia", price: 1_800_000, price_unit: "abs", intent: "sell", broker_phone: "+971501234567" }),
    true,
  );
  assert.equal(vm.title, "Semi Furnished 3 BHK for Sale at Belgravia");
  assert.match(vm.priceLabel, /M$/);
  assert.equal(vm.statusLabel, "Listed");
  assert.equal(vm.waLink, "/api/contact-broker/1");
});

// Deal tags: whitelist enforced server-side too; here we verify the public
// card renders the right label + tone for each known tag and silently drops
// anything that isn't on the whitelist (defence-in-depth against bad DB rows).
check("deal_tags renders label + tone for whitelisted tags", () => {
  const vm = toListingCardViewModel(
    base({ deal_tags: ["distress_sale", "bank_auction", "negotiable"] }),
    false,
  );
  assert.equal(vm.dealTags.length, 3);
  assert.deepEqual(vm.dealTags.map((t) => t.tag), ["distress_sale", "bank_auction", "negotiable"]);
  assert.deepEqual(vm.dealTags.map((t) => t.label), ["Distress sale", "Bank auction", "Negotiable"]);
  // Tone classes are Tailwind class fragments; assert presence of the brand colour.
  assert.match(vm.dealTags[0].tone, /red/);
  assert.match(vm.dealTags[1].tone, /blue/);
  assert.match(vm.dealTags[2].tone, /emerald/);
});
check("deal_tags drops unknown values silently (no crash, no leak)", () => {
  const vm = toListingCardViewModel(
    base({ deal_tags: ["distress_sale", "liquidation", "URGENT_SALE", "  ", null as unknown as string] }),
    false,
  );
  // 'URGENT_SALE' is the same whitelist entry as 'urgent_sale' (case-insensitive).
  assert.equal(vm.dealTags.length, 2);
  assert.deepEqual(vm.dealTags.map((t) => t.tag), ["distress_sale", "urgent_sale"]);
});
check("deal_tags null/empty -> empty VM array", () => {
  const a = toListingCardViewModel(base({ deal_tags: null }), false);
  const b = toListingCardViewModel(base({ deal_tags: [] }), false);
  const c = toListingCardViewModel(base({}), false);
  assert.deepEqual(a.dealTags, []);
  assert.deepEqual(b.dealTags, []);
  assert.deepEqual(c.dealTags, []);
});

// Additional charges: fixed amounts render as '+ AED Xk/M'; percent
// amounts render as 'N% of price'; malformed entries are dropped silently.
check("additional_charges renders fixed amounts with explicit unit", () => {
  const vm = toListingCardViewModel(
    base({
      additional_charges: [
        { label: "Society dues", amount: 10_000, amount_type: "fixed" },
        { label: "Professional fees", amount: 1_500_000, amount_type: "fixed" },
      ],
    }),
    false,
  );
  assert.equal(vm.additionalCharges.length, 2);
  assert.equal(vm.additionalCharges[0].label, "Society dues");
  assert.equal(vm.additionalCharges[0].amountLabel, "+ AED 10k");
  assert.equal(vm.additionalCharges[1].amountLabel, "+ AED 1.5M");
});
check("additional_charges renders percent_of_price as 'N% of price'", () => {
  const vm = toListingCardViewModel(
    base({
      additional_charges: [{ label: "Professional fees", amount: 3, amount_type: "percent_of_price" }],
    }),
    false,
  );
  assert.equal(vm.additionalCharges.length, 1);
  assert.equal(vm.additionalCharges[0].amountLabel, "3% of price");
});
check("additional_charges drops malformed entries silently", () => {
  const vm = toListingCardViewModel(
    base({
      additional_charges: [
        { label: "Society dues", amount: 100000, amount_type: "fixed" },              // valid
        { label: "", amount: 100000, amount_type: "fixed" },                          // missing label
        { label: "Garbage" } as unknown as AdditionalCharge,                          // missing amount
        { label: "Bad", amount: 100000, amount_type: "weekly" } as unknown as AdditionalCharge, // bad amount_type
        { label: "NaN", amount: Number.NaN, amount_type: "fixed" },                   // non-finite amount
        null as unknown as AdditionalCharge,                                          // null entry
      ],
    }),
    false,
  );
  assert.equal(vm.additionalCharges.length, 1);
  assert.equal(vm.additionalCharges[0].label, "Society dues");
});
check("additional_charges null/empty -> empty VM array", () => {
  const a = toListingCardViewModel(base({ additional_charges: null }), false);
  const b = toListingCardViewModel(base({ additional_charges: [] }), false);
  const c = toListingCardViewModel(base({}), false);
  assert.deepEqual(a.additionalCharges, []);
  assert.deepEqual(b.additionalCharges, []);
  assert.deepEqual(c.additionalCharges, []);
});

// ── SEO slug (buildListingSlug) ────────────────────────────────────
//
// The public route /listings/[slug]/[id] uses this slug. Format is
// "{bhk-or-property-type}-{locality-or-empty}-{id}" — the id is always
// appended so the URL stays unique even when the prefix is empty.
check("buildListingSlug formats bhk + locality + id", () => {
  assert.equal(
    buildListingSlug({ id: 12345, bhk: "3 BHK", micro_market: "Dubai Marina" }),
    "3-bhk-dubai-marina-12345",
  );
});
check("buildListingSlug falls back to building when locality missing", () => {
  assert.equal(
    buildListingSlug({ id: 319236, bhk: "3 BHK", micro_market: null, building_name: "Marina Gate" }),
    "3-bhk-marina-gate-319236",
  );
});
check("buildListingSlug returns just the id when no fields available", () => {
  assert.equal(buildListingSlug({ id: 999 }), "999");
  assert.equal(buildListingSlug({ id: 999, bhk: "", micro_market: "", building_name: "" }), "999");
});
check("buildListingSlug handles fractional bhk", () => {
  assert.equal(
    buildListingSlug({ id: 999, bhk: "2.5 BHK", micro_market: "Business Bay" }),
    "2-5-bhk-business-bay-999",
  );
});
check("buildListingSlug removes a float suffix from whole-number bhk", () => {
  assert.equal(
    buildListingSlug({ id: 93, bhk: "2.0", building_name: "Marina Gate", micro_market: "JBR" }),
    "2-bhk-marina-gate-jbr-93",
  );
});
check("furnishing labels keep Fully Furnished as two words", () => {
  const vm = toListingCardViewModel(base({ furnishing: "fullyfurnished" }), false);
  assert.match(vm.title, /^Fully Furnished /);
  assert.match(vm.specRow, /Fully Furnished/);
});
check("buildListingSlug returns null for non-finite id", () => {
  assert.equal(buildListingSlug({ id: NaN as unknown as number, bhk: "3 BHK" }), null);
});
check("href uses the slug and id required by the public route", () => {
  const vm = toListingCardViewModel(
    base({ id: 319236, bhk: "3 BHK", micro_market: "Al Barsha", building_name: "Al Sarab Tower" }),
    false,
  );
  assert.equal(vm.href, "/listings/3-bhk-al-sarab-tower-al-barsha-319236/319236");
  assert.equal(vm.slug, "3-bhk-al-sarab-tower-al-barsha-319236");
});
check("href falls back to bare id when slug cannot be computed", () => {
  // NaN id produces a null slug and a null href.
  const vm = toListingCardViewModel(base({ id: NaN as unknown as number }), false);
  assert.equal(vm.href, null);
  assert.equal(vm.slug, null);
});

// ── waAvailable (broker contactability) ────────────────────────────
//
// Mirrors the server-side check in /api/contact-broker/[id] so the public
// card never shows a button that would just 302 back to the listing.
check("waAvailable true when broker_phone is UAE E.164 digits", () => {
  const vm = toListingCardViewModel(base({ broker_phone: "+971501234567" }), false);
  assert.equal(vm.waAvailable, true);
});
check("waAvailable true when broker_phone has bare country-code digits", () => {
  const vm = toListingCardViewModel(base({ broker_phone: "971501234567" }), false);
  assert.equal(vm.waAvailable, true);
});
check("waAvailable true for legacy India-format rows", () => {
  const vm = toListingCardViewModel(base({ broker_phone: "+91 9820056180" }), false);
  assert.equal(vm.waAvailable, true);
});
check("waAvailable false when broker_phone null", () => {
  const vm = toListingCardViewModel(base({ broker_phone: null }), false);
  assert.equal(vm.waAvailable, false);
});
check("waAvailable false when broker_phone too short", () => {
  const vm = toListingCardViewModel(base({ broker_phone: "12345" }), false);
  assert.equal(vm.waAvailable, false);
});
check("isBrokerContactable handles raw digits", () => {
  assert.equal(isBrokerContactable("971501234567"), true);
  assert.equal(isBrokerContactable("+971 50 123 4567"), true);
  assert.equal(isBrokerContactable("919820056180"), true);
  assert.equal(isBrokerContactable("9820056180"), true);
  assert.equal(isBrokerContactable(null), false);
  assert.equal(isBrokerContactable(undefined), false);
  assert.equal(isBrokerContactable(""), false);
  assert.equal(isBrokerContactable("12345"), false);
});

console.log(`\n${passed} checks passed`);
