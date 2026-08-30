'use client';

import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  X, Download, RefreshCw, FolderOpen, CheckCircle2, XCircle, Loader2, AlertCircle,
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { parseKey } from '@/lib/layout';
import {
  getStorageObjectDetail,
  presignStorageObjectDownload,
  headStorageObject,
  listBucketObjects,
  type StorageObjectDetail,
  type HeadResult,
  type BucketObject,
} from '@/lib/actions/storage-registry';
import type { DataBackend } from '@/lib/storage';

// ---------------------------------------------------------------------------
// Right-side slide-over drawer: full provenance for one registry object, plus
// the Phase-2 OSN-transparency actions (download / live HEAD verify / list the
// actual bucket prefix). Read-only inspection — nothing writes back.
// ---------------------------------------------------------------------------

function fmtBytes(n: number | null): string {
  if (n == null) return '—';
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

function fmtDate(ts: string | null): string {
  if (!ts) return '—';
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !iso.includes('+')) iso = iso + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function parentPrefix(key: string): string {
  const i = key.lastIndexOf('/');
  return i >= 0 ? key.slice(0, i + 1) : '';
}

function knownKeyLabel(key: string): string {
  try {
    const parsed = parseKey(key);
    const scope = Object.entries(parsed.scope)
      .map(([k, v]) => `${k}=${v}`)
      .join(', ');
    return `${parsed.productType}${scope ? ` (${scope})` : ''}`;
  } catch {
    return 'unrecognized key';
  }
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-2 py-1 text-sm">
      <span className="text-text-secondary">{label}</span>
      <span className="text-text-primary break-all">{children}</span>
    </div>
  );
}

