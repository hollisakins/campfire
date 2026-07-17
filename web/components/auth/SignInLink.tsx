'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState, type ReactNode } from 'react';

/**
 * A "Sign In" link that remembers the current page so the user is returned
 * there after authenticating. The current path *and query string* are passed
 * to `/login` as a `redirect` query param, which `LoginForm` honors once the
 * user is signed in — so shared deep-link state (catalog filters, map
 * coordinates/zoom, etc.) survives the login round-trip.
 *
 * On the home page and the auth pages themselves a round-trip back would be
 * pointless, so those fall back to a bare `/login` (which lands on the home
 * page after login).
 *
 * The query string is read from `window.location` after mount rather than via
 * `useSearchParams()` so that statically-rendered pages hosting this link are
 * not forced into client-side rendering.
 */
function buildHref(target: string): string {
  const pathname = target.split('?')[0];
  const skipRedirect =
    !pathname ||
    pathname === '/' ||
    pathname.startsWith('/login') ||
    pathname.startsWith('/signup') ||
    pathname.startsWith('/welcome');

  return skipRedirect ? '/login' : `/login?redirect=${encodeURIComponent(target)}`;
}

export function SignInLink({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  const pathname = usePathname();

  // Initialize with the pathname alone so the server-rendered href matches the
  // first client render (no hydration mismatch), then fold in the live query
  // string once mounted.
  const [target, setTarget] = useState(pathname ?? '/');

  useEffect(() => {
    setTarget(window.location.pathname + window.location.search);
  }, [pathname]);

  return (
    <Link href={buildHref(target)} className={className}>
      {children}
    </Link>
  );
}
