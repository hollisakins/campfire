'use server';

import { getRequestIdentity } from '@/lib/auth/identity';
import { requestDrTag } from '@/lib/dr/server';
import { createClient } from '@/lib/supabase/server';
import { paginateQuery } from '@/lib/supabase/paginate';
import { generateDownloadUrls } from '@/lib/r2';
import { parseKey } from '@/lib/layout';
import { listReleaseMembersIn, releaseDownloadUrls, type ReleaseMember } from '@/lib/server/dr-files';
import { thumbnailBase, quicklookBase, expmapPlotKey } from '@/lib/nircam-product-keys';
import {
  IMAGING_INSTRUMENTS,
  type ImagingProvenance, type NircamImage, type NircamExpmap, type NircamFieldCard,
  type NircamFieldSummary,
} from '@/lib/types';

// The product types of every imaging instrument's mosaics and of their PNG
// renditions (`<instrument>_mosaic`, `_mosaic_thumbnail`, `_mosaic_quicklook`):
// the field page lists NIRCam's and the HST instruments' side by side.
const MOSAIC_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_mosaic`);
const THUMBNAIL_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_mosaic_thumbnail`);
const QUICKLOOK_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_mosaic_quicklook`);
// The field products, one set per instrument of a field: the per-filter
// exposure map and its dark plot, and the instrument's layout plot. The
// layout shown for a field is the first instrument's that has one, in
// IMAGING_INSTRUMENTS order (NIRCam first), as get_nircam_fields picks it.
const EXPMAP_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_expmap`);
const EXPMAP_PLOT_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_expmap_plot`);
const LAYOUT_TYPES: string[] = IMAGING_INSTRUMENTS.map((i) => `${i}_layout`);

/** The instrument a product type belongs to (`acswfc_expmap` -> `acswfc`). */
function instrumentOfType(productType: string): string {
  return productType.split('_')[0];
}

/** Position in the fixed instrument order (NIRCam first); unknown last. */
function instrumentRank(instrument: string): number {
  const i = (IMAGING_INSTRUMENTS as readonly string[]).indexOf(instrument);
  return i < 0 ? IMAGING_INSTRUMENTS.length : i;
}

// Presigned-GET lifetime for <img> sources (layout / expmap plots / thumbnails).
// Raw presigned URLs go straight into <img> tags — no proxy hop, no CORS needed
// (same pattern as the admin exposure previews) — so this just needs to outlive
// a browsing session.
const IMG_PRESIGN_TTL_SECONDS = 21600;

export interface NircamImagesResult {
  images: NircamImage[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamExpmapsResult {
  expmaps: NircamExpmap[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldsResult {
  fields: NircamFieldCard[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldSummaryResult {
  summary: NircamFieldSummary | null;
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldImagesResult {
  /** Presigned GET for the field's <field>_layout.png, if deployed. */
  layoutUrl: string | null;
  /** `<instrument>/<filter>` (expmapPlotKey) -> presigned GET for that
   *  filter's dark expmap plot PNG. */
  expmapPlots: Record<string, string>;
  /** mosaic base key (thumb key minus `_thumb.png`) -> presigned GET. */
  thumbnails: Record<string, string>;
  /** mosaic base key -> presigned GET for the large popup quick-look. */
  quicklooks: Record<string, string>;
  /** Under a release (P6c-2): the live keys of the field's FITS products
   *  (mosaics, expmaps) whose archived copies the release holds — the rows
   *  the table may offer for download. Null for the live catalog (all). */
  downloadable?: string[] | null;
  error?: string;
}

/** The release's archived members of one field's products, by live key: the
 *  field's component of every imaging instrument (`<instrument>/<field>`). */
async function releaseFieldMembers(
  supabase: Awaited<ReturnType<typeof createClient>>,
  tag: string,
  field: string,
  productTypes: readonly string[],
): Promise<Map<string, ReleaseMember | null>> {
  const members = await listReleaseMembersIn(supabase, tag, {
    components: IMAGING_INSTRUMENTS.map((i) => `${i}/${field}`), productTypes: [...productTypes],
  });
  return new Map(members.map((m) => [m.storageKey, m]));
}

/**
 * Fetch NIRCam images from the database, optionally scoped to one field.
 * Requires authentication but no program-based access control.
 * Returns all matching images for client-side filtering/sorting.
 */
export async function getNircamImages(field?: string): Promise<NircamImagesResult> {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return {
      images: [],
      isAuthenticated: false,
    };
  }

  try {
    const { data, error } = await paginateQuery<NircamImage>(
      () => {
        // cat_mosaics: live rows, or the release's frozen mosaics
        // while one is browsed (P6c-1)
        let q = supabase
          .from('cat_mosaics')
          .select('*');
        if (field) q = q.eq('field', field);
        return q
          .order('field', { ascending: true })
          .order('instrument', { ascending: true })
          .order('filter', { ascending: true })
          .order('tile', { ascending: true })
          .order('id', { ascending: true });
      },
    );

    if (error) {
      console.error('Error fetching NIRCam images:', error);
      return {
        images: [],
        error: error.message,
        isAuthenticated: true,
      };
    }

    return {
      images: await attachStoredSizes(supabase, data, field),
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam images:', err);
    return {
      images: [],
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Attach the registry's stored (gzipped) byte counts to mosaic rows.
 *
 * `mosaics.file_size` is the logical (uncompressed) size; the bytes a
 * download actually transfers live on `storage_objects.stored_size_bytes`
 * (NULL for verbatim objects). Fails OPEN: any error (including a DB that
 * predates the column) just returns the rows without stored sizes, so the
 * page renders plain sizes rather than breaking.
 */
async function attachStoredSizes(
  supabase: Awaited<ReturnType<typeof createClient>>,
  images: NircamImage[],
  field?: string,
): Promise<NircamImage[]> {
  if (images.length === 0) return images;
  try {
    const { data, error } = await paginateQuery<{
      storage_key: string; stored_size_bytes: number | null;
    }>(
      () => {
        let q = supabase
          .from('storage_objects')
          .select('storage_key, stored_size_bytes')
          .in('product_type', MOSAIC_TYPES)
          .eq('status', 'active')
          .not('stored_size_bytes', 'is', null);
        if (field) q = q.eq('field', field);
        return q.order('storage_key');
      },
    );
    if (error || !data || data.length === 0) return images;

    const storedByKey = new Map(
      data.map((r) => [r.storage_key, r.stored_size_bytes as number]),
    );
    return images.map((img) => {
      const stored = storedByKey.get(img.file_path);
      return stored != null ? { ...img, file_size_stored: stored } : img;
    });
  } catch (err) {
    console.error('Error attaching stored sizes (continuing without):', err);
    return images;
  }
}

/**
 * Render an arcsec/pix scale in the same vocabulary the mosaic Scale column uses
 * ('30mas'), so expmap and mosaic rows read as one column: 0.5 -> '500mas'.
 * Returns null for a null/non-finite input so the cell falls back to an em-dash.
 */
function formatPixelScale(arcsec: unknown): string | null {
  if (typeof arcsec !== 'number' || !Number.isFinite(arcsec) || arcsec <= 0) return null;
  return `${Math.round(arcsec * 1000)}mas`;
}

/** The exposure-map grid scale of each instrument of a field, formatted, from
 *  a `cat_fields` row: NIRCam's is the typed column, another instrument's is in
 *  `instrument_coverage` (the SQL field_coverage() map, rebuilt here because
 *  this reads the view directly). */
function expmapScales(row: { expmap_pixel_scale_arcsec?: unknown; instrument_coverage?: unknown } | null | undefined): Map<string, string | null> {
  const out = new Map<string, string | null>();
  if (!row) return out;
  out.set('nircam', formatPixelScale(row.expmap_pixel_scale_arcsec));
  const other = (row.instrument_coverage ?? {}) as Record<string, { pixel_scale_arcsec?: unknown } | null>;
  for (const [instrument, entry] of Object.entries(other)) {
    if (instrument !== 'nircam') out.set(instrument, formatPixelScale(entry?.pixel_scale_arcsec));
  }
  return out;
}

/**
 * Fetch the per-(field, instrument, filter) exposure-coverage maps a user may see.
 *
 * Expmaps are registered in `storage_objects` (product_type `<instrument>_expmap`) and
 * their visibility rides the owning field deployment via RLS: a published field
 * deployment is public to everyone, drafts stay admin-only. We simply select
 * active rows and let `select_storage_objects_by_access` do the gating — no
 * bespoke access logic here, mirroring how mosaics rely on `mosaics` RLS.
 */
export async function getNircamExpmaps(field?: string): Promise<NircamExpmapsResult> {
  const { user, supabase } = await getRequestIdentity();
  if (!user) {
    return { expmaps: [], isAuthenticated: false };
  }

  try {
    // Under a release (P6c-2) the expmaps are the release's archived members
    // of the field (the manifest, under RLS, is the authorization); a field
    // is required because the manifest is per component.
    const dr = await requestDrTag();
    if (dr) {
      if (!field) return { expmaps: [], isAuthenticated: true };
      // every imaging instrument's component of the field
      const members = await releaseFieldMembers(supabase, dr, field, EXPMAP_TYPES);
      const { data: fieldRow } = await supabase.from('cat_fields').select('name, expmap_pixel_scale_arcsec, instrument_coverage').eq('name', field).maybeSingle();
      const scales = expmapScales(fieldRow);
      const expmaps: NircamExpmap[] = [];
      for (const m of members.values()) {
        if (!m) continue;
        let filt: string | undefined;
        try { filt = parseKey(m.storageKey).scope.filt as string | undefined; } catch { filt = undefined; }
        if (!filt) continue;
        const instrument = instrumentOfType(m.productType);
        expmaps.push({ field, instrument, filter: filt, storage_key: m.storageKey, file_size: m.sizeBytes ?? undefined, pixel_scale: scales.get(instrument) ?? null });
      }
      expmaps.sort((a, b) =>
        instrumentRank(a.instrument) - instrumentRank(b.instrument)
        || a.filter.localeCompare(b.filter));
      return { expmaps, isAuthenticated: true };
    }

    const { data, error } = await paginateQuery<{
      field: string; filter: string | null; storage_key: string;
      size_bytes: number | null; product_type: string;
    }>(
      () => {
        let q = supabase
          .from('storage_objects')
          .select('field, filter, storage_key, size_bytes, product_type')
          .in('product_type', EXPMAP_TYPES)
          .eq('status', 'active');
        if (field) q = q.eq('field', field);
        return q
          .order('field', { ascending: true })
          .order('product_type', { ascending: true })
          .order('filter', { ascending: true });
      },
    );

    if (error) {
      console.error('Error fetching NIRCam expmaps:', error);
      return { expmaps: [], error: error.message, isAuthenticated: true };
    }

    // Grid scale per field and instrument. An expmap FITS records its scale only
    // in CDELT1/2 and the registry row has no scale axis, so it comes from the
    // `fields` registry, where deploy lifts it out of that instrument's
    // <field>_layout.json. One value per instrument of a field — expmap builds a
    // single shared WCS across all of an instrument's filters. A failure here is
    // non-fatal: the rows still render, just without a Scale.
    let scaleQuery = supabase.from('cat_fields').select('name, expmap_pixel_scale_arcsec, instrument_coverage');
    if (field) scaleQuery = scaleQuery.eq('name', field);
    const { data: fieldRows } = await scaleQuery;
    const scalesByField = new Map(
      (fieldRows || []).map((f) => [f.name as string, expmapScales(f)])
    );

    const expmaps: NircamExpmap[] = data
      .filter((r) => r.filter)  // per-filter product; skip any unscoped row
      .map((r) => {
        const instrument = instrumentOfType(r.product_type);
        return {
          field: r.field,
          instrument,
          filter: r.filter as string,
          storage_key: r.storage_key,
          file_size: r.size_bytes ?? undefined,
          pixel_scale: scalesByField.get(r.field)?.get(instrument) ?? null,
        };
      })
      .sort((a, b) =>
        a.field.localeCompare(b.field)
        || instrumentRank(a.instrument) - instrumentRank(b.instrument)
        || a.filter.localeCompare(b.filter));

    return { expmaps, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam expmaps:', err);
    return { expmaps: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * The /imaging landing grid: one card per field the caller can see any mosaic
 * of (get_nircam_fields RPC — SECURITY INVOKER, so mosaics RLS scopes
 * non-admins to published data). Each row's layout key is presigned into a
 * ready-to-render <img> URL here so the client never handles storage keys.
 */
export async function getNircamFields(): Promise<NircamFieldsResult> {
  const { user, supabase } = await getRequestIdentity();
  if (!user) {
    return { fields: [], isAuthenticated: false };
  }

  try {
    const { data, error } = await supabase.rpc('get_nircam_fields');
    if (error) {
      console.error('Error fetching NIRCam fields:', error);
      return { fields: [], error: error.message, isAuthenticated: true };
    }

    const rows = (data ?? []) as (Omit<NircamFieldCard, 'layout_url'> & {
      layout_key: string | null;
    })[];

    // Presign the layout plots in one dual-read batch; a failed presign just
    // leaves that card's preview null (the card renders a placeholder).
    const dr = await requestDrTag();
    let fields: NircamFieldCard[];
    if (dr) {
      // Under a release the card's layout is the release's ARCHIVED layout
      // member of that field — decided on the manifest row's state, never on
      // the RPC's layout_key (which is the live key while the member is
      // pending). No archived layout: no preview (P6c-2: never a live file).
      // A field's layout is the first instrument's the release holds, NIRCam
      // first (the live rule, get_nircam_fields).
      const layoutByField = new Map<string, ReleaseMember>();
      try {
        const members = await listReleaseMembersIn(supabase, dr, {
          components: rows.flatMap((r) => IMAGING_INSTRUMENTS.map((i) => `${i}/${r.field}`)),
          productTypes: LAYOUT_TYPES,
        });
        for (const m of members) {
          const slash = m.component.indexOf('/');
          const fieldName = m.component.slice(slash + 1);
          const held = layoutByField.get(fieldName);
          if (!held || instrumentRank(instrumentOfType(m.productType)) < instrumentRank(instrumentOfType(held.productType))) {
            layoutByField.set(fieldName, m);
          }
        }
      } catch (err) {
        console.error('Error listing the release layouts:', err);
      }
      const byKey = new Map<string, ReleaseMember | null>([...layoutByField.values()].map((m) => [m.storageKey, m]));
      const urlByKey = await releaseDownloadUrls(byKey, IMG_PRESIGN_TTL_SECONDS);
      fields = rows.map(({ layout_key: _lk, ...r }) => {
        void _lk;
        const m = layoutByField.get(r.field);
        return { ...r, layout_url: m ? urlByKey.get(m.storageKey) ?? null : null };
      });
    } else {
      const keys = rows.map((r) => r.layout_key).filter((k): k is string => !!k);
      const urlByKey = new Map<string, string>();
      if (keys.length > 0) {
        try {
          const urls = await generateDownloadUrls(keys, IMG_PRESIGN_TTL_SECONDS);
          keys.forEach((k, i) => urlByKey.set(k, urls[i]));
        } catch (err) {
          console.error('Error presigning NIRCam layout plots:', err);
        }
      }
      fields = rows.map(({ layout_key, ...r }) => ({
        ...r,
        layout_url: layout_key ? urlByKey.get(layout_key) ?? null : null,
      }));
    }

    return { fields, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam fields:', err);
    return { fields: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * The /imaging/[field] overview (get_nircam_field_summary RPC). Returns null
 * for a field the caller can't see any mosaic of — the page treats unknown
 * and unauthorized fields identically.
 */
export async function getNircamFieldSummary(field: string): Promise<NircamFieldSummaryResult> {
  const { user, supabase } = await getRequestIdentity();
  if (!user) {
    return { summary: null, isAuthenticated: false };
  }

  try {
    const { data, error } = await supabase.rpc('get_nircam_field_summary', {
      p_field: field,
    });
    if (error) {
      console.error('Error fetching NIRCam field summary:', error);
      return { summary: null, error: error.message, isAuthenticated: true };
    }

    const row = ((data ?? []) as (Omit<NircamFieldSummary,
      'cfpipe_version' | 'jwst_version' | 'crds_context'> & { layout_key: string | null })[])[0];
    if (!row) return { summary: null, isAuthenticated: true };

    // layout_key is presigned separately by getNircamFieldImages; strip it.
    const { layout_key: _lk, ...base } = row;
    void _lk;

    // Reduction provenance from the latest published deployment of each
    // instrument (the deployments log is readable by all authenticated
    // users; one deploy is one instrument's products of the field). Fail
    // open — a missing row just leaves the provenance rows off the overview.
    let provenance: ImagingProvenance = { cfpipe_version: null, jwst_version: null, crds_context: null };
    // Under a release the deployments log is live-only; the frozen summary
    // carries no provenance until P6c-2.
    if (await requestDrTag()) return { summary: { ...base, ...provenance }, isAuthenticated: true };
    const byInstrument: Record<string, ImagingProvenance> = {};
    try {
      const { data: deps } = await supabase
        .from('deployments')
        .select('instrument, cfpipe_version, jwst_version, crds_context')
        .eq('field', field)
        .eq('status', 'published')
        .order('deployed_at', { ascending: false })
        .limit(200);
      for (const d of deps ?? []) {
        const inst = (d.instrument as string | null) ?? 'nircam';
        // newest first: the first row seen for an instrument is its latest
        if (!(inst in byInstrument)) {
          byInstrument[inst] = {
            cfpipe_version: d.cfpipe_version, jwst_version: d.jwst_version, crds_context: d.crds_context,
          };
        }
      }
      // The summary's own three fields stay NIRCam's (another instrument's
      // deploy must not replace them); a field without NIRCam shows the
      // first instrument it has.
      const first = IMAGING_INSTRUMENTS.find((i) => i in byInstrument);
      if (first) provenance = byInstrument[first];
    } catch (err) {
      console.error('Error fetching deployment provenance (continuing):', err);
    }

    return {
      summary: { ...base, ...provenance, provenance_by_instrument: byInstrument },
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam field summary:', err);
    return { summary: null, error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * Presigned <img> URLs for one field's plot products: the field layout PNG,
 * the per-filter dark expmap plots, and the per-mosaic thumbnails.
 *
 * The key set is derived entirely server-side from `storage_objects` under the
 * caller's RLS session (published deployments visible to all authed users,
 * drafts admin-only), so no client-supplied key is ever presigned. Raw
 * presigned GET URLs go straight into <img> tags — browsers don't apply CORS
 * to image rendering, so no proxy hop is needed (the same pattern as the
 * admin exposure previews).
 */
export async function getNircamFieldImages(field: string): Promise<NircamFieldImagesResult> {
  const empty: NircamFieldImagesResult = { layoutUrl: null, expmapPlots: {}, thumbnails: {}, quicklooks: {} };

  const { user, supabase } = await getRequestIdentity();
  if (!user) return empty;

  try {
    // Under a release (P6c-2): the field's archived members, presigned on
    // their archive keys; `downloadable` tells the table which FITS rows the
    // release holds archived.
    const dr = await requestDrTag();
    if (dr) {
      const fitsTypes = [...MOSAIC_TYPES, ...EXPMAP_TYPES];
      const byKey = await releaseFieldMembers(supabase, dr, field, [
        ...LAYOUT_TYPES, ...EXPMAP_PLOT_TYPES, ...THUMBNAIL_TYPES, ...QUICKLOOK_TYPES,
        ...fitsTypes,
      ]);
      const images = new Map([...byKey].filter(([, m]) => m && !fitsTypes.includes(m.productType)));
      const urlByKey = await releaseDownloadUrls(images, IMG_PRESIGN_TTL_SECONDS);
      const result: NircamFieldImagesResult = {
        layoutUrl: null, expmapPlots: {}, thumbnails: {}, quicklooks: {},
        downloadable: [...byKey.values()]
          .filter((m): m is ReleaseMember => !!m && fitsTypes.includes(m.productType))
          .map((m) => m.storageKey),
      };
      let layoutRank = Infinity;
      for (const [key, m] of images) {
        const url = urlByKey.get(key);
        if (!url || !m) continue;
        const instrument = instrumentOfType(m.productType);
        if (LAYOUT_TYPES.includes(m.productType)) {
          // the first instrument's layout the release holds, NIRCam first
          if (instrumentRank(instrument) < layoutRank) {
            layoutRank = instrumentRank(instrument);
            result.layoutUrl = url;
          }
        } else if (EXPMAP_PLOT_TYPES.includes(m.productType)) {
          let filt: string | undefined;
          try { filt = parseKey(key).scope.filt as string | undefined; } catch { filt = undefined; }
          if (filt) result.expmapPlots[expmapPlotKey(instrument, filt)] = url;
        } else if (THUMBNAIL_TYPES.includes(m.productType)) result.thumbnails[thumbnailBase(key)] = url;
        else if (QUICKLOOK_TYPES.includes(m.productType)) result.quicklooks[quicklookBase(key)] = url;
      }
      return result;
    }

    const { data, error } = await paginateQuery<{
      storage_key: string; product_type: string; filter: string | null;
    }>(
      () => supabase
        .from('storage_objects')
        .select('storage_key, product_type, filter')
        .eq('field', field)
        .eq('status', 'active')
        .in('product_type', [...LAYOUT_TYPES, ...EXPMAP_PLOT_TYPES, ...THUMBNAIL_TYPES, ...QUICKLOOK_TYPES])
        .order('storage_key'),
    );

    if (error) {
      console.error('Error fetching NIRCam field images:', error);
      return { ...empty, error: error.message };
    }
    if (data.length === 0) return empty;

    const keys = data.map((r) => r.storage_key);
    const urls = await generateDownloadUrls(keys, IMG_PRESIGN_TTL_SECONDS);
    const urlByKey = new Map(keys.map((k, i) => [k, urls[i]]));

    const result: NircamFieldImagesResult = { layoutUrl: null, expmapPlots: {}, thumbnails: {}, quicklooks: {} };
    let layoutRank = Infinity;
    for (const row of data) {
      const url = urlByKey.get(row.storage_key);
      if (!url) continue;
      const instrument = instrumentOfType(row.product_type);
      if (LAYOUT_TYPES.includes(row.product_type)) {
        // the first instrument's layout, NIRCam first (get_nircam_fields' rule)
        if (instrumentRank(instrument) < layoutRank) {
          layoutRank = instrumentRank(instrument);
          result.layoutUrl = url;
        }
      } else if (EXPMAP_PLOT_TYPES.includes(row.product_type) && row.filter) {
        result.expmapPlots[expmapPlotKey(instrument, row.filter)] = url;
      } else if (THUMBNAIL_TYPES.includes(row.product_type)) {
        // Keyed by mosaic base — the shared contract with the table's
        // mosaicBase() lives in lib/nircam-product-keys.ts.
        result.thumbnails[thumbnailBase(row.storage_key)] = url;
      } else if (QUICKLOOK_TYPES.includes(row.product_type)) {
        result.quicklooks[quicklookBase(row.storage_key)] = url;
      }
    }

    return result;
  } catch (err) {
    console.error('Unexpected error fetching NIRCam field images:', err);
    return { ...empty, error: 'An unexpected error occurred' };
  }
}
