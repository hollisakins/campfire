'use client';

import dynamic from 'next/dynamic';
import type { MapLayer, MapMarker } from '@/lib/actions/map';

// Dynamic import to avoid SSR issues with Leaflet (requires window/document)
const MapViewer = dynamic(
  () => import('@/components/map/MapViewer').then(mod => ({ default: mod.MapViewer })),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-full bg-surface dark:bg-slate-900">
        <div className="text-text-secondary dark:text-slate-400">Loading map...</div>
      </div>
    ),
  }
);

interface MapViewerWrapperProps {
  layers: MapLayer[];
  initialField?: string;
  initialFilter?: string;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
  markerFilter?: (marker: MapMarker) => boolean;
  filteredMarkerCount?: number;
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
}

export function MapViewerWrapper(props: MapViewerWrapperProps) {
  return <MapViewer {...props} />;
}
