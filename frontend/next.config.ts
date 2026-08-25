import type { NextConfig } from "next";

const apiBaseUrl = (process.env.LAB_API_BASE_URL || "http://api:8000").replace(/\/$/, "");

// Serve the dashboard under a path prefix (e.g. /broker on ae.propai.live).
// Empty/unset keeps the current root-served deployment unchanged.
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH || "").replace(/\/$/, "");

const nextConfig: NextConfig = {
  ...(basePath ? { basePath } : {}),
  typescript: { ignoreBuildErrors: true },
  experimental: {
    // Coolify exposes the host CPU count during Docker builds, so Next would
    // otherwise spawn 15 workers inside a much smaller build container. Cap
    // concurrency to prevent the builder/helper container being OOM-killed.
    cpus: 2,
  },
  transpilePackages: [
    "d3-force",
    "d3-zoom",
    "d3-selection",
    "d3-drag",
    "d3-dispatch",
    "d3-timer",
    "d3-interpolate",
    "d3-scale",
  ],
  async rewrites() {
    // Proxy API calls only after Next has checked its own route handlers.
    // Otherwise the broad /api rewrite sends /api/admin/analytics to FastAPI
    // and bypasses the local authenticated analytics handler.
    return {
      beforeFiles: [],
      afterFiles: [
        {
          source: "/api/chat",
          destination: "/api/chat",
        },
        {
          source: "/api/:path*",
          destination: `${apiBaseUrl}/api/:path*`,
        },
        {
          source: "/manifest",
          destination: "/manifest.json",
        },
      ],
      fallback: [],
    };
  },
  async headers() {
    return [
      {
        // The authenticated app shell contains deployment-specific chunk URLs and
        // user-specific navigation. Never let the CDN retain it across releases.
        // Hashed Next assets, API routes and public files are excluded below.
        source: "/((?!api(?:/|$)|_next(?:/|$)|sw\\.js$|manifest\\.json$|.*\\.[^/]+$).*)",
        headers: [
          {
            key: "Cache-Control",
            value: "private, no-store, no-cache, must-revalidate, max-age=0",
          },
        ],
      },
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
      {
        source: "/manifest.json",
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
          { key: "Content-Type", value: "application/manifest+json" },
        ],
      },
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
    ];
  },
};

export default nextConfig;
