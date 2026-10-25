import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Self-contained server bundle for the Docker image (web/Dockerfile,
  // ops/README.md). Vercel ignores this setting and keeps building its own
  // output, so it is safe while both deployments are live.
  output: 'standalone',
  // Nothing uses Next's image optimizer (RGBImage passes `unoptimized`, other
  // previews are plain <img>), so switch it off: /_next/image then answers
  // 404 instead of fetching and decoding arbitrary same-origin images with
  // sharp, which is the attack surface of several Next.js advisories.
  images: { unoptimized: true },
  // Ensure the Updates markdown is bundled into the serverless functions for
  // these routes, which render dynamically once a gated update is published.
  outputFileTracingIncludes: {
    '/': ['./lib/updates/content/**/*'],
    '/updates': ['./lib/updates/content/**/*'],
  },
  webpack: (config) => {
    // Handle raw markdown file imports
    config.module.rules.push({
      test: /\.md$/,
      type: 'asset/source',
    });
    return config;
  },
  // No CAMPFIRE page is ever indexed by a search engine (design-public-mirror.md
  // §9). Deliberately a header on EVERY response rather than a robots.txt
  // Disallow: Disallow blocks crawling, and a page that is never crawled is a
  // page whose noindex is never read -- so a URL discovered from an external
  // link can still be indexed bare. That is exactly the accidental-paste case
  // share links must survive, so we let crawlers fetch and tell them noindex.
  // The root-layout `robots` metadata is the HTML-level belt to this brace.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [{ key: 'X-Robots-Tag', value: 'noindex, nofollow' }],
      },
    ];
  },
  async redirects() {
    return [
      {
        source: '/nirspec/programs',
        destination: '/nirspec/metadata',
        permanent: false,
      },
      {
        source: '/nirspec/programs/:slug',
        destination: '/nirspec/metadata/programs/:slug',
        permanent: false,
      },
    ];
  },
};

export default nextConfig;
