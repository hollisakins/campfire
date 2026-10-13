// Data-release export builder (P4b): paging into gzip JSONL, the budgeted
// run over the ledger (resume, failure isolation, budget cut), and the
// partition visibility rules the partitions route applies.
import { describe, it, expect, vi } from 'vitest';
import { gunzipSync } from 'zlib';

vi.mock('server-only', () => ({}));

import {
  buildPartitionFile,
  openComponentPartitions,
  partitionVisible,
  releaseVisible,
  runExportBudget,
  type DrExportLedgerRow,
} from './dr-exports';

describe('visibility', () => {
  const open = openComponentPartitions(['nirspec', 'nircam/cosmos', 'photometry/cosmos/COSMOS v1.2']);

  it('program partitions need the program; the release components are open on members', () => {
    const held = new Set(['pub']);
    expect(partitionVisible('spectra', 'pub', held, open)).toBe(true);
    expect(partitionVisible('spectra', 'priv', held, open)).toBe(false);
    expect(partitionVisible('members', 'nircam/cosmos', held, open)).toBe(true);
    expect(partitionVisible('members', 'photometry/cosmos/COSMOS v1.2', held, open)).toBe(true);
    expect([...open]).not.toContain('nirspec');
  });

  it('a component-shaped name is not open on other streams or outside the release (Codex on #600)', () => {
    const held = new Set(['pub']);
    // a private program slugged like a component: its program streams stay gated
    for (const stream of ['objects', 'spectra', 'line_fits', 'photometry']) {
      expect(partitionVisible(stream, 'nircam/cosmos', held, open)).toBe(false);
    }
    // a component the release did not freeze is not open either
    expect(partitionVisible('members', 'nircam/egs', held, open)).toBe(false);
    expect(partitionVisible('members', 'nircam/egs', new Set(['nircam/egs']), open)).toBe(true);
    expect(openComponentPartitions(null).size).toBe(0);
    expect(openComponentPartitions([1, 'nircam/x']).has('nircam/x')).toBe(true);
  });

  it('readers see published releases only; admins every state', () => {
    expect(releaseVisible({ state: 'published' }, false)).toBe(true);
    expect(releaseVisible({ state: 'frozen' }, false)).toBe(false);
    expect(releaseVisible({ state: 'withdrawn' }, false)).toBe(false);
    expect(releaseVisible({ state: 'frozen' }, true)).toBe(true);
  });
});

/** A fake service client: rpc + from('dr_exports') with a mutable ledger. */
function fakeSupabase(opts: {
  pages: Record<string, { next_cursor: string; row_json: unknown }[][]>;
  ledger: DrExportLedgerRow[];
  failRows?: string;
  /** stream/partition keys another run holds a claim on */
  busy?: string[];
}) {
  const calls: { name: string; params: Record<string, unknown> }[] = [];
  const ledger = opts.ledger;
  const client = {
    rpc: async (name: string, params: Record<string, unknown>) => {
      calls.push({ name, params });
      if (name === 'dr_export_begin') return { data: { state: 'exporting' }, error: null };
      if (name === 'dr_export_mark') {
        const key = `${params.p_stream}/${params.p_partition}`;
        if (params.p_state === 'building' && opts.busy?.includes(key)) {
          return { data: null, error: { message: `export file ${key} is being built by another run` } };
        }
        const row = ledger.find((r) => r.stream === params.p_stream && r.partition === params.p_partition);
        if (params.p_state === 'building') {
          if (row) row.state = 'building';
          return { data: { state: 'exporting', claim: `claim-${key}` }, error: null };
        }
        if (row) {
          row.state = params.p_state as DrExportLedgerRow['state'];
          if (params.p_state === 'ready') {
            row.sha256 = params.p_sha256 as string;
            row.size_bytes = params.p_size_bytes as number;
            row.rows = params.p_rows as number;
          }
          if (params.p_state === 'failed') row.error = params.p_error as string;
        }
        const pending = ledger.filter((r) => r.state !== 'ready').length;
        return { data: { state: pending === 0 ? 'exported' : 'exporting' }, error: null };
      }
      if (name === 'dr_export_rows') {
        const key = `${params.p_stream}/${params.p_partition}`;
        if (opts.failRows === key) return { data: null, error: { message: 'boom' } };
        const pages = opts.pages[key] ?? [[]];
        const idx = params.p_after === null ? 0 : pages.findIndex((p) => p[p.length - 1]?.next_cursor === params.p_after) + 1;
        return { data: pages[idx] ?? [], error: null };
      }
      throw new Error(`unexpected rpc ${name}`);
    },
    from: (table: string) => {
      if (table !== 'dr_exports') throw new Error(table);
      const q = {
        select: () => q,
        eq: () => q,
        order: () => q,
        then: (resolve: (v: unknown) => void) => resolve({ data: ledger.map((r) => ({ ...r })), error: null }),
      };
      return q;
    },
  };
  return { client: client as never, calls, ledger };
}

