'use client';

import React, { useState } from 'react';
import { Eye, EyeOff, Download } from 'lucide-react';
import { TileThumbnail } from './TileThumbnail';
import { generateShutterRegion } from '@/lib/actions/shutter-region';

interface TileThumbnailWithToggleProps {
  targetId: string;
  size?: number;
  displaySize?: number;
  fov?: number;
  ra: number;
  dec: number;
  field: string;
  linkToMap?: {
    field: string;
    ra: number;
    dec: number;
  };
  /** Map of target_id → hex color for multi-target shutter coloring */
  memberColors?: Record<string, string>;
}

/**
 * TileThumbnail with a toggle button to show/hide shutter overlays.
 * Toggle is instant (CSS visibility) — no image refetch needed.
 */
export const TileThumbnailWithToggle: React.FC<TileThumbnailWithToggleProps> = ({
  targetId,
  size = 600,
  displaySize = 300,
  fov = 3.2,
  ra,
  dec,
  field,
  linkToMap,
  memberColors,
}) => {
  const [showShutters, setShowShutters] = useState(true);
  const [downloading, setDownloading] = useState(false);

  const handleDownloadRegion = async () => {
    if (downloading) return;
    setDownloading(true);
    try {
      const result = await generateShutterRegion(targetId, fov);
      if (result.error || !result.text || !result.filename) {
        console.error('Region download failed:', result.error);
        return;
      }
      const blob = new Blob([result.text], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = result.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="space-y-2">
      <TileThumbnail
        targetId={targetId}
        size={size}
        displaySize={displaySize}
        fov={fov}
        shutters={showShutters}
        ra={ra}
        dec={dec}
        field={field}
        linkToMap={linkToMap}
        memberColors={memberColors}
      />
      <div className="flex items-center gap-3">
        <button
          onClick={() => setShowShutters((prev) => !prev)}
          className="flex items-center gap-1.5 text-xs text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
        >
          {showShutters ? (
            <EyeOff className="w-3.5 h-3.5" />
          ) : (
            <Eye className="w-3.5 h-3.5" />
          )}
          {showShutters ? 'Hide shutters' : 'Show shutters'}
        </button>
        <button
          onClick={handleDownloadRegion}
          disabled={downloading}
          title="Download DS9 region file (.reg)"
          className="flex items-center gap-1.5 text-xs text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors disabled:opacity-50"
        >
          <Download className="w-3.5 h-3.5" />
          {downloading ? 'Preparing...' : '.reg'}
        </button>
      </div>
    </div>
  );
};
