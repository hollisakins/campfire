import { describe, expect, it, vi } from 'vitest';
import { createOncePerUser } from './once-per-user';

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('createOncePerUser', () => {
  it('runs the load once for concurrent callers with the same id (the boot race)', async () => {
    const d = deferred<string>();
    const load = vi.fn(() => d.promise);
    const gate = createOncePerUser(load);

    // SIGNED_IN subscriber and getSession().then, 1 ms apart
    const a = gate.run('u1');
    const b = gate.run('u1');
    expect(load).toHaveBeenCalledTimes(1);
    expect(b).toBe(a);

    d.resolve('profile');
    await expect(a).resolves.toBe('profile');
    expect(gate.userId).toBe('u1');

    // Later events for the same user are no-ops too (TOKEN_REFRESHED)
    await gate.run('u1');
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('runs again for a different user id', async () => {
    const load = vi.fn(async (id: string) => id);
    const gate = createOncePerUser(load);
    await gate.run('u1');
    await gate.run('u2');
    expect(load.mock.calls.map(([id]) => id)).toEqual(['u1', 'u2']);
    expect(gate.userId).toBe('u2');
  });

  it('force re-runs for the same id (USER_UPDATED, refreshProfile)', async () => {
    const load = vi.fn(async (id: string) => id);
    const gate = createOncePerUser(load);
    await gate.run('u1');
    await gate.run('u1', { force: true });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('reset forgets the id so the next run loads again', async () => {
    const load = vi.fn(async (id: string) => id);
    const gate = createOncePerUser(load);
    await gate.run('u1');
    gate.reset('u2'); // not the current id: no-op
    await gate.run('u1');
    expect(load).toHaveBeenCalledTimes(1);
    gate.reset('u1');
    expect(gate.userId).toBeNull();
    await gate.run('u1');
    expect(load).toHaveBeenCalledTimes(2);
    gate.reset();
    expect(gate.userId).toBeNull();
  });

  it('a rejected load is not deduped: the next run retries', async () => {
    const load = vi
      .fn<(id: string) => Promise<string>>()
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce('profile');
    const gate = createOncePerUser(load);
    await expect(gate.run('u1')).rejects.toThrow('network');
    expect(gate.userId).toBeNull();
    await expect(gate.run('u1')).resolves.toBe('profile');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('a rejection from a superseded run does not clear a newer one', async () => {
    const first = deferred<string>();
    const load = vi.fn<(id: string) => Promise<string>>().mockReturnValueOnce(first.promise).mockResolvedValueOnce('fresh');
    const gate = createOncePerUser(load);
    const stale = gate.run('u1');
    await gate.run('u1', { force: true });
    first.reject(new Error('late failure'));
    await expect(stale).rejects.toThrow('late failure');
    expect(gate.userId).toBe('u1');
  });
});
