import Link from 'next/link';
import { LogIn } from 'lucide-react';
import { getMapLayers } from '@/lib/actions/map';
import { MapViewerWrapper } from '@/components/map/MapViewerWrapper';

interface MapPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export default async function MapPage({ searchParams }: MapPageProps) {
  const params = await searchParams;

  // Parse query params
  const field = typeof params.field === 'string' ? params.field : undefined;
  const ra = typeof params.ra === 'string' ? parseFloat(params.ra) : undefined;
  const dec = typeof params.dec === 'string' ? parseFloat(params.dec) : undefined;
  const zoom = typeof params.zoom === 'string' ? parseInt(params.zoom, 10) : undefined;
  const highlight = typeof params.highlight === 'string' ? params.highlight : undefined;

  const initialCenter = ra !== undefined && dec !== undefined ? { ra, dec } : undefined;

  // Fetch available map layers
  const { layers, isAuthenticated } = await getMapLayers();

  if (!isAuthenticated) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view the map
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to the image map viewer requires authentication.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-72px)]">
      <MapViewerWrapper
        layers={layers}
        initialField={field}
        initialCenter={initialCenter}
        initialZoom={zoom}
        highlightObjectId={highlight}
      />
    </div>
  );
}
