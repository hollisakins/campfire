'use client';

import dynamic from 'next/dynamic';
import type { MapLayer, MapObjectMarker, FitsglDataset } from '@/lib/actions/map';

// Dynamic import to avoid SSR issues with Leaflet (requires window/document).
// The chunk group (Leaflet + FitsGL, ~100 kB br) starts downloading at module
// evaluation rather than on first render (perf T1-6 / #502): this wrapper is
// only in the /map route's first-load JS, so the request goes out while the
// page is still hydrating and the marker query (kicked off in MapPageContent)
// runs in parallel with it.
let mapViewerPromise: Promise<{ default: typeof import('@/components/map/MapViewer').MapViewer }> | null = null;
function loadMapViewer() {
  if (!mapViewerPromise) {
    mapViewerPromise = import('@/components/map/MapViewer').then(mod => ({ default: mod.MapViewer }));
  }
  return mapViewerPromise;
}
if (typeof window !== 'undefined') void loadMapViewer();

const MapViewer = dynamic(
  loadMapViewer,
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-full bg-surface-2">
        <div className="text-text-secondary">Loading map...</div>
      </div>
    ),
  }
);

interface MapViewerWrapperProps {
  layers: MapLayer[];
  fitsglDatasets: FitsglDataset[];
  initialField?: string;
  initialFilter?: string;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
  markerFilter?: (marker: MapObjectMarker) => boolean;
  filteredIdSet?: Set<string> | null;
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
  onFieldChange?: (field: string) => void;
}

export function MapViewerWrapper(props: MapViewerWrapperProps) {
  return <MapViewer {...props} />;
}
