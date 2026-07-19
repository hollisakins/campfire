// Minimal FITS writer for science cutouts (epic #337, Phase 5) — enough of the
// standard to emit an empty primary HDU + one float32 IMAGE extension per band,
// each carrying the pyramid level's own WCS with CRPIX shifted to the crop
// origin. No resampling ever happens on this path (pixels are the fpack tiles'
// values verbatim), so no WCS engine is needed server-side — astropy reads the
// shifted header exactly as it reads a fitsgl supertile's.
//
// Pure (no sharp/Next coupling); unit-tested by parsing the emitted bytes.

const CARD = 80;
const BLOCK = 2880;

/** Format one 80-char header card. Numbers are right-justified in the fixed
 *  20-char value field; strings are quoted (FITS 8-char minimum, '' escaping). */
function card(key: string, value: number | string | boolean | null, comment?: string): string {
  if (key === 'COMMENT' || key === 'HISTORY') {
    return `${key} ${String(value ?? '')}`.padEnd(CARD).slice(0, CARD);
  }
  let val: string;
  if (value === null) {
    val = ''.padStart(20);
  } else if (typeof value === 'boolean') {
    val = (value ? 'T' : 'F').padStart(20);
  } else if (typeof value === 'number') {
    val = formatNumber(value).padStart(20);
  } else {
    // Quoted string, min 8 chars inside the quotes, single-quotes doubled.
    const inner = value.replace(/'/g, "''").padEnd(8);
    val = `'${inner}'`;
  }
  let text = `${key.padEnd(8).slice(0, 8)}= ${val}`;
  if (comment) text += ` / ${comment}`;
  return text.padEnd(CARD).slice(0, CARD);
}

/** FITS-legal number text: integers verbatim; floats via repr with `E` exponent. */
function formatNumber(v: number): string {
  if (!Number.isFinite(v)) throw new Error(`non-finite FITS header value: ${v}`);
  if (Number.isInteger(v) && Math.abs(v) < 1e15) return String(v);
  let s = String(v);
  if (s.includes('e')) s = s.replace('e', 'E');
  else if (!s.includes('.')) s += '.0';
  return s;
}

/** Pad a header (array of cards) with END + blanks to a 2880-byte block. */
function headerBlock(cards: string[]): Buffer {
  const all = [...cards, 'END'.padEnd(CARD)];
  const n = Math.ceil((all.length * CARD) / BLOCK) * BLOCK;
  const buf = Buffer.alloc(n, 0x20); // space-filled
  all.forEach((c, i) => buf.write(c, i * CARD, 'ascii'));
  return buf;
}

/** Big-endian float32 data unit, zero-padded to a 2880-byte block. */
function dataBlock(data: Float32Array): Buffer {
  const nBytes = Math.ceil((data.length * 4) / BLOCK) * BLOCK;
  const buf = Buffer.alloc(nBytes, 0);
  const view = new DataView(buf.buffer, buf.byteOffset);
  for (let i = 0; i < data.length; i++) view.setFloat32(i * 4, data[i], false);
  return buf;
}

/** Structural keys the writer owns — never copied from a source WCS dict. */
const RESERVED = new Set([
  'SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3',
  'XTENSION', 'PCOUNT', 'GCOUNT', 'EXTEND', 'EXTNAME', 'END',
]);

export interface FitsBandCutout {
  /** Extension name (band, upper-cased on write). */
  name: string;
  /** Row-major pixels; row 0 is FITS row 1 (same axis order as the level). */
  data: Float32Array;
  width: number;
  height: number;
  /** The pyramid level's flat FITS WCS header dict (from the band manifest). */
  wcs: Record<string, unknown>;
  /** Level-pixel origin `(x0, y0)` of the crop — subtracted from CRPIX1/2. */
  origin: readonly [number, number];
  pixelScaleArcsec: number;
  /** Pyramid level index the pixels came from (0 = native). */
  levelZ: number;
}

export interface FitsCutoutMeta {
  /** Extra primary-header keys, written in order (e.g. FIELD, RA_CEN...). */
  primary?: Record<string, number | string | boolean>;
  /** COMMENT lines for the primary header (provenance notes). */
  comments?: string[];
}

/**
 * Encode band cutouts as a multi-extension FITS: empty primary HDU + one
 * float32 IMAGE extension per band (EXTNAME = band). Each extension carries
 * its level's WCS with CRPIX shifted by the crop origin.
 */
export function encodeFitsCutout(bands: FitsBandCutout[], meta: FitsCutoutMeta = {}): Buffer {
  if (bands.length === 0) throw new Error('encodeFitsCutout: no bands');

  const primaryCards = [
    card('SIMPLE', true, 'conforms to FITS standard'),
    card('BITPIX', 8),
    card('NAXIS', 0),
    card('EXTEND', true),
    card('ORIGIN', 'CAMPFIRE', 'FitsGL cutout service'),
    card('NEXTEND', bands.length, 'one IMAGE extension per band'),
  ];
  for (const [k, v] of Object.entries(meta.primary ?? {})) {
    if (!RESERVED.has(k.toUpperCase())) primaryCards.push(card(k.toUpperCase(), v));
  }
  for (const c of meta.comments ?? []) primaryCards.push(card('COMMENT', c));

  const parts: Buffer[] = [headerBlock(primaryCards)];

  for (const band of bands) {
    if (band.data.length !== band.width * band.height) {
      throw new Error(`band ${band.name}: data length != width*height`);
    }
    const cards = [
      card('XTENSION', 'IMAGE', 'IMAGE extension'),
      card('BITPIX', -32, 'IEEE single-precision float'),
      card('NAXIS', 2),
      card('NAXIS1', band.width),
      card('NAXIS2', band.height),
      card('PCOUNT', 0),
      card('GCOUNT', 1),
      card('EXTNAME', band.name.toUpperCase(), 'band'),
    ];
    for (const [k, v] of Object.entries(band.wcs)) {
      const key = k.toUpperCase();
      if (RESERVED.has(key)) continue;
      let value = v as number | string | boolean;
      if (key === 'CRPIX1') value = (v as number) - band.origin[0];
      if (key === 'CRPIX2') value = (v as number) - band.origin[1];
      if (typeof value !== 'number' && typeof value !== 'string' && typeof value !== 'boolean') continue;
      cards.push(card(key, value));
    }
    cards.push(card('LEVEL', band.levelZ, 'fitsgl pyramid level (0 = native)'));
    cards.push(card('PIXSCALE', band.pixelScaleArcsec, '[arcsec/px] level pixel scale'));
    cards.push(card('COMMENT',
      'Direct cutout from the FitsGL display pyramid (RICE_1, q=8, dither):'));
    cards.push(card('COMMENT',
      'lossy-quantized, ~0.03% photometry-faithful. Not the archival mosaic.'));
    parts.push(headerBlock(cards), dataBlock(band.data));
  }

  return Buffer.concat(parts);
}
