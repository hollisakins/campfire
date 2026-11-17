'use client';

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import Link from 'next/link';
import { DrLink, useDrTag } from '@/lib/dr/client';
import { SignInLink } from '@/components/auth/SignInLink';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { NircamTable } from '@/components/nircam/NircamTable';
import {
  NircamFilterBar,
  NircamFilterOptions,
  DEFAULT_NIRCAM_FILTERS,
} from '@/components/nircam/NircamFilterBar';
import { CurlScriptGenerator } from '@/components/nircam/CurlScriptGenerator';
import { FieldSelectorDropdown } from '@/components/nircam/FieldSelectorDropdown';
import { ExpmapBrowser } from '@/components/nircam/ExpmapBrowser';
import {
  getNircamImages,
  getNircamExpmaps,
  getNircamFields,
  getNircamFieldSummary,
  getNircamFieldImages,
  type NircamFieldImagesResult,
} from '@/lib/actions/nircam';
import { getFitsglDatasets } from '@/lib/actions/map';
import { expmapPlotKey } from '@/lib/nircam-product-keys';
import {
  IMAGING_INSTRUMENTS,
  imagingInstrumentLabel,
  type NircamFieldCard,
  type NircamFieldSummary,
  type NircamProductRow,
} from '@/lib/types';
import { LogIn, Loader2, ImageIcon, ExternalLink, Scissors } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

