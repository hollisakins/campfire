'use client';

import React, { Suspense, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Button } from '@/components/ui/Button';
import {
  Loader2, GitBranch, Eye, EyeOff, Undo2, History, AlertCircle,
} from 'lucide-react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminFilterBar } from '@/components/admin/AdminFilterBar';
import {
  flatFilterCodec, useTableUrlState, type SortDirection, type SortState,
} from '@/lib/hooks/useTableUrlState';
import { useAdminTableQuery } from '@/lib/hooks/useAdminTableQuery';
import {
  getDeployments, getDeployEvents, setDeploymentLifecycle,
  DEPLOYMENT_SORT_KEYS,
  type DeploymentRow, type DeployEventRow, type DeployStatus, type LifecycleAction,
} from '@/lib/actions/deployments';

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

function statusBadge(status: DeployStatus) {
  const map: Record<DeployStatus, string> = {
    draft: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
    published: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300',
    revoked: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${map[status]}`}>
      {status}
    </span>
  );
}

function fmt(ts: string | null): string {
  if (!ts) return '—';
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !iso.includes('+')) iso = iso + 'Z';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

// Exactly one of observation/field is set (deployments_scope_check).
function scopeOf(d: DeploymentRow): string {
  return d.observation ?? d.field ?? `#${d.id}`;
}

function instrumentOf(d: DeploymentRow): 'NIRSpec' | 'NIRCam' {
  return d.observation ? 'NIRSpec' : 'NIRCam';
}

function instrumentBadge(d: DeploymentRow) {
  const inst = instrumentOf(d);
  const cls = inst === 'NIRSpec'
    ? 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300'
    : 'bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300';
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {inst}
    </span>
  );
}

// The lifecycle control offered for a deployment, by its current status.
function actionFor(status: DeployStatus): { action: LifecycleAction; label: string; icon: React.ElementType } | null {
  if (status === 'draft') return { action: 'publish', label: 'Publish', icon: Eye };
  if (status === 'published') return { action: 'revoke', label: 'Revoke', icon: EyeOff };
  if (status === 'revoked') return { action: 'recover', label: 'Recover', icon: Undo2 };
  return null;
}

const PAST_TENSE: Record<LifecycleAction, string> = {
  publish: 'Published',
  revoke: 'Revoked',
  recover: 'Recovered',
};

// ---------------------------------------------------------------------------
// URL-state config (module-level: codec/whitelist must be stable references)
// ---------------------------------------------------------------------------

const FILTER_KEYS = ['status', 'instrument'] as const;
const codec = flatFilterCodec(FILTER_KEYS);
const DEFAULT_SORT: SortState = { column: 'deployed_at', direction: 'desc' };

