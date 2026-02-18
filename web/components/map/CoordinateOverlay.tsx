'use client';

import React from 'react';
import { formatRA, formatDec } from '@/lib/utils/wcs';

interface CoordinateOverlayProps {
  coords: { ra: number; dec: number } | null;
}

export function CoordinateOverlay({ coords }: CoordinateOverlayProps) {
  if (!coords) return null;

  return (
    <div className="absolute bottom-4 left-4 z-[1000] bg-black/70 text-white rounded px-3 py-1.5 font-mono text-sm pointer-events-none select-none">
      <span className="text-gray-400 mr-1">RA:</span>
      {formatRA(coords.ra)}
      <span className="text-gray-400 mx-2">Dec:</span>
      {formatDec(coords.dec)}
      <span className="text-gray-500 ml-3 text-xs">
        ({coords.ra.toFixed(5)}, {coords.dec.toFixed(5)})
      </span>
    </div>
  );
}
