'use client';

import React, { useState } from 'react';
import { usePreferences } from '@/lib/contexts/PreferencesContext';

interface SpectrumThumbnailProps {
  objectId: string;
  width?: number;
  height?: number;
}

/**
 * Displays a small spectrum sparkline thumbnail for quick visual inspection.
 * Fetches SVG from /api/spectrum-thumbnail endpoint.
 * Uses user's accent color and flux unit preference.
 */
export const SpectrumThumbnail: React.FC<SpectrumThumbnailProps> = ({
  objectId,
  width = 120,
  height = 32,
}) => {
  const { accentColorHex, spectrumPreferences } = usePreferences();
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  // Build URL with flux unit preference
  const thumbnailUrl = `/api/spectrum-thumbnail?object_id=${encodeURIComponent(objectId)}&flux_unit=${spectrumPreferences.fluxUnit}`;

  // Placeholder for loading/error states
  if (hasError) {
    return (
      <div
        className="bg-gray-100 dark:bg-slate-700 rounded flex items-center justify-center"
        style={{ width, height, color: accentColorHex }}
      >
        <span className="text-gray-400 dark:text-slate-500 text-xs">--</span>
      </div>
    );
  }

  return (
    <div
      className="relative rounded overflow-hidden bg-gray-100 dark:bg-slate-700"
      style={{ width, height, color: accentColorHex }}
    >
      {isLoading && (
        <div
          className="absolute inset-0 bg-gray-100 dark:bg-slate-700 animate-pulse"
          style={{ width, height }}
        />
      )}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={thumbnailUrl}
        alt={`Spectrum for ${objectId}`}
        width={width}
        height={height}
        loading="lazy"
        onLoad={() => setIsLoading(false)}
        onError={() => {
          setIsLoading(false);
          setHasError(true);
        }}
        className={isLoading ? 'opacity-0' : 'opacity-100'}
        style={{ transition: 'opacity 0.2s' }}
      />
    </div>
  );
};
