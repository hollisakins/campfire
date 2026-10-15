// /releases pages (P6a), server side: the export files a viewer sees on a
// release page are exactly the ones /api/v1/dr/<tag>/files would hand them.
import { describe, expect, it, vi } from 'vitest';

vi.mock('server-only', () => ({}));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({}) }));

import type { DrExportLedgerRow } from './dr-exports';
import { visibleFiles } from './dr-releases';

function row(stream: DrExportLedgerRow['stream'], partition: string, state: DrExportLedgerRow['state'] = 'ready'): DrExportLedgerRow {
  return {
    tag: 'DR-t', stream, partition, state,
    storage_key: state === 'ready' ? `dr/DR-t/exports/${partition}/${stream}.jsonl.gz` : null,
    sha256: 'ab'.repeat(32), size_bytes: 100, rows: 3, built_at: null, error: null,
  };
}

describe('visibleFiles', () => {
  const components = ['nirspec', 'nircam/cosmos'];
  const ledger = [
    row('spectra', 'pub'),
    row('spectra', 'priv'),
    row('objects', 'pub', 'building'),
    row('manifest', 'nircam/cosmos'),
    row('spectra', 'nircam/cosmos'),
  ];

  it('keeps ready files within the viewer programs plus the open manifest components', () => {
    const files = visibleFiles(ledger, new Set(['pub']), components);
    expect(files.map((f) => `${f.stream}:${f.scope}`)).toEqual(['spectra:pub', 'manifest:nircam/cosmos']);
  });

  it('never exposes the storage key', () => {
    const [f] = visibleFiles(ledger, new Set(['pub']), components);
    expect(Object.keys(f).sort()).toEqual(['rows', 'scope', 'sha256', 'size', 'stream']);
  });

  it('opens a component scope only when the release lists it', () => {
    expect(visibleFiles(ledger, new Set(), ['nirspec'])).toEqual([]);
  });
});
