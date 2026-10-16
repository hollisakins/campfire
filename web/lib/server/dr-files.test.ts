// Release files (P6c-2): a member is served only from its archived copy;
// pending members are unavailable; nothing resolves to a live key.
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('server-only', () => ({}));

type Row = Record<string, unknown>;

/** A chainable PostgREST fake: every filter narrows `rows`; awaiting yields them. */
function fakeTable(rows: Row[]) {
  const state = { rows: [...rows] };
  const chain: Record<string, unknown> = {};
  const narrow = (pred: (r: Row) => boolean) => { state.rows = state.rows.filter(pred); return chain; };
  Object.assign(chain, {
    select: () => chain,
    eq: (col: string, v: unknown) => narrow((r) => r[col] === v),
    is: (col: string, v: unknown) => narrow((r) => (v === null ? r[col] == null : r[col] === v)),
    in: (col: string, vs: unknown[]) => narrow((r) => vs.includes(r[col])),
    or: (expr: string) => {
      // only the shape dr-files builds: program_slug.is.null,program_slug.in.(a,b)
      const m = /program_slug\.in\.\(([^)]*)\)/.exec(expr);
      const slugs = m ? m[1].split(',').map((s) => s.replace(/^"|"$/g, '')) : [];
      return narrow((r) => r.program_slug == null || slugs.includes(String(r.program_slug)));
    },
    order: () => chain,
    then: (res: (v: { data: Row[]; error: null }) => unknown) => Promise.resolve({ data: state.rows, error: null }).then(res),
  });
  return chain;
}

let manifest: Row[] = [];
let archiveRegistry: Row[] = [];
let signed: string[][] = [];
let serviceQueries = 0;

vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: () => ({
    from: (table: string) => {
      serviceQueries += 1;
      if (table !== 'storage_objects') throw new Error(`unexpected service table ${table}`);
      return fakeTable(archiveRegistry);
    },
  }),
}));
vi.mock('@/lib/r2', () => ({
  generateDownloadUrls: async (keys: string[]) => {
    signed.push(keys);
    return keys.map((k) => `https://osn.example/${k}?sig=1`);
  },
}));
vi.mock('@/lib/server/cdn-front', () => ({
  frontUrlsForResolved: async (keys: string[], resolved: { contentHash: string | null; registeredAt: number | null }[]) =>
    new Map(keys.map((k, i) => [k, resolved[i].contentHash ? `https://front.example/o/${k}?h=${resolved[i].contentHash}&r=${resolved[i].registeredAt ?? 0}` : null])),
}));

import {
  listReleaseMembers,
  listReleaseMembersIn,
  releaseDownloadUrls,
  releaseFrontUrls,
  resolveReleaseMembers,
  resolveReleaseMembersAs,
} from './dr-files';

const viewer = { from: (table: string) => { if (table !== 'dr_manifest') throw new Error(table); return fakeTable(manifest); } } as never;

const LIVE_FITS = 'spectra/obs_a/obs_a_prism_clear_1_spec.fits';           // legacy form, as spectra.fits_path
const CANON_FITS = 'data/products/nirspec/obs_a/obs_a_prism_clear_1_spec.fits';
const CANON_JSON = 'data/products/nirspec/obs_a/obs_a_prism_clear_1_spec.json';
const ARCH_FITS = `archive/${'a'.repeat(64)}/products/nirspec/obs_a/obs_a_prism_clear_1_spec.fits`;
const ARCH_JSON = `archive/${'b'.repeat(64)}/products/nirspec/obs_a/obs_a_prism_clear_1_spec.json`;

function member(storage_key: string, over: Row = {}): Row {
  return {
    tag: 'DR-t', storage_key, product_type: 'nirspec_spec', component: 'nirspec', program_slug: 'pub',
    archive_key: null, content_hash: 'sha256:' + 'c'.repeat(64), stamped_hash: null, size_bytes: 10,
    backend: 'osn', bucket: 'data', byte_state: 'pending', ...over,
  };
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  manifest = [];
  archiveRegistry = [];
  signed = [];
  serviceQueries = 0;
});

describe('resolveReleaseMembers', () => {
  it('matches legacy and canonical input keys to the canonical manifest row and keys the answer by input', async () => {
    manifest = [member(CANON_FITS, { byte_state: 'stamped', archive_key: ARCH_FITS, stamped_hash: 'sha256:' + 's'.repeat(64) })];
    const out = await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS, CANON_FITS]);
    expect(out.get(LIVE_FITS)?.archiveKey).toBe(ARCH_FITS);
    expect(out.get(CANON_FITS)?.archiveKey).toBe(ARCH_FITS);
    // the stamped hash is the archived bytes' identity
    expect(out.get(LIVE_FITS)?.contentHash).toBe('sha256:' + 's'.repeat(64));
  });

  it('answers null for pending, failed, unknown-layout and unlisted keys', async () => {
    manifest = [
      member(CANON_FITS, { byte_state: 'pending' }),
      member(CANON_JSON, { byte_state: 'failed', archive_key: ARCH_JSON, product_type: 'spectrum_json' }),
    ];
    const out = await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS, CANON_JSON, 'not/a/key', 'spectra/obs_b/x_spec.fits']);
    expect([...out.values()]).toEqual([null, null, null, null]);
  });

  it('never returns a copied member without an archive key', async () => {
    manifest = [member(CANON_FITS, { byte_state: 'copied', archive_key: null })];
    const out = await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS]);
    expect(out.get(LIVE_FITS)).toBeNull();
  });
});

