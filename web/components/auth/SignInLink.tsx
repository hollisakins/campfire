'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

/**
 * A "Sign In" link that remembers the current page so the user is returned
 * there after authenticating. The current path is passed to `/login` as a
 * `redirect` query param, which `LoginForm` honors once the user is signed in.
 *
 * On the home page and the auth pages themselves a round-trip back would be
 * pointless, so those fall back to a bare `/login` (which lands on the home
 * page after login).
 */
export function SignInLink({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  const pathname = usePathname();

  const skipRedirect =
    !pathname ||
    pathname === '/' ||
    pathname.startsWith('/login') ||
    pathname.startsWith('/signup') ||
    pathname.startsWith('/welcome');

  const href = skipRedirect
    ? '/login'
    : `/login?redirect=${encodeURIComponent(pathname)}`;

  return (
    <Link href={href} className={className}>
      {children}
    </Link>
  );
}
