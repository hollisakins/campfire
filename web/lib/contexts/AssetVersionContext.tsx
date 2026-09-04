'use client';

// Client half of lib/asset-version.ts: hands the per-field imaging asset
// version to client components that build cutout URLs (`TileThumbnail`), so a
// re-deployed field's thumbnails change URL and stop hitting a week-old
// browser-cache entry. Provided by app/nirspec/layout.tsx; outside a provider
// the hook returns undefined and callers simply omit `v=`.

import React, { createContext, useContext } from 'react';
import type { AssetVersions } from '@/lib/asset-version';

const AssetVersionContext = createContext<AssetVersions | null>(null);

export const AssetVersionProvider: React.FC<{
  versions: AssetVersions;
  children: React.ReactNode;
}> = ({ versions, children }) => (
  <AssetVersionContext.Provider value={versions}>{children}</AssetVersionContext.Provider>
);

/**
 * The asset version token for `field` (or the global token when the field is
 * unknown / not versioned). `undefined` outside a provider or when versions
 * could not be resolved server-side.
 */
export function useAssetVersion(field?: string | null): string | undefined {
  const versions = useContext(AssetVersionContext);
  if (!versions) return undefined;
  return (field && versions.byField[field]) || versions.global || undefined;
}
