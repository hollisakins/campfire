import { getAssetVersions } from '@/lib/asset-version';
import { AssetVersionProvider } from '@/lib/contexts/AssetVersionContext';

/**
 * NIRSpec section layout: resolves the per-field imaging asset versions once
 * per request (memoized five minutes in the data cache) and hands them to the
 * cutout thumbnails below, which append `v=` to their `/api/tile-thumbnail`
 * URLs so a re-deployed field's imagery is not served from a week-old
 * browser-cache entry (#497).
 */
export default async function NirspecLayout({ children }: { children: React.ReactNode }) {
  const versions = await getAssetVersions();
  return <AssetVersionProvider versions={versions}>{children}</AssetVersionProvider>;
}
