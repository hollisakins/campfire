'use client';

// Client half of lib/asset-version.ts: hands the global imaging asset version
// to client components that build cutout URLs (`TileThumbnail`), so a
// re-deployed field's thumbnails change URL and stop hitting a week-old
// browser-cache entry. Only the global token crosses to the client — the
// per-field roster names every field in the DB, draft-backed ones included,
// and stays on the server. Provided by app/nirspec/layout.tsx; outside a
// provider the hook returns undefined and callers simply omit `v=`.

import React, { createContext, useContext } from 'react';
import type { ClientAssetVersion } from '@/lib/asset-version';

const AssetVersionContext = createContext<ClientAssetVersion | null>(null);

export const AssetVersionProvider: React.FC<{
  version: ClientAssetVersion;
  children: React.ReactNode;
}> = ({ version, children }) => (
  <AssetVersionContext.Provider value={version}>{children}</AssetVersionContext.Provider>
);

/**
 * The global asset version token. The `field` argument is accepted so call
 * sites read naturally and can regain per-field precision later without
 * changing; today every field shares the one token. `undefined` outside a
 * provider or when versions could not be resolved server-side.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function useAssetVersion(field?: string | null): string | undefined {
  const version = useContext(AssetVersionContext);
  return version?.global || undefined;
}
