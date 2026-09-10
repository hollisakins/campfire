'use client';

/**
 * Go-to-coordinate box (epic #337) — the "coord search" launcher the tool-rail spec
 * reserved (`docs/design-fitsgl-map-ux.md` §4) and Phase 4.5 never built.
 *
 * A floating glass panel over the map: type an RA/Dec, Enter recentres the view and
 * pins FitsGL's sky-locked crosshair there (`handle.setTarget`), so the position
 * stays marked while you pan and zoom around it. Escape clears the pin and closes.
 *
 * Parsing is FitsGL's `parseSkyCoord`, not CAMPFIRE's `parseCoordinates`: it takes
 * space-separated sexagesimal (`10 00 00 +02 12 00`) and bare h-m-s forms that the
 * local parser's strict two-token split rejects, and it is the same parser the
 * FitsGL viewer's own go-to box uses, so the two surfaces accept the same input.
 */

import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { GLASS } from './glass';

/** Why a go-to attempt failed, or `'ok'`. Mirrors the FitsGL explorer's statuses. */
export type GoToStatus = 'ok' | 'bad-input' | 'no-wcs';

interface GoToBoxProps {
  /** The currently pinned target (drives the resolved-coordinate hint), or null. */
  target: { ra: number; dec: number } | null;
  /** Whether the pinned target landed on the mosaic; null when nothing is pinned. */
  targetInside: boolean | null;
  /** Sexagesimal rendering of the pinned target for the hint line. */
  formatTarget: (target: { ra: number; dec: number }) => string;
  /** Recentre + pin. Returns why it failed so the box can say so. */
  onGo: (text: string) => GoToStatus;
  /** Clear the pinned crosshair. */
  onClear: () => void;
  onClose: () => void;
}

export function GoToBox({ target, targetInside, formatTarget, onGo, onClear, onClose }: GoToBoxProps) {
  const [text, setText] = useState('');
  const [status, setStatus] = useState<GoToStatus>('ok');
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Open focused: the box exists to be typed into.
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const submit = () => {
    if (text.trim() === '') return;
    setStatus(onGo(text));
  };

  const err = status !== 'ok';
  const hint =
    status === 'bad-input'
      ? 'Could not read that as RA/Dec.'
      : status === 'no-wcs'
        ? 'This band has no usable WCS.'
        : target
          ? `${formatTarget(target)}${targetInside === false ? '  ·  outside image' : ''}`
          : 'Decimal or sexagesimal, ICRS.';

  // Positioned below the band rail (top-3) and clear of the right dock and status
  // pill; z-550 matches the dock's popovers so it stays above the other chrome.
  return (
    <div
      className={`absolute left-1/2 top-16 z-[550] w-[min(24rem,calc(100%-6rem))] -translate-x-1/2 rounded-xl ${GLASS} p-2`}
      role="dialog"
      aria-label="Go to coordinates"
    >
      <div className="flex items-center gap-1.5">
        <input
          ref={inputRef}
          type="text"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setStatus('ok');
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              submit();
            } else if (e.key === 'Escape') {
              // Handled here, not by the map's window listener, which ignores
              // keys typed into a field.
              e.preventDefault();
              e.stopPropagation();
              onClear();
              onClose();
            }
          }}
          placeholder="RA Dec — e.g. 10:00:00 +02:12:00"
          spellCheck={false}
          autoComplete="off"
          aria-label="RA and Dec"
          className="min-w-0 flex-1 rounded-md border border-border bg-[var(--surface-2)] px-2 py-1.5 font-mono text-xs text-text-primary outline-none placeholder:text-text-tertiary focus:border-primary"
        />
        <button
          type="button"
          onClick={submit}
          className="rounded-md px-2.5 py-1.5 text-xs text-text-secondary transition-colors hover:bg-card-hover hover:text-text-primary"
        >
          Go
        </button>
        <button
          type="button"
          onClick={onClose}
          title="Close"
          aria-label="Close"
          className="flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-card-hover hover:text-text-primary"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className={`mt-1 px-1 font-mono text-[10px] ${err ? 'text-danger' : 'text-text-tertiary'}`}>
        {hint}
      </div>
      {target && (
        <button
          type="button"
          onClick={() => {
            onClear();
            setText('');
            setStatus('ok');
          }}
          className="mt-0.5 px-1 text-[10px] text-text-tertiary underline-offset-2 transition-colors hover:text-text-primary hover:underline"
        >
          Clear crosshair
        </button>
      )}
    </div>
  );
}
