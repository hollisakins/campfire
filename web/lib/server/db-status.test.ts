// The shared database probe: a stall is reported only after BAD_SAMPLES bad
// samples, one good sample clears it, probes are single-flight and cached for
// PROBE_TTL_MS, and the operator message comes from a file.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

vi.mock('server-only', () => ({}));

type Answer = { data: unknown; error: unknown } | 'throw';
let answer: Answer = { data: null, error: null };
const rpc = vi.fn();
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: () => ({
    rpc: (name: string) => {
      rpc(name);
      return {
        abortSignal: async () => {
          if (answer === 'throw') throw new Error('aborted');
          return answer;
        },
      };
    },
  }),
}));

import {
  classify,
  getDbStatus,
  getStatusMessage,
  resetDbStatus,
  resetStatusMessage,
  PROBE_TTL_MS,
  WAL_STALL_S,
} from './db-status';

const healthy = { wal_waiters: 0, wal_wait_s: 0, lock_waiters: 0, lock_wait_s: 0 };
const walStall = { wal_waiters: 3, wal_wait_s: 540, lock_waiters: 0, lock_wait_s: 0 };

/** Answer the next probe with `a`, after the cache has expired. */
async function sample(a: Answer) {
  answer = a;
  vi.advanceTimersByTime(PROBE_TTL_MS);
  return getDbStatus();
}

describe('classify', () => {
  it('names the stall', () => {
    expect(classify(healthy)).toBeNull();
    expect(classify(null)).toBe('unreachable');
    expect(classify(walStall)).toBe('wal');
    expect(classify({ ...healthy, wal_waiters: 1, wal_wait_s: WAL_STALL_S - 1 })).toBeNull();
    expect(classify({ ...healthy, lock_waiters: 2, lock_wait_s: 45 })).toBe('locks');
  });
});

describe('getDbStatus', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetDbStatus();
    rpc.mockClear();
  });
  afterEach(() => vi.useRealTimers());

  it('reports ok while the probe is healthy, through db_stall_status', async () => {
    expect(await sample({ data: healthy, error: null })).toMatchObject({ state: 'ok', reason: null });
    expect(rpc).toHaveBeenCalledWith('db_stall_status');
  });

  it('raises a WAL stall on the second bad sample and clears on one good one', async () => {
    await sample({ data: healthy, error: null });
    expect((await sample({ data: walStall, error: null })).state).toBe('ok');
    expect(await sample({ data: walStall, error: null })).toMatchObject({ state: 'degraded', reason: 'wal' });
    expect((await sample({ data: healthy, error: null })).state).toBe('ok');
  });

  it('reports down when the probe errors or times out', async () => {
    await sample({ data: healthy, error: null });
    await sample({ data: null, error: { message: 'boom' } });
    expect(await sample('throw')).toMatchObject({ state: 'down', reason: 'unreachable' });
  });

  it('reports the first bad sample of a process that never had a good one (deploy gate)', async () => {
    expect(await sample({ data: null, error: { message: 'no such function' } })).toMatchObject({
      state: 'down',
      reason: 'unreachable',
    });
    expect((await sample({ data: healthy, error: null })).state).toBe('ok');
    expect((await sample({ data: walStall, error: null })).state).toBe('ok');
  });

  it('caches a sample for PROBE_TTL_MS and shares one probe between callers', async () => {
    answer = { data: healthy, error: null };
    await Promise.all([getDbStatus(), getDbStatus(), getDbStatus()]);
    expect(rpc).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(PROBE_TTL_MS - 1);
    await getDbStatus();
    expect(rpc).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    await getDbStatus();
    expect(rpc).toHaveBeenCalledTimes(2);
  });
});

describe('getStatusMessage', () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'status-'));
    resetStatusMessage();
  });
  afterEach(() => {
    delete process.env.STATUS_MESSAGE_FILE;
    rmSync(dir, { recursive: true, force: true });
  });

  it('is null without a configured or existing file', async () => {
    expect(await getStatusMessage()).toBeNull();
    process.env.STATUS_MESSAGE_FILE = join(dir, 'missing');
    resetStatusMessage();
    expect(await getStatusMessage()).toBeNull();
  });

  it('reads the trimmed file, capped at 300 characters, empty = none', async () => {
    const f = join(dir, 'message');
    process.env.STATUS_MESSAGE_FILE = f;
    writeFileSync(f, '  Jetstream2 storage incident.\n');
    expect(await getStatusMessage()).toBe('Jetstream2 storage incident.');
    writeFileSync(f, 'x'.repeat(400));
    resetStatusMessage();
    expect(await getStatusMessage()).toHaveLength(300);
    writeFileSync(f, '\n');
    resetStatusMessage();
    expect(await getStatusMessage()).toBeNull();
  });
});
