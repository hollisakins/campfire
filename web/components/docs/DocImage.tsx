'use client';

import React, { useEffect, useState } from 'react';

/**
 * A docs screenshot: click to open in a lightbox (ESC / click closes). The
 * only interactive piece of the markdown output, kept as its own client
 * component so the rest of the document can render on the server.
 */
export function DocImage({ src, alt }: { src?: string; alt?: string }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open]);

  if (!src) return null;

  return (
    <>
      <figure className="my-6">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt || ''}
          className="rounded-lg border border-border shadow-sm max-w-full cursor-pointer hover:opacity-90 transition-opacity"
          onClick={() => setOpen(true)}
        />
        {alt && (
          <figcaption className="mt-2 text-center text-sm text-text-secondary italic">
            {alt}
          </figcaption>
        )}
      </figure>

      {open && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Image preview"
          className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade-in"
          onClick={() => setOpen(false)}
        >
          <div className="absolute inset-0 bg-black/80 backdrop-blur-sm" />
          <div
            className="relative z-10 flex flex-col items-center animate-zoom-in"
            onClick={(e) => e.stopPropagation()}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={src}
              alt={alt || ''}
              className="max-w-[90vw] max-h-[85vh] object-contain rounded-lg shadow-2xl"
            />
            {alt && (
              <p className="mt-4 text-white/90 text-center text-sm max-w-2xl">
                {alt}
              </p>
            )}
            <button
              onClick={() => setOpen(false)}
              className="absolute -top-2 -right-2 w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white transition-colors"
              aria-label="Close preview"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}
    </>
  );
}
