'use client';

import React, { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { TileThumbnail } from './TileThumbnail';

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
}) => {
  const [showShutters, setShowShutters] = useState(true);

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
      />
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
    </div>
  );
};
