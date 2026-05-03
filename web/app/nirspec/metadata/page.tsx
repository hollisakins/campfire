'use client';

import React, { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { Database, Download, FileText, ExternalLink, LogIn } from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { LoadingState } from '@/components/ui/LoadingState';
import { ErrorState } from '@/components/ui/ErrorState';
import { ScopeHeader } from '@/components/metadata/ScopeHeader';
import { MetadataSearchBar } from '@/components/metadata/MetadataSearchBar';
import { MetadataFilterBar } from '@/components/metadata/MetadataFilterBar';
import { ProgramsList } from '@/components/metadata/ProgramsList';
import { ObservationsTable } from '@/components/metadata/ObservationsTable';
import {
  useProgramsOverviewQuery,
  useObservationsOverviewQuery,
  useDatabaseOverviewQuery,
} from '@/lib/hooks/useProgramsQuery';
import { useDebouncedValue } from '@/lib/hooks/useDebouncedValue';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  isWithinRecency,
  metadataFiltersToURLParams,
  parseMetadataFiltersFromURL,
  type MetadataFilters,
  type MetadataTab,
} from '@/lib/actions/metadata-filters';
import {
  flattenPointings,
  pointingsToCsv,
  pointingsToDs9,
  downloadBlob,
} from '@/lib/pointings';
import type { ProgramOverview, ObservationOverview } from '@/lib/actions/programs';

