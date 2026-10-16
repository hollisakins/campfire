// @vitest-environment node
// The catalog query keys carry the release (P6c-1): live and release pages
// never share a cache entry, and the /nircam index refetches when the tag flips.
import React from 'react';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

const useQuery = vi.fn((_opts: { queryKey: unknown[] }) => ({ data: undefined, isFetching: false }));
vi.mock('@tanstack/react-query', () => ({
  useQuery: (o: { queryKey: unknown[] }) => useQuery(o),
  useQueryClient: () => ({ getQueryData: () => undefined, setQueryData: () => {}, prefetchQuery: () => {} }),
  keepPreviousData: (x: unknown) => x,
}));
vi.mock('@/lib/actions/spectra', () => ({ getSpectra: vi.fn() }));

import { DrProvider } from '@/lib/dr/client';
import { useSpectraQuery } from './useSpectraQuery';

const Probe = () => {
  useSpectraQuery({ filters: {}, page: 1, pageSize: 25, sortColumn: 'object_id', sortDirection: 'asc', viewMode: 'objects' });
  return null;
};

describe('useSpectraQuery keys', () => {
  it('key on "live" without a release and on the tag under one', () => {
    useQuery.mockClear();
    renderToString(<DrProvider tag={null}><Probe /></DrProvider>);
    renderToString(<DrProvider tag="DR-seed"><Probe /></DrProvider>);
    const keys = useQuery.mock.calls.map((c) => c[0].queryKey);
    expect(keys[0][0]).toBe('spectra');
    expect((keys[0][1] as { dr: string }).dr).toBe('live');
    expect((keys[1][1] as { dr: string }).dr).toBe('DR-seed');
  });
});
