import { getAssetVersions, clientAssetVersion } from '@/lib/asset-version';
import { AssetVersionProvider } from '@/lib/contexts/AssetVersionContext';

/**
 * /inspect sits outside the NIRSpec section tree but renders the same cutout
 * thumbnail and shutter overlay (DashboardPanel), so it needs the same asset
 * version token: without it the `/api/tile-thumbnail` and `/api/shutters`
 * URLs carry no `v=` and a deployment has no way to change them
 * (app/nirspec/layout.tsx; #497, #506).
 */
export default async function InspectLayout({ children }: { children: React.ReactNode }) {
  const versions = await getAssetVersions();
  return <AssetVersionProvider version={clientAssetVersion(versions)}>{children}</AssetVersionProvider>;
}