function applyProgramFilters(
  programs: ProgramOverview[],
  f: MetadataFilters
): ProgramOverview[] {
  const q = f.search.trim().toLowerCase();
  return programs.filter((p) => {
    if (q) {
      const haystack = [
        p.program_name,
        p.slug,
        p.pi_name,
        p.description,
        ...p.jwst_pids.map(String),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    if (f.cycle.length && (p.cycle === null || !f.cycle.includes(p.cycle))) return false;
    if (f.pi.length && (!p.pi_name || !f.pi.includes(p.pi_name))) return false;
    if (f.is_public !== null && p.is_public !== f.is_public) return false;
    if (f.fields.length && !f.fields.some((field) => p.fields.includes(field))) return false;
    if (f.gratings.length && !f.gratings.some((g) => p.gratings.includes(g))) return false;
    if (f.recency_days !== null && !isWithinRecency(p.last_reduced_at, f.recency_days)) {
      return false;
    }
    return true;
  });
}

function applyObservationFilters(
  observations: ObservationOverview[],
  f: MetadataFilters
): ObservationOverview[] {
  const q = f.search.trim().toLowerCase();
  return observations.filter((o) => {
    if (q) {
      const haystack = [o.observation, o.program_slug, o.program_name, o.field]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    if (f.programs.length && !f.programs.includes(o.program_slug)) return false;
    if (
      f.reduction_version.length &&
      (!o.reduction_version || !f.reduction_version.includes(o.reduction_version))
    ) {
      return false;
    }
    if (
      f.crds_context.length &&
      (!o.crds_context || !f.crds_context.includes(o.crds_context))
    ) {
      return false;
    }
    if (f.has_patches !== null) {
      const has = o.n_patches_since_full > 0;
      if (has !== f.has_patches) return false;
    }
    if (f.fields.length && !f.fields.includes(o.field)) return false;
    if (f.gratings.length && !f.gratings.some((g) => o.gratings.includes(g))) return false;
    if (f.recency_days !== null && !isWithinRecency(o.reduced_at, f.recency_days)) {
      return false;
    }
    return true;
  });
}

function buildSpectraViewerUrl(filtered: ObservationOverview[]): string {
  const params = new URLSearchParams();
  const programs = Array.from(new Set(filtered.map((o) => o.program_slug)));
  const observations = Array.from(new Set(filtered.map((o) => o.observation)));
  const fields = Array.from(new Set(filtered.map((o) => o.field)));
  const gratings = Array.from(
    new Set(filtered.flatMap((o) => o.gratings).filter(Boolean))
  );
  if (programs.length) params.set('programs', programs.join(','));
  if (observations.length) params.set('observations', observations.join(','));
  if (fields.length) params.set('fields', fields.join(','));
  if (gratings.length) params.set('gratings', gratings.join(','));
  const qs = params.toString();
  return qs ? `/nirspec?${qs}` : '/nirspec';
}

function MetadataPageContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { user, loading: authLoading } = useAuth();
  const enabled = !authLoading && !!user;

  const programsQ = useProgramsOverviewQuery(enabled);
  const observationsQ = useObservationsOverviewQuery(enabled);
  const overviewQ = useDatabaseOverviewQuery(enabled);

  const programs = useMemo(
    () => programsQ.data?.programs ?? [],
    [programsQ.data]
  );
  const observations = useMemo(
    () => observationsQ.data?.observations ?? [],
    [observationsQ.data]
  );
  const overview = overviewQ.data?.overview ?? null;
  const error =
    programsQ.data?.error ?? observationsQ.data?.error ?? overviewQ.data?.error ?? null;
  const loading = programsQ.isLoading || observationsQ.isLoading;

  const initialFilters = useMemo(
    () => parseMetadataFiltersFromURL(new URLSearchParams(searchParams.toString())),
    // intentionally only on first mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );
  const [filters, setFilters] = useState<MetadataFilters>(initialFilters);
  const { debouncedValue: debouncedFilters } = useDebouncedValue(filters, 200);

  // Sync filter state to URL.
  useEffect(() => {
    const params = metadataFiltersToURLParams(debouncedFilters);
    const qs = params.toString();
    const next = qs ? `${pathname}?${qs}` : pathname;
    router.replace(next, { scroll: false });
  }, [debouncedFilters, pathname, router]);

  const setTab = (tab: MetadataTab) => setFilters((prev) => ({ ...prev, tab }));

  const filteredPrograms = useMemo(
    () => applyProgramFilters(programs, debouncedFilters),
    [programs, debouncedFilters]
  );

  const filteredObservations = useMemo(
    () => applyObservationFilters(observations, debouncedFilters),
    [observations, debouncedFilters]
  );

  // Auth gate
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec', href: '/nirspec' },
            { label: 'Metadata' },
          ]}
          className="mb-6"
        />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view database metadata
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to JWST program information requires authentication. Please sign in
            with your CAMPFIRE account to browse programs and observations.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRSpec', href: '/nirspec' },
          { label: 'Metadata' },
        ]}
        className="mb-6"
      />

      <div className="mb-6">
        <div className="flex items-center gap-3 mb-2">
          <Database className="w-7 h-7 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">
            NIRSpec Metadata
          </h1>
        </div>
        <p className="text-text-secondary dark:text-slate-400 max-w-3xl">
          Programs, observations, and pipeline reductions in the CAMPFIRE database. Program
          metadata is sourced from{' '}
          <a
            href="https://www.stsci.edu/jwst/science-execution/approved-programs"
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:text-primary-hover transition-colors"
          >
            STScI
          </a>
          . Program missing from CAMPFIRE?{' '}
          <a
            href="https://github.com/hollisakins/campfire/issues/new?template=data_request.yml"
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:text-primary-hover transition-colors"
          >
            Request it here
          </a>
          .
        </p>
      </div>

      <ScopeHeader overview={overview} loading={overviewQ.isLoading} />

      <div className="mb-4">
        <MetadataSearchBar
          programs={programs}
          observations={observations}
          value={filters.search}
          onChange={(search) => setFilters((prev) => ({ ...prev, search }))}
          onSelectObservation={(name) =>
            setFilters((prev) => ({ ...prev, tab: 'observations', search: name }))
          }
          onSelectPi={(piName) =>
            setFilters((prev) => ({ ...prev, tab: 'programs', pi: [piName] }))
          }
        />
      </div>

      {error && !loading && <ErrorState message={error} className="mb-4" />}

      <Tabs value={filters.tab} onValueChange={(v) => setTab(v as MetadataTab)}>
        <TabsList className="mb-4">
          <TabsTrigger value="programs">
            Programs ({programs.length.toLocaleString()})
          </TabsTrigger>
          <TabsTrigger value="observations">
            Observations ({observations.length.toLocaleString()})
          </TabsTrigger>
        </TabsList>

        <TabsContent value="programs">
          <MetadataFilterBar
            tab="programs"
            filters={filters}
            onChange={setFilters}
            programs={programs}
            observations={observations}
          />
          {loading ? (
            <LoadingState label="Loading programs..." />
          ) : (
            <ProgramsList programs={filteredPrograms} />
          )}
        </TabsContent>

        <TabsContent value="observations">
          <MetadataFilterBar
            tab="observations"
            filters={filters}
            onChange={setFilters}
            programs={programs}
            observations={observations}
            rightSlot={
              <ObservationsBulkActions filtered={filteredObservations} />
            }
          />
          {loading ? (
            <LoadingState label="Loading observations..." />
          ) : (
            <ObservationsTable observations={filteredObservations} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

const ObservationsBulkActions: React.FC<{ filtered: ObservationOverview[] }> = ({
  filtered,
}) => {
  const hasPointings = filtered.some((o) => (o.pointings?.length ?? 0) > 0);
  const downloadCsv = () => {
    const rows = flattenPointings(filtered);
    if (rows.length === 0) return;
    downloadBlob(pointingsToCsv(rows), 'campfire_pointings.csv', 'text/csv');
  };
  const downloadDs9 = () => {
    const rows = flattenPointings(filtered);
    if (rows.length === 0) return;
    downloadBlob(pointingsToDs9(rows), 'campfire_pointings.reg', 'text/plain');
  };

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={downloadCsv}
        disabled={!hasPointings}
        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-border dark:border-slate-700 hover:bg-card-hover dark:hover:bg-slate-700/40 disabled:opacity-50 disabled:cursor-not-allowed text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100 transition-colors"
        title="Download CSV of pointings for filtered observations"
      >
        <Download className="w-3.5 h-3.5" />
        CSV
      </button>
      <button
        onClick={downloadDs9}
        disabled={!hasPointings}
        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-border dark:border-slate-700 hover:bg-card-hover dark:hover:bg-slate-700/40 disabled:opacity-50 disabled:cursor-not-allowed text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100 transition-colors"
        title="Download DS9 region file for filtered observations"
      >
        <FileText className="w-3.5 h-3.5" />
        DS9
      </button>
      <Link
        href={buildSpectraViewerUrl(filtered)}
        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
        title="View matching spectra"
      >
        View spectra
        <ExternalLink className="w-3.5 h-3.5" />
      </Link>
    </div>
  );
};

export default function MetadataPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading metadata..." />}>
      <MetadataPageContent />
    </Suspense>
  );
}
