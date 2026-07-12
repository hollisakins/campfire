'use client';

import React, { useEffect } from 'react';
import { X } from 'lucide-react';

interface ThumbnailPopupProps {
  /** Presigned thumbnail URL to show enlarged; null = closed. */
  url: string | null;
  /** Caption, e.g. "F444W · A1 · sci". */
  title: string;
  onClose: () => void;
}

/** Click-to-enlarge modal for a mosaic thumbnail (Escape / backdrop closes). */
export const ThumbnailPopup: React.FC<ThumbnailPopupProps> = ({
  url,
  title,
  onClose,
}) => {
  useEffect(() => {
    if (!url) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [url, onClose]);

  if (!url) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-card border border-border-strong rounded-xl p-3.5 w-fit max-w-[min(64rem,92vw)]">
        <div className="flex items-center justify-between mb-2.5">
          <span className="font-mono text-[13px] text-text-primary">{title}</span>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-md text-text-tertiary hover:text-text-primary hover:bg-surface-2 transition-colors"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        {/* Presigned cross-origin PNG; plain <img> (see NircamFieldCard). */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={url}
          alt={title}
          className="block max-w-full max-h-[82vh] rounded-lg bg-[#0d0b12]"
        />
      </div>
    </div>
  );
};
