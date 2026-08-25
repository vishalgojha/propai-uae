// Build-time base path so the broker dashboard can also be served under a
// path prefix (e.g. https://ae.propai.live/broker) behind a reverse proxy.
// Set NEXT_PUBLIC_BASE_PATH at build time; leave unset when served at a
// domain root (current app.propai.live deployment).
//
// next/link, next/navigation and rewrite sources pick up basePath from
// next.config automatically. Everything below covers what Next does NOT
// rewrite for us: raw fetch() calls, plain <a>/<img> tags, auth redirect
// URLs and injected scripts.
export const BASE_PATH: string = process.env.NEXT_PUBLIC_BASE_PATH || "";

export function withBasePath(path: string): string {
  return `${BASE_PATH}${path}`;
}

// Browser-absolute URL (origin + basePath + path) for auth redirect
// allowlists such as Supabase emailRedirectTo.
export function absoluteUrl(path: string): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  return `${origin}${BASE_PATH}${path.startsWith("/") ? path : `/${path}`}`;
}

export function apiUrl(path: string): string {
  return `${BASE_PATH}/api${path.startsWith("/") ? path : `/${path}`}`;
}
