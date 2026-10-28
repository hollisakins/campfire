// Retain-on-supersede (P2, narrowed to frozen release members by P4e): the
// plan against a fake registry and a fake `retain_required_hashes`, and the
// copy step against a fake S3 client. The S3 semantics mirrored here (HEAD
// 404, conditional copy 412) are the ones measured against OSN on 2026-10-11.
import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { CopyObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';
import { executeRetain, planRetain, summarizeDecisions, type LiveRow } from './retain';

const SHA_A = 'sha256:' + 'a'.repeat(64);
const SHA_B = 'sha256:' + 'b'.repeat(64);
const LIVE_SPEC = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits';
const LEGACY_SPEC = 'spectra/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits';
const LIVE_EXPOSURE = 'data/products/nirspec/ember_egs_p1/jw07076020001_04101_00001_nrs1_15182.fits';
const ARCHIVE_SPEC = `archive/${'a'.repeat(64)}/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits`;

function row(over: Partial<LiveRow> = {}): LiveRow {
  return {
    id: 1,
    storage_key: LIVE_SPEC,
    content_hash: SHA_A,
    size_bytes: 1000,
    stored_size_bytes: null,
    content_type: 'application/fits',
    product_type: 'nirspec_spec',
    instrument: 'nirspec',
    observation: 'ember_egs_p1',
    spectrum_id: 'ember_egs_p1_prism_clear_15182',
    program_slug: 'ember',
    deployment_id: 42,
    cfpipe_version: '0.6.0',
    updated_at: '2026-10-01T00:00:00Z',
    ...over,
  };
}

/**
 * Minimal PostgREST fake: `.from('storage_objects').select().in().eq().eq()` → rows;
 * `rpc('retain_required_hashes')` → the asked hashes that are in `required` (a frozen,
 * unarchived release member; by default the fixture row's hash so the plan tests
 * exercise the retain path).
 */
function fakeSupabase(rows: LiveRow[], required: string[] = [SHA_A], collecting: string[] = []) {
  const upserts: Array<{ row: Record<string, unknown>; opts: unknown }> = [];
  const rpcs: Array<{ name: string; args: unknown }> = [];
  const client = {
    from: (table: string) => ({
      select: () => ({
        in: (_col: string, keys: string[]) => ({
          eq: () => ({
            eq: async () => ({ data: rows.filter((r) => keys.includes(r.storage_key)), error: null }),
          }),
        }),
      }),
      upsert: async (r: Record<string, unknown>, opts: unknown) => {
        if (table !== 'storage_objects') throw new Error('unexpected table');
        upserts.push({ row: r, opts });
        return { error: null };
      },
    }),
    rpc: async (name: string, args: unknown) => {
      rpcs.push({ name, args });
      if (name === 'retain_required_hashes') {
        const asked = (args as { p_hashes: string[] }).p_hashes;
        return { data: asked.filter((h) => required.includes(h)).sort(), error: null };
      }
      if (name === 'archive_register_retained') {
        const row = (args as { p_row: Record<string, unknown> }).p_row;
        if (out.bucket) out.bucketCallsAtRegister = out.bucket.sent.map((c) => c.kind);
        if (collecting.includes(row.storage_key as string)) {
          return { data: null, error: new Error(`archive object ${row.storage_key} is being collected (status collecting); retry`) };
        }
        upserts.push({ row, opts: { rpc: name } });
        return { data: { inserted: true }, error: null };
      }
      return { data: null, error: null };
    },
  };
  const out: { client: never; upserts: typeof upserts; rpcs: typeof rpcs; bucket?: { sent: Array<{ kind: string }> }; bucketCallsAtRegister?: string[] } = {
    client: client as never,
    upserts,
    rpcs,
  };
  return out;
}

/** Fake S3 client: `send(cmd)` dispatches on the command class. */
function fakeS3(objects: Record<string, { size: number; etag: string }>, opts: { copy412?: boolean } = {}) {
  const sent: Array<{ kind: string; input: Record<string, unknown> }> = [];
  const notFound = () => Object.assign(new Error('NotFound'), { name: 'NotFound', $metadata: { httpStatusCode: 404 } });
  const client = {
    send: async (cmd: HeadObjectCommand | CopyObjectCommand) => {
      const input = cmd.input as unknown as Record<string, unknown>;
      if (cmd instanceof HeadObjectCommand) {
        sent.push({ kind: 'head', input });
        const o = objects[input.Key as string];
        if (!o) throw notFound();
        return { ContentLength: o.size, ETag: `"${o.etag}"` };
      }
      if (cmd instanceof CopyObjectCommand) {
        sent.push({ kind: 'copy', input });
        if (opts.copy412) throw Object.assign(new Error('PreconditionFailed'), { name: 'PreconditionFailed', $metadata: { httpStatusCode: 412 } });
        const src = (input.CopySource as string).split('/').slice(1).join('/');
        objects[input.Key as string] = { ...objects[src] };
        return { CopyObjectResult: { ETag: `"${objects[src].etag}"` } };
      }
      throw new Error('unexpected command');
    },
  };
  return { client: client as never, sent, objects };
}

describe('planRetain', () => {
  it('retains a changed live NIRSpec final, by canonical key, with the archive key of the OLD hash', async () => {
    const { client } = fakeSupabase([row()]);
    const [d] = await planRetain([{ key: LEGACY_SPEC, newHash: SHA_B }], client);
    expect(d.action).toBe('retain');
    if (d.action === 'retain') {
      expect(d.archiveKey).toBe(ARCHIVE_SPEC);
      expect(d.row.storage_key).toBe(LIVE_SPEC);
    }
  });

  it('skips new keys, non-retained types, no-op re-uploads and provisional hashes', async () => {
    const { client } = fakeSupabase([
      row(),
      row({ id: 2, storage_key: LIVE_EXPOSURE, product_type: 'nirspec_spectrum_exposure' }),
      row({ id: 3, storage_key: LIVE_SPEC.replace('15182', '99999'), content_hash: 'etag:deadbeef', spectrum_id: 'x' }),
    ]);
    const ds = await planRetain(
      [
        { key: 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_1_spec.fits' },
        { key: LIVE_EXPOSURE },
        { key: LIVE_SPEC, newHash: SHA_A },
        { key: LIVE_SPEC.replace('15182', '99999') },
      ],
      client,
    );
    expect(ds.map((d) => d.action)).toEqual(['skip-new', 'skip-type', 'skip-unchanged', 'skip-provisional-hash']);
    expect(summarizeDecisions(ds)).toEqual({ 'skip-new': 1, 'skip-type': 1, 'skip-unchanged': 1, 'skip-provisional-hash': 1 });
  });

  it('reports drift when the registry hash moved past the plan (for a referenced object)', async () => {
    const { client } = fakeSupabase([row({ content_hash: SHA_B })], [SHA_B]);
    const [d] = await planRetain([{ key: LIVE_SPEC, expectedOldHash: SHA_A }], client);
    expect(d.action).toBe('drift');
  });

  it('skips an object no frozen release still needs, whatever the client planned against', async () => {
    const { client, rpcs } = fakeSupabase([row(), row({ id: 2, storage_key: LIVE_EXPOSURE, product_type: 'nirspec_spectrum_exposure' })], []);
    const ds = await planRetain([{ key: LIVE_SPEC, newHash: SHA_B }, { key: LIVE_SPEC, expectedOldHash: SHA_B }, { key: LIVE_EXPOSURE }], client);
    expect(ds.map((d) => d.action)).toEqual(['skip-unreferenced', 'skip-unreferenced', 'skip-type']);
    // asked once, about the one retainable hash only
    expect(rpcs).toEqual([{ name: 'retain_required_hashes', args: { p_hashes: [SHA_A] } }]);
  });

  it('asks nothing when no candidate passes the local tests', async () => {
    const { client, rpcs } = fakeSupabase([row(), row({ id: 2, storage_key: LIVE_EXPOSURE, product_type: 'nirspec_spectrum_exposure' })]);
    const ds = await planRetain([{ key: LIVE_SPEC, newHash: SHA_A }, { key: LIVE_EXPOSURE }], client);
    expect(ds.map((d) => d.action)).toEqual(['skip-unchanged', 'skip-type']);
    expect(rpcs).toEqual([]);
  });

  it('surfaces a failing lookup instead of guessing', async () => {
    const { client } = fakeSupabase([row()]);
    (client as unknown as { rpc: unknown }).rpc = async () => ({ data: null, error: new Error('boom') });
    await expect(planRetain([{ key: LIVE_SPEC, newHash: SHA_B }], client)).rejects.toThrow('boom');
  });

  it('never considers archive rows (root filter) and tolerates unknown keys', async () => {
    const { client } = fakeSupabase([]);
    const ds = await planRetain([{ key: 'totally/made/up.bin' }], client);
    expect(ds[0].action).toBe('skip-new');
  });
});

describe('executeRetain', () => {
  const decision = async () => {
    const { client } = fakeSupabase([row()]);
    const [d] = await planRetain([{ key: LIVE_SPEC, newHash: SHA_B }], client);
    if (d.action !== 'retain') throw new Error('expected retain');
    return d;
  };

  it('copies with the live ETag precondition and registers the archive row', async () => {
    const d = await decision();
    const s3 = fakeS3({ [LIVE_SPEC]: { size: 1000, etag: 'e1' } });
    const sb = fakeSupabase([]);
    sb.bucket = s3;
    const out = await executeRetain(d, { s3: s3.client, bucket: 'campfire-jwst', supabase: sb.client, actorId: 'u1', now: () => new Date('2026-10-11T00:00:00Z') });
    expect(out).toEqual({ key: LIVE_SPEC, status: 'archived', archiveKey: ARCHIVE_SPEC, copied: true });
    const copy = s3.sent.find((c) => c.kind === 'copy')!;
    expect(copy.input.CopySource).toBe(`campfire-jwst/${LIVE_SPEC}`);
    expect(copy.input.CopySourceIfMatch).toBe('e1');
    expect(copy.input.Key).toBe(ARCHIVE_SPEC);
    expect(sb.upserts).toHaveLength(1);
    const r = sb.upserts[0].row;
    expect(r).toMatchObject({
      storage_key: ARCHIVE_SPEC, root: 'archive', status: 'archived', content_hash: SHA_A,
      archived_from_key: LIVE_SPEC, program_slug: 'ember', spectrum_id: 'ember_egs_p1_prism_clear_15182',
      product_type: 'nirspec_spec', archived_at: '2026-10-11T00:00:00.000Z', uploaded_by: 'u1',
    });
    expect(sb.upserts[0].opts).toMatchObject({ rpc: 'archive_register_retained' });
    expect(sb.rpcs.map((r) => r.name)).toEqual(['archive_register_retained', 'log_deploy_event']);
    expect(sb.rpcs[1]).toMatchObject({ name: 'log_deploy_event', args: { p_action: 'archive' } });
    // the row went in before the copy (P4e): the bucket had seen only the live HEAD by then
    expect(sb.bucketCallsAtRegister).toEqual(['head']);
  });

  it('does not copy again when the archive object already exists (dedup by hash) but still registers', async () => {
    const d = await decision();
    const s3 = fakeS3({ [LIVE_SPEC]: { size: 1000, etag: 'e1' }, [ARCHIVE_SPEC]: { size: 1000, etag: 'e1' } });
    const sb = fakeSupabase([]);
    const out = await executeRetain(d, { s3: s3.client, bucket: 'b', supabase: sb.client });
    expect(out).toMatchObject({ status: 'archived', copied: false });
    expect(s3.sent.filter((c) => c.kind === 'copy')).toHaveLength(0);
    expect(sb.upserts).toHaveLength(1);
  });

  it('fails closed when a collection has claimed the archive key (registration refused)', async () => {
    // the object is in the bucket (a purge is deleting it): no copy is made, the
    // registration refuses, the gate reports an error so the deploy retries later
    const sb = fakeSupabase([row()], [SHA_A], [ARCHIVE_SPEC]);
    const s3 = fakeS3({ [LIVE_SPEC]: { size: 1000, etag: 'e1' }, [ARCHIVE_SPEC]: { size: 1000, etag: 'e1' } });
    const [d] = await planRetain([{ key: LIVE_SPEC, newHash: SHA_B }], sb.client);
    if (d.action !== 'retain') throw new Error('expected retain');
    const out = await executeRetain(d, { s3: s3.client, bucket: 'campfire-jwst', supabase: sb.client });
    expect(out.status).toBe('error');
    if (out.status === 'error') expect(out.reason).toContain('being collected');
    expect(sb.upserts).toHaveLength(0);
    expect(sb.rpcs.map((r) => r.name)).not.toContain('log_deploy_event');
    expect(s3.sent.filter((c) => c.kind === 'copy')).toHaveLength(0);
  });

  it('reports drift when the live size no longer matches the registry', async () => {
    const d = await decision();
    const s3 = fakeS3({ [LIVE_SPEC]: { size: 999, etag: 'e1' } });
    const sb = fakeSupabase([]);
    const out = await executeRetain(d, { s3: s3.client, bucket: 'b', supabase: sb.client });
    expect(out.status).toBe('drift');
    expect(s3.sent.filter((c) => c.kind === 'copy')).toHaveLength(0);
    expect(sb.upserts).toHaveLength(0);
  });

  it('reports drift when the conditional copy fails its precondition (412)', async () => {
    const d = await decision();
    const s3 = fakeS3({ [LIVE_SPEC]: { size: 1000, etag: 'e1' } }, { copy412: true });
    const sb = fakeSupabase([]);
    const out = await executeRetain(d, { s3: s3.client, bucket: 'b', supabase: sb.client });
    expect(out).toMatchObject({ status: 'drift' });
    // the row went in before the copy (P4e): it stays, dangling, which is what
    // `verify --cloud` reports when frozen bytes were lost to a write outside a deploy
    expect(sb.upserts).toHaveLength(1);
  });

  it('reports drift when the live object is missing (dangling registry row)', async () => {
    const d = await decision();
    const s3 = fakeS3({});
    const sb = fakeSupabase([]);
    const out = await executeRetain(d, { s3: s3.client, bucket: 'b', supabase: sb.client });
    expect(out).toMatchObject({ status: 'drift' });
  });
});
