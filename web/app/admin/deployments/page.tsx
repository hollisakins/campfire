'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2, RefreshCw, GitBranch, Eye, EyeOff, Undo2, History, AlertCircle,
} from 'lucide-react';
import {
  getDeployments, getDeployEvents, setDeploymentLifecycle,
  type DeploymentRow, type DeployEventRow, type DeployStatus, type LifecycleAction,
} from '@/lib/actions/deployments';

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'draft', label: 'Draft' },
  { value: 'published', label: 'Published' },
  { value: 'revoked', label: 'Revoked' },
];

const INSTRUMENT_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All instruments' },
  { value: 'nirspec', label: 'NIRSpec' },
  { value: 'nircam', label: 'NIRCam' },
];

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

const PAST_TENSE: Record<LifecycleAction, string> = {
  publish: 'Published',
  revoke: 'Revoked',
  recover: 'Recovered',
};

function statusBadge(status: DeployStatus) {
  const map: Record<DeployStatus, string> = {
    draft: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
    published: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300',
    revoked: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  };
  const label: Record<DeployStatus, string> = {
    draft: 'draft', published: 'published', revoked: 'revoked',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${map[status]}`}>
      {label[status]}
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

// The lifecycle control offered for a deployment, by its current status.
function actionFor(status: DeployStatus): { action: LifecycleAction; label: string; icon: React.ElementType } | null {
  if (status === 'draft') return { action: 'publish', label: 'Publish', icon: Eye };
  if (status === 'published') return { action: 'revoke', label: 'Revoke', icon: EyeOff };
  if (status === 'revoked') return { action: 'recover', label: 'Recover', icon: Undo2 };
  return null;
}

export default function DeploymentsPage() {
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [instrumentFilter, setInstrumentFilter] = useState<string>('all');
  const [deployments, setDeployments] = useState<DeploymentRow[]>([]);
  const [events, setEvents] = useState<DeployEventRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const status = statusFilter === 'all' ? undefined : (statusFilter as DeployStatus);
    const instrument = instrumentFilter === 'all'
      ? undefined
      : (instrumentFilter as 'nirspec' | 'nircam');
    const [dep, ev] = await Promise.all([
      getDeployments({ status, instrument, pageSize: 100 }),
      getDeployEvents({ pageSize: 50 }),
    ]);
    if (dep.error) setError(dep.error);
    else setDeployments(dep.deployments);
    if (!ev.error) setEvents(ev.events);
    setLoading(false);
  }, [statusFilter, instrumentFilter]);

  useEffect(() => { refresh(); }, [refresh]);

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
    const res = await setDeploymentLifecycle(dep.id, action);
    setBusyId(null);
    if (!res.success) {
      setError(res.error ?? `Failed to ${verb}`);
      return;
    }
    const unit = res.updatedKind === 'images' ? 'images' : 'spectra';
    setNotice(`${PAST_TENSE[action]} ${scope} — ${res.updated ?? 0} ${unit} updated.`);
    await refresh();
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <GitBranch className="w-6 h-6 text-primary" />
          <h1 className="text-2xl font-semibold text-text-primary">Deployments</h1>
        </div>
        <Button variant="secondary" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>
      <p className="text-text-secondary text-sm mb-6">
        Review and publish deployments. <span className="font-medium">Drafts</span> — explicit
        <code className="px-1">--draft</code> deploys and incomplete reductions (intermediates
        uploaded, no stage3 finals yet) — are admin-only and invisible to users until published;
        revoking hides a published deployment without deleting its data. The files each deployment
        uploaded are browsable under <span className="font-medium">Intermediate Products</span>.
      </p>

      {notice && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-green-50 dark:bg-green-950 text-green-800 dark:text-green-300 text-sm">
          {notice}
        </div>
      )}
      {error && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-300 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4" /> {error}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1 rounded-full text-sm transition-colors ${
              statusFilter === f.value
                ? 'bg-primary text-on-primary'
                : 'bg-card-hover text-text-secondary hover:text-text-primary'
            }`}
          >
            {f.label}
          </button>
        ))}
        <span className="mx-1 h-5 w-px bg-border" aria-hidden />
        {INSTRUMENT_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setInstrumentFilter(f.value)}
            className={`px-3 py-1 rounded-full text-sm transition-colors ${
              instrumentFilter === f.value
                ? 'bg-primary text-on-primary'
                : 'bg-card-hover text-text-secondary hover:text-text-primary'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <Card className="mb-8 overflow-hidden p-0">
        {loading && deployments.length === 0 ? (
          <div className="p-8 flex items-center justify-center text-text-secondary">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading…
          </div>
        ) : deployments.length === 0 ? (
          <div className="p-8 text-center text-text-secondary text-sm">No deployments match this filter.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-card-hover text-text-secondary text-left">
              <tr>
                <th className="px-4 py-2 font-medium">Scope</th>
                <th className="px-4 py-2 font-medium">Instrument</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium text-right">Spectra</th>
                <th className="px-4 py-2 font-medium text-right">Targets / Exp.</th>
                <th className="px-4 py-2 font-medium">Pipeline</th>
                <th className="px-4 py-2 font-medium">Deployed</th>
                <th className="px-4 py-2 font-medium text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map((d) => {
                const act = actionFor(d.status);
                return (
                  <tr key={d.id} className="border-t border-border hover:bg-card-hover/50">
                    <td className="px-4 py-2 font-mono text-text-primary">{scopeOf(d)}</td>
                    <td className="px-4 py-2">{instrumentBadge(d)}</td>
                    <td className="px-4 py-2">{statusBadge(d.status)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{d.n_spectra ?? '—'}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{d.n_targets ?? '—'}</td>
                    <td className="px-4 py-2 font-mono text-xs text-text-secondary">{d.cfpipe_version ?? '—'}</td>
                    <td className="px-4 py-2 text-text-secondary whitespace-nowrap">{fmt(d.deployed_at)}</td>
                    <td className="px-4 py-2 text-right">
                      {act && (
                        <Button
                          variant={act.action === 'revoke' ? 'secondary' : 'primary'}
                          size="sm"
                          disabled={busyId === d.id}
                          onClick={() => onAction(d, act.action, act.label)}
                        >
                          {busyId === d.id
                            ? <Loader2 className="w-4 h-4 animate-spin" />
                            : <act.icon className="w-4 h-4" />}
                          {act.label}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>

      <div className="flex items-center gap-2 mb-3">
        <History className="w-5 h-5 text-text-secondary" />
        <h2 className="text-lg font-semibold text-text-primary">Audit Log</h2>
      </div>
      <Card className="overflow-hidden p-0">
        {events.length === 0 ? (
          <div className="p-6 text-center text-text-secondary text-sm">No lifecycle events yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-card-hover text-text-secondary text-left">
              <tr>
                <th className="px-4 py-2 font-medium">When</th>
                <th className="px-4 py-2 font-medium">Action</th>
                <th className="px-4 py-2 font-medium">Scope</th>
                <th className="px-4 py-2 font-medium">→ Status</th>
                <th className="px-4 py-2 font-medium text-right">Affected</th>
                <th className="px-4 py-2 font-medium">By</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} className="border-t border-border">
                  <td className="px-4 py-2 text-text-secondary whitespace-nowrap">{fmt(e.occurred_at)}</td>
                  <td className="px-4 py-2 font-medium text-text-primary">{e.action}</td>
                  <td className="px-4 py-2 font-mono text-xs">{e.observation ?? e.field ?? '—'}</td>
                  <td className="px-4 py-2 text-text-secondary">{e.status_to ?? '—'}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{e.affected_count ?? '—'}</td>
                  <td className="px-4 py-2 text-text-secondary">{e.actor_name ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
