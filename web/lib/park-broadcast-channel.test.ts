import { describe, expect, it, vi } from 'vitest';
import { parkBroadcastChannel } from './park-broadcast-channel';

function pageshow(persisted: boolean): Event {
  return Object.assign(new Event('pageshow'), { persisted });
}

/** A module-state owner like lib/nircam-png-store.ts: a `channel` variable with `onmessage` wiring. */
function setup(initial: BroadcastChannel | null = new BroadcastChannel('park-test')) {
  let channel: BroadcastChannel | null = initial;
  const received: unknown[] = [];
  const wire = vi.fn((c: BroadcastChannel) => {
    c.onmessage = (ev) => received.push(ev.data);
  });
  if (channel) wire(channel);
  const created: BroadcastChannel[] = [];
  const target = new EventTarget();
  const onPark = vi.fn();
  const onRestore = vi.fn();
  const uninstall = parkBroadcastChannel<BroadcastChannel>({
    get: () => channel,
    set: (c) => {
      channel = c;
    },
    wire,
    onPark,
    onRestore,
    target,
    createChannel: (name) => {
      const c = new BroadcastChannel(name);
      created.push(c);
      return c;
    },
  });
  return { get: () => channel, received, wire, created, target, onPark, onRestore, uninstall, initial };
}

const tick = () => new Promise((r) => setTimeout(r, 20));

describe('parkBroadcastChannel', () => {
  it('closes the channel on pagehide and reopens and rewires one of the same name on a persisted pageshow', async () => {
    const s = setup();
    const otherTab = new BroadcastChannel('park-test');

    s.target.dispatchEvent(new Event('pagehide'));
    expect(s.get()).toBeNull();
    expect(s.onPark).toHaveBeenCalledTimes(1);
    // Parked: a post from another tab reaches nobody here.
    otherTab.postMessage({ type: 'while-parked' });
    await tick();
    expect(s.received).toEqual([]);

    s.target.dispatchEvent(pageshow(true));
    expect(s.created).toHaveLength(1);
    expect(s.get()).toBe(s.created[0]);
    expect(s.wire).toHaveBeenLastCalledWith(s.created[0], s.initial);
    expect(s.onRestore).toHaveBeenCalledTimes(1);

    otherTab.postMessage({ type: 'after-restore' });
    await tick();
    expect(s.received).toEqual([{ type: 'after-restore' }]);

    otherTab.close();
    s.get()?.close();
    s.uninstall();
  });

  it('ignores a non-persisted pageshow and a pageshow with no park before it', () => {
    const s = setup();
    s.target.dispatchEvent(pageshow(true));
    s.target.dispatchEvent(new Event('pagehide'));
    s.target.dispatchEvent(pageshow(false));
    expect(s.created).toHaveLength(0);
    expect(s.onRestore).not.toHaveBeenCalled();
    s.uninstall();
  });

  it('passes the first parked channel as `original` on every cycle and drops each replacement', () => {
    const s = setup();
    for (let i = 0; i < 3; i++) {
      s.target.dispatchEvent(new Event('pagehide'));
      s.target.dispatchEvent(pageshow(true));
    }
    expect(s.created).toHaveLength(3);
    for (const call of s.wire.mock.calls.slice(1)) expect((call as unknown[])[1]).toBe(s.initial);
    expect(s.get()).toBe(s.created[2]);
    s.get()?.close();
    s.uninstall();
  });

  it('with no channel to close: still runs onPark/onRestore, opens nothing', () => {
    const s = setup(null);
    s.target.dispatchEvent(new Event('pagehide'));
    s.target.dispatchEvent(pageshow(true));
    expect(s.created).toHaveLength(0);
    expect(s.onPark).toHaveBeenCalledTimes(1);
    expect(s.onRestore).toHaveBeenCalledTimes(1);
    s.uninstall();
  });

  it('a failed reopen is logged and retried on the next restore', () => {
    let channel: BroadcastChannel | null = new BroadcastChannel('park-test');
    const target = new EventTarget();
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let attempts = 0;
    const onRestore = vi.fn();
    parkBroadcastChannel<BroadcastChannel>({
      get: () => channel,
      set: (c) => {
        channel = c;
      },
      wire: () => {},
      onRestore,
      target,
      createChannel: (name) => {
        attempts += 1;
        if (attempts === 1) throw new Error('nope');
        return new BroadcastChannel(name);
      },
    });
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    expect(channel).toBeNull();
    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect(onRestore).toHaveBeenCalledTimes(1);

    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    expect(channel).not.toBeNull();
    (channel as BroadcastChannel | null)?.close();
    errorSpy.mockRestore();
  });

  it('uninstall removes the listeners', () => {
    const s = setup();
    s.uninstall();
    s.target.dispatchEvent(new Event('pagehide'));
    expect(s.get()).toBe(s.initial);
    s.initial?.close();
  });
});
