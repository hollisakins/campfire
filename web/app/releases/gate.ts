import 'server-only';

import { notFound } from 'next/navigation';
import { getRequestPrincipal, type RequestPrincipal } from '@/lib/auth/identity';
import { drPagesAccess, drPagesMode } from '@/lib/dr-pages';

/**
 * The NEXT_PUBLIC_DR_PAGES gate shared by the /releases pages: the viewer's
 * principal when the pages show, null when a sign-in prompt should render,
 * and a 404 otherwise (flag off, admin mode for a non-admin, link accounts).
 */
export async function releasePagesPrincipal(): Promise<RequestPrincipal | null> {
  const principal = await getRequestPrincipal();
  const access = drPagesAccess(drPagesMode(), {
    signedIn: principal !== null,
    isAdmin: principal?.access.isAdmin ?? false,
    isLinkAccount: principal?.access.isLinkAccount ?? false,
  });
  if (access === 'not-found') notFound();
  return access === 'show' ? principal : null;
}
