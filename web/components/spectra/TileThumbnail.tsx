'use client';

import React, { useState } from 'react';
import Link from 'next/link';

interface TileThumbnailProps {
  objectId: string;
  size?: number;
  /** CSS display size in px. Defaults to `size`. Use a smaller value than `size` for higher-resolution rendering. */
  displaySize?: number;
  shutters?: boolean;
  fov?: number;
  linkToMap?: {
    field: string;
    ra: number;
    dec: number;
  };
  className?: string;
}

/**
 * Displays a tile-composited thumbnail for a NIRSpec object.
 * Replaces both RGBThumbnail (table) and TileCutout (detail page).
 */
export const TileThumbnail: React.FC<TileThumbnailProps> = ({
  objectId,
  size = 48,
  displaySize,
  shutters = false,
  fov = 5,
  linkToMap,
  className,
}) => {
  const cssSize = displaySize ?? size;
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  const src = `/api/tile-thumbnail?object_id=${encodeURIComponent(objectId)}&size=${size}&fov=${fov}${shutters ? '&shutters=true' : ''}`;

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
        href={`/map?field=${encodeURIComponent(linkToMap.field)}&ra=${linkToMap.ra}&dec=${linkToMap.dec}&z=8&highlight=${encodeURIComponent(objectId)}`}
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
        alt={`Tile cutout for ${objectId}`}
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
    </div>
  );

  if (linkToMap) {
    return (
      <Link
        href={`/map?field=${encodeURIComponent(linkToMap.field)}&ra=${linkToMap.ra}&dec=${linkToMap.dec}&z=8&highlight=${encodeURIComponent(objectId)}`}
        className="block"
        title="View on map"
      >
        {img}
      </Link>
    );
  }

  return img;
};
