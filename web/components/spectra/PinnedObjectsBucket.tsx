'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Pin, X } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { MAX_PINNED_OBJECTS, QUALITY_LABELS, type PinnedObject } from '@/lib/types';
import { TileThumbnail } from './TileThumbnail';

// Delay before the panel collapses after the pointer leaves, so the hover
// path from chip to panel is forgiving of small gaps and diagonal movement.
const CLOSE_DELAY_MS = 200;

const detailHref = (pin: PinnedObject) =>
  `/nirspec/${pin.route}/${encodeURIComponent(pin.target_id)}`;

const PinnedCard: React.FC<{
  pin: PinnedObject;
  index: number;
  onUnpin: (targetId: string) => void;
}> = ({ pin, index, onUnpin }) => {
  const quality = QUALITY_LABELS.find(q => q.value === pin.redshift_quality);

  return (
    <Link
      href={detailHref(pin)}
      className="group/pin flex items-center gap-3 p-2 rounded-lg hover:bg-card-hover transition-colors animate-slide-in-up"
      style={{ animationDelay: `${index * 30}ms` }}
    >
      <TileThumbnail targetId={pin.target_id} size={40} className="flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-mono text-text-primary truncate group-hover/pin:text-primary transition-colors">
          {pin.target_id}
        </p>
        <p className="text-xs text-text-secondary truncate">
          <span className="uppercase">{pin.field}</span>
          <span className="mx-1.5 text-text-tertiary">·</span>
          <span className="font-mono">
            {pin.redshift !== null ? `z=${pin.redshift.toFixed(4)}` : 'z=—'}
          </span>
          {quality && (
            <>
              <span className="mx-1.5 text-text-tertiary">·</span>
              <span title={quality.label}>
                {quality.icon} {quality.short_label}
              </span>
            </>
          )}
        </p>
      </div>
      <button
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onUnpin(pin.target_id);
        }}
        title="Unpin"
        aria-label={`Unpin ${pin.target_id}`}
        className="flex-shrink-0 p-1 rounded-md text-text-tertiary opacity-0 group-hover/pin:opacity-100 focus-visible:opacity-100 hover:text-danger hover:bg-surface-2 transition-all active:scale-90"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </Link>
  );
};

/**
 * The "pinned spectra bucket" — a collapsed chip in the NIRSpec page header
 * showing stacked thumbnails of pinned objects; expands on hover (or click,
 * for touch/keyboard) into a panel of compact cards linking to detail pages.
 */
export const PinnedObjectsBucket: React.FC = () => {
  const { user } = useAuth();
  const { pinnedObjects, unpinObject } = usePreferences();
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const cancelClose = useCallback(() => {
    if (closeTimer.current) {
      clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);

  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = setTimeout(() => setOpen(false), CLOSE_DELAY_MS);
  }, [cancelClose]);

  // Close on outside click / Escape (covers touch and keyboard users, where
  // there is no mouseleave to collapse the panel).
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: MouseEvent | TouchEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('touchstart', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('touchstart', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  useEffect(() => cancelClose, [cancelClose]);

  if (!user) return null;

  const hasPins = pinnedObjects.length > 0;
  const stack = pinnedObjects.slice(0, 3);

  return (
    <div
      ref={rootRef}
      className="relative"
      onMouseEnter={() => {
        cancelClose();
        setOpen(true);
      }}
      onMouseLeave={scheduleClose}
    >
      {/* Collapsed chip */}
      <button
        onClick={() => setOpen(prev => !prev)}
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={
          hasPins
            ? `Pinned objects (${pinnedObjects.length})`
            : 'Pinned objects (empty)'
        }
        className={`flex items-center gap-2 pl-3 pr-3.5 py-1.5 rounded-full border transition-all duration-200 ${
          open
            ? 'border-primary/50 bg-card shadow-md'
            : hasPins
              ? 'border-border bg-card hover:border-primary/50 hover:shadow-md'
              : 'border-border bg-card opacity-60 hover:opacity-100'
        }`}
      >
        <Pin
          className={`w-4 h-4 transition-colors ${
            hasPins ? 'text-primary fill-current' : 'text-text-tertiary'
          }`}
        />
        {hasPins && (
          <>
            {/* Stacked mini-thumbnails of the most recent pins */}
            <span className="flex items-center">
              {stack.map((pin, i) => (
                <span
                  key={pin.target_id}
                  className={`block rounded ring-2 ring-card ${i > 0 ? '-ml-1.5' : ''}`}
                >
                  <TileThumbnail targetId={pin.target_id} size={20} />
                </span>
              ))}
            </span>
            <span className="text-xs font-medium text-text-secondary tabular-nums">
              {pinnedObjects.length}
            </span>
          </>
        )}
        {!hasPins && (
          <span className="text-xs font-medium text-text-secondary">Pins</span>
        )}
      </button>

      {/* Expanded panel */}
      {open && (
        <div
          className="absolute right-0 top-full mt-2 w-[22rem] z-50 bg-card border border-border rounded-card shadow-xl origin-top-right animate-unfurl"
          role="menu"
        >
          <div className="flex items-center justify-between px-3.5 pt-3 pb-2 border-b border-border">
            <span className="text-xs font-medium text-text-secondary uppercase tracking-wider">
              Pinned objects
            </span>
            <span className="text-xs text-text-tertiary tabular-nums">
              {pinnedObjects.length} / {MAX_PINNED_OBJECTS}
            </span>
          </div>
          {hasPins ? (
            <div className="p-1.5 max-h-[60vh] overflow-y-auto">
              {pinnedObjects.map((pin, i) => (
                <PinnedCard key={pin.target_id} pin={pin} index={i} onUnpin={unpinObject} />
              ))}
            </div>
          ) : (
            <div className="px-4 py-6 text-center">
              <Pin className="w-5 h-5 mx-auto mb-2 text-text-tertiary" />
              <p className="text-sm text-text-secondary">No pinned objects yet.</p>
              <p className="text-xs text-text-tertiary mt-1">
                Hover a table row and click the pin icon to keep an object handy here.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
