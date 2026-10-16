/**
 * The data-release pages flag (P6, docs/design-data-releases-impl-plan.md §8).
 *
 * `NEXT_PUBLIC_DR_PAGES`:
 *   unset / anything else  pages 404 for everyone, no nav entry
 *   admin                  admins only (the rehearsal week)
 *   all                    every signed-in, non-link account
 *
 * Next inlines NEXT_PUBLIC_* at build time, so the nav entry and the pages
 * agree within one deployment. No directive: importable from both sides.
 */
export type DrPagesMode = 'off' | 'admin' | 'all';

export function parseDrPagesMode(value: string | undefined | null): DrPagesMode {
  const v = (value ?? '').trim().toLowerCase();
  return v === 'admin' || v === 'all' ? v : 'off';
}

export function drPagesMode(): DrPagesMode {
  return parseDrPagesMode(process.env.NEXT_PUBLIC_DR_PAGES);
}

export interface DrPagesViewer {
  signedIn: boolean;
  isAdmin: boolean;
  isLinkAccount: boolean;
}

/**
 * What a viewer gets: the pages, a sign-in prompt, or a 404. Share-link
 * accounts never see releases (the dr_releases policy refuses them too).
 */
export function drPagesAccess(
  mode: DrPagesMode,
  viewer: DrPagesViewer,
): 'show' | 'sign-in' | 'not-found' {
  if (mode === 'off' || viewer.isLinkAccount) return 'not-found';
  if (mode === 'admin') return viewer.signedIn && viewer.isAdmin ? 'show' : 'not-found';
  return viewer.signedIn ? 'show' : 'sign-in';
}

/** Whether the nav offers a Releases link to this viewer. */
export function drPagesInNav(mode: DrPagesMode, viewer: DrPagesViewer): boolean {
  return drPagesAccess(mode, viewer) === 'show';
}