const entry = (stream: DrExportLedgerRow['stream'], partition: string, state: DrExportLedgerRow['state'] = 'pending'): DrExportLedgerRow => ({
  tag: 'DR-t', stream, partition, storage_key: `dr/DR-t/exports/${stream}/${partition}.jsonl.gz`,
  sha256: null, size_bytes: null, rows: null, state, built_at: null, error: null,
});

describe('buildPartitionFile', () => {
  it('pages with the cursor until a short page and gzips one row per line', async () => {
    const pages = {
      'spectra/pub': [
        [{ next_cursor: 'a', row_json: { id: 1 } }, { next_cursor: 'b', row_json: { id: 2 } }],
        [{ next_cursor: 'c', row_json: { id: 3 } }],
      ],
    };
    const { client, calls } = fakeSupabase({ pages, ledger: [] });
    const { body, rows } = await buildPartitionFile(client, 'DR-t', 'spectra', 'pub', 2);
    expect(rows).toBe(3);
    expect(gunzipSync(body).toString()).toBe('{"id":1}\n{"id":2}\n{"id":3}\n');
    expect(calls.map((c) => c.params.p_after)).toEqual([null, 'b']);
  });

  it('an empty partition is an empty gzip file', async () => {
    const { client } = fakeSupabase({ pages: {}, ledger: [] });
    const { body, rows } = await buildPartitionFile(client, 'DR-t', 'objects', 'none');
    expect(rows).toBe(0);
    expect(gunzipSync(body).toString()).toBe('');
  });
});

