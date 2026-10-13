// Data-release export builder (P4b): paging into gzip JSONL, the budgeted
// run over the ledger (resume, failure isolation, budget cut), and the
// partition visibility rules the partitions route applies.
import { describe, it, expect, vi } from 'vitest';
import { gunzipSync } from 'zlib';

vi.mock('server-only', () => ({}));

import {
  buildPartitionFile,
  contentExportKey,
  openComponentPartitions,
  partitionVisible,
  releaseVisible,
  runExportBudget,
  EXPORT_SWEEP_MIN_AGE_MS,
  type DrExportLedgerRow,
} from './dr-exports';

/** An in-memory S3: Put / ListObjectsV2 / DeleteObject, keyed by command class. */
function fakeS3(seed: Record<string, number> = {}) {
  const store = new Map<string, { Body?: Buffer; modified: number }>();
  for (const [k, modified] of Object.entries(seed)) store.set(k, { modified });
  const puts: { Key: string; Body: Buffer }[] = [];
  const deletes: string[] = [];
  const s3 = {
    send: async (cmd: { constructor: { name: string }; input: Record<string, unknown> }) => {
      const input = cmd.input as { Key: string; Body: Buffer; Prefix: string };
      switch (cmd.constructor.name) {
        case 'PutObjectCommand':
          puts.push({ Key: input.Key, Body: input.Body });
          store.set(input.Key, { Body: input.Body, modified: Date.now() });
          return {};
        case 'ListObjectsV2Command':
          return {
            Contents: [...store.entries()].filter(([k]) => k.startsWith(input.Prefix))
              .map(([Key, v]) => ({ Key, LastModified: new Date(v.modified) })),
            IsTruncated: false,
          };
        case 'DeleteObjectCommand':
          deletes.push(input.Key);
          store.delete(input.Key);
          return {};
        default:
          throw new Error(`unexpected command ${cmd.constructor.name}`);
      }
    },
  };
  return { s3: s3 as never, send: s3.send, puts, deletes, store };
}

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
  /** the other run finishes the busy files while this one runs */
  busyFinishes?: boolean;
  /** stream/partition keys whose claim is taken over while this run builds them */
  loseClaim?: string[];
}) {
  const calls: { name: string; params: Record<string, unknown> }[] = [];
  const ledger = opts.ledger;
  const client = {
    rpc: async (name: string, params: Record<string, unknown>) => {
      calls.push({ name, params });
      if (name === 'dr_export_begin') return { data: { state: 'exporting' }, error: null };
      if (name === 'dr_export_status') {
        const remaining = ledger.filter((r) => r.state !== 'ready').length;
        return { data: { state: ledger.length > 0 && remaining === 0 ? 'exported' : 'exporting', remaining }, error: null };
      }
      if (name === 'dr_export_mark') {
        const key = `${params.p_stream}/${params.p_partition}`;
        const row = ledger.find((r) => r.stream === params.p_stream && r.partition === params.p_partition);
        if (params.p_state === 'building' && opts.busy?.includes(key)) {
          // the other run finishes it meanwhile (the recount must see that)
          if (opts.busyFinishes && row) row.state = 'ready';
          return { data: null, error: { message: `export file ${key} is being built by another run` } };
        }
        if (params.p_state === 'building') {
          if (row) row.state = 'building';
          return { data: { state: 'exporting', claim: `claim-${key}`, base_key: row?.storage_key }, error: null };
        }
        if (opts.loseClaim?.includes(key) && params.p_claim) {
          // another run took the file over and finished it meanwhile
          if (row) row.state = 'ready';
          if (params.p_state === 'failed') return { data: { stale_claim: true, state: 'exported' }, error: null };
          return { data: null, error: { message: `export file ${key} is no longer claimed by this run` } };
        }
        if (row) {
          row.state = params.p_state as DrExportLedgerRow['state'];
          if (params.p_state === 'ready') {
            row.storage_key = contentExportKey(row.storage_key as string, params.p_sha256 as string, params.p_claim as string);
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
      if (table === 'dr_releases') {
        // the release state the ledger implies (what dr_derive_state gives)
        const state = ledger.length > 0 && ledger.every((r) => r.state === 'ready') ? 'exported' : 'exporting';
        const r = {
          select: () => r,
          eq: () => r,
          maybeSingle: async () => ({ data: { tag: 'DR-t', state }, error: null }),
        };
        return r;
      }
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
    const { s3, puts } = fakeS3();
    const { client, ledger, calls } = fakeSupabase({
      pages,
      ledger: [entry('objects', 'pub'), entry('spectra', 'pub', 'ready'), entry('members', 'nircam/f', 'failed')],
    });
    ledger[1].sha256 = 'sha256:old';
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.built.map((b) => `${b.stream}/${b.partition}`)).toEqual(['objects/pub', 'members/nircam/f']);
    expect(res.failed).toEqual([]);
    expect(res.remaining).toBe(0);
    expect(res.incomplete).toBe(false);
    expect(res.state).toBe('exported');
    // per-build keys (content hash + claim), and the ledger records the same ones (Codex on #600)
    expect(puts.map((p) => p.Key)).toEqual([
      expect.stringMatching(/^dr\/DR-t\/exports\/objects\/pub\.[0-9a-f]{16}\.[^.]{8}\.jsonl\.gz$/),
      expect.stringMatching(/^dr\/DR-t\/exports\/members\/nircam\/f\.[0-9a-f]{16}\.[^.]{8}\.jsonl\.gz$/),
    ]);
    expect(ledger.find((r) => r.stream === 'objects')!.storage_key).toBe(puts[0].Key);
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
    const { s3 } = fakeS3();
    const { client, ledger } = fakeSupabase({
      pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')], failRows: 'objects/pub',
    });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.failed).toEqual([{ stream: 'objects', partition: 'pub', error: expect.stringContaining('boom') }]);
    expect(res.built.map((b) => b.stream)).toEqual(['spectra']);
    expect(res.remaining).toBe(1);
    expect(res.state).toBe('exporting');
    expect(ledger[0].state).toBe('failed');
    expect(ledger[0].error).toContain('boom');
  });

  it('stops at the budget and reports incomplete', async () => {
    const { s3 } = fakeS3();
    const { client } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    let t = 0;
    const now = () => (t += 1000);
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 1500, now);
    expect(res.built).toHaveLength(1);
    expect(res.incomplete).toBe(true);
    expect(res.remaining).toBe(1);
  });

  it('content key format matches dr_export_content_key', () => {
    const sha = `sha256:${'0123456789abcdef'.repeat(4)}`;
    const claim = 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff';
    expect(contentExportKey('dr/T/exports/spectra/pub.jsonl.gz', sha, claim))
      .toBe('dr/T/exports/spectra/pub.0123456789abcdef.a1b2c3d4.jsonl.gz');
    expect(contentExportKey('dr/T/exports/members/nircam_f-1a2b3c4d.jsonl.gz', sha, claim))
      .toBe('dr/T/exports/members/nircam_f-1a2b3c4d.0123456789abcdef.a1b2c3d4.jsonl.gz');
  });

  it('every build has its own key, never the published one (Codex on #600)', async () => {
    // different bytes, or the same bytes under another claim, land on different keys
    const sha1 = `sha256:${'a'.repeat(64)}`;
    const sha2 = `sha256:${'b'.repeat(64)}`;
    const base = 'dr/DR-t/exports/members/pub.jsonl.gz';
    const c1 = '11111111-0000-4000-8000-000000000000';
    const c2 = '22222222-0000-4000-8000-000000000000';
    expect(contentExportKey(base, sha1, c1)).not.toBe(contentExportKey(base, sha2, c1));
    expect(contentExportKey(base, sha1, c1)).not.toBe(contentExportKey(base, sha1, c2));
  });

  it('a lost claim is skipped, not failed, when another run finished the file (Codex on #600)', async () => {
    const { s3, puts, deletes, store } = fakeS3();
    const { client } = fakeSupabase({ pages, loseClaim: ['objects/pub'], ledger: [entry('objects', 'pub')] });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.failed).toEqual([]);
    expect(res.skipped).toEqual([
      { stream: 'objects', partition: 'pub', reason: expect.stringContaining('claim lost while building') },
    ]);
    expect(res.remaining).toBe(0);
    // its own upload is deleted at once, not left for a sweep that never returns (Codex on #600)
    expect(puts).toHaveLength(1);
    expect(deletes).toEqual([puts[0].Key]);
    expect(store.has(puts[0].Key)).toBe(false);
  });

  it('once every file is ready, old objects the ledger does not name are swept (Codex on #600)', async () => {
    const old = Date.now() - EXPORT_SWEEP_MIN_AGE_MS - 60_000;
    const { s3, deletes, store } = fakeS3({
      'dr/DR-t/exports/objects/pub.aaaaaaaaaaaaaaaa.11111111.jsonl.gz': old,    // superseded build
      'dr/DR-t/exports/objects/pub.bbbbbbbbbbbbbbbb.22222222.jsonl.gz': Date.now(), // too young: may be in flight
      'dr/DR-u/exports/objects/pub.cccccccccccccccc.33333333.jsonl.gz': old,    // another release
    });
    const { client, ledger } = fakeSupabase({ pages, ledger: [entry('objects', 'pub')] });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.remaining).toBe(0);
    expect(res.swept).toBe(1);
    expect(deletes).toEqual(['dr/DR-t/exports/objects/pub.aaaaaaaaaaaaaaaa.11111111.jsonl.gz']);
    // the file the ledger names stays, as do the young and the foreign ones
    expect(store.has(ledger[0].storage_key as string)).toBe(true);
    expect(store.size).toBe(3);
  });

  it('nothing is swept while files remain', async () => {
    const old = Date.now() - EXPORT_SWEEP_MIN_AGE_MS - 60_000;
    const { s3, deletes } = fakeS3({ 'dr/DR-t/exports/objects/x.aaaaaaaaaaaaaaaa.11111111.jsonl.gz': old });
    const { client } = fakeSupabase({ pages, failRows: 'objects/pub', ledger: [entry('objects', 'pub')] });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.remaining).toBe(1);
    expect(res.swept).toBe(0);
    expect(deletes).toEqual([]);
  });

  it('remaining is recounted after a concurrent run finished a skipped file (Codex on #600)', async () => {
    const { s3 } = fakeS3();
    const { client } = fakeSupabase({
      pages, busy: ['objects/pub'], busyFinishes: true, ledger: [entry('objects', 'pub')],
    });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
    expect(res.skipped).toHaveLength(1);
    expect(res.remaining).toBe(0);
    // the state is re-read too, not this run's stale `exporting` (Codex on #600)
    expect(res.state).toBe('exported');
  });

  it('skips a file another run holds and passes its claim on ready / failed (Codex on #600)', async () => {
    const { s3 } = fakeS3();
    const { client, ledger, calls } = fakeSupabase({
      pages,
      failRows: 'members/nircam/f',
      busy: ['objects/pub'],
      ledger: [entry('objects', 'pub'), entry('spectra', 'pub'), entry('members', 'nircam/f')],
    });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 60_000);
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
    const slow = fakeS3();
    const s3 = { send: async (cmd: never) => { t += 100_000; return slow.send(cmd); } } as never;
    const { client } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    const res = await runExportBudget(client, s3, 'bucket', 'DR-t', 280_000, () => t);
    expect(res.built).toHaveLength(1);
    expect(res.incomplete).toBe(true);
    // fast partitions all fit
    t = 0;
    const quick = fakeS3();
    const fast = { send: async (cmd: never) => { t += 1_000; return quick.send(cmd); } };
    const { client: c2 } = fakeSupabase({ pages, ledger: [entry('objects', 'pub'), entry('spectra', 'pub')] });
    const res2 = await runExportBudget(c2, fast as never, 'bucket', 'DR-t', 280_000, () => t);
    expect(res2.built).toHaveLength(2);
    expect(res2.incomplete).toBe(false);
  });
});
