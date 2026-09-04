import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import worker from './index';
import {
  objectTokenMessage,
  decodeObjectKey,
  upstreamPathMatchesKey,
  contentTypeFor,
  objectCacheKey,
} from './object';

const SECRET = 'test-shared-secret';
const KEY = 'data/products/nirspec/obs/x_spec.json';
const HASH = 'sha256:0123abcd';
const UPSTREAM = `https://uaz1.osn.mghpcc.org/campfire-jwst/${KEY}?X-Amz-Signature=deadbeef`;
const ENV = { JWT_SECRET: SECRET, ALLOWED_ORIGINS: 'https://campfire.hollisakins.com', ALLOWED_FETCH_HOSTS: 'uaz1.osn.mghpcc.org' };

// Mirror of the app's mint (lib/server/worker-token.ts) — proves sign/verify agree.
async function sign(message: string, secret: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  const b64 = btoa(String.fromCharCode(...new Uint8Array(sig)));
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

async function objectUrl(opts: { key?: string; hash?: string; exp?: number; upstream?: string; secret?: string } = {}) {
  const key = opts.key ?? KEY;
  const hash = opts.hash ?? HASH;
  const exp = String(opts.exp ?? Math.floor(Date.now() / 1000) + 3600);
  const upstream = opts.upstream ?? UPSTREAM;
  const t = await sign(objectTokenMessage(key, hash, exp, upstream), opts.secret ?? SECRET);
  const path = key.split('/').map(encodeURIComponent).join('/');
  return `https://campfire-download.hollisakins.com/o/${path}?h=${encodeURIComponent(hash)}&e=${exp}&t=${t}&u=${encodeURIComponent(upstream)}`;
}

/** A tiny in-memory stand-in for the Workers Cache API (full objects only;
 * answers Range from a stored body as 206 the way Cloudflare's does). */
function fakeCache() {
  const store = new Map<string, { headers: Headers; body: Uint8Array }>();
  const cache = {
    async match(req: Request) {
      const hit = store.get(req.url);
      if (!hit) return undefined;
      const range = req.headers.get('Range');
      if (range) {
        const m = /^bytes=(\d+)-(\d+)?$/.exec(range)!;
        const start = Number(m[1]);
        const end = m[2] ? Math.min(Number(m[2]), hit.body.length - 1) : hit.body.length - 1;
        const h = new Headers(hit.headers);
        h.set('Content-Range', `bytes ${start}-${end}/${hit.body.length}`);
        h.set('Content-Length', String(end - start + 1));
        return new Response(hit.body.slice(start, end + 1), { status: 206, headers: h });
      }
      return new Response(hit.body, { status: 200, headers: hit.headers });
    },
    async put(req: Request, res: Response) {
      store.set(req.url, { headers: new Headers(res.headers), body: new Uint8Array(await res.arrayBuffer()) });
    },
  };
  return { cache, store };
}

describe('object helpers', () => {
  it('decodes a segment-encoded key and rejects traversal', () => {
    expect(decodeObjectKey('data/products/a%20b/x.json')).toBe('data/products/a b/x.json');
    expect(decodeObjectKey('data/../x.json')).toBeNull();
    expect(decodeObjectKey('data//x.json')).toBeNull();
    expect(decodeObjectKey('data/%2Fx.json')).toBeNull();
    expect(decodeObjectKey('')).toBeNull();
  });

  it('accepts path-style and virtual-hosted upstream paths for the key only', () => {
    expect(upstreamPathMatchesKey(`https://uaz1.osn.mghpcc.org/campfire-jwst/${KEY}?x=1`, KEY)).toBe(true);
    expect(upstreamPathMatchesKey(`https://bucket.acct.r2.cloudflarestorage.com/${KEY}`, KEY)).toBe(true);
    expect(upstreamPathMatchesKey(`https://uaz1.osn.mghpcc.org/campfire-jwst/other/${KEY}`, KEY)).toBe(false);
    expect(upstreamPathMatchesKey(`https://uaz1.osn.mghpcc.org/campfire-jwst/${KEY}`, 'data/other.json')).toBe(false);
  });

  it('maps product extensions to browser-usable content types', () => {
    expect(contentTypeFor('a/b.json', 'binary/octet-stream')).toBe('application/json');
    expect(contentTypeFor('a/b.png', null)).toBe('image/png');
    expect(contentTypeFor('a/b.fits', 'application/octet-stream')).toBe('application/fits');
    expect(contentTypeFor('a/b.bin', 'text/plain')).toBe('text/plain');
    expect(contentTypeFor('a/b.bin', 'application/octet-stream')).toBe('application/octet-stream');
  });

  it('keys the cache by key + hash on the Worker origin, never by the presigned url', () => {
    const k = objectCacheKey('https://w.example', KEY, HASH);
    expect(k).toBe(`https://w.example/_cache/o/${KEY}?h=${encodeURIComponent(HASH)}`);
    expect(k).not.toContain('X-Amz');
  });
});

describe('/o/ handler', () => {
  let fetched: { url: string; init?: RequestInit }[];
  beforeEach(() => {
    fetched = [];
    vi.stubGlobal('fetch', async (input: string, init?: RequestInit) => {
      fetched.push({ url: input, init });
      const range = (init?.headers as Record<string, string> | undefined)?.Range;
      const body = 'ABCDEFGHIJ';
      if (range) {
        const m = /^bytes=(\d+)-(\d+)$/.exec(range)!;
        const s = Number(m[1]), e = Number(m[2]);
        return new Response(body.slice(s, e + 1), {
          status: 206,
          headers: { 'Content-Type': 'binary/octet-stream', 'Content-Range': `bytes ${s}-${e}/${body.length}`, 'Content-Length': String(e - s + 1) },
        });
      }
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'binary/octet-stream', 'Content-Length': String(body.length), ETag: '"abc"' },
      });
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('answers the preflight with * and Range allowed', async () => {
    const res = await worker.fetch(new Request(await objectUrl(), { method: 'OPTIONS' }), ENV);
    expect(res.status).toBe(204);
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect(res.headers.get('Access-Control-Allow-Headers')).toContain('Range');
  });

  it('serves a full object with CORS *, a mapped content type and the immutable serve policy', async () => {
    const res = await worker.fetch(new Request(await objectUrl()), ENV);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('ABCDEFGHIJ');
    expect(res.headers.get('Content-Type')).toBe('application/json');
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect(res.headers.get('Access-Control-Expose-Headers')).toContain('Content-Range');
    expect(res.headers.get('Cache-Control')).toBe('private, max-age=86400, immutable');
    expect(res.headers.get('X-Cache')).toBe('MISS');
    expect(fetched[0].init?.redirect).toBe('manual');
  });

  it('rejects a tampered hash, an expired token, the wrong secret and a foreign upstream', async () => {
    const good = await objectUrl();
    const tamperedHash = good.replace(encodeURIComponent(HASH), encodeURIComponent('sha256:ffff'));
    expect((await worker.fetch(new Request(tamperedHash), ENV)).status).toBe(403);

    const expired = await objectUrl({ exp: Math.floor(Date.now() / 1000) - 10 });
    expect((await worker.fetch(new Request(expired), ENV)).status).toBe(403);

    const wrongSecret = await objectUrl({ secret: 'other' });
    expect((await worker.fetch(new Request(wrongSecret), ENV)).status).toBe(403);

    // A valid token for a different upstream host is still refused by the allowlist.
    const foreign = await objectUrl({ upstream: `https://evil.example/campfire-jwst/${KEY}` });
    expect((await worker.fetch(new Request(foreign), ENV)).status).toBe(403);

    // A valid token whose upstream names another key cannot fill this key's slot.
    const mismatch = await objectUrl({ upstream: 'https://uaz1.osn.mghpcc.org/campfire-jwst/data/other.json?X=1' });
    expect((await worker.fetch(new Request(mismatch), ENV)).status).toBe(403);
    expect(fetched).toHaveLength(0);
  });

  it('forwards Range upstream on a miss and returns the 206 untouched (the partial is never stored)', async () => {
    const { cache, store } = fakeCache();
    vi.stubGlobal('caches', { default: cache });
    const res = await worker.fetch(new Request(await objectUrl(), { headers: { Range: 'bytes=2-4' } }), ENV);
    expect(res.status).toBe(206);
    expect(await res.text()).toBe('CDE');
    expect(res.headers.get('Content-Range')).toBe('bytes 2-4/10');
    expect(res.headers.get('Accept-Ranges')).toBe('bytes');
    expect(store.size).toBe(0);
  });

  it('a Range miss fills the slot with one background full GET, so the next Range hits', async () => {
    const { cache, store } = fakeCache();
    vi.stubGlobal('caches', { default: cache });
    const pending: Promise<unknown>[] = [];
    const ctx = { waitUntil: (p: Promise<unknown>) => { pending.push(p); } };
    // Hold the full-object upstream answer until released, the way a 16 MB
    // fill outlives the Range answers that triggered it.
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const upstream = globalThis.fetch;
    vi.stubGlobal('fetch', async (input: string, init?: RequestInit) => {
      if (!(init?.headers as Record<string, string> | undefined)?.Range) await gate;
      return upstream(input, init);
    });

    // The FITS reader's shape: two Range requests against a cold slot.
    const head = await worker.fetch(new Request(await objectUrl(), { headers: { Range: 'bytes=0-1' } }), ENV, ctx as never);
    const data = await worker.fetch(new Request(await objectUrl(), { headers: { Range: 'bytes=2-9' } }), ENV, ctx as never);
    expect(head.headers.get('X-Cache')).toBe('MISS');
    expect(data.headers.get('X-Cache')).toBe('MISS');
    expect(await head.text()).toBe('AB');
    expect(await data.text()).toBe('CDEFGHIJ');
    expect(store.size).toBe(0);
    release();
    await Promise.all(pending);

    // Two ranged upstream GETs plus exactly one full fill (deduplicated).
    const full = fetched.filter((f) => !(f.init?.headers as Record<string, string> | undefined)?.Range);
    expect(full).toHaveLength(1);
    expect(store.size).toBe(1);

    const again = await worker.fetch(new Request(await objectUrl(), { headers: { Range: 'bytes=2-4' } }), ENV, ctx as never);
    expect(again.status).toBe(206);
    expect(again.headers.get('X-Cache')).toBe('HIT');
    expect(await again.text()).toBe('CDE');
    expect(fetched).toHaveLength(3);
  });

  it('stores a full 200 under (key, hash) and answers later GETs and Ranges from the cache', async () => {
    const { cache, store } = fakeCache();
    vi.stubGlobal('caches', { default: cache });
    const ctx = { waitUntil: (p: Promise<unknown>) => { pending.push(p); } };
    const pending: Promise<unknown>[] = [];

    const first = await worker.fetch(new Request(await objectUrl()), ENV, ctx as never);
    expect(await first.text()).toBe('ABCDEFGHIJ');
    await Promise.all(pending);
    expect([...store.keys()][0]).toBe(objectCacheKey('https://campfire-download.hollisakins.com', KEY, HASH));

    const second = await worker.fetch(new Request(await objectUrl()), ENV, ctx as never);
    expect(second.headers.get('X-Cache')).toBe('HIT');
    expect(await second.text()).toBe('ABCDEFGHIJ');
    expect(second.headers.get('Access-Control-Allow-Origin')).toBe('*');

    const ranged = await worker.fetch(new Request(await objectUrl(), { headers: { Range: 'bytes=0-2' } }), ENV, ctx as never);
    expect(ranged.status).toBe(206);
    expect(await ranged.text()).toBe('ABC');
    expect(ranged.headers.get('X-Cache')).toBe('HIT');
    expect(fetched).toHaveLength(1);
  });

  it('a new hash for the same key is a different cache slot', async () => {
    const { cache, store } = fakeCache();
    vi.stubGlobal('caches', { default: cache });
    const pending: Promise<unknown>[] = [];
    const ctx = { waitUntil: (p: Promise<unknown>) => { pending.push(p); } };
    await (await worker.fetch(new Request(await objectUrl()), ENV, ctx as never)).text();
    await (await worker.fetch(new Request(await objectUrl({ hash: 'sha256:new' })), ENV, ctx as never)).text();
    await Promise.all(pending);
    expect(store.size).toBe(2);
    expect(fetched).toHaveLength(2);
  });

  it('maps an upstream 404 and refuses upstream redirects', async () => {
    vi.stubGlobal('fetch', async () => new Response('nope', { status: 404 }));
    expect((await worker.fetch(new Request(await objectUrl()), ENV)).status).toBe(404);
    vi.stubGlobal('fetch', async () => new Response(null, { status: 302, headers: { Location: 'https://evil.example/' } }));
    expect((await worker.fetch(new Request(await objectUrl()), ENV)).status).toBe(502);
  });

  it('the legacy /proxy endpoint still works alongside', async () => {
    const res = await worker.fetch(new Request('https://campfire-download.hollisakins.com/proxy'), ENV);
    expect(res.status).toBe(400);
  });
});
