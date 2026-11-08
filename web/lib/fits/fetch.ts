/**
 * Fetch just the SCI extension of an uncompressed NIRCam canonical exposure and
 * decode it to a native-endian Float32Array — without downloading the whole
 * ~122 MB file (epic #261, N4, decision D7).
 *
 * Two HTTP range requests against the file's source url (`resolveFitsSource`:
 * the delivery front when configured, else the admin `/api/nircam-fits` proxy):
 *   1. the first ~64 KB → walk the HDU header chain (PRIMARY → SCI) to learn
 *      SCI's dimensions and its data-block byte offset;
 *   2. the SCI data block itself (2048² × 4 B ≈ 16 MB).
 * FITS float32 is big-endian, so the block is byte-swapped in place before it
 * becomes a Float32Array. NaN bad pixels are preserved bit-exact (the shader
 * paints them as background).
 */

import { parseFitsHeader, FitsHeader, IncompleteHeaderError, padToBlock } from './fits-header';
import { decodeGzip2Tile, gunzip } from '@fitsgl/core/internal';

export interface SciImage {
  /** Native-endian SCI pixels, row-major, length `width * height`. Row 0 is the FITS first row. */
  data: Float32Array;
  width: number;
  height: number;
}

/** A named image HDU (e.g. S2D_SCI) was walked past without being found — the
 * canonical file has no rectified view for this cutout. Distinct from
 * IncompleteHeaderError (window too short, grow) so callers render a fallback. */
export class HduNotFoundError extends Error {}

/** One S2D cutout cell for the NIRSpec nods renderer: the rectified slitlet
 * pixels plus the header cards the renderer overlays (STKSHTRS on PRIMARY,
 * SHUTSTA on the live SCI ext) — all captured from the one header window walk. */
export interface S2dCell {
  data: Float32Array;
  width: number;
  height: number;
  primaryHeader: FitsHeader;
  sciHeader: FitsHeader | null;
}

// The S2D_* HDUs sit behind the slit arrays (SCI/ERR/DQ/VAR*×3 + PRE_BKGSUB_*×5),
// so its header can be well past the 1 MB NIRCam cap. Cutouts are small rectified
// slitlets, so a generous contiguous cap virtually always reaches it in one grow
// loop; if not, we degrade to HduNotFoundError (the OQ-6 "no rectified view" cell).
const MAX_S2D_HEADER_BYTES = 16 * 1024 * 1024;

/** Initial header-window size; grown on IncompleteHeaderError (large PRIMARY headers). */
const INITIAL_HEADER_BYTES = 64 * 1024;
const MAX_HEADER_BYTES = 1024 * 1024;

/** Resolved source urls per storage key. A front url is stable for at least
 * one presign window (6 h); an hour here keeps a triage session on one url
 * without re-asking the app for every nod cell that shares an exposure. */
const resolvedSources = new Map<string, { url: string; at: number }>();
const RESOLVED_SOURCE_TTL_MS = 60 * 60 * 1000;

/**
 * The url to range-fetch a canonical FITS product from. Asks
 * `/api/nircam-fits?key=…&resolve=1` for a delivery-front url (perf T2-D1,
 * #507); when the front is off, the file is local, or the resolve fails, the
 * same-origin proxy url is returned and the proxy streams the ranges.
 */
export async function resolveFitsSource(key: string, signal?: AbortSignal): Promise<string> {
  const proxyUrl = `/api/nircam-fits?key=${encodeURIComponent(key)}`;
  const hit = resolvedSources.get(key);
  if (hit && Date.now() - hit.at < RESOLVED_SOURCE_TTL_MS) return hit.url;
  try {
    const res = await fetch(`${proxyUrl}&resolve=1`, { signal });
    if (!res.ok) return proxyUrl;
    const body = (await res.json()) as { url?: string | null };
    const url = typeof body.url === 'string' && body.url ? body.url : proxyUrl;
    resolvedSources.set(key, { url, at: Date.now() });
    return url;
  } catch (err) {
    if (signal?.aborted) throw err;
    return proxyUrl;
  }
}

/** Drop the memoized source that resolved to `url`, so the next resolve
 * re-asks the app instead of replaying a front url that stopped answering. */