describe('resolveReleaseMembersAs (service role)', () => {
  const rows = () => [
    member(CANON_FITS, { byte_state: 'copied', archive_key: ARCH_FITS, program_slug: 'priv' }),
    member(CANON_JSON, { byte_state: 'copied', archive_key: ARCH_JSON, product_type: 'nircam_layout', component: 'nircam/f', program_slug: null }),
  ];
  it('keeps field-level rows for everyone and program rows only within the scope', async () => {
    manifest = rows();
    const out = await resolveReleaseMembersAs(viewer, 'DR-t', [CANON_FITS, CANON_JSON], { accessible: ['pub'], isAdmin: false });
    expect(out.get(CANON_FITS)).toBeNull();
    expect(out.get(CANON_JSON)?.archiveKey).toBe(ARCH_JSON);
  });
  it('opens program rows to a holder of the program, and everything to admins', async () => {
    manifest = rows();
    const held = await resolveReleaseMembersAs(viewer, 'DR-t', [CANON_FITS], { accessible: ['priv'], isAdmin: false });
    expect(held.get(CANON_FITS)?.archiveKey).toBe(ARCH_FITS);
    manifest = rows();
    const admin = await resolveReleaseMembersAs(viewer, 'DR-t', [CANON_FITS], { accessible: [], isAdmin: true });
    expect(admin.get(CANON_FITS)?.archiveKey).toBe(ARCH_FITS);
  });
});

describe('listReleaseMembers', () => {
  it('lists the archived members of a component, narrowed by product type', async () => {
    manifest = [
      member('data/products/nircam/f/f_layout.png', { byte_state: 'copied', archive_key: 'archive/x/products/nircam/f/f_layout.png', product_type: 'nircam_layout', component: 'nircam/f', program_slug: null }),
      member('data/products/nircam/f/f444w/expmap_f_f444w.png', { byte_state: 'pending', product_type: 'nircam_expmap_plot', component: 'nircam/f', program_slug: null }),
      member('data/products/nircam/f/f444w/mosaic_f.fits.gz', { byte_state: 'copied', archive_key: 'archive/y/mosaic', product_type: 'nircam_mosaic', component: 'nircam/f', program_slug: null }),
    ];
    const out = await listReleaseMembers(viewer, 'DR-t', { component: 'nircam/f', productTypes: ['nircam_layout', 'nircam_expmap_plot'] });
    expect(out.map((m) => m.productType)).toEqual(['nircam_layout']);
  });
});

describe('releaseFrontUrls / releaseDownloadUrls', () => {
  it('mints front urls on the archive key with the registry identity and registration time', async () => {
    manifest = [member(CANON_FITS, { byte_state: 'stamped', archive_key: ARCH_FITS, stamped_hash: 'sha256:' + 's'.repeat(64) })];
    archiveRegistry = [{ storage_key: ARCH_FITS, content_hash: 'sha256:' + 's'.repeat(64), archived_at: '2026-10-15T12:00:00Z', root: 'archive', status: 'archived' }];
    const members = await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS, 'spectra/obs_b/none_spec.fits']);
    const urls = await releaseFrontUrls(members);
    expect(urls.get(LIVE_FITS)).toBe(`https://front.example/o/${ARCH_FITS}?h=sha256:${'s'.repeat(64)}&r=${Math.floor(Date.parse('2026-10-15T12:00:00Z') / 1000)}`);
    expect(urls.get('spectra/obs_b/none_spec.fits')).toBeNull();
    expect(serviceQueries).toBe(1);
  });

  it('falls back to the manifest hash with r=0 when the archive row is missing', async () => {
    manifest = [member(CANON_FITS, { byte_state: 'copied', archive_key: ARCH_FITS })];
    const urls = await releaseFrontUrls(await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS]));
    expect(urls.get(LIVE_FITS)).toContain(`h=sha256:${'c'.repeat(64)}&r=0`);
  });

  it('presigns the archive key, never the live one, and skips unavailable members', async () => {
    manifest = [member(CANON_FITS, { byte_state: 'copied', archive_key: ARCH_FITS })];
    const members = await resolveReleaseMembers(viewer, 'DR-t', [LIVE_FITS, CANON_JSON]);
    const urls = await releaseDownloadUrls(members, 3600, { attachment: true });
    expect(signed).toEqual([[ARCH_FITS]]);
    expect(urls.get(LIVE_FITS)).toBe(`https://osn.example/${ARCH_FITS}?sig=1`);
    expect(urls.get(CANON_JSON)).toBeNull();
  });
});

describe('listReleaseMembersIn', () => {
  it('lists the archived members of several components in one query, by product type', async () => {
    const lay = (field: string, state: string) => member(`data/products/nircam/${field}/${field}_layout.png`, {
      product_type: 'nircam_layout', component: `nircam/${field}`, program_slug: null, byte_state: state,
      archive_key: state === 'copied' ? `archive/${'d'.repeat(64)}/products/nircam/${field}/${field}_layout.png` : null,
    });
    manifest = [lay('a2744', 'copied'), lay('cosmos', 'pending'), lay('egs', 'copied'),
      member('data/products/nircam/a2744/f444w/x_thumb.png', { product_type: 'nircam_mosaic_thumbnail', component: 'nircam/a2744', program_slug: null, byte_state: 'copied', archive_key: 'archive/ee/t.png' })];
    const out = await listReleaseMembersIn(viewer, 'DR-t', { components: ['nircam/a2744', 'nircam/cosmos'], productTypes: ['nircam_layout'] });
    expect(out.map((m) => m.component)).toEqual(['nircam/a2744']);
    expect(await listReleaseMembersIn(viewer, 'DR-t', { components: [] })).toEqual([]);
  });
});

