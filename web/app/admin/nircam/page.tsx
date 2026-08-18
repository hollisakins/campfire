'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Card } from '@/components/ui/Card';
import { Loader2, ChevronRight, Check } from 'lucide-react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminFilterBar } from '@/components/admin/AdminFilterBar';
import { flatFilterCodec, useTableUrlState, type SortState } from '@/lib/hooks/useTableUrlState';
import { useAdminTableQuery } from '@/lib/hooks/useAdminTableQuery';
import {
  getNircamExposures,
  getReductionProgress,
  getExposureFilterOptions,
  type ReductionProgress,
} from '@/lib/actions/nircam-exposures';
import { EXPOSURE_SORT_KEYS } from '@/lib/admin/sort-keys';
import {
  ensureReviewOutboxRunning,
  overlayReviewDecisions,
} from '@/lib/nircam-review-outbox';
import type { NircamExposure } from '@/lib/types';
import { stageBadgeClasses, NIRCAM_STAGES } from '@/lib/nircam-stages';
import { buildExposureNavQuery } from '@/lib/nircam-exposure-nav';

// ---------------------------------------------------------------------------
// Status badge helpers
// ---------------------------------------------------------------------------

function ReviewBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    pending: 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300',
    approved: 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300',
    excluded: 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || 'bg-surface-2 text-text-primary'}`}>
      {status}
    </span>
  );
}

function ActionBadge({ status, label }: { status: string; label: string }) {
  if (status === 'none') return <span className="text-xs text-text-secondary">&mdash;</span>;
  const colors: Record<string, string> = {
    needed: 'bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-300',
    done: 'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || ''}`}>
      {label}: {status}
    </span>
  );
}

// "Masked" is derived state — a non-empty mask_regions is the sole signal
// (the redundant `masking` status column was dropped). Read-only: masks are
// drawn in the exposure detail view, not toggled here.
function MaskedBadge({ regions }: { regions: NircamExposure['mask_regions'] }) {
  if ((regions?.polygons?.length ?? 0) === 0) {
    return <span className="text-xs text-text-secondary">&mdash;</span>;
  }
  return (
    <span className="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300">
      masked
    </span>
  );
}

