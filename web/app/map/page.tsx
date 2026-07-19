import { SignInLink } from '@/components/auth/SignInLink';
import { LogIn } from 'lucide-react';
import { getMapLayers, getFitsglDatasets } from '@/lib/actions/map';
import { MapPageContent } from './MapPageContent';

export const dynamic = 'force-dynamic';

interface MapPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export default async function MapPage({ searchParams }: MapPageProps) {
  const params = await searchParams;

  // Parse map-specific query params
  const field = typeof params.field === 'string' ? params.field : undefined;
  const filter = typeof params.filter === 'string' ? params.filter : undefined;
  const ra = typeof params.ra === 'string' ? parseFloat(params.ra) : undefined;
  const dec = typeof params.dec === 'string' ? parseFloat(params.dec) : undefined;
  const zoomRaw = typeof params.z === 'string' ? params.z : typeof params.zoom === 'string' ? params.zoom : undefined;
  // parseFloat (not parseInt): the FitsGL map writes fractional zoom (e.g. 0.43),
  // and Leaflet's integer levels parse identically as floats.
  const zoomParsed = zoomRaw !== undefined ? parseFloat(zoomRaw) : undefined;
  const zoom = zoomParsed !== undefined && Number.isFinite(zoomParsed) ? zoomParsed : undefined;
  const highlight = typeof params.highlight === 'string' ? params.highlight : undefined;

  const initialCenter = ra !== undefined && dec !== undefined ? { ra, dec } : undefined;

  // Fetch available map layers + FitsGL datasets (the map prefers FitsGL for a
  // field that has one; RLS keeps draft-backed datasets admin-only).
  const [{ layers, isAuthenticated }, fitsglDatasets] = await Promise.all([
    getMapLayers(),
    getFitsglDatasets(),
  ]);

  if (!isAuthenticated) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view the map
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Access to the image map viewer requires authentication.
          </p>
          <SignInLink
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </SignInLink>
        </div>
      </div>
    );
  }

  return (
    <MapPageContent
      layers={layers}
      fitsglDatasets={fitsglDatasets}
      initialField={field}
      initialFilter={filter}
      initialCenter={initialCenter}
      initialZoom={zoom}
      highlightObjectId={highlight}
    />
  );
}