const STATUS_OPTIONS = [
  { value: 'draft', label: 'Draft' },
  { value: 'published', label: 'Published' },
  { value: 'revoked', label: 'Revoked' },
];
const INSTRUMENT_OPTIONS = [
  { value: 'nirspec', label: 'NIRSpec' },
  { value: 'nircam', label: 'NIRCam' },
];
const EVENT_ACTION_OPTIONS = ['upload', 'publish', 'revoke', 'recover', 'supersede', 'delete']
  .map((a) => ({ value: a, label: a }));

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function DeploymentsPageInner() {
  const queryClient = useQueryClient();
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Deployments table: URL-backed state (filters/sort/page survive back-nav
  // and deep links, e.g. /admin/deployments?status=draft from the dashboard).
  const state = useTableUrlState({
    codec,
    sortWhitelist: DEPLOYMENT_SORT_KEYS,
    defaultSort: DEFAULT_SORT,
  });

  const deployments = useAdminTableQuery<DeploymentRow>({
    scope: 'admin-deployments',
    filters: state.debouncedFilters,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    fetchPage: async (page) => {
      const res = await getDeployments({
        status: (state.debouncedFilters.status || undefined) as DeployStatus | undefined,
        instrument: (state.debouncedFilters.instrument || undefined) as 'nirspec' | 'nircam' | undefined,
        sortColumn: state.sort.column,
        sortDirection: state.sort.direction,
        page,
        pageSize: state.pageSize,
      });
      return { rows: res.deployments, total: res.total, error: res.error };
    },
  });

  // Audit log: secondary table — local state (a second URL-backed table on the
  // same route would collide on the page/sort params), but fully paginated and
  // server-sorted through the same framework.
  const [eventAction, setEventAction] = useState('');
  const [eventSort, setEventSort] = useState<SortState>({ column: 'occurred_at', direction: 'desc' });
  const [eventPage, setEventPage] = useState(1);
  const [eventPageSize, setEventPageSize] = useState(25);

  const events = useAdminTableQuery<DeployEventRow>({
    scope: 'admin-deploy-events',
    filters: { action: eventAction },
    sort: eventSort,
    page: eventPage,
    pageSize: eventPageSize,
    fetchPage: async (page) => {
      const res = await getDeployEvents({
        action: eventAction || undefined,
        sortColumn: eventSort.column,
        sortDirection: eventSort.direction,
        page,
        pageSize: eventPageSize,
      });
      return { rows: res.events, total: res.total, error: res.error };
    },
  });

  const onAction = async (dep: DeploymentRow, action: LifecycleAction, label: string) => {
    const verb = label.toLowerCase();
    const scope = scopeOf(dep);
    const isNirspec = instrumentOf(dep) === 'NIRSpec';
    const noun = isNirspec ? 'spectra' : 'NIRCam images';
    const affects = isNirspec
      ? `This affects ${dep.n_spectra ?? '?'} spectra.`
      : `This affects its NIRCam images.`;
    const warn = action === 'revoke'
      ? `Revoke "${scope}"? Its ${noun} will be hidden from users (bytes retained, recoverable).`
      : `${label} "${scope}"? ${affects}`;
    if (!window.confirm(warn)) return;
    setBusyId(dep.id);
    setNotice(null);
    setActionError(null);
    const res = await setDeploymentLifecycle(dep.id, action);
    setBusyId(null);
    if (!res.success) {
      setActionError(res.error ?? `Failed to ${verb}`);
      return;
    }
    const unit = res.updatedKind === 'images' ? 'images' : 'spectra';
    setNotice(`${PAST_TENSE[action]} ${scope} — ${res.updated ?? 0} ${unit} updated.`);
    queryClient.invalidateQueries({ queryKey: ['admin-deployments'] });
    queryClient.invalidateQueries({ queryKey: ['admin-deploy-events'] });
  };

  const deploymentColumns = useMemo<ColumnDef<DeploymentRow, unknown>[]>(() => [
    {
      id: 'scope',
      header: 'Scope',
      cell: ({ row }) => (
        <span className="font-mono text-text-primary">{scopeOf(row.original)}</span>
      ),
      meta: { sortKey: 'scope' },
    },
    {
      id: 'instrument',
      header: 'Instrument',
      cell: ({ row }) => instrumentBadge(row.original),
    },
    {
      id: 'status',
      header: 'Status',
      cell: ({ row }) => statusBadge(row.original.status),
      meta: { sortKey: 'status' },
    },
    {
      id: 'n_spectra',
      header: 'Spectra',
      cell: ({ row }) => <span className="tabular-nums">{row.original.n_spectra ?? '—'}</span>,
      meta: { align: 'right' },
    },
    {
      id: 'n_targets',
      header: 'Targets / Exp.',
      cell: ({ row }) => <span className="tabular-nums">{row.original.n_targets ?? '—'}</span>,
      meta: { align: 'right' },
    },
    {
      id: 'cfpipe_version',
      header: 'Pipeline',
      cell: ({ row }) => (
        <span className="font-mono text-xs text-text-secondary">
          {row.original.cfpipe_version ?? '—'}
        </span>
      ),
    },
    {
      id: 'deployed_at',
      header: 'Deployed',
      cell: ({ row }) => (
        <span className="text-text-secondary whitespace-nowrap">{fmt(row.original.deployed_at)}</span>
      ),
      meta: { sortKey: 'deployed_at' },
    },
    {
      id: 'action',
      header: '',
      cell: ({ row }) => {
        const d = row.original;
        const act = actionFor(d.status);
        if (!act) return null;
        return (
          <Button
            variant={act.action === 'revoke' ? 'secondary' : 'primary'}
            size="sm"
            disabled={busyId === d.id}
            onClick={(e) => {
              e.stopPropagation();
              onAction(d, act.action, act.label);
            }}
          >
            {busyId === d.id
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <act.icon className="w-4 h-4" />}
            {act.label}
          </Button>
        );
      },
      meta: { align: 'right' },
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [busyId]);

  const eventColumns = useMemo<ColumnDef<DeployEventRow, unknown>[]>(() => [
    {
      id: 'occurred_at',
      header: 'When',
      cell: ({ row }) => (
        <span className="text-text-secondary whitespace-nowrap">{fmt(row.original.occurred_at)}</span>
      ),
      meta: { sortKey: 'occurred_at' },
    },
    {
      id: 'action',
      header: 'Action',
      cell: ({ row }) => (
        <span className="font-medium text-text-primary">{row.original.action}</span>
      ),
      meta: { sortKey: 'action' },
    },
    {
      id: 'scope',
      header: 'Scope',
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {row.original.observation ?? row.original.field ?? '—'}
        </span>
      ),
    },
    {
      id: 'status_to',
      header: '→ Status',
      cell: ({ row }) => (
        <span className="text-text-secondary">{row.original.status_to ?? '—'}</span>
      ),
    },
    {
      id: 'affected_count',
      header: 'Affected',
      cell: ({ row }) => (
        <span className="tabular-nums">{row.original.affected_count ?? '—'}</span>
      ),
      meta: { align: 'right' },
    },
    {
      id: 'actor',
      header: 'By',
      cell: ({ row }) => (
        <span className="text-text-secondary">{row.original.actor_name ?? '—'}</span>
      ),
    },
  ], []);

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <GitBranch className="w-6 h-6 text-primary" />
        <h1 className="text-2xl font-semibold text-text-primary">Deployments</h1>
      </div>
      <p className="text-text-secondary text-sm mb-6">
        Review and publish deployments. <span className="font-medium">Drafts</span> — explicit
        <code className="px-1">--draft</code> deploys and incomplete reductions (intermediates
        uploaded, no stage3 finals yet) — are admin-only and invisible to users until published;
        revoking hides a published deployment without deleting its data. The files each deployment
        uploaded are browsable under <span className="font-medium">Storage</span>.
      </p>

      {notice && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-green-50 dark:bg-green-950 text-green-800 dark:text-green-300 text-sm">
          {notice}
        </div>
      )}
      {actionError && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-300 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4" /> {actionError}
        </div>
      )}

      <AdminFilterBar
        facets={[
          { kind: 'pills', key: 'status', options: STATUS_OPTIONS },
          { kind: 'pills', key: 'instrument', options: INSTRUMENT_OPTIONS, allLabel: 'All instruments' },
        ]}
        values={state.filters}
        onChange={(key, value) => state.setFilters({ ...state.filters, [key]: value })}
        onReset={state.resetFilters}
      />

      <div className="mb-8">
        <AdminTable
          columns={deploymentColumns}
          data={deployments.rows}
          total={deployments.total}
          page={state.page}
          pageSize={state.pageSize}
          sort={state.sort}
          loading={deployments.isInitialLoading}
          fetching={deployments.isFetching || state.isDebouncing}
          error={deployments.error}
          emptyTitle="No deployments match this filter."
          onSortChange={state.setSort}
          onPageChange={state.setPage}
          onPageSizeChange={state.setPageSize}
          getRowKey={(d) => d.id}
        />
      </div>

      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <History className="w-5 h-5 text-text-secondary" />
          <h2 className="text-lg font-semibold text-text-primary">Audit Log</h2>
        </div>
        <AdminFilterBar
          facets={[{ kind: 'select', key: 'action', label: 'Action', options: EVENT_ACTION_OPTIONS }]}
          values={{ action: eventAction }}
          onChange={(_k, v) => {
            setEventAction(v);
            setEventPage(1);
          }}
        />
      </div>
      <AdminTable
        columns={eventColumns}
        data={events.rows}
        total={events.total}
        page={eventPage}
        pageSize={eventPageSize}
        sort={eventSort}
        loading={events.isInitialLoading}
        fetching={events.isFetching}
        error={events.error}
        emptyTitle="No lifecycle events yet."
        onSortChange={(column, direction: SortDirection) => {
          setEventSort({ column, direction });
          setEventPage(1);
        }}
        onPageChange={setEventPage}
        onPageSizeChange={(n) => {
          setEventPageSize(n);
          setEventPage(1);
        }}
        getRowKey={(e) => e.id}
        pageSizeOptions={[10, 25, 50, 100]}
      />
    </div>
  );
}

export default function DeploymentsPage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 flex items-center justify-center text-text-secondary">
          <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading…
        </div>
      }
    >
      <DeploymentsPageInner />
    </Suspense>
  );
}
