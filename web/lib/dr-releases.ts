/**
 * Display helpers for the data-release pages (P6a): state labels, the
 * component summary, and the citation block. Pure; no directive, so the pages,
 * the tests and later client components share them. The reads live in
 * `lib/server/dr-releases.ts`.
 */

export interface ReleaseCitationInput {
  tag: string;
  doi: string | null;
  published_at: string | null;
  frozen_at: string | null;
}

/** The release lifecycle (dr_releases.state) as a label and tone (admin pages). */
export function releaseStateBadge(state: string): { label: string; tone: 'published' | 'pending' | 'withdrawn' | 'failed' } {
  switch (state) {
    case 'published':
      return { label: 'Published', tone: 'published' };
    case 'withdrawn':
      return { label: 'Withdrawn', tone: 'withdrawn' };
    case 'failed':
      return { label: 'Failed', tone: 'failed' };
    default:
      // draft / freezing / frozen / exporting / exported / copying / ready
      return { label: `${state.charAt(0).toUpperCase()}${state.slice(1)}`, tone: 'pending' };
  }
}

/** The admin's next CLI step for a release in this state (null when none). */
export function releaseNextStep(state: string, tag: string): string | null {
  switch (state) {
    case 'frozen':
    case 'exported':
      return `campfire dr archive --tag ${tag}`;
    case 'copying':
      return `campfire dr archive --tag ${tag} --status`;
    case 'ready':
      return `campfire dr publish ${tag}`;
    case 'failed':
      return `campfire dr purge ${tag}`;
    default:
      return null;
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
    if (raw === 'nirspec') return { kind: 'nirspec', label: 'NIRSpec', raw };
    if (raw.startsWith('nircam/')) return { kind: 'nircam', label: `NIRCam · ${raw.slice(7)}`, raw };
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

interface ComponentFacts {
  files: number | null;
  bytes: number | null;
  area_arcmin2: number | null;
}

/** counts.by_component[name] (the freeze records files and bytes per component). */
function componentFacts(counts: Record<string, unknown> | null | undefined, name: string): ComponentFacts {
  const by = counts?.by_component;
  const entry = by && typeof by === 'object' ? (by as Record<string, unknown>)[name] : undefined;
  const rec = entry && typeof entry === 'object' ? (entry as Record<string, unknown>) : {};
  return { files: releaseCount(rec, 'files'), bytes: releaseCount(rec, 'bytes'), area_arcmin2: releaseCount(rec, 'area_arcmin2') };
}

/** The total, or null unless every field has the value: a partial sum would read as the release's total. */
function sumComplete(values: (number | null)[]): number | null {
  if (values.length === 0 || values.some((v) => v == null)) return null;
  return values.reduce<number>((a, b) => a + (b as number), 0);
}

export interface ReleaseSummary {
  nirspec: { spectra: number | null; objects: number | null; programs: number | null; fields: number | null; bytes: number | null } | null;
  nircam: { fields: { field: string; mosaics: number | null; bytes: number | null; area_arcmin2: number | null }[]; mosaics: number | null; bytes: number | null; area_arcmin2: number | null } | null;
  photometry: { catalogs: { field: string; catalog: string }[]; rows: number | null } | null;
}

/**
 * What a release contains, per instrument, from `components` and `counts`:
 * the numbers the pages show as cards. A component the freeze did not record
 * sizes for (pre-P6a releases) shows null bytes.
 */
export function releaseSummary(components: unknown, counts: Record<string, unknown> | null | undefined): ReleaseSummary {
  const parsed = parseReleaseComponents(components);
  const nirspec = parsed.some((c) => c.kind === 'nirspec')
    ? {
        spectra: releaseCount(counts, 'spectra'),
        objects: releaseCount(counts, 'objects'),
        programs: releaseCount(counts, 'nirspec_programs'),
        fields: releaseCount(counts, 'nirspec_fields'),
        bytes: componentFacts(counts, 'nirspec').bytes,
      }
    : null;
  const nircamFields = parsed
    .filter((c) => c.kind === 'nircam')
    .map((c) => {
      const facts = componentFacts(counts, c.raw);
      return { field: c.raw.slice(7), mosaics: facts.files, bytes: facts.bytes, area_arcmin2: facts.area_arcmin2 };
    })
    .sort((a, b) => a.field.localeCompare(b.field));
  const nircam = nircamFields.length > 0
    ? {
        fields: nircamFields,
        mosaics: sumComplete(nircamFields.map((f) => f.mosaics)),
        bytes: sumComplete(nircamFields.map((f) => f.bytes)),
        area_arcmin2: sumComplete(nircamFields.map((f) => f.area_arcmin2)),
      }
    : null;
  const catalogs = parsed
    .filter((c) => c.kind === 'photometry')
    .map((c) => {
      const [, field, ...catalog] = c.raw.split('/');
      return { field, catalog: catalog.join('/') };
    });
  const photometry = catalogs.length > 0 ? { catalogs, rows: releaseCount(counts, 'photometry') } : null;
  return { nirspec, nircam, photometry };
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
 * until P7 deposits the release.
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

/** Sky area as the NIRCam pages show it: deg² when it reads naturally, arcmin² for small fields. */
export function formatArea(arcmin2: number | null | undefined): string {
  if (arcmin2 == null || !Number.isFinite(arcmin2)) return '—';
  const deg2 = arcmin2 / 3600;
  return deg2 >= 0.05 ? `${deg2.toFixed(2)} deg²` : `${Math.round(arcmin2)} arcmin²`;
}
