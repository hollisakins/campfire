'use client';

import React, { useState } from 'react';

interface RGBThumbnailProps {
  objectId: string;
  size?: number;
}

/**
 * Displays a small RGB image thumbnail for quick visual identification.
 * Fetches image via /api/rgb-thumbnail endpoint which redirects to R2.
 */
export const RGBThumbnail: React.FC<RGBThumbnailProps> = ({
  objectId,
  size = 32,
}) => {
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  // Placeholder for error states
  if (hasError) {
    return (
      <div
        className="bg-gray-200 dark:bg-slate-700 rounded flex items-center justify-center"
        style={{ width: size, height: size }}
      >
        <span className="text-gray-400 dark:text-slate-500 text-xs">N/A</span>
      </div>
    );
  }

  return (
    <div
      className="relative rounded overflow-hidden border border-gray-200 dark:border-slate-600"
      style={{ width: size, height: size }}
    >
      {isLoading && (
        <div
          className="absolute inset-0 bg-gray-200 dark:bg-slate-700 animate-pulse"
          style={{ width: size, height: size }}
        />
      )}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={`/api/rgb-thumbnail?object_id=${encodeURIComponent(objectId)}`}
        alt={`RGB cutout for ${objectId}`}
        width={size}
        height={size}
        loading="lazy"
        onLoad={() => setIsLoading(false)}
        onError={() => {
          setIsLoading(false);
          setHasError(true);
        }}
        className={`object-cover ${isLoading ? 'opacity-0' : 'opacity-100'}`}
        style={{ width: size, height: size, transition: 'opacity 0.2s' }}
      />
    </div>
  );
};
