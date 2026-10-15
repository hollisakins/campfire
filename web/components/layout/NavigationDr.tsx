'use client';

import React, { Suspense } from 'react';
import { Navigation } from './Navigation';
import { useDrTag } from '@/lib/dr/client';

function NavigationWithRelease() {
  return <Navigation drTag={useDrTag()} />;
}

/**
 * The navigation with the release it should carry on its catalog links
 * (P6c-1). The fallback is the same navigation with live links, so a
 * statically prerendered page shows the identical header and swaps the
 * hrefs after hydration.
 */
export function NavigationDr() {
  return (
    <Suspense fallback={<Navigation />}>
      <NavigationWithRelease />
    </Suspense>
  );
}
