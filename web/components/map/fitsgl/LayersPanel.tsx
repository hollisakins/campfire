'use client';

/**
 * Layers panel (epic #337, Phase 4.5) — docked-right toggles for what's drawn OVER
 * the imagery (`docs/design-fitsgl-map-ux.md` §4): NIRSpec objects, MSA shutters
 * (Phase 4b), and the RA/Dec graticule. No quality legend (locked decision 5).
 */

import { PANEL_CAP } from './glass';
import { SwitchVisual } from './Switch';

interface ToggleRowProps {
  label: string;
  on: boolean;
  onToggle: (on: boolean) => void;
  disabled?: boolean;
  hint?: string;
}

function ToggleRow({ label, on, onToggle, disabled, hint }: ToggleRowProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onToggle(!on)}
      className={`flex w-full items-center justify-between py-1 text-xs ${disabled ? 'cursor-default text-text-tertiary' : 'text-text-secondary hover:text-text-primary'}`}
    >
      <span>{label}{hint && <span className="ml-1.5 text-[10px] text-text-tertiary">{hint}</span>}</span>
      <SwitchVisual on={on} />
    </button>
  );
}

interface LayersPanelProps {
  showMarkers: boolean;
  onToggleMarkers: (on: boolean) => void;
  markerCount: number;
  graticule: boolean;
  onToggleGraticule: (on: boolean) => void;
  /** MSA shutters — the region overlay lands in Phase 4b; the row is inert until then. */
  shuttersAvailable?: boolean;
  showShutters?: boolean;
  onToggleShutters?: (on: boolean) => void;
}

export function LayersPanel({
  showMarkers,
  onToggleMarkers,
  markerCount,
  graticule,
  onToggleGraticule,
  shuttersAvailable = false,
  showShutters = false,
  onToggleShutters,
}: LayersPanelProps) {
  return (
    <div className="space-y-1">
      <h4 className={PANEL_CAP}>Layers</h4>
      <ToggleRow label={`NIRSpec objects (${markerCount})`} on={showMarkers} onToggle={onToggleMarkers} />
      <ToggleRow
        label="MSA shutters"
        on={shuttersAvailable ? showShutters : false}
        onToggle={onToggleShutters ?? (() => {})}
        disabled={!shuttersAvailable}
        hint={shuttersAvailable ? undefined : 'soon'}
      />
      <ToggleRow label="Graticule" on={graticule} onToggle={onToggleGraticule} />
    </div>
  );
}
