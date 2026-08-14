'use client';

import { usePathname } from 'next/navigation';
import { Footer } from '@/components/layout/Footer';

// The map viewer fills the viewport (100vh minus the nav), so the footer would
// only push it into a scroll. Hide the footer on the map route.
//
// /s/inactive is the share-link dead end (docs/design-public-mirror.md §7.1):
// it stands alone, with no nav and no footer, so a visitor whose link was
// revoked is not handed a set of links that all render empty.
export function ConditionalFooter() {
  const pathname = usePathname();

  if (pathname === '/map' || pathname === '/s/inactive') return null;

  return <Footer />;
}
