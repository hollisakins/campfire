'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { getNearbyShutters } from '@/lib/actions/map';
import { computeShutterRects, type ShutterGeometry } from '@/lib/utils/shutter-overlay';

interface TileThumbnailProps {
  targetId: string;
  size?: number;
  /** CSS display size in px. Defaults to `size`. Use a smaller value than `size` for higher-resolution rendering. */
  displaySize?: number;
  shutters?: boolean;
  fov?: number;
  /** Required when shutters=true: object coordinates for shutter geometry lookup */
  ra?: number;
  dec?: number;
  field?: string;
  linkToMap?: {
    field: string;
    ra: number;
    dec: number;
  };
  className?: string;
}

/**
 * Displays a tile-composited thumbnail for a NIRSpec object.
 * When shutters=true and coordinates are provided, renders a client-side
 * SVG overlay with vector shutter rectangles.
 */
export const TileThumbnail: React.FC<TileThumbnailProps> = ({
  targetId,
  size = 48,
  displaySize,
  shutters = false,
  fov = 5,
  ra,
  dec,
  field,
  linkToMap,
  className,
}) => {
  const cssSize = displaySize ?? size;
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [shutterRects, setShutterRects] = useState<ReturnType<typeof computeShutterRects>>([]);

  // Cutout image URL (never includes shutters — always a clean RGB crop)
  const src = `/api/tile-thumbnail?target_id=${encodeURIComponent(targetId)}&size=${size}&fov=${fov}`;

  // Fetch shutter geometry when coordinates are provided (independent of visibility toggle).
  // This way toggling shutters on/off is instant CSS — no refetch needed.
  const hasCoordinates = ra !== undefined && dec !== undefined && field !== undefined;
  useEffect(() => {
    if (!hasCoordinates) {
      setShutterRects([]);
      return;
    }

    let cancelled = false;
    getNearbyShutters(ra, dec, field, fov).then(({ shutters: shutterData }) => {
      if (cancelled) return;
      const rects = computeShutterRects(
        shutterData as ShutterGeometry[],
        ra, dec, fov, cssSize, targetId,
      );
      setShutterRects(rects);
    });

    return () => { cancelled = true; };
  }, [ra, dec, field, fov, cssSize, targetId, hasCoordinates]);

  if (hasError) {
    const placeholder = (
      <div
        className={`bg-gray-200 dark:bg-slate-700 rounded flex items-center justify-center ${className || ''}`}
        style={{ width: cssSize, height: cssSize }}
      >
        <span className="text-gray-400 dark:text-slate-500 text-xs">N/A</span>
      </div>
    );
    return linkToMap ? (
      <Link
        href={`/map?field=${encodeURIComponent(linkToMap.field)}&ra=${linkToMap.ra}&dec=${linkToMap.dec}&z=8&highlight=${encodeURIComponent(targetId)}`}
        title="View on map"
      >
        {placeholder}
      </Link>
    ) : placeholder;
  }

  const img = (
    <div
      className={`relative rounded overflow-hidden border border-gray-200 dark:border-slate-600 ${
        linkToMap ? 'hover:border-primary dark:hover:border-primary transition-colors' : ''
      } ${className || ''}`}
      style={{ width: cssSize, height: cssSize }}
    >
      {isLoading && (
        <div
          className="absolute inset-0 bg-gray-200 dark:bg-slate-700 animate-pulse"
          style={{ width: cssSize, height: cssSize }}
        />
      )}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={`Tile cutout for ${targetId}`}
        width={cssSize}
        height={cssSize}
        loading="lazy"
        onLoad={() => setIsLoading(false)}
        onError={() => {
          setIsLoading(false);
          setHasError(true);
        }}
        className={`object-cover ${isLoading ? 'opacity-0' : 'opacity-100'}`}
        style={{ width: cssSize, height: cssSize, imageRendering: 'auto', transition: 'opacity 0.2s' }}
      />
      {/* Vector shutter overlay */}
      {shutters && shutterRects.length > 0 && (
        <svg
          width={cssSize}
          height={cssSize}
          className="absolute inset-0 pointer-events-none"
          style={{ width: cssSize, height: cssSize }}
        >
          {shutterRects.map((rect, i) => (
            <rect
              key={i}
              x={-rect.width / 2}
              y={-rect.height / 2}
              width={rect.width}
              height={rect.height}
              fill={rect.fill}
              fillOpacity={rect.fillOpacity}
              stroke={rect.stroke}
              strokeOpacity={rect.strokeOpacity}
              strokeWidth={rect.strokeWidth}
              strokeDasharray={rect.strokeDasharray}
              transform={`translate(${rect.x},${rect.y}) rotate(${rect.rotation})`}
            />
          ))}
        </svg>
      )}
    </div>
  );

  if (linkToMap) {
    return (
      <Link
        href={`/map?field=${encodeURIComponent(linkToMap.field)}&ra=${linkToMap.ra}&dec=${linkToMap.dec}&z=8&highlight=${encodeURIComponent(targetId)}`}
        className="block"
        title="View on map"
      >
        {img}
      </Link>
    );
  }

  return img;
};
