// The NIRCam bulk-download script (lib/nircam-download-script.ts).
//
// Two layers. The text tests pin the contract the field page relies on: one
// `fetch` line per product, every download through GET /api/v1/storage/download
// with a url-encoded key, and no presigned url baked in (the old script carried
// ~6 h presigned urls, and a whole-field download ran longer than that).
//
// The end-to-end tests run the generated script with bash + curl against an
// in-process HTTP server that plays the API (bearer check, 302 to the "store")
// and the store (byte ranges, one transfer cut mid-way), and check what the
// user actually cares about: a failed run is re-run and finishes — complete
// files skipped, partial ones resumed, the rest reported.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { createServer, type Server } from 'node:http';
import { spawn, spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { AddressInfo } from 'node:net';
import type { NircamProductRow } from '@/lib/types';
import {
  buildNircamDownloadScript,
  downloadRouteUrl,
  expectedBytes,
  localFilename,
  shellQuote,
  transferBytes,
} from './nircam-download-script';

const ORIGIN = 'https://campfire.example.org';

function mosaic(field: string, filt: string, name: string, size?: number): NircamProductRow {
  return {
    kind: 'mosaic',
    field,
    filter: filt,
    tile: 'tile1',
    pixel_scale: '30mas',
    extension: 'sci',
    epoch: '',
    file_path: `data/products/nircam/${field}/${filt}/${name}`,
    file_size: size === undefined ? undefined : size * 3,
    file_size_stored: size,
  };
}

function expmap(field: string, filt: string, name: string, size?: number): NircamProductRow {
  return {
    kind: 'expmap',
    field,
    filter: filt,
    tile: null,
    pixel_scale: null,
    extension: 'exp',
    file_path: `data/products/nircam/${field}/${filt}/${name}`,
    file_size: size,
  };
}

describe('buildNircamDownloadScript (text)', () => {
  const rows = [
    mosaic('cosmos', 'f444w', 'mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz', 1000),
    expmap('cosmos', 'f444w', 'expmap_f444w.fits', 200),
    mosaic('egs', 'f277w', 'mosaic_nircam_f277w_egs_30mas_tile1_sci.fits.gz', 3000),
  ];

  it('is empty for an empty selection', () => {
    expect(buildNircamDownloadScript([], ORIGIN)).toBe('');
  });

  it('emits one fetch line per product, filed under its field, with the url-encoded key and transfer size', () => {
    const script = buildNircamDownloadScript(rows, ORIGIN);
    const fetches = script.split('\n').filter((l) => l.startsWith('fetch '));
    expect(fetches).toEqual([
      `fetch 'data%2Fproducts%2Fnircam%2Fcosmos%2Ff444w%2Fmosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz' 'cosmos/mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz' 1000`,
      `fetch 'data%2Fproducts%2Fnircam%2Fcosmos%2Ff444w%2Fexpmap_f444w.fits' 'cosmos/expmap_f444w.fits' 200`,
      `fetch 'data%2Fproducts%2Fnircam%2Fegs%2Ff277w%2Fmosaic_nircam_f277w_egs_30mas_tile1_sci.fits.gz' 'egs/mosaic_nircam_f277w_egs_30mas_tile1_sci.fits.gz' 3000`,
    ]);
    // The header accounts for the transfer (stored/gzipped) bytes, not logical.
    expect(script).toContain('# Files: 3');
    expect(script).toContain('# Total size: 4.1 KB');
  });

  it('downloads through the API route at the given origin and carries no presigned url', () => {
    const script = buildNircamDownloadScript(rows, `${ORIGIN}/`);
    expect(script).toContain(`BASE_URL='${ORIGIN}'`);
    expect(script).toContain('"$BASE_URL/api/v1/storage/download?key=$key"');
    expect(script).toContain('"$BASE_URL/api/v1/auth/whoami"');
    expect(script).not.toMatch(/X-Amz-|sig=|\/proxy\?/);
    // Resumable by construction: partial downloads land in .part and resume.
    expect(script).toContain('-C -');
    expect(script).toContain('mv -f "$part" "$file"');
    // And the user is told where the key comes from.
    expect(script).toContain(`${ORIGIN}/profile/api-keys`);
  });

  it('has an unknown size (0) for a product the registry did not size', () => {
    const script = buildNircamDownloadScript([expmap('egs', 'f277w', 'expmap_f277w.fits')], ORIGIN);
    expect(script).toContain(`'egs/expmap_f277w.fits' 0`);
  });

  it('never checks a gzipped mosaic against its logical size', () => {
    // The page attaches stored sizes fail-open, so a compressed mosaic can
    // arrive with only file_size (uncompressed). Checking the .fits.gz on disk
    // against that would fail every run; the script must treat it as unknown.
    const row = mosaic('cosmos', 'f444w', 'mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz');
    row.file_size = 123456;
    expect(expectedBytes(row)).toBe(0);
    expect(buildNircamDownloadScript([row], ORIGIN)).toContain(
      `'cosmos/mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz' 0`,
    );
    // The header still estimates the transfer from what it has.
    expect(buildNircamDownloadScript([row], ORIGIN)).toContain('# Total size: 120.6 KB');
    // Uncompressed products are checked against their logical size.
    expect(expectedBytes(expmap('a', 'f', 'e.fits', 7))).toBe(7);
  });

  it('helpers', () => {
    expect(transferBytes(mosaic('a', 'f', 'm.fits.gz', 10))).toBe(10);
    expect(transferBytes(expmap('a', 'f', 'e.fits', 7))).toBe(7);
    expect(transferBytes(expmap('a', 'f', 'e.fits'))).toBe(0);
    expect(localFilename(mosaic('a', 'f', 'm.fits.gz'))).toBe('m.fits.gz');
    expect(shellQuote(`it's`)).toBe(`'it'\\''s'`);
    expect(downloadRouteUrl(ORIGIN, 'data/products/nircam/a b')).toBe(
      `${ORIGIN}/api/v1/storage/download?key=data%2Fproducts%2Fnircam%2Fa%20b`,
    );
  });
});

// ---------------------------------------------------------------------------
// End to end: bash + curl against a fake API/store.

const haveTools =
  spawnSync('bash', ['--version']).status === 0 && spawnSync('curl', ['--version']).status === 0;

const API_KEY = 'sk_test_key';

interface FakeObject {
  bytes: Buffer;
  /** Close the connection after this many bytes on the first store request. */
  cutFirstAt?: number;
}

describe.skipIf(!haveTools)('generated script, end to end', () => {
  let server: Server;
  let origin = '';
  let tmp = '';
  const objects = new Map<string, FakeObject>();
  const routeHits = new Map<string, number>();
  const storeRanges = new Map<string, (string | null)[]>();

  const objA = 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz';
  const objB = 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_tile1_wht.fits.gz';
  const objC = 'data/products/nircam/cosmos/f444w/expmap_f444w.fits'; // never authorized
  const objD = 'data/products/nircam/egs/f277w/expmap_f277w.fits'; // size unknown to the script

  const bytesA = Buffer.alloc(64 * 1024, 'A');
  const bytesB = Buffer.alloc(96 * 1024, 'B');
  const bytesD = Buffer.alloc(1024, 'D');

  const rows: NircamProductRow[] = [
    mosaic('cosmos', 'f444w', 'mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz', bytesA.length),
    mosaic('cosmos', 'f444w', 'mosaic_nircam_f444w_cosmos_30mas_tile1_wht.fits.gz', bytesB.length),
    expmap('cosmos', 'f444w', 'expmap_f444w.fits', 555),
    expmap('egs', 'f277w', 'expmap_f277w.fits'),
  ];

  beforeAll(async () => {
    objects.set(objA, { bytes: bytesA });
    objects.set(objB, { bytes: bytesB, cutFirstAt: 40 * 1024 });
    objects.set(objD, { bytes: bytesD });

    server = createServer((req, res) => {
      const url = new URL(req.url || '/', 'http://localhost');
      const authed = req.headers.authorization === `Bearer ${API_KEY}`;

      if (url.pathname === '/api/v1/auth/whoami') {
        res.writeHead(authed ? 200 : 401, { 'Content-Type': 'application/json' });
        res.end('{}');
        return;
      }

      if (url.pathname === '/api/v1/storage/download') {
        if (!authed) {
          res.writeHead(401, { 'Content-Type': 'application/json' });
          res.end('{"error":"Invalid or missing authentication"}');
          return;
        }
        const key = url.searchParams.get('key') || '';
        routeHits.set(key, (routeHits.get(key) ?? 0) + 1);
        if (!objects.has(key)) {
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end('{"error":"Not found or not accessible"}');
          return;
        }
        // The "presigned url": the store on this same server.
        res.writeHead(302, {
          Location: `${origin}/store/${key.split('/').map(encodeURIComponent).join('/')}?X-Amz-Signature=fake`,
          'Cache-Control': 'no-store',
        });
        res.end();
        return;
      }

      if (url.pathname.startsWith('/store/')) {
        const key = decodeURIComponent(url.pathname.slice('/store/'.length));
        const obj = objects.get(key);
        if (!obj) {
          res.writeHead(404);
          res.end();
          return;
        }
        const ranges = storeRanges.get(key) ?? [];
        const range = req.headers.range ?? null;
        ranges.push(range);
        storeRanges.set(key, ranges);

        let start = 0;
        const m = range ? /^bytes=(\d+)-$/.exec(range) : null;
        if (m) start = Number(m[1]);
        if (start >= obj.bytes.length) {
          res.writeHead(416, { 'Content-Range': `bytes */${obj.bytes.length}` });
          res.end();
          return;
        }
        const body = obj.bytes.subarray(start);
        res.writeHead(start > 0 ? 206 : 200, {
          'Content-Type': 'application/octet-stream',
          'Content-Length': String(body.length),
          'Accept-Ranges': 'bytes',
          ...(start > 0 ? { 'Content-Range': `bytes ${start}-${obj.bytes.length - 1}/${obj.bytes.length}` } : {}),
        });
        if (obj.cutFirstAt !== undefined && ranges.length === 1) {
          // Mid-transfer failure: send part of the body, then close the
          // connection (FIN, so the bytes sent are delivered — a reset could
          // discard them client-side and the resume offset would be moot).
          res.write(body.subarray(0, obj.cutFirstAt), () => req.socket.end());
          return;
        }
        res.end(body);
        return;
      }

      res.writeHead(404);
      res.end();
    });

    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    tmp = mkdtempSync(join(tmpdir(), 'nircam-dl-'));
    writeFileSync(join(tmp, 'download.sh'), buildNircamDownloadScript(rows, origin));
    // The script backs off between attempts; a no-op `sleep` on PATH keeps the
    // test fast without a test-only knob in the product.
    mkdirSync(join(tmp, 'bin'));
    writeFileSync(join(tmp, 'bin', 'sleep'), '#!/bin/sh\nexit 0\n');
    chmodSync(join(tmp, 'bin', 'sleep'), 0o755);
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    if (tmp) rmSync(tmp, { recursive: true, force: true });
  });

  // Async, not spawnSync: the fake API/store runs in this process, so a
  // blocking spawn would deadlock (the server could never answer curl).
  function run(env: Record<string, string | undefined> = {}) {
    return new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve, reject) => {
      const childEnv: NodeJS.ProcessEnv = { ...process.env };
      const overrides: Record<string, string | undefined> = {
        PATH: `${join(tmp, 'bin')}:${process.env.PATH}`,
        HOME: tmp,
        CAMPFIRE_API_KEY: API_KEY,
        CAMPFIRE_DOWNLOAD_DIR: join(tmp, 'out'),
        ...env,
      };
      // An `undefined` override unsets the variable (Node would otherwise
      // pass the string "undefined").
      for (const [k, v] of Object.entries(overrides)) {
        if (v === undefined) delete childEnv[k];
        else childEnv[k] = v;
      }
      const child = spawn('bash', [join(tmp, 'download.sh')], {
        cwd: tmp,
        env: childEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stdout = '';
      let stderr = '';
      child.stdout.setEncoding('utf8').on('data', (d: string) => { stdout += d; });
      child.stderr.setEncoding('utf8').on('data', (d: string) => { stderr += d; });
      child.on('error', reject);
      child.on('close', (status: number | null) => resolve({ status, stdout, stderr }));
    });
  }

  const outPath = (key: string) => join(tmp, 'out', key.split('/')[3], key.split('/').pop() as string);

  it('refuses to run without a key, and with a rejected key, touching no files', async () => {
    const noKey = await run({ CAMPFIRE_API_KEY: undefined });
    expect(noKey.status).toBe(1);
    expect(noKey.stderr).toContain('no API key');

    const badKey = await run({ CAMPFIRE_API_KEY: 'sk_wrong' });
    expect(badKey.status).toBe(1);
    expect(badKey.stderr).toContain('rejected this key (HTTP 401)');

    expect(existsSync(join(tmp, 'out'))).toBe(false);
    expect(routeHits.size).toBe(0);
  });

  it('first run: downloads, resumes the cut transfer, reports the unauthorized file', async () => {
    const r = await run();
    expect(r.status).toBe(1); // objC failed
    expect(readFileSync(outPath(objA)).equals(bytesA)).toBe(true);
    expect(readFileSync(outPath(objB)).equals(bytesB)).toBe(true);
    expect(readFileSync(outPath(objD)).equals(bytesD)).toBe(true);
    expect(existsSync(outPath(objC))).toBe(false);
    expect(existsSync(`${outPath(objB)}.part`)).toBe(false);

    // B was cut at 40 KB; the second attempt asked for a fresh url and resumed
    // from where the .part stopped instead of starting over.
    expect(routeHits.get(objB)).toBe(2);
    expect(storeRanges.get(objB)).toEqual([null, `bytes=${40 * 1024}-`]);
    expect(routeHits.get(objA)).toBe(1);
    expect(routeHits.get(objC)).toBe(1);

    expect(r.stdout).toContain('transfer interrupted');
    expect(r.stdout).toContain('Done: 3 downloaded, 0 already present, 1 failed');
    expect(r.stderr).toContain('not available for your account (HTTP 404)');
    expect(r.stderr).toContain('cosmos/expmap_f444w.fits');
  });

  it('second run: skips every complete file without asking the API, retries only the failure', async () => {
    const before = new Map(routeHits);
    const r = await run();
    expect(r.status).toBe(1);
    expect(routeHits.get(objA)).toBe(before.get(objA));
    expect(routeHits.get(objB)).toBe(before.get(objB));
    expect(routeHits.get(objD)).toBe(before.get(objD));
    expect(routeHits.get(objC)).toBe((before.get(objC) ?? 0) + 1);
    expect(r.stdout).toContain('Done: 0 downloaded, 3 already present, 1 failed');
  });

  it('re-fetches a file whose size no longer matches, and finishes a complete .part without re-downloading', async () => {
    // A: on disk with the wrong size (a partial from an older script, or a
    // product re-deployed since) — must be replaced by a fresh download.
    writeFileSync(outPath(objA), Buffer.alloc(100, 'x'));
    // B: a previous run died between the download completing and the rename.
    rmSync(outPath(objB));
    writeFileSync(`${outPath(objB)}.part`, bytesB);
    // C: now authorized.
    objects.set(objC, { bytes: Buffer.alloc(555, 'C') });

    const before = new Map(routeHits);
    const r = await run();
    expect(r.status).toBe(0);
    expect(readFileSync(outPath(objA)).equals(bytesA)).toBe(true);
    expect(readFileSync(outPath(objB)).equals(bytesB)).toBe(true);
    expect(statSync(outPath(objC)).size).toBe(555);
    expect(existsSync(`${outPath(objB)}.part`)).toBe(false);

    expect(routeHits.get(objA)).toBe((before.get(objA) ?? 0) + 1);
    expect(storeRanges.get(objA)?.at(-1)).toBeNull();
    // B's complete .part: the store answered 416 to the resume and the script
    // kept the bytes it had.
    expect(storeRanges.get(objB)?.at(-1)).toBe(`bytes=${bytesB.length}-`);
    expect(r.stdout).toContain('exists with 100 bytes, expected 65536');
    expect(r.stdout).toContain('Done: 3 downloaded, 1 already present, 0 failed');
  });
});
