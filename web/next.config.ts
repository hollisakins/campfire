import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  webpack: (config) => {
    // Handle raw markdown file imports
    config.module.rules.push({
      test: /\.md$/,
      type: 'asset/source',
    });
    return config;
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
