/**
 * Minimal FITS header parser.
 *
 * A FITS header is a sequence of 2880-byte blocks of 36 eighty-character ASCII
 * "cards", terminated by an `END` card and padded with spaces to the next 2880
 * boundary. We only need to read keyword values (ints, floats, strings, logicals)
 * and to know where the header ends (= where the data unit, or the next HDU,
 * begins), so we can walk the HDU chain of an uncompressed NIRCam exposure and
 * range-fetch just the SCI data block.
 *
 * Vendored from the owner's `fitsgl` project (`fitsgl-core/src/fpack/
 * fits-header.ts`) — CAMPFIRE renders per-exposure FITS in-browser without
 * depending on the (tile-pyramid-oriented) npm package (epic #261, N4). Kept
 * deliberately small: not a general FITS library.
 */

const CARD = 80;
const BLOCK = 2880;

/** Thrown when a header has no `END` card within the bytes provided so far. */
export class IncompleteHeaderError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'IncompleteHeaderError';
  }
}

function cardText(buf: Uint8Array, off: number): string {
  let s = '';
  for (let i = 0; i < CARD; i++) s += String.fromCharCode(buf[off + i]!);
  return s;
}

/** A parsed FITS header: keyword → raw value field, plus the data-unit offset. */
export class FitsHeader {
  /** byte offset of the data unit / next HDU (one past the END block, 2880-aligned). */
  readonly dataStart: number;
  private readonly values: Map<string, string>;

  constructor(values: Map<string, string>, dataStart: number) {
    this.values = values;
    this.dataStart = dataStart;
  }

  has(key: string): boolean {
    return this.values.has(key);
  }

  private token(key: string): string | undefined {
    const raw = this.values.get(key);
    if (raw === undefined) return undefined;
    return raw.split('/')[0]!.trim();
  }

  getInt(key: string): number | undefined {
    const tok = this.token(key);
    if (tok === undefined || tok === '') return undefined;
    const n = Number(tok);
    return Number.isFinite(n) ? Math.trunc(n) : undefined;
  }

  getFloat(key: string): number | undefined {
    const tok = this.token(key);
    if (tok === undefined || tok === '') return undefined;
    const n = Number(tok);
    return Number.isFinite(n) ? n : undefined;
  }

  getBool(key: string): boolean | undefined {
    const tok = this.token(key);
    if (tok === 'T') return true;
    if (tok === 'F') return false;
    return undefined;
  }

  /** Parse a quoted FITS string value (handling `''` escapes, trailing-space trimming). */
  getString(key: string): string | undefined {
    const raw = this.values.get(key);
    if (raw === undefined) return undefined;
    const q = raw.indexOf("'");
    if (q === -1) {
      const tok = raw.split('/')[0]!.trim();
      return tok === '' ? undefined : tok;
    }
    let out = '';
    let i = q + 1;
    for (; i < raw.length; i++) {
      const ch = raw[i]!;
      if (ch === "'") {
        if (raw[i + 1] === "'") {
          out += "'";
          i++;
        } else {
          break;
        }
      } else {
        out += ch;
      }
    }
    return out.replace(/\s+$/, '');
  }

  requireInt(key: string): number {
    const v = this.getInt(key);
    if (v === undefined) throw new Error(`FITS header: required integer keyword ${key} is missing`);
    return v;
  }
}

/**
 * Parse a FITS header starting at byte `start` in `buf`.
 *
 * @throws {IncompleteHeaderError} if no `END` card is found before `buf` runs
 *   out — the caller should fetch more bytes and retry.
 */
export function parseFitsHeader(buf: Uint8Array, start: number): FitsHeader {
  const values = new Map<string, string>();
  let off = start;
  for (;;) {
    if (off + BLOCK > buf.length) {
      throw new IncompleteHeaderError(
        `FITS header starting at byte ${start} has no END card within the ` +
          `available ${buf.length} bytes`,
      );
    }
    let ended = false;
    for (let i = 0; i < BLOCK; i += CARD) {
      const card = cardText(buf, off + i);
      const key = card.slice(0, 8).trim();
      if (key === 'END') {
        ended = true;
        break;
      }
      // A value card has "= " in columns 9–10. Keep only the first occurrence.
      if (key.length > 0 && card[8] === '=' && card[9] === ' ' && !values.has(key)) {
        values.set(key, card.slice(10));
      }
    }
    off += BLOCK;
    if (ended) break;
  }
  return new FitsHeader(values, off);
}

/** Round `n` up to the next 2880-byte FITS block boundary. */
export function padToBlock(n: number): number {
  return Math.ceil(n / BLOCK) * BLOCK;
}