export function StorageObjectDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['admin-storage-object-detail', id],
    queryFn: () => getStorageObjectDetail(id),
    staleTime: 30_000,
  });

  const [downloading, setDownloading] = useState(false);
  const [head, setHead] = useState<HeadResult | null>(null);
  const [headLoading, setHeadLoading] = useState(false);
  const [listing, setListing] = useState<BucketObject[] | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const obj: StorageObjectDetail | null = data?.object ?? null;

  const onDownload = async () => {
    setDownloading(true);
    const res = await presignStorageObjectDownload(id);
    setDownloading(false);
    const dl = res[id];
    if (dl?.url) window.location.assign(dl.url);
    else alert(dl?.error ?? 'Failed to presign download');
  };

  const onVerify = async () => {
    setHeadLoading(true);
    setHead(await headStorageObject(id));
    setHeadLoading(false);
  };

  const onList = async () => {
    if (!obj) return;
    setListLoading(true);
    setListError(null);
    const res = await listBucketObjects(obj.backend as DataBackend, parentPrefix(obj.storage_key));
    setListLoading(false);
    if (res.error) setListError(res.error);
    else setListing(res.objects);
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Scrim */}
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      {/* Panel */}
      <div className="relative w-full max-w-xl bg-card border-l border-border h-full overflow-y-auto shadow-xl">
        <div className="sticky top-0 bg-card border-b border-border px-5 py-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text-primary">Object detail</h2>
          <button onClick={onClose} className="text-text-secondary hover:text-text-primary">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-5">
          {isLoading ? (
            <div className="flex items-center gap-2 text-text-secondary">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : error || data?.error || !obj ? (
            <div className="flex items-center gap-2 text-red-700 dark:text-red-400 text-sm">
              <AlertCircle className="w-4 h-4" /> {data?.error ?? 'Failed to load object'}
            </div>
          ) : (
            <>
              {/* Key + actions */}
              <div>
                <p className="font-mono text-xs text-text-primary break-all bg-card-hover rounded p-2">
                  {obj.storage_key}
                </p>
                <p className="text-xs text-text-secondary mt-1">{knownKeyLabel(obj.storage_key)}</p>
                <div className="flex flex-wrap gap-2 mt-3">
                  <Button size="sm" onClick={onDownload} disabled={downloading}>
                    {downloading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                    Download
                  </Button>
                  <Button size="sm" variant="secondary" onClick={onVerify} disabled={headLoading}>
                    {headLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                    Verify in bucket
                  </Button>
                  <Button size="sm" variant="secondary" onClick={onList} disabled={listLoading}>
                    {listLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <FolderOpen className="w-4 h-4" />}
                    List prefix
                  </Button>
                </div>
              </div>

              {/* HEAD result */}
              {head && (
                <div className="rounded border border-border p-3 text-sm">
                  {head.error ? (
                    <span className="text-red-700 dark:text-red-400">{head.error}</span>
                  ) : head.present ? (
                    <div className="space-y-1">
                      <div className="flex items-center gap-2 text-green-700 dark:text-green-400">
                        <CheckCircle2 className="w-4 h-4" /> Present in bucket
                      </div>
                      <Row label="Live size">
                        {fmtBytes(head.contentLength)}
                        {head.sizeMatches === false && (
                          <span className="text-amber-600 dark:text-amber-400">
                            {' '}— differs from registry ({fmtBytes(head.registrySize)})
                          </span>
                        )}
                      </Row>
                      <Row label="Live ETag">{head.etag ?? '—'}</Row>
                      <Row label="Last modified">{fmtDate(head.lastModified)}</Row>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 text-red-700 dark:text-red-400">
                      <XCircle className="w-4 h-4" /> Not found in bucket — registry row is stale
                    </div>
                  )}
                </div>
              )}

              {/* Metadata */}
              <div className="border-t border-border pt-3">
                <Row label="Product">{obj.product_type}</Row>
                <Row label="Scope">{obj.observation ?? obj.field ?? '—'}</Row>
                <Row label="Exposure ref">{obj.exposure_ref ?? '—'}</Row>
                <Row label="Size">{fmtBytes(obj.size_bytes)}</Row>
                <Row label="Content type">{obj.content_type}</Row>
                <Row label="Backend / bucket">
                  <span className="uppercase">{obj.backend}</span> / {obj.bucket}
                </Row>
                <Row label="Status">{obj.status}</Row>
                <Row label="Content hash">{obj.content_hash}</Row>
                <Row label="SCI+DQ hash">{obj.sci_dq_hash ?? '—'}</Row>
                <Row label="WCS hash">{obj.wcs_hash ?? '—'}</Row>
                <Row label="cfpipe version">{obj.cfpipe_version ?? '—'}</Row>
                <Row label="Created">{fmtDate(obj.created_at)}</Row>
                <Row label="Updated">{fmtDate(obj.updated_at)}</Row>
              </div>

              {/* Producing deployment */}
              {data?.deployment && (
                <div className="border-t border-border pt-3">
                  <h3 className="text-sm font-medium text-text-primary mb-1">
                    Deployment #{data.deployment.id}
                  </h3>
                  <Row label="Status">{data.deployment.status}</Row>
                  <Row label="Deployed">{fmtDate(data.deployment.deployed_at)}</Row>
                  <Row label="cfpipe">{data.deployment.cfpipe_version ?? '—'}</Row>
                  <Row label="jwst">{data.deployment.jwst_version ?? '—'}</Row>
                  <Row label="CRDS">{data.deployment.crds_context ?? '—'}</Row>
                </div>
              )}

              {/* Lifecycle history */}
              {data && data.events.length > 0 && (
                <div className="border-t border-border pt-3">
                  <h3 className="text-sm font-medium text-text-primary mb-2">Lifecycle</h3>
                  <ul className="space-y-1 text-sm">
                    {data.events.map((e) => (
                      <li key={e.id} className="flex items-center justify-between gap-2">
                        <span className="text-text-primary">
                          {e.action}
                          {e.status_to ? ` → ${e.status_to}` : ''}
                        </span>
                        <span className="text-text-secondary whitespace-nowrap text-xs">
                          {e.actor_name ? `${e.actor_name} · ` : ''}{fmtDate(e.occurred_at)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Bucket prefix listing */}
              {(listing || listError) && (
                <div className="border-t border-border pt-3">
                  <h3 className="text-sm font-medium text-text-primary mb-2">
                    Bucket prefix <span className="font-mono text-xs text-text-secondary">{parentPrefix(obj.storage_key)}</span>
                  </h3>
                  {listError ? (
                    <span className="text-red-700 dark:text-red-400 text-sm">{listError}</span>
                  ) : listing && listing.length === 0 ? (
                    <span className="text-text-secondary text-sm">No objects under this prefix.</span>
                  ) : (
                    <ul className="space-y-0.5 text-xs font-mono">
                      {listing!.map((o) => (
                        <li key={o.key} className="flex items-center justify-between gap-2">
                          <span className="truncate text-text-primary" title={o.key}>
                            {o.key.slice(parentPrefix(obj.storage_key).length)}
                          </span>
                          <span className="flex items-center gap-2 whitespace-nowrap text-text-secondary">
                            {fmtBytes(o.size)}
                            {o.registered ? (
                              <span className="text-green-600 dark:text-green-400" title="Registered">●</span>
                            ) : (
                              <span className="text-amber-500" title="Not in registry">○</span>
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {listing && (
                    <p className="text-xs text-text-secondary mt-2">
                      <span className="text-green-600 dark:text-green-400">●</span> registered ·{' '}
                      <span className="text-amber-500">○</span> unregistered (blind to the registry)
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
