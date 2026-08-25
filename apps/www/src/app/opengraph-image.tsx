import { ImageResponse } from "next/og";

export const runtime = "edge";
export const alt = "PropAI — Real Dubai listings from broker WhatsApp groups";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          backgroundColor: "#000000",
          padding: "80px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 20, marginBottom: 40 }}>
          <div
            style={{
              width: 64,
              height: 64,
              borderRadius: 16,
              backgroundColor: "#6B8E63",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 40,
              color: "#FAF7F0",
            }}
          >
            P
          </div>
          <div style={{ display: "flex", fontSize: 52, fontWeight: 700, color: "#ffffff" }}>
            Prop<span style={{ color: "#6B8E63" }}>AI</span>
          </div>
        </div>
        <div style={{ display: "flex", fontSize: 68, fontWeight: 700, color: "#ffffff", lineHeight: 1.1 }}>
          Dubai&apos;s freshest property
        </div>
        <div style={{ display: "flex", fontSize: 68, fontWeight: 700, color: "#6B8E63", lineHeight: 1.1 }}>
          listings, straight from brokers
        </div>
        <div style={{ display: "flex", fontSize: 30, color: "#a1a1aa", marginTop: 28 }}>
          Real inventory from WhatsApp broker networks — no stale photos.
        </div>
      </div>
    ),
    { ...size },
  );
}