describe('runExportBudget', () => {
  const pages = {
    'objects/pub': [[{ next_cursor: 'A', row_json: { object_id: 'A' } }]],
    'spectra/pub': [[{ next_cursor: 's1', row_json: { spectrum_id: 's1' } }]],
    'members/nircam/f': [[{ next_cursor: 'k', row_json: { storage_key: 'k' } }]],
  };

  it('builds every non-ready partition, uploads, marks ready, reaches exported', async () => {
    const puts: { Key: string; Body: Buffer }[] = [];
    const s3 = { send: async (cmd: { input: { Key: string; Body: Buffer } }) => { puts.push(cmd.input); } };
    const { client, ledger, calls } = fakeSupabase({
      pages,
      ledger: [entry('objects', 'pub'), entry('spectra', 'pub', 'ready'), entry('members', 'nircam/f', 'failed')],
    });
    ledger[1].sha256 = 'sha256:old';
    const res = await runExportBudget(client, s3 as never, 'bucket', 'DR-t', 60_000);
    expect(res.built.map((b) => `${b.stream}/${b.partition}`)).toEqual(['objects/pub', 'members/nircam/f']);
    expect(res.failed).toEqual([]);
    expect(res.remaining).toBe(0);
    expect(res.incomplete).toBe(false);
    expect(res.state).toBe('exported');
    expect(puts.map((p) => p.Key)).toEqual([
      'dr/DR-t/exports/objects/pub.jsonl.gz', 'dr/DR-t/exports/members/nircam/f.jsonl.gz',
    ]);
    // the already-ready partition was neither rebuilt nor re-marked
    expect(calls.filter((c) => c.name === 'dr_export_rows' && c.params.p_stream === 'spectra')).toHaveLength(0);
    expect(ledger[1].sha256).toBe('sha256:old');
    const ready = ledger.find((r) => r.stream === 'objects')!;
    expect(ready.state).toBe('ready');
    expect(ready.sha256).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(ready.rows).toBe(1);
    expect(calls[0].name).toBe('dr_export_begin');
  });

  it('a failing partition is marked failed and the run continues', async () => {
    const s3 = { send: async () => {} };
    const { client, ledger } = fakeSupabase({
      pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')], failRows: 'objects/pub',
    });
    const res = await runExportBudget(client, s3 as never, 'bucket', 'DR-t', 60_000);
    expect(res.failed).toEqual([{ stream: 'objects', partition: 'pub', error: expect.stringContaining('boom') }]);
    expect(res.built.map((b) => b.stream)).toEqual(['spectra']);
    expect(res.remaining).toBe(1);
    expect(res.state).toBe('exporting');
    expect(ledger[0].state).toBe('failed');
    expect(ledger[0].error).toContain('boom');
  });

  it('stops at the budget and reports incomplete', async () => {
    const s3 = { send: async () => {} };
    const { client } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    let t = 0;
    const now = () => (t += 1000);
    const res = await runExportBudget(client, s3 as never, 'bucket', 'DR-t', 1500, now);
    expect(res.built).toHaveLength(1);
    expect(res.incomplete).toBe(true);
    expect(res.remaining).toBe(1);
  });

  it('skips a file another run holds and passes its claim on ready / failed (Codex on #600)', async () => {
    const s3 = { send: async () => {} };
    const { client, ledger, calls } = fakeSupabase({
      pages,
      failRows: 'members/nircam/f',
      busy: ['objects/pub'],
      ledger: [entry('objects', 'pub'), entry('spectra', 'pub'), entry('members', 'nircam/f')],
    });
    const res = await runExportBudget(client, s3 as never, 'bucket', 'DR-t', 60_000);
    expect(res.skipped).toEqual([
      { stream: 'objects', partition: 'pub', reason: expect.stringContaining('being built by another run') },
    ]);
    // the held file was neither built nor marked failed
    expect(calls.filter((c) => c.name === 'dr_export_rows' && c.params.p_stream === 'objects')).toHaveLength(0);
    expect(ledger[0].state).toBe('pending');
    const marks = calls.filter((c) => c.name === 'dr_export_mark' && c.params.p_state !== 'building');
    expect(marks.map((c) => [c.params.p_state, c.params.p_claim])).toEqual([
      ['ready', 'claim-spectra/pub'],
      ['failed', 'claim-members/nircam/f'],
    ]);
    expect(res.remaining).toBe(2);
  });

  it('keeps headroom for another partition before the function deadline (Codex on #600)', async () => {
    // each partition's upload takes 100 s: after the first, 180 s of a 280 s
    // budget are left, less than twice the slowest partition -> stop
    let t = 0;
    const s3 = { send: async () => { t += 100_000; } };
    const { client } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    const res = await runExportBudget(client, s3 as never, 'bucket', 'DR-t', 280_000, () => t);
    expect(res.built).toHaveLength(1);
    expect(res.incomplete).toBe(true);
    // fast partitions all fit
    t = 0;
    const fast = { send: async () => { t += 1_000; } };
    const { client: c2 } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    const res2 = await runExportBudget(c2, fast as never, 'bucket', 'DR-t', 280_000, () => t);
    expect(res2.built).toHaveLength(2);
    expect(res2.incomplete).toBe(false);
  });
});