function forgetResolvedSource(url: string): void {
  for (const [key, entry] of resolvedSources) {
    if (entry.url === url) resolvedSources.delete(key);
  }
}

async function fetchRange(url: string, start: number, end: number, signal?: AbortSignal): Promise<ArrayBuffer> {
  let resp: Response;
  try {
    resp = await fetch(url, { headers: { Range: `bytes=${start}-${end}` }, signal });
  } catch (err) {
    if (!signal?.aborted) forgetResolvedSource(url);
    throw err;
  }
  if (!(resp.status === 206 || resp.status === 200)) {
    forgetResolvedSource(url);
    throw new Error(`FITS range fetch failed: HTTP ${resp.status} for bytes ${start}-${end}`);
  }
  return resp.arrayBuffer();
}

interface HduHeader {
  header: FitsHeader;
  dataStart: number;
  nextHdu: number;
  extname?: string;
}

/** Parse one HDU header at `start`; compute its data size to locate the next HDU. */
function hduAt(buf: Uint8Array, start: number): HduHeader {
  const header = parseFitsHeader(buf, start);
  const naxis = header.getInt('NAXIS') ?? 0;
  const bitpix = header.getInt('BITPIX') ?? 0;
  let dataBytes = 0;
  if (naxis > 0) {
    let npix = 1;
    for (let a = 1; a <= naxis; a++) npix *= header.getInt(`NAXIS${a}`) ?? 1;
    // A BINTABLE's heap (PCOUNT: variable-length arrays, tile-compressed images,
    // the rate file's ASDF) follows its main table inside the same data unit.
    const pcount = header.getInt('PCOUNT') ?? 0;
    const gcount = header.getInt('GCOUNT') ?? 1;
    dataBytes = gcount * (pcount + (npix * Math.abs(bitpix)) / 8);
  }
  return {
    header,
    dataStart: header.dataStart,
    nextHdu: header.dataStart + padToBlock(dataBytes),
    extname: header.getString('EXTNAME'),
  };
}

/** Walk the header chain in `buf` to the SCI image extension. */
function findSci(buf: Uint8Array): { header: FitsHeader; dataStart: number } {
  let off = 0;
  for (let i = 0; i < 32; i++) {
    if (off + 2880 > buf.length) {
      throw new IncompleteHeaderError(`SCI header not reached within ${buf.length} bytes`);
    }
    const hdu = hduAt(buf, off);
    // The primary HDU (i === 0) has no EXTNAME; SCI is the first named IMAGE ext.
    if (hdu.extname === 'SCI') return { header: hdu.header, dataStart: hdu.dataStart };
    off = hdu.nextHdu;
  }
  throw new Error('No SCI extension found in the first 32 HDUs');
}

/** Byte-swap a big-endian float32 block in place, then view it as Float32Array. */
function swapToFloat32(buffer: ArrayBuffer): Float32Array {
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i + 3 < bytes.length; i += 4) {
    const b0 = bytes[i]!;
    const b1 = bytes[i + 1]!;
    bytes[i] = bytes[i + 3]!;
    bytes[i + 1] = bytes[i + 2]!;
    bytes[i + 2] = b1;
    bytes[i + 3] = b0;
  }
  return new Float32Array(buffer);
}

/**
 * Fetch + decode the SCI extension of the exposure served at `fitsUrl`
 * (`/api/nircam-fits?key=...`). Throws on a missing/compressed/non-float32 SCI.
 */
