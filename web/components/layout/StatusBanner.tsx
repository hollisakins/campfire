'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle } from 'lucide-react';
import { fetchJson } from '@/lib/fetch-json';
import type { SiteStatus } from '@/app/api/status/route';

/** Polled every minute while all is well, every 15 s while the banner shows. */
export const STATUS_POLL_OK_MS = 60_000;
export const STATUS_POLL_BAD_MS = 15_000;

const STATE_TEXT: Record<Exclude<SiteStatus['state'], 'ok'>, string> = {
  degraded: 'Service degraded: sign-in and saving may fail.',
  down: 'Database unavailable: most pages will not load.',
};

/** The banner for one status answer, or null when there is nothing to say. */
export function statusBannerText(status: SiteStatus | undefined): string | null {
  if (!status) return null;
  if (status.message) return status.message;
  return status.state === 'ok' ? null : STATE_TEXT[status.state];
}

/**
 * The site-wide outage strip under the navigation (/api/status). Shows the
 * operator's message when there is one, else a line for a degraded or
 * unreachable database; clears itself on the next good answer. A failed poll
 * (the web server itself unreachable) shows nothing: the page would not have
 * loaded either.
 */
export function StatusBanner() {
  const { data } = useQuery({
    queryKey: ['site-status'],
    queryFn: ({ signal }) => fetchJson<SiteStatus>('/api/status', { signal }),
    refetchInterval: query => (statusBannerText(query.state.data) ? STATUS_POLL_BAD_MS : STATUS_POLL_OK_MS),
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: STATUS_POLL_BAD_MS,
  });
  const text = statusBannerText(data);
  if (!text) return null;
  return (
    <div data-slot="status-banner" role="status" className="border-b border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-900/20">
      <div className="container mx-auto flex items-center gap-3 px-4 py-1.5 text-sm text-amber-800 dark:text-amber-200">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span>{text}</span>
      </div>
    </div>
  );
}
