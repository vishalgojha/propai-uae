import assert from "node:assert/strict";
import { canonicalLocality } from "../src/lib/locality-canon";

function check(input: string, expected: ReturnType<typeof canonicalLocality>) {
  assert.deepEqual(canonicalLocality(input), expected, input);
}

check("Jumeirah Beach Residence", {
  label: "JBR",
  slug: "jbr",
  public: true,
  standalonePage: true,
});

check("JLT", {
  label: "JLT",
  slug: "jlt",
  public: true,
  standalonePage: true,
});

check("Nad Al Sheba", {
  label: "Nad Al Sheba",
  slug: "nad-al-sheba",
  public: true,
  standalonePage: true,
});

check("Dubai Marina to JBR Corridor", {
  label: "",
  slug: "",
  public: false,
  standalonePage: false,
});

check("Dubai Marina", {
  label: "Dubai Marina",
  slug: "dubai-marina",
  public: true,
  standalonePage: true,
});

// Dynamic route params are URL slugs. They must resolve to the same canonical
// locality as their human-readable labels or every /localities/[slug] page
// falls through to notFound().
for (const [label, slug] of [
  ["Dubai Marina", "dubai-marina"],
  ["Palm Jumeirah", "palm-jumeirah"],
  ["Dubai Hills Estate", "dubai-hills-estate"],
  ["Al Barsha South", "al-barsha-south"],
] as const) {
  check(slug, {
    label,
    slug,
    public: true,
    standalonePage: true,
  });
}

console.log("locality canonicalization tests passed");
