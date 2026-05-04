/**
 * Client-side serializers for NIRSpec pointing metadata.
 *
 * Exports CSV (flat tabular) and DS9 region file (4-quadrant polygons)
 * formats. Both run entirely in the browser from the JSONB pointings
 * array — no server round-trip.
 */

import type { Pointing } from '@/lib/types';

interface PointingRow extends Pointing {
  observation: string;
}

const CSV_COLUMNS: Array<{ key: keyof PointingRow; header: string }> = [
  { key: 'observation', header: 'observation' },
  { key: 'msametid', header: 'msametid' },
  { key: 'msametfl', header: 'msametfl' },
  { key: 'jwst_program', header: 'jwst_program' },
  { key: 'jwst_obs_ids', header: 'jwst_obs_ids' },
  { key: 'ra_center', header: 'ra_center_deg' },
  { key: 'dec_center', header: 'dec_center_deg' },
  { key: 'pa_aper', header: 'pa_aper_deg' },
  { key: 'gratings', header: 'gratings' },
  { key: 'filters', header: 'filters' },
  { key: 'n_exposures', header: 'n_exposures' },
  { key: 'n_dithers', header: 'n_dithers' },
  { key: 'exptime_total', header: 'exptime_total_s' },
  { key: 'date_obs_start', header: 'date_obs_start' },
  { key: 'date_obs_end', header: 'date_obs_end' },
];

function csvEscape(value: unknown): string {
  if (value == null) return '';
  let s: string;
  if (Array.isArray(value)) s = value.join(';');
  else s = String(value);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

export function pointingsToCsv(rows: PointingRow[]): string {
  const header = CSV_COLUMNS.map(c => c.header).join(',');
  const lines = rows.map(r => CSV_COLUMNS.map(c => csvEscape(r[c.key])).join(','));
  return [header, ...lines].join('\n') + '\n';
}

export function pointingsToDs9(rows: PointingRow[]): string {
  const lines: string[] = ['# Region file format: DS9', 'icrs'];
  for (const r of rows) {
    const tag = `${r.observation}_msametid${r.msametid}`;
    lines.push(`# composite ${r.ra_center} ${r.dec_center} ${r.pa_aper} ||| tag={${tag}}`);
    for (let q = 0; q < r.footprint.length; q++) {
      const corners = r.footprint[q];
      const coords = corners.map(([ra, dec]) => `${ra},${dec}`).join(',');
      lines.push(`polygon(${coords}) # tag={${tag}} text={Q${q + 1}}`);
    }
  }
  return lines.join('\n') + '\n';
}

export function flattenPointings(
  observations: Array<{ observation: string; pointings: Pointing[] | null }>,
): PointingRow[] {
  const rows: PointingRow[] = [];
  for (const obs of observations) {
    if (!obs.pointings) continue;
    for (const p of obs.pointings) {
      rows.push({ ...p, observation: obs.observation });
    }
  }
  return rows;
}

export function downloadBlob(content: string, filename: string, mimeType: string) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
