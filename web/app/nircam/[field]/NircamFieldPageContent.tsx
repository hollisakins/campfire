'use client';

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import Link from 'next/link';
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
import type {
  NircamFieldCard,
  NircamFieldSummary,
  NircamProductRow,
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

const formatCoverage = (s: NircamFieldSummary): string => {
  if (s.coverage_area_deg2 != null && s.coverage_area_deg2 >= 0.05) {
    return `${s.coverage_area_deg2.toFixed(2)} deg²`;
  }
  if (s.coverage_area_arcmin2 != null) {
    return `${Math.round(s.coverage_area_arcmin2)} arcmin²`;
  }
  return '—';
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

interface NircamFieldPageContentProps {
  field: string;
}

export function NircamFieldPageContent({ field }: NircamFieldPageContentProps) {
  const { user, loading: authLoading } = useAuth();

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
          getFitsglDatasets(field).catch(() => []),
        ]);
      if (stale) return;

      if (summaryRes.error) {
        setError(summaryRes.error);
      }
      setSummary(summaryRes.summary);
      setAllFields(fieldsRes.error ? [] : fieldsRes.fields);
      setFieldImages(imagesUrlsRes);

      // Merge mosaics + expmaps into the single products list. Expmaps ride as
      // a synthetic 'exp' extension with no tile/scale axes.
      const mosaicRows: NircamProductRow[] = (imagesRes.error ? [] : imagesRes.images).map(
        (img) => ({
          kind: 'mosaic',
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
          field: e.field,
          filter: e.filter,
          tile: null,
          pixel_scale: null,
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
  }, [authLoading, field, user]);

  const handleSelectionChange = useCallback((selected: NircamProductRow[]) => {
    setSelectedProducts(selected);
  }, []);

  // Facet options, derived from the field-scoped products themselves.
  const available = useMemo(() => {
    const mosaics = products.filter((p) => p.kind === 'mosaic');
    return {
      tiles: [...new Set(mosaics.map((p) => p.tile as string))].sort(tileSort),
      filters: [...new Set(products.map((p) => p.filter))].sort(),
      pixel_scales: [...new Set(mosaics.map((p) => p.pixel_scale as string))].sort(),
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
            { label: 'NIRCam', href: '/nircam' },
            { label: field.toUpperCase() },
          ]}
          className="mb-6"
        />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view NIRCam images
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Access to NIRCam imaging data requires authentication. Please sign in with your
            CAMPFIRE account to browse and download images.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  const displayName = summary?.display_name ?? field.toUpperCase();
  const channels = summary ? filterChannelSplit(summary.filters) : null;
  const namedEpochs = summary ? summary.epochs.filter((e) => e !== '') : [];

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRCam', href: '/nircam' },
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
            No NIRCam data available for “{field}”.
          </p>
          <Link
            href="/nircam"
            className="inline-block mt-3 text-sm text-primary hover:text-primary-hover hover:underline"
          >
            ← All fields
          </Link>
        </div>
      )}

      {!loading && summary && (
        <>
          {/* Field header + map bridge */}
          <div className="flex items-start gap-4 flex-wrap mb-6">
            <h1 className="text-2xl font-bold text-text-primary">
              {displayName} NIRCam Imaging
            </h1>
            <div className="flex-1" />
            <div className="flex items-center gap-2">
              {hasCutouts && (
                <Link
                  href={`/nircam/${encodeURIComponent(field)}/cutouts`}
                  className="inline-flex items-center gap-2 px-4 py-2 bg-surface-2 border border-border-strong text-text-primary rounded-lg text-sm font-medium hover:bg-card-hover transition-colors"
                >
                  <Scissors className="w-3.5 h-3.5" />
                  Cutouts
                </Link>
              )}
              <Link
                href={`/map?field=${encodeURIComponent(field)}`}
                className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-on-primary rounded-lg text-sm font-medium hover:bg-primary-hover transition-colors"
              >
                Open field in Map
                <ExternalLink className="w-3.5 h-3.5" />
              </Link>
            </div>
          </div>

          {/* Field overview: metadata + exposure-map browser */}
          <div className="bg-card border border-border rounded-xl p-5 mb-7">
            <div className="grid gap-6 md:grid-cols-[2fr_3fr]">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary mb-3">
                  Field overview
                </h3>
                <div className="text-[13px]">
                  {(
                    [
                      [
                        'Filters',
                        channels ? (
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
                      ['Sky coverage', formatCoverage(summary)],
                      ['Data volume', formatVolume(summary.total_bytes)],
                      // Reduction provenance (latest published deployment)
                      ...(summary.last_updated
                        ? [['Last reduced', summary.last_updated.slice(0, 10)]]
                        : []),
                      ...(summary.cfpipe_version
                        ? [[
                            'Pipeline',
                            <span key="cfp" className="font-mono text-xs break-all">
                              cfpipe {summary.cfpipe_version}
                            </span>,
                          ]]
                        : []),
                      ...(summary.jwst_version
                        ? [[
                            'jwst',
                            <span key="jwst" className="font-mono text-xs">
                              {summary.jwst_version}
                            </span>,
                          ]]
                        : []),
                      ...(summary.crds_context
                        ? [[
                            'CRDS context',
                            <span key="crds" className="font-mono text-xs">
                              {summary.crds_context}
                            </span>,
                          ]]
                        : []),
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

              <ExpmapBrowser
                filters={summary.filters}
                expmapPlots={fieldImages.expmapPlots}
              />
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

          <div className="mb-4">
            <CurlScriptGenerator selectedImages={selectedProducts} />
          </div>

          {products.length === 0 ? (
            <div className="text-center py-16 bg-card border border-border rounded-lg">
              <ImageIcon className="w-12 h-12 text-text-secondary mx-auto mb-4" />
              <p className="text-text-secondary">No data products available yet.</p>
            </div>
          ) : (
            <NircamTable
              products={products}
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