function StageBadge({ stage }: { stage: string }) {
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full font-mono ${stageBadgeClasses(stage)}`}>
      {stage}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Reduction progress — inspection-centric summary, grouped field → filter →
// detector. Fields collapse (default closed) so the exposure table stays one
// scroll away regardless of how many field/filter combinations exist. Each
// filter row shows inspection progress (approved+excluded of total), the mask
// count, and per-detector quick-filter links into the pending queue below.
// (The old per-stage distribution bar is gone: whole filters sit at the same
// pipeline stage in practice, so it carried no signal. The excluded-exposures
// copy-paste panel is gone too — `campfire pull` materializes exclusions into
// reference/nircam/<field>/exposures.json, no fields.toml edit needed.)
// ---------------------------------------------------------------------------

interface DetectorProgress { detector: string; total: number; pending: number }

interface TriageCounts {
  total: number;
  pending: number;
  approved: number;
  excluded: number;
  masked: number;
  needsCorrection: number;
}

interface FilterProgress extends TriageCounts {
  filter: string;
  detectors: DetectorProgress[];
}

interface FieldProgress extends TriageCounts {
  field: string;
  filters: FilterProgress[];
}

const emptyCounts = (): TriageCounts => ({
  total: 0, pending: 0, approved: 0, excluded: 0, masked: 0, needsCorrection: 0,
});

// Rows arrive ordered by (field, filter, detector) — see getReductionProgress
// — so grouping is a single linear pass appending to the tail.
function groupProgress(rows: ReductionProgress[]): FieldProgress[] {
  const out: FieldProgress[] = [];
  for (const r of rows) {
    let fld = out[out.length - 1];
    if (!fld || fld.field !== r.field) {
      fld = { field: r.field, filters: [], ...emptyCounts() };
      out.push(fld);
    }
    let flt = fld.filters[fld.filters.length - 1];
    if (!flt || flt.filter !== r.filter) {
      flt = { filter: r.filter, detectors: [], ...emptyCounts() };
      fld.filters.push(flt);
    }
    flt.detectors.push({ detector: r.detector, total: r.total, pending: r.pending_review });
    for (const acc of [fld, flt]) {
      acc.total += r.total;
      acc.pending += r.pending_review;
      acc.approved += r.approved;
      acc.excluded += r.excluded;
      acc.masked += r.masked;
      acc.needsCorrection += r.needs_correction;
    }
  }
  return out;
}

function pendingHref(field: string, filter?: string, detector?: string) {
  const p = new URLSearchParams({ field, review: 'pending' });
  if (filter) p.set('filter', filter);
  if (detector) p.set('detector', detector);
  return `/admin/nircam?${p.toString()}`;
}

// Single-hue completion meter: fill = inspected (approved + excluded) share.
// The approved/excluded split lives in the title tooltip and the text counts,
// not in bar segments, so the meter stays readable for all color vision.
function InspectionMeter({ counts }: { counts: TriageCounts }) {
  const inspected = counts.approved + counts.excluded;
  const pct = counts.total > 0 ? (inspected / counts.total) * 100 : 0;
  return (
    <div
      className="flex items-center gap-2"
      title={`${counts.approved} approved · ${counts.excluded} excluded · ${counts.pending} pending`}
    >
      <div className="h-2 flex-1 min-w-16 rounded-full bg-surface-2 overflow-hidden">
        <div
          className="h-full rounded-full bg-green-500 dark:bg-green-600"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs tabular-nums text-text-secondary whitespace-nowrap">
        {inspected}/{counts.total}
      </span>
    </div>
  );
}

// Quick-filter chip: jumps to the exposure table pre-filtered to this
// field/filter/detector's pending queue.
function PendingChip({ label, count, href }: { label: string; count: number; href: string }) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-mono
        bg-yellow-100 dark:bg-yellow-900/50 text-yellow-800 dark:text-yellow-300
        hover:bg-yellow-200 dark:hover:bg-yellow-900"
    >
      {label}
      <span className="font-semibold tabular-nums">{count}</span>
    </Link>
  );
}

function FieldSection({ fp }: { fp: FieldProgress }) {
  const [open, setOpen] = useState(false);

  return (
    <div>
      <div className="flex items-center gap-3 px-4 py-2.5 hover:bg-card-hover">
        <button
          onClick={() => setOpen(o => !o)}
          className="flex items-center gap-2 flex-1 min-w-0 text-left"
          aria-expanded={open}
        >
          <ChevronRight
            className={`w-4 h-4 shrink-0 text-text-secondary transition-transform ${open ? 'rotate-90' : ''}`}
          />
          <span className="font-medium text-text-primary">{fp.field}</span>
          <span className="text-xs text-text-secondary whitespace-nowrap">
            {fp.filters.length} filter{fp.filters.length !== 1 ? 's' : ''} · {fp.total} exp
            {fp.masked > 0 && <> · {fp.masked} masked</>}
          </span>
        </button>
        <div className="w-40 shrink-0 hidden sm:block">
          <InspectionMeter counts={fp} />
        </div>
        <div className="w-20 shrink-0 text-right">
          {fp.pending > 0 ? (
            <PendingChip label="pending" count={fp.pending} href={pendingHref(fp.field)} />
          ) : (
            <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
              <Check className="w-3.5 h-3.5" /> done
            </span>
          )}
        </div>
      </div>

      {open && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-t border-border">
              <th className="pl-11 pr-3 py-1.5 text-left text-xs font-medium text-text-secondary uppercase">Filter</th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-text-secondary uppercase w-52">Inspected</th>
              <th className="px-3 py-1.5 text-right text-xs font-medium text-text-secondary uppercase w-20">Masks</th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-text-secondary uppercase">Pending by detector</th>
              <th className="px-3 py-1.5 text-right text-xs font-medium text-text-secondary uppercase w-16">Corr</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {fp.filters.map((flt) => (
              <tr key={flt.filter} className="border-t border-border hover:bg-card-hover/50">
                <td className="pl-11 pr-3 py-1.5">
                  <Link
                    href={`/admin/nircam?${new URLSearchParams({ field: fp.field, filter: flt.filter })}`}
                    className="font-mono text-text-primary hover:text-primary hover:underline"
                  >
                    {flt.filter}
                  </Link>
                </td>
                <td className="px-3 py-1.5">
                  <InspectionMeter counts={flt} />
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {flt.masked > 0
                    ? <span className="text-blue-600 dark:text-blue-400">{flt.masked}</span>
                    : <span className="text-text-secondary">&mdash;</span>}
                </td>
                <td className="px-3 py-1.5">
                  {flt.pending === 0 ? (
                    <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                      <Check className="w-3.5 h-3.5" /> inspected
                    </span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {flt.detectors
                        .filter((d) => d.pending > 0)
                        .map((d) => (
                          <PendingChip
                            key={d.detector}
                            label={d.detector}
                            count={d.pending}
                            href={pendingHref(fp.field, flt.filter, d.detector)}
                          />
                        ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-1.5 text-right">
                  {flt.needsCorrection > 0 ? (
                    <Link
                      href={`/admin/nircam?${new URLSearchParams({ field: fp.field, filter: flt.filter, correction: 'needed' })}`}
                      className="text-orange-600 dark:text-orange-400 font-medium hover:underline tabular-nums"
                    >
                      {flt.needsCorrection}
                    </Link>
                  ) : (
                    <span className="text-text-secondary">&mdash;</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ProgressSummary({ progress }: { progress: ReductionProgress[] }) {
  const fields = useMemo(() => groupProgress(progress), [progress]);

  const grand = useMemo(() => {
    const g = emptyCounts();
    for (const f of fields) {
      g.total += f.total;
      g.pending += f.pending;
      g.approved += f.approved;
      g.excluded += f.excluded;
      g.masked += f.masked;
      g.needsCorrection += f.needsCorrection;
    }
    return g;
  }, [fields]);
  const inspected = grand.approved + grand.excluded;

  return (
    <Card className="mb-6 overflow-hidden">
      <div className="px-4 py-3 border-b border-border bg-surface-2 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium text-text-primary uppercase tracking-wider">
          Reduction Progress
        </h2>
        {grand.total > 0 && (
          <span className="text-xs text-text-secondary whitespace-nowrap">
            {inspected}/{grand.total} inspected ({Math.round((inspected / grand.total) * 100)}%)
            · {grand.masked} masked · {grand.pending} pending
          </span>
        )}
      </div>
      {fields.length === 0 ? (
        <p className="text-sm text-text-secondary p-4">
          No reduction data available. Deploy exposures with <code className="text-xs bg-card dark:bg-card-hover px-1 py-0.5 rounded">campfire deploy nircam</code>.
        </p>
      ) : (
        <div className="divide-y divide-border">
          {fields.map((fp) => (
            <FieldSection key={fp.field} fp={fp} />
          ))}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// URL-state config (module-level: codec/whitelist must be stable references).
// The same param names are what the detail page parses for prev/next nav —
// see lib/nircam-exposure-nav.ts.
// ---------------------------------------------------------------------------

const FILTER_KEYS = ['field', 'filter', 'detector', 'review', 'stage', 'correction'] as const;
const codec = flatFilterCodec(FILTER_KEYS);
const DEFAULT_SORT: SortState = { column: 'filename', direction: 'asc' };

const REVIEW_OPTIONS = [
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'excluded', label: 'Excluded' },
];
const ACTION_STATE_OPTIONS = [
  { value: 'needed', label: 'Needed' },
  { value: 'done', label: 'Done' },
  { value: 'none', label: 'None' },
];

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

function AdminNircamPageInner() {
  const state = useTableUrlState({
    codec,
    sortWhitelist: EXPOSURE_SORT_KEYS,
    defaultSort: DEFAULT_SORT,
  });

  // Replay triage decisions stranded by a reload as soon as the operator is
  // back in the tool — the list page is where sessions land.
  useEffect(() => { ensureReviewOutboxRunning(); }, []);

  const exposures = useAdminTableQuery<NircamExposure>({
    scope: 'admin-nircam-exposures',
    filters: state.debouncedFilters,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    fetchPage: async (page) => {
      const f = state.debouncedFilters;
      const res = await getNircamExposures({
        field: f.field || undefined,
        filter: f.filter || undefined,
        detector: f.detector || undefined,
        reviewStatus: f.review || undefined,
        stage: f.stage || undefined,
        correction: f.correction || undefined,
        sortColumn: state.sort.column,
        sortDirection: state.sort.direction,
        page,
        pageSize: state.pageSize,
      });
      return {
        // Fold not-yet-delivered triage decisions over the server rows, so
        // returning to the table right after a fast run shows what the
        // operator decided rather than statuses the outbox hasn't finished
        // syncing (the queue drains in the background either way).
        rows: res.exposures.map(overlayReviewDecisions),
        total: res.total,
        error: res.error,
      };
    },
  });

  const { data: progressResult } = useQuery({
    queryKey: ['admin-nircam-progress'],
    queryFn: getReductionProgress,
    staleTime: 30_000,
  });
  const { data: facets } = useQuery({
    queryKey: ['admin-nircam-facets'],
    queryFn: getExposureFilterOptions,
    staleTime: 5 * 60_000,
  });

  // Row links carry the current filter+sort state so the detail page derives
  // prev/next from the same set (see lib/nircam-exposure-nav.ts).
  const navQuery = useMemo(
    () => buildExposureNavQuery(state.filters, state.sort, DEFAULT_SORT),
    [state.filters, state.sort],
  );
  const detailHref = (id: number) => `/admin/nircam/${id}${navQuery ? `?${navQuery}` : ''}`;

  const columns = useMemo<ColumnDef<NircamExposure, unknown>[]>(() => [
    {
      id: 'filename',
      header: 'Filename',
      cell: ({ row }) => (
        <Link
          href={detailHref(row.original.id)}
          className="text-sm font-mono text-primary hover:underline"
        >
          {row.original.filename}
        </Link>
      ),
      meta: { sortKey: 'filename' },
    },
    {
      id: 'field',
      header: 'Field',
      cell: ({ row }) => <span className="text-sm text-text-primary">{row.original.field}</span>,
      meta: { sortKey: 'field' },
    },
    {
      id: 'filter',
      header: 'Filter',
      cell: ({ row }) => <span className="text-sm text-text-primary">{row.original.filter}</span>,
      meta: { sortKey: 'filter' },
    },
    {
      id: 'detector',
      header: 'Detector',
      cell: ({ row }) => <span className="text-sm text-text-secondary">{row.original.detector}</span>,
      meta: { sortKey: 'detector' },
    },
    {
      id: 'stage',
      header: 'Stage',
      cell: ({ row }) => <StageBadge stage={row.original.stage} />,
      meta: { sortKey: 'stage' },
    },
    {
      id: 'review',
      header: 'Review',
      cell: ({ row }) => <ReviewBadge status={row.original.review_status} />,
      meta: { sortKey: 'review_status' },
    },
    {
      id: 'masked',
      header: 'Masked',
      cell: ({ row }) => <MaskedBadge regions={row.original.mask_regions} />,
    },
    {
      id: 'correction',
      header: 'Correction',
      cell: ({ row }) => <ActionBadge status={row.original.correction} label="corr" />,
    },
    {
      id: 'open',
      header: '',
      cell: ({ row }) => (
        <Link href={detailHref(row.original.id)}>
          <ChevronRight className="w-4 h-4 text-text-secondary" />
        </Link>
      ),
      meta: { align: 'right' },
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [navQuery]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary">NIRCam Reductions</h1>
      </div>

      {progressResult?.error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{progressResult.error}</p>
        </div>
      )}

      {/* Progress summary */}
      <ProgressSummary progress={progressResult?.progress ?? []} />

      <AdminFilterBar
        facets={[
          {
            kind: 'select', key: 'field', label: 'Field',
            options: (facets?.fields ?? []).map((f) => ({ value: f, label: f })),
          },
          {
            kind: 'select', key: 'filter', label: 'Filter',
            options: (facets?.filters ?? []).map((f) => ({ value: f, label: f })),
          },
          {
            kind: 'select', key: 'detector', label: 'Detector',
            options: (facets?.detectors ?? []).map((d) => ({ value: d, label: d })),
          },
          {
            kind: 'select', key: 'stage', label: 'Stage',
            options: NIRCAM_STAGES.map((s) => ({ value: s, label: s })),
          },
          { kind: 'select', key: 'review', label: 'Review', options: REVIEW_OPTIONS },
          { kind: 'select', key: 'correction', label: 'Correction', options: ACTION_STATE_OPTIONS },
        ]}
        values={state.filters}
        onChange={(key, value) => state.setFilters({ ...state.filters, [key]: value })}
        onReset={state.resetFilters}
      />

      <AdminTable
        columns={columns}
        data={exposures.rows}
        total={exposures.total}
        page={state.page}
        pageSize={state.pageSize}
        sort={state.sort}
        loading={exposures.isInitialLoading}
        fetching={exposures.isFetching || state.isDebouncing}
        error={exposures.error}
        emptyTitle="No exposures found."
        onSortChange={state.setSort}
        onPageChange={state.setPage}
        onPageSizeChange={state.setPageSize}
        getRowKey={(e) => e.id}
        pageSizeOptions={[25, 50, 100, 200]}
      />
    </div>
  );
}

export default function AdminNircamPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <AdminNircamPageInner />
    </Suspense>
  );
}
