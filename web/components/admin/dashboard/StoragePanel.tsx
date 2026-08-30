'use client';

import React from 'react';
import Link from 'next/link';
import { HardDrive } from 'lucide-react';
import type { StorageBudget } from '@/lib/actions/storage-registry';
import { Panel, PanelError, SkeletonRows, Meter, ViewAllLink } from './primitives';
import { fmtBytes, fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Storage: the budget meter plus the breakdowns get_storage_budget has always
// returned and nothing ever rendered — by product type, backend, and status.
// by_status is a list BESIDE the meter, never segments inside it: superseded/
// revoked bytes sit outside total_bytes (which counts active + tiles only),
// so stacking them into the budget bar would over-fill it.
// ---------------------------------------------------------------------------

const TOP_PRODUCTS = 6;

export function StoragePanel({
  budget, loading, error, onRetry,
}: {
  budget: StorageBudget | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <Panel icon={HardDrive} title="Storage"><SkeletonRows n={8} /></Panel>
    );
  }
  if (error || !budget) {
    return (
      <Panel icon={HardDrive} title="Storage">
        <PanelError message={error} onRetry={onRetry} />
      </Panel>
    );
  }

  const pct = Number(budget.pct_used) || 0;
  const fill = pct >= 90 ? 'bg-danger' : pct >= 80 ? 'bg-warning' : 'bg-success';
  const products = Object.entries(budget.by_product_type ?? {})
    .sort((a, b) => b[1] - a[1]);
  const top = products.slice(0, TOP_PRODUCTS);
  const otherBytes = products.slice(TOP_PRODUCTS).reduce((s, [, b]) => s + b, 0);
  const backends = Object.entries(budget.by_backend ?? {}).sort((a, b) => b[1] - a[1]);
  const statuses = budget.by_status ?? {};
  const reclaimable =
    (statuses.superseded?.bytes ?? 0) + (statuses.revoked?.bytes ?? 0);

  return (
    <Panel
      icon={HardDrive}
      title="Storage"
      right={
        <>
          <span className="tabular-nums text-text-secondary">
            {fmtBytes(budget.total_bytes)} / {fmtBytes(budget.cap_bytes)} · {pct}%
          </span>
          <ViewAllLink href="/admin/intermediate-products" label="Registry" />
        </>
      }
    >
      <div className="p-3 space-y-3">
        <Link href="/admin/intermediate-products" className="block">
          <Meter
            segments={[{ value: pct, className: fill, title: `${pct}% of cap` }]}
            totalOverride={100}
          />
        </Link>

        <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto] gap-x-6 gap-y-3">
          <div className="space-y-1 min-w-0">
            {top.map(([type, bytes]) => (
              <Link
                key={type}
                href={`/admin/intermediate-products?product=${encodeURIComponent(type)}`}
                className="flex items-center gap-2 group"
              >
                <span className="font-mono text-[11px] text-text-secondary group-hover:text-text-primary truncate w-40 shrink-0">
                  {type}
                </span>
                <Meter
                  segments={[{ value: bytes, className: 'bg-primary/50' }]}
                  totalOverride={top[0][1]}
                  height={4}
                  className="flex-1"
                />
                <span className="text-[11px] tabular-nums text-text-secondary w-16 text-right shrink-0">
                  {fmtBytes(bytes)}
                </span>
              </Link>
            ))}
            {otherBytes > 0 && (
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-text-tertiary w-40 shrink-0">
                  +{products.length - TOP_PRODUCTS} more
                </span>
                <span className="flex-1" />
                <span className="text-[11px] tabular-nums text-text-tertiary w-16 text-right shrink-0">
                  {fmtBytes(otherBytes)}
                </span>
              </div>
            )}
          </div>

          <div className="space-y-2 text-xs sm:w-44">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-text-tertiary mb-0.5">
                Backend
              </div>
              {backends.map(([b, bytes]) => (
                <Link
                  key={b}
                  href={`/admin/intermediate-products?backend=${encodeURIComponent(b)}`}
                  className="flex justify-between hover:text-text-primary text-text-secondary"
                >
                  <span className="uppercase">{b}</span>
                  <span className="tabular-nums">{fmtBytes(bytes)}</span>
                </Link>
              ))}
              {budget.tile_bytes > 0 && (
                <div className="flex justify-between text-text-tertiary" title="Map tiles are aggregated in map_layers, not registered per-object">
                  <span>tiles</span>
                  <span className="tabular-nums">{fmtBytes(budget.tile_bytes)}</span>
                </div>
              )}
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-text-tertiary mb-0.5">
                Status
              </div>
              {(['active', 'superseded', 'revoked'] as const).map((s) =>
                statuses[s] ? (
                  <Link
                    key={s}
                    href={`/admin/intermediate-products?status=${s}`}
                    className="flex justify-between hover:text-text-primary text-text-secondary"
                  >
                    <span>{s}</span>
                    <span className="tabular-nums">
                      {fmtCount(statuses[s].count)} · {fmtBytes(statuses[s].bytes)}
                    </span>
                  </Link>
                ) : null,
              )}
              {reclaimable > 0 && (
                <Link
                  href="/admin/intermediate-products?status=superseded"
                  className="flex justify-between text-warning hover:underline"
                >
                  <span>reclaimable</span>
                  <span className="tabular-nums">{fmtBytes(reclaimable)}</span>
                </Link>
              )}
            </div>
          </div>
        </div>

        <p className="text-[10px] text-text-tertiary">
          Logical sizes; compressed products store smaller (#479).
        </p>
      </div>
    </Panel>
  );
}