export async function fetchSciImage(fitsUrl: string, signal?: AbortSignal): Promise<SciImage> {
  // 1. Header window → SCI dimensions + data offset (grow on IncompleteHeaderError).
  let headerBytes = INITIAL_HEADER_BYTES;
  let sci: { header: FitsHeader; dataStart: number } | null = null;
  for (;;) {
    const head = new Uint8Array(await fetchRange(fitsUrl, 0, headerBytes - 1, signal));
    try {
      sci = findSci(head);
      break;
    } catch (e) {
      if (e instanceof IncompleteHeaderError && headerBytes < MAX_HEADER_BYTES) {
        headerBytes = Math.min(headerBytes * 4, MAX_HEADER_BYTES);
        continue;
      }
      throw e;
    }
  }

  const { header, dataStart } = sci;
  const width = header.requireInt('NAXIS1');
  const height = header.requireInt('NAXIS2');
  const bitpix = header.requireInt('BITPIX');
  if (bitpix !== -32) {
    throw new Error(`SCI is not float32 (BITPIX=${bitpix}); N4 handles uncompressed float32 only`);
  }
  if (header.has('ZIMAGE')) {
    throw new Error('SCI is fpack-compressed (ZIMAGE); N4 handles uncompressed SCI only');
  }

  // 2. SCI data block.
  const sciBytes = width * height * 4;
  const block = await fetchRange(fitsUrl, dataStart, dataStart + sciBytes - 1, signal);
  if (block.byteLength < sciBytes) {
    throw new Error(`Short SCI read: got ${block.byteLength} of ${sciBytes} bytes`);
  }
  const data = swapToFloat32(block.byteLength === sciBytes ? block : block.slice(0, sciBytes));
  return { data, width, height };
}

/** Walk `buf`'s HDU chain to `extname`, capturing the PRIMARY (i=0) and live-SCI
 * headers along the way. Throws IncompleteHeaderError if the buffer runs out
 * mid-walk (caller grows), or HduNotFoundError if all HDUs are walked without a
 * match (absent → fallback cell). */
function findS2dHdu(buf: Uint8Array, extname: string): {
  header: FitsHeader; dataStart: number; primaryHeader: FitsHeader; sciHeader: FitsHeader | null;
} {
  let off = 0;
  let primaryHeader: FitsHeader | null = null;
  let sciHeader: FitsHeader | null = null;
  for (let i = 0; i < 64; i++) {
    if (off + 2880 > buf.length) {
      throw new IncompleteHeaderError(`${extname} header not reached within ${buf.length} bytes`);
    }
    const hdu = hduAt(buf, off);
    if (i === 0) primaryHeader = hdu.header;
    if (hdu.extname === 'SCI') sciHeader = hdu.header;
    if (hdu.extname === extname) {
      return { header: hdu.header, dataStart: hdu.dataStart, primaryHeader: primaryHeader!, sciHeader };
    }
    off = hdu.nextHdu;
  }
  throw new HduNotFoundError(`No ${extname} extension in the first 64 HDUs`);
}

/**
 * Fetch + decode a named S2D image HDU (default `S2D_SCI`) from a canonical
 * NIRSpec spectrum-exposure served at `fitsUrl`, plus the header cards the nods
 * renderer overlays. Throws {@link HduNotFoundError} when the rectified view is
 * absent (OQ-6), or on a compressed/non-float32 HDU.
 */
export async function fetchS2dCell(
  fitsUrl: string,
  opts?: { extname?: string; signal?: AbortSignal },
): Promise<S2dCell> {
  const extname = opts?.extname ?? 'S2D_SCI';
  const signal = opts?.signal;

  let headerBytes = INITIAL_HEADER_BYTES;
  let found: ReturnType<typeof findS2dHdu> | null = null;
  for (;;) {
    const head = new Uint8Array(await fetchRange(fitsUrl, 0, headerBytes - 1, signal));
    try {
      found = findS2dHdu(head, extname);
      break;
    } catch (e) {
      if (e instanceof IncompleteHeaderError && headerBytes < MAX_S2D_HEADER_BYTES) {
        headerBytes = Math.min(headerBytes * 4, MAX_S2D_HEADER_BYTES);
        continue;
      }
      if (e instanceof IncompleteHeaderError) {
        throw new HduNotFoundError(`${extname} not reached within ${headerBytes} bytes`);
      }
      throw e; // HduNotFoundError or a parse error
    }
  }

  const { header, dataStart, primaryHeader, sciHeader } = found;
  const width = header.requireInt('NAXIS1');
  const height = header.requireInt('NAXIS2');
  const bitpix = header.requireInt('BITPIX');
  if (bitpix !== -32) {
    throw new Error(`${extname} is not float32 (BITPIX=${bitpix})`);
  }
  if (header.has('ZIMAGE')) {
    throw new Error(`${extname} is fpack-compressed (ZIMAGE)`);
  }

  const bytes = width * height * 4;
  const block = await fetchRange(fitsUrl, dataStart, dataStart + bytes - 1, signal);
  if (block.byteLength < bytes) {
    throw new Error(`Short ${extname} read: got ${block.byteLength} of ${bytes} bytes`);
  }
  const data = swapToFloat32(block.byteLength === bytes ? block : block.slice(0, bytes));
  return { data, width, height, primaryHeader, sciHeader };
}

