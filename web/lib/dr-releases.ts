/**
 * Display helpers for the data-release pages (P6a): state labels, the
 * component list, and the citation block. Pure; no directive, so the pages,
 * the tests and later client components share them. The reads live in
 * `lib/server/dr-releases.ts`.
 */

export interface ReleaseCitationInput {
  tag: string;
  doi: string | null;
  published_at: string | null;
  frozen_at: string | null;
}

/** The release lifecycle (dr_releases.state) as a reader-facing label and tone. */
export function releaseStateBadge(state: string): { label: string; tone: 'published' | 'pending' | 'withdrawn' | 'failed' } {
  switch (state) {
    case 'published':
      return { label: 'Published', tone: 'published' };
    case 'withdrawn':
      return { label: 'Withdrawn', tone: 'withdrawn' };
    case 'failed':
      return { label: 'Failed', tone: 'failed' };
    case 'draft':
    case 'freezing':
      return { label: state === 'draft' ? 'Draft' : 'Freezing', tone: 'pending' };
    default:
      // frozen / exporting / exported / copying / ready: built, not yet public
      return { label: `${state.charAt(0).toUpperCase()}${state.slice(1)} · unpublished`, tone: 'pending' };
  }
}

export interface ReleaseComponent {
  kind: 'nirspec' | 'nircam' | 'photometry' | 'other';
  label: string;
  raw: string;
}

/** `dr_releases.components` (["nirspec", "nircam/<field>", "photometry/<field>/<catalog>"]) for display. */
export function parseReleaseComponents(components: unknown): ReleaseComponent[] {
  const list = Array.isArray(components) ? components.filter((c): c is string => typeof c === 'string') : [];
  return list.map((raw) => {
    if (raw === 'nirspec') return { kind: 'nirspec', label: 'NIRSpec spectra', raw };
    if (raw.startsWith('nircam/')) return { kind: 'nircam', label: `NIRCam mosaics · ${raw.slice(7)}`, raw };
    if (raw.startsWith('photometry/')) {
      const [, field, ...catalog] = raw.split('/');
      return { kind: 'photometry', label: `Photometry · ${field} · ${catalog.join('/')}`, raw };
    }
    return { kind: 'other', label: raw, raw };
  });
}

/** A count from `dr_releases.counts`, or null when the freeze did not record it. */
export function releaseCount(counts: Record<string, unknown> | null | undefined, key: string): number | null {
  const v = counts?.[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function releaseYear(r: ReleaseCitationInput): string {
  const when = r.published_at ?? r.frozen_at;
  const year = when ? new Date(when).getUTCFullYear() : NaN;
  return Number.isFinite(year) ? String(year) : 'n.d.';
}

/** BibTeX key: letters and digits only. */
function bibKey(tag: string): string {
  return `campfire_${tag.replace(/[^A-Za-z0-9]+/g, '_')}`;
}

/**
 * Plain-text and BibTeX citations for a release. The DOI is a placeholder
 * until P7 deposits the release (the cite block says so).
 */
export function releaseCitation(r: ReleaseCitationInput, siteUrl: string): { text: string; bibtex: string } {
  const year = releaseYear(r);
  const url = `${siteUrl.replace(/\/$/, '')}/releases/${encodeURIComponent(r.tag)}`;
  const doi = r.doi ?? null;
  const text =
    `CAMPFIRE Collaboration (${year}). CAMPFIRE Data Release ${r.tag}. ` +
    (doi ? `doi:${doi}` : url);
  const lines = [
    `@misc{${bibKey(r.tag)},`,
    `  author       = {{CAMPFIRE Collaboration}},`,
    `  title        = {{CAMPFIRE Data Release ${r.tag}}},`,
    `  year         = {${year}},`,
    doi ? `  doi          = {${doi}},` : null,
    `  howpublished = {\\url{${url}}}`,
    `}`,
  ].filter((l): l is string => l !== null);
  return { text, bibtex: lines.join('\n') };
}

/** Bytes as a short human string (1.2 MB). */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