const formatVolume = (bytes: number): string => {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

const formatArea = (arcmin2: number | null | undefined, deg2: number | null | undefined): string | null => {
  if (deg2 != null && deg2 >= 0.05) return `${deg2.toFixed(2)} deg²`;
  if (arcmin2 != null) return `${Math.round(arcmin2)} arcmin²`;
  return null;
};

// One instrument's sky coverage of the field. NIRCam's is the summary's typed
// pair (the `coverage` map carries the same numbers); another instrument's is
// in the map only.
const coverageOf = (s: NircamFieldSummary, instrument: string): string | null => {
  const c = s.coverage?.[instrument];
  if (c) return formatArea(c.area_arcmin2, c.area_deg2);
  return instrument === 'nircam' ? formatArea(s.coverage_area_arcmin2, s.coverage_area_deg2) : null;
};

// NIRCam SW/LW split: short-wavelength filters sit below the ~2.4 µm channel
// boundary (F090W…F210M), long-wavelength above (F250M…F480M).
const filterChannelSplit = (filters: string[]): { sw: number; lw: number } => {
  let sw = 0;
  let lw = 0;
  for (const f of filters) {
    const n = parseInt(f.slice(1, 4), 10);
    if (Number.isNaN(n)) continue;
    if (n < 240) sw += 1;
    else lw += 1;
  }
  return { sw, lw };
};

// Alphanumeric tile sort (A1, A2, A10, B1 …).
const tileSort = (a: string, b: string): number => {
  const aMatch = a.match(/^([A-Z]+)(\d+)$/);
  const bMatch = b.match(/^([A-Z]+)(\d+)$/);
  if (aMatch && bMatch) {
    const [, aLetter, aNumber] = aMatch;
    const [, bLetter, bNumber] = bMatch;
    if (aLetter !== bLetter) return aLetter.localeCompare(bLetter);
    return parseInt(aNumber, 10) - parseInt(bNumber, 10);
  }
  return a.localeCompare(b);
};

// Extension facet order: science products first, the folded-in expmaps last.
const EXT_ORDER = ['sci', 'err', 'wht', 'rms', 'srcmask', 'exp'];
const extSort = (a: string, b: string): number => {
  const ai = EXT_ORDER.indexOf(a.toLowerCase());
  const bi = EXT_ORDER.indexOf(b.toLowerCase());
  if (ai === -1 && bi === -1) return a.localeCompare(b);
  if (ai === -1) return 1;
  if (bi === -1) return -1;
  return ai - bi;
};

// Scale facet order: finest grid first, numerically — a plain string sort would
// put the expmaps' '500mas' before a mosaic's '30mas'.
const scaleSort = (a: string, b: string): number => {
  const mas = (s: string) => parseFloat(s);
  const [am, bm] = [mas(a), mas(b)];
  if (Number.isNaN(am) || Number.isNaN(bm)) return a.localeCompare(b);
  return am - bm;
};

interface NircamFieldPageContentProps {
  field: string;
}

export function NircamFieldPageContent({ field }: NircamFieldPageContentProps) {
  const { user, loading: authLoading } = useAuth();
  // Release browsing: the summary and the mosaic list come from the frozen
  // rows; images, exposure maps and downloads are the release's archived
  // copies (P6c-2); the map bridge, cutouts and the download script are live-only.
  const drTag = useDrTag();
  const readOnly = drTag !== null;

  const [summary, setSummary] = useState<NircamFieldSummary | null>(null);
  const [allFields, setAllFields] = useState<NircamFieldCard[]>([]);
  const [products, setProducts] = useState<NircamProductRow[]>([]);
  const [fieldImages, setFieldImages] = useState<NircamFieldImagesResult>({
    layoutUrl: null,
    expmapPlots: {},
    thumbnails: {},
    quicklooks: {},
  });
  const [viewableFilters, setViewableFilters] = useState<Set<string>>(new Set());
  const [hasCutouts, setHasCutouts] = useState(false);
  const [filters, setFilters] = useState<NircamFilterOptions>(DEFAULT_NIRCAM_FILTERS);
  const [selectedProducts, setSelectedProducts] = useState<NircamProductRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Field-scoped fetch. The dropdown switches fields on the SAME mounted
  // component (prop change, no remount), so the effect guards against a
  // stale response: a slower field-A reply resolving after field-B must not
  // overwrite B's rendered state (claude-review on #379).
  useEffect(() => {
    if (authLoading) return;
    let stale = false;

    const fetchData = async () => {
      setLoading(true);
      setError(null);
      setFilters(DEFAULT_NIRCAM_FILTERS);

      try {
      const [summaryRes, imagesRes, expmapsRes, imagesUrlsRes, fieldsRes, fitsglDatasets] =
        await Promise.all([
          getNircamFieldSummary(field),
          getNircamImages(field),
          getNircamExpmaps(field),
          getNircamFieldImages(field),
          getNircamFields(),
          readOnly ? Promise.resolve([]) : getFitsglDatasets(field).catch(() => []),
        ]);
      if (stale) return;

      if (summaryRes.error) {
        setError(summaryRes.error);
      }
      setSummary(summaryRes.summary);
      setAllFields(fieldsRes.error ? [] : fieldsRes.fields);
      setFieldImages(imagesUrlsRes);

      // Merge mosaics + expmaps into the single products list. Expmaps ride as
      // a synthetic 'exp' extension with no tile axis; their scale is the
      // field's expmap grid (one shared WCS across every filter), not a
      // per-product axis like a mosaic's. Each mosaic tile also carries its own
      // full-resolution 'exp' extension (_exp.fits: EXPTIME + NEXP), so 'exp'
      // spans both scales: per tile at the mosaic scale, field-wide coarse.
      const mosaicRows: NircamProductRow[] = (imagesRes.error ? [] : imagesRes.images).map(
        (img) => ({
          kind: 'mosaic',
          instrument: img.instrument ?? 'nircam',
          field: img.field,
          filter: img.filter,
          tile: img.tile,
          pixel_scale: img.pixel_scale,
          extension: img.extension,
          epoch: img.epoch ?? '',
          file_path: img.file_path,
          file_size: img.file_size,
          file_size_stored: img.file_size_stored,
        }),
      );
      const expmapRows: NircamProductRow[] = (expmapsRes.error ? [] : expmapsRes.expmaps).map(
        (e) => ({
          kind: 'expmap',
          instrument: e.instrument,
          field: e.field,
          filter: e.filter,
          tile: null,
          pixel_scale: e.pixel_scale,
          extension: 'exp',
          file_path: e.storage_key,
          file_size: e.file_size,
        }),
      );
      setProducts([...mosaicRows, ...expmapRows]);
      if (imagesRes.error) setError(imagesRes.error);

      // Filters covered by a FitsGL dataset enable the per-row map View; a
      // field-composite dataset also unlocks the cutout tool.
      const bands = new Set<string>();
      for (const ds of fitsglDatasets) {
        for (const b of ds.bands ?? []) bands.add(b.toLowerCase());
      }
      setViewableFilters(bands);
      setHasCutouts(fitsglDatasets.some((ds) => ds.kind === 'field'));
      } catch (err) {
        if (stale) return;
        setError('Failed to fetch data');
        console.error(err);
      } finally {
        if (!stale) setLoading(false);
      }
    };

    fetchData();
    return () => {
      stale = true;
    };
  }, [authLoading, field, user, readOnly]);

  const handleSelectionChange = useCallback((selected: NircamProductRow[]) => {
    setSelectedProducts(selected);
  }, []);

  // Facet options, derived from the field-scoped products themselves.
  const available = useMemo(() => {
    const mosaics = products.filter((p) => p.kind === 'mosaic');
    return {
      // in the fixed instrument order (NIRCam first), not alphabetical
      instruments: IMAGING_INSTRUMENTS.filter((i) => products.some((p) => p.instrument === i)) as string[],
      tiles: [...new Set(mosaics.map((p) => p.tile as string))].sort(tileSort),
      filters: [...new Set(products.map((p) => p.filter))].sort(),
      // Every product with a scale, not just mosaics — expmaps now carry the
      // field's grid scale, so the facet has to offer it or the Scale filter
      // could never match a row the Scale column visibly labels.
      pixel_scales: [...new Set(
        products.map((p) => p.pixel_scale).filter((s): s is string => s !== null),
      )].sort(scaleSort),
      extensions: [...new Set(products.map((p) => p.extension))].sort(extSort),
      epochs: [...new Set(mosaics.map((p) => p.epoch ?? ''))].sort(),
    };
  }, [products]);

  // Transfer estimate: what a download actually moves — stored (gzipped)
  // bytes when recorded, logical bytes otherwise.
  const selectedBytes = useMemo(
    () => selectedProducts.reduce((s, p) => s + (p.file_size_stored ?? p.file_size ?? 0), 0),
    [selectedProducts],
  );

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Imaging', href: readOnly ? `/imaging?dr=${encodeURIComponent(drTag as string)}` : '/imaging' },
            { label: field.toUpperCase() },
          ]}
          className="mb-6"
        />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view images
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Access to imaging data requires authentication. Please sign in with your
            CAMPFIRE account to browse and download images.
          </p>
          <SignInLink
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </SignInLink>
        </div>
      </div>
    );
  }

  const displayName = summary?.display_name ?? field.toUpperCase();
  // The instruments with mosaics of the field, NIRCam first. A field with
  // NIRCam alone reads exactly as it did before the HST instruments.
  const instruments = IMAGING_INSTRUMENTS.filter(
    (i) => (summary?.instruments ?? ['nircam']).includes(i),
  ) as string[];
  const nircamOnly = instruments.length === 1 && instruments[0] === 'nircam';
  // Filters per instrument, from the mosaic rows (the summary's list is the
  // union); the SW / LW split is NIRCam's.
  const filtersOf = (inst: string) =>
    [...new Set(products.filter((p) => p.instrument === inst).map((p) => p.filter))].sort();
  const nircamFilters = nircamOnly && summary ? summary.filters : filtersOf('nircam');
  const channels = summary && nircamFilters.length > 0 ? filterChannelSplit(nircamFilters) : null;
  // The exposure-map panel: one tab per (instrument, filter), instruments in
  // their fixed order. With one instrument the tabs read as they always did;
  // with several, each instrument's tabs sit under its name.
  const expmapTabs = instruments.flatMap((inst) =>
    (inst === 'nircam' ? nircamFilters : filtersOf(inst)).map((f) => ({
      key: expmapPlotKey(inst, f),
      label: f.toUpperCase(),
      group: instruments.length > 1 ? imagingInstrumentLabel(inst) : undefined,
    })));
  // Under a release the panel shows only when the release holds a plot.
  const showExpmaps = expmapTabs.length > 0
    && (!readOnly || Object.keys(fieldImages.expmapPlots).length > 0);
  // Sky coverage per instrument (each has its own exposures and footprint).
  const coverageRows: [string, React.ReactNode][] = summary
    ? (nircamOnly
        ? [['Sky coverage', coverageOf(summary, 'nircam') ?? '—']]
        : instruments.map((inst): [string, React.ReactNode] => [
            instruments.length > 1 ? `Sky coverage (${imagingInstrumentLabel(inst)})` : 'Sky coverage',
            coverageOf(summary, inst) ?? '—',
          ]))
    : [];
  const namedEpochs = summary ? summary.epochs.filter((e) => e !== '') : [];
  const provenanceRows: [string, React.ReactNode][] = [];
  if (summary) {
    const byInstrument = summary.provenance_by_instrument
      ?? { nircam: { cfpipe_version: summary.cfpipe_version, jwst_version: summary.jwst_version, crds_context: summary.crds_context } };
    for (const inst of instruments) {
      const p = byInstrument[inst];
      if (!p) continue;
      const tag = nircamOnly ? '' : ` (${imagingInstrumentLabel(inst)})`;
      if (p.cfpipe_version) {
        provenanceRows.push([`Pipeline${tag}`,
          <span key={`cfp-${inst}`} className="font-mono text-xs break-all">cfpipe {p.cfpipe_version}</span>]);
      }
      if (p.jwst_version) {
        provenanceRows.push([inst === 'nircam' ? `jwst${tag}` : `Calibration software${tag}`,
          <span key={`cal-${inst}`} className="font-mono text-xs">{p.jwst_version}</span>]);
      }
      if (p.crds_context) {
        provenanceRows.push([`CRDS context${tag}`,
          <span key={`crds-${inst}`} className="font-mono text-xs">{p.crds_context}</span>]);
      }
    }
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'Imaging', href: readOnly ? `/imaging?dr=${encodeURIComponent(drTag as string)}` : '/imaging' },
          { label: displayName },
        ]}
        className="mb-6"
      />

      {/* Field selector — top-left, above the field header */}
      <div className="mb-4">
        <FieldSelectorDropdown fields={allFields} current={field} />
      </div>

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary">Loading {displayName}...</span>
        </div>
      )}

      {/* Error State */}
      {error && !loading && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Unknown / inaccessible field */}
      {!loading && !error && !summary && (
        <div className="text-center py-16 bg-card border border-border rounded-lg">
          <ImageIcon className="w-12 h-12 text-text-secondary mx-auto mb-4" />
          <p className="text-text-secondary">
            No imaging data available for “{field}”.
          </p>
          <DrLink
            href="/imaging"
            className="inline-block mt-3 text-sm text-primary hover:text-primary-hover hover:underline"
          >
            ← All fields
          </DrLink>
        </div>
      )}

      {!loading && summary && (
        <>
          {/* Field header + map bridge */}
          <div className="flex items-start gap-4 flex-wrap mb-6">
            <h1 className="text-2xl font-bold text-text-primary">
              {displayName} {nircamOnly ? 'NIRCam Imaging' : 'Imaging'}
            </h1>
            <div className="flex-1" />
            {!readOnly && (
              <div className="flex items-center gap-2">
                {hasCutouts && (
                  <Link
                    href={`/imaging/${encodeURIComponent(field)}/cutouts`}
                    className="inline-flex items-center gap-2 px-4 py-2 bg-surface-2 border border-border-strong text-text-primary rounded-lg text-sm font-medium hover:bg-card-hover transition-colors"
                  >
                    <Scissors className="w-3.5 h-3.5" />
                    Cutouts
                  </Link>
                )}
                {/* A field with NIRCam always had the link; a field without
                    it gets one once its bands are in a map dataset. */}
                {(nircamFilters.length > 0 || viewableFilters.size > 0) && (
                  <Link
                    href={`/map?field=${encodeURIComponent(field)}`}
                    className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-on-primary rounded-lg text-sm font-medium hover:bg-primary-hover transition-colors"
                  >
                    Open field in Map
                    <ExternalLink className="w-3.5 h-3.5" />
                  </Link>
                )}
              </div>
            )}
          </div>

          {/* Field overview: metadata + exposure-map browser */}
          <div className="bg-card border border-border rounded-xl p-5 mb-7">
            <div className={showExpmaps ? 'grid gap-6 md:grid-cols-[2fr_3fr]' : ''}>
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary mb-3">
                  Field overview
                </h3>
                <div className="text-[13px]">
                  {(
                    [
                      ...(nircamOnly
                        ? []
                        : [['Instruments', instruments.map(imagingInstrumentLabel).join(' · ')]]),
                      [
                        'Filters',
                        !nircamOnly && instruments.length > 1 ? (
                          <>
                            {instruments.reduce((n, i) => n + filtersOf(i).length, 0)}{' '}
                            <span className="text-text-tertiary">
                              ({instruments.map((i) => `${imagingInstrumentLabel(i)} ${filtersOf(i).length}`).join(' · ')})
                            </span>
                          </>
                        ) : channels ? (
                          <>
                            {summary.filters.length}{' '}
                            <span className="text-text-tertiary">
                              ({channels.sw} SW · {channels.lw} LW)
                            </span>
                          </>
                        ) : (
                          summary.filters.length
                        ),
                      ],
                      ['Tiles', summary.tiles.length],
                      [
                        'Pixel scale',
                        <span key="ps" className="font-mono">
                          {summary.pixel_scales.join(' · ') || '—'}
                        </span>,
                      ],
                      [
                        'Extensions',
                        <span key="ext" className="font-mono text-xs">
                          {/* RPC aggregates alphabetically; show science-first */}
                          {[...summary.extensions].sort(extSort).join(' · ') || '—'}
                        </span>,
                      ],
                      [
                        'Epochs',
                        namedEpochs.length > 0
                          ? `full field + ${namedEpochs.length}`
                          : 'full field',
                      ],
                      ['Mosaic files', summary.n_files.toLocaleString()],
                      ...coverageRows,
                      ['Data volume', formatVolume(summary.total_bytes)],
                      // Reduction provenance (latest published deployment)
                      ...(summary.last_updated
                        ? [['Last reduced', summary.last_updated.slice(0, 10)]]
                        : []),
                      ...provenanceRows,
                    ] as [string, React.ReactNode][]
                  ).map(([k, v], i, arr) => (
                    <div
                      key={k}
                      className={`flex justify-between gap-3 py-1.5 ${
                        i < arr.length - 1 ? 'border-b border-border' : ''
                      }`}
                    >
                      <span className="text-text-tertiary">{k}</span>
                      <span className="font-medium text-text-primary text-right">{v}</span>
                    </div>
                  ))}
                </div>
              </div>

              {showExpmaps && (
                <ExpmapBrowser
                  tabs={expmapTabs}
                  expmapPlots={fieldImages.expmapPlots}
                />
              )}
            </div>
          </div>

          {/* Data products */}
          <div className="flex items-center gap-3 mb-3">
            <h2 className="text-lg font-semibold text-text-primary">Data products</h2>
            <span className="text-sm text-text-secondary">
              {products.length.toLocaleString()} files · {formatVolume(summary.total_bytes)}
            </span>
          </div>

          <div className="mb-4 flex flex-wrap items-center gap-3">
            <NircamFilterBar
              filterState={filters}
              onFiltersChange={setFilters}
              hideFieldFacet
              availableInstruments={available.instruments}
              availableTiles={available.tiles}
              availableFilters={available.filters}
              availablePixelScales={available.pixel_scales}
              availableExtensions={available.extensions}
              availableEpochs={available.epochs}
            />
            <div className="flex-1" />
            <span className="text-xs font-mono text-text-tertiary">
              Selected {selectedProducts.length.toLocaleString()} of{' '}
              {products.length.toLocaleString()} · {formatVolume(selectedBytes)}
            </span>
          </div>

          {!readOnly && (
            <div className="mb-4">
              <CurlScriptGenerator selectedImages={selectedProducts} />
            </div>
          )}

          {products.length === 0 ? (
            <div className="text-center py-16 bg-card border border-border rounded-lg">
              <ImageIcon className="w-12 h-12 text-text-secondary mx-auto mb-4" />
              <p className="text-text-secondary">No data products available yet.</p>
            </div>
          ) : (
            <NircamTable
              products={products}
              downloadable={fieldImages.downloadable ?? null}
              filters={filters}
              thumbnails={fieldImages.thumbnails}
              quicklooks={fieldImages.quicklooks}
              viewableFilters={viewableFilters}
              onSelectionChange={handleSelectionChange}
            />
          )}
        </>
      )}
    </div>
  );
}