/** Walk `buf`'s HDU chain to the HDU named `extname`. Throws IncompleteHeaderError
 * when the buffer runs out mid-walk (caller grows) or HduNotFoundError when every
 * HDU in the buffer was walked without a match. */
function findNamedHdu(buf: Uint8Array, extname: string): HduHeader {
  let off = 0;
  for (let i = 0; i < 64; i++) {
    if (off + 2880 > buf.length) {
      throw new IncompleteHeaderError(`${extname} header not reached within ${buf.length} bytes`);
    }
    const hdu = hduAt(buf, off);
    if (hdu.extname === extname) return hdu;
    off = hdu.nextHdu;
  }
  throw new HduNotFoundError(`No ${extname} extension in the first 64 HDUs`);
}

const DECODE_BATCH = 64;

/** Byte width of one BINTABLE field from its TFORMn (`rT`, e.g. `1PB(3688)`, `2E`). */
export function tformWidth(tform: string): number {
  const m = /^\s*(\d*)([LXBIJKAEDCMPQ])/.exec(tform);
  if (!m) throw new Error(`Unsupported TFORM ${JSON.stringify(tform)}`);
  const repeat = m[1] === '' ? 1 : Number(m[1]);
  const code = m[2]!;
  if (code === 'X') return Math.ceil(repeat / 8);
  const size: Record<string, number> = {
    L: 1, B: 1, A: 1, I: 2, J: 4, E: 4, K: 8, D: 8, C: 8, M: 16, P: 8, Q: 16,
  };
  return repeat * size[code]!;
}

/**
 * Fetch + decode a tile-compressed image HDU (`ZIMAGE = T`) named `extname`, e.g.
 * the 2-D master background (`MBKG2D`) of a `_mbkg.fits`: lossless GZIP_2 float32,
 * one tile per detector row. One range request for the header window (grown as
 * needed), one for the HDU's whole data unit (tile table + heap); each tile is
 * gunzipped and byte-unshuffled by FitsGL's `decodeGzip2Tile`. Only GZIP_2 on
 * float32 without quantization is accepted (RICE_1 / quantized floats are
 * rejected), which is what the pipeline writes for it.
 */
