// Plain helpers for the NIRSpec nods renderer (P5). Kept OUT of the 'use server'
// action module (nirspec-nods actions): a 'use server' file may export only async
// functions, so any const/helper lives here (mirrors lib/admin/sort-keys.ts).

import type { SpectrumExposure } from '@/lib/types';

export const NOD_DETECTORS = ['nrs1', 'nrs2'] as const;
export type NodDetector = (typeof NOD_DETECTORS)[number];

// A "source" the renderer groups by is (observation, source_id). Encode both into
// one URL segment; decode by splitting on the LAST '__' (source_id is a bare int,
// so the split is unambiguous even if the observation name contains '__').
export function encodeSource(observation: string, sourceId: number): string {
  return `${encodeURIComponent(observation)}__${sourceId}`;
}

export function decodeSource(segment: string): { observation: string; sourceId: number } | null {
  const i = segment.lastIndexOf('__');
  if (i < 0) return null;
  const sourceId = Number(segment.slice(i + 2));
  if (!Number.isInteger(sourceId)) return null;
  return { observation: decodeURIComponent(segment.slice(0, i)), sourceId };
}

/** One row of the nods grid: an (exp_group, nod) group with a cell per detector. */
export interface NodGridRow {
  exp_group: number | null;
  nod: string;
  label: string;                       // e.g. "d1:00001" (multi-group) or "00001"
  cells: Record<NodDetector, SpectrumExposure | null>;
}

/**
 * Reshape flat spectrum_exposures rows into ordered (exp_group, nod) grid rows ×
 * detector columns — the `*_nods.pdf` layout (plots.py: rows sorted by exp_group
 * then nod, columns = detector). Rows are labelled `d{n}:{nod}` when there is more
 * than one exp_group, else just the nod.
 */
export function buildNodGrid(rows: SpectrumExposure[]): NodGridRow[] {
  const groups = new Map<string, NodGridRow>();
  const order: string[] = [];
  for (const r of rows) {
    const key = `${r.exp_group ?? -1}::${r.nod}`;
    let row = groups.get(key);
    if (!row) {
      row = { exp_group: r.exp_group, nod: r.nod, label: r.nod,
              cells: { nrs1: null, nrs2: null } };
      groups.set(key, row);
      order.push(key);
    }
    if (r.detector === 'nrs1' || r.detector === 'nrs2') row.cells[r.detector] = r;
  }
  const result = order.map((k) => groups.get(k)!);
  // sort by (exp_group, nod); null exp_group sorts first
  result.sort((a, b) => {
    const ea = a.exp_group ?? -1, eb = b.exp_group ?? -1;
    if (ea !== eb) return ea - eb;
    return a.nod.localeCompare(b.nod);
  });
  // label with the dither ordinal only when >1 exp_group is present (like the PDF)
  const distinctGroups = new Set(result.map((r) => r.exp_group ?? -1));
  if (distinctGroups.size > 1) {
    const idxByGroup = new Map<number, number>();
    let n = 0;
    for (const g of [...distinctGroups].sort((a, b) => a - b)) idxByGroup.set(g, ++n);
    for (const r of result) r.label = `d${idxByGroup.get(r.exp_group ?? -1)}:${r.nod}`;
  }
  return result;
}
