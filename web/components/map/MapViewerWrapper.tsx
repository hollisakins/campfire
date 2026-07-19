'use client';

import dynamic from 'next/dynamic';
import type { MapLayer, MapObjectMarker, FitsglDataset } from '@/lib/actions/map';

// Dynamic import to avoid SSR issues with Leaflet (requires window/document)
const MapViewer = dynamic(
  () => import('@/components/map/MapViewer').then(mod => ({ default: mod.MapViewer })),
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
