'use client';

import React from 'react';
import Link from 'next/link';
import { withDr } from './tag';

/**
 * The release being browsed, as the SERVER decided it (P6c-1): the root
 * layout reads the header middleware stamped from `?dr=` and provides it
 * here. Client code never derives the mode from the URL, so the banner,
 * the read-only switches, the query keys and the links all agree with the
 * data the server actually read. Null = the live catalog.
 */
const DrContext = React.createContext<string | null>(null);

export function DrProvider({ tag, children }: { tag: string | null; children: React.ReactNode }) {
  return <DrContext.Provider value={tag}>{children}</DrContext.Provider>;
}

export function useDrTag(): string | null {
  return React.useContext(DrContext);
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
