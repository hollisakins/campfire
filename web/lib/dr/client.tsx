'use client';

import React from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { DR_PARAM, parseDrTag, withDr } from './tag';

/**
 * The release the page is browsing (`?dr=<tag>`), or null for the live
 * catalog. Client components read it here; server components use
 * `drTagFromSearchParams` / `requestDrTag`. Callers must sit under a
 * Suspense boundary (useSearchParams).
 */
export function useDrTag(): string | null {
  const sp = useSearchParams();
  return parseDrTag(sp?.get(DR_PARAM));
}

/** `href => href carrying the current release`. */
export function useWithDr(): (href: string) => string {
  const tag = useDrTag();
  return React.useCallback((href: string) => withDr(href, tag), [tag]);
}

type DrLinkProps = React.ComponentProps<typeof Link> & {
  /** Opt out: the target is live-only (map, lists, inspection). */
  live?: boolean;
};

/**
 * next/link that carries `?dr=` while a release is selected. Catalog links
 * use this; links to live-only surfaces pass `live`.
 */
export const DrLink = React.forwardRef<HTMLAnchorElement, DrLinkProps>(function DrLink(
  { href, live, ...rest },
  ref,
) {
  const tag = useDrTag();
  const target = !live && tag && typeof href === 'string' ? withDr(href, tag) : href;
  return <Link ref={ref} href={target} {...rest} />;
});
