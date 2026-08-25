import { NextResponse } from "next/server";
import { providers } from "@/lib/ai-provider";
import { getAllLocalities } from "@/lib/localities";

export const runtime = "edge";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const q = (searchParams.get("q") || "").slice(0, 300).trim();

  if (!q) {
    return NextResponse.json({ error: "Missing q parameter" }, { status: 400 });
  }

  if (providers.length === 0) {
    return NextResponse.json({ error: "No LLM providers configured" }, { status: 503 });
  }

  let localities: Array<{ locality: string; slug: string }> = [];
  try {
    localities = await getAllLocalities();
  } catch {
    // Continue without locality list — LLM can still parse structure
  }

  const localityNames = localities.map((l) => l.locality).filter(Boolean).join(", ");

  const system = `You are a property search parser. Extract structured intent from the user's search query.

You MUST return ONLY valid JSON (no markdown, no explanation) with this exact schema:
{
  "locality": "string or null — the best-matching locality from the known list",
  "buildingName": "string or null — building/society name if mentioned (e.g. 'Kalpataru Vivant', 'Lodha Park')",
  "bhk": "number or null — number of bedrooms (0 for studio)",
  "intent": "\"rent\" | \"sale\" | null",
  "asset": "\"residential\" | \"commercial\" | null",
  "furnishing": "\"furnished\" | \"semi-furnished\" | \"unfurnished\" | null",
  "minPrice": "number or null — minimum budget in rupees",
  "maxPrice": "number or null — maximum budget in rupees",
  "confidence": "number 0-1 — how confident you are this is a real-estate query"
}

Known localities: ${localityNames}

Rules:
- A building/society name alone (e.g. "kalpataru", "lodha park") IS a valid property query — set confidence >= 0.5 and populate buildingName.
- If the query has NO real-estate intent (greetings, gibberish, unrelated questions), return confidence: 0 and nullify everything except confidence.
- Map locality abbreviations to full names (e.g. "jbr" -> "JBR", "marina" -> "Dubai Marina").
- Parse budget like "100k to 150k" -> minPrice: 100000, maxPrice: 150000.
- Parse "under 2 cr" -> maxPrice: 20000000.
- Parse "2 BHK" -> bhk: 2, "studio" -> bhk: 0.
- Return ONLY the JSON object. No markdown fences, no explanation.`;

  for (const provider of providers) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);

      const res = await fetch(`${provider.baseURL}/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${provider.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: provider.model,
          messages: [
            { role: "system", content: system },
            { role: "user", content: q },
          ],
          max_tokens: 200,
          temperature: 0,
        }),
        signal: controller.signal,
      });

      clearTimeout(timeout);

      if (!res.ok) continue;

      const data = await res.json();
      const text = data?.choices?.[0]?.message?.content?.trim();
      if (!text) continue;

      // Try to parse the JSON response
      const cleaned = text.replace(/^```json?\n?|```$/g, "").trim();
      const parsed = JSON.parse(cleaned);

      // Validate structure
      if (typeof parsed.confidence !== "number") continue;

      // Match locality against known list
      let matchedLocality: string | null = null;
      if (parsed.locality && localities.length > 0) {
        const qLower = String(parsed.locality).toLowerCase();
        const match = localities.find(
          (l) => l.locality.toLowerCase() === qLower || l.slug === qLower.replace(/\s+/g, "-"),
        );
        if (match) {
          matchedLocality = match.locality;
        } else {
          // Fuzzy match — check if any known locality contains the extracted text
          const fuzzy = localities.find(
            (l) =>
              l.locality.toLowerCase().includes(qLower) ||
              qLower.includes(l.locality.toLowerCase()),
          );
          if (fuzzy) matchedLocality = fuzzy.locality;
        }
      }

      return NextResponse.json({
        locality: matchedLocality,
        localities: matchedLocality ? [matchedLocality] : [],
        buildingName: typeof parsed.buildingName === "string" ? parsed.buildingName : null,
        bhk: typeof parsed.bhk === "number" ? parsed.bhk : null,
        intent: parsed.intent === "rent" || parsed.intent === "sale" ? parsed.intent : null,
        asset: parsed.asset === "residential" || parsed.asset === "commercial" ? parsed.asset : null,
        furnishing: ["furnished", "semi-furnished", "unfurnished"].includes(parsed.furnishing)
          ? parsed.furnishing
          : null,
        minPrice: typeof parsed.minPrice === "number" ? parsed.minPrice : null,
        maxPrice: typeof parsed.maxPrice === "number" ? parsed.maxPrice : null,
        confidence: parsed.confidence,
      });
    } catch {
      continue;
    }
  }

  // All providers failed — return low confidence so caller falls back to regex
  return NextResponse.json({
    locality: null,
    localities: [],
    bhk: null,
    intent: null,
    asset: null,
    furnishing: null,
    minPrice: null,
    maxPrice: null,
    confidence: 0,
  });
}
