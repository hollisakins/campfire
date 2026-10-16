import { assertReleaseViewer } from '@/lib/dr/gate';

/**
 * NIRCam section layout: the pages below render client-side, so the
 * release gate runs here (P6c-1) — `?dr=` names an unknown release, or the
 * viewer is not admitted by NEXT_PUBLIC_DR_PAGES → 404 before any HTML.
 */
export default async function NircamLayout({ children }: { children: React.ReactNode }) {
  await assertReleaseViewer();
  return children;
}
