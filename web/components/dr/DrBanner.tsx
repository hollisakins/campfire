'use client';

import React, { Suspense } from 'react';
import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { Archive } from 'lucide-react';
import { DR_PARAM, parseDrTag, withoutDr } from '@/lib/dr/tag';

/**
 * The release-mode strip under the navigation (P6c-1): the tag, a link to
 * its release page and the way back to the live catalog. Renders nothing
 * while browsing live. Pure function of the URL, so it needs no data.
 */
export function DrBannerContent() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const tag = parseDrTag(searchParams?.get(DR_PARAM));
  if (!tag) return null;
  const qs = searchParams?.toString() ?? '';
  const liveHref = withoutDr(`${pathname}${qs ? `?${qs}` : ''}`);
  return (
    <div data-slot="dr-banner" className="border-b border-primary/30 bg-primary/10">
      <div className="container mx-auto flex items-center gap-3 px-4 py-1.5 text-sm">
        <Archive className="h-4 w-4 text-primary" />
        <Link href={`/releases/${encodeURIComponent(tag)}`} className="font-mono font-semibold text-text-primary hover:underline">
          {tag}
        </Link>
        <Link href={liveHref} className="ml-auto text-text-secondary hover:text-text-primary hover:underline">
          Live catalog
        </Link>
      </div>
    </div>
  );
}

export function DrBanner() {
  return (
    <Suspense fallback={null}>
      <DrBannerContent />
    </Suspense>
  );
}
