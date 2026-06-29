'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, RefreshCw, Database, AlertCircle, HardDrive } from 'lucide-react';
import {
  getStorageObjects, getStorageBudget,
  type StorageObjectRow, type StorageBudget,
} from '@/lib/actions/storage-registry';

// Intermediate/data product types worth filtering by (the registry holds more,
// but these are the ones an admin browses here). 'all' clears the filter.
const PRODUCT_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'nirspec_spectrum_exposure', label: 'Spectrum exposures' },
  { value: 'nirspec_spec', label: 'Spectra (final)' },
  { value: 'nircam_exposure', label: 'NIRCam exposures' },
  { value: 'zfit', label: 'Zfit' },
];

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'superseded', label: 'Superseded' },
  { value: 'revoked', label: 'Revoked' },
];

function fmtBytes(n: number): string {
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

function fmtDate(ts: string): string {
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !iso.includes('+')) iso = iso + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    active: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300',
    superseded: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
    revoked: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${map[status] ?? ''}`}>
      {status}
    </span>
  );
}

export default function IntermediateProductsPage() {
  const [productFilter, setProductFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('active');
  const [objects, setObjects] = useState<StorageObjectRow[]>([]);
  const [total, setTotal] = useState(0);
  const [budget, setBudget] = useState<StorageBudget | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const [res, bud] = await Promise.all([
      getStorageObjects({
        productType: productFilter === 'all' ? undefined : productFilter,
        status: statusFilter === 'all' ? undefined : statusFilter,
        pageSize: 200,
      }),
      getStorageBudget(),
    ]);
    if (res.error) setError(res.error);
    else { setObjects(res.objects); setTotal(res.total); }
    if (bud && !('error' in bud)) setBudget(bud as StorageBudget);
    setLoading(false);
  }, [productFilter, statusFilter]);

  useEffect(() => { refresh(); }, [refresh]);

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Database className="w-6 h-6 text-primary" />
          <h1 className="text-2xl font-semibold text-text-primary">Intermediate Products</h1>
        </div>
        <Button variant="secondary" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>
      <p className="text-text-secondary text-sm mb-6">
        Every object in cloud storage — the canonical spectrum-exposure intermediates and final
        products each deploy uploads. This is the storage/registry view; the draft → published
        lifecycle lives on the <span className="font-medium">Deployments</span> page.
      </p>

      {budget && (
        <Card className="mb-6 p-4">
          <div className="flex items-center gap-3 text-sm">
            <HardDrive className="w-5 h-5 text-text-secondary" />
            <span className="text-text-primary font-medium">{fmtBytes(budget.total_bytes)}</span>
            <span className="text-text-secondary">of {fmtBytes(budget.cap_bytes)} ({budget.pct_used}%)</span>
          </div>
        </Card>
      )}

      {error && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-300 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4" /> {error}
        </div>
      )}

      <div className="flex flex-wrap gap-2 mb-2">
        {PRODUCT_FILTERS.map((f) => (
          <button key={f.value} onClick={() => setProductFilter(f.value)}
            className={`px-3 py-1 rounded-full text-sm transition-colors ${
              productFilter === f.value ? 'bg-primary text-on-primary'
                : 'bg-card-hover text-text-secondary hover:text-text-primary'}`}>
            {f.label}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap gap-2 mb-4">
        {STATUS_FILTERS.map((f) => (
          <button key={f.value} onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1 rounded-full text-xs transition-colors ${
              statusFilter === f.value ? 'bg-primary text-on-primary'
                : 'bg-card-hover text-text-secondary hover:text-text-primary'}`}>
            {f.label}
          </button>
        ))}
      </div>

      <Card className="overflow-hidden p-0">
        {loading && objects.length === 0 ? (
          <div className="p-8 flex items-center justify-center text-text-secondary">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading…
          </div>
        ) : objects.length === 0 ? (
          <div className="p-8 text-center text-text-secondary text-sm">No storage objects match this filter.</div>
        ) : (
          <>
            <div className="px-4 py-2 text-xs text-text-secondary border-b border-border">
              Showing {objects.length} of {total}
            </div>
            <table className="w-full text-sm">
              <thead className="bg-card-hover text-text-secondary text-left">
                <tr>
                  <th className="px-4 py-2 font-medium">Key</th>
                  <th className="px-4 py-2 font-medium">Product</th>
                  <th className="px-4 py-2 font-medium">Observation</th>
                  <th className="px-4 py-2 font-medium text-right">Size</th>
                  <th className="px-4 py-2 font-medium">Backend</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {objects.map((o) => (
                  <tr key={o.id} className="border-t border-border hover:bg-card-hover/50">
                    <td className="px-4 py-2 font-mono text-xs text-text-primary truncate max-w-[28rem]" title={o.storage_key}>
                      {o.storage_key.split('/').pop()}
                    </td>
                    <td className="px-4 py-2 text-text-secondary">{o.product_type}</td>
                    <td className="px-4 py-2 font-mono text-xs">{o.observation ?? '—'}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{fmtBytes(o.size_bytes)}</td>
                    <td className="px-4 py-2 uppercase text-xs text-text-secondary">{o.backend}</td>
                    <td className="px-4 py-2">{statusBadge(o.status)}</td>
                    <td className="px-4 py-2 text-text-secondary whitespace-nowrap">{fmtDate(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Card>
    </div>
  );
}