export async function fetchCompressedImage(
  fitsUrl: string,
  extname: string,
  signal?: AbortSignal,
): Promise<SciImage> {
  let headerBytes = INITIAL_HEADER_BYTES;
  let hdu: HduHeader;
  for (;;) {
    const head = new Uint8Array(await fetchRange(fitsUrl, 0, headerBytes - 1, signal));
    try {
      hdu = findNamedHdu(head, extname);
      break;
    } catch (e) {
      if (!(e instanceof IncompleteHeaderError)) throw e;
      // A short read is the end of the file: every HDU was walked.
      if (head.length < headerBytes || headerBytes >= MAX_S2D_HEADER_BYTES) {
        throw new HduNotFoundError(`No ${extname} extension found`);
      }
      headerBytes = Math.min(headerBytes * 4, MAX_S2D_HEADER_BYTES);
    }
  }

  const h = hdu.header;
  if (h.getBool('ZIMAGE') !== true) throw new Error(`${extname} is not a tile-compressed image`);
  const cmp = h.getString('ZCMPTYPE');
  if (cmp !== 'GZIP_2') throw new Error(`${extname}: compression ${cmp ?? '?'} is not supported (GZIP_2 only)`);
  if (h.requireInt('ZBITPIX') !== -32) throw new Error(`${extname} is not float32 (ZBITPIX=${h.getInt('ZBITPIX')})`);
  const width = h.requireInt('ZNAXIS1');
  const height = h.requireInt('ZNAXIS2');
  const tileW = h.getInt('ZTILE1') ?? width;
  const tileH = h.getInt('ZTILE2') ?? 1;
  const rowBytes = h.requireInt('NAXIS1');
  const nRows = h.requireInt('NAXIS2');
  const pcount = h.getInt('PCOUNT') ?? 0;
  const heapStart = h.getInt('THEAP') ?? rowBytes * nRows;

  // Column layout: where COMPRESSED_DATA (and the per-tile lossless fallback,
  // GZIP_COMPRESSED_DATA) sit in each row, and their descriptor kind (P/Q).
  const nFields = h.requireInt('TFIELDS');
  const cols = new Map<string, { offset: number; wide: boolean }>();
  let off = 0;
  for (let n = 1; n <= nFields; n++) {
    const tform = h.getString(`TFORM${n}`) ?? '';
    const name = (h.getString(`TTYPE${n}`) ?? '').toUpperCase();
    cols.set(name, { offset: off, wide: /^\s*\d*Q/.test(tform) });
    off += tformWidth(tform);
  }
  if (cols.has('ZSCALE')) throw new Error(`${extname} is quantized; only lossless GZIP_2 is supported`);
  const data = cols.get('COMPRESSED_DATA');
  if (!data) throw new Error(`${extname} has no COMPRESSED_DATA column`);
  const fallback = cols.get('GZIP_COMPRESSED_DATA');

  const span = rowBytes * nRows + pcount;
  const buf = await fetchRange(fitsUrl, hdu.dataStart, hdu.dataStart + span - 1, signal);
  if (buf.byteLength < span) throw new Error(`Short ${extname} read: got ${buf.byteLength} of ${span} bytes`);
  const view = new DataView(buf);
  const bytes = new Uint8Array(buf);
  const descriptor = (row: number, col: { offset: number; wide: boolean }): [number, number] => {
    const at = row * rowBytes + col.offset;
    return col.wide
      ? [Number(view.getBigInt64(at)), Number(view.getBigInt64(at + 8))]
      : [view.getInt32(at), view.getInt32(at + 4)];
  };

  const nTilesX = Math.ceil(width / tileW);
  const nTilesY = Math.ceil(height / tileH);
  if (nTilesX * nTilesY !== nRows) {
    throw new Error(`${extname}: ${nRows} tile rows for a ${nTilesX} x ${nTilesY} tile grid`);
  }
  const out = new Float32Array(width * height);
  const decodeRow = async (row: number) => {
    const tx = row % nTilesX;
    const ty = Math.floor(row / nTilesX);
    const w = Math.min(tileW, width - tx * tileW);
    const th = Math.min(tileH, height - ty * tileH);
    let [len, at] = descriptor(row, data);
    let tile: Float32Array;
    if (len > 0) {
      const start = heapStart + at;
      tile = await decodeGzip2Tile(bytes.subarray(start, start + len), w * th);
    } else if (fallback) {
      // Per-tile lossless fallback: raw big-endian floats, gzipped, no shuffle.
      [len, at] = descriptor(row, fallback);
      const start = heapStart + at;
      const raw = await gunzip(bytes.subarray(start, start + len));
      const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
      tile = new Float32Array(w * th);
      for (let i = 0; i < tile.length; i++) tile[i] = dv.getFloat32(i * 4);
    } else {
      tile = new Float32Array(w * th).fill(NaN);
    }
    for (let y = 0; y < th; y++) {
      out.set(tile.subarray(y * w, (y + 1) * w), (ty * tileH + y) * width + tx * tileW);
    }
  };
  // a bounded number of tiles in flight (MBKG2D has one per detector row, 2048)
  for (let start = 0; start < nRows; start += DECODE_BATCH) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const end = Math.min(start + DECODE_BATCH, nRows);
    await Promise.all(Array.from({ length: end - start }, (_, i) => decodeRow(start + i)));
  }
  return { data: out, width, height };
}

/** Rate minus master background, pixel by pixel; NaN where either is NaN. */
export function subtractImages(a: SciImage, b: SciImage): SciImage {
  if (a.width !== b.width || a.height !== b.height) {
    throw new Error(`Image sizes differ: ${a.width}x${a.height} vs ${b.width}x${b.height}`);
  }
  const data = new Float32Array(a.data.length);
  for (let i = 0; i < data.length; i++) data[i] = a.data[i]! - b.data[i]!;
  return { data, width: a.width, height: a.height };
}
