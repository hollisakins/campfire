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

async function fetchRange(url: string, start: number, end: number, signal?: AbortSignal): Promise<ArrayBuffer> {
  const resp = await fetch(url, { headers: { Range: `bytes=${start}-${end}` }, signal });
  if (!(resp.status === 206 || resp.status === 200)) {
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
    dataBytes = (npix * Math.abs(bitpix)) / 8;
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
