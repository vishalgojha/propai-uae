/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  poweredByHeader: false,
  // WWW is a fully separate app from app.propai.live
  // Static export for SSG/ISR of locality and building pages
  experimental: {
    optimizePackageImports: ['lucide-react'],
  },
  // Preserve URLs that were indexed before the public site standardized on
  // `/localities`. Google and old broker shares still use `/locations`.
  async redirects() {
    return [
      {
        source: '/locations/:slug/:segment',
        destination: '/localities/:slug/:segment',
        permanent: true,
      },
      {
        source: '/locations/:slug',
        destination: '/localities/:slug',
        permanent: true,
      },
    ]
  },
  // Proxy /broker/* to the internal broker dashboard container.
  // The broker app is a separate Coolify deployment built with
  // NEXT_PUBLIC_BASE_PATH=/broker, so it expects the /broker prefix.
  async rewrites() {
    return [
      {
        source: '/broker/:path*',
        destination: 'http://muicb57133jqi2aqk5sxsi1m:3000/broker/:path*',
      },
    ]
  },
  async headers() {
    return [
      {
        source: '/sw.js',
        headers: [
          { key: 'Cache-Control', value: 'no-cache, must-revalidate' },
          { key: 'Service-Worker-Allowed', value: '/' },
        ],
      },
      {
        source: '/manifest.webmanifest',
        headers: [
          { key: 'Content-Type', value: 'application/manifest+json' },
          { key: 'Cache-Control', value: 'public, max-age=3600, must-revalidate' },
        ],
      },
    ]
  },
}

module.exports = nextConfig
