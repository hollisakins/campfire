'use client';

import dynamic from 'next/dynamic';
import type { MapLayer, Shutter } from '@/lib/actions/map';

// Dynamic import to avoid SSR issues with Leaflet (requires window/document)
const TileCutout = dynamic(
  () => import('@/components/spectra/TileCutout').then(mod => ({ default: mod.TileCutout })),
  {
    ssr: false,
    loading: () => (
      <div className="bg-gray-200 dark:bg-slate-700 rounded-lg flex items-center justify-center animate-pulse"
           style={{ width: 300, height: 300 }} />
    ),
  }
);

interface TileCutoutWrapperProps {
  objectId: string;
  ra: number;
  dec: number;
  field: string;
  mapLayer: MapLayer | null;
  shutters: Shutter[];
  size?: number;
}

export function TileCutoutWrapper(props: TileCutoutWrapperProps) {
  return <TileCutout {...props} />;
}
