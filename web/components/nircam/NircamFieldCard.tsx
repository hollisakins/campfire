'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { ImageIcon } from 'lucide-react';
import type { NircamFieldCard as FieldCard } from '@/lib/types';

const formatVolume = (bytes: number): string => {
  if (!bytes) return '—';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

// Sky coverage label: deg² when it reads naturally, arcmin² for small fields.
const formatCoverage = (card: FieldCard): string => {
  if (card.coverage_area_deg2 != null && card.coverage_area_deg2 >= 0.05) {
    return `${card.coverage_area_deg2.toFixed(2)} deg²`;
  }
  if (card.coverage_area_arcmin2 != null) {
    return `${Math.round(card.coverage_area_arcmin2)} arcmin²`;
  }
  return '—';
};

const formatDate = (iso: string | null): string =>
  iso ? iso.slice(0, 10) : '—';

interface NircamFieldCardProps {
  card: FieldCard;
}

/**
 * One landing-grid card: the field's `_layout.png` coverage plot as the
 * preview (dark placeholder until the field's first post-redesign deploy),
 * name + center overlay, key stats, and a link into /nircam/[field].
 */
export const NircamFieldCard: React.FC<NircamFieldCardProps> = ({ card }) => {
  // A presigned URL can exist while the object 404s (e.g. preview branches
  // without bucket data) — fall back to the placeholder on load error.
  const [imgFailed, setImgFailed] = useState(false);
  const showImg = card.layout_url && !imgFailed;

  return (
    <Link
      href={`/nircam/${encodeURIComponent(card.field)}`}
      className="group block bg-card border border-border rounded-xl overflow-hidden transition-all hover:border-border-strong hover:-translate-y-0.5"
    >
      {/* Layout-plot preview */}
      <div className="relative h-40 border-b border-border bg-[#0d0b12]">
        {showImg ? (
          // Presigned cross-origin URL; next/image won't optimize it and the
          // plot is already a small rendered PNG — plain <img> is correct here.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={card.layout_url as string}
            alt={`${card.display_name} coverage layout`}
            className="w-full h-full object-cover"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <ImageIcon className="w-8 h-8 text-text-tertiary opacity-40" />
          </div>
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-transparent pointer-events-none" />
        {card.center_ra != null && card.center_dec != null && (
          <span className="absolute right-3 top-2.5 font-mono text-[10.5px] text-white/80 bg-black/40 rounded px-1.5 py-0.5">
            α {card.center_ra.toFixed(2)} · δ {card.center_dec >= 0 ? '+' : ''}
            {card.center_dec.toFixed(2)}
          </span>
        )}
        <span className="absolute left-3.5 bottom-2.5 text-xl font-bold text-white drop-shadow-lg">
          {card.display_name}
        </span>
      </div>

      {/* Stats */}
      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-x-5 gap-y-2 mb-2.5">
          {[
            [card.n_filters, 'Filters'],
            [card.n_tiles, 'Tiles'],
            [formatCoverage(card), 'Coverage'],
            [formatVolume(card.total_bytes), 'Volume'],
          ].map(([n, k]) => (
            <div key={k as string}>
              <div className="text-sm font-semibold text-text-primary">{n}</div>
              <div className="text-[10.5px] uppercase tracking-wide text-text-tertiary">{k}</div>
            </div>
          ))}
        </div>
        <div className="flex items-center justify-between border-t border-border pt-2 text-[11.5px] text-text-tertiary">
          <span>Last reduced {formatDate(card.last_updated)}</span>
          <span className="font-mono text-primary opacity-0 group-hover:opacity-100 transition-opacity">
            Open →
          </span>
        </div>
      </div>
    </Link>
  );
};
