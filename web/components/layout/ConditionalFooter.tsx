'use client';

import { usePathname } from 'next/navigation';
import { Footer } from '@/components/layout/Footer';

// The map viewer fills the viewport (100vh minus the nav), so the footer would
// only push it into a scroll. Hide the footer on the map route.
export function ConditionalFooter() {
  const pathname = usePathname();

  if (pathname === '/map') return null;

  return <Footer />;
}
