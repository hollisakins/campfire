'use client';

import React, { useMemo } from 'react';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import type { Spectrum } from '@/lib/types';

interface SpectrumThumbnailInlineProps {
  spectra: Spectrum[];
  width?: number;
  height?: number;
}

// Placeholder color used in pre-generated SVGs (matches pipeline/plots.py)
const PLACEHOLDER_COLOR = '#c026d3';

/**
 * Displays a spectrum sparkline thumbnail using pre-fetched SVG data.
 * No API calls - renders directly from the spectra data included in the RPC response.
 * Uses user's accent color and flux unit preference.
 */
export const SpectrumThumbnailInline: React.FC<SpectrumThumbnailInlineProps> = ({
  spectra,
  width = 120,
  height = 32,
}) => {
  const { accentColorHex, spectrumPreferences } = usePreferences();
  const fluxUnit = spectrumPreferences.fluxUnit;

  // Find the first spectrum with a thumbnail (prefer PRISM, then by grating order)
  const thumbnailSvg = useMemo(() => {
    if (!spectra || spectra.length === 0) return null;

    // Sort by grating preference: PRISM first, then alphabetically
    const sortedSpectra = [...spectra].sort((a, b) => {
      if (a.grating === 'PRISM') return -1;
      if (b.grating === 'PRISM') return 1;
      return a.grating.localeCompare(b.grating);
    });

    for (const spectrum of sortedSpectra) {
      const svg = fluxUnit === 'fnu'
        ? spectrum.thumbnail_svg_fnu
        : spectrum.thumbnail_svg_flambda;
      if (svg) return svg;
    }

    // Fallback: try the other flux unit if primary not available
    for (const spectrum of sortedSpectra) {
      const svg = fluxUnit === 'fnu'
        ? spectrum.thumbnail_svg_flambda
        : spectrum.thumbnail_svg_fnu;
      if (svg) return svg;
    }

    return null;
  }, [spectra, fluxUnit]);

  // Apply user's accent color by replacing the placeholder
  const colorizedSvg = useMemo(() => {
    if (!thumbnailSvg) return null;
    // Replace placeholder color with user's accent color (case-insensitive)
    return thumbnailSvg.replace(new RegExp(PLACEHOLDER_COLOR, 'gi'), accentColorHex);
  }, [thumbnailSvg, accentColorHex]);

  // No thumbnail available
  if (!colorizedSvg) {
    return (
      <div
        className="flex items-center justify-center"
        style={{ width, height }}
      >
        <span className="text-gray-400 dark:text-slate-500 text-xs">--</span>
      </div>
    );
  }

  return (
    <div
      className="flex items-center"
      style={{ width, height }}
      dangerouslySetInnerHTML={{ __html: colorizedSvg }}
    />
  );
};
