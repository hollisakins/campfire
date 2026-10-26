'use client';

import React, { useLayoutEffect, useState } from 'react';
import { createPortal } from 'react-dom';

interface PopoverProps {
  anchorRef: React.RefObject<HTMLElement | null>;
  open: boolean;
  className?: string;
  children: React.ReactNode;
}

/**
 * A chip's dropdown rendered through a portal at a fixed position below (or
 * above) its anchor, so a scrolling or overflow-clipped container such as
 * the Advanced modal never cuts it off. Click-outside handlers must treat
 * `[data-popover]` descendants as inside (see `isInsidePopover`).
 */
export function Popover({ anchorRef, open, className = '', children }: PopoverProps) {
  const [style, setStyle] = useState<React.CSSProperties | null>(null);

  useLayoutEffect(() => {
    if (!open) { setStyle(null); return; }
    const place = () => {
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight;
      const vw = window.innerWidth;
      const below = vh - r.bottom - 8;
      const above = r.top - 8;
      const placeAbove = below < 280 && above > below;
      setStyle({
        position: 'fixed',
        zIndex: 10001,
        left: Math.max(8, Math.min(r.left, vw - 8 - 360)),
        ...(placeAbove ? { bottom: vh - r.top + 4 } : { top: r.bottom + 4 }),
        maxHeight: Math.max(160, (placeAbove ? above : below) - 4),
        overflowY: 'auto',
      });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open, anchorRef]);

  if (!open || !style || typeof document === 'undefined') return null;
  return createPortal(
    <div data-popover style={style} className={className}>
      {children}
    </div>,
    document.body,
  );
}

/** True when a pointer event landed inside the anchor or a portaled popover. */
export function isInsidePopover(target: EventTarget | null, anchor: HTMLElement | null): boolean {
  if (!(target instanceof Node)) return false;
  if (anchor && anchor.contains(target)) return true;
  return target instanceof Element ? target.closest('[data-popover]') !== null : (target.parentElement?.closest('[data-popover]') ?? null) !== null;
}
