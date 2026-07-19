'use client';

/**
 * Status pill (epic #337, Phase 4.5) — the bottom-center glass readout
 * (`docs/design-fitsgl-map-ux.md` §4): dual RA/Dec (sexagesimal + decimal, keeping
 * the existing CAMPFIRE formatters — decision 8), the cursor's pixel value per
 * active band, zoom, and `band · stretch`. Grows to show separation + position
 * angle while the ruler tool has a measurement.
 */

import { formatRA, formatDec } from '@/lib/utils/wcs';
import { formatSeparation } from '@fitsgl/core';
import { GLASS_PILL } from './glass';
import type { RulerMeasurement } from './ruler';

interface StatusPillProps {
  ra: number | null;
  dec: number | null;
  values: ReadonlyArray<number | null> | null;
  native: boolean;
  zoom: number | null;
  bandLabel: string;
  stretch: string;
  ruler: RulerMeasurement | null;
  /** Hide the per-band pixel value readout (e.g. a many-band trilogy composite). */
  showValue?: boolean;
}

function fmtVal(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(2);
  return v.toPrecision(4);
}

function Item({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <span className="flex items-baseline gap-1">
      <span className="text-text-tertiary">{k}</span>
      <b className="font-medium text-text-primary">{children}</b>
    </span>
  );
}

export function StatusPill({ ra, dec, values, native, zoom, bandLabel, stretch, ruler, showValue = true }: StatusPillProps) {
  const hasVal = !!values && values.some((v) => v !== null && Number.isFinite(v));
  const valStr = hasVal ? values!.map(fmtVal).join(' ') + (native ? '' : '*') : '—';
  return (
    <div
      className={`absolute bottom-4 left-1/2 z-[500] flex -translate-x-1/2 items-center gap-4 whitespace-nowrap ${GLASS_PILL} px-4 py-2 font-mono text-xs`}
    >
      <Item k="α">{ra !== null ? formatRA(ra) : '—'}</Item>
      <Item k="δ">{dec !== null ? formatDec(dec) : '—'}</Item>
      <span className="text-[10px] text-text-tertiary">
        {ra !== null && dec !== null ? `(${ra.toFixed(5)}, ${dec.toFixed(5)})` : ''}
      </span>
      <span className="h-3.5 w-px bg-border" aria-hidden />
      {showValue && <Item k="val">{valStr}</Item>}
      <Item k="zoom">{zoom !== null ? `${zoom.toFixed(2)}×` : '—'}</Item>
      <span className="text-text-tertiary">{bandLabel} · {stretch}</span>
      {ruler && (
        <>
          <span className="h-3.5 w-px bg-border" aria-hidden />
          <Item k="sep">
            {ruler.sepDeg !== null ? formatSeparation(ruler.sepDeg) : `${ruler.pixelDist.toFixed(1)} px`}
          </Item>
          {ruler.paDeg !== null && <Item k="PA">{ruler.paDeg.toFixed(1)}°</Item>}
        </>
      )}
    </div>
  );
}
